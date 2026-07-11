from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Cross-process reader/writer coordination for SQLite cutover
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "coordination_lock.py").write_text(
    r'''"""Cross-process shared/exclusive file lock for SQLite authority cutover."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class CoordinationLockTimeout(TimeoutError):
    pass


class CoordinationFileLock:
    """Shared lock for normal DB access; exclusive lock for maintenance cutover."""

    def __init__(
        self,
        path: Path | str,
        *,
        shared: bool,
        timeout_seconds: float = 60.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.shared = bool(shared)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "CoordinationFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock(handle)
                self._handle = handle
                return self
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise CoordinationLockTimeout(
                        f"timed out acquiring {'shared' if self.shared else 'exclusive'} "
                        f"coordination lock: {self.path}"
                    ) from exc
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def _try_lock(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_NBRLCK if self.shared else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
            return
        import fcntl

        mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)

    def _unlock(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# Windows Job Object containment/proof
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "windows_job.py").write_text(
    r'''"""Windows Job Object containment for supervised provider process trees."""

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
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# ExecutionDB shared/exclusive coordination
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "db.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from buildforme.storage import utc_now_iso\n",
    "from buildforme.coordination_lock import CoordinationFileLock\n"
    "from buildforme.storage import utc_now_iso\n",
    label="db coordination import",
)
text = replace_once(
    text,
    '''        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialized = False
''',
    '''        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._coordination_path = Path(str(self.path) + ".coord.lock")
        self._lock = threading.RLock()
        self._initialized = False
''',
    label="db coordination path",
)
text = replace_once(
    text,
    '''    def ensure_schema(self) -> None:
        with self._lock:
            conn = self._raw_connect()
''',
    '''    def ensure_schema(self) -> None:
        with CoordinationFileLock(self._coordination_path, shared=True), self._lock:
            conn = self._raw_connect()
''',
    label="schema shared lock",
)
text = replace_once(
    text,
    '''    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._raw_connect()
''',
    '''    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with CoordinationFileLock(self._coordination_path, shared=True), self._lock:
            conn = self._raw_connect()
''',
    label="transaction shared lock",
)
text = replace_once(
    text,
    '''    def pragmas(self) -> dict[str, Any]:
        with self._lock:
            conn = self._raw_connect()
''',
    '''    @contextmanager
    def maintenance_lock(self, *, timeout_seconds: float = 120.0) -> Iterator[None]:
        """Block every normal reader/writer while maintenance replaces the DB file."""
        with CoordinationFileLock(
            self._coordination_path,
            shared=False,
            timeout_seconds=timeout_seconds,
        ), self._lock:
            yield

    def pragmas(self) -> dict[str, Any]:
        with CoordinationFileLock(self._coordination_path, shared=True), self._lock:
            conn = self._raw_connect()
''',
    label="maintenance lock and pragma shared lock",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Migration holds exclusive lock for snapshot, import validation and cutover
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
marker = '''    def migrate_from_json(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
        """Import through a temporary SQLite authority and atomically replace on success.
'''
replacement = '''    def migrate_from_json(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
        if dry_run:
            return self._migrate_from_json_atomically_locked(
                runtime_dir,
                dry_run=True,
                cutover=False,
            )
        with self.db.maintenance_lock(timeout_seconds=120.0):
            return self._migrate_from_json_atomically_locked(
                runtime_dir,
                dry_run=False,
                cutover=cutover,
            )

    def _migrate_from_json_atomically_locked(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
        """Import through a temporary SQLite authority and atomically replace on success.
'''
text = replace_once(text, marker, replacement, label="migration exclusive lock wrapper")
# Avoid nested shared lock while exclusive lock is already held.
text = replace_once(
    text,
    '''        with self.db.transaction() as conn:
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('queued','starting','running','cancel_requested')"
                ).fetchone()[0]
            )
''',
    '''        conn = self.db._raw_connect()
        try:
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('queued','starting','running','cancel_requested')"
                ).fetchone()[0]
            )
        finally:
            conn.close()
''',
    label="migration active check under exclusive lock",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Windows-aware process termination proof
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "process_termination.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    known_pids: list[int] | None = None,
) -> dict[str, Any]:
''',
    '''    known_pids: list[int] | None = None,
    windows_job: Any | None = None,
) -> dict[str, Any]:
''',
    label="confirmation windows job parameter",
)
text = replace_once(
    text,
    '''    group_exists = _group_exists(process_group_id)
    group_absent = group_exists is False or group_exists is None and os.name == "nt"
    confirmed = not live_pids and group_absent and not snapshot.get("problems")
