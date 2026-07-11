"""Codex CLI adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from buildforme.adapters.cli_base import CliProviderAdapter


class CodexCliAdapter(CliProviderAdapter):
    provider_id = "codex"
    display_name = "Codex CLI"

    def build_argv(self, run: dict[str, Any], packet: dict[str, Any], *, prompt_path: Path) -> list[str]:
        # codex-cli 0.142+: `codex exec` is non-interactive.
        # Prompt: use `-` to read stdin (process supervisor supplies prompt file as stdin).
        # Do NOT pass prompt_path as the PROMPT argument (that would treat the path string as instructions).
        # Do NOT use invented flags like -q (not in this CLI family).
        # workspace-write: agent may edit files inside the supervised worktree cwd only.
        # Never use danger-full-access from Buildforme defaults.
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--json",
            "-s",
            "workspace-write",
            "-",  # read prompt from stdin
        ]

    def stdin_for_execution(self, *, prompt_path: Path) -> bytes | None:
        try:
            return Path(prompt_path).read_bytes()
        except OSError:
            return None
