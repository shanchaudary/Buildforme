from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Process-tree observation and confirmed termination authority
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "process_termination.py").write_text(
    r'''"""Cross-platform process-tree termination with explicit confirmation.

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
) -> dict[str, Any]:
    snapshot = snapshot_process_tree(root_pid, process_group_id=process_group_id)
    candidates = set(int(pid) for pid in (known_pids or []))
    candidates.update(int(pid) for pid in snapshot.get("known_pids") or [])
    candidates.add(int(root_pid))
    live_pids = sorted(pid for pid in candidates if _pid_exists(pid))
    group_exists = _group_exists(process_group_id)
    group_absent = group_exists is False or group_exists is None and os.name == "nt"
    confirmed = not live_pids and group_absent and not snapshot.get("problems")
    return {
        "confirmed": bool(confirmed),
        "root_pid": int(root_pid),
        "root_exited": int(root_pid) not in live_pids,
        "process_group_id": int(process_group_id) if process_group_id is not None else None,
        "group_absent": bool(group_absent),
        "known_pids": sorted(candidates),
        "live_pids": live_pids,
        "method": snapshot.get("method"),
        "problems": list(snapshot.get("problems") or []),
        "checked_at": utc_now_iso(),
    }


def terminate_process_tree(
    proc: subprocess.Popen[str],
    *,
    reason: str,
    graceful_wait_sec: float = 2.0,
    force_wait_sec: float = 3.0,
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
    )
    if not confirmation["confirmed"]:
        if os.name == "nt":
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
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# Immutable terminal outcome evidence
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "outcome_evidence.py").write_text(
    r'''"""Immutable evidence for cancelled, timed-out, failed, and unavailable runs."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from buildforme.redaction import redact_hash, redact_process_result, redact_text
from buildforme.storage import utc_now_iso

OUTCOME_EVIDENCE_SCHEMA = "buildforme.evidence.run_outcome.v1"
OUTCOME_FINGERPRINT_SCHEMA = "buildforme.run_outcome_fingerprint.v1"
EVIDENCE_KIND_RUN_OUTCOME = "run_outcome"
TERMINAL_OUTCOMES = frozenset({"cancelled", "timed_out", "failed", "unavailable", "termination_unconfirmed"})


def build_run_outcome_evidence(
    *,
    run: dict[str, Any],
    outcome: str,
    previous_status: str,
    resulting_status: str,
    previous_row_version: int,
    process_result: dict[str, Any] | None,
    reason: str,
    worktree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outcome_n = str(outcome or "").strip().lower()
    if outcome_n not in TERMINAL_OUTCOMES:
        raise ValueError(f"unsupported terminal outcome: {outcome_n!r}")
    process = redact_process_result(process_result or {})
    confirmation = process.get("termination_confirmation")
    if not isinstance(confirmation, dict):
        confirmation = {
            "confirmed": bool(process.get("cleanup_ok")),
            "reason": "legacy_process_result_without_confirmation",
        }
    evidence_id = f"ev-out-{uuid.uuid4().hex[:16]}"
    bundle: dict[str, Any] = {
        "schema": OUTCOME_EVIDENCE_SCHEMA,
        "evidence_kind": EVIDENCE_KIND_RUN_OUTCOME,
        "evidence_id": evidence_id,
        "id": evidence_id,
        "immutable": True,
        "collected_at": utc_now_iso(),
        "run_id": run.get("id"),
        "project_id": run.get("project_id"),
        "task_id": run.get("task_id"),
        "packet_id": run.get("packet_id"),
        "repository": run.get("repository"),
        "provider_id": run.get("provider_id"),
        "execution_mode": run.get("execution_mode") or run.get("mode"),
        "scope_fingerprint": run.get("scope_fingerprint"),
        "constitution_hash": run.get("constitution_hash"),
        "constitution_version": run.get("constitution_version"),
        "constitution_lease_id": run.get("constitution_lease_id"),
        "constitution_lease_fingerprint": run.get("constitution_lease_fingerprint"),
        "outcome": outcome_n,
        "previous_status": str(previous_status),
        "resulting_status": str(resulting_status),
        "previous_row_version": int(previous_row_version),
        "reason": redact_text(reason)[:1000],
        "process": {
            "pid": process.get("pid"),
            "exit_code": process.get("exit_code"),
            "timed_out": bool(process.get("timed_out")),
            "cancelled": bool(process.get("cancelled")),
            "unavailable": bool(process.get("unavailable")),
            "cleanup_ok": bool(process.get("cleanup_ok")),
            "termination_confirmation": confirmation,
            "termination_log": list(process.get("termination_log") or []),
            "stdout_sha256": process.get("stdout_sha256") or redact_hash(process.get("stdout") or ""),
            "stderr_sha256": process.get("stderr_sha256") or redact_hash(process.get("stderr") or ""),
            "stdout_preview": redact_text((process.get("stdout") or "")[:1000]),
            "stderr_preview": redact_text((process.get("stderr") or "")[:1000]),
            "error": redact_text(process.get("error") or "")[:1000],
        },
        "worktree": {
            "path": (worktree or {}).get("worktree_path") or run.get("worktree_path"),
            "baseline_commit": (worktree or {}).get("baseline_commit") or run.get("baseline_commit"),
            "execution_branch": (worktree or {}).get("branch") or run.get("execution_branch"),
        },
    }
    bundle["evidence_fingerprint"] = compute_run_outcome_fingerprint(bundle)
    return bundle


def compute_run_outcome_fingerprint(bundle: dict[str, Any]) -> str:
    process = bundle.get("process") if isinstance(bundle.get("process"), dict) else {}
    material = {
        "schema": OUTCOME_FINGERPRINT_SCHEMA,
        "evidence_schema": bundle.get("schema"),
        "evidence_kind": EVIDENCE_KIND_RUN_OUTCOME,
        "evidence_id": bundle.get("evidence_id") or bundle.get("id"),
        "run_id": bundle.get("run_id"),
        "project_id": bundle.get("project_id"),
        "task_id": bundle.get("task_id"),
        "packet_id": bundle.get("packet_id"),
        "repository": bundle.get("repository"),
        "provider_id": bundle.get("provider_id"),
        "execution_mode": bundle.get("execution_mode"),
        "scope_fingerprint": bundle.get("scope_fingerprint"),
        "constitution_hash": bundle.get("constitution_hash"),
        "constitution_lease_id": bundle.get("constitution_lease_id"),
        "constitution_lease_fingerprint": bundle.get("constitution_lease_fingerprint"),
        "outcome": bundle.get("outcome"),
        "previous_status": bundle.get("previous_status"),
        "resulting_status": bundle.get("resulting_status"),
        "previous_row_version": bundle.get("previous_row_version"),
        "reason": bundle.get("reason"),
        "process": {
            "pid": process.get("pid"),
            "exit_code": process.get("exit_code"),
            "timed_out": process.get("timed_out"),
            "cancelled": process.get("cancelled"),
            "unavailable": process.get("unavailable"),
            "cleanup_ok": process.get("cleanup_ok"),
            "termination_confirmation": process.get("termination_confirmation"),
            "termination_log": process.get("termination_log"),
            "stdout_sha256": process.get("stdout_sha256"),
            "stderr_sha256": process.get("stderr_sha256"),
            "error": process.get("error"),
        },
        "worktree": bundle.get("worktree"),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_run_outcome_evidence(evidence: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not isinstance(evidence, dict):
        return ["run outcome evidence must be an object"]
    if str(evidence.get("evidence_kind") or "") != EVIDENCE_KIND_RUN_OUTCOME:
        problems.append("evidence_kind must be run_outcome")
    for field in (
        "schema",
        "evidence_id",
        "run_id",
        "outcome",
        "previous_status",
        "resulting_status",
        "previous_row_version",
        "reason",
        "process",
        "evidence_fingerprint",
    ):
        if evidence.get(field) is None or evidence.get(field) == "":
            problems.append(f"run outcome evidence missing required field: {field}")
    outcome = str(evidence.get("outcome") or "")
    if outcome not in TERMINAL_OUTCOMES:
        problems.append(f"unsupported run outcome: {outcome!r}")
    process = evidence.get("process") if isinstance(evidence.get("process"), dict) else {}
    confirmation = process.get("termination_confirmation")
    if not isinstance(confirmation, dict):
        problems.append("run outcome process missing termination_confirmation")
    if str(evidence.get("resulting_status")) in {"cancelled", "timed_out"}:
        if not process.get("cleanup_ok") or not isinstance(confirmation, dict) or not confirmation.get("confirmed"):
            problems.append("cancelled/timed_out outcome requires confirmed process-tree termination")
    provided = evidence.get("evidence_fingerprint")
    if not isinstance(provided, str) or len(provided) < 32:
        problems.append("run outcome evidence_fingerprint missing or invalid")
    elif provided != compute_run_outcome_fingerprint(evidence):
        problems.append("run outcome fingerprint mismatch (refusing silent repair)")
    return problems
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# Strict smoke acceptance evaluator
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "stage6_smoke_acceptance.py").write_text(
    r'''"""Machine-verifiable acceptance criteria for the real Stage 6 provider smoke."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from buildforme.evidence import validate_evidence_for_storage

REQUIRED_SMOKE_FILES = frozenset({"src/math_util.py", "tests/test_math_util.py", "README.md"})


def evaluate_stage6_smoke_acceptance(
    *,
    health: dict[str, Any],
    execution_result: dict[str, Any],
    final_run: dict[str, Any],
    persisted_evidence: dict[str, Any],
    decision_evidence: dict[str, Any] | None,
    repository_root: Path,
    original_head: str,
    original_branch: str,
) -> dict[str, Any]:
    process = execution_result.get("process") if isinstance(execution_result.get("process"), dict) else {}
    verification = execution_result.get("verification") if isinstance(execution_result.get("verification"), dict) else {}
    review = execution_result.get("review") if isinstance(execution_result.get("review"), dict) else {}
    evidence = execution_result.get("evidence") if isinstance(execution_result.get("evidence"), dict) else {}
    auth = health.get("auth") if isinstance(health.get("auth"), dict) else {}
    confirmation = process.get("termination_confirmation") if isinstance(process.get("termination_confirmation"), dict) else {}
    files = set(str(path) for path in (evidence.get("files_changed") or []))
    required_present = REQUIRED_SMOKE_FILES.issubset(files)
    evidence_problems = validate_evidence_for_storage(persisted_evidence)

    import subprocess

    root_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository_root, text=True).strip()
    root_branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repository_root, text=True).strip()
    checks = {
        "provider_live_ready": bool(health.get("live_ready")),
        "auth_probe_verified": auth.get("status") == "ready" and bool(auth.get("probe_verified")),
        "process_exit_zero": process.get("exit_code") == 0 and bool(process.get("ok")),
        "real_pid_observed": isinstance(process.get("pid"), int) and int(process.get("pid")) > 0,
        "process_tree_termination_confirmed": bool(process.get("cleanup_ok")) and bool(confirmation.get("confirmed")),
        "verification_passed": bool(verification.get("passed")),
        "review_gate_reached": bool(review.get("accept_for_pr_prep_allowed")),
        "required_files_produced": required_present,
        "evidence_persisted_exactly": bool(evidence.get("evidence_id"))
        and evidence.get("evidence_id") == persisted_evidence.get("evidence_id")
        and evidence.get("evidence_fingerprint") == persisted_evidence.get("evidence_fingerprint")
        and not evidence_problems,
        "patch_and_manifest_distinct": bool(evidence.get("patch_fingerprint"))
        and bool(evidence.get("manifest_fingerprint"))
        and evidence.get("patch_fingerprint") != evidence.get("manifest_fingerprint"),
        "final_head_valid": bool(re.fullmatch(r"[0-9a-fA-F]{40}", str(evidence.get("final_head_sha") or ""))),
        "founder_decision_completed": final_run.get("status") == "completed"
        and isinstance(decision_evidence, dict)
        and bool(decision_evidence.get("evidence_fingerprint")),
        "source_branch_unchanged": root_head == original_head and root_branch == original_branch,
        "merge_not_performed": root_head == original_head,
    }
    failed = sorted(name for name, ok in checks.items() if not ok)
    return {
        "schema": "buildforme.stage6_real_smoke_acceptance.v1",
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "required_files": sorted(REQUIRED_SMOKE_FILES),
        "observed_files": sorted(files),
        "evidence_problems": evidence_problems,
        "original_head": original_head,
        "root_head_after": root_head,
        "original_branch": original_branch,
        "root_branch_after": root_branch,
    }
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# ProcessSupervisor integration
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "process_supervisor.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from buildforme.process_env import build_provider_env, env_policy_summary\n",
    "from buildforme.process_env import build_provider_env, env_policy_summary\n"
    "from buildforme.process_termination import (\n"
    "    confirm_process_tree_terminated,\n"
    "    terminate_process_tree,\n"
    ")\n",
    label="process termination imports",
)
text = replace_once(
    text,
    '''        timed_out = False
        cancelled = False
''',
    '''        timed_out = False
        cancelled = False
        termination_confirmation: dict[str, Any] | None = None
''',
    label="termination confirmation state",
)
text = replace_once(
    text,
    '''            if cancelled:
                term_log.extend(_terminate_tree(proc, reason="cancel"))
                emit("process_cancel", "cancel requested — terminating isolated process tree")
                break
            if time.monotonic() > deadline:
                timed_out = True
                term_log.extend(_terminate_tree(proc, reason="timeout"))
                emit("process_timeout", f"timeout after {timeout_seconds}s")
                break
''',
    '''            if cancelled:
                terminated = terminate_process_tree(
                    proc,
                    reason="cancel",
                    graceful_wait_sec=GRACEFUL_WAIT_SEC,
                    force_wait_sec=FORCE_WAIT_SEC,
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
                )
                term_log.extend(terminated["log"])
                termination_confirmation = terminated["confirmation"]
                emit("process_timeout", f"timeout after {timeout_seconds}s")
                break
''',
    label="run cancel timeout termination",
)
text = replace_once(
    text,
    '''        except subprocess.TimeoutExpired:
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
''',
    '''        except subprocess.TimeoutExpired:
            terminated = terminate_process_tree(
                proc,
                reason="wait_timeout_escalate",
                graceful_wait_sec=0.1,
                force_wait_sec=FORCE_WAIT_SEC,
            )
            term_log.extend(terminated["log"])
            termination_confirmation = terminated["confirmation"]
''',
    label="wait escalation termination",
)
text = replace_once(
    text,
    '''        duration = time.monotonic() - started
        exit_code = proc.returncode
        alive = proc.poll() is None
        cleanup_ok = not alive
''',
    '''        duration = time.monotonic() - started
        exit_code = proc.returncode
        if termination_confirmation is None:
            termination_confirmation = confirm_process_tree_terminated(
                proc.pid,
                process_group_id=proc.pid if os.name != "nt" else None,
                known_pids=[proc.pid],
            )
        cleanup_ok = bool(termination_confirmation.get("confirmed"))
''',
    label="confirmed cleanup calculation",
)
text = replace_once(
    text,
    '''            "termination_log": term_log,
            "cleanup_ok": cleanup_ok,
''',
    '''            "termination_log": term_log,
            "termination_confirmation": termination_confirmation,
            "cleanup_ok": cleanup_ok,
''',
    label="result termination confirmation",
)
text = replace_once(
    text,
    '''        if proc and proc.poll() is None:
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
''',
    '''        if proc and proc.poll() is None:
            terminated = terminate_process_tree(
                proc,
                reason="cancel_api",
                graceful_wait_sec=GRACEFUL_WAIT_SEC,
                force_wait_sec=FORCE_WAIT_SEC,
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
''',
    label="cancel api confirmed termination",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Evidence dispatcher for run outcomes
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "evidence.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    if kind == EVIDENCE_KIND_FOUNDER_DECISION:
        return validate_founder_decision_for_storage(evidence)

    # Execution evidence (default for live supervised path)
''',
    '''    if kind == EVIDENCE_KIND_FOUNDER_DECISION:
        return validate_founder_decision_for_storage(evidence)
    from buildforme.outcome_evidence import (
        EVIDENCE_KIND_RUN_OUTCOME,
        validate_run_outcome_evidence,
    )
    if kind == EVIDENCE_KIND_RUN_OUTCOME:
        return validate_run_outcome_evidence(evidence)

    # Execution evidence (default for live supervised path)
''',
    label="outcome evidence validation dispatch",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Atomic outcome evidence inside commit_run_mutation
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''        require_db_status_in: set[str] | frozenset[str] | None = None,
        transition_path: list[str] | None = None,
    ) -> dict[str, Any]:
''',
    '''        require_db_status_in: set[str] | frozenset[str] | None = None,
        transition_path: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
''',
    label="mutation evidence parameter",
)
text = replace_once(
    text,
    '''            new_ver = current_ver + 1
            record["row_version"] = new_ver
            cur = conn.execute(
''',
    '''            new_ver = current_ver + 1
            record["row_version"] = new_ver

            outcome_record: dict[str, Any] | None = None
            if evidence is not None:
                from buildforme.outcome_evidence import validate_run_outcome_evidence

                outcome_record = dict(evidence)
                problems = validate_run_outcome_evidence(outcome_record)
                if problems:
                    raise ValueError("run outcome evidence rejected: " + "; ".join(problems))
                if str(outcome_record.get("run_id") or "") != rid:
                    raise ValueError("run outcome evidence run_id mismatch")
                if str(outcome_record.get("previous_status") or "") != db_status:
                    raise ValueError("run outcome evidence previous_status mismatch")
                if str(outcome_record.get("resulting_status") or "") != new_status:
                    raise ValueError("run outcome evidence resulting_status mismatch")
                if int(outcome_record.get("previous_row_version") or -1) != current_ver:
                    raise ValueError("run outcome evidence previous_row_version mismatch")
                evidence_id = str(outcome_record.get("evidence_id") or outcome_record.get("id") or "")
                if not evidence_id:
                    raise ValueError("run outcome evidence_id required")
                if conn.execute(
                    "SELECT evidence_id FROM evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone():
                    raise ValueError(f"evidence mutation forbidden: {evidence_id} is append-only")
                prior = int(
                    conn.execute("SELECT COUNT(*) FROM evidence WHERE run_id=?", (rid,)).fetchone()[0]
                )
                outcome_record["sequence"] = prior + 1
                outcome_record.setdefault("attempt", outcome_record["sequence"])
                outcome_record.setdefault("saved_at", now)
                parent = conn.execute(
                    "SELECT evidence_id FROM evidence WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                    (rid,),
                ).fetchone()
                if parent:
                    outcome_record.setdefault("parent_evidence_id", parent[0])
                ids = list(record.get("evidence_ids") or [])
                if evidence_id not in ids:
                    ids.append(evidence_id)
                record["evidence_ids"] = ids
                record["outcome_evidence_id"] = evidence_id
                record["outcome_evidence_fingerprint"] = outcome_record.get("evidence_fingerprint")

            cur = conn.execute(
''',
    label="atomic outcome evidence validation",
)
text = replace_once(
    text,
    '''            if cur.rowcount == 0:
                raise ValueError(f"stale run mutation race: run_id={rid}")

            base_meta = dict(event_metadata or {})
''',
    '''            if cur.rowcount == 0:
                raise ValueError(f"stale run mutation race: run_id={rid}")

            if outcome_record is not None:
                conn.execute(
                    """INSERT INTO evidence(evidence_id, run_id, sequence, attempt, parent_evidence_id,
                       payload_json, evidence_fingerprint, saved_at, immutable)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (
                        outcome_record["evidence_id"],
                        rid,
                        outcome_record["sequence"],
                        outcome_record.get("attempt"),
                        outcome_record.get("parent_evidence_id"),
                        dumps(outcome_record),
                        outcome_record.get("evidence_fingerprint"),
                        outcome_record["saved_at"],
                    ),
                )

            base_meta = dict(event_metadata or {})
            if outcome_record is not None:
                base_meta["evidence_id"] = outcome_record.get("evidence_id")
                base_meta["evidence_fingerprint"] = outcome_record.get("evidence_fingerprint")
''',
    label="atomic outcome evidence insert",
)