''',
    '''    group_exists = _group_exists(process_group_id)
    problems = list(snapshot.get("problems") or [])
    windows_job_active: int | None = None
    if os.name == "nt":
        if windows_job is None:
            group_absent = False
            problems.append("Windows Job Object proof unavailable")
        else:
            try:
                windows_job_active = int(windows_job.active_processes())
                group_absent = windows_job_active == 0
            except Exception as exc:
                group_absent = False
                problems.append(redact_text(str(exc))[:300])
    else:
        group_absent = group_exists is False
    confirmed = not live_pids and group_absent and not problems
''',
    label="job-based confirmation",
)
text = replace_once(
    text,
    '''        "group_absent": bool(group_absent),
        "known_pids": sorted(candidates),
''',
    '''        "group_absent": bool(group_absent),
        "windows_job_active_processes": windows_job_active,
        "known_pids": sorted(candidates),
''',
    label="job accounting evidence",
)
text = replace_once(
    text,
    '''        "problems": list(snapshot.get("problems") or []),
''',
    '''        "problems": problems,
''',
    label="confirmation problems",
)
text = replace_once(
    text,
    '''    force_wait_sec: float = 3.0,
) -> dict[str, Any]:
''',
    '''    force_wait_sec: float = 3.0,
    windows_job: Any | None = None,
) -> dict[str, Any]:
''',
    label="termination windows job parameter",
)
# Pass job through both confirmation calls.
text = text.replace(
    '''        known_pids=known_pids,
    )
''',
    '''        known_pids=known_pids,
        windows_job=windows_job,
    )
''',
)
# Use Job Object as Windows force authority before taskkill fallback.
text = replace_once(
    text,
    '''        if os.name == "nt":
            try:
                completed = subprocess.run(
''',
    '''        if os.name == "nt":
            if windows_job is not None:
                try:
                    windows_job.terminate(exit_code=1)
                    add("terminate_job_object", True)
                except Exception as exc:
                    add("terminate_job_object", False, detail=redact_text(str(exc))[:300])
            try:
                completed = subprocess.run(
''',
    label="Windows job force termination",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# ProcessSupervisor owns Job Object and never treats missing registry as proof
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "process_supervisor.py"
text = path.read_text(encoding="utf-8")
text = text.replace("import signal\n", "")
text = replace_once(
    text,
    '''from buildforme.storage import utc_now_iso
''',
    '''from buildforme.storage import utc_now_iso
from buildforme.windows_job import WindowsJob
''',
    label="Windows job import",
)
text = replace_once(
    text,
    '''        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._cancel_flags: dict[str, bool] = {}
''',
    '''        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._windows_jobs: dict[str, WindowsJob] = {}
        self._cancel_flags: dict[str, bool] = {}
''',
    label="supervisor job registry",
)
text = replace_once(
    text,
    '''        try:
            proc = subprocess.Popen(**popen_kwargs)
            if stdin_bytes is not None and proc.stdin is not None:
''',
    '''        windows_job: WindowsJob | None = None
        try:
            proc = subprocess.Popen(**popen_kwargs)
            if os.name == "nt":
                try:
                    windows_job = WindowsJob.create_and_assign(proc.pid)
                except Exception as exc:
                    terminated = terminate_process_tree(
                        proc,
                        reason="windows_job_assignment_failed",
                    )
                    confirmation = dict(terminated.get("confirmation") or {})
                    confirmation["confirmed"] = False
                    problems = list(confirmation.get("problems") or [])
                    problems.append(redact_text(str(exc))[:300])
                    confirmation["problems"] = problems
                    return redact_process_result(
                        {
                            "ok": False,
                            "exit_code": proc.returncode,
                            "stdout": "",
                            "stderr": "Windows Job Object assignment failed",
                            "timed_out": False,
                            "cancelled": False,
                            "duration_seconds": 0,
                            "error": "Windows Job Object assignment failed",
                            "argv": argv,
                            "cwd": str(cwd_path),
                            "env_names": env_names,
                            "env_policy": env_policy_summary(provider_id, env_names),
                            "termination_log": terminated.get("log") or [],
                            "termination_confirmation": confirmation,
                            "cleanup_ok": False,
                            "process_group_isolated": False,
                            "pid": proc.pid,
                        }
                    )
            if stdin_bytes is not None and proc.stdin is not None:
''',
    label="assign Windows job immediately",
)
text = replace_once(
    text,
    '''        with self._lock:
            self._procs[run_id] = proc
            self._cancel_flags[run_id] = False
''',
    '''        with self._lock:
            self._procs[run_id] = proc
            if windows_job is not None:
                self._windows_jobs[run_id] = windows_job
            self._cancel_flags[run_id] = False
''',
    label="store Windows job",
)
# Pass windows_job into terminate calls and confirmations in run method.
text = text.replace(
    '''                    force_wait_sec=FORCE_WAIT_SEC,
                )
''',
    '''                    force_wait_sec=FORCE_WAIT_SEC,
                    windows_job=windows_job,
                )
