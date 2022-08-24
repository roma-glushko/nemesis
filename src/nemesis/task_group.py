import asyncio
import inspect
import logging
import weakref
from types import TracebackType
from typing import Callable, Any, Awaitable, Optional, cast

from nemesis.typing import ASGIFramework, ASGIReceiveEvent, ASGISendEvent, Scope, ASGIReceiveCallable, \
    ASGISendCallable, ASGI2Framework, ASGI3Framework

logger = logging.getLogger(__name__)

MAX_APP_QUEUE_SIZE: int = 10


def _is_asgi_2(app: ASGIFramework) -> bool:
    if inspect.isclass(app):
        return True

    if hasattr(app, "__call__") and inspect.iscoroutinefunction(app.__call__):  # type: ignore
        return False

    return not inspect.iscoroutinefunction(app)


async def invoke_asgi(
        app: ASGIFramework, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
) -> None:
    if _is_asgi_2(app):
        scope["asgi"]["version"] = "2.0"
        app = cast(ASGI2Framework, app)
        asgi_instance = app(scope)
        return await asgi_instance(receive, send)

    scope["asgi"]["version"] = "3.0"
    app = cast(ASGI3Framework, app)
    await app(scope, receive, send)


async def _handle(
        app: ASGIFramework,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: Callable[[Optional[ASGISendEvent]], Awaitable[None]],
) -> None:
    try:
        await invoke_asgi(app, scope, receive, send)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Error in ASGI Framework")
    finally:
        await send(None)


class TaskGroup:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._tasks: weakref.WeakSet = weakref.WeakSet()
        self._exiting = False

    async def spawn_app(
        self,
        app: ASGIFramework,
        scope: Scope,
        send: Callable[[Optional[ASGISendEvent]], Awaitable[None]],
    ) -> Callable[[ASGIReceiveEvent], Awaitable[None]]:
        app_queue: asyncio.Queue[ASGIReceiveEvent] = asyncio.Queue(MAX_APP_QUEUE_SIZE)

        self.spawn(_handle, app, scope, app_queue.get, send)

        return app_queue.put

    def spawn(self, func: Callable, *args: Any) -> None:
        if self._exiting:
            raise RuntimeError("Spawning whilst exiting")

        self._tasks.add(self._loop.create_task(func(*args)))

    async def __aenter__(self) -> "TaskGroup":
        return self

    async def __aexit__(self, exc_type: type, exc_value: BaseException, tb: TracebackType) -> None:
        self._exiting = True
        if exc_type is not None:
            self._cancel_tasks()

        try:
            task = asyncio.gather(*self._tasks)
            await task
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _cancel_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