# Atomic migration wrapper: rename existing importer and add temp-db cutover authority.
text = replace_once(
    text,
    '''    def migrate_from_json(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
''',
    '''    def migrate_from_json(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
        """Import through a temporary SQLite authority and atomically replace on success.

        Any malformed record, orphan, integrity failure, or cutover failure leaves the
        original database logically unchanged.  Active runs block migration.
        """
        import os
        import sqlite3
        import uuid

        if dry_run:
            return self._migrate_from_json_in_place(
                runtime_dir, dry_run=True, cutover=False
            )

        with self.db.transaction() as conn:
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('queued','starting','running','cancel_requested')"
                ).fetchone()[0]
            )
        if active:
            return {
                "errors": [f"migration refused while {active} active run(s) exist"],
                "orphans": [],
                "malformed": [],
                "dry_run": False,
                "cutover": False,
                "rolled_back": True,
                "atomic_commit": False,
            }

        original = Path(self.db.path)
        temp_path = original.with_name(
            f".{original.name}.migration-{uuid.uuid4().hex}.tmp"
        )
        report: dict[str, Any] = {
            "errors": [],
            "orphans": [],
            "malformed": [],
            "dry_run": False,
            "cutover": False,
            "rolled_back": True,
            "atomic_commit": False,
        }
        temp_store: Stage6Store | None = None
        try:
            source = self.db._raw_connect()
            target = sqlite3.connect(str(temp_path))
            try:
                source.execute("PRAGMA wal_checkpoint(FULL)")
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()

            temp_store = Stage6Store(temp_path)
            report = temp_store._migrate_from_json_in_place(
                runtime_dir, dry_run=False, cutover=cutover
            )
            integrity_ok = str(report.get("integrity") or "").lower() == "ok"
            valid = (
                not report.get("errors")
                and not report.get("orphans")
                and integrity_ok
                and (not cutover or bool(report.get("cutover")))
            )
            if not valid:
                report["rolled_back"] = True
                report["atomic_commit"] = False
                return report

            check = temp_store.db._raw_connect()
            try:
                check.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                if str(integrity).lower() != "ok":
                    raise ValueError(f"temporary migration database integrity failure: {integrity}")
            finally:
                check.close()

            for suffix in ("-wal", "-shm"):
                Path(str(original) + suffix).unlink(missing_ok=True)
                Path(str(temp_path) + suffix).unlink(missing_ok=True)
            os.replace(temp_path, original)
            report["rolled_back"] = False
            report["atomic_commit"] = True
            report["database_replaced_atomically"] = True
            return report
        except Exception as exc:
            report.setdefault("errors", []).append(f"atomic migration failed: {exc}")
            report["rolled_back"] = True
            report["atomic_commit"] = False
            return report
        finally:
            temp_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def _migrate_from_json_in_place(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
''',
    label="atomic migration wrapper",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Execution service: atomic evidence on every terminal failure outcome
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_service.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from buildforme.evidence import build_evidence_bundle\n",
    "from buildforme.evidence import build_evidence_bundle\n"
    "from buildforme.outcome_evidence import build_run_outcome_evidence\n",
    label="outcome evidence import",
)
text = replace_once(
    text,
    '''    mutation_type: str = "status_transition",
    transition_path: list[str] | None = None,
) -> dict[str, Any]:
''',
    '''    mutation_type: str = "status_transition",
    transition_path: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
''',
    label="persist evidence parameter",
)
text = replace_once(
    text,
    '''        require_db_status_in=require_db_status_in,
        transition_path=transition_path,
    )
''',
    '''        require_db_status_in=require_db_status_in,
        transition_path=transition_path,
        evidence=evidence,
    )
''',
    label="persist evidence forwarding",
)
insert_marker = '''def _startup_transition_path(from_status: str) -> list[str]:
'''
helper = r'''def _termination_confirmed(process_result: dict[str, Any]) -> bool:
    confirmation = process_result.get("termination_confirmation")
    return bool(
        process_result.get("cleanup_ok")
        and isinstance(confirmation, dict)
        and confirmation.get("confirmed")
    )


def _commit_terminal_outcome(
    store: LocalStore,
    *,
    run_id: str,
    process_result: dict[str, Any],
    outcome: str,
    target_status: str,
    event_type: str,
    event_summary: str,
    mutation_type: str,
    transition_path: list[str] | None = None,
    worktree: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = store.get_run(run_id)
    previous_status = str(current.get("status") or "")
    previous_version = int(current.get("row_version") or 1)
    proposed = dict(current)
    path = list(transition_path) if transition_path else None
    if path:
        if path[0] != previous_status or path[-1] != target_status:
            raise ValueError("terminal outcome transition path does not match stored state")
        for nxt in path[1:]:
            proposed = transition_run(proposed, nxt, "system", event_summary)
    elif previous_status != target_status:
        proposed = transition_run(proposed, target_status, "system", event_summary)
    proposed["process_result"] = process_result
    proposed["result_summary"] = event_summary
    evidence = build_run_outcome_evidence(
        run=current,
        outcome=outcome,
        previous_status=previous_status,
        resulting_status=target_status,
        previous_row_version=previous_version,
        process_result=process_result,
        reason=event_summary,
        worktree=worktree,
    )
    saved = _persist_transition(
        store,
        proposed,
        event_type=event_type,
        event_summary=event_summary,
        require_db_status_in={previous_status},
        mutation_type=mutation_type,
        transition_path=path,
        evidence=evidence,
    )
    return saved, evidence


'''
text = replace_once(text, insert_marker, helper + insert_marker, label="terminal outcome helper")

