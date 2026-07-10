"""Codex CLI adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from buildforme.adapters.cli_base import CliProviderAdapter


class CodexCliAdapter(CliProviderAdapter):
    provider_id = "codex"
    display_name = "Codex CLI"

    def build_argv(self, run: dict[str, Any], packet: dict[str, Any], *, prompt_path: Path) -> list[str]:
        # Prefer non-interactive exec-style invocation when available.
        # Exact flags vary by CLI version; discovery verifies binary exists.
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-q",
            str(prompt_path),
        ]
