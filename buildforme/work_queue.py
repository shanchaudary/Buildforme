"""GitHub work queue assembly for the local supervisor dashboard.

Read-only against GitHub. Risk classification and local approvals are applied
in-process. This module never merges, labels, comments, or mutates GitHub.
"""

from __future__ import annotations

from typing import Any

from buildforme.github_client import GitHubClient, GitHubClientError
from buildforme.policy import classify_github_item, recommended_action_for
from buildforme.storage import LocalStore


DEFAULT_WATCHED_REPO = "shanchaudary/Buildforme"


def build_work_queue(
    store: LocalStore,
    client: GitHubClient,
    repos: list[str] | None = None,
    *,
    pr_limit: int = 20,
    issue_limit: int = 20,
) -> dict[str, Any]:
    """Assemble a work-queue payload for one or more repositories."""
    watched = _resolve_repos(store, repos)
    pull_requests: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    repo_summaries: list[dict[str, Any]] = []

    for repository in watched:
        try:
            repo_meta = client.get_repo(repository)
        except (GitHubClientError, ValueError) as exc:
            errors.append({"repository": repository, "error": str(exc)})
            repo_summaries.append({"full_name": repository, "error": str(exc)})
            continue

        repo_summaries.append(repo_meta)
        pr_errors = _append_pull_requests(
            client=client,
            store=store,
            repository=repository,
            pull_requests=pull_requests,
            limit=pr_limit,
        )
        errors.extend(pr_errors)

        issue_errors = _append_issues(
            client=client,
            store=store,
            repository=repository,
            issues=issues,
            limit=issue_limit,
        )
        errors.extend(issue_errors)

    summary = _build_summary(pull_requests, issues)
    recommended = pick_recommended_next_task(pull_requests, issues)

    return {
        "repos": repo_summaries,
        "watched_repositories": watched,
        "summary": summary,
        "pull_requests": pull_requests,
        "issues": issues,
        "recommended_next_task": recommended,
        "errors": errors,
        "github_token_configured": bool(client.token),
        "note": "GitHub access is read-only. Local approvals are not GitHub approvals.",
    }


def build_pr_status(
    client: GitHubClient,
    store: LocalStore,
    repository: str,
    number: int,
) -> dict[str, Any]:
    """Normalized single-PR status for the work queue / inspect views."""
    pr = client.get_pull_request(repository, number)
    files = client.list_pull_request_files(repository, number)
    filenames = [str(item.get("filename") or "") for item in files if item.get("filename")]
    head_sha = str(pr.get("head_sha") or "")
    ci = client.get_commit_checks(repository, head_sha) if head_sha else {"status": "unknown", "summary": "No head SHA"}

    classification = classify_github_item(
        item_type="pull_request",
        repository=repository,
        number=number,
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
        labels=[],
        files_changed=filenames,
        draft=bool(pr.get("draft")),
        ci_status=str(ci.get("status") or "unknown"),
    )
    local = store.find_approval("pull_request", repository, number)
    action = recommended_action_for(
        classification.risk,
        target_type="pull_request",
        ci_status=str(ci.get("status") or "unknown"),
        draft=bool(pr.get("draft")),
    )
    return {
        "repository": repository,
        "pull_request": pr,
        "files": files,
        "ci": ci,
        "classification": classification.to_dict(),
        "recommended_action": action,
        "local_approval": local,
        "note": "Local approval is not a GitHub review or merge approval.",
    }


