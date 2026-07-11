"""Supervised process execution — isolated process groups, env allowlist, redaction."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from buildforme.process_env import build_provider_env, env_policy_summary
from buildforme.process_termination import (
    confirm_process_tree_terminated,
    terminate_process_tree,
)
from buildforme.redaction import redact_argv, redact_event, redact_process_result, redact_text
from buildforme.storage import utc_now_iso
from buildforme.windows_job import WindowsJob

MAX_STREAM_BYTES = 512_000
MAX_EVENT_LINES = 2_000
GRACEFUL_WAIT_SEC = 2.0
FORCE_WAIT_SEC = 3.0


class ProcessSupervisor:
    """Launch and supervise a single provider process with timeout and cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._windows_jobs: dict[str, WindowsJob] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._termination_log: dict[str, list[dict[str, Any]]] = {}

    def run(
        self,
        *,
        run_id: str,
        argv: list[str],
        cwd: str | Path,
        timeout_seconds: int,
        provider_id: str = "",
        env: dict[str, str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        use_provider_env_allowlist: bool = True,
        stdin_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """Execute argv (list only). Never shell=True. Own process group on POSIX."""
        if not argv or not str(argv[0]).strip():
            raise ValueError("argv required")
        if any(not isinstance(a, str) for a in argv):
            raise ValueError("argv must be list[str]")
        for part in argv:
            if "\x00" in part:
                raise ValueError("nul byte in argv")

        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            raise ValueError(f"cwd does not exist: {cwd_path}")

        if use_provider_env_allowlist:
            safe_env, env_names = build_provider_env(provider_id or "generic")
            # Optional explicit overlays that are non-secret
            if env:
                for k, v in env.items():
                    if str(k).upper() in safe_env or str(k) in {
                        "BUILDFORME_RUN_ID",
                        "BUILDFORME_PROVIDER_ID",
                    }:
                        if not any(s in str(k).lower() for s in ("token", "secret", "password", "key")):
                            safe_env[str(k)] = str(v)
                            if str(k) not in env_names:
                                env_names.append(str(k))
        else:
            # Tests may pass a fully controlled env map
            safe_env = dict(env or {})
            env_names = sorted(safe_env.keys())

        term_log: list[dict[str, Any]] = []
        self._termination_log[run_id] = term_log

        def emit(event_type: str, message: str, **meta: Any) -> None:
            if on_event:
                payload = redact_event(
                    {
                        "type": event_type,
                        "message": message,
                        "at": utc_now_iso(),
                        "run_id": run_id,
                        **meta,
                    }
                )
                on_event(payload)

        emit(
            "process_start",
            f"launch {' '.join(redact_argv(argv[:6]))}",
            argv=redact_argv(argv[:20]),
            cwd=str(cwd_path),
            env_names=env_names,
        )

        popen_kwargs: dict[str, Any] = {
            "args": argv,
            "cwd": str(cwd_path),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            "text": True,
            "shell": False,
            "env": safe_env,
        }
        # Own session/process group so termination cannot hit the parent (CI/test runner).
        if os.name == "nt":
            # The process must not execute before Job Object assignment.  Starting
            # suspended closes the child-spawn escape window between Popen and
            # AssignProcessToJobObject.
            create_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            create_suspended = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            popen_kwargs["creationflags"] = create_group | create_suspended
        else:
            popen_kwargs["start_new_session"] = True

        windows_job: WindowsJob | None = None
        try:
            proc = subprocess.Popen(**popen_kwargs)
            if os.name == "nt":
                try:
                    windows_job = WindowsJob.create_and_assign(proc.pid)
                    WindowsJob.resume_process(proc)
                except Exception as exc:
                    if windows_job is not None:
                        try:
                            windows_job.terminate(exit_code=1)
                        except Exception:
                            pass
                    terminated = terminate_process_tree(
                        proc,
                        reason="windows_job_assignment_or_resume_failed",
                        windows_job=windows_job,
                    )
                    confirmation = dict(terminated.get("confirmation") or {})
                    confirmation["confirmed"] = False
                    problems = list(confirmation.get("problems") or [])
                    problems.append(redact_text(str(exc))[:300])
                    confirmation["problems"] = problems
                    result = redact_process_result(
                        {
                            "ok": False,
                            "exit_code": proc.returncode,
                            "stdout": "",
                            "stderr": "Windows suspended-launch containment failed",
                            "timed_out": False,
                            "cancelled": False,
                            "duration_seconds": 0,
                            "error": "Windows suspended-launch containment failed",
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
                    if windows_job is not None:
                        windows_job.close()
                    return result
            if stdin_bytes is not None and proc.stdin is not None:
                try:
                    # text mode expects str
                    text_in = stdin_bytes.decode("utf-8", errors="replace")
                    proc.stdin.write(text_in)
                    proc.stdin.close()
                except Exception:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
        except OSError as exc:
            return redact_process_result(
                {
                    "ok": False,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": str(exc),
                    "timed_out": False,
                    "cancelled": False,
                    "duration_seconds": 0,
                    "error": f"launch failed: {exc}",
                    "argv": argv,
                    "cwd": str(cwd_path),
                    "env_names": env_names,
                    "env_policy": env_policy_summary(provider_id, env_names),
                    "termination_log": term_log,
                    "process_group_isolated": True,
                }
            )

        with self._lock:
            self._procs[run_id] = proc
            if windows_job is not None:
                self._windows_jobs[run_id] = windows_job
            self._cancel_flags[run_id] = False

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_bytes = 0
        stderr_bytes = 0
        event_lines = 0
        started = time.monotonic()
        timed_out = False
        cancelled = False
        termination_confirmation: dict[str, Any] | None = None

        def reader(stream: Any, bucket: list[str], which: str) -> None:
            nonlocal stdout_bytes, stderr_bytes, event_lines
            if stream is None:
                return
            try:
                for line in stream:
                    with self._lock:
                        encoded_len = len(line.encode("utf-8", errors="replace"))
                        if which == "stdout":
                            if stdout_bytes >= MAX_STREAM_BYTES:
                                continue
                            stdout_bytes += encoded_len
                            bucket.append(line)
                        else:
                            if stderr_bytes >= MAX_STREAM_BYTES:
                                continue
                            stderr_bytes += encoded_len
                            bucket.append(line)
                        event_lines += 1
                        if event_lines <= MAX_EVENT_LINES and on_event:
                            emit("process_output", redact_text(line.rstrip()[:500]), stream=which)
            except Exception:
                pass

        t_out = threading.Thread(target=reader, args=(proc.stdout, stdout_chunks, "stdout"), daemon=True)
        t_err = threading.Thread(target=reader, args=(proc.stderr, stderr_chunks, "stderr"), daemon=True)
        t_out.start()
        t_err.start()

        deadline = started + max(1, int(timeout_seconds))
        while True:
            with self._lock:
                if self._cancel_flags.get(run_id):
                    cancelled = True
            if cancelled:
                terminated = terminate_process_tree(
                    proc,
                    reason="cancel",
                    graceful_wait_sec=GRACEFUL_WAIT_SEC,
                    force_wait_sec=FORCE_WAIT_SEC,
                    windows_job=windows_job,
                )
                term_log.extend(terminated["log"])
                termination_confirmation = terminated["confirmation"]
                emit("process_cancel", "cancel requested — terminating isolated process tree")
                break
            if time.monotonic() > deadline:
                timed_out = True
                terminated = terminate_process_tree(
                    proc,
                    reason="timeout",
                    graceful_wait_sec=GRACEFUL_WAIT_SEC,
                    force_wait_sec=FORCE_WAIT_SEC,
                    windows_job=windows_job,
                )
                term_log.extend(terminated["log"])
                termination_confirmation = terminated["confirmation"]
                emit("process_timeout", f"timeout after {timeout_seconds}s")
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        try:
            proc.wait(timeout=FORCE_WAIT_SEC)
        except subprocess.TimeoutExpired:
            terminated = terminate_process_tree(
                proc,
                reason="wait_timeout_escalate",
                graceful_wait_sec=0.1,
                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
            term_log.extend(terminated["log"])
            termination_confirmation = terminated["confirmation"]

        # Never block forever on stream threads
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

        duration = time.monotonic() - started
        exit_code = proc.returncode
        if termination_confirmation is None:
            termination_confirmation = confirm_process_tree_terminated(
                proc.pid,
                process_group_id=proc.pid if os.name != "nt" else None,
                known_pids=[proc.pid],
                windows_job=windows_job,
            )
        if not termination_confirmation.get("confirmed"):
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

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if stdout_bytes >= MAX_STREAM_BYTES:
            stdout += "\n[truncated: stdout limit reached — continue in evidence artifacts]\n"
        if stderr_bytes >= MAX_STREAM_BYTES:
            stderr += "\n[truncated: stderr limit reached — continue in evidence artifacts]\n"

        ok = (exit_code == 0) and not timed_out and not cancelled and cleanup_ok
        emit(
            "process_end",
            f"exit={exit_code} timed_out={timed_out} cancelled={cancelled} cleanup_ok={cleanup_ok}",
            exit_code=exit_code,
        )
        result = {
            "ok": ok,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "duration_seconds": round(duration, 3),
            "argv": argv,
            "cwd": str(cwd_path),
            "truncated_stdout": stdout_bytes >= MAX_STREAM_BYTES,
            "truncated_stderr": stderr_bytes >= MAX_STREAM_BYTES,
            "env_names": env_names,
            "env_policy": env_policy_summary(provider_id, env_names),
            "termination_log": term_log,
            "termination_confirmation": termination_confirmation,
            "cleanup_ok": cleanup_ok,
            "process_group_isolated": True,
            "pid": proc.pid,
        }
        if not cleanup_ok:
            result["error"] = "process-tree cleanup incomplete"
        cleaned = redact_process_result(result)
        if windows_job is not None:
            windows_job.close()
        return cleaned

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancel_flags[run_id] = True
            proc = self._procs.get(run_id)
            windows_job = self._windows_jobs.get(run_id)
        if proc and proc.poll() is None:
            terminated = terminate_process_tree(
                proc,
                reason="cancel_api",
                graceful_wait_sec=GRACEFUL_WAIT_SEC,
                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
            log = list(terminated["log"])
            confirmation = dict(terminated["confirmation"])
            self._termination_log.setdefault(run_id, []).extend(log)
            return {
                "cancelled": True,
                "run_id": run_id,
                "signal_sent": True,
                "termination_log": log,
                "termination_confirmation": confirmation,
                "cleanup_ok": bool(confirmation.get("confirmed")),
            }
        confirmation = {
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


_SUPERVISOR = ProcessSupervisor()


def get_process_supervisor() -> ProcessSupervisor:
    return _SUPERVISOR