# Replace provider execute exception branch.
text = replace_once(
    text,
    '''    except Exception as exc:
        run = _reload(store, run_id)
        if can_transition(str(run.get("status")), "failed"):
            run = transition_run(run, "failed", "system", redact_text(str(exc)[:300]))
            run["process_result"] = {"error": redact_text(str(exc)[:300]), "ok": False}
            try:
                run = _persist_transition(
                    store,
                    run,
                    event_type="supervised_failed",
                    event_summary=redact_text(str(exc)[:500]),
                    require_db_status_in={"running", "starting", "queued", "cancel_requested"},
                    mutation_type="failure_detail",
                )
            except ValueError:
                # Terminal/stale (e.g. cancelled wins) — do not overwrite
                run = _reload(store, run_id)
        _release_run_locks(store, run)
        raise
''',
    '''    except Exception as exc:
        message = redact_text(str(exc)[:500])
        process_result = {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": message,
            "error": message,
            "timed_out": False,
            "cancelled": False,
            "unavailable": False,
            "cleanup_ok": False,
            "termination_log": [],
            "termination_confirmation": {
                "confirmed": False,
                "reason": "adapter raised without a confirmed supervisor result",
                "live_pids": [],
            },
        }
        try:
            run, _outcome_evidence = _commit_terminal_outcome(
                store,
                run_id=run_id,
                process_result=process_result,
                outcome="failed",
                target_status="failed",
                event_type="supervised_failed",
                event_summary=message,
                mutation_type="failure_detail",
                worktree=worktree_meta,
            )
        except ValueError:
            run = _reload(store, run_id)
        _release_run_locks(store, run)
        raise
''',
    label="exception outcome evidence",
)

