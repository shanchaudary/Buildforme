"""Agent packet generator for Buildforme Stage 3.

Produces tool-neutral handoff packets Shan can paste into Grok, Codex, Claude,
GLM, or any future coding agent. Does not execute agents, call provider APIs,
write to GitHub, or grant production/merge authority.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from buildforme.policy import RiskLevel, classify_github_item, classify_task
from buildforme.storage import utc_now_iso

DEFAULT_REPO = "shanchaudary/Buildforme"
DEFAULT_BRANCH = "main"
DEFAULT_FORBIDDEN_FILES = [".env", "secrets/**", "credentials/**", "**/*token*", "**/*secret*"]

GOVERNANCE_DOCS = [
    "README.md",
    "AGENTS.md",
    "docs/ARCHITECTURE.md",
    "docs/OPERATING_MODEL.md",
    "docs/ROADMAP.md",
]

ALWAYS_FORBIDDEN_ACTIONS = [
    "Print, log, or commit secrets, tokens, API keys, or .env contents",
    "Commit .env or secret files",
    "Production writes or live data mutation without explicit Shan approval",
    "Production deployment",
    "Payment capture, refunds, or Stripe live actions",
    "Auto-merge or merge to main without human approval on GitHub",
    "GitHub write actions (labels, comments, reviews, PR create) unless the packet explicitly allows them",
    "Broad unrelated refactors",
    "Fake success, hide failing tests, or stub production behavior as complete",
    "Bypass auth or disable security controls",
    "Database migrations without explicit Shan approval",
    "Store or rotate provider credentials",
]

FINAL_REPORT_TEMPLATE = """Task ID:
Agent:
Branch:
Commit:
Files changed:
Commands run:
Tests:
Manual proof:
Data mutation: none / describe
Secrets: none / describe
External services: none / describe
GitHub writes: none / describe
Risk result:
What works:
What does not work:
Remaining risks:
Recommended next task:
Final git status:
"""

SECRET_KEY_HINTS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "private_key",
    "access_key",
)


def generate_agent_packet(input_data: dict[str, Any]) -> dict[str, Any]:
    """Generate a complete agent handoff packet from a generator request."""
    if not isinstance(input_data, dict):
        raise ValueError("input_data must be a JSON object")

    source_type = str(input_data.get("source_type") or "manual").strip().lower()
    if source_type not in {"manual", "task", "pull_request", "issue"}:
        raise ValueError("source_type must be manual, task, pull_request, or issue")

    if source_type == "task":
        task = input_data.get("task")
        if isinstance(task, dict):
            base = packet_from_task(task, overrides=input_data)
        else:
            # Form re-generate after import uses flat fields only.
            base = _packet_from_manual({**input_data, "source_type": "manual"})
            base["source_type"] = "task"
    elif source_type == "pull_request":
        pr = input_data.get("pull_request") or input_data.get("pr")
        if isinstance(pr, dict):
            base = packet_from_pr(pr, overrides=input_data)
        else:
            base = _packet_from_manual({**input_data, "source_type": "manual"})
            base["source_type"] = "pull_request"
    elif source_type == "issue":
        issue = input_data.get("issue")
        if isinstance(issue, dict):
            base = packet_from_issue(issue, overrides=input_data)
        else:
            base = _packet_from_manual({**input_data, "source_type": "manual"})
            base["source_type"] = "issue"
    else:
        base = _packet_from_manual(input_data)

    # Optional field overrides from form after import
    base = _apply_overrides(base, input_data)
    base["markdown"] = render_packet_markdown(base)
    return base


def packet_from_task(task: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a generator packet from a saved task record or raw task packet."""
    overrides = overrides or {}
    raw_task = task.get("task") if isinstance(task.get("task"), dict) else task
    if not isinstance(raw_task, dict):
        raise ValueError("task must be an object")

    objective = str(raw_task.get("objective") or overrides.get("objective") or "").strip()
    if not objective:
        raise ValueError("objective is required")

    title = str(
        overrides.get("title")
        or raw_task.get("task_id")
        or objective[:80]
    ).strip()
    mode = str(raw_task.get("operating_mode") or overrides.get("operating_mode") or "IMPLEMENTATION")
    allowed = _listish(raw_task.get("allowed_files") or overrides.get("allowed_files") or ["docs/**"])
    forbidden = _listish(raw_task.get("forbidden_files") or overrides.get("forbidden_files") or DEFAULT_FORBIDDEN_FILES)
    acceptance = _listish(
        raw_task.get("acceptance_criteria") or overrides.get("acceptance_criteria") or ["Report findings"]
    )

    classify_payload = {
        "task_id": str(raw_task.get("task_id") or f"PKT-{uuid.uuid4().hex[:8]}"),
        "objective": objective,
        "operating_mode": mode,
        "allowed_files": allowed,
        "forbidden_files": forbidden,
        "acceptance_criteria": acceptance,
        "data_mutation_allowed": bool(raw_task.get("data_mutation_allowed", False)),
        "files_changed": _listish(raw_task.get("files_changed") or []),
    }
    classification = classify_task(classify_payload)

    return _assemble_packet(
        title=title,
        source_type="task",
        source_ref={
            "task_id": classify_payload["task_id"],
            "saved_status": task.get("status"),
        },
        target_repository=str(overrides.get("target_repository") or raw_task.get("repository") or DEFAULT_REPO),
        target_branch=str(overrides.get("target_branch") or raw_task.get("target_branch") or DEFAULT_BRANCH),
        operating_mode=mode,
        classification=classification.to_dict(),
        allowed_files=allowed,
        forbidden_files=forbidden,
        objective=objective,
        context=str(overrides.get("context") or raw_task.get("context") or "Imported from saved task packet."),
        acceptance_criteria=acceptance,
        required_tests=_listish(overrides.get("required_tests") or []),
        manual_proof=_listish(overrides.get("manual_proof") or []),
        files_changed=_listish(raw_task.get("files_changed") or []),
        packet_id=str(overrides.get("id") or ""),
    )