def pick_recommended_next_task(
    pull_requests: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rank queue items into a single next-action recommendation."""
    items = [*pull_requests, *issues]

    def risk_of(item: dict[str, Any]) -> str:
        return str((item.get("classification") or {}).get("risk") or "RED")

    def is_pr(item: dict[str, Any]) -> bool:
        return item.get("target_type") == "pull_request"

    black = [i for i in items if risk_of(i) == "BLACK"]
    if black:
        item = black[0]
        return _recommendation(
            item,
            priority=1,
            headline="Reject or rewrite unsafe work",
            detail="BLACK risk detected. Do not run an agent on this as written.",
        )

    red = [i for i in items if risk_of(i) == "RED"]
    if red:
        item = red[0]
        return _recommendation(
            item,
            priority=2,
            headline="Shan decision required",
            detail="RED item is blocked until founder approval. Plan only; no unattended execution.",
        )

    failing = [
        i
        for i in pull_requests
        if str((i.get("ci") or {}).get("status") or "") == "failing"
    ]
    if failing:
        item = failing[0]
        return _recommendation(
            item,
            priority=3,
            headline="Fix failing CI",
            detail="A PR has failing checks. Prefer fixing CI before more feature work.",
        )

    yellow_pass = [
        i
        for i in pull_requests
        if risk_of(i) == "YELLOW" and str((i.get("ci") or {}).get("status") or "") == "passing"
    ]
    if yellow_pass:
        item = yellow_pass[0]
        return _recommendation(
            item,
            priority=4,
            headline="Review YELLOW PR (CI passing)",
            detail="Scoped implementation with green checks. Review required; no merge authority from Buildforme.",
        )

    green_issues = [i for i in issues if risk_of(i) == "GREEN"]
    if green_issues:
        item = green_issues[0]
        return _recommendation(
            item,
            priority=5,
            headline="Safe next agent task",
            detail="GREEN issue looks safe for an unattended agent run with a scoped task packet.",
        )

    green_prs = [i for i in pull_requests if risk_of(i) == "GREEN"]
    if green_prs:
        item = green_prs[0]
        return _recommendation(
            item,
            priority=5,
            headline="May review unattended",
            detail="GREEN PR. Review is fine unattended; still no auto-merge.",
        )

    if items:
        item = items[0]
        return _recommendation(
            item,
            priority=6,
            headline="Inspect open work",
            detail="No higher-priority signal. Inspect open items and create a scoped task packet.",
        )

    return {
        "priority": 7,
        "headline": "Create the next planned task",
        "detail": "No open PRs or issues in the watched repositories. Draft a task packet in Classify task.",
        "target_type": None,
        "repository": None,
        "number": None,
        "title": None,
        "risk": None,
        "html_url": None,
        "recommended_action": "Create next planned task packet in Classify task.",
    }


def _resolve_repos(store: LocalStore, repos: list[str] | None) -> list[str]:
    if repos:
        cleaned = []
        for repo in repos:
            value = str(repo).strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned or [DEFAULT_WATCHED_REPO]

    watched = store.list_repos()
    if not watched:
        store.add_repo(DEFAULT_WATCHED_REPO)
        return [DEFAULT_WATCHED_REPO]
    return watched


def _append_pull_requests(
    *,
    client: GitHubClient,
    store: LocalStore,
    repository: str,
    pull_requests: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    try:
        listed = client.list_pull_requests(repository, state="open", limit=limit)
    except (GitHubClientError, ValueError) as exc:
        return [{"repository": repository, "error": f"list PRs failed: {exc}"}]

    for summary in listed:
        number = int(summary.get("number") or 0)
        if number <= 0:
            continue
        try:
            detail = client.get_pull_request(repository, number)
            files = client.list_pull_request_files(repository, number)
            filenames = [str(f.get("filename") or "") for f in files if f.get("filename")]
            head_sha = str(detail.get("head_sha") or summary.get("head_sha") or "")
            try:
                ci = client.get_commit_checks(repository, head_sha) if head_sha else {
                    "status": "unknown",
                    "summary": "No head SHA",
                }
            except GitHubClientError as ci_exc:
                ci = {"status": "unknown", "summary": f"CI lookup failed: {ci_exc}"}

            classification = classify_github_item(
                item_type="pull_request",
                repository=repository,
                number=number,
                title=str(detail.get("title") or summary.get("title") or ""),
                body=str(detail.get("body") or ""),
                labels=[],
                files_changed=filenames,
                draft=bool(detail.get("draft")),
                ci_status=str(ci.get("status") or "unknown"),
            )
            local = store.find_approval("pull_request", repository, number)
            action = recommended_action_for(
                classification.risk,
                target_type="pull_request",
                ci_status=str(ci.get("status") or "unknown"),
                draft=bool(detail.get("draft")),
            )
            pull_requests.append(
                {
                    "target_type": "pull_request",
                    "repository": repository,
                    "number": number,
                    "title": detail.get("title") or summary.get("title"),
                    "state": detail.get("state") or summary.get("state"),
                    "draft": bool(detail.get("draft")),
                    "mergeable": detail.get("mergeable"),
                    "html_url": detail.get("html_url") or summary.get("html_url"),
                    "changed_files_count": detail.get("changed_files") or len(filenames),
                    "additions": detail.get("additions"),
                    "deletions": detail.get("deletions"),
                    "files": filenames[:30],
                    "ci": ci,
                    "classification": classification.to_dict(),
                    "recommended_action": action,
                    "local_approval": local,
                    "updated_at": detail.get("updated_at") or summary.get("updated_at"),
                }
            )
        except (GitHubClientError, ValueError) as exc:
            errors.append({"repository": repository, "error": f"PR #{number}: {exc}"})
    return errors


def _append_issues(
    *,
    client: GitHubClient,
    store: LocalStore,
    repository: str,
    issues: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    try:
        listed = client.list_issues(repository, state="open", limit=limit)
    except (GitHubClientError, ValueError) as exc:
        return [{"repository": repository, "error": f"list issues failed: {exc}"}]

    for item in listed:
        if item.get("is_pull_request"):
            continue
        number = int(item.get("number") or 0)
        if number <= 0:
            continue
        labels = [str(label) for label in (item.get("labels") or [])]
        classification = classify_github_item(
            item_type="issue",
            repository=repository,
            number=number,
            title=str(item.get("title") or ""),
            body=str(item.get("body") or ""),
            labels=labels,
            files_changed=[],
            draft=False,
            ci_status=None,
        )
        local = store.find_approval("issue", repository, number)
        action = recommended_action_for(
            classification.risk,
            target_type="issue",
            ci_status=None,
            draft=False,
        )
        issues.append(
            {
                "target_type": "issue",
                "repository": repository,
                "number": number,
                "title": item.get("title"),
                "state": item.get("state"),
                "labels": labels,
                "html_url": item.get("html_url"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "classification": classification.to_dict(),
                "recommended_action": action,
                "local_approval": local,
            }
        )
    return errors


def _build_summary(
    pull_requests: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, int]:
    items = [*pull_requests, *issues]
    ci_failures = sum(
        1 for pr in pull_requests if str((pr.get("ci") or {}).get("status") or "") == "failing"
    )
    blocked = sum(
        1
        for item in items
        if str((item.get("classification") or {}).get("risk") or "") in {"RED", "BLACK"}
        or str((item.get("local_approval") or {}).get("decision") or "") == "blocked"
    )
    ready_for_review = sum(
        1
        for pr in pull_requests
        if str((pr.get("classification") or {}).get("risk") or "") in {"GREEN", "YELLOW"}
        and str((pr.get("ci") or {}).get("status") or "") in {"passing", "unknown", "pending"}
        and not pr.get("draft")
    )
    safe_next = sum(
        1
        for item in items
        if str((item.get("classification") or {}).get("risk") or "") == "GREEN"
        and str((item.get("local_approval") or {}).get("decision") or "") != "blocked"
    )
    return {
        "open_prs": len(pull_requests),
        "open_issues": len(issues),
        "ci_failures": ci_failures,
        "blocked": blocked,
        "ready_for_review": ready_for_review,
        "safe_next_tasks": safe_next,
    }


def _recommendation(
    item: dict[str, Any],
    *,
    priority: int,
    headline: str,
    detail: str,
) -> dict[str, Any]:
    classification = item.get("classification") or {}
    return {
        "priority": priority,
        "headline": headline,
        "detail": detail,
        "target_type": item.get("target_type"),
        "repository": item.get("repository"),
        "number": item.get("number"),
        "title": item.get("title"),
        "risk": classification.get("risk"),
        "html_url": item.get("html_url"),
        "recommended_action": item.get("recommended_action"),
        "ci_status": (item.get("ci") or {}).get("status"),
    }