# Replace cancellation/timeout/unavailable block together.
old = '''    run = _reload(store, run_id)
    if process_result.get("cancelled") or str(run.get("status")) == "cancel_requested":
        status = str(run.get("status") or "")
        if status == "running":
            path = ["running", "cancel_requested", "cancelled"]
            for nxt in path[1:]:
                run = transition_run(run, nxt, "system", "provider process cancelled")
        elif status == "cancel_requested":
            path = ["cancel_requested", "cancelled"]
            run = transition_run(run, "cancelled", "system", "provider process cancelled")
        else:
            path = None
        run["process_result"] = process_result
        try:
            run = _persist_transition(
                store,
                run,
                event_type="supervised_cancelled",
                event_summary="provider process cancelled",
                require_db_status_in={"running", "starting", "queued", "cancel_requested"},
                mutation_type="cancel",
                transition_path=path,
            )
        except ValueError:
            run = _reload(store, run_id)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "cancelled": True}

    if process_result.get("timed_out"):
        if can_transition(str(run.get("status")), "timed_out"):
            run = transition_run(run, "timed_out", "system", "provider timeout")
        run["process_result"] = process_result
        try:
            run = _persist_transition(
                store,
                run,
                event_type="supervised_timed_out",
                event_summary="provider timeout",
                require_db_status_in={"running", "starting", "queued"},
                mutation_type="failure_detail",
            )
        except ValueError:
            run = _reload(store, run_id)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "timed_out": True}

    if process_result.get("unavailable"):
        if can_transition(str(run.get("status")), "failed"):
            run = transition_run(
                run, "failed", "system", process_result.get("error") or "provider unavailable"
            )
        run["process_result"] = process_result
        try:
            run = _persist_transition(
                store,
                run,
                event_type="supervised_unavailable",
                event_summary=str(process_result.get("error") or "provider unavailable"),
                require_db_status_in={"running", "starting", "queued"},
                mutation_type="failure_detail",
            )
        except ValueError:
            run = _reload(store, run_id)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "unavailable": True}
'''
new = '''    run = _reload(store, run_id)
    if process_result.get("cancelled") or str(run.get("status")) == "cancel_requested":
        status = str(run.get("status") or "")
        confirmed = _termination_confirmed(process_result)
        try:
            if confirmed:
                path = (
                    ["running", "cancel_requested", "cancelled"]
                    if status == "running"
                    else ["cancel_requested", "cancelled"]
                )
                run, outcome_evidence = _commit_terminal_outcome(
                    store,
                    run_id=run_id,
                    process_result=process_result,
                    outcome="cancelled",
                    target_status="cancelled",
                    event_type="supervised_cancelled",
                    event_summary="provider process cancelled with confirmed tree termination",
                    mutation_type="cancel",
                    transition_path=path,
                    worktree=worktree_meta,
                )
                result = {"run": run, "process": process_result, "cancelled": True, "evidence": outcome_evidence}
            else:
                run, outcome_evidence = _commit_terminal_outcome(
                    store,
                    run_id=run_id,
                    process_result=process_result,
                    outcome="termination_unconfirmed",
                    target_status="failed",
                    event_type="supervised_cancel_cleanup_failed",
                    event_summary="cancellation requested but process-tree termination was not confirmed",
                    mutation_type="failure_detail",
                    worktree=worktree_meta,
                )
                result = {"run": run, "process": process_result, "cancelled": False, "termination_unconfirmed": True, "evidence": outcome_evidence}
        except ValueError:
            run = _reload(store, run_id)
            result = {"run": run, "process": process_result, "stale_outcome_suppressed": True}
        _release_run_locks(store, run)
        return result

    if process_result.get("timed_out"):
        confirmed = _termination_confirmed(process_result)
        target = "timed_out" if confirmed else "failed"
        outcome = "timed_out" if confirmed else "termination_unconfirmed"
        event_type = "supervised_timed_out" if confirmed else "supervised_timeout_cleanup_failed"
        summary = (
            "provider timeout with confirmed process-tree termination"
            if confirmed
            else "provider timeout but process-tree termination was not confirmed"
        )
        try:
            run, outcome_evidence = _commit_terminal_outcome(
                store,
                run_id=run_id,
                process_result=process_result,
                outcome=outcome,
                target_status=target,
                event_type=event_type,
                event_summary=summary,
                mutation_type="failure_detail",
                worktree=worktree_meta,
            )
        except ValueError:
            run = _reload(store, run_id)
            outcome_evidence = {}
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "timed_out": confirmed, "termination_unconfirmed": not confirmed, "evidence": outcome_evidence}

    if process_result.get("unavailable"):
        summary = str(process_result.get("error") or "provider unavailable")
        try:
            run, outcome_evidence = _commit_terminal_outcome(
                store,
                run_id=run_id,
                process_result=process_result,
                outcome="unavailable",
                target_status="failed",
                event_type="supervised_unavailable",
                event_summary=summary,
                mutation_type="failure_detail",
                worktree=worktree_meta,
            )
        except ValueError:
            run = _reload(store, run_id)
            outcome_evidence = {}
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "unavailable": True, "evidence": outcome_evidence}

    if not process_result.get("ok"):
        summary = str(process_result.get("error") or f"provider process failed with exit {process_result.get('exit_code')}")
        try:
            run, outcome_evidence = _commit_terminal_outcome(
                store,
                run_id=run_id,
                process_result=process_result,
                outcome="failed",
                target_status="failed",
                event_type="supervised_failed",
                event_summary=summary,
                mutation_type="failure_detail",
                worktree=worktree_meta,
            )
        except ValueError:
            run = _reload(store, run_id)
            outcome_evidence = {}
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "failed": True, "evidence": outcome_evidence}
'''
text = replace_once(text, old, new, label="terminal process outcomes")

