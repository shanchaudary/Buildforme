"""Declarative provider capability registry (Stage 5).

All providers are dry-run only. No credentials. No network calls.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from buildforme.storage import utc_now_iso

DEFAULT_PROVIDERS: list[dict[str, Any]] = [
    {
        "provider_id": "codex",
        "display_name": "Codex",
        "enabled": True,
        "mode": "dry_run",
        "live_execution_available": False,
        "capabilities": [
            "read_repository",
            "edit_repository",
            "run_tests",
            "produce_patch",
            "open_pr",
        ],
        "prohibited_capabilities": ["merge", "deploy", "production_write"],
        "supported_risk_levels": ["GREEN", "YELLOW"],
        "supported_operating_modes": [
            "READ_ONLY_AUDIT",
            "PLAN_ONLY",
            "DOCUMENTATION_ONLY",
            "IMPLEMENTATION",
            "REVIEW",
        ],
        "max_concurrent_runs": 1,
        "default_timeout_minutes": 30,
        "max_timeout_minutes": 120,
        "supports_cancel": True,
        "supports_resume": False,
        "credentials_required": True,
        "credentials_configured": False,
    },
    {
        "provider_id": "claude",
        "display_name": "Claude",
        "enabled": True,
        "mode": "dry_run",
        "live_execution_available": False,
        "capabilities": [
            "read_repository",
            "edit_repository",
            "run_tests",
            "produce_patch",
            "open_pr",
        ],
        "prohibited_capabilities": ["merge", "deploy", "production_write"],
        "supported_risk_levels": ["GREEN", "YELLOW"],
        "supported_operating_modes": [
            "READ_ONLY_AUDIT",
            "PLAN_ONLY",
            "DOCUMENTATION_ONLY",
            "IMPLEMENTATION",
            "REVIEW",
        ],
        "max_concurrent_runs": 1,
        "default_timeout_minutes": 30,
        "max_timeout_minutes": 120,
        "supports_cancel": True,
        "supports_resume": False,
        "credentials_required": True,
        "credentials_configured": False,
    },
    {
        "provider_id": "grok",
        "display_name": "Grok",
        "enabled": True,
        "mode": "dry_run",
        "live_execution_available": False,
        "capabilities": [
            "read_repository",
            "edit_repository",
            "run_tests",
            "produce_patch",
        ],
        "prohibited_capabilities": ["merge", "deploy", "production_write", "open_pr"],
        "supported_risk_levels": ["GREEN", "YELLOW"],
        "supported_operating_modes": [
            "READ_ONLY_AUDIT",
            "PLAN_ONLY",
            "DOCUMENTATION_ONLY",
            "IMPLEMENTATION",
            "REVIEW",
        ],
        "max_concurrent_runs": 1,
        "default_timeout_minutes": 30,
        "max_timeout_minutes": 120,
        "supports_cancel": True,
        "supports_resume": False,
        "credentials_required": True,
        "credentials_configured": False,
    },
    {
        "provider_id": "glm",
        "display_name": "GLM",
        "enabled": True,
        "mode": "dry_run",
        "live_execution_available": False,
        "capabilities": [
            "read_repository",
            "edit_repository",
            "run_tests",
            "produce_patch",
        ],
        "prohibited_capabilities": ["merge", "deploy", "production_write", "open_pr"],
        "supported_risk_levels": ["GREEN", "YELLOW"],
        "supported_operating_modes": [
            "READ_ONLY_AUDIT",
            "PLAN_ONLY",
            "DOCUMENTATION_ONLY",
            "IMPLEMENTATION",
            "REVIEW",
        ],
        "max_concurrent_runs": 1,
        "default_timeout_minutes": 30,
        "max_timeout_minutes": 90,
        "supports_cancel": True,
        "supports_resume": False,
        "credentials_required": True,
        "credentials_configured": False,
    },
]

FORBIDDEN_LIVE_CAPABILITIES = frozenset({"merge", "deploy", "production_write"})
PLANNING_EDITABLE_FIELDS = frozenset(
    {
        "enabled",
        "max_concurrent_runs",
        "default_timeout_minutes",
        "max_timeout_minutes",
        "supported_operating_modes",
        "supported_risk_levels",
    }
)


def default_provider_registry() -> list[dict[str, Any]]:
    now = utc_now_iso()
    out = []
    for item in DEFAULT_PROVIDERS:
        record = deepcopy(item)
        record["updated_at"] = now
        # Hard force dry-run only
        record["mode"] = "dry_run"
        record["live_execution_available"] = False
        record["credentials_configured"] = False
        out.append(record)
    return out


def get_provider(providers: list[dict[str, Any]], provider_id: str) -> dict[str, Any] | None:
    for item in providers:
        if str(item.get("provider_id")) == str(provider_id):
            return item
    return None


def sanitize_provider_update(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply non-secret planning field updates only."""
    updated = deepcopy(existing)
    for key, value in (patch or {}).items():
        if key not in PLANNING_EDITABLE_FIELDS:
            continue
        updated[key] = value
    # Never allow live mode or credentials through patch
    updated["mode"] = "dry_run"
    updated["live_execution_available"] = False
    updated["credentials_configured"] = False
    # Strip any accidental secret keys
    for bad in list(updated.keys()):
        low = str(bad).lower()
        if any(x in low for x in ("token", "secret", "password", "api_key", "apikey", "credential")):
            updated.pop(bad, None)
    updated["updated_at"] = utc_now_iso()
    # Clamp numbers
    try:
        updated["max_concurrent_runs"] = max(1, min(10, int(updated.get("max_concurrent_runs") or 1)))
    except (TypeError, ValueError):
        updated["max_concurrent_runs"] = 1
    try:
        updated["default_timeout_minutes"] = max(1, min(120, int(updated.get("default_timeout_minutes") or 30)))
    except (TypeError, ValueError):
        updated["default_timeout_minutes"] = 30
    try:
        updated["max_timeout_minutes"] = max(
            int(updated["default_timeout_minutes"]),
            min(120, int(updated.get("max_timeout_minutes") or 120)),
        )
    except (TypeError, ValueError):
        updated["max_timeout_minutes"] = 120
    return updated


def provider_supports(provider: dict[str, Any], *, risk: str, mode: str, capabilities: list[str]) -> list[str]:
    """Return blocking reasons if provider cannot handle request; empty list if OK."""
    reasons: list[str] = []
    if not provider.get("enabled", False):
        reasons.append("provider disabled")
    if str(provider.get("mode")) != "dry_run":
        reasons.append("only dry_run mode allowed in Stage 5")
    if provider.get("live_execution_available"):
        reasons.append("live execution must be false in Stage 5")
    if str(risk).upper() not in {str(r).upper() for r in (provider.get("supported_risk_levels") or [])}:
        reasons.append(f"provider does not support risk {risk}")
    if str(mode).upper() not in {str(m).upper() for m in (provider.get("supported_operating_modes") or [])}:
        reasons.append(f"provider does not support operating mode {mode}")
    caps = set(provider.get("capabilities") or [])
    prohibited = set(provider.get("prohibited_capabilities") or []) | FORBIDDEN_LIVE_CAPABILITIES
    for cap in capabilities:
        if cap in prohibited:
            reasons.append(f"capability forbidden: {cap}")
        elif cap not in caps:
            reasons.append(f"capability unsupported: {cap}")
    return reasons
