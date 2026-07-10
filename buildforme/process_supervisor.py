"""Supervised process execution — isolated process groups, env allowlist, redaction."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from buildforme.process_env import build_provider_env, env_policy_summary
from buildforme.redaction import redact_argv, redact_event, redact_process_result, redact_text
from buildforme.storage import utc_now_iso

MAX_STREAM_BYTES = 512_000
MAX_EVENT_LINES = 2_000
GRACEFUL_WAIT_SEC = 2.0
FORCE_WAIT_SEC = 3.0


class ProcessSupervisor:
    """Launch and supervise a single provider process with timeout and cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen[str]] = {}
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
            "text": True,
            "shell": False,
            "env": safe_env,
        }
        # Own session/process group so termination cannot hit the parent (CI/test runner).
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP = 0x00000200
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(**popen_kwargs)
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
            self._cancel_flags[run_id] = False

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_bytes = 0
        stderr_bytes = 0
        event_lines = 0
        started = time.monotonic()
        timed_out = False
        cancelled = False

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
                term_log.extend(_terminate_tree(proc, reason="cancel"))
                emit("process_cancel", "cancel requested — terminating isolated process tree")
                break
            if time.monotonic() > deadline:
                timed_out = True
                term_log.extend(_terminate_tree(proc, reason="timeout"))
                emit("process_timeout", f"timeout after {timeout_seconds}s")
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        try:
            proc.wait(timeout=FORCE_WAIT_SEC)
        except subprocess.TimeoutExpired:
            term_log.extend(_terminate_tree(proc, reason="wait_timeout_escalate"))
            try:
                proc.wait(timeout=FORCE_WAIT_SEC)
            except subprocess.TimeoutExpired:
                term_log.append(
                    {
                        "at": utc_now_iso(),
                        "action": "cleanup_incomplete",
                        "pid": proc.pid,
                        "ok": False,
                        "detail": "process still alive after force",
                    }
                )
                try:
                    proc.kill()
                except Exception:
                    pass

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
        alive = proc.poll() is None
        cleanup_ok = not alive

        with self._lock:
            self._procs.pop(run_id, None)
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
            "cleanup_ok": cleanup_ok,
            "process_group_isolated": True,
            "pid": proc.pid,
        }
        if not cleanup_ok:
            result["error"] = "process-tree cleanup incomplete"
        return redact_process_result(result)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancel_flags[run_id] = True
            proc = self._procs.get(run_id)
        if proc and proc.poll() is None:
            log = _terminate_tree(proc, reason="cancel_api")
            self._termination_log.setdefault(run_id, []).extend(log)
            return {
                "cancelled": True,
                "run_id": run_id,
                "signal_sent": True,
                "termination_log": log,
                "cleanup_ok": proc.poll() is not None,
            }
        return {"cancelled": True, "run_id": run_id, "signal_sent": bool(proc), "cleanup_ok": True}


_SUPERVISOR = ProcessSupervisor()


def get_process_supervisor() -> ProcessSupervisor:
    return _SUPERVISOR


def _terminate_tree(proc: subprocess.Popen[str], *, reason: str) -> list[dict[str, Any]]:
    """Graceful then forced termination of the child-owned process group only."""
    log: list[dict[str, Any]] = []
    if proc.poll() is not None:
        log.append({"at": utc_now_iso(), "action": "already_exited", "reason": reason, "ok": True, "pid": proc.pid})
        return log

    pid = proc.pid
    if os.name == "nt":
        # Graceful: CTRL_BREAK_EVENT to the new process group if possible, else taskkill tree.
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            log.append({"at": utc_now_iso(), "action": "ctrl_break", "reason": reason, "ok": True, "pid": pid})
        except Exception as exc:
            log.append(
                {
                    "at": utc_now_iso(),
                    "action": "ctrl_break",
                    "reason": reason,
                    "ok": False,
                    "detail": str(exc),
                    "pid": pid,
                }
            )
        try:
            proc.wait(timeout=GRACEFUL_WAIT_SEC)
            log.append({"at": utc_now_iso(), "action": "graceful_exit", "ok": True, "pid": pid})
            return log
        except subprocess.TimeoutExpired:
            pass
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                shell=False,
                check=False,
                timeout=15,
            )
            log.append(
                {
                    "at": utc_now_iso(),
                    "action": "taskkill_tree",
                    "reason": reason,
                    "ok": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "stderr": redact_text((completed.stderr or b"").decode("utf-8", errors="replace")[:300]),
                    "pid": pid,
                }
            )
        except Exception as exc:
            log.append(
                {
                    "at": utc_now_iso(),
                    "action": "taskkill_tree",
                    "reason": reason,
                    "ok": False,
                    "detail": str(exc),
                    "pid": pid,
                }
            )
            try:
                proc.kill()
                log.append({"at": utc_now_iso(), "action": "kill", "ok": True, "pid": pid})
            except Exception as exc2:
                log.append({"at": utc_now_iso(), "action": "kill", "ok": False, "detail": str(exc2), "pid": pid})
        return log

    # POSIX: signal only the child's new session/process group.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        log.append({"at": utc_now_iso(), "action": "sigterm_group", "reason": reason, "ok": True, "pgid": pid})
    except ProcessLookupError:
        log.append({"at": utc_now_iso(), "action": "sigterm_group", "ok": True, "detail": "already gone", "pid": pid})
        return log
    except Exception as exc:
        log.append({"at": utc_now_iso(), "action": "sigterm_group", "ok": False, "detail": str(exc), "pid": pid})
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=GRACEFUL_WAIT_SEC)
        log.append({"at": utc_now_iso(), "action": "graceful_exit", "ok": True, "pid": pid})
        return log
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        log.append({"at": utc_now_iso(), "action": "sigkill_group", "reason": reason, "ok": True, "pgid": pid})
    except ProcessLookupError:
        log.append({"at": utc_now_iso(), "action": "sigkill_group", "ok": True, "detail": "already gone", "pid": pid})
    except Exception as exc:
        log.append({"at": utc_now_iso(), "action": "sigkill_group", "ok": False, "detail": str(exc), "pid": pid})
        try:
            proc.kill()
            log.append({"at": utc_now_iso(), "action": "kill", "ok": True, "pid": pid})
        except Exception as exc2:
            log.append({"at": utc_now_iso(), "action": "kill", "ok": False, "detail": str(exc2), "pid": pid})
    return log
