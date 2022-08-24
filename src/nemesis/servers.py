import asyncio
import socket
from ssl import SSLError
from typing import Optional, Generator, Any, Tuple, Callable

from nemesis.events import Closed, RawData, Event, Updated
from nemesis.protocols.h11p import H11Protocol
from nemesis.task_group import TaskGroup
from nemesis.typing import ASGIFramework
from nemesis.worker import WorkerContext

SECONDS = 1
MAX_RECV = 2 ** 16
READ_TIMEOUT: Optional[int] = None
KEEP_ALIVE_TIMEOUT = 5 * SECONDS

def parse_socket_addr(family: int, address: tuple) -> Optional[Tuple[str, int]]:
    if family == socket.AF_INET:
        return address  # type: ignore
    elif family == socket.AF_INET6:
        return (address[0], address[1])
    else:
        return None


class TCPServer:
    def __init__(
            self,
            app: ASGIFramework,
            loop: asyncio.AbstractEventLoop,
            context: WorkerContext,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
    ) -> None:
        self.app = app
        self.context = context
        self.loop = loop
        self.protocol: H11Protocol
        self.reader = reader
        self.writer = writer
        self.send_lock = asyncio.Lock()
        self.timeout_lock = asyncio.Lock()

        self._keep_alive_timeout_handle: Optional[asyncio.Task] = None

    def __await__(self) -> Generator[Any, None, None]:
        return self.run().__await__()

    async def run(self) -> None:
        socket = self.writer.get_extra_info("socket")
        try:
            client = parse_socket_addr(socket.family, socket.getpeername())
            server = parse_socket_addr(socket.family, socket.getsockname())

            ssl = False

            async with TaskGroup(self.loop) as task_group:
                self.protocol = H11Protocol(
                    self.app,
                    self.context,
                    task_group,
                    ssl,
                    client,
                    server,
                    self.protocol_send,
                )
                await self.protocol.initiate()
                await self._start_keep_alive_timeout()
                await self._read_data()
        except OSError:
            pass
        finally:
            await self._close()

    async def protocol_send(self, event: Event) -> None:
        if isinstance(event, RawData):
            async with self.send_lock:
                try:
                    self.writer.write(event.data)
                    await self.writer.drain()
                except (ConnectionError, RuntimeError):
                    await self.protocol.handle(Closed())
        elif isinstance(event, Closed):
            await self._close()
            await self.protocol.handle(Closed())
        elif isinstance(event, Updated):
            if event.idle:
                await self._start_keep_alive_timeout()
            else:
                await self._stop_keep_alive_timeout()

    async def _read_data(self) -> None:
        while not self.reader.at_eof():
            try:
                data = await asyncio.wait_for(self.reader.read(MAX_RECV), READ_TIMEOUT)
            except (
                    ConnectionError,
                    OSError,
                    asyncio.TimeoutError,
                    TimeoutError,
                    SSLError,
            ):
                await self.protocol.handle(Closed())
                break
            else:
                await self.protocol.handle(RawData(data))

    async def _close(self) -> None:
        try:
            self.writer.write_eof()
        except (NotImplementedError, OSError, RuntimeError):
            pass  # Likely SSL connection

        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass  # Already closed

        await self._stop_keep_alive_timeout()

    async def _start_keep_alive_timeout(self) -> None:
        async with self.timeout_lock:
            if self._keep_alive_timeout_handle is None:
                self._keep_alive_timeout_handle = self.loop.create_task(
                    _call_later(KEEP_ALIVE_TIMEOUT, self._timeout)
                )

    async def _timeout(self) -> None:
        await self.protocol.handle(Closed())
        self.writer.close()

    async def _stop_keep_alive_timeout(self) -> None:
        async with self.timeout_lock:
            if self._keep_alive_timeout_handle is not None:
                self._keep_alive_timeout_handle.cancel()
                try:
                    await self._keep_alive_timeout_handle
                except asyncio.CancelledError:
                    pass


async def _call_later(timeout: float, callback: Callable) -> None:
    await asyncio.sleep(timeout)
    await asyncio.shield(callback())
