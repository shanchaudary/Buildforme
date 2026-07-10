"""Grok CLI adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from buildforme.adapters.cli_base import CliProviderAdapter


class GrokCliAdapter(CliProviderAdapter):
    provider_id = "grok"
    display_name = "Grok CLI"

    def build_argv(self, run: dict[str, Any], packet: dict[str, Any], *, prompt_path: Path) -> list[str]:
        return [
            "grok",
            "run",
            "--prompt-file",
            str(prompt_path),
        ]
