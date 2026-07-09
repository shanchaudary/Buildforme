"""Deterministic risk policy for supervised AI engineering tasks.

The policy engine is intentionally conservative. It does not grant legal,
production, payment, deployment, or repository-merge authority. It classifies
work so an external supervisor can decide whether to auto-run, prepare a PR, or
wait for Shan.
"""

from __future__ import annotations

import re
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
    "secret storage",
    "credential storage",
    "provider credential",
    "rotate secret",
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
    "deployment",
    "migration",
    "prisma/migrations",
    "auth",
    "tenant",
    "stripe",
    "payment",
    ".github/workflows",
    "database",
    "s3",
    "email",
    "erp",
    "regulatory",
    "legal",
}

# Paths that are typically low-risk when they are the only changes.
LOW_RISK_PATH_PREFIXES = (
    "docs/",
    "public/",
    "tests/",
    "test/",
)

LOW_RISK_PATH_SUFFIXES = (
    ".md",
    ".txt",
    ".css",
    ".html",
)


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
    mode = str(task.get("operating_mode") or "").strip().upper()
    red_hits = _filter_red_hits_for_context(haystack, mode, red_hits)

    if problems:
        reasons.extend(problems)
    if red_hits:
        reasons.extend(f"High-risk term detected: {hit}" for hit in red_hits)
    if sensitive_file_hits:
        reasons.extend(f"Sensitive allowed/changed file or area detected: {hit}" for hit in sensitive_file_hits)

    explicit_mutation = bool(task.get("data_mutation_allowed"))
    if explicit_mutation:
        reasons.append("Task allows data mutation")

    if reasons:
        # PLAN_ONLY with high-risk topics remains RED (founder gate) but plan-only.
        if mode == "PLAN_ONLY" and not sensitive_file_hits and not explicit_mutation and not problems:
            return Classification(
                risk=RiskLevel.RED,
                auto_run_allowed=False,
                auto_merge_allowed=False,
                required_human_approval=True,
                reasons=reasons + ["PLAN_ONLY mode: planning allowed, implementation blocked without Shan approval"],
                required_actions=[
                    "Produce plan and risk analysis only",
                    "Do not implement code changes unless Shan upgrades the packet",
                    "No merge, deploy, secrets, or production writes",
                ],
            )
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


def classify_github_item(
    *,
    item_type: str,
    repository: str,
    number: int,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    files_changed: list[str] | None = None,
    draft: bool = False,
    ci_status: str | None = None,
) -> Classification:
    """Classify a GitHub PR or issue by converting it into a task packet.

    Sensitive paths are evaluated from `files_changed` only. Listing `.env` under
    forbidden_files remains a safety control and does not escalate by itself.
    """
    labels = labels or []
    files_changed = [path for path in (files_changed or []) if path]
    label_text = " ".join(labels)
    body_snippet = (body or "")[:1200]
    objective = "\n".join(part for part in (title, body_snippet, label_text) if part).strip()
    if not objective:
        objective = f"GitHub {item_type} #{number} in {repository}"

    if item_type == "pull_request":
        operating_mode = "REVIEW"
    elif any(token in objective.lower() for token in ("docs", "documentation", "readme")):
        operating_mode = "DOCUMENTATION_ONLY"
    elif any(token in objective.lower() for token in ("audit", "read-only", "review")):
        operating_mode = "READ_ONLY_AUDIT"
    elif any(token in objective.lower() for token in ("plan", "design")):
        operating_mode = "PLAN_ONLY"
    else:
        operating_mode = "IMPLEMENTATION"

    task: dict[str, Any] = {
        "task_id": f"GH-{item_type}-{repository.replace('/', '-')}-{number}",
        "objective": objective,
        "operating_mode": operating_mode,
        # Intentionally non-sensitive placeholders — risk paths come from files_changed.
        "allowed_files": ["(github-item)"],
        "forbidden_files": [".env", "secrets/**"],
        "acceptance_criteria": [
            "Work queue classification complete",
            "No secrets exposed",
            "Buildforme does not grant merge rights",
        ],
        "data_mutation_allowed": False,
        "files_changed": files_changed,
        "labels": labels,
        "repository": repository,
        "draft": draft,
        "ci_status": ci_status or "unknown",
    }

    classification = classify_task(task)

    # Docs/tests-only PR file sets should not stay RED solely due to uncertainty.
    if (
        classification.risk == RiskLevel.RED
        and files_changed
        and _files_are_low_risk_only(files_changed)
        and not _hits(_task_text(task), RED_PATTERNS)
        and not _hits(_task_text(task), BLACK_PATTERNS)
    ):
        return Classification(
            risk=RiskLevel.GREEN,
            auto_run_allowed=True,
            auto_merge_allowed=False,
            required_human_approval=False,
            reasons=["Changed files appear docs/UI/tests only with no high-risk terms"],
            required_actions=["Review diff", "Report final status", "No auto-merge"],
        )

    if (
        classification.risk == RiskLevel.RED
        and files_changed
        and not _files_are_low_risk_only(files_changed)
        and not _sensitive_file_hits(task)
        and not _hits(_task_text(task), RED_PATTERNS)
        and not _hits(_task_text(task), BLACK_PATTERNS)
        and item_type == "pull_request"
    ):
        # Implementation-shaped PR without explicit high-risk signals → YELLOW
        return Classification(
            risk=RiskLevel.YELLOW,
            auto_run_allowed=True,
            auto_merge_allowed=False,
            required_human_approval=True,
            reasons=["PR changes code paths without explicit high-risk terms; treat as scoped implementation"],
            required_actions=[
                "Review required",
                "Run required tests / confirm CI",
                "No merge without human approval",
            ],
        )

    return classification


