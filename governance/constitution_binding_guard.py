"""Deterministic cross-record guards for constitutional bindings.

The Constitution engine remains the policy authority. These helpers compare
persisted execution records and fail closed when their inherited bindings no
longer describe the same run lease.
"""

from __future__ import annotations

from typing import Any


def validate_approval_binding(
    approval: dict[str, Any],
    run: dict[str, Any],
    *,
    expected_scope_fingerprint: str | None = None,
) -> list[str]:
    """Return problems when an approval is stale or bound to another lease."""
    problems: list[str] = []
    if not isinstance(approval, dict):
        return ["approval must be an object"]

    expected = {
        "run_id": run.get("id"),
        "packet_id": run.get("packet_id"),
        "constitution_version": run.get("constitution_version"),
        "constitution_hash": run.get("constitution_hash"),
        "constitution_lease_id": run.get("constitution_lease_id"),
        "constitution_lease_fingerprint": run.get("constitution_lease_fingerprint"),
    }
    for field, value in expected.items():
        if str(approval.get(field) or "") != str(value or ""):
            problems.append(f"approval {field} does not match run")

    if expected_scope_fingerprint is not None:
        actual_scope = str(
            approval.get("scope_fingerprint") or approval.get("scope") or ""
        )
        if actual_scope != str(expected_scope_fingerprint):
            problems.append("approval scope fingerprint does not match run")

    return problems
