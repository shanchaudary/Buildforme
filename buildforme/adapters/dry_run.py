"""Dry-run adapter — no network, shell, repo edit, or GitHub mutation."""

from __future__ import annotations

from typing import Any


class DryRunAdapter:
    """Simulates an execution plan without calling any provider."""

    def __init__(self, provider_id: str = "dry_run"):
        self.provider_id = provider_id

    def get_capabilities(self) -> list[str]:
        return [
            "read_repository",
            "edit_repository",
            "run_tests",
            "produce_patch",
            "open_pr",
        ]

    def validate_request(self, run: dict[str, Any], packet: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        if not run.get("id"):
            problems.append("run id required")
        if not packet:
            problems.append("packet required")
        for cap in run.get("requested_capabilities") or []:
            if cap in {"merge", "deploy", "production_write"}:
                problems.append(f"blocked capability: {cap}")
        return problems

    def prepare_execution(self, run: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
        return {
            "prepared": True,
            "mode": "dry_run",
            "provider_id": self.provider_id or run.get("provider_id"),
            "note": "Preparation only; no live execution.",
        }

    def dry_run(self, run: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
        problems = self.validate_request(run, packet)
        if problems:
            raise ValueError("; ".join(problems))

        provider_id = str(run.get("provider_id") or self.provider_id)
        allowed = list(packet.get("allowed_files") or run.get("allowed_files") or [])
        forbidden = list(packet.get("forbidden_files") or [".env", "secrets/**"])
        tests = list(packet.get("required_tests") or [])
        commands = list(packet.get("starting_commands") or [])
        if not tests:
            tests = [
                'python -m unittest discover -s tests -p "test_*.py"',
                "python -m buildforme.cli classify data/sample_task.json",
            ]

        requested = list(run.get("requested_capabilities") or [])
        blocked_steps = []
        planned_steps = [
            "Load packet and verify preflight already passed",
            f"Checkout target branch: {run.get('target_branch')}",
            "Inspect allowed files only",
        ]
        if "read_repository" in requested or not requested:
            planned_steps.append("Read repository context within allowed globs")
        if "edit_repository" in requested:
            planned_steps.append("Propose file edits within allowed globs (not applied in dry-run)")
        else:
            blocked_steps.append("edit_repository not requested")
        if "run_tests" in requested:
            planned_steps.append("Run required tests")
            planned_steps.extend(f"Would run: {t}" for t in tests[:5])
        if "produce_patch" in requested:
            planned_steps.append("Produce patch summary (not written to git in dry-run)")
        if "open_pr" in requested:
            planned_steps.append("Would prepare PR description only (no GitHub write in Stage 5)")
        for bad in ("merge", "deploy", "production_write"):
            if bad in requested:
                blocked_steps.append(f"{bad} is forbidden")

        return {
            "mode": "dry_run",
            "provider_id": provider_id,
            "would_execute": False,
            "requested_capabilities": requested,
            "planned_steps": planned_steps,
            "blocked_steps": blocked_steps,
            "commands_would_run": commands + tests,
            "expected_files": allowed[:40],
            "forbidden_files": forbidden,
            "required_tests": tests,
            "estimated_timeout_minutes": int(run.get("timeout_minutes") or 30),
            "network_calls": [],
            "filesystem_writes": ["runtime/runs.json", "runtime/run_events.json"],
            "github_writes": [],
            "shell_commands_executed": [],
            "summary": "Dry-run plan generated; no agent was called.",
        }

    def cancel(self, run_id: str) -> dict[str, Any]:
        return {
            "cancelled": True,
            "run_id": run_id,
            "mode": "dry_run",
            "note": "Dry-run cancel is local state only; no provider process exists.",
        }

    def get_status(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "mode": "dry_run", "provider_process": None}
