"""Adapter registry — maps provider_id to real adapter implementations."""

from __future__ import annotations

from typing import Any

from buildforme.adapters.claude_cli import ClaudeCliAdapter
from buildforme.adapters.codex_cli import CodexCliAdapter
from buildforme.adapters.dry_run import DryRunAdapter
from buildforme.adapters.glm_cli import GlmCliAdapter
from buildforme.adapters.grok_cli import GrokCliAdapter

_CLI_ADAPTERS = {
    "codex": CodexCliAdapter,
    "claude": ClaudeCliAdapter,
    "grok": GrokCliAdapter,
    "glm": GlmCliAdapter,
}


def get_adapter(
    provider_id: str,
    *,
    mode: str = "dry_run",
    provider_record: dict[str, Any] | None = None,
) -> Any:
    """Return adapter for provider. Live mode uses CLI adapters; dry_run uses DryRunAdapter."""
    pid = str(provider_id or "").strip().lower()
    mode_n = str(mode or "dry_run").strip().lower().replace("-", "_")
    record = provider_record or {"provider_id": pid}

    if mode_n in {"live", "live_supervised", "supervised"}:
        cls = _CLI_ADAPTERS.get(pid)
        if not cls:
            raise ValueError(f"no live CLI adapter for provider: {pid}")
        return cls(record)

    # Dry-run always available for every known provider id
    return DryRunAdapter(provider_id=pid)


def list_live_adapter_ids() -> list[str]:
    return sorted(_CLI_ADAPTERS.keys())


def all_providers_have_adapters() -> bool:
    required = {"codex", "claude", "grok", "glm"}
    return required.issubset(set(_CLI_ADAPTERS.keys()))
