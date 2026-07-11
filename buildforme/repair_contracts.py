"""Stage 7 Packet 7D immutable repair-packet authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from buildforme.storage import utc_now_iso

REPAIR_PACKET_SCHEMA = "buildforme.repair_packet.v1"


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint(material: dict[str, Any]) -> str:
    raw = json.dumps(
        {"schema": REPAIR_PACKET_SCHEMA, "material": _canonical(material)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _blocking_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [dict(item) for item in findings if isinstance(item, dict) and item.get("blocking") is True]
    return sorted(items, key=lambda item: str(item.get("finding_id") or ""))


def _report_fingerprints(reports: list[dict[str, Any]]) -> list[str]:
    return sorted(str(item.get("report_fingerprint") or "") for item in reports if item.get("report_fingerprint"))


def build_repair_packet_record(
    *,
    cycle: dict[str, Any],
    run: dict[str, Any],
    evidence: dict[str, Any],
    reports: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    repair_provider_id: str,
    actor: str,
    provider_ack: dict[str, Any],
    repair_packet_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if str(cycle.get("status") or "") != "repair_required":
        raise ValueError("repair packet requires finalized repair_required review cycle")
    aggregate = cycle.get("aggregate") if isinstance(cycle.get("aggregate"), dict) else {}
    if str(aggregate.get("status") or "") != "repair_required" or aggregate.get("quorum_met") is not True:
        raise ValueError("repair packet requires quorum-backed repair_required aggregate")
    if str(run.get("status") or "") != "needs_review":
        raise ValueError("repair packet requires source run status needs_review")
    if str(run.get("stage7_review_cycle_id") or "") != str(cycle.get("cycle_id") or ""):
        raise ValueError("source run is not bound to repair review cycle")
    if str(evidence.get("evidence_id") or "") != str(cycle.get("evidence_id") or ""):
        raise ValueError("repair packet source evidence id mismatch")
    if str(evidence.get("evidence_fingerprint") or "") != str(cycle.get("evidence_fingerprint") or ""):
        raise ValueError("repair packet source evidence fingerprint mismatch")

    provider_id = str(repair_provider_id or "").strip().lower()
    if not provider_id:
        raise ValueError("repair_provider_id required")
    reviewer_providers = sorted(str(item) for item in (aggregate.get("provider_ids") or []))
    if provider_id in reviewer_providers:
        raise ValueError("a source reviewer provider cannot author the governed repair")
    if provider_ack.get("constitution_acknowledged") is not True:
        raise ValueError("repair provider has not acknowledged the Constitution")
    if str(provider_ack.get("constitution_hash") or "") != str(cycle.get("constitution_hash") or ""):
        raise ValueError("repair provider Constitution acknowledgement mismatch")

    blocking = _blocking_findings(findings)
    if not blocking:
        raise ValueError("repair_required cycle has no persisted blocking findings")
    blocking_ids = [str(item.get("finding_id") or "") for item in blocking]
    aggregate_blocking_ids = sorted(str(item) for item in (aggregate.get("blocking_finding_ids") or []))
    if blocking_ids != aggregate_blocking_ids:
        raise ValueError("persisted blocking findings do not match aggregate")

    report_fps = _report_fingerprints(reports)
    aggregate_report_fps = sorted(str(item) for item in (aggregate.get("report_fingerprints") or []))
    if report_fps != aggregate_report_fps:
        raise ValueError("persisted review reports do not match aggregate")

    source_packet = run.get("packet") if isinstance(run.get("packet"), dict) else {}
    allowed_files = sorted(str(item) for item in (source_packet.get("allowed_files") or evidence.get("allowed_files") or []))
    forbidden_files = sorted(str(item) for item in (source_packet.get("forbidden_files") or evidence.get("forbidden_files") or []))
    acceptance = [
        f"Resolve blocking finding {item.get('finding_id')}: {str(item.get('summary') or '').strip()}"
        for item in blocking
    ]
    packet_id = repair_packet_id or f"rpair-{uuid.uuid4().hex[:18]}"
    timestamp = created_at or utc_now_iso()
    material = {
        "repair_packet_id": packet_id,
        "source_cycle_id": cycle.get("cycle_id"),
        "source_run_id": run.get("id"),
        "source_evidence_id": evidence.get("evidence_id"),
        "source_evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "source_scope_fingerprint": cycle.get("scope_fingerprint"),
        "source_constitution_hash": cycle.get("constitution_hash"),
        "source_constitution_lease_id": cycle.get("constitution_lease_id"),
        "source_aggregate_fingerprint": aggregate.get("aggregate_fingerprint"),
        "source_report_fingerprints": report_fps,
        "source_blocking_findings": blocking,
        "repair_provider_id": provider_id,
        "source_implementer_provider_id": cycle.get("implementer_provider_id"),
        "source_reviewer_provider_ids": reviewer_providers,
        "next_review_excluded_provider_id": provider_id,
        "repository": run.get("repository"),
        "repository_local_path": run.get("repository_local_path"),
        "source_worktree_path": evidence.get("worktree_path") or run.get("worktree_path"),
        "approved_baseline_commit": evidence.get("approved_baseline_commit") or run.get("baseline_commit"),
        "source_final_head_sha": evidence.get("final_head_sha"),
        "source_execution_branch": evidence.get("execution_branch"),
        "source_manifest_fingerprint": evidence.get("manifest_fingerprint"),
        "source_patch_fingerprint": evidence.get("patch_fingerprint"),
        "allowed_files": allowed_files,
        "forbidden_files": forbidden_files,
        "repair_acceptance_criteria": acceptance,
        "repair_scope_expansion_forbidden": True,
        "fresh_execution_evidence_required": True,
        "new_independent_review_cycle_required": True,
        "created_by": str(actor or "shan"),
    }
    record = {
        "schema": REPAIR_PACKET_SCHEMA,
        **material,
        "status": "packet_ready",
        "created_at": timestamp,
        "immutable": True,
    }
    record["repair_fingerprint"] = _fingerprint(material)
    return record


def validate_repair_packet_for_storage(
    packet: dict[str, Any],
    *,
    cycle: dict[str, Any],
    run: dict[str, Any],
    evidence: dict[str, Any],
    reports: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    provider_ack: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    if not isinstance(packet, dict):
        return ["repair packet must be an object"]
    for field in (
        "repair_packet_id",
        "source_cycle_id",
        "source_run_id",
        "source_evidence_id",
        "source_evidence_fingerprint",
        "source_scope_fingerprint",
        "source_constitution_hash",
        "source_constitution_lease_id",
        "source_aggregate_fingerprint",
        "source_report_fingerprints",
        "source_blocking_findings",
        "repair_provider_id",
        "allowed_files",
        "forbidden_files",
        "repair_acceptance_criteria",
        "repair_fingerprint",
    ):
        if packet.get(field) in (None, ""):
            problems.append(f"repair packet missing {field}")
    if packet.get("immutable") is not True:
        problems.append("repair packet immutable must be exactly true")
    if str(packet.get("status") or "") != "packet_ready":
        problems.append("repair packet status must be packet_ready")
    if packet.get("repair_scope_expansion_forbidden") is not True:
        problems.append("repair packet must forbid scope expansion")
    if packet.get("fresh_execution_evidence_required") is not True:
        problems.append("repair packet must require fresh execution evidence")
    if packet.get("new_independent_review_cycle_required") is not True:
        problems.append("repair packet must require a new independent review cycle")
    try:
        expected = build_repair_packet_record(
            cycle=cycle,
            run=run,
            evidence=evidence,
            reports=reports,
            findings=findings,
            repair_provider_id=str(packet.get("repair_provider_id") or ""),
            actor=str(packet.get("created_by") or "shan"),
            provider_ack=provider_ack,
            repair_packet_id=str(packet.get("repair_packet_id") or ""),
            created_at=str(packet.get("created_at") or ""),
        )
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    for field, value in expected.items():
        if _canonical(packet.get(field)) != _canonical(value):
            problems.append(f"repair packet {field} mismatch")
    return problems
