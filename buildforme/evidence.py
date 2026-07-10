"""Append-oriented evidence collection — provider claims alone are not evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from buildforme.redaction import redact_hash, redact_mapping, redact_process_result, redact_text
from buildforme.storage import utc_now_iso


def build_evidence_bundle(
    *,
    run: dict[str, Any],
    packet: dict[str, Any] | None,
    process_result: dict[str, Any] | None,
    worktree: dict[str, Any] | None,
    diff: dict[str, Any] | None,
    provider_health: dict[str, Any] | None,
    verification: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    constitution_result: dict[str, Any] | None = None,
    cleanup_result: dict[str, Any] | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    packet = packet or {}
    process_result = redact_process_result(process_result or {})
    worktree = worktree or {}
    diff = diff or {}
    provider_health = redact_mapping(provider_health or {})
    verification = verification or {}
    manifest = diff.get("manifest") if isinstance(diff.get("manifest"), dict) else {}
    if not manifest and isinstance(verification.get("changed_file_manifest"), dict):
        manifest = verification["changed_file_manifest"]

    files = list(manifest.get("files") or [])
    paths = list(manifest.get("files_changed") or diff.get("files_changed") or [])

    evidence_id = f"ev-{uuid.uuid4().hex[:16]}"
    bundle: dict[str, Any] = {
        "schema": "buildforme.evidence.v1",
        "evidence_id": evidence_id,
        "id": evidence_id,
        "immutable": True,
        "collected_at": utc_now_iso(),
        "task_id": run.get("task_id"),
        "packet_id": run.get("packet_id") or packet.get("id"),
        "run_id": run.get("id"),
        "project_id": run.get("project_id"),
        "attempt": attempt or int(run.get("attempt") or 0) + 1,
        "repository": run.get("repository"),
        "repository_local_path": run.get("repository_local_path"),
        "approved_baseline_commit": run.get("baseline_commit") or worktree.get("baseline_commit"),
        "baseline_commit": run.get("baseline_commit") or worktree.get("baseline_commit"),
        "post_run_head_sha": worktree.get("head_commit") or run.get("head_commit"),
        "head_commit": worktree.get("head_commit") or run.get("head_commit"),
        "branch": worktree.get("branch") or run.get("target_branch"),
        "worktree_path": worktree.get("worktree_path") or run.get("worktree_path"),
        "worktree_identity": {
            "path": worktree.get("worktree_path"),
            "baseline": worktree.get("baseline_commit"),
            "fresh_branch": worktree.get("fresh_branch"),
        },
        "provider_id": run.get("provider_id"),
        "transport": run.get("transport") or "cli",
        "provider_version": provider_health.get("version") or run.get("provider_version"),
        "provider_executable": provider_health.get("executable"),
        "operating_mode": run.get("operating_mode"),
        "risk": run.get("risk"),
        "requested_capabilities": list(run.get("requested_capabilities") or []),
        "allowed_files": list(packet.get("allowed_files") or []),
        "forbidden_files": list(packet.get("forbidden_files") or []),
        "process": {
            "argv": process_result.get("argv"),
            "exit_code": process_result.get("exit_code"),
            "timed_out": process_result.get("timed_out"),
            "cancelled": process_result.get("cancelled"),
            "duration_seconds": process_result.get("duration_seconds"),
            "truncated_stdout": process_result.get("truncated_stdout"),
            "truncated_stderr": process_result.get("truncated_stderr"),
            "stdout_sha256": process_result.get("stdout_sha256") or redact_hash(process_result.get("stdout") or ""),
            "stderr_sha256": process_result.get("stderr_sha256") or redact_hash(process_result.get("stderr") or ""),
            "stdout_preview": redact_text((process_result.get("stdout") or "")[:4000]),
            "stderr_preview": redact_text((process_result.get("stderr") or "")[:4000]),
            "env_names": process_result.get("env_names") or [],
            "cleanup_ok": process_result.get("cleanup_ok"),
            "process_group_isolated": process_result.get("process_group_isolated"),
            "termination_log": process_result.get("termination_log") or [],
        },
        "changed_file_manifest": manifest,
        "files_changed": paths,
        "file_count": len(paths),
        "diff_stat": redact_text(diff.get("diff_stat") or manifest.get("diff_stat") or ""),
        "diff_stat_hash": redact_hash(diff.get("diff_stat") or manifest.get("diff_stat") or ""),
        "manifest_fingerprint": manifest.get("manifest_fingerprint") or diff.get("manifest_fingerprint"),
        "patch_hash": manifest.get("manifest_fingerprint"),
        "constitution": {
            "version": run.get("constitution_version"),
            "hash": run.get("constitution_hash"),
            "lease_id": run.get("constitution_lease_id"),
            "lease_fingerprint": run.get("constitution_lease_fingerprint"),
            "result": constitution_result,
        },
        "verification": verification,
        "review": review,
        "cleanup_result": cleanup_result,
        "event_count": len(events or []),
        "event_hashes": [
            redact_hash(json.dumps(e, sort_keys=True, default=str)) for e in (events or [])[:50]
        ],
        "provider_self_claims_are_not_evidence": True,
        "notes": [
            "Provider narrative is not accepted as proof of completion.",
            "Deterministic verification and repository inspection are required.",
            "Evidence records are append-only.",
        ],
    }
    bundle["evidence_fingerprint"] = _fingerprint(bundle)
    return bundle


def _fingerprint(bundle: dict[str, Any]) -> str:
    material = {
        "evidence_id": bundle.get("evidence_id"),
        "run_id": bundle.get("run_id"),
        "packet_id": bundle.get("packet_id"),
        "repository": bundle.get("repository"),
        "approved_baseline_commit": bundle.get("approved_baseline_commit"),
        "post_run_head_sha": bundle.get("post_run_head_sha"),
        "branch": bundle.get("branch"),
        "provider_id": bundle.get("provider_id"),
        "files_changed": bundle.get("files_changed"),
        "manifest_fingerprint": bundle.get("manifest_fingerprint"),
        "process": {
            "exit_code": (bundle.get("process") or {}).get("exit_code"),
            "stdout_sha256": (bundle.get("process") or {}).get("stdout_sha256"),
            "stderr_sha256": (bundle.get("process") or {}).get("stderr_sha256"),
        },
        "constitution": bundle.get("constitution"),
        "verification_passed": (bundle.get("verification") or {}).get("passed"),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