''',
)
text = text.replace(
    '''                known_pids=[proc.pid],
            )
''',
    '''                known_pids=[proc.pid],
                windows_job=windows_job,
            )
''',
)
# Escalate descendants remaining after a normal root exit.
text = replace_once(
    text,
    '''        cleanup_ok = bool(termination_confirmation.get("confirmed"))

        with self._lock:
            self._procs.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)
''',
    '''        if not termination_confirmation.get("confirmed"):
            terminated = terminate_process_tree(
                proc,
                reason="post_exit_descendant_cleanup",
                graceful_wait_sec=0.1,
                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
            term_log.extend(terminated["log"])
            termination_confirmation = terminated["confirmation"]
        cleanup_ok = bool(termination_confirmation.get("confirmed"))

        with self._lock:
            self._procs.pop(run_id, None)
            self._windows_jobs.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)
''',
    label="post-exit descendant cleanup",
)
# Close Job Object only after final proof (kill-on-close is a final safety net).
text = replace_once(
    text,
    '''        if not cleanup_ok:
            result["error"] = "process-tree cleanup incomplete"
        return redact_process_result(result)
''',
    '''        if not cleanup_ok:
            result["error"] = "process-tree cleanup incomplete"
        cleaned = redact_process_result(result)
        if windows_job is not None:
            windows_job.close()
        return cleaned
''',
    label="close Windows job after proof",
)
# Cancel retrieves Job Object and no-handle is unconfirmed.
text = replace_once(
    text,
    '''        with self._lock:
            self._cancel_flags[run_id] = True
            proc = self._procs.get(run_id)
        if proc and proc.poll() is None:
''',
    '''        with self._lock:
            self._cancel_flags[run_id] = True
            proc = self._procs.get(run_id)
            windows_job = self._windows_jobs.get(run_id)
        if proc and proc.poll() is None:
''',
    label="cancel retrieves Windows job",
)
text = replace_once(
    text,
    '''                force_wait_sec=FORCE_WAIT_SEC,
            )
''',
    '''                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
''',
    label="cancel passes Windows job",
)
old_no_proc = '''        confirmation = {
            "confirmed": True,
            "root_exited": True,
            "group_absent": True,
            "known_pids": [],
            "live_pids": [],
            "method": "no_active_process",
            "problems": [],
            "checked_at": utc_now_iso(),
        }
        return {
            "cancelled": True,
            "run_id": run_id,
            "signal_sent": bool(proc),
            "termination_log": [],
            "termination_confirmation": confirmation,
            "cleanup_ok": True,
        }
'''
new_no_proc = '''        confirmation = {
            "confirmed": False,
            "root_exited": False,
            "group_absent": False,
            "known_pids": [],
            "live_pids": [],
            "method": "process_registry_miss",
            "problems": [
                "no active process handle in this supervisor instance; OS termination is not proven"
            ],
            "checked_at": utc_now_iso(),
        }
        return {
            "cancelled": True,
            "run_id": run_id,
            "signal_sent": False,
            "termination_log": [],
            "termination_confirmation": confirmation,
            "cleanup_ok": False,
        }
'''
text = replace_once(text, old_no_proc, new_no_proc, label="registry miss fails closed")
# Remove duplicate legacy termination authority at end of file.
legacy = text.find("\ndef _terminate_tree(")
if legacy == -1:
    raise RuntimeError("legacy _terminate_tree authority not found")
text = text[:legacy].rstrip() + "\n"
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Pre-start cancellation is explicit; active-state registry loss fails closed
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_service.py"
text = path.read_text(encoding="utf-8")
old = '''    from buildforme.process_supervisor import get_process_supervisor

    try:
        process_result = get_process_supervisor().cancel(run_id)
    except Exception as exc:
        process_result = {
            "cancelled": True,
            "cleanup_ok": False,
            "error": redact_text(str(exc)[:500]),
            "termination_log": [],
            "termination_confirmation": {
                "confirmed": False,
                "reason": "cancel API raised",
                "live_pids": [],
            },
        }

    if status in {"running", "starting", "queued", "cancel_requested"}:
