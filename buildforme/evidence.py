"""Evidence collection for supervised runs — provider claims alone are not evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

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
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    packet = packet or {}
    process_result = process_result or {}
    worktree = worktree or {}
    diff = diff or {}
    provider_health = provider_health or {}

    bundle = {
        "schema": "buildforme.evidence.v1",
        "collected_at": utc_now_iso(),
        "task_id": run.get("task_id"),
        "packet_id": run.get("packet_id") or packet.get("id"),
        "run_id": run.get("id"),
        "project_id": run.get("project_id"),
        "repository": run.get("repository"),
        "baseline_commit": worktree.get("baseline_commit") or run.get("baseline_commit"),
        "head_commit": worktree.get("head_commit") or run.get("head_commit"),
        "branch": worktree.get("branch") or run.get("target_branch"),
        "worktree_path": worktree.get("worktree_path") or run.get("worktree_path"),
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
            # Store bounded hashes of streams, not full secret dumps in summary
            "stdout_sha256": _sha(process_result.get("stdout") or ""),
            "stderr_sha256": _sha(process_result.get("stderr") or ""),
            "stdout_preview": _preview(process_result.get("stdout") or "", 4000),
            "stderr_preview": _preview(process_result.get("stderr") or "", 4000),
        },
        "files_changed": list(diff.get("files_changed") or []),
        "diff_stat": diff.get("diff_stat") or "",
        "file_count": int(diff.get("file_count") or 0),
        "constitution": {
            "version": run.get("constitution_version"),
            "hash": run.get("constitution_hash"),
            "lease_id": run.get("constitution_lease_id"),
            "lease_fingerprint": run.get("constitution_lease_fingerprint"),
        },
        "verification": verification,
        "event_count": len(events or []),
        "provider_self_claims_are_not_evidence": True,
        "notes": [
            "Provider narrative is not accepted as proof of completion.",
            "Deterministic verification and repository inspection are required.",
        ],
    }
    bundle["evidence_fingerprint"] = _fingerprint(bundle)
    return bundle


def _preview(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated preview]\n"


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()


def _fingerprint(bundle: dict[str, Any]) -> str:
    material = {
        k: bundle.get(k)
        for k in (
            "run_id",
            "packet_id",
            "repository",
            "baseline_commit",
            "head_commit",
            "branch",
            "provider_id",
            "files_changed",
            "process",
            "constitution",
        )
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
