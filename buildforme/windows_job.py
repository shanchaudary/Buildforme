"""Windows Job Object containment for supervised provider process trees."""

from __future__ import annotations

import os
import threading
from typing import Any

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400


class WindowsJobError(RuntimeError):
    pass


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


class WindowsJob:
    def __init__(self, handle: Any, root_pid: int) -> None:
        self._handle = handle
        self.root_pid = int(root_pid)
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def create_and_assign(cls, root_pid: int) -> "WindowsJob":
        if os.name != "nt":
            raise WindowsJobError("Windows Job Objects are available only on Windows")
        import ctypes

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise WindowsJobError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        job = cls(handle, root_pid)
        try:
            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise WindowsJobError(
                    f"SetInformationJobObject failed: {ctypes.get_last_error()}"
                )
            process = kernel32.OpenProcess(
                PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION,
                False,
                int(root_pid),
            )
            if not process:
                raise WindowsJobError(f"OpenProcess failed: {ctypes.get_last_error()}")
            try:
                if not kernel32.AssignProcessToJobObject(handle, process):
                    raise WindowsJobError(
                        f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
                    )
            finally:
                kernel32.CloseHandle(process)
            return job
        except Exception:
            job.close()
            raise

    def active_processes(self) -> int:
        if os.name != "nt":
            return 0
        import ctypes

        with self._lock:
            if self._closed:
                raise WindowsJobError("job handle already closed")
            info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            if not kernel32.QueryInformationJobObject(
                self._handle,
                JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                raise WindowsJobError(
                    f"QueryInformationJobObject failed: {ctypes.get_last_error()}"
                )
            return int(info.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        if os.name != "nt":
            return
        import ctypes

        with self._lock:
            if self._closed:
                raise WindowsJobError("job handle already closed")
            if not kernel32.TerminateJobObject(self._handle, int(exit_code)):
                raise WindowsJobError(
                    f"TerminateJobObject failed: {ctypes.get_last_error()}"
                )

    def close(self) -> None:
        if os.name != "nt":
            self._closed = True
            return
        with self._lock:
            if self._closed:
                return
            kernel32.CloseHandle(self._handle)
            self._closed = True
