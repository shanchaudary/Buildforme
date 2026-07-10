"""Automatic constitution inheritance for packets, runs, providers, reviews."""

from __future__ import annotations

from typing import Any

from governance.constitution_hash import compute_constitution_hash, short_hash
from governance.constitution_lease import issue_lease, validate_lease_integrity
from buildforme.storage import utc_now_iso

# Compact reminder — never re-send full constitution every prompt (Stage 5.6 refresh design).
CRITICAL_REMINDER_LAWS = (
    "LAW-001",
    "LAW-002",
    "LAW-004",
    "LAW-005",
    "LAW-009",
    "LAW-012",
    "LAW-020",
)


def inherit_for_packet(constitution: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Attach constitution binding to a packet (version, hash, critical laws)."""
    out = dict(packet)
    binding = constitution_binding(constitution, purpose="packet")
    out["constitution"] = binding
    out["constitution_version"] = binding["version"]
    out["constitution_hash"] = binding["hash"]
    # Do not embed full laws in packet body — reminder only.
    out["constitution_critical_laws"] = binding["critical_laws"]
    out["constitution_reminder"] = binding["reminder"]
    return out


def inherit_for_run(
    constitution: dict[str, Any],
    run: dict[str, Any],
    *,
    lease: dict[str, Any] | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    """Bind run to constitution lease. Existing valid lease is preserved."""
    out = dict(run)
    if lease and not validate_lease_integrity(lease):
        bound_lease = dict(lease)
    elif isinstance(out.get("constitution_lease"), dict) and not validate_lease_integrity(
        out["constitution_lease"]
    ):
        bound_lease = dict(out["constitution_lease"])
    else:
        bound_lease = issue_lease(
            constitution,
            run_id=str(out.get("id") or ""),
            provider_id=str(out.get("provider_id") or "") or None,
            packet_id=str(out.get("packet_id") or "") or None,
            actor=actor,
        )
    out["constitution_lease"] = bound_lease
    out["constitution_lease_id"] = bound_lease.get("lease_id")
    out["constitution_lease_fingerprint"] = bound_lease.get("lease_fingerprint")
    out["constitution_version"] = bound_lease.get("constitution_version")
    out["constitution_hash"] = bound_lease.get("constitution_hash")
    out["constitution_compliance"] = out.get("constitution_compliance") or {
        "status": "bound",
        "violations": [],
        "validated_at": None,
    }
    out["constitution_reminder"] = build_reminder(
        {
            "version": bound_lease.get("constitution_version"),
            "hash": bound_lease.get("constitution_hash"),
            "critical_law_ids": bound_lease.get("critical_law_ids") or list(CRITICAL_REMINDER_LAWS),
        },
        laws=constitution.get("laws") or [],
        phase="run_start",
    )
    return out


def inherit_for_approval(
    constitution: dict[str, Any],
    approval: dict[str, Any],
    *,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind approval to constitution hash and exact run lease."""
    out = dict(approval)
    content_hash = str(
        (run or {}).get("constitution_hash") or compute_constitution_hash(constitution)
    )
    version = str((run or {}).get("constitution_version") or constitution.get("version") or "")
    out["constitution_version"] = version
    out["constitution_hash"] = content_hash
    if run and run.get("constitution_lease_id"):
        out["constitution_lease_id"] = run.get("constitution_lease_id")
        out["constitution_lease_fingerprint"] = run.get("constitution_lease_fingerprint")
    return out


def inherit_for_provider(constitution: dict[str, Any], provider: dict[str, Any]) -> dict[str, Any]:
    """Ensure provider profile carries constitution support fields (not yet acknowledged)."""
    out = dict(provider)
    content_hash = compute_constitution_hash(constitution)
    version = str(constitution.get("version") or "1.0.0")
    out.setdefault("constitution_supported", True)
    # Acknowledgement is explicit — do not auto-set true.
    if "constitution_acknowledged" not in out:
        out["constitution_acknowledged"] = False
    out["constitution_version"] = out.get("constitution_version") or version
    out["constitution_hash"] = out.get("constitution_hash") or content_hash
    if out.get("constitution_last_refresh") is None:
        out["constitution_last_refresh"] = None
    if out.get("constitution_acknowledged_at") is None:
        out["constitution_acknowledged_at"] = None
    return out


def acknowledge_provider(
    constitution: dict[str, Any],
    provider: dict[str, Any],
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    """Record that a provider profile received and acknowledges the full constitution once."""
    out = inherit_for_provider(constitution, provider)
    content_hash = compute_constitution_hash(constitution)
    version = str(constitution.get("version") or "1.0.0")
    now = utc_now_iso()
    out["constitution_supported"] = True
    out["constitution_acknowledged"] = True
    out["constitution_version"] = version
    out["constitution_hash"] = content_hash
    out["constitution_acknowledged_at"] = now
    out["constitution_last_refresh"] = now
    out["constitution_ack_actor"] = actor
    return out


def provider_requires_refresh(provider: dict[str, Any], constitution: dict[str, Any]) -> bool:
    """True when provider ack is missing or bound to a different constitution hash."""
    if not provider.get("constitution_acknowledged"):
        return True
    current = compute_constitution_hash(constitution)
    return str(provider.get("constitution_hash") or "") != current


def constitution_binding(constitution: dict[str, Any], *, purpose: str = "general") -> dict[str, Any]:
    content_hash = compute_constitution_hash(constitution)
    critical_ids = list(constitution.get("critical_law_ids") or list(CRITICAL_REMINDER_LAWS))
    laws = constitution.get("laws") or []
    critical = [
        {
            "id": law.get("id"),
            "name": law.get("name"),
            "severity": law.get("severity"),
            "violation_response": law.get("violation_response"),
        }
        for law in laws
        if isinstance(law, dict) and str(law.get("id")) in set(critical_ids)
    ]
    return {
        "purpose": purpose,
        "constitution_id": constitution.get("constitution_id"),
        "version": constitution.get("version"),
        "hash": content_hash,
        "hash_short": short_hash(content_hash),
        "law_count": len(laws),
        "critical_laws": critical,
        "reminder": build_reminder(
            {"version": constitution.get("version"), "hash": content_hash, "critical_law_ids": critical_ids},
            laws=laws,
            phase=purpose,
        ),
        "full_text_policy": "send_once_per_provider_ack_then_reminder_only",
        "bypass_forbidden": True,
    }


def build_reminder(
    binding: dict[str, Any],
    *,
    laws: list[Any],
    phase: str = "run_start",
) -> dict[str, Any]:
    """Small immutable reminder — not the full constitution."""
    critical_ids = list(binding.get("critical_law_ids") or list(CRITICAL_REMINDER_LAWS))
    by_id = {str(law.get("id")): law for law in laws if isinstance(law, dict)}
    lines = [
        f"CONSTITUTION REMINDER [{phase}]",
        f"Version: {binding.get('version')}",
        f"Hash: {binding.get('hash')}",
        "Critical laws (full text already acknowledged by provider):",
    ]
    for lid in critical_ids:
        law = by_id.get(str(lid)) or {}
        lines.append(f"- {lid} {law.get('name') or ''}: {law.get('description') or 'see Constitution'}")
    lines.append("LAW-020: Governance cannot be bypassed, disabled, replaced, or ignored.")
    return {
        "phase": phase,
        "version": binding.get("version"),
        "hash": binding.get("hash"),
        "critical_law_ids": critical_ids,
        "text": "\n".join(lines),
        "full_constitution_resent": False,
    }


def markdown_constitution_block(binding: dict[str, Any]) -> list[str]:
    """Markdown section for packets — hash + critical laws only."""
    lines = [
        "## Constitution (inherited)",
        "",
        f"- **Version:** `{binding.get('version') or ''}`",
        f"- **Hash:** `{binding.get('hash') or ''}`",
        f"- **Laws:** `{binding.get('law_count') or 0}`",
        "- **Bypass:** forbidden",
        "- **Full text:** already on file for acknowledged providers; this packet carries the binding + critical reminder only.",
        "",
        "### Critical laws",
        "",
    ]
    for law in binding.get("critical_laws") or []:
        lines.append(f"- **{law.get('id')}** {law.get('name')}: severity `{law.get('severity')}`")
    lines.append("")
    return lines
