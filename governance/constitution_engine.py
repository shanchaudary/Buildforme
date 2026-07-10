"""Constitution engine facade — single authority for Stage 5.6 constitutional law.

LAW-013: This module is the single source of authority for constitution load,
hash, lease, inheritance, validation, refresh reminders, and export.
Stage 5.5 `buildforme.governance` remains the authority for run/preflight
validators only — not a competing constitution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from governance.constitution_audit import summarize_violations, violations_from_validation
from governance.constitution_hash import compute_constitution_hash, short_hash, verify_constitution_hash
from governance.constitution_inheritance import (
    acknowledge_provider,
    build_reminder,
    constitution_binding,
    inherit_for_approval,
    inherit_for_packet,
    inherit_for_provider,
    inherit_for_run,
    provider_requires_refresh,
)
from governance.constitution_lease import issue_lease, lease_matches_constitution, refresh_note_for_lease
from governance.constitution_validator import (
    validate_constitution_document,
    validate_output,
    validate_packet_binding,
    validate_provider_acknowledgement,
    validate_run_binding,
)
from buildforme.storage import utc_now_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION_JSON = Path(__file__).resolve().parent / "AI_CONSTITUTION.json"
CONSTITUTION_MD = Path(__file__).resolve().parent / "AI_CONSTITUTION.md"

_ENGINE: ConstitutionEngine | None = None


def constitution_path() -> Path:
    return CONSTITUTION_JSON


def load_constitution(*, path: Path | None = None) -> dict[str, Any]:
    target = path or CONSTITUTION_JSON
    raw = target.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("constitution must be a JSON object")
    data = dict(data)
    data["content_hash"] = compute_constitution_hash(data)
    data["loaded_at"] = utc_now_iso()
    return data


def get_engine(*, path: Path | None = None, force_reload: bool = False) -> ConstitutionEngine:
    global _ENGINE
    if _ENGINE is None or force_reload or (path is not None and path != _ENGINE.path):
        _ENGINE = ConstitutionEngine(path=path or CONSTITUTION_JSON)
    return _ENGINE


class ConstitutionEngine:
    """Operating-system constitutional layer for all agents and providers."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CONSTITUTION_JSON
        self._constitution = load_constitution(path=self.path)

    @property
    def constitution(self) -> dict[str, Any]:
        return dict(self._constitution)

    def reload(self) -> dict[str, Any]:
        self._constitution = load_constitution(path=self.path)
        return self.constitution

    def version(self) -> str:
        return str(self._constitution.get("version") or "")

    def content_hash(self) -> str:
        return compute_constitution_hash(self._constitution)

    def short_content_hash(self) -> str:
        return short_hash(self.content_hash())

    def laws(self) -> list[dict[str, Any]]:
        return [dict(law) for law in (self._constitution.get("laws") or []) if isinstance(law, dict)]

    def law_by_id(self, law_id: str) -> dict[str, Any] | None:
        for law in self.laws():
            if str(law.get("id")) == str(law_id):
                return law
        return None

    def status(self) -> dict[str, Any]:
        doc = validate_constitution_document(self._constitution)
        return {
            "constitution_id": self._constitution.get("constitution_id"),
            "version": self.version(),
            "hash": self.content_hash(),
            "hash_short": self.short_content_hash(),
            "stage": self._constitution.get("stage"),
            "status": self._constitution.get("status"),
            "law_count": len(self.laws()),
            "critical_law_ids": list(self._constitution.get("critical_law_ids") or []),
            "immutable_during_run": bool(self._constitution.get("immutable_during_run", True)),
            "provider_bypass_forbidden": bool(self._constitution.get("provider_bypass_forbidden", True)),
            "document_valid": doc["valid"],
            "document_problems": doc["problems"],
            "path": str(self.path),
            "loaded_at": self._constitution.get("loaded_at"),
        }

    def validate_document(self) -> dict[str, Any]:
        return validate_constitution_document(self._constitution)

    def binding(self, *, purpose: str = "general") -> dict[str, Any]:
        return constitution_binding(self._constitution, purpose=purpose)

    def reminder(self, *, phase: str = "run_start", lease: dict[str, Any] | None = None) -> dict[str, Any]:
        if lease:
            return build_reminder(
                {
                    "version": lease.get("constitution_version"),
                    "hash": lease.get("constitution_hash"),
                    "critical_law_ids": lease.get("critical_law_ids"),
                },
                laws=self.laws(),
                phase=phase,
            )
        return build_reminder(
            {
                "version": self.version(),
                "hash": self.content_hash(),
                "critical_law_ids": list(self._constitution.get("critical_law_ids") or []),
            },
            laws=self.laws(),
            phase=phase,
        )

    def attach_to_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        return inherit_for_packet(self._constitution, packet)

    def attach_to_run(
        self,
        run: dict[str, Any],
        *,
        lease: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        return inherit_for_run(self._constitution, run, lease=lease, actor=actor)

    def attach_to_approval(
        self,
        approval: dict[str, Any],
        *,
        run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return inherit_for_approval(self._constitution, approval, run=run)

    def attach_to_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        return inherit_for_provider(self._constitution, provider)

    def acknowledge_provider(self, provider: dict[str, Any], *, actor: str = "shan") -> dict[str, Any]:
        return acknowledge_provider(self._constitution, provider, actor=actor)

    def issue_run_lease(
        self,
        *,
        run_id: str | None = None,
        provider_id: str | None = None,
        packet_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        return issue_lease(
            self._constitution,
            run_id=run_id,
            provider_id=provider_id,
            packet_id=packet_id,
            actor=actor,
        )

    def validate_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        return validate_provider_acknowledgement(provider, self._constitution)

    def validate_run(self, run: dict[str, Any]) -> dict[str, Any]:
        return validate_run_binding(run, self._constitution)

    def validate_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        return validate_packet_binding(packet, self._constitution)

    def validate_output(
        self,
        output: dict[str, Any] | str,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return validate_output(output, constitution=self._constitution, context=context)

    def record_validation_violations(
        self,
        store: Any,
        validation: dict[str, Any],
        *,
        run_id: str | None = None,
        packet_id: str | None = None,
        provider_id: str | None = None,
        lease_id: str | None = None,
        actor: str = "system",
    ) -> list[dict[str, Any]]:
        events = violations_from_validation(
            validation,
            run_id=run_id,
            packet_id=packet_id,
            provider_id=provider_id,
            lease_id=lease_id,
            actor=actor,
        )
        saved: list[dict[str, Any]] = []
        for event in events:
            saved.append(store.append_constitution_violation(event))
        return saved

    def provider_needs_refresh(self, provider: dict[str, Any]) -> bool:
        return provider_requires_refresh(provider, self._constitution)

    def refresh_provider(self, provider: dict[str, Any], *, actor: str = "shan") -> dict[str, Any]:
        """Re-deliver full constitution acknowledgement binding (not every prompt)."""
        return self.acknowledge_provider(provider, actor=actor)

    def lease_refresh_policy(self, lease: dict[str, Any]) -> dict[str, Any]:
        return refresh_note_for_lease(lease, self._constitution)

    def lease_matches_current(self, lease: dict[str, Any]) -> bool:
        return lease_matches_constitution(lease, self._constitution)

    def verify_hash(self, expected: str) -> bool:
        return verify_constitution_hash(self._constitution, expected)

    def export(self, *, format: str = "json") -> str:
        fmt = str(format or "json").lower()
        if fmt == "json":
            body = dict(self._constitution)
            body.pop("loaded_at", None)
            body["content_hash"] = self.content_hash()
            return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if fmt == "markdown":
            if CONSTITUTION_MD.exists():
                return CONSTITUTION_MD.read_text(encoding="utf-8")
            return self._render_markdown()
        if fmt == "reminder":
            return self.reminder(phase="export")["text"]
        raise ValueError("format must be json, markdown, or reminder")

    def dashboard_payload(
        self,
        store: Any,
    ) -> dict[str, Any]:
        """UI payload: version, hash, laws, violations, leases, provider acks, run compliance."""
        providers = [self.attach_to_provider(p) for p in store.list_providers()]
        acks = [
            {
                "provider_id": p.get("provider_id"),
                "display_name": p.get("display_name"),
                "constitution_supported": p.get("constitution_supported"),
                "constitution_acknowledged": p.get("constitution_acknowledged"),
                "constitution_version": p.get("constitution_version"),
                "constitution_hash": p.get("constitution_hash"),
                "constitution_last_refresh": p.get("constitution_last_refresh"),
                "needs_refresh": self.provider_needs_refresh(p),
            }
            for p in providers
        ]
        runs = store.list_runs()
        run_compliance = []
        for run in runs[:50]:
            binding = validate_run_binding(run, self._constitution)
            run_compliance.append(
                {
                    "run_id": run.get("id"),
                    "status": run.get("status"),
                    "constitution_version": run.get("constitution_version"),
                    "constitution_hash": run.get("constitution_hash"),
                    "lease_id": run.get("constitution_lease_id"),
                    "compliance": run.get("constitution_compliance"),
                    "binding_valid": binding["valid"],
                    "binding_problems": binding["problems"],
                }
            )
        violations = store.list_constitution_violations(limit=100)
        leases = store.list_constitution_leases(limit=50)
        return {
            "status": self.status(),
            "laws": self.laws(),
            "violations": violations,
            "violation_summary": summarize_violations(violations),
            "leases": leases,
            "provider_acknowledgements": acks,
            "run_compliance": run_compliance,
            "reminder_sample": self.reminder(phase="dashboard"),
        }

    def full_validation_suite(self, store: Any | None = None) -> dict[str, Any]:
        doc = self.validate_document()
        checks = [
            {
                "name": "document_structure",
                "ok": doc["valid"],
                "detail": "ok" if doc["valid"] else "; ".join(doc["problems"][:5]),
            },
            {
                "name": "hash_stable",
                "ok": self.verify_hash(self.content_hash()),
                "detail": self.short_content_hash(),
            },
            {
                "name": "law_count_20",
                "ok": len(self.laws()) >= 20,
                "detail": f"laws={len(self.laws())}",
            },
            {
                "name": "law_020_present",
                "ok": self.law_by_id("LAW-020") is not None,
                "detail": "Governance Cannot Be Bypassed",
            },
        ]
        if store is not None:
            for provider in store.list_providers():
                p = self.attach_to_provider(provider)
                # Acknowledgement optional until refresh; structure required.
                has_fields = all(
                    k in p
                    for k in (
                        "constitution_supported",
                        "constitution_acknowledged",
                        "constitution_version",
                        "constitution_hash",
                    )
                )
                checks.append(
                    {
                        "name": f"provider_fields_{p.get('provider_id')}",
                        "ok": has_fields and p.get("constitution_supported") is not False,
                        "detail": f"ack={p.get('constitution_acknowledged')}",
                    }
                )
        passed = all(c["ok"] for c in checks)
        return {
            "passed": passed,
            "version": self.version(),
            "hash": self.content_hash(),
            "law_count": len(self.laws()),
            "checks": checks,
            "document": doc,
        }

    def _render_markdown(self) -> str:
        lines = [
            f"# {self._constitution.get('title') or 'Buildforme AI Constitution'}",
            "",
            f"**Version:** {self.version()}  ",
            f"**Hash:** `{self.content_hash()}`  ",
            f"**Stage:** {self._constitution.get('stage')}  ",
            "",
            "Immutable engineering laws for every provider, packet, run, and review.",
            "",
        ]
        for law in self.laws():
            lines.extend(
                [
                    f"## {law.get('id')} — {law.get('name')}",
                    "",
                    str(law.get("description") or ""),
                    "",
                    f"- **Severity:** {law.get('severity')}",
                    f"- **Applies to:** {', '.join(law.get('applies_to') or [])}",
                    f"- **Violation response:** {law.get('violation_response')}",
                    "",
                ]
            )
        return "\n".join(lines)
