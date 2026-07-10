"""Constitution leases — immutable binding for the life of a run."""

from __future__ import annotations

import uuid
from typing import Any

from governance.constitution_hash import compute_constitution_hash, short_hash
from buildforme.storage import utc_now_iso


def issue_lease(
    constitution: dict[str, Any],
    *,
    run_id: str | None = None,
    provider_id: str | None = None,
    packet_id: str | None = None,
    actor: str = "system",
    lease_id: str | None = None,
) -> dict[str, Any]:
    """Create an immutable constitution lease for a run or binding context."""
    content_hash = compute_constitution_hash(constitution)
    version = str(constitution.get("version") or "0.0.0")
    now = utc_now_iso()
    lid = lease_id or f"lease-{uuid.uuid4().hex[:16]}"
    return {
        "lease_id": lid,
        "constitution_id": str(constitution.get("constitution_id") or "buildforme-ai-constitution"),
        "constitution_version": version,
        "constitution_hash": content_hash,
        "hash_short": short_hash(content_hash),
        "run_id": run_id,
        "provider_id": provider_id,
        "packet_id": packet_id,
        "issued_at": now,
        "issued_by": actor,
        "immutable": True,
        "status": "active",
        "law_count": len(constitution.get("laws") or []),
        "critical_law_ids": list(constitution.get("critical_law_ids") or []),
    }


def lease_matches_constitution(lease: dict[str, Any], constitution: dict[str, Any]) -> bool:
    """True if lease still matches *current* constitution (new runs only)."""
    if not lease:
        return False
    expected = compute_constitution_hash(constitution)
    return str(lease.get("constitution_hash") or "") == expected and str(
        lease.get("constitution_version") or ""
    ) == str(constitution.get("version") or "")


def validate_lease_integrity(lease: dict[str, Any]) -> list[str]:
    """Return problems if lease is incomplete or non-immutable."""
    problems: list[str] = []
    if not isinstance(lease, dict):
        return ["lease must be an object"]
    for field in ("lease_id", "constitution_version", "constitution_hash"):
        if not str(lease.get(field) or "").strip():
            problems.append(f"lease missing {field}")
    if lease.get("immutable") is False:
        problems.append("lease must be immutable during a run")
    if str(lease.get("status") or "active") not in {"active", "bound", "historical"}:
        problems.append(f"invalid lease status: {lease.get('status')!r}")
    return problems


def refresh_note_for_lease(lease: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Describe whether a refresh would issue a new lease (existing runs keep old)."""
    current_hash = compute_constitution_hash(current)
    same = str(lease.get("constitution_hash") or "") == current_hash
    return {
        "lease_id": lease.get("lease_id"),
        "run_bound_hash": lease.get("constitution_hash"),
        "current_hash": current_hash,
        "hash_match": same,
        "policy": "existing_run_keeps_original_lease",
        "action": "keep_lease" if same else "keep_lease_new_runs_get_new_version",
    }
