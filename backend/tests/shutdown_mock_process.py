"""Windows-only mock ASGI child for test_local_server_shutdown; no real app/IO."""
import argparse
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import platform
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4


def deny_external_io(event, args):
    if event == "socket.connect":
        # Python's Windows Proactor creates its wakeup socketpair using a
        # loopback connect. Permit only that exact stdlib call site.
        fallback = getattr(socket, "_fallback_socketpair", None)
        if (fallback is not None and sys._getframe(1).f_code is fallback.__code__
                and args[1][0] in {"127.0.0.1", "::1"}):
            return
    if event in {"socket.connect", "sqlite3.connect", "subprocess.Popen"}:
        raise RuntimeError("Mock shutdown test forbids external IO")
    if event == "import" and args[0] == "app.main":
        raise RuntimeError("Mock shutdown test must not import create_app")


# Prime the stdlib's read-only Windows version probe before blocking subprocess
# creation; SQLAlchemy import consults platform.machine() and can invoke `ver`.
platform.uname()
sys.addaudithook(deny_external_io)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.local_server import LocalServer, server_config
from app.tasks import document_processing_executor as module

parser = argparse.ArgumentParser()
parser.add_argument("--nonce", required=True)
parser.add_argument("--seconds", type=float, required=True)
parser.add_argument("--failure", choices=["none", "startup", "shutdown"], default="none")
args = parser.parse_args()
started = time.monotonic()
output_lock = threading.RLock()
resources_open = True
advances = 0
renewals = 0


def event(name, **fields):
    with output_lock:
        print(json.dumps(dict(event=name, elapsed_ms=round((time.monotonic()-started)*1000, 3),
                              pid=os.getpid(), **fields)), flush=True)


candidate = (uuid4(), uuid4())
@contextmanager
def sessions():
    assert resources_open
    yield SimpleNamespace(execute=lambda _: SimpleNamespace(first=lambda: candidate))
    assert resources_open


def advance(*a, **kw):
    global advances
    advances += 1
    event("worker_entered")
    try:
        threading.Event().wait(args.seconds)
        assert resources_open
        return True
    finally:
        event("worker_finally")


def renew(*a, **kw):
    global renewals
    assert resources_open
    renewals += 1


module.renew_processing_lease = renew
class ObservedExecutor(module.DocumentProcessingExecutor):
    def _heartbeat(self, *a):
        try:
            super()._heartbeat(*a)
        finally:
            event("heartbeat_finally", renewals=renewals)


settings = SimpleNamespace(document_processing_executor_enabled=args.seconds > 0,
    document_processing_shutdown_grace_seconds=10, document_processing_poll_interval_seconds=0.01,
    kg_lease_seconds=0.15)
executor = ObservedExecutor(settings=settings, session_factory=sessions, advance=advance)


async def mock_app(scope, receive, send):
    global resources_open
    assert scope["type"] == "lifespan"
    try:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                event("lifespan_startup")
                if args.failure == "startup":
                    await send({"type": "lifespan.startup.failed", "message": "SYNTHETIC_STARTUP_FAILURE"})
                    raise RuntimeError("SYNTHETIC_STARTUP_FAILURE")
                executor.start()
                await send({"type": "lifespan.startup.complete"})
            else:
                assert message["type"] == "lifespan.shutdown"
                event("lifespan_shutdown")
                await executor.shutdown()
                assert executor.join(0)
                resources_open = False
                event("resources_closed", advances=advances, renewals=renewals,
                      worker_alive=bool(executor._thread and executor._thread.is_alive()),
                      heartbeat_alive=bool(executor._heartbeat_thread and executor._heartbeat_thread.is_alive()))
                if args.failure == "shutdown":
                    await send({"type": "lifespan.shutdown.failed", "message": "SYNTHETIC_SHUTDOWN_FAILURE"})
                    raise RuntimeError("SYNTHETIC_SHUTDOWN_FAILURE")
                await send({"type": "lifespan.shutdown.complete"})
                event("lifespan_completed")
                return
    finally:
        event("lifespan_finally")


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
kernel.GetConsoleProcessList.restype = wintypes.DWORD
kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL


def console_pids():
    buf = (wintypes.DWORD * 64)()
    count = kernel.GetConsoleProcessList(buf, len(buf))
    if not 0 < count <= len(buf):
        raise RuntimeError("DEDICATED_CONSOLE_CHECK_FAILED")
    return list(buf[:count])


class ObservedServer(LocalServer):
    def handle_exit(self, sig, frame):
        event("signal_received", signal=int(sig))
        super().handle_exit(sig, frame)
    async def startup(self, sockets=None):
        await super().startup(sockets)
        event("ready", nonce=args.nonce, parent_pid=os.getppid(), console_pids=console_pids(),
              addresses=[sock.getsockname() for listener in self.servers for sock in listener.sockets])
    async def shutdown(self, sockets=None):
        event("server_shutdown_entered")
        await super().shutdown(sockets)
        event("server_shutdown_completed", verified=self.shutdown_completed)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
server = ObservedServer(server_config(mock_app, port=0))


def control():
    command = json.loads(sys.stdin.readline())
    attached = set(console_pids())
    allowed = {os.getpid()}
    if command.get("launcher_pid") == os.getppid():
        allowed.add(os.getppid())
    if (command.get("nonce") != args.nonce or command.get("child_pid") != os.getpid()
            or not attached <= allowed):
        event("signal_refused")
        server.should_exit = True
        return
    event("native_ctrl_c_requested", console_pids=sorted(attached))
    if not kernel.GenerateConsoleCtrlEvent(0, 0):
        event("signal_failed", winerror=ctypes.get_last_error())
        server.should_exit = True


if args.failure != "startup":
    threading.Thread(target=control, daemon=True).start()
try:
    server.run()
    event("run_returned", imported_main="app.main" in sys.modules)
except Exception as exc:
    event("top_level_error", exception_type=type(exc).__name__)
    raise
finally:
    event("top_level_finally")
