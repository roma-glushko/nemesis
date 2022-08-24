import asyncio
import logging
import socket
import weakref
from typing import Union, Optional, Callable, Awaitable, Any, Sequence

from nemesis.servers import TCPServer
from nemesis.typing import ASGIFramework

SECONDS: int = 1
BACKLOG: int = 100
GRACEFUL_TIMEOUT: int = 3 * SECONDS

logger = logging.getLogger(__name__)


def repr_socket_addr(family: int, address: tuple) -> str:
    if family == socket.AF_INET:
        return f"{address[0]}:{address[1]}"
    elif family == socket.AF_INET6:
        return f"[{address[0]}]:{address[1]}"
    elif family == socket.AF_UNIX:
        return f"unix:{address}"
    else:
        return f"{address}"


class WorkerContext:
    def __init__(self) -> None:
        self.terminated = False

    @staticmethod
    async def sleep(wait: Union[float, int]) -> None:
        return await asyncio.sleep(wait)

    @staticmethod
    def time() -> float:
        return asyncio.get_event_loop().time()


async def serve_worker(
    app: ASGIFramework,
    *,
    sockets: Sequence[socket.socket],
    shutdown_trigger: Optional[Callable[..., Awaitable[None]]] = None,
) -> None:
    loop = asyncio.get_event_loop()

    # if shutdown_trigger is None:
    #     signal_event = asyncio.Event()
    #
    #     def _signal_handler(*_: Any) -> None:  # noqa: N803
    #         signal_event.set()
    #
    #     for signal_name in {"SIGINT", "SIGTERM", "SIGBREAK"}:
    #         if hasattr(signal, signal_name):
    #             try:
    #                 loop.add_signal_handler(getattr(signal, signal_name), _signal_handler)
    #             except NotImplementedError:
    #                 # Add signal handler may not be implemented on Windows
    #                 signal.signal(getattr(signal, signal_name), _signal_handler)
    #
    #     shutdown_trigger = signal_event.wait  # type: ignore

    lifespan = Lifespan(app)

    lifespan_task = loop.create_task(lifespan.handle_lifespan())
    await lifespan.wait_for_startup()

    if lifespan_task.done():
        exception = lifespan_task.exception()
        if exception is not None:
            raise exception

    context = WorkerContext()
    server_tasks = weakref.WeakSet()

    async def _server_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_tasks.add(asyncio.current_task(loop))

        await TCPServer(app, loop, context, reader, writer)

    servers = []

    for sock in sockets:
        servers.append(
            await asyncio.start_server(
                _server_callback,
                backlog=BACKLOG,
                sock=sock,
            )
        )

        bind = repr_socket_addr(sock.family, sock.getsockname())
        logger.info(f"Running on https://{bind} (CTRL + C to quit)")

    tasks = []

    tasks.append(loop.create_task(raise_shutdown(shutdown_trigger)))

    try:
        if len(tasks):
            gathered_tasks = asyncio.gather(*tasks)
            await gathered_tasks
        else:
            loop.run_forever()
    except (ShutdownError, KeyboardInterrupt):
        pass
    finally:
        context.terminated = True

        for server in servers:
            server.close()
            await server.wait_closed()

        # Retrieve the Gathered Tasks Cancelled Exception, to
        # prevent a warning that this hasn't been done.
        gathered_tasks.exception()

        try:
            gathered_server_tasks = asyncio.gather(*server_tasks)
            await asyncio.wait_for(gathered_server_tasks, GRACEFUL_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        # Retrieve the Gathered Tasks Cancelled Exception, to
        # prevent a warning that this hasn't been done.
        gathered_server_tasks.exception()

        await lifespan.wait_for_shutdown()
        lifespan_task.cancel()
        await lifespan_task
