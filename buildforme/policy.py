"""Deterministic risk policy for supervised AI engineering tasks.

The policy engine is intentionally conservative. It does not grant legal,
production, payment, deployment, or repository-merge authority. It classifies
work so an external supervisor can decide whether to auto-run, prepare a PR, or
wait for Shan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class RiskLevel(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    BLACK = "BLACK"


@dataclass(frozen=True)
class Classification:
    risk: RiskLevel
    auto_run_allowed: bool
    auto_merge_allowed: bool
    required_human_approval: bool
    reasons: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk.value,
            "auto_run_allowed": self.auto_run_allowed,
            "auto_merge_allowed": self.auto_merge_allowed,
            "required_human_approval": self.required_human_approval,
            "reasons": self.reasons,
            "required_actions": self.required_actions,
        }


REQUIRED_TASK_FIELDS = {
    "task_id",
    "objective",
    "operating_mode",
    "allowed_files",
    "forbidden_files",
    "acceptance_criteria",
}

BLACK_PATTERNS = {
    "print secret",
    "print secrets",
    "show api key",
    "commit .env",
    "commit env",
    "bypass auth",
    "disable auth",
    "fake success",
    "pretend it works",
    "skip tests and merge",
    "merge without review",
    "production write without approval",
    "delete audit log",
    "hide failing tests",
}

RED_PATTERNS = {
    "production",
    "deploy",
    "deployment",
    "stripe",
    "payment",
    "charge",
    "capture",
    "refund",
    "database migration",
    "migration",
    "rls",
    "row level security",
    "tenant isolation",
    "auth",
    "session",
    "secret",
    "credential",
    "write-mode ingestion",
    "write mode ingestion",
    "erp credential",
    "s3",
    "email customers",
    "send customer email",
    "legal conclusion",
    "regulatory conclusion",
    "merge to main",
    "auto-merge",
}

YELLOW_PATTERNS = {
    "fix",
    "implement",
    "route",
    "api",
    "frontend",
    "backend",
    "parser",
    "dashboard",
    "playwright",
    "test coverage",
    "component",
    "workflow",
}

GREEN_PATTERNS = {
    "read-only",
    "audit",
    "documentation",
    "docs",
    "test-only",
    "tests only",
    "lint",
    "type-only",
    "review",
    "plan",
}

SENSITIVE_FILE_PATTERNS = {
    ".env",
    "secrets",
    "credentials",
    "private-key",
    "id_rsa",
    "deploy",
    "migration",
    "prisma/migrations",
    "auth",
    "tenant",
    "stripe",
    "payment",
}


def validate_task_packet(task: dict[str, Any]) -> list[str]:
    """Return validation problems for a task packet."""
    problems: list[str] = []
    missing = sorted(REQUIRED_TASK_FIELDS - set(task.keys()))
    if missing:
        problems.append(f"Missing required fields: {', '.join(missing)}")

    if not isinstance(task.get("allowed_files", []), list):
        problems.append("allowed_files must be a list")
    if not isinstance(task.get("forbidden_files", []), list):
        problems.append("forbidden_files must be a list")
    if not str(task.get("objective", "")).strip():
        problems.append("objective must not be empty")
    if not str(task.get("operating_mode", "")).strip():
        problems.append("operating_mode must not be empty")

    return problems


def classify_task(task: dict[str, Any]) -> Classification:
    """Classify an AI engineering task.

    Conservative defaults:
    - invalid packets are RED
    - BLACK patterns override all other results
    - RED patterns require human approval
    - YELLOW work may prepare PRs but not merge
    - GREEN work may run unattended, but never auto-merges by default
    """
    problems = validate_task_packet(task)
    haystack = _task_text(task)
    reasons: list[str] = []
    actions: list[str] = []

    black_hits = _hits(haystack, BLACK_PATTERNS)
    if black_hits:
        return Classification(
            risk=RiskLevel.BLACK,
            auto_run_allowed=False,
            auto_merge_allowed=False,
            required_human_approval=True,
            reasons=[f"Blacklisted unsafe request: {hit}" for hit in black_hits],
            required_actions=["Reject task", "Ask user to rewrite safely"],
        )

    sensitive_file_hits = _sensitive_file_hits(task)
    red_hits = _hits(haystack, RED_PATTERNS)
    if problems:
        reasons.extend(problems)
    if red_hits:
        reasons.extend(f"High-risk term detected: {hit}" for hit in red_hits)
    if sensitive_file_hits:
        reasons.extend(f"Sensitive file or area detected: {hit}" for hit in sensitive_file_hits)

    explicit_mutation = bool(task.get("data_mutation_allowed"))
    if explicit_mutation:
        reasons.append("Task allows data mutation")

    if reasons:
        actions.extend(
            [
                "Require Shan approval before execution or merge",
                "Prepare plan and review packet before code changes",
                "Ensure tests cover failure paths and authorization boundaries",
            ]
        )
        return Classification(
            risk=RiskLevel.RED,
            auto_run_allowed=False,
            auto_merge_allowed=False,
            required_human_approval=True,
            reasons=reasons,
            required_actions=actions,
        )

    yellow_hits = _hits(haystack, YELLOW_PATTERNS)
    if yellow_hits:
        return Classification(
            risk=RiskLevel.YELLOW,
            auto_run_allowed=True,
            auto_merge_allowed=False,
            required_human_approval=True,
            reasons=[f"Implementation work detected: {hit}" for hit in yellow_hits],
            required_actions=[
                "Create branch or PR only",
                "Run required tests",
                "Send to second-pass review before merge",
            ],
        )

    green_hits = _hits(haystack, GREEN_PATTERNS)
    if green_hits:
        return Classification(
            risk=RiskLevel.GREEN,
            auto_run_allowed=True,
            auto_merge_allowed=False,
            required_human_approval=False,
            reasons=[f"Low-risk work detected: {hit}" for hit in green_hits],
            required_actions=["Run scoped checks", "Report final status"],
        )

    return Classification(
        risk=RiskLevel.RED,
        auto_run_allowed=False,
        auto_merge_allowed=False,
        required_human_approval=True,
        reasons=["Risk uncertain; defaulting to RED"],
        required_actions=["Ask Shan or reviewer for explicit approval"],
    )


def _task_text(task: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in task.items():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
        else:
            parts.append(str(value))
    return "\n".join(parts).lower()


def _hits(text: str, patterns: set[str]) -> list[str]:
    return sorted(pattern for pattern in patterns if pattern in text)


def _sensitive_file_hits(task: dict[str, Any]) -> list[str]:
    file_values: list[str] = []
    for key in ("allowed_files", "forbidden_files", "files_changed", "target_files"):
        value = task.get(key, [])
        if isinstance(value, str):
            file_values.append(value)
        elif isinstance(value, Iterable):
            file_values.extend(str(item) for item in value)

    lowered = "\n".join(file_values).lower()
    return sorted(pattern for pattern in SENSITIVE_FILE_PATTERNS if pattern in lowered)
