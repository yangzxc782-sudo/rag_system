"""Bounded synchronous child execution and host-wide slot for one work root."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
from threading import Event, Thread
import time

from app.casting.execution_protocol import CastingExecutionError, ExecutionLimits


@contextmanager
def calculation_slot(root: Path):
    # OS locks release on process death; never unlink a lock file used by peers.
    with (root / ".calculation.lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CastingExecutionError("CASTING_BUSY", "system", "已有计算正在运行，请稍后重试", retryable=True) from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class _WindowsJob:
    """KILL_ON_JOB_CLOSE also applies if the parent exits unexpectedly."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("PerProcessUserTimeLimit", c.c_longlong), ("PerJobUserTimeLimit", c.c_longlong),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", c.c_size_t),
                        ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(c.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", c.c_size_t),
                        ("JobMemoryLimit", c.c_size_t), ("PeakProcessMemoryUsed", c.c_size_t), ("PeakJobMemoryUsed", c.c_size_t)]

        class Accounting(c.Structure):
            _fields_ = [(name, c.c_longlong) for name in ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                (name, w.DWORD) for name in ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

        self.api = c.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.TerminateJobObject.restype = w.BOOL
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        self.api.QueryInformationJobObject.restype = w.BOOL
        self.accounting_type = Accounting
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def attach(self, process: subprocess.Popen) -> None:
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process: subprocess.Popen) -> None:
        """Resume the primary thread only after the suspended launcher joins our job."""
        import ctypes as c
        from ctypes import wintypes as w

        class ThreadEntry(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("thread_id", w.DWORD),
                        ("owner_pid", w.DWORD), ("base_priority", w.LONG), ("delta_priority", w.LONG), ("flags", w.DWORD)]

        self.api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.api.Thread32First.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
        self.api.Thread32Next.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
        self.api.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenThread.restype = w.HANDLE
        self.api.ResumeThread.argtypes = [w.HANDLE]
        self.api.ResumeThread.restype = w.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = c.sizeof(entry)
            found = self.api.Thread32First(snapshot, c.byref(entry))
            while found:
                if entry.owner_pid == process.pid:
                    thread = self.api.OpenThread(0x2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise c.WinError(c.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) != 1:
                            raise OSError("Unexpected suspended thread state")
                    finally:
                        self.api.CloseHandle(thread)
                    return
                entry.dwSize = c.sizeof(entry)
                found = self.api.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Suspended process thread missing")
        finally:
            self.api.CloseHandle(snapshot)

    def close(self) -> None:
        if self.handle:
            import ctypes as c
            from ctypes import wintypes as w
            handles = []
            try:
                class ProcessIds(c.Structure):
                    _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("ids", c.c_size_t * 256)]

                self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
                self.api.OpenProcess.restype = w.HANDLE
                self.api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
                self.api.WaitForSingleObject.restype = w.DWORD
                ids = ProcessIds()
                if not self.api.QueryInformationJobObject(self.handle, 3, c.byref(ids), c.sizeof(ids), None):
                    raise c.WinError(c.get_last_error())
                for pid in ids.ids[:ids.count]:
                    handle = self.api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                    if handle:
                        handles.append(handle)
                if not self.api.TerminateJobObject(self.handle, 1):
                    raise c.WinError(c.get_last_error())
                # A Windows venv can start an inner interpreter. Waiting only on
                # Popen's launcher does not guarantee the actual worker is dead.
                deadline = time.monotonic() + 5
                while True:
                    info = self.accounting_type()
                    if not self.api.QueryInformationJobObject(self.handle, 1, c.byref(info), c.sizeof(info), None):
                        raise c.WinError(c.get_last_error())
                    if info.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise CastingExecutionError("CASTING_TERMINATION_FAILED", "system", "计算进程未能及时结束")
                    time.sleep(0.01)
                # ActiveProcesses can reach zero just before the kernel marks
                # individual process objects signaled. Wait for actual exit too.
                for handle in handles:
                    milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
                    if self.api.WaitForSingleObject(handle, milliseconds) != 0:
                        raise CastingExecutionError("CASTING_TERMINATION_FAILED", "system", "计算进程未能及时结束")
            finally:
                for handle in handles:
                    self.api.CloseHandle(handle)
                self.api.CloseHandle(self.handle)
                self.handle = None


def check_directory_budget(directory: Path, maximum: int) -> None:
    total, count = 0, 0
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise CastingExecutionError("CASTING_OUTPUT_INVALID", "integrity", "计算目录包含非法文件引用")
            count += 1
            if path.is_file():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass  # Atomic marker rename can race this observational scan.
            if total > maximum or count > 64:
                raise CastingExecutionError("CASTING_OUTPUT_LIMIT", "capacity", "计算产物超过容量上限")


def run_child(command: list[str], directory: Path, limits: ExecutionLimits) -> tuple[int, int]:
    process = None
    job = None
    threads: list[Thread] = []
    exceeded, storage_error = Event(), Event()

    def drain(pipe, path):
        remaining = limits.max_log_bytes
        try:
            with path.open("wb") as target:
                while chunk := pipe.read(8192):
                    target.write(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()
                    remaining = max(0, remaining - len(chunk))
        except OSError:
            storage_error.set()
        finally:
            pipe.close()

    try:
        job = _WindowsJob() if os.name == "nt" else None
        process = subprocess.Popen(
            command, cwd=directory, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # CREATE_SUSPENDED closes the venv launcher race: it must not spawn
            # an inner interpreter before being attached to the Job Object.
            creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        if job:
            job.attach(process)
            job.resume(process)
        for pipe, filename in ((process.stdout, "stdout.log"), (process.stderr, "stderr.log")):
            thread = Thread(target=drain, args=(pipe, directory / filename), daemon=True)
            thread.start()
            threads.append(thread)
        process.stdin.write(b"G")
        process.stdin.close()
        deadline = time.monotonic() + limits.timeout_seconds
        while process.poll() is None:
            if exceeded.is_set():
                raise CastingExecutionError("CASTING_LOG_LIMIT", "capacity", "计算日志超过容量上限")
            if storage_error.is_set():
                raise CastingExecutionError("CASTING_STORAGE_ERROR", "system", "计算日志写入失败", retryable=True)
            check_directory_budget(directory, limits.max_output_bytes)
            if time.monotonic() >= deadline:
                raise CastingExecutionError("CASTING_TIMEOUT", "system", "计算超时，请调整输入后重试", retryable=True)
            time.sleep(0.05)
        return_code, pid = process.returncode, process.pid
    except OSError as exc:
        raise CastingExecutionError("CASTING_ENGINE_UNAVAILABLE", "system", "无法启动或隔离计算进程", retryable=True) from exc
    finally:
        try:
            if job:
                job.close()  # Kill the entire job, including any descendants.
        finally:
            if process:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()  # Also covers attach/startup failure.
                process.wait(timeout=5)
                if process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                for thread in threads:
                    thread.join(timeout=5)
                for pipe in (process.stdout, process.stderr):
                    if pipe and not pipe.closed:
                        pipe.close()
    if exceeded.is_set():
        raise CastingExecutionError("CASTING_LOG_LIMIT", "capacity", "计算日志超过容量上限")
    if storage_error.is_set():
        raise CastingExecutionError("CASTING_STORAGE_ERROR", "system", "计算日志写入失败", retryable=True)
    check_directory_budget(directory, limits.max_output_bytes)
    return return_code, pid
