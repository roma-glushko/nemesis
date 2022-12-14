from __future__ import annotations

import logging
from enum import auto, Enum
from time import time
from typing import Awaitable, Callable, Optional, Tuple, Iterable, List
from urllib.parse import unquote

from nemesis.events import Body, EndBody, Event, Request, Response, StreamClosed
from nemesis.task_group import TaskGroup

from nemesis.typing import (
    ASGIFramework,
    ASGISendEvent,
    HTTPResponseStartEvent,
    HTTPScope,
)
from nemesis.workers import WorkerContext

PUSH_VERSIONS = {"2", "3"}

logger = logging.getLogger(__name__)


class ASGIHTTPState(Enum):
    # The ASGI Spec is clear that a response should not start till the
    # framework has sent at least one body message hence why this
    # state tracking is required.
    REQUEST = auto()
    RESPONSE = auto()
    CLOSED = auto()


class UnexpectedMessageError(Exception):
    def __init__(self, state: Enum, message_type: str) -> None:
        super().__init__(f"Unexpected message type, {message_type} given the state {state}")


def suppress_body(method: str, status_code: int) -> bool:
    return method == "HEAD" or 100 <= status_code < 200 or status_code in {204, 304, 412}


def build_and_validate_headers(headers: Iterable[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    # Validates that the header name and value are bytes
    validated_headers: List[Tuple[bytes, bytes]] = []
    for name, value in headers:
        if name[0] == b":"[0]:
            raise ValueError("Pseudo headers are not valid")
        validated_headers.append((bytes(name).lower().strip(), bytes(value).strip()))
    return validated_headers


class HTTPStream:
    def __init__(
        self,
        app: ASGIFramework,
        context: WorkerContext,
        task_group: TaskGroup,
        ssl: bool,
        client: Optional[Tuple[str, int]],
        server: Optional[Tuple[str, int]],
        send: Callable[[Event], Awaitable[None]],
        stream_id: int,
    ) -> None:
        self.app = app
        self.client = client
        self.closed = False
        self.context = context
        self.response: HTTPResponseStartEvent
        self.scope: HTTPScope
        self.send = send
        self.scheme = "https" if ssl else "http"
        self.server = server
        self.start_time: float
        self.state = ASGIHTTPState.REQUEST
        self.stream_id = stream_id
        self.task_group = task_group

    @property
    def idle(self) -> bool:
        return False

    async def handle(self, event: Event) -> None:
        if self.closed:
            return
        elif isinstance(event, Request):
            self.start_time = time()
            path, _, query_string = event.raw_path.partition(b"?")

            self.scope = {
                "type": "http",
                "http_version": event.http_version,
                "asgi": {"spec_version": "2.1"},
                "method": event.method,
                "scheme": self.scheme,
                "path": unquote(path.decode("ascii")),
                "raw_path": path,
                "query_string": query_string,
                "root_path": "",
                "headers": event.headers,
                "client": self.client,
                "server": self.server,
                "extensions": {},
            }

            if event.http_version in PUSH_VERSIONS:
                self.scope["extensions"]["http.response.push"] = {}

            # if valid_server_name(self.config, event):
            #     self.app_put = await self.task_group.spawn_app(
            #         self.app, self.config, self.scope, self.app_send
            #     )
            # else:
            #     await self._send_error_response(404)
            #     self.closed = True

            self.app_put = await self.task_group.spawn_app(
                self.app, self.scope, self.app_send
            )

        elif isinstance(event, Body):
            await self.app_put(
                {"type": "http.request", "body": bytes(event.data), "more_body": True}
            )
        elif isinstance(event, EndBody):
            await self.app_put({"type": "http.request", "body": b"", "more_body": False})
        elif isinstance(event, StreamClosed):
            self.closed = True
            if self.app_put is not None:
                await self.app_put({"type": "http.disconnect"})  # type: ignore

    async def app_send(self, message: Optional[ASGISendEvent]) -> None:
        if self.closed:
            # Allow app to finish after close
            return

        if message is None:  # ASGI App has finished sending messages
            # Cleanup if required
            if self.state == ASGIHTTPState.REQUEST:
                await self._send_error_response(500)
            await self.send(StreamClosed(stream_id=self.stream_id))
        else:
            if message["type"] == "http.response.start" and self.state == ASGIHTTPState.REQUEST:
                self.response = message
            elif (
                    message["type"] == "http.response.push"
                    and self.scope["http_version"] in PUSH_VERSIONS
            ):
                if not isinstance(message["path"], str):
                    raise TypeError(f"{message['path']} should be a str")
                headers = [(b":scheme", self.scope["scheme"].encode())]
                for name, value in self.scope["headers"]:
                    if name == b"host":
                        headers.append((b":authority", value))
                headers.extend(build_and_validate_headers(message["headers"]))
                await self.send(
                    Request(
                        stream_id=self.stream_id,
                        headers=headers,
                        http_version=self.scope["http_version"],
                        method="GET",
                        raw_path=message["path"].encode(),
                    )
                )
            elif message["type"] == "http.response.body" and self.state in {
                ASGIHTTPState.REQUEST,
                ASGIHTTPState.RESPONSE,
            }:
                if self.state == ASGIHTTPState.REQUEST:
                    headers = build_and_validate_headers(self.response.get("headers", []))
                    await self.send(
                        Response(
                            stream_id=self.stream_id,
                            headers=headers,
                            status_code=int(self.response["status"]),
                        )
                    )
                    self.state = ASGIHTTPState.RESPONSE

                if (
                        not suppress_body(self.scope["method"], int(self.response["status"]))
                        and message.get("body", b"") != b""
                ):
                    await self.send(
                        Body(stream_id=self.stream_id, data=bytes(message.get("body", b"")))
                    )

                if not message.get("more_body", False):
                    if self.state != ASGIHTTPState.CLOSED:
                        self.state = ASGIHTTPState.CLOSED
                        # await self.config.log.access(
                        #     self.scope, self.response, time() - self.start_time
                        # )
                        await self.send(EndBody(stream_id=self.stream_id))
                        await self.send(StreamClosed(stream_id=self.stream_id))
            else:
                raise UnexpectedMessageError(self.state, message["type"])

    async def _send_error_response(self, status_code: int) -> None:
        await self.send(
            Response(
                stream_id=self.stream_id,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
                status_code=status_code,
            )
        )
        await self.send(EndBody(stream_id=self.stream_id))
        self.state = ASGIHTTPState.CLOSED
        # await self.config.log.access(
        #     self.scope, {"status": status_code, "headers": []}, time() - self.start_time
        # )