'''
new = '''    prestart_statuses = {
        "draft",
        "awaiting_preflight",
        "awaiting_approval",
        "approved",
        "needs_review",
    }
    if status in prestart_statuses:
        process_result = {
            "cancelled": True,
            "cleanup_ok": True,
            "termination_log": [],
            "termination_confirmation": {
                "confirmed": True,
                "root_exited": True,
                "group_absent": True,
                "reason": "governed lifecycle proves no provider process is active",
                "live_pids": [],
                "method": "lifecycle_prestart_or_postrun",
            },
        }
    else:
        from buildforme.process_supervisor import get_process_supervisor

        try:
            process_result = get_process_supervisor().cancel(run_id)
        except Exception as exc:
            process_result = {
                "cancelled": True,
                "cleanup_ok": False,
                "error": redact_text(str(exc)[:500]),
                "termination_log": [],
                "termination_confirmation": {
                    "confirmed": False,
                    "reason": "cancel API raised",
                    "live_pids": [],
                },
            }

    if status in {"running", "starting", "queued", "cancel_requested"}:
'''
text = replace_once(text, old, new, label="cancel lifecycle proof split")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Full immutable outcome fingerprint (except DB-assigned linkage metadata)
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "outcome_evidence.py"
text = path.read_text(encoding="utf-8")
start = text.index("def compute_run_outcome_fingerprint(")
end = text.index("\ndef validate_run_outcome_evidence", start)
new_fp = r'''def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def compute_run_outcome_fingerprint(bundle: dict[str, Any]) -> str:
    """Bind every caller-produced persisted field in the immutable evidence bundle.

    Database linkage fields are assigned only at append time and are separately
    protected by the append-only evidence table.
    """
    excluded = {
        "evidence_fingerprint",
        "sequence",
        "attempt",
        "saved_at",
        "parent_evidence_id",
    }
    material = {
        "fingerprint_schema": OUTCOME_FINGERPRINT_SCHEMA,
        "bundle": {
            str(key): _canonical(value)
            for key, value in bundle.items()
            if str(key) not in excluded
        },
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
'''
text = text[:start] + new_fp + text[end:]
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Auth probes are code-owned and output-semantics verified
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "provider_discovery.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''AUTH_PROBES: dict[str, dict[str, Any] | None] = {
    "codex": {"args": ["login", "status"], "success_exit_codes": [0], "read_only": True},
    "claude": {"args": ["auth", "status"], "success_exit_codes": [0], "read_only": True},
    "grok": None,
    "glm": None,
}
''',
    '''AUTH_PROBES: dict[str, dict[str, Any] | None] = {
    "codex": {
        "args": ["login", "status"],
        "success_exit_codes": [0],
        "success_patterns": [r"(?i)\\blogged in\\b"],
        "failure_patterns": [r"(?i)\\bnot logged in\\b", r"(?i)\\berror checking login status\\b"],
        "read_only": True,
        "contract_source": "openai/codex codex-rs/cli/src/login.rs run_login_status",
    },
    # No primary-source, machine-verifiable status contract has been accepted yet.
    "claude": None,
    "grok": None,
    "glm": None,
}
''',
    label="code-owned auth contracts",
)
text = replace_once(
    text,
    '''    record = provider_record or {}
    configured = record.get("auth_probe") if isinstance(record.get("auth_probe"), dict) else None
    probe = dict(configured or AUTH_PROBES.get(pid) or {})
''',
    '''    # Runtime/provider records cannot supply executable commands or weaken
    # success criteria. Probe contracts are reviewed code authority only.
    probe = dict(AUTH_PROBES.get(pid) or {})
''',
    label="remove caller auth command override",
)
text = replace_once(
    text,
    '''        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        ready = completed.returncode in success_codes
        return {
''',
    '''        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = stdout + "\n" + stderr
        import re

        success_patterns = [str(pattern) for pattern in (probe.get("success_patterns") or [])]
        failure_patterns = [str(pattern) for pattern in (probe.get("failure_patterns") or [])]
        positive = bool(success_patterns) and any(
            re.search(pattern, combined) is not None for pattern in success_patterns
        )
        negative = any(
            re.search(pattern, combined) is not None for pattern in failure_patterns
        )
        ready = completed.returncode in success_codes and positive and not negative
        return {
''',
    label="auth output semantics",
)
text = replace_once(
    text,
    '''            "command_shape": [Path(str(executable)).name, *args],
            "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
''',
    '''            "command_shape": [Path(str(executable)).name, *args],
            "contract_source": probe.get("contract_source"),
            "positive_status_marker": bool(positive),
            "negative_status_marker": bool(negative),
            "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
''',
    label="auth proof metadata",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Adversarial tests for red-team findings
# ---------------------------------------------------------------------------
(ROOT / "tests" / "test_stage6_redteam_round2.py").write_text(
    r'''"""Second red-team pass for Stage 6 proof authority."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from buildforme.execution_service import cancel_run
from buildforme.execution_store import Stage6Store
from buildforme.outcome_evidence import (
    build_run_outcome_evidence,
    validate_run_outcome_evidence,
)
from buildforme.process_supervisor import ProcessSupervisor
from buildforme.provider_discovery import probe_authentication
from buildforme.storage import LocalStore


class CancellationRegistryLossTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")

    def _run(self, run_id: str, status: str):
        return self.store.save_run_for_setup(
            {
                "id": run_id,
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "execution_mode": "live_supervised",
                "mode": "live_supervised",
                "status": status,
                "scope_fingerprint": "scope",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "lease",
                "constitution_lease_fingerprint": "lf",
                "status_history": [],
            }
        )

    def test_supervisor_registry_miss_is_not_termination_proof(self):
        result = ProcessSupervisor().cancel("missing-run")
        self.assertFalse(result["cleanup_ok"])
        self.assertFalse(result["termination_confirmation"]["confirmed"])
        self.assertEqual(
            result["termination_confirmation"]["method"],
            "process_registry_miss",
        )

    def test_running_cancel_after_registry_loss_fails_closed(self):
        self._run("lost-running", "running")
        before = self.store.get_run("lost-running")
        saved = cancel_run(self.store, "lost-running")
        self.assertEqual(saved["status"], "failed")
        self.assertNotEqual(saved["status"], "cancelled")
        self.assertGreater(saved["row_version"], before["row_version"])
        evidence = self.store.get_evidence_by_id(saved["outcome_evidence_id"])
        self.assertEqual(evidence["outcome"], "termination_unconfirmed")
        self.assertFalse(evidence["process"]["termination_confirmation"]["confirmed"])

    def test_prestart_cancel_uses_lifecycle_proof_not_registry(self):
        self._run("prestart", "approved")
        saved = cancel_run(self.store, "prestart")
        self.assertEqual(saved["status"], "rejected")
        evidence = self.store.get_evidence_by_id(saved["outcome_evidence_id"])
        self.assertTrue(evidence["process"]["termination_confirmation"]["confirmed"])
        self.assertEqual(
            evidence["process"]["termination_confirmation"]["method"],
            "lifecycle_prestart_or_postrun",
        )


class OutcomeFingerprintCoverageTests(unittest.TestCase):
    def _evidence(self):
        run = {
            "id": "r",
            "project_id": "p",
            "provider_id": "codex",
            "repository": "o/r",
            "execution_mode": "live_supervised",
            "scope_fingerprint": "scope",
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease",
            "constitution_lease_fingerprint": "lf",
        }
        return build_run_outcome_evidence(
            run=run,
            outcome="failed",
            previous_status="running",
            resulting_status="failed",
            previous_row_version=4,
            process_result={
                "ok": False,
                "exit_code": 1,
                "stdout": "out",
                "stderr": "err",
                "cleanup_ok": True,
                "termination_confirmation": {"confirmed": True, "live_pids": []},
                "termination_log": [{"action": "proof", "ok": True}],
            },
            reason="failure",
        )

    def test_every_material_preview_and_governance_field_is_fingerprinted(self):
        mutations = [
            ("collected_at", "2099-01-01T00:00:00+00:00"),
            ("constitution_version", "9.9.9"),
            ("immutable", False),
        ]
        for field, value in mutations:
            evidence = self._evidence()
            evidence[field] = value
            self.assertIn("fingerprint mismatch", validate_run_outcome_evidence(evidence))

        evidence = self._evidence()
        evidence["process"]["stdout_preview"] = "tampered"
        self.assertIn("fingerprint mismatch", validate_run_outcome_evidence(evidence))

        evidence = self._evidence()
        evidence["worktree"]["execution_branch"] = "evil"
        self.assertIn("fingerprint mismatch", validate_run_outcome_evidence(evidence))


class AuthProbeAuthorityTests(unittest.TestCase):
    def _fake(self, output: str, exit_code: int = 0):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "codex"
        path.write_text(
            "#!/bin/sh\nprintf '%s' " + json.dumps(output) + f"\nexit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return td, str(path)

    @unittest.skipIf(os.name == "nt", "shell fixture is POSIX-only")
    def test_provider_record_cannot_override_auth_command_or_success(self):
        td, executable = self._fake("Not logged in", exit_code=0)
        self.addCleanup(td.cleanup)
        result = probe_authentication(
            "codex",
            executable,
            provider_record={
                "auth_probe": {
                    "args": ["dangerous", "login"],
                    "success_exit_codes": [0],
                    "read_only": True,
                }
            },
        )
        self.assertFalse(result["probe_verified"])
        self.assertEqual(result["command_shape"], ["codex", "login", "status"])
        self.assertTrue(result["negative_status_marker"])

    @unittest.skipIf(os.name == "nt", "shell fixture is POSIX-only")
    def test_exit_zero_without_positive_login_marker_is_not_ready(self):
        td, executable = self._fake("status command completed", exit_code=0)
        self.addCleanup(td.cleanup)
        result = probe_authentication("codex", executable)
        self.assertFalse(result["probe_verified"])
        self.assertFalse(result["positive_status_marker"])

    @unittest.skipIf(os.name == "nt", "shell fixture is POSIX-only")
    def test_official_codex_logged_in_marker_is_ready(self):
        td, executable = self._fake("Logged in using ChatGPT", exit_code=0)
        self.addCleanup(td.cleanup)
        result = probe_authentication("codex", executable)
        self.assertTrue(result["probe_verified"])
        self.assertTrue(result["positive_status_marker"])
        self.assertFalse(result["negative_status_marker"])

    def test_unverified_claude_status_contract_fails_closed(self):
        result = probe_authentication("claude", sys.executable)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["probe_verified"])


class MigrationCoordinationTests(unittest.TestCase):
    def test_exclusive_maintenance_lock_blocks_other_process_transactions(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / "authority.db"
        store = Stage6Store(db_path)
        store.save_run_for_setup(
            {
                "id": "existing",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "draft",
                "execution_mode": "dry_run",
            }
        )
        script = Path(temp.name) / "writer.py"
        script.write_text(
            "from buildforme.execution_store import Stage6Store\n"
            "import sys\n"
            "s=Stage6Store(sys.argv[1])\n"
            "r=s.get_run('existing')\n"
            "r['result_summary']='writer'\n"
            "s.save_run(r, expected_row_version=r['row_version'])\n"
            "print('done', flush=True)\n",
            encoding="utf-8",
        )
        with store.db.maintenance_lock(timeout_seconds=5):
            proc = subprocess.Popen(
                [sys.executable, str(script), str(db_path)],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "writer bypassed exclusive migration lock")
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertIn("done", stdout)
        self.assertEqual(store.get_run("existing")["result_summary"], "writer")

    def test_migration_method_holds_maintenance_lock_contract(self):
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn("with self.db.maintenance_lock", source)


class WindowsJobContractTests(unittest.TestCase):
    def test_windows_job_contract_is_present_and_duplicate_terminator_removed(self):
        job_source = Path("buildforme/windows_job.py").read_text(encoding="utf-8")
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", job_source)
        self.assertIn("AssignProcessToJobObject", job_source)
        supervisor_source = Path("buildforme/process_supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn("def _terminate_tree", supervisor_source)
        self.assertIn("WindowsJob.create_and_assign", supervisor_source)


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)


# Update Stage 6 documentation truth.
path = ROOT / "docs" / "STAGE_6_MULTI_PROVIDER_EXECUTION.md"
text = path.read_text(encoding="utf-8")
text += '''

## Red-team hardening round 2

- A missing in-memory process handle is never accepted as OS termination proof.
- Windows live processes require Job Object assignment with kill-on-close and zero-active-process accounting.
- The old duplicate process-tree terminator was removed; `process_termination.py` is the sole termination authority.
- Outcome fingerprints bind the full caller-produced immutable evidence bundle, including timestamps, previews, and governance fields.
- Authentication probe commands and success semantics are code-owned. Provider records cannot supply commands or success exit codes.
- Migration holds an exclusive cross-process coordination lock from active-run check through snapshot, validation, and atomic replacement. Normal DB reads/writes hold shared locks.
'''
path.write_text(text, encoding="utf-8")

print("Stage 6 red-team hardening round 2 applied")
