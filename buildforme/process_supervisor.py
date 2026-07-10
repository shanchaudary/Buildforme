"""Supervised process execution — no shell interpolation, process-tree cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from buildforme.storage import utc_now_iso

# Bound captured output to protect storage and avoid secret dumps flooding disks
MAX_STREAM_BYTES = 512_000
MAX_EVENT_LINES = 2_000


class ProcessSupervisor:
    """Launch and supervise a single provider process with timeout and cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._cancel_flags: dict[str, bool] = {}

    def run(
        self,
        *,
        run_id: str,
        argv: list[str],
        cwd: str | Path,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute argv (list only). Never shell=True."""
        if not argv or not str(argv[0]).strip():
            raise ValueError("argv required")
        if any(not isinstance(a, str) for a in argv):
            raise ValueError("argv must be list[str]")
        # Reject obvious shell metacharacter injection when a single string was wrongly split
        for part in argv:
            if "\x00" in part:
                raise ValueError("nul byte in argv")

        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            raise ValueError(f"cwd does not exist: {cwd_path}")

        safe_env = os.environ.copy()
        if env:
            for k, v in env.items():
                # Never inject secret-looking keys from untrusted payload blindly;
                # only allow non-secret overlays.
                if _is_secret_key(k):
                    continue
                safe_env[str(k)] = str(v)
        # Strip accidental dump of common secret vars into logs — we don't log env.

        def emit(event_type: str, message: str, **meta: Any) -> None:
            if on_event:
                on_event(
                    {
                        "type": event_type,
                        "message": message,
                        "at": utc_now_iso(),
                        "run_id": run_id,
                        **meta,
                    }
                )

        emit("process_start", f"launch {' '.join(argv[:6])}", argv=argv[:20], cwd=str(cwd_path))

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                env=safe_env,
            )
        except OSError as exc:
            return {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
                "cancelled": False,
                "duration_seconds": 0,
                "error": f"launch failed: {exc}",
            }

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
                        if which == "stdout":
                            if stdout_bytes >= MAX_STREAM_BYTES:
                                continue
                            stdout_bytes += len(line.encode("utf-8", errors="replace"))
                            bucket.append(line)
                        else:
                            if stderr_bytes >= MAX_STREAM_BYTES:
                                continue
                            stderr_bytes += len(line.encode("utf-8", errors="replace"))
                            bucket.append(line)
                        event_lines += 1
                        if event_lines <= MAX_EVENT_LINES and on_event:
                            emit("process_output", line.rstrip()[:500], stream=which)
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
                _terminate_tree(proc)
                emit("process_cancel", "cancel requested — terminating process tree")
                break
            if time.monotonic() > deadline:
                timed_out = True
                _terminate_tree(proc)
                emit("process_timeout", f"timeout after {timeout_seconds}s")
                break
            code = proc.poll()
            if code is not None:
                break
            time.sleep(0.05)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        t_out.join(timeout=2)
        t_err.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        duration = time.monotonic() - started
        exit_code = proc.returncode

        with self._lock:
            self._procs.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        # Truncation markers (honest, not silent)
        if stdout_bytes >= MAX_STREAM_BYTES:
            stdout += "\n[truncated: stdout limit reached — continue in evidence artifacts]\n"
        if stderr_bytes >= MAX_STREAM_BYTES:
            stderr += "\n[truncated: stderr limit reached — continue in evidence artifacts]\n"

        ok = (exit_code == 0) and not timed_out and not cancelled
        emit(
            "process_end",
            f"exit={exit_code} timed_out={timed_out} cancelled={cancelled}",
            exit_code=exit_code,
        )
        return {
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
        }

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancel_flags[run_id] = True
            proc = self._procs.get(run_id)
        if proc and proc.poll() is None:
            _terminate_tree(proc)
            return {"cancelled": True, "run_id": run_id, "signal_sent": True}
        return {"cancelled": True, "run_id": run_id, "signal_sent": bool(proc)}


_SUPERVISOR = ProcessSupervisor()


def get_process_supervisor() -> ProcessSupervisor:
    return _SUPERVISOR


def _terminate_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            # Windows: taskkill process tree
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                shell=False,
                check=False,
                timeout=15,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _is_secret_key(key: str) -> bool:
    low = str(key).lower()
    return any(x in low for x in ("token", "secret", "password", "api_key", "apikey", "authorization", "credential"))
