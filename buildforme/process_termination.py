"""Cross-platform process-tree termination with explicit confirmation.

A signal being sent is not proof of termination.  This module snapshots the
child-owned process tree, terminates only that tree, and confirms that the root,
known descendants, and POSIX process group are absent before cleanup is true.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from typing import Any

from buildforme.redaction import redact_text
from buildforme.storage import utc_now_iso


def _process_table() -> tuple[dict[int, tuple[int, int | None]], str, list[str]]:
    problems: list[str] = []
    if os.name == "nt":
        command = (
            "$ErrorActionPreference='Stop'; "
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=12,
                shell=False,
                check=False,
            )
            if proc.returncode != 0:
                return {}, "powershell_cim", [f"process table exit {proc.returncode}"]
            raw = json.loads(proc.stdout or "[]")
            rows = raw if isinstance(raw, list) else [raw]
            table: dict[int, tuple[int, int | None]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid = int(row.get("ProcessId") or 0)
                ppid = int(row.get("ParentProcessId") or 0)
                if pid > 0:
                    table[pid] = (ppid, None)
            return table, "powershell_cim", problems
        except Exception as exc:
            return {}, "powershell_cim", [redact_text(str(exc))[:300]]

    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=8,
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            return {}, "ps", [f"process table exit {proc.returncode}"]
        table = {}
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                pid, ppid, pgid = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                continue
            table[pid] = (ppid, pgid)
        return table, "ps", problems
    except Exception as exc:
        return {}, "ps", [redact_text(str(exc))[:300]]


def snapshot_process_tree(root_pid: int, *, process_group_id: int | None = None) -> dict[str, Any]:
    table, method, problems = _process_table()
    descendants: set[int] = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _pgid) in table.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    group_members = {
        pid
        for pid, (_ppid, pgid) in table.items()
        if process_group_id is not None and pgid == int(process_group_id)
    }
    return {
        "root_pid": int(root_pid),
        "process_group_id": int(process_group_id) if process_group_id is not None else None,
        "known_pids": sorted(descendants | group_members),
        "method": method,
        "problems": problems,
        "captured_at": utc_now_iso(),
    }


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        table, _method, _problems = _process_table()
        return pid in table
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _group_exists(pgid: int | None) -> bool | None:
    if pgid is None or os.name == "nt":
        return None
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def confirm_process_tree_terminated(
    root_pid: int,
    *,
    process_group_id: int | None = None,
    known_pids: list[int] | None = None,
    windows_job: Any | None = None,
) -> dict[str, Any]:
    snapshot = snapshot_process_tree(root_pid, process_group_id=process_group_id)
    candidates = set(int(pid) for pid in (known_pids or []))
    candidates.update(int(pid) for pid in snapshot.get("known_pids") or [])
    candidates.add(int(root_pid))
    live_pids = sorted(pid for pid in candidates if _pid_exists(pid))
    group_exists = _group_exists(process_group_id)
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
    return {
        "confirmed": bool(confirmed),
        "root_pid": int(root_pid),
        "root_exited": int(root_pid) not in live_pids,
        "process_group_id": int(process_group_id) if process_group_id is not None else None,
        "group_absent": bool(group_absent),
        "windows_job_active_processes": windows_job_active,
        "known_pids": sorted(candidates),
        "live_pids": live_pids,
        "method": snapshot.get("method"),
        "problems": problems,
        "checked_at": utc_now_iso(),
    }


def terminate_process_tree(
    proc: subprocess.Popen[str],
    *,
    reason: str,
    graceful_wait_sec: float = 2.0,
    force_wait_sec: float = 3.0,
    windows_job: Any | None = None,
) -> dict[str, Any]:
    pgid = proc.pid if os.name != "nt" else None
    before = snapshot_process_tree(proc.pid, process_group_id=pgid)
    known_pids = list(before.get("known_pids") or [proc.pid])
    log: list[dict[str, Any]] = []

    def add(action: str, ok: bool, **extra: Any) -> None:
        log.append(
            {
                "at": utc_now_iso(),
                "action": action,
                "reason": reason,
                "ok": bool(ok),
                "pid": proc.pid,
                **extra,
            }
        )

    if proc.poll() is None:
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                add("ctrl_break", True)
            except Exception as exc:
                add("ctrl_break", False, detail=redact_text(str(exc))[:300])
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                add("sigterm_group", True, pgid=proc.pid)
            except ProcessLookupError:
                add("sigterm_group", True, detail="already gone", pgid=proc.pid)
            except Exception as exc:
                add("sigterm_group", False, detail=redact_text(str(exc))[:300], pgid=proc.pid)
                try:
                    proc.terminate()
                    add("terminate_root_fallback", True)
                except Exception as exc2:
                    add("terminate_root_fallback", False, detail=redact_text(str(exc2))[:300])

    try:
        proc.wait(timeout=max(0.1, graceful_wait_sec))
        add("graceful_wait", True, exit_code=proc.returncode)
    except subprocess.TimeoutExpired:
        add("graceful_wait", False, detail="timeout")

    confirmation = confirm_process_tree_terminated(
        proc.pid,
        process_group_id=pgid,
        known_pids=known_pids,
        windows_job=windows_job,
    )
    if not confirmation["confirmed"]:
        if os.name == "nt":
            if windows_job is not None:
                try:
                    windows_job.terminate(exit_code=1)
                    add("terminate_job_object", True)
                except Exception as exc:
                    add("terminate_job_object", False, detail=redact_text(str(exc))[:300])
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    shell=False,
                    check=False,
                )
                add("taskkill_tree", completed.returncode == 0, exit_code=completed.returncode)
            except Exception as exc:
                add("taskkill_tree", False, detail=redact_text(str(exc))[:300])
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                add("sigkill_group", True, pgid=proc.pid)
            except ProcessLookupError:
                add("sigkill_group", True, detail="already gone", pgid=proc.pid)
            except Exception as exc:
                add("sigkill_group", False, detail=redact_text(str(exc))[:300], pgid=proc.pid)
                try:
                    proc.kill()
                    add("kill_root_fallback", True)
                except Exception as exc2:
                    add("kill_root_fallback", False, detail=redact_text(str(exc2))[:300])

        deadline = time.monotonic() + max(0.1, force_wait_sec)
        while time.monotonic() < deadline:
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            confirmation = confirm_process_tree_terminated(
                proc.pid,
                process_group_id=pgid,
                known_pids=known_pids,
            )
            if confirmation["confirmed"]:
                break
            time.sleep(0.05)

    add("termination_confirmed", bool(confirmation.get("confirmed")), live_pids=confirmation.get("live_pids") or [])
    return {"log": log, "confirmation": confirmation, "snapshot": before}
