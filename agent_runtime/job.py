from __future__ import annotations
import contextlib
import ctypes
import os
import sys
import threading
from ctypes import wintypes

# winnt.h: job object limit flags and info classes
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
# processthreadsapi.h: access rights required by
# AssignProcessToJobObject
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    """Kill-on-close Job Object for child processes.

    atexit cannot run when the server dies hard (taskkill, crash,
    host restart after hibernation), which would otherwise orphan
    dev servers. Children assigned to this job are terminated by the
    kernel when the server's last job handle closes, however the
    server dies; grandchildren join automatically. Assignment is
    best-effort: if the server itself already runs inside a job
    (some hosts sandbox MCP servers), children stay in the host's
    job and behavior is unchanged from before.
    """

    def __init__(self, kill_on_close=True):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        # LPSECURITY_ATTRIBUTES is not in ctypes.wintypes; passing None
        # (default security) only needs a pointer-sized argument.
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32 = kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._job = job
        if not kill_on_close:
            return
        info = _EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            self._job = None
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, pid: int) -> bool:
        handle = self._kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self._job, handle))
        finally:
            self._kernel32.CloseHandle(handle)

    def terminate(self):
        """Kill every process in the job, however deep in the tree."""
        self._kernel32.TerminateJobObject(self._job, 1)

    def __del__(self):
        # Per-process jobs are dropped with their process entry.
        with contextlib.suppress(Exception):
            if self._job:
                self._kernel32.CloseHandle(self._job)


def track(pid: int):
    """Best-effort: a job holding pid and everything it starts, or None.

    Lets a process tree be stopped after its root exited, when
    taskkill /T no longer reaches the rest. Windows only; nested in
    the kill-on-close job when pid is already in it.
    """
    if os.name != "nt":
        return None
    try:
        job = WindowsJob(kill_on_close=False)
        return job if job.assign(pid) else None
    except Exception:
        return None


_job: WindowsJob | None = None
_job_failed = False
# Parallel spawns must not each create a job.
_job_lock = threading.Lock()


def adopt(pid: int) -> None:
    """Best-effort: attach a child process to the server's
    kill-on-close job so it cannot outlive the server."""
    global _job, _job_failed
    if os.name != "nt" or _job_failed:
        return
    if _job is None:
        with _job_lock:
            if _job is None and not _job_failed:
                try:
                    _job = WindowsJob()
                except Exception as e:
                    _job_failed = True
                    # Said once, on stderr: a silent failure hid a
                    # broken job from 1.8.0 to 1.9.2.
                    print(f"localforge: kill-on-close job unavailable, children may outlive "
                          f"the server: {e}", file=sys.stderr)
        if _job is None:
            return
    try:
        _job.assign(pid)
    except Exception:
        pass