def packet_from_pr(pr: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a generator packet from a PR-shaped dict (API or work-queue row)."""
    overrides = overrides or {}
    repository = str(
        overrides.get("target_repository")
        or pr.get("repository")
        or pr.get("repo")
        or DEFAULT_REPO
    )
    number = int(pr.get("number") or overrides.get("number") or 0)
    title = str(overrides.get("title") or pr.get("title") or f"Review PR #{number}").strip()
    body = str(pr.get("body") or "")
    files = _file_names(pr)
    classification = pr.get("classification")
    if not isinstance(classification, dict):
        result = classify_github_item(
            item_type="pull_request",
            repository=repository,
            number=number or 0,
            title=title,
            body=body,
            labels=[],
            files_changed=files,
            draft=bool(pr.get("draft")),
            ci_status=str((pr.get("ci") or {}).get("status") or pr.get("ci_status") or "unknown"),
        )
        classification = result.to_dict()

    objective = str(
        overrides.get("objective")
        or f"Review and complete safe follow-up for PR #{number}: {title}"
    ).strip()
    mode = str(overrides.get("operating_mode") or "REVIEW")
    allowed = _listish(overrides.get("allowed_files") or files or ["docs/**", "tests/**"])
    forbidden = _listish(overrides.get("forbidden_files") or DEFAULT_FORBIDDEN_FILES)
    context_bits = [
        f"Source: pull request #{number} in {repository}",
        f"State: {pr.get('state') or 'open'}",
        f"Draft: {bool(pr.get('draft'))}",
        f"CI: {str((pr.get('ci') or {}).get('status') or pr.get('ci_status') or 'unknown')}",
        f"Recommended action: {pr.get('recommended_action') or 'Review required'}",
    ]
    if body.strip():
        context_bits.append(f"PR body (truncated):\n{body[:1500]}")

    return _assemble_packet(
        title=title,
        source_type="pull_request",
        source_ref={
            "repository": repository,
            "number": number,
            "html_url": pr.get("html_url"),
            "ci_status": str((pr.get("ci") or {}).get("status") or "unknown"),
        },
        target_repository=repository,
        target_branch=str(overrides.get("target_branch") or pr.get("head") or pr.get("base") or DEFAULT_BRANCH),
        operating_mode=mode,
        classification=classification,
        allowed_files=allowed,
        forbidden_files=forbidden,
        objective=objective,
        context=str(overrides.get("context") or "\n".join(context_bits)),
        acceptance_criteria=_listish(
            overrides.get("acceptance_criteria")
            or [
                "PR intent understood",
                "Risk gates respected",
                "Tests/CI considered",
                "No secrets exposed",
            ]
        ),
        required_tests=_listish(overrides.get("required_tests") or []),
        manual_proof=_listish(overrides.get("manual_proof") or []),
        files_changed=files,
        packet_id=str(overrides.get("id") or ""),
    )


def packet_from_issue(issue: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a generator packet from an issue-shaped dict."""
    overrides = overrides or {}
    repository = str(
        overrides.get("target_repository")
        or issue.get("repository")
        or issue.get("repo")
        or DEFAULT_REPO
    )
    number = int(issue.get("number") or overrides.get("number") or 0)
    title = str(overrides.get("title") or issue.get("title") or f"Issue #{number}").strip()
    body = str(issue.get("body") or "")
    labels = [str(x) for x in (issue.get("labels") or [])]
    classification = issue.get("classification")
    if not isinstance(classification, dict):
        result = classify_github_item(
            item_type="issue",
            repository=repository,
            number=number or 0,
            title=title,
            body=body,
            labels=labels,
            files_changed=[],
        )
        classification = result.to_dict()

    objective = str(overrides.get("objective") or f"Address issue #{number}: {title}").strip()
    mode = str(overrides.get("operating_mode") or "IMPLEMENTATION")
    risk = str(classification.get("risk") or "RED")
    if risk == "GREEN":
        mode = str(overrides.get("operating_mode") or "READ_ONLY_AUDIT")
    elif risk == "BLACK":
        mode = "PLAN_ONLY"

    allowed = _listish(overrides.get("allowed_files") or ["docs/**", "tests/**"])
    forbidden = _listish(overrides.get("forbidden_files") or DEFAULT_FORBIDDEN_FILES)
    context_bits = [
        f"Source: issue #{number} in {repository}",
        f"Labels: {', '.join(labels) if labels else '(none)'}",
        f"Recommended action: {issue.get('recommended_action') or 'Classify and scope carefully'}",
    ]
    if body.strip():
        context_bits.append(f"Issue body (truncated):\n{body[:1500]}")

    return _assemble_packet(
        title=title,
        source_type="issue",
        source_ref={
            "repository": repository,
            "number": number,
            "html_url": issue.get("html_url"),
            "labels": labels,
        },
        target_repository=repository,
        target_branch=str(overrides.get("target_branch") or DEFAULT_BRANCH),
        operating_mode=mode,
        classification=classification,
        allowed_files=allowed,
        forbidden_files=forbidden,
        objective=objective,
        context=str(overrides.get("context") or "\n".join(context_bits)),
        acceptance_criteria=_listish(
            overrides.get("acceptance_criteria")
            or [
                "Issue acceptance criteria covered",
                "Tests pass where applicable",
                "No secrets exposed",
                "Final report completed",
            ]
        ),
        required_tests=_listish(overrides.get("required_tests") or []),
        manual_proof=_listish(overrides.get("manual_proof") or []),
        files_changed=[],
        packet_id=str(overrides.get("id") or ""),
    )


def render_packet_markdown(packet: dict[str, Any]) -> str:
    """Render a packet dict as Markdown suitable for agent handoff."""
    risk = str(packet.get("risk") or "RED")
    lines: list[str] = [
        f"# Agent Packet — {packet.get('title') or 'Untitled'}",
        "",
        "> This packet is an instruction set only. It does **not** authorize production writes,",
        "> secrets, deployments, payments, merges, or GitHub mutations unless Shan explicitly",
        "> approved them outside this packet.",
        "",
        "## Header",
        "",
        f"- **Packet ID:** `{packet.get('id') or ''}`",
        f"- **Repository:** `{packet.get('target_repository') or DEFAULT_REPO}`",
        f"- **Branch:** `{packet.get('target_branch') or DEFAULT_BRANCH}`",
        f"- **Source:** `{packet.get('source_type') or 'manual'}` { _source_ref_line(packet.get('source_ref')) }",
        f"- **Risk:** **{risk}**",
        f"- **Operating mode:** `{packet.get('operating_mode') or 'IMPLEMENTATION'}`",
        f"- **Auto-run allowed:** `{_bool_from_classification(packet, 'auto_run_allowed')}`",
        f"- **Auto-merge allowed:** `false` (never granted by Buildforme)",
        f"- **Human approval required:** `{_bool_from_classification(packet, 'required_human_approval')}`",
        "",
        "## Mission",
        "",
        str(packet.get("objective") or "").strip() or "(missing objective)",
        "",
        "## Context",
        "",
        str(packet.get("context") or "None provided.").strip(),
        "",
        "## Starting checks",
        "",
        "Run these first. If the tree is dirty unexpectedly, **stop and report**.",
        "",
    ]
    for cmd in packet.get("starting_commands") or []:
        lines.append(f"- `{cmd}`")
    lines.extend(["", "## Files to inspect", ""])
    for path in packet.get("files_to_inspect") or []:
        lines.append(f"- `{path}`")
    lines.extend(["", "## Allowed files (scope)", ""])
    for path in packet.get("allowed_files") or []:
        lines.append(f"- `{path}`")
    lines.extend(["", "## Forbidden files", ""])
    for path in packet.get("forbidden_files") or []:
        lines.append(f"- `{path}`")
    lines.extend(["", "## Allowed actions", ""])
    for item in packet.get("allowed_actions") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Forbidden actions", ""])
    for item in packet.get("forbidden_actions") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Required tests", ""])
    for item in packet.get("required_tests") or []:
        lines.append(f"- `{item}`" if not str(item).startswith("Manual") else f"- {item}")
    lines.extend(["", "## Manual proof required", ""])
    for item in packet.get("manual_proof") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Acceptance criteria", ""])
    for item in packet.get("acceptance_criteria") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Stop conditions", ""])
    for item in packet.get("stop_conditions") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Final report template",
            "",
            "```text",
            str(packet.get("final_report_template") or FINAL_REPORT_TEMPLATE).rstrip(),
            "```",
            "",
            "## Next-step recommendation",
            "",
            str(packet.get("next_step_recommendation") or "Report results to Shan.").strip(),
            "",
            "---",
            "",
            "_Generated by Buildforme Agent Packet Generator. Tool-neutral. No live agent execution._",
            "",
        ]
    )
    return "\n".join(lines)


