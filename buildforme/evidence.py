"""Append-oriented evidence collection — provider claims alone are not evidence.

Fingerprint integrity (Stage 6):
- One canonical function: compute_evidence_fingerprint()
- Fingerprint is calculated only after all material fields are present
- Storage re-validates; never silently repairs a mismatched fingerprint
- Manifest fingerprint is never used as the patch fingerprint
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from buildforme.redaction import redact_hash, redact_mapping, redact_process_result, redact_text
from buildforme.storage import utc_now_iso

EVIDENCE_SCHEMA = "buildforme.evidence.v1"
FINGERPRINT_SCHEMA = "buildforme.evidence_fingerprint.v3"
EVIDENCE_KIND_EXECUTION = "execution"

# Material fields required on execution evidence before storage accepts it.
EXECUTION_REQUIRED_TOP_LEVEL = (
    "schema",
    "evidence_id",
    "evidence_kind",
    "run_id",
    "repository",
    "provider_id",
    "transport",
    "approved_baseline_commit",
    "final_head_sha",
    "execution_branch",
    "manifest_fingerprint",
    "patch_fingerprint",
    "files_changed",
    "process",
    "constitution",
    "verification",
    "evidence_fingerprint",
)


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
    # Final execution identity — must be supplied before fingerprinting
    patch_fingerprint: str | None = None,
    final_head_sha: str | None = None,
    execution_branch: str | None = None,
    approved_baseline_sha: str | None = None,
    manifest_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build complete execution evidence and fingerprint it once at the end.

    Callers must pass final HEAD, execution branch, and patch fingerprint here.
    Do not attach material fields after this function returns.
    """
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

    baseline = (
        approved_baseline_sha
        or run.get("baseline_commit")
        or worktree.get("baseline_commit")
    )
    head = final_head_sha or worktree.get("head_commit") or run.get("head_commit") or run.get("final_head_sha")
    branch = (
        execution_branch
        or run.get("execution_branch")
        or worktree.get("branch")
        or run.get("target_branch")
    )
    man_fp = (
        manifest_fingerprint
        or manifest.get("manifest_fingerprint")
        or diff.get("manifest_fingerprint")
    )
    # Patch fingerprint is distinct from manifest fingerprint — never substitute.
    patch_fp = patch_fingerprint
    if patch_fp is None:
        patch_fp = diff.get("patch_fingerprint")
    if patch_fp is None and isinstance(manifest.get("patch_fingerprint"), str):
        patch_fp = manifest.get("patch_fingerprint")
    # Explicitly do NOT fall back to man_fp

    evidence_id = f"ev-{uuid.uuid4().hex[:16]}"
    process_block = {
        "argv": process_result.get("argv"),
        "exit_code": process_result.get("exit_code"),
        "pid": process_result.get("pid"),
        "timed_out": process_result.get("timed_out"),
        "cancelled": process_result.get("cancelled"),
        "duration_seconds": process_result.get("duration_seconds"),
        "truncated_stdout": process_result.get("truncated_stdout"),
        "truncated_stderr": process_result.get("truncated_stderr"),
        "stdout_sha256": process_result.get("stdout_sha256")
        or redact_hash(process_result.get("stdout") or ""),
        "stderr_sha256": process_result.get("stderr_sha256")
        or redact_hash(process_result.get("stderr") or ""),
        "stdout_preview": redact_text((process_result.get("stdout") or "")[:4000]),
        "stderr_preview": redact_text((process_result.get("stderr") or "")[:4000]),
        "env_names": process_result.get("env_names") or [],
        "cleanup_ok": process_result.get("cleanup_ok"),
        "process_group_isolated": process_result.get("process_group_isolated"),
        "termination_log": process_result.get("termination_log") or [],
    }

    bundle: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "evidence_kind": EVIDENCE_KIND_EXECUTION,
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
        "approved_baseline_commit": baseline,
        "approved_baseline_sha": baseline,
        "baseline_commit": baseline,
        "final_head_sha": head,
        "post_run_head_sha": head,
        "head_commit": head,
        "execution_branch": branch,
        "branch": branch,
        "worktree_path": worktree.get("worktree_path") or run.get("worktree_path"),
        "worktree_identity": {
            "path": worktree.get("worktree_path"),
            "baseline": worktree.get("baseline_commit") or baseline,
            "fresh_branch": worktree.get("fresh_branch"),
            "head_commit": head,
            "branch": branch,
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
        "process": process_block,
        "changed_file_manifest": manifest,
        "files_changed": paths,
        "file_count": len(paths),
        "diff_stat": redact_text(diff.get("diff_stat") or manifest.get("diff_stat") or ""),
        "diff_stat_hash": redact_hash(diff.get("diff_stat") or manifest.get("diff_stat") or ""),
        "manifest_fingerprint": man_fp,
        # Distinct from manifest — never alias manifest into patch_hash
        "patch_fingerprint": patch_fp,
        "patch_hash": patch_fp,
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
            "Fingerprint binds final HEAD, branch, patch, verification, and constitution.",
        ],
    }
    # Single authority: fingerprint only after all material fields are attached.
    bundle["evidence_fingerprint"] = compute_evidence_fingerprint(bundle)
    return bundle