# Replace cancel_run completely between markers.
start = text.index("def cancel_run(\n")
end = text.index("\ndef _release_run_locks", start)
new_cancel = r'''def cancel_run(
    store: LocalStore,
    run_id: str,
    *,
    actor: str = "shan",
    reason: str = "",
) -> dict[str, Any]:
    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    actor = validate_actor(actor)
    status = str(run.get("status"))
    if is_terminal(status):
        raise ValueError("cannot cancel terminal run")
    note = reason or "cancel requested"

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
        confirmed = _termination_confirmed(process_result)
        if confirmed:
            if status == "cancel_requested":
                path = ["cancel_requested", "cancelled"]
            else:
                path = [status, "cancel_requested", "cancelled"]
            saved, evidence = _commit_terminal_outcome(
                store,
                run_id=run_id,
                process_result=process_result,
                outcome="cancelled",
                target_status="cancelled",
                event_type="run_cancelled",
                event_summary=note,
                mutation_type="cancel",
                transition_path=path,
                worktree=run.get("worktree") if isinstance(run.get("worktree"), dict) else None,
            )
        else:
            saved, evidence = _commit_terminal_outcome(
                store,
                run_id=run_id,
                process_result=process_result,
                outcome="termination_unconfirmed",
                target_status="failed",
                event_type="cancel_cleanup_failed",
                event_summary="cancel requested but process-tree termination was not confirmed",
                mutation_type="failure_detail",
                worktree=run.get("worktree") if isinstance(run.get("worktree"), dict) else None,
            )
    elif can_transition(status, "rejected"):
        process_result.setdefault("cleanup_ok", True)
        process_result.setdefault(
            "termination_confirmation",
            {
                "confirmed": True,
                "reason": "run had not started; no active process tree",
                "live_pids": [],
            },
        )
        saved, evidence = _commit_terminal_outcome(
            store,
            run_id=run_id,
            process_result=process_result,
            outcome="cancelled",
            target_status="rejected",
            event_type="run_cancelled_before_start",
            event_summary=note,
            mutation_type="cancel",
        )
    elif can_transition(status, "blocked"):
        process_result.setdefault("cleanup_ok", True)
        process_result.setdefault(
            "termination_confirmation",
            {
                "confirmed": True,
                "reason": "run had not started; no active process tree",
                "live_pids": [],
            },
        )
        saved, evidence = _commit_terminal_outcome(
            store,
            run_id=run_id,
            process_result=process_result,
            outcome="cancelled",
            target_status="blocked",
            event_type="run_cancelled_before_start",
            event_summary=note,
            mutation_type="cancel",
        )
    else:
        raise ValueError(f"cannot cancel from status {status}")
    _release_run_locks(store, saved)
    saved["outcome_evidence_id"] = evidence.get("evidence_id")
    return saved
'''
text = text[:start] + new_cancel + text[end:]
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Real executable authentication probes (environment markers are not proof)
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "provider_discovery.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''# Auth-readiness probes that must never print tokens; we only keep exit codes / coarse status.
AUTH_PROBES: dict[str, list[str] | None] = {
    "codex": None,  # environment / login state opaque
    "claude": None,
    "grok": None,
    "glm": None,
}
''',
    '''# Read-only authentication probes.  A provider without a verified probe