def sanitize_for_storage(packet: dict[str, Any]) -> dict[str, Any]:
    """Return a copy safe to persist (strip token-like fields recursively)."""
    return _scrub(packet)  # type: ignore[return-value]


def _packet_from_manual(input_data: dict[str, Any]) -> dict[str, Any]:
    objective = str(input_data.get("objective") or "").strip()
    if not objective:
        raise ValueError("objective is required")
    title = str(input_data.get("title") or objective[:80]).strip()
    mode = str(input_data.get("operating_mode") or "IMPLEMENTATION").strip()
    allowed = _listish(input_data.get("allowed_files") or ["docs/**", "tests/**"])
    forbidden = _listish(input_data.get("forbidden_files") or DEFAULT_FORBIDDEN_FILES)
    acceptance = _listish(input_data.get("acceptance_criteria") or ["Complete objective", "No secrets exposed"])
    task = {
        "task_id": str(input_data.get("task_id") or f"PKT-{uuid.uuid4().hex[:8]}"),
        "objective": objective,
        "operating_mode": mode,
        "allowed_files": allowed,
        "forbidden_files": forbidden,
        "acceptance_criteria": acceptance,
        "data_mutation_allowed": bool(input_data.get("data_mutation_allowed", False)),
        "files_changed": _listish(input_data.get("files_changed") or []),
    }
    classification = classify_task(task)
    return _assemble_packet(
        title=title,
        source_type="manual",
        source_ref={"task_id": task["task_id"]},
        target_repository=str(input_data.get("target_repository") or DEFAULT_REPO),
        target_branch=str(input_data.get("target_branch") or DEFAULT_BRANCH),
        operating_mode=mode,
        classification=classification.to_dict(),
        allowed_files=allowed,
        forbidden_files=forbidden,
        objective=objective,
        context=str(input_data.get("context") or "Manual objective entered in Buildforme."),
        acceptance_criteria=acceptance,
        required_tests=_listish(input_data.get("required_tests") or []),
        manual_proof=_listish(input_data.get("manual_proof") or []),
        files_changed=_listish(input_data.get("files_changed") or []),
        packet_id=str(input_data.get("id") or ""),
    )


