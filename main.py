import asyncio
import socket
import sys
import time
from functools import partial
from importlib import import_module
from multiprocessing import Process
from pathlib import Path
from typing import Sequence, Any, Callable, Awaitable, Optional

from nemesis.typing import ASGIFramework


class NoAppError(Exception):
    pass


def create_sockets(binds: Sequence[str], sock_type: int = socket.SOCK_STREAM) -> list[socket.socket]:
    sockets: list[socket.socket] = []

    for bind in binds:
        try:
            value = bind.rsplit(":", 1)
            host, port = value[0], int(value[1])
        except (ValueError, IndexError):
            host, port = bind, 8000

        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, sock_type)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        sock.bind((host, port))
        sock.setblocking(False)

        try:
            sock.set_inheritable(True)
        except AttributeError:
            pass

        sockets.append(sock)

    return sockets


def create_workers(worker_type, worker_kwargs: dict[str, Any], worker_num: int = 1) -> list[Process]:
    worker_processes: list[Process] = []

    for _ in range(worker_num):
        process = Process(
            target=worker_type,
            kwargs=worker_kwargs,
        )

        process.daemon = True
        process.start()

        worker_processes.append(process)

        time.sleep(0.1)

    return worker_processes


def load_app(path: str) -> ASGIFramework:
    try:
        module_name, app_name = path.split(":", 1)
    except ValueError:
        module_name, app_name = path, "app"
    except AttributeError:
        raise NoAppError()

    module_path = Path(module_name).resolve()
    sys.path.insert(0, str(module_path.parent))

    if module_path.is_file():
        import_name = module_path.with_suffix("").name
    else:
        import_name = module_path.name
    try:
        module = import_module(import_name)
    except ModuleNotFoundError as error:
        if error.name == import_name:
            raise NoAppError()
        else:
            raise

    try:
        return eval(app_name, vars(module))
    except NameError:
        raise NoAppError()


def cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]

    if not tasks:
        return

    for task in tasks:
        task.cancel()

    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))

    for task in tasks:
        if not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler(
                {
                    "message": "unhandled exception during shutdown",
                    "exception": task.exception(),
                    "task": task,
                }
            )


def run_in_event_loop(
    func: Callable,
    *,
    debug: bool = False,
    shutdown_trigger: Optional[Callable[..., Awaitable[None]]] = None,
) -> None:
    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)
    loop.set_debug(debug)

    try:
        loop.run_until_complete(func(shutdown_trigger=shutdown_trigger))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            cancel_all_tasks(loop)
            loop.run_until_complete(loop.shutdown_asyncgens())

            try:
                loop.run_until_complete(loop.shutdown_default_executor())
            except AttributeError:
                pass  # shutdown_default_executor is new to Python 3.9

        finally:
            asyncio.set_event_loop(None)
            loop.close()


def init_asyncio_worker(app_path: str, sockets: list[socket.socket]) -> None:
    app = load_app(app_path)

    run_in_event_loop(
        partial(serve_worker, app, sockets=sockets),
        debug=True,
        # shutdown_trigger=shutdown_trigger,
    )


def run(app: str, binds: Sequence[str], worker_num: int = 1) -> None:
    """
    Main application entry point
    """

    sockets = create_sockets(binds=binds)
    workers = create_workers(
        worker_type=init_asyncio_worker,
        worker_kwargs={"app_path": app, "sockets": sockets},
        worker_num=worker_num,
    )

    for process in workers:
        process.join()

    for process in workers:
        process.terminate()

    for sock in sockets:
        sock.close()

async def application(scope, receive, send):
    event = await receive()
    print(event)

    await send({"type": "websocket.send", ...})

if __name__ == "__main__":
    run(
        app="todo",
        binds=("127.0.0.1:8310", "127.0.0.1:8311")
    )