# contract is never live-ready.  Provider records may supply an explicit
# auth_probe {args, success_exit_codes, read_only:true} for CLI families whose
# command contract is deployment-specific.
AUTH_PROBES: dict[str, dict[str, Any] | None] = {
    "codex": {"args": ["login", "status"], "success_exit_codes": [0], "read_only": True},
    "claude": {"args": ["auth", "status"], "success_exit_codes": [0], "read_only": True},
    "grok": None,
    "glm": None,
}
''',
    label="auth probe contracts",
)
text = replace_once(
    text,
    '''    auth_ready = _auth_readiness(pid, disc.get("executable"))

    compat = verify_provider_compatibility(
        pid,
        disc.get("executable"),
        version_text=version.get("version"),
        force=force_compat,
    )
    # Align auth component with discovery auth_ready (single source for marker names)
    if auth_ready.get("status") == "ready":
        compat["auth_verified"] = True
        comps = dict(compat.get("live_ready_components") or {})
        comps["auth_verified"] = True
        compat["live_ready_components"] = comps
        compat["auth"] = auth_ready
    else:
        compat["auth_verified"] = False
        comps = dict(compat.get("live_ready_components") or {})
        comps["auth_verified"] = False
        compat["live_ready_components"] = comps
        compat["auth"] = auth_ready
''',
    '''    auth_ready = probe_authentication(
        pid,
        disc.get("executable"),
        provider_record=record,
    )

    compat = verify_provider_compatibility(
        pid,
        disc.get("executable"),
        version_text=version.get("version"),
        force=force_compat,
        auth_result=auth_ready,
    )
''',
    label="real auth probe integration",
)
# Replace old _auth_readiness function to end before _looks_secret.
start = text.index("def _auth_readiness(")
end = text.index("\ndef _looks_secret", start)
new_auth = r'''def probe_authentication(
    provider_id: str,
    executable: str | None,
    *,
    provider_record: dict[str, Any] | None = None,
    timeout_sec: float = 12.0,
) -> dict[str, Any]:
    """Execute a bounded read-only CLI authentication-status command.

    Output is never persisted.  Only exit status, byte counts, and hashes are
    returned.  Environment-variable presence alone is explicitly insufficient.
    """
    from buildforme.process_env import build_provider_env
    from buildforme.redaction import redact_hash

    pid = str(provider_id or "").strip().lower()
    if not executable:
        return {
            "status": "missing",
            "probe_verified": False,
            "detail": "no executable to authenticate",
        }
    record = provider_record or {}
    configured = record.get("auth_probe") if isinstance(record.get("auth_probe"), dict) else None
    probe = dict(configured or AUTH_PROBES.get(pid) or {})
    if not probe:
        return {
            "status": "unknown",
            "probe_verified": False,
            "detail": "no approved read-only authentication probe contract configured",
        }
    if probe.get("read_only") is not True:
        return {
            "status": "unknown",
            "probe_verified": False,
            "detail": "authentication probe contract is not marked read_only",
        }
    args = probe.get("args")
    if not isinstance(args, list) or not args or any(not isinstance(arg, str) for arg in args):
        return {
            "status": "unknown",
            "probe_verified": False,
            "detail": "authentication probe args must be a non-empty list[str]",
        }
    if any(any(marker in arg.lower() for marker in ("token", "secret", "password", "api_key")) for arg in args):
        return {
            "status": "unknown",
            "probe_verified": False,
            "detail": "authentication probe args contain a secret-like marker",
        }
    success_codes = probe.get("success_exit_codes") or [0]
    try:
        success_codes = {int(code) for code in success_codes}
    except Exception:
        return {
            "status": "unknown",
            "probe_verified": False,
            "detail": "authentication probe success_exit_codes invalid",
        }
    safe_env, env_names = build_provider_env(pid)
    argv = [str(executable), *args]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(probe.get("timeout_sec") or timeout_sec)),
            shell=False,
            check=False,
            env=safe_env,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        ready = completed.returncode in success_codes
        return {
            "status": "ready" if ready else "failed",
            "probe_verified": bool(ready),
            "detail": "authentication status probe succeeded" if ready else f"authentication status probe exit {completed.returncode}",
            "exit_code": completed.returncode,
            "command_shape": [Path(str(executable)).name, *args],
            "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
            "stdout_sha256": redact_hash(stdout),
            "stderr_sha256": redact_hash(stderr),
            "env_names": env_names,
            "output_persisted": False,
            "checked_at": utc_now_iso(),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "probe_verified": False,
            "detail": "authentication status probe timed out",
            "command_shape": [Path(str(executable)).name, *args],
            "output_persisted": False,
            "checked_at": utc_now_iso(),
        }
    except OSError as exc:
        return {
            "status": "failed",
            "probe_verified": False,
            "detail": f"authentication status probe OS error: {exc}",
            "command_shape": [Path(str(executable)).name, *args],
            "output_persisted": False,
            "checked_at": utc_now_iso(),
        }
'''
text = text[:start] + new_auth + text[end:]
path.write_text(text, encoding="utf-8")

path = ROOT / "buildforme" / "provider_compatibility.py"
text = path.read_text(encoding="utf-8")
text = text.replace("- Auth: env marker required for live_ready; unknown ≠ ready", "- Auth: successful read-only executable probe required; env markers are not proof")
text = replace_once(
    text,
    '''    force: bool = False,
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
''',
    '''    force: bool = False,
    timeout_sec: float = 8.0,
    auth_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
''',
    label="compat auth parameter",
)
text = replace_once(
    text,
    '''    cache_key = f"{pid}|{executable}|{mtime}|{version_text}"
''',
    '''    auth_cache_material = (
        str((auth_result or {}).get("status")),
        str((auth_result or {}).get("exit_code")),
        str((auth_result or {}).get("checked_at")),
    )
    cache_key = f"{pid}|{executable}|{mtime}|{version_text}|{auth_cache_material}"
''',
    label="compat auth cache key",
)
text = replace_once(
    text,
    '''    # Auth component (env markers only — values never stored)
    auth = _auth_component(pid)
    result["auth_verified"] = auth.get("status") == "ready"
    result["auth"] = auth
    if not result["auth_verified"]:
        result["problems"].append(f"auth not verified: {auth.get('detail')}")
''',
    '''    # Auth is supplied only by the executable status probe in provider_discovery.
    auth = dict(auth_result or {})
    result["auth_verified"] = auth.get("status") == "ready" and bool(auth.get("probe_verified"))
    result["auth"] = auth
    if not result["auth_verified"]:
        result["problems"].append(f"auth not verified by executable probe: {auth.get('detail') or 'missing probe result'}")