def _assemble_packet(
    *,
    title: str,
    source_type: str,
    source_ref: dict[str, Any],
    target_repository: str,
    target_branch: str,
    operating_mode: str,
    classification: dict[str, Any],
    allowed_files: list[str],
    forbidden_files: list[str],
    objective: str,
    context: str,
    acceptance_criteria: list[str],
    required_tests: list[str],
    manual_proof: list[str],
    files_changed: list[str],
    packet_id: str = "",
) -> dict[str, Any]:
    risk = str(classification.get("risk") or RiskLevel.RED.value)
    now = utc_now_iso()
    branch = target_branch or DEFAULT_BRANCH
    files_to_inspect = _files_to_inspect(allowed_files, files_changed)
    tests = required_tests or _default_tests(risk=risk, allowed_files=allowed_files, files_changed=files_changed)
    proof = manual_proof or _default_manual_proof()
    packet = {
        "id": packet_id or f"pkt_{uuid.uuid4().hex[:12]}",
        "title": title,
        "source_type": source_type,
        "source_ref": source_ref,
        "target_repository": target_repository or DEFAULT_REPO,
        "target_branch": branch,
        "operating_mode": operating_mode,
        "risk": risk,
        "classification": classification,
        "allowed_files": allowed_files,
        "forbidden_files": forbidden_files or list(DEFAULT_FORBIDDEN_FILES),
        "objective": objective,
        "context": context,
        "starting_commands": [
            "git fetch origin",
            f"git checkout {branch}",
            f"git pull --ff-only origin {branch}",
            "git status --short",
            "git log -1 --oneline",
        ],
        "files_to_inspect": files_to_inspect,
        "allowed_actions": _allowed_actions(risk, operating_mode),
        "forbidden_actions": list(ALWAYS_FORBIDDEN_ACTIONS),
        "required_tests": tests,
        "manual_proof": proof,
        "acceptance_criteria": acceptance_criteria,
        "stop_conditions": _stop_conditions(risk),
        "final_report_template": FINAL_REPORT_TEMPLATE.strip() + "\n",
        "next_step_recommendation": _next_step(risk),
        "disclaimer": (
            "This packet does not authorize production writes, secrets, deployments, "
            "payments, merges, or GitHub mutations unless explicitly stated and approved by Shan."
        ),
        "created_at": now,
        "updated_at": now,
    }
    packet["markdown"] = render_packet_markdown(packet)
    return packet


