import asyncio
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import uvicorn

from app.local_server import LocalServer, server_config


async def unused_app(scope, receive, send):
    raise AssertionError("This unit test must not start an application")


def test_only_verified_single_sigint_replay_is_consumed(monkeypatch):
    def run(self, sockets=None):
        self.started = self.shutdown_completed = True
        self._captured_signals = [signal.SIGINT]
        raise KeyboardInterrupt()
    monkeypatch.setattr(uvicorn.Server, "run", run)
    LocalServer(server_config(unused_app)).run()


@pytest.mark.parametrize("complete,signals", [
    (False, [signal.SIGINT]), (True, []), (True, [signal.SIGINT, signal.SIGINT]),
])
def test_early_or_unrecognized_interrupt_is_not_hidden(monkeypatch, complete, signals):
    def run(self, sockets=None):
        self.shutdown_completed = complete
        self._captured_signals = signals
        raise KeyboardInterrupt()
    monkeypatch.setattr(uvicorn.Server, "run", run)
    with pytest.raises(KeyboardInterrupt):
        LocalServer(server_config(unused_app)).run()


@pytest.mark.parametrize("error", [RuntimeError("startup failure"), asyncio.CancelledError()])
def test_other_exceptions_propagate(monkeypatch, error):
    def run(self, sockets=None):
        raise error
    monkeypatch.setattr(uvicorn.Server, "run", run)
    with pytest.raises(type(error)):
        LocalServer(server_config(unused_app)).run()


@pytest.mark.parametrize("phase,flag", [
    ("startup", "startup_failed"), ("startup", "error_occurred"),
    ("shutdown", "shutdown_failed"), ("shutdown", "error_occurred"),
])
def test_uvicorn_reported_lifespan_failures_are_not_success(monkeypatch, phase, flag):
    async def operation(self, sockets=None):
        self.lifespan = SimpleNamespace(**{flag: True})
    monkeypatch.setattr(uvicorn.Server, phase, operation)
    server = LocalServer(server_config(unused_app))
    server.started = True
    with pytest.raises(RuntimeError, match=f"lifespan {phase} failed"):
        asyncio.run(getattr(server, phase)())
    assert not server.shutdown_completed


def test_shutdown_exception_and_force_exit_cannot_mark_completion(monkeypatch):
    server = LocalServer(server_config(unused_app))
    server.started = True
    async def failed(self, sockets=None):
        raise RuntimeError("shutdown failure")
    monkeypatch.setattr(uvicorn.Server, "shutdown", failed)
    with pytest.raises(RuntimeError, match="shutdown failure"):
        asyncio.run(server.shutdown())
    assert not server.shutdown_completed
    async def forced(self, sockets=None):
        self.force_exit = True
        self.lifespan = SimpleNamespace()
    monkeypatch.setattr(uvicorn.Server, "shutdown", forced)
    asyncio.run(server.shutdown())
    assert not server.shutdown_completed


@pytest.mark.skipif(sys.platform != "win32" or os.getenv("RUN_ISOLATED_SIGINT_TESTS") != "1",
                    reason="explicit opt-in Windows isolated-console test")
@pytest.mark.parametrize("seconds,failure", [(0, "none"), (2, "none"), (12, "none"),
                                           (0, "startup"), (0, "shutdown")])