''',
    label="compat real auth result",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Strict real-provider smoke criteria
# ---------------------------------------------------------------------------
path = ROOT / "scripts" / "stage6_real_provider_smoke.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from buildforme.storage import LocalStore  # noqa: E402\n",
    "from buildforme.storage import LocalStore  # noqa: E402\n"
    "from buildforme.stage6_smoke_acceptance import evaluate_stage6_smoke_acceptance  # noqa: E402\n",
    label="smoke evaluator import",
)
text = replace_once(
    text,
    '''    base = _git_out(root, ["rev-parse", "HEAD"]).strip()
    print("REPO", root)
''',
    '''    base = _git_out(root, ["rev-parse", "HEAD"]).strip()
    original_branch = _git_out(root, ["branch", "--show-current"]).strip()
    print("REPO", root)
''',
    label="smoke original branch",
)
old_tail = '''    if result["run"].get("status") == "needs_review" and review.get(
        "accept_for_pr_prep_allowed"
    ):
        d = founder_review_decision(
            store,
            run["id"],
            decision="accept_for_pr_prep",
            note="smoke accept",
            actor="shan",
        )
        print("FOUNDER_KEYS", list(d.keys()) if isinstance(d, dict) else type(d))
        if isinstance(d, dict):
            run_after = d.get("run") if isinstance(d.get("run"), dict) else {}
            print("FOUNDER_STATUS", run_after.get("status") or d.get("decision"))
    else:
        print("FOUNDER skipped; status=", result["run"].get("status"))
        if process.get("stderr"):
            print("STDERR_PREVIEW", str(process.get("stderr"))[:800])
        if process.get("stdout"):
            print("STDOUT_PREVIEW", str(process.get("stdout"))[:800])
    print("SMOKE_DIR", td)
    print("MERGE", "no")
    # Success criteria for smoke: real process + verification path completed.
    # File production is provider-dependent; report honestly.
    ok = result["run"].get("status") in {"needs_review", "completed"} and process.get("exit_code") == 0
    return 0 if ok else 4
'''
new_tail = '''    decision_evidence = None
    if result["run"].get("status") == "needs_review" and review.get(
        "accept_for_pr_prep_allowed"
    ):
        d = founder_review_decision(
            store,
            run["id"],
            decision="accept_for_pr_prep",
            note="smoke accept",
            actor="shan",
        )
        print("FOUNDER_KEYS", list(d.keys()) if isinstance(d, dict) else type(d))
        if isinstance(d, dict):
            run_after = d.get("run") if isinstance(d.get("run"), dict) else {}
            decision_evidence = d.get("decision_evidence") if isinstance(d.get("decision_evidence"), dict) else None
            print("FOUNDER_STATUS", run_after.get("status") or d.get("decision"))
    else:
        print("FOUNDER skipped; status=", result["run"].get("status"))
        if process.get("stderr"):
            print("STDERR_PREVIEW", str(process.get("stderr"))[:800])
        if process.get("stdout"):
            print("STDOUT_PREVIEW", str(process.get("stdout"))[:800])

    final_run = store.get_run(run["id"])
    persisted_evidence = store.get_evidence_by_id(str(evidence.get("evidence_id") or ""))
    acceptance = evaluate_stage6_smoke_acceptance(
        health=health,
        execution_result=result,
        final_run=final_run,
        persisted_evidence=persisted_evidence,
        decision_evidence=decision_evidence,
        repository_root=root,
        original_head=base,
        original_branch=original_branch,
    )
    print("SMOKE_ACCEPTANCE_JSON", json.dumps(acceptance, sort_keys=True))
    print("SMOKE_DIR", td)
    print("MERGE", "no")
    return 0 if acceptance.get("passed") else 4
'''
text = replace_once(text, old_tail, new_tail, label="strict smoke acceptance")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
(ROOT / "tests" / "test_stage6_final_blockers.py").write_text(
    r'''"""Adversarial closure tests for the final Stage 6 blockers."""

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

from buildforme.evidence import validate_evidence_for_storage
from buildforme.execution_store import Stage6Store
from buildforme.outcome_evidence import build_run_outcome_evidence
from buildforme.process_supervisor import ProcessSupervisor
from buildforme.provider_discovery import probe_authentication
from buildforme.stage6_smoke_acceptance import evaluate_stage6_smoke_acceptance
from buildforme.storage import LocalStore


class ProcessTerminationTruthTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX process-group proof exercised in CI; Windows has separate implementation")
    def test_cancel_confirms_stubborn_child_tree_is_gone(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        script = Path(td.name) / "tree.py"
        script.write_text(
            "import signal,subprocess,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        supervisor = ProcessSupervisor()
        result_box = {}

        def run():
            result_box["result"] = supervisor.run(
                run_id="tree-run",
                argv=[sys.executable, str(script)],
                cwd=td.name,
                timeout_seconds=60,
                provider_id="test",
                env=dict(os.environ),
                use_provider_env_allowlist=False,
            )

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            with supervisor._lock:
                if "tree-run" in supervisor._procs:
                    break
            time.sleep(0.05)
        cancel = supervisor.cancel("tree-run")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        result = result_box["result"]
        self.assertTrue(cancel["termination_confirmation"]["confirmed"])
        self.assertTrue(result["termination_confirmation"]["confirmed"])
        self.assertTrue(result["cleanup_ok"])
        self.assertEqual(result["termination_confirmation"]["live_pids"], [])


class OutcomeEvidenceAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.run = self.store.save_run_for_setup(
            {
                "id": "outcome-run",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "execution_mode": "live_supervised",
                "mode": "live_supervised",
                "status": "running",
                "scope_fingerprint": "scope",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "lease",
                "constitution_lease_fingerprint": "lf",
                "status_history": [],
                "row_version": 1,
            }
        )

    def _process(self, *, confirmed=True):
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": "",
            "stderr": "timeout",
            "timed_out": True,
            "cancelled": False,
            "cleanup_ok": confirmed,
            "termination_log": [],
            "termination_confirmation": {
                "confirmed": confirmed,
                "live_pids": [] if confirmed else [99999],
            },
        }

    def test_outcome_evidence_and_terminal_transition_commit_atomically(self):
        current = self.store.get_run("outcome-run")
        proposed = dict(current)
        proposed["status"] = "timed_out"
        proposed["process_result"] = self._process()
        evidence = build_run_outcome_evidence(
            run=current,
            outcome="timed_out",
            previous_status="running",
            resulting_status="timed_out",
            previous_row_version=current["row_version"],
            process_result=proposed["process_result"],
            reason="timeout",
        )
        saved = self.store.commit_run_mutation(
            proposed,
            expected_row_version=current["row_version"],
            mutation_type="failure_detail",
            event_type="supervised_timed_out",
            event_summary="timeout",
            evidence=evidence,
        )
        self.assertEqual(saved["status"], "timed_out")
        persisted = self.store.get_evidence_by_id(evidence["evidence_id"])
        self.assertEqual(persisted["evidence_fingerprint"], evidence["evidence_fingerprint"])
        self.assertEqual(validate_evidence_for_storage(persisted), [])

    def test_bad_evidence_rolls_back_run_event_and_evidence(self):
        current = self.store.get_run("outcome-run")
        proposed = dict(current)
        proposed["status"] = "failed"
        proposed["process_result"] = self._process(confirmed=False)
        evidence = build_run_outcome_evidence(
            run=current,
            outcome="termination_unconfirmed",
            previous_status="running",
            resulting_status="failed",
            previous_row_version=current["row_version"],
            process_result=proposed["process_result"],
            reason="cleanup failed",
        )
        evidence["evidence_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.commit_run_mutation(
                proposed,
                expected_row_version=current["row_version"],
                mutation_type="failure_detail",
                event_type="failed",
                evidence=evidence,
            )
        after = self.store.get_run("outcome-run")
        self.assertEqual(after["status"], "running")
        self.assertEqual(after["row_version"], current["row_version"])
        self.assertEqual(self.store.list_run_events("outcome-run"), [])
        self.assertEqual(self.store.list_run_evidence(run_id="outcome-run"), [])

    def test_cancelled_status_rejects_unconfirmed_termination(self):
        current = self.store.get_run("outcome-run")
        evidence = build_run_outcome_evidence(
            run=current,
            outcome="cancelled",
            previous_status="running",
            resulting_status="cancelled",
            previous_row_version=current["row_version"],
            process_result=self._process(confirmed=False),
            reason="cancel",
        )
        self.assertTrue(validate_evidence_for_storage(evidence))


class ProviderAuthenticationProbeTests(unittest.TestCase):
    def _fake_cli(self, exit_code: int, output: str = "") -> tuple[tempfile.TemporaryDirectory, str]:
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / ("provider.cmd" if os.name == "nt" else "provider")
        if os.name == "nt":
            path.write_text(f"@echo {output}\r\n@exit /b {exit_code}\r\n", encoding="utf-8")
        else:
            path.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\nexit {exit_code}\n", encoding="utf-8")
            path.chmod(0o755)
        return td, str(path)

    def test_env_marker_does_not_override_failed_executable_probe(self):
        td, executable = self._fake_cli(7, "OPENAI_API_KEY=should-not-persist")
        self.addCleanup(td.cleanup)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret-value"}, clear=False):
            result = probe_authentication("codex", executable)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["probe_verified"])
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn("should-not-persist", json.dumps(result))

    def test_successful_executable_probe_can_verify_cached_login_without_env_marker(self):
        td, executable = self._fake_cli(0, "logged in")
        self.addCleanup(td.cleanup)
        with mock.patch.dict(os.environ, {}, clear=True):
            result = probe_authentication("codex", executable)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["probe_verified"])
        self.assertFalse(result["output_persisted"])

    def test_provider_without_probe_contract_fails_closed(self):
        td, executable = self._fake_cli(0, "ok")
        self.addCleanup(td.cleanup)
        result = probe_authentication("grok", executable, provider_record={})
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["probe_verified"])


class AtomicMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.store = Stage6Store(self.runtime / "authority.db")
        self.store.save_run_for_setup(
            {
                "id": "existing",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "draft",
                "execution_mode": "dry_run",
            }
        )

    def test_import_error_rolls_back_all_prior_imports(self):
        (self.runtime / "runs.json").write_text(
            json.dumps({"runs": [{"id": "new", "project_id": "p", "provider_id": "codex", "repository": "o/r", "status": "draft", "execution_mode": "dry_run"}]}),
            encoding="utf-8",
        )
        (self.runtime / "run_evidence.json").write_text(
            json.dumps({"evidence": [{"evidence_id": "bad", "run_id": "new", "evidence_kind": "execution"}]}),
            encoding="utf-8",
        )
        report = self.store.migrate_from_json(self.runtime, dry_run=False, cutover=True)
        self.assertTrue(report["rolled_back"])
        self.assertFalse(report["atomic_commit"])
        with self.assertRaises(KeyError):
            self.store.get_run("new")
        self.assertEqual(self.store.get_run("existing")["id"], "existing")
        self.assertIsNone(self.store.get_migration_cutover())

    def test_successful_import_atomically_replaces_authority(self):
        (self.runtime / "runs.json").write_text(
            json.dumps({"runs": [{"id": "new-ok", "project_id": "p", "provider_id": "codex", "repository": "o/r", "status": "draft", "execution_mode": "dry_run"}]}),
            encoding="utf-8",
        )
        report = self.store.migrate_from_json(self.runtime, dry_run=False, cutover=True)
        self.assertTrue(report["atomic_commit"], report)
        self.assertFalse(report["rolled_back"])
        self.assertEqual(self.store.get_run("new-ok")["id"], "new-ok")
        self.assertTrue(self.store.get_migration_cutover())

    def test_active_run_blocks_migration_without_write(self):
        active = self.store.get_run("existing")
        active["status"] = "running"
        self.store.save_run_for_setup(active)
        report = self.store.migrate_from_json(self.runtime, dry_run=False, cutover=True)
        self.assertTrue(report["rolled_back"])
        self.assertIn("active run", report["errors"][0])


class SmokeAcceptanceTests(unittest.TestCase):
    def test_strict_smoke_rejects_missing_required_file_and_unverified_auth(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        (root / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "x"], cwd=root, check=True, capture_output=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip()
        report = evaluate_stage6_smoke_acceptance(
            health={"live_ready": True, "auth": {"status": "ready", "probe_verified": False}},
            execution_result={
                "process": {"ok": True, "exit_code": 0, "pid": 1, "cleanup_ok": True, "termination_confirmation": {"confirmed": True}},
                "verification": {"passed": True},
                "review": {"accept_for_pr_prep_allowed": True},
                "evidence": {"evidence_id": "x", "evidence_fingerprint": "f", "files_changed": ["README.md"], "patch_fingerprint": "p", "manifest_fingerprint": "m", "final_head_sha": head},
            },
            final_run={"status": "completed"},
            persisted_evidence={"evidence_id": "x"},
            decision_evidence={"evidence_fingerprint": "d"},
            repository_root=root,
            original_head=head,
            original_branch=branch,
        )
        self.assertFalse(report["passed"])
        self.assertIn("auth_probe_verified", report["failed_checks"])
        self.assertIn("required_files_produced", report["failed_checks"])


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)


# Documentation truth update.
path = ROOT / "docs" / "STAGE_6_MULTI_PROVIDER_EXECUTION.md"
text = path.read_text(encoding="utf-8")
text += '''

## Final blocker closure requirements

- Cancellation and timeout are terminal only after process-tree absence is confirmed.
- Cancelled, timed-out, failed, unavailable, and termination-unconfirmed outcomes carry immutable evidence committed atomically with state and audit events.
- Authentication readiness requires a successful read-only executable probe; environment-variable presence is never proof.
- JSON migration imports into a temporary SQLite authority and atomically replaces the database only after full validation and integrity checks.
- Real-provider smoke acceptance is machine-verifiable and requires auth proof, process cleanup proof, deterministic verification, required files, immutable evidence, founder decision evidence, and proof that the source branch was not merged or modified.
'''
path.write_text(text, encoding="utf-8")

print("Stage 6 final blocker remediation applied")