def compute_evidence_fingerprint(bundle: dict[str, Any]) -> str:
    """Canonical evidence fingerprint — sole authority for construction and storage.

    Does not include mutable storage metadata (saved_at, sequence, collected_at).
    """
    process = bundle.get("process") if isinstance(bundle.get("process"), dict) else {}
    constitution = (
        bundle.get("constitution") if isinstance(bundle.get("constitution"), dict) else {}
    )
    verification = (
        bundle.get("verification") if isinstance(bundle.get("verification"), dict) else {}
    )
    # Canonical verification material (stable, no wall-clock fields)
    checks_material = []
    for check in verification.get("checks") or []:
        if not isinstance(check, dict):
            continue
        checks_material.append(
            {
                "name": check.get("name"),
                "status": check.get("status"),
                "detail": check.get("detail") or check.get("message") or check.get("summary"),
            }
        )
    checks_material.sort(key=lambda c: str(c.get("name") or ""))
    verification_material = {
        "passed": verification.get("passed"),
        "blocking_reasons": list(verification.get("blocking_reasons") or []),
        "checks": checks_material,
    }
    # Constitution validation result (material subset)
    const_result = constitution.get("result")
    if isinstance(const_result, dict):
        const_result_material = {
            "passed": const_result.get("passed"),
            "problems": list(const_result.get("problems") or const_result.get("violations") or []),
        }
    else:
        const_result_material = const_result

    material = {
        "schema": FINGERPRINT_SCHEMA,
        "evidence_schema": bundle.get("schema"),
        "evidence_kind": bundle.get("evidence_kind") or EVIDENCE_KIND_EXECUTION,
        "evidence_id": bundle.get("evidence_id") or bundle.get("id"),
        "run_id": bundle.get("run_id"),
        "task_id": bundle.get("task_id"),
        "packet_id": bundle.get("packet_id"),
        "project_id": bundle.get("project_id"),
        "repository": bundle.get("repository"),
        "approved_baseline_commit": bundle.get("approved_baseline_commit")
        or bundle.get("approved_baseline_sha")
        or bundle.get("baseline_commit"),
        "final_head_sha": bundle.get("final_head_sha") or bundle.get("post_run_head_sha"),
        "execution_branch": bundle.get("execution_branch") or bundle.get("branch"),
        "provider_id": bundle.get("provider_id"),
        "provider_version": bundle.get("provider_version"),
        "transport": bundle.get("transport"),
        "files_changed": list(bundle.get("files_changed") or []),
        "manifest_fingerprint": bundle.get("manifest_fingerprint"),
        "patch_fingerprint": bundle.get("patch_fingerprint"),
        "process": {
            "exit_code": process.get("exit_code"),
            "pid": process.get("pid"),
            "timed_out": process.get("timed_out"),
            "cancelled": process.get("cancelled"),
            "cleanup_ok": process.get("cleanup_ok"),
            "stdout_sha256": process.get("stdout_sha256"),
            "stderr_sha256": process.get("stderr_sha256"),
            "argv": process.get("argv"),
        },
        "constitution": {
            "version": constitution.get("version"),
            "hash": constitution.get("hash"),
            "lease_id": constitution.get("lease_id"),
            "lease_fingerprint": constitution.get("lease_fingerprint"),
            "result": const_result_material,
        },
        "verification": verification_material,
        "event_hashes": list(bundle.get("event_hashes") or []),
        "event_count": bundle.get("event_count"),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_evidence_for_storage(evidence: dict[str, Any]) -> list[str]:
    """Return problems if evidence must not be persisted. Empty list = OK.

    Storage must call this and reject on any problem — never auto-repair fingerprints.
    """
    problems: list[str] = []
    if not isinstance(evidence, dict):
        return ["evidence must be an object"]

    kind = str(evidence.get("evidence_kind") or "")
    # Execution evidence (default for live supervised path)
    is_execution = kind == EVIDENCE_KIND_EXECUTION or (
        not kind and isinstance(evidence.get("process"), dict) and evidence.get("run_id")
    )

    provided = evidence.get("evidence_fingerprint")
    if not provided or not isinstance(provided, str) or len(provided) < 32:
        problems.append("evidence_fingerprint missing or invalid")
    else:
        expected = compute_evidence_fingerprint(evidence)
        if provided != expected:
            problems.append(
                "evidence_fingerprint mismatch: caller fingerprint does not match "
                "canonical material (refusing silent repair)"
            )

    if is_execution:
        for field in EXECUTION_REQUIRED_TOP_LEVEL:
            if field == "evidence_fingerprint":
                continue
            if field not in evidence or evidence.get(field) is None:
                # files_changed may be empty list; process/verification may be empty dict
                if field in {"files_changed"} and isinstance(evidence.get(field), list):
                    continue
                if field in {"process", "constitution", "verification"} and isinstance(
                    evidence.get(field), dict
                ):
                    continue
                problems.append(f"execution evidence missing required field: {field}")

        process = evidence.get("process") if isinstance(evidence.get("process"), dict) else {}
        for pfield in (
            "exit_code",
            "stdout_sha256",
            "stderr_sha256",
            "timed_out",
            "cancelled",
            "cleanup_ok",
        ):
            if pfield not in process:
                problems.append(f"execution evidence process missing: {pfield}")

        # Patch fingerprint must not equal manifest fingerprint when both set
        man = evidence.get("manifest_fingerprint")
        patch = evidence.get("patch_fingerprint")
        if man and patch and man == patch:
            problems.append(
                "patch_fingerprint must not equal manifest_fingerprint "
                "(distinct patch evidence required)"
            )

        # Live execution evidence requires non-empty final identity strings
        for field in ("final_head_sha", "execution_branch", "approved_baseline_commit"):
            val = evidence.get(field) or (
                evidence.get("approved_baseline_sha") if field == "approved_baseline_commit" else None
            )
            if field == "approved_baseline_commit":
                val = evidence.get("approved_baseline_commit") or evidence.get(
                    "approved_baseline_sha"
                )
            if not val or not str(val).strip():
                problems.append(f"execution evidence requires non-empty {field}")

        if not evidence.get("patch_fingerprint"):
            problems.append("execution evidence requires patch_fingerprint")
        if not evidence.get("manifest_fingerprint"):
            problems.append("execution evidence requires manifest_fingerprint")

    return problems