def test_native_ctrl_c_in_isolated_mock_process(tmp_path, seconds, failure):
    import ctypes
    from ctypes import wintypes
    from uuid import uuid4
    nonce = str(uuid4())
    env = dict(os.environ, DATABASE_URL="sqlite://", CONVERSATION_ENABLED="false",
               CASTING_DESIGN_ENABLED="false", DOCUMENT_PROCESSING_EXECUTOR_ENABLED="false",
               DOCUMENT_DELETION_EXECUTOR_ENABLED="false")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    child = subprocess.Popen([sys.executable, "-B", "-u", str(Path(__file__).with_name("shutdown_mock_process.py")),
                              "--nonce", nonce, "--seconds", str(seconds), "--failure", failure],
        cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE)
    received, events = queue.Queue(), []
    def capture(stream, path, parse):
        with path.open("wb") as target:
            for line in iter(stream.readline, b""):
                target.write(line)
                target.flush()
                if parse:
                    value = json.loads(line)
                    events.append(value)
                    received.put(value)
        if parse:
            received.put(None)
    readers = [threading.Thread(target=capture, args=(child.stdout, tmp_path/"stdout.log", True), daemon=True),
               threading.Thread(target=capture, args=(child.stderr, tmp_path/"stderr.log", False), daemon=True)]
    for reader in readers:
        reader.start()
    ready = None
    while True:
        item = received.get(timeout=25)
        if item is None or item["event"] == "top_level_error":
            break
        if item["event"] == "ready":
            ready = item
            break
    process_handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if ready:
        assert ready["nonce"] == nonce
        assert ready["pid"] == child.pid or ready["parent_pid"] == child.pid
        assert set(ready["console_pids"]) <= {child.pid, ready["pid"]}
        assert len(ready["addresses"]) == 1
        host, port = ready["addresses"][0]
        assert host == "127.0.0.1" and port not in {0, 8000}
        process_handle = kernel.OpenProcess(0x00100000 | 0x1000, False, ready["pid"])
        assert process_handle
        command = {"nonce": nonce, "child_pid": ready["pid"], "launcher_pid": child.pid}
        child.stdin.write((json.dumps(command)+"\n").encode())
        child.stdin.flush()
    try:
        if process_handle:
            # Wait on the exact Python child, not just the venv launcher.
            # No automatic terminate or second signal, even on test failure.
            assert kernel.WaitForSingleObject(process_handle, 30000) == 0
            actual_exit = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(process_handle, ctypes.byref(actual_exit))
            actual_exit = actual_exit.value
        else:
            actual_exit = None
        launcher_exit = child.wait(timeout=10)
    finally:
        if process_handle:
            kernel.CloseHandle(process_handle)
    for reader in readers:
        reader.join(timeout=5)
        assert not reader.is_alive()
    (tmp_path/"result.json").write_text(json.dumps({"events": events, "exit_code": actual_exit,
        "launcher_exit_code": launcher_exit}, indent=2), encoding="utf-8")
    stderr = (tmp_path/"stderr.log").read_text(encoding="utf-8")
    names = [item["event"] for item in events]
    if ready:
        assert names.count("signal_received") == 1
        assert next(item for item in events if item["event"] == "signal_received")["signal"] == signal.SIGINT
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex((host, port)) != 0
    if failure != "none":
        assert launcher_exit != 0
        assert f"SYNTHETIC_{failure.upper()}_FAILURE" in stderr
        assert f"lifespan {failure} failed" in stderr
        assert "run_returned" not in names
        return
    assert ready and actual_exit == launcher_exit == 0
    assert "KeyboardInterrupt" not in stderr and "CancelledError" not in stderr and "Traceback" not in stderr
    assert "Application shutdown complete." in stderr
    assert names.index("lifespan_completed") < names.index("server_shutdown_completed") < names.index("run_returned")
    closed = next(item for item in events if item["event"] == "resources_closed")
    assert not closed["worker_alive"] and not closed["heartbeat_alive"]
    assert next(item for item in events if item["event"] == "run_returned")["imported_main"] is False
    if seconds:
        assert closed["advances"] == 1 and closed["renewals"] > 0
        assert names.index("worker_finally") < names.index("resources_closed")
        assert names.index("heartbeat_finally") < names.index("resources_closed")
    if seconds > 10:
        assert "pdf_executor_shutdown_waiting" in stderr
        assert '"worker_alive":true' in stderr and '"heartbeat_alive":true' in stderr
        assert closed["elapsed_ms"] >= seconds * 1000