def _apply_overrides(packet: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    """Apply selected form fields after source import without dropping classification safety."""
    mapping = {
        "title": "title",
        "target_repository": "target_repository",
        "target_branch": "target_branch",
        "objective": "objective",
        "context": "context",
        "operating_mode": "operating_mode",
    }
    out = dict(packet)
    for key, dest in mapping.items():
        if key in input_data and str(input_data.get(key) or "").strip():
            out[dest] = str(input_data[key]).strip()

    if "allowed_files" in input_data and input_data["allowed_files"] not in (None, ""):
        out["allowed_files"] = _listish(input_data["allowed_files"])
    if "forbidden_files" in input_data and input_data["forbidden_files"] not in (None, ""):
        out["forbidden_files"] = _listish(input_data["forbidden_files"])
    if "acceptance_criteria" in input_data and input_data["acceptance_criteria"] not in (None, ""):
        out["acceptance_criteria"] = _listish(input_data["acceptance_criteria"])
    if "required_tests" in input_data and input_data["required_tests"] not in (None, ""):
        out["required_tests"] = _listish(input_data["required_tests"])
    if "manual_proof" in input_data and input_data["manual_proof"] not in (None, ""):
        out["manual_proof"] = _listish(input_data["manual_proof"])

    # Re-classify if objective/mode/files changed via overrides
    task = {
        "task_id": str((out.get("source_ref") or {}).get("task_id") or out.get("id") or "PKT"),
        "objective": out["objective"],
        "operating_mode": out["operating_mode"],
        "allowed_files": out["allowed_files"],
        "forbidden_files": out["forbidden_files"],
        "acceptance_criteria": out["acceptance_criteria"],
        "data_mutation_allowed": bool(input_data.get("data_mutation_allowed", False)),
        "files_changed": _listish(input_data.get("files_changed") or []),
    }
    classification = classify_task(task).to_dict()
    out["classification"] = classification
    out["risk"] = classification["risk"]
    out["allowed_actions"] = _allowed_actions(out["risk"], out["operating_mode"])
    out["stop_conditions"] = _stop_conditions(out["risk"])
    out["next_step_recommendation"] = _next_step(out["risk"])
    out["files_to_inspect"] = _files_to_inspect(out["allowed_files"], task["files_changed"])
    if not out.get("required_tests"):
        out["required_tests"] = _default_tests(
            risk=out["risk"],
            allowed_files=out["allowed_files"],
            files_changed=task["files_changed"],
        )
    out["updated_at"] = utc_now_iso()
    out["markdown"] = render_packet_markdown(out)
    return out


def _allowed_actions(risk: str, operating_mode: str) -> list[str]:
    mode = operating_mode.upper()
    if risk == "BLACK":
        return [
            "Reject the request",
            "Rewrite the objective safely",
            "Report why execution is blocked",
            "Do not implement code changes",
        ]
    if risk == "RED":
        return [
            "Produce a plan, risks, and test strategy only",
            "Inspect allowed files read-only unless Shan explicitly approved implementation in writing",
            "Document required approvals before any code change",
            "Do not merge, deploy, or touch production data",
        ]
    if risk == "GREEN" or mode in {"READ_ONLY_AUDIT", "DOCUMENTATION_ONLY", "PLAN_ONLY", "REVIEW"}:
        actions = [
            "Read and analyze scoped files",
            "Propose or draft docs/tests if mode allows",
            "Run scoped verification commands",
            "Report final status with proof",
            "Do not merge",
        ]
        if mode == "DOCUMENTATION_ONLY":
            actions.insert(1, "Edit documentation files only within allowed globs")
        if mode == "PLAN_ONLY":
            actions = [
                "Produce an implementation plan only",
                "List risks, file scope, and tests",
                "Do not change production code unless Shan upgrades the packet",
                "Do not merge",
            ]
        if mode == "REVIEW":
            actions = [
                "Review the diff and risk classification",
                "Run or re-run required tests if safe",
                "Recommend approve / rework / block with reasons",
                "Do not merge",
            ]
        return actions
    # YELLOW / implementation
    return [
        "Implement only within allowed files",
        "Create/update tests covering the change",
        "Keep changes on a feature branch / PR only",
        "Run required tests and report proof",
        "Request human review before merge",
        "Do not merge",
    ]


def _stop_conditions(risk: str) -> list[str]:
    base = [
        "Working tree dirty unexpectedly after pull",
        "Required tests fail and cannot be fixed within scope",
        "Work would require forbidden files, secrets, or production access",
        "Risk appears higher than the packet classification",
        "Packet is incomplete or contradictory",
    ]
    if risk == "BLACK":
        return ["Any attempt to execute this packet as written — stop immediately"] + base
    if risk == "RED":
        return [
            "No explicit Shan approval for implementation beyond planning",
            "Any production, payment, secret, or merge request",
        ] + base
    return base


def _next_step(risk: str) -> str:
    if risk == "BLACK":
        return "Reject/rewrite the objective with Shan before any agent work."
    if risk == "RED":
        return "Wait for Shan approval before implementation; plan-only is allowed."
    if risk == "YELLOW":
        return "Agent may implement on a branch with tests; human review before merge."
    return "Agent may run unattended within scope; still no auto-merge."


def _default_tests(*, risk: str, allowed_files: list[str], files_changed: list[str]) -> list[str]:
    tests = [
        'python -m unittest discover -s tests -p "test_*.py"',
        "python -m buildforme.cli classify data/sample_task.json",
    ]
    joined = "\n".join(allowed_files + files_changed).lower()
    if any(token in joined for token in ("public/", "index.html", "app.js", "styles.css", "ui")):
        tests.append("Manual browser check at http://127.0.0.1:8787")
    if any(token in joined for token in ("github", "work_queue", "work-queue")):
        tests.append("Manual work-queue refresh for shanchaudary/Buildforme (read-only)")
    if risk in {"RED", "BLACK"}:
        tests.append("Confirm no production/secret/merge actions were performed")
    return tests


def _default_manual_proof() -> list[str]:
    return [
        "Browser screenshot or short description of UI check (if UI touched)",
        "Command outputs for tests and classify",
        "CI status for related PR if any (passing/failing/pending/unknown — never invent pass)",
        "Final git status --short",
        "What works / what does not work",
    ]


def _files_to_inspect(allowed_files: list[str], files_changed: list[str]) -> list[str]:
    ordered: list[str] = []
    for path in GOVERNANCE_DOCS + files_changed + allowed_files:
        value = str(path).strip()
        if value and value not in ordered and not value.startswith("("):
            ordered.append(value)
    return ordered[:40]


def _file_names(pr: dict[str, Any]) -> list[str]:
    if isinstance(pr.get("files"), list):
        names = []
        for item in pr["files"]:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("filename"):
                names.append(str(item["filename"]))
        if names:
            return names
    return _listish(pr.get("files_changed") or [])


def _listish(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _source_ref_line(source_ref: Any) -> str:
    if not isinstance(source_ref, dict):
        return ""
    bits = []
    if source_ref.get("repository"):
        bits.append(str(source_ref["repository"]))
    if source_ref.get("number") is not None:
        bits.append(f"#{source_ref['number']}")
    if source_ref.get("task_id"):
        bits.append(str(source_ref["task_id"]))
    return " ".join(bits)


def _bool_from_classification(packet: dict[str, Any], key: str) -> str:
    classification = packet.get("classification") or {}
    return "true" if classification.get(key) else "false"


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if any(hint in key_l for hint in SECRET_KEY_HINTS):
                cleaned[key] = "[redacted]"
                continue
            cleaned[key] = _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        # Redact obvious bearer/token patterns in free text
        text = re.sub(r"(?i)(bearer\s+)[a-z0-9._\\-]{8,}", r"\1[redacted]", value)
        text = re.sub(r"(?i)(ghp_|github_pat_|sk-)[a-z0-9]{10,}", "[redacted]", text)
        return text
    return value
