"""Constitution leases — immutable binding for the life of a run.

This module contains deterministic lease primitives used by the Constitution
engine. It does not define constitutional policy; it makes a policy binding
verifiable and append-only through normal platform execution paths.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from governance.constitution_hash import compute_constitution_hash, short_hash
from buildforme.storage import utc_now_iso

LEASE_FINGERPRINT_FIELDS = (
    "lease_id",
    "constitution_id",
    "constitution_version",
    "constitution_hash",
    "run_id",
    "provider_id",
    "packet_id",
    "issued_at",
    "issued_by",
    "immutable",
    "status",
    "law_count",
    "critical_law_ids",
)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=lambda item: str(item))}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def lease_fingerprint_payload(lease: dict[str, Any]) -> dict[str, Any]:
    """Return only immutable lease fields in deterministic form."""
    return {field: _canonical(lease.get(field)) for field in LEASE_FINGERPRINT_FIELDS}


def compute_lease_fingerprint(lease: dict[str, Any]) -> str:
    raw = json.dumps(
        lease_fingerprint_payload(lease),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def seal_lease(lease: dict[str, Any]) -> dict[str, Any]:
    """Return a copy carrying its deterministic immutable-field fingerprint."""
    sealed = dict(lease)
    sealed["lease_fingerprint"] = compute_lease_fingerprint(sealed)
    return sealed


def issue_lease(
    constitution: dict[str, Any],
    *,
    run_id: str | None = None,
    provider_id: str | None = None,
    packet_id: str | None = None,
    actor: str = "system",
    lease_id: str | None = None,
) -> dict[str, Any]:
    """Create a fingerprinted immutable constitution lease."""
    content_hash = compute_constitution_hash(constitution)
    version = str(constitution.get("version") or "0.0.0")
    now = utc_now_iso()
    lid = lease_id or f"lease-{uuid.uuid4().hex[:16]}"
    return seal_lease(
        {
            "lease_id": lid,
            "constitution_id": str(
                constitution.get("constitution_id") or "buildforme-ai-constitution"
            ),
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
    )


def lease_matches_constitution(lease: dict[str, Any], constitution: dict[str, Any]) -> bool:
    """True if lease still matches *current* constitution (new runs only)."""
    if not lease:
        return False
    expected = compute_constitution_hash(constitution)
    return str(lease.get("constitution_hash") or "") == expected and str(
        lease.get("constitution_version") or ""
    ) == str(constitution.get("version") or "")


def validate_lease_integrity(
    lease: dict[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_provider_id: str | None = None,
    expected_packet_id: str | None = None,
) -> list[str]:
    """Return deterministic integrity and identity problems for a lease."""
    problems: list[str] = []
    if not isinstance(lease, dict):
        return ["lease must be an object"]

    for field in (
        "lease_id",
        "constitution_id",
        "constitution_version",
        "constitution_hash",
        "issued_at",
        "issued_by",
        "status",
        "lease_fingerprint",
    ):
        if not str(lease.get(field) or "").strip():
            problems.append(f"lease missing {field}")

    if lease.get("immutable") is not True:
        problems.append("lease immutable must be exactly true")
    if str(lease.get("status") or "") not in {"active", "bound", "historical"}:
        problems.append(f"invalid lease status: {lease.get('status')!r}")
    if not isinstance(lease.get("critical_law_ids"), list):
        problems.append("lease critical_law_ids must be a list")
    try:
        if int(lease.get("law_count")) < 1:
            problems.append("lease law_count must be positive")
    except (TypeError, ValueError):
        problems.append("lease law_count must be an integer")

    expected_fingerprint = compute_lease_fingerprint(lease)
    if str(lease.get("lease_fingerprint") or "") != expected_fingerprint:
        problems.append("lease fingerprint mismatch")

    identities = (
        ("run_id", expected_run_id),
        ("provider_id", expected_provider_id),
        ("packet_id", expected_packet_id),
    )
    for field, expected in identities:
        if expected is not None and str(lease.get(field) or "") != str(expected):
            problems.append(f"lease {field} does not match expected identity")

    return problems


def lease_records_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare immutable lease records, ignoring storage-only metadata."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        lease_fingerprint_payload(left) == lease_fingerprint_payload(right)
        and str(left.get("lease_fingerprint") or "")
        == str(right.get("lease_fingerprint") or "")
        == compute_lease_fingerprint(left)
        == compute_lease_fingerprint(right)
    )


def persist_lease_append_only(store: Any, lease: dict[str, Any]) -> dict[str, Any]:
    """Persist once; an existing lease id may only be replayed identically."""
    problems = validate_lease_integrity(lease)
    if problems:
        raise ValueError("invalid constitution lease: " + "; ".join(problems))

    lease_id = str(lease["lease_id"])
    try:
        existing = store.get_constitution_lease(lease_id)
    except KeyError:
        saved = store.save_constitution_lease(lease)
        if not lease_records_equal(saved, lease):
            raise ValueError("stored constitution lease differs from issued lease")
        return saved

    if not lease_records_equal(existing, lease):
        raise ValueError("constitution lease id collision or mutation attempt")
    return existing


def validate_run_lease_against_store(run: dict[str, Any], store: Any) -> dict[str, Any]:
    """Validate run binding against the canonical persisted lease record."""
    problems: list[str] = []
    embedded = run.get("constitution_lease") if isinstance(run.get("constitution_lease"), dict) else None
    run_id = str(run.get("id") or "")
    provider_id = str(run.get("provider_id") or "")
    packet_id = str(run.get("packet_id") or "")

    if embedded is None:
        problems.append("run missing constitution_lease")
        return {"valid": False, "problems": problems, "run_id": run.get("id")}

    problems.extend(
        validate_lease_integrity(
            embedded,
            expected_run_id=run_id,
            expected_provider_id=provider_id,
            expected_packet_id=packet_id,
        )
    )

    lease_id = str(run.get("constitution_lease_id") or "")
    if lease_id != str(embedded.get("lease_id") or ""):
        problems.append("run constitution_lease_id does not match embedded lease")

    try:
        stored = store.get_constitution_lease(lease_id)
    except (KeyError, ValueError):
        stored = None
        problems.append("canonical constitution lease not found")

    if stored is not None:
        problems.extend(
            validate_lease_integrity(
                stored,
                expected_run_id=run_id,
                expected_provider_id=provider_id,
                expected_packet_id=packet_id,
            )
        )
        if not lease_records_equal(embedded, stored):
            problems.append("embedded constitution lease differs from canonical stored lease")

    if str(run.get("constitution_version") or "") != str(embedded.get("constitution_version") or ""):
        problems.append("run constitution_version does not match lease")
    if str(run.get("constitution_hash") or "") != str(embedded.get("constitution_hash") or ""):
        problems.append("run constitution_hash does not match lease")
    if str(run.get("constitution_lease_fingerprint") or "") != str(
        embedded.get("lease_fingerprint") or ""
    ):
        problems.append("run constitution_lease_fingerprint does not match lease")

    return {
        "valid": not problems,
        "problems": problems,
        "run_id": run.get("id"),
        "lease_id": lease_id,
    }


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
