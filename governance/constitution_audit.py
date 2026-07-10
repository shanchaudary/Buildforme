"""Constitution violation audit trail (local JSON via LocalStore)."""

from __future__ import annotations

import uuid
from typing import Any

from buildforme.storage import utc_now_iso


def make_violation_event(
    *,
    law_id: str,
    name: str,
    severity: str,
    evidence: str,
    response: str,
    run_id: str | None = None,
    packet_id: str | None = None,
    provider_id: str | None = None,
    actor: str = "system",
    constitution_version: str | None = None,
    constitution_hash: str | None = None,
    lease_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"viol-{uuid.uuid4().hex[:12]}",
        "type": "constitution_violation",
        "law_id": law_id,
        "name": name,
        "severity": severity,
        "evidence": evidence,
        "response": response,
        "run_id": run_id,
        "packet_id": packet_id,
        "provider_id": provider_id,
        "actor": actor,
        "constitution_version": constitution_version,
        "constitution_hash": constitution_hash,
        "lease_id": lease_id,
        "metadata": metadata or {},
        "created_at": utc_now_iso(),
    }


def violations_from_validation(
    validation: dict[str, Any],
    *,
    run_id: str | None = None,
    packet_id: str | None = None,
    provider_id: str | None = None,
    actor: str = "system",
    lease_id: str | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in validation.get("violations") or []:
        events.append(
            make_violation_event(
                law_id=str(item.get("law_id") or "UNKNOWN"),
                name=str(item.get("name") or ""),
                severity=str(item.get("severity") or "high"),
                evidence=str(item.get("evidence") or ""),
                response=str(item.get("response") or "Reject"),
                run_id=run_id,
                packet_id=packet_id,
                provider_id=provider_id,
                actor=actor,
                constitution_version=str(validation.get("constitution_version") or ""),
                constitution_hash=str(validation.get("constitution_hash") or ""),
                lease_id=lease_id,
            )
        )
    return events


def summarize_violations(violations: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    by_law: dict[str, int] = {}
    for item in violations:
        sev = str(item.get("severity") or "unknown")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        lid = str(item.get("law_id") or "UNKNOWN")
        by_law[lid] = by_law.get(lid, 0) + 1
    return {
        "total": len(violations),
        "by_severity": by_severity,
        "by_law": by_law,
        "critical": by_severity.get("critical", 0),
    }
