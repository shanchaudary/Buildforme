"""Claude Code CLI adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from buildforme.adapters.cli_base import CliProviderAdapter


class ClaudeCliAdapter(CliProviderAdapter):
    provider_id = "claude"
    display_name = "Claude Code CLI"

    def build_argv(self, run: dict[str, Any], packet: dict[str, Any], *, prompt_path: Path) -> list[str]:
        # Prefer path-based instruction to avoid giant argv / command-line limits.
        return [
            "claude",
            "-p",
            f"Read and execute the supervised task packet file: {prompt_path}",
            "--output-format",
            "text",
        ]
