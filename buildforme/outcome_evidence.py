"""Immutable evidence for cancelled, timed-out, failed, and unavailable runs."""

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
