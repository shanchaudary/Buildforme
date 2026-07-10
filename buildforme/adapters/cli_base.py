"""Provider-neutral CLI adapter base for Stage 6 supervised execution."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from buildforme.process_supervisor import get_process_supervisor
from buildforme.provider_discovery import health_check_provider
from buildforme.storage import utc_now_iso


class CliProviderAdapter:
    """Shared contract for Codex / Claude / Grok / GLM CLI adapters.

    Provider-specific argv construction stays in subclasses.
    Core planner/constitution/approval/verification must not depend on subclass details.
    """

    provider_id: str = "generic"
    display_name: str = "Generic CLI"

    def __init__(self, provider_record: dict[str, Any] | None = None):
        self.provider_record = provider_record or {}

    def get_capabilities(self) -> list[str]:
        return list(
            self.provider_record.get("capabilities")
            or [
                "read_repository",
                "edit_repository",
                "run_tests",
                "produce_patch",
            ]
        )

    def discover_health(self) -> dict[str, Any]:
        return health_check_provider(self.provider_id, self.provider_record)

    def validate_request(self, run: dict[str, Any], packet: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        if not run.get("id"):
            problems.append("run id required")
        if not packet:
            problems.append("packet required")
        for cap in run.get("requested_capabilities") or []:
            if cap in {"merge", "deploy", "production_write"}:
                problems.append(f"blocked capability: {cap}")
        if str(run.get("target_branch") or "") in {"main", "master"}:
            if str(run.get("operating_mode") or "").upper() not in {
                "READ_ONLY_AUDIT",
                "PLAN_ONLY",
                "REVIEW",
                "DOCUMENTATION_ONLY",
            }:
                problems.append("implementation cannot target main/master")
        health = self.discover_health()
        if not health.get("available"):
            problems.append(f"provider executable unavailable: {', '.join(health.get('unsupported_reasons') or [])}")
        return problems

    def prepare_execution(self, run: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
        health = self.discover_health()
        problems = self.validate_request(run, packet)
        return {
            "prepared": not problems,
            "problems": problems,
            "mode": "live_supervised",
            "provider_id": self.provider_id,
            "transport": "cli",
            "health": health,
            "worktree_required": True,
            "prepared_at": utc_now_iso(),
        }

    def build_argv(self, run: dict[str, Any], packet: dict[str, Any], *, prompt_path: Path) -> list[str]:
        """Subclass must return argv list; never a shell string."""
        raise NotImplementedError

    def build_prompt(self, run: dict[str, Any], packet: dict[str, Any]) -> str:
        """Compact prompt: packet markdown + constitution reminder (not full re-dump every time)."""
        reminder = ""
        if isinstance(run.get("constitution_reminder"), dict):
            reminder = str(run["constitution_reminder"].get("text") or "")
        elif isinstance(packet.get("constitution_reminder"), dict):
            reminder = str(packet["constitution_reminder"].get("text") or "")
        md = str(packet.get("markdown") or packet.get("objective") or "")
        parts = [
            f"# Supervised run {run.get('id')}",
            f"Provider: {self.provider_id}",
            f"Repository worktree only. Branch: {run.get('target_branch')}",
            f"Baseline: {run.get('baseline_commit')}",
            "Do not merge, deploy, or access secrets.",
            "",
            reminder,
            "",
            md,
        ]
        return "\n".join(parts)

    def execute(
        self,
        run: dict[str, Any],
        packet: dict[str, Any],
        *,
        worktree_path: str | Path,
        on_event: Any = None,
    ) -> dict[str, Any]:
        problems = self.validate_request(run, packet)
        if problems:
            return {
                "ok": False,
                "error": "; ".join(problems),
                "provider_id": self.provider_id,
                "unavailable": True,
            }
        health = self.discover_health()
        executable = health.get("executable")
        if not executable:
            return {
                "ok": False,
                "error": "provider unavailable",
                "provider_id": self.provider_id,
                "unavailable": True,
                "health": health,
            }

        prompt = self.build_prompt(run, packet)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            delete=False,
            prefix=f"bf-{self.provider_id}-",
        ) as handle:
            handle.write(prompt)
            prompt_path = Path(handle.name)

        try:
            argv = self.build_argv(run, packet, prompt_path=prompt_path)
            # Ensure executable is argv[0]
            if argv and Path(argv[0]).name != Path(str(executable)).name:
                argv = [str(executable), *argv[1:]]
            elif not argv:
                argv = [str(executable)]
            else:
                argv[0] = str(executable)

            stdin_bytes = None
            if hasattr(self, "stdin_for_execution"):
                try:
                    stdin_bytes = self.stdin_for_execution(prompt_path=prompt_path)
                except Exception:
                    stdin_bytes = None

            timeout_min = int(run.get("timeout_minutes") or 30)
            supervisor = get_process_supervisor()
            result = supervisor.run(
                run_id=str(run.get("id")),
                argv=argv,
                cwd=worktree_path,
                timeout_seconds=max(30, timeout_min * 60),
                provider_id=self.provider_id,
                on_event=on_event,
                use_provider_env_allowlist=True,
                stdin_bytes=stdin_bytes,
            )
            result["provider_id"] = self.provider_id
            result["transport"] = "cli"
            result["health"] = health
            result["prompt_path"] = str(prompt_path)
            result["mode"] = "live_supervised"
            return result
        finally:
            try:
                prompt_path.unlink(missing_ok=True)
            except OSError:
                pass

    def cancel(self, run_id: str) -> dict[str, Any]:
        return get_process_supervisor().cancel(run_id)

    def get_status(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "provider_id": self.provider_id, "transport": "cli"}

    def dry_run(self, run: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
        """Planning view without launching provider."""
        health = self.discover_health()
        prep = self.prepare_execution(run, packet)
        return {
            "mode": "dry_run",
            "provider_id": self.provider_id,
            "would_execute": bool(health.get("available")) and prep.get("prepared"),
            "health": health,
            "planned_argv_shape": self._planned_argv_shape(),
            "summary": f"Dry-run for {self.provider_id}; no process launched.",
        }

    def _planned_argv_shape(self) -> list[str]:
        return [f"<{self.provider_id}-cli>", "<provider-specific-args>", "<prompt-file>"]


def write_json_prompt(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