def recommended_action_for(
    risk: RiskLevel | str,
    *,
    target_type: str = "task",
    ci_status: str | None = None,
    draft: bool = False,
) -> str:
    """Human-facing next action for work-queue rows."""
    risk_value = risk.value if isinstance(risk, RiskLevel) else str(risk).upper()
    if risk_value == "BLACK":
        return "Reject or rewrite. Unsafe instruction or secret/auth bypass risk."
    if risk_value == "RED":
        return "Blocked until Shan approval."
    if draft:
        return "Draft PR — continue review locally; no merge authority."
    if ci_status == "failing" and target_type == "pull_request":
        return "CI failing — fix checks before merge consideration."
    if risk_value == "YELLOW":
        if ci_status == "passing":
            return "Review required. May prepare merge recommendation after CI passes."
        if ci_status == "pending":
            return "Review required. Wait for CI to finish; no merge authority."
        return "Review required. May prepare merge recommendation after CI passes."
    if risk_value == "GREEN":
        if target_type == "issue":
            return "May run agent unattended with a scoped task packet. No merge authority."
        return "May review unattended. No merge authority."
    return "Risk uncertain — treat as blocked until Shan approval."


def _files_are_low_risk_only(files: list[str]) -> bool:
    if not files:
        return False
    for path in files:
        lowered = path.lower().replace("\\", "/")
        if any(pattern in lowered for pattern in SENSITIVE_FILE_PATTERNS):
            return False
        if lowered.startswith(".github/workflows"):
            return False
        if lowered.startswith(LOW_RISK_PATH_PREFIXES):
            continue
        if lowered.endswith(LOW_RISK_PATH_SUFFIXES):
            continue
        if lowered in {"readme.md", "agents.md", "pyproject.toml", "license", "license.md"}:
            continue
        if "/__pycache__/" in lowered or lowered.endswith(".pyc"):
            continue
        # Any other path (e.g. application code) is not low-risk-only.
        return False
    return True


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
    """Return patterns found in text.

    Multi-word and path-like patterns use substring match. Single-token patterns
    use word boundaries so ``auth`` does not match ``authority``.
    """
    found: list[str] = []
    for pattern in patterns:
        if any(ch in pattern for ch in " /._-"):
            if pattern in text:
                found.append(pattern)
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", text):
            found.append(pattern)
    return sorted(found)


# Modes where high-risk *topics* may be discussed without implying execution.
_DOC_AUDIT_MODES = {"DOCUMENTATION_ONLY", "READ_ONLY_AUDIT"}

# Phrases that still mean real execution even in docs/audit mode.
_EXECUTION_INTENT_PHRASES = {
    "deploy production",
    "deploy to production",
    "production write",
    "write production",
    "rewrite auth",
    "change auth",
    "implement auth",
    "disable auth",
    "bypass auth",
    "run migration",
    "apply migration",
    "capture payment",
    "charge card",
    "merge to main",
    "auto-merge",
    "rotate secret",
    "store credential",
    "send customer email",
}


def _filter_red_hits_for_context(haystack: str, mode: str, red_hits: list[str]) -> list[str]:
    """Reduce false positives when work is clearly docs/audit *about* high-risk topics.

    Does not weaken BLACK, sensitive files, or explicit execution phrasing.
    PLAN_ONLY keeps RED topic hits (founder gate) but classify_task handles plan-only actions.
    """
    if not red_hits:
        return red_hits
    if mode not in _DOC_AUDIT_MODES:
        return red_hits
    if any(phrase in haystack for phrase in _EXECUTION_INTENT_PHRASES):
        return red_hits
    # Pure documentation/audit language about risks — drop bare topic hits.
    doc_markers = (
        "document",
        "documentation",
        "docs",
        "describe",
        "explain",
        "audit",
        "read-only",
        "inspect",
        "review risks",
        "risk analysis",
        "plan only",
    )
    if any(marker in haystack for marker in doc_markers):
        return []
    return red_hits


def _sensitive_file_hits(task: dict[str, Any]) -> list[str]:
    """Detect sensitive files only when they are allowed, targeted, or changed.

    Listing `.env` or `secrets/**` under `forbidden_files` is a safety control,
    not a reason to escalate the task by itself.
    """
    file_values: list[str] = []
    for key in ("allowed_files", "files_changed", "target_files"):
        value = task.get(key, [])
        if isinstance(value, str):
            file_values.append(value)
        elif isinstance(value, Iterable):
            file_values.extend(str(item) for item in value)

    lowered = "\n".join(file_values).lower()
    return sorted(pattern for pattern in SENSITIVE_FILE_PATTERNS if pattern in lowered)
