"""Read-only GitHub client for Buildforme.

The MVP uses GitHub's REST API through the standard library. A token is optional:
public repositories can be inspected without one, while private repositories can
be inspected by setting BUILDFORME_GITHUB_TOKEN or GITHUB_TOKEN locally. The app
must never display or persist the token value.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

GITHUB_API_BASE = "https://api.github.com"


class GitHubClientError(RuntimeError):
    """Raised when a read-only GitHub API request fails."""


@dataclass(frozen=True)
class GitHubClient:
    token: str | None = None
    api_base: str = GITHUB_API_BASE
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "GitHubClient":
        token = os.environ.get("BUILDFORME_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        return cls(token=token or None)

    def get_repo(self, repository: str) -> dict[str, Any]:
        data = self._get(f"/repos/{_repo_path(repository)}")
        return {
            "full_name": data.get("full_name"),
            "private": data.get("private"),
            "default_branch": data.get("default_branch"),
            "html_url": data.get("html_url"),
            "open_issues_count": data.get("open_issues_count"),
            "visibility": data.get("visibility"),
        }

    def list_issues(self, repository: str, state: str = "open", limit: int = 20) -> list[dict[str, Any]]:
        query = {
            "state": state,
            "per_page": str(min(max(limit, 1), 50)),
            "sort": "updated",
            "direction": "desc",
        }
        data = self._get(f"/repos/{_repo_path(repository)}/issues", query)
        if not isinstance(data, list):
            raise GitHubClientError("GitHub issues response was not a list")
        issues: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            issues.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "html_url": item.get("html_url"),
                    "body": item.get("body") or "",
                    "labels": [
                        label.get("name")
                        for label in item.get("labels", [])
                        if isinstance(label, dict) and label.get("name")
                    ],
                    "is_pull_request": "pull_request" in item,
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
            if len(issues) >= limit:
                break
        return issues

    def list_pull_requests(self, repository: str, state: str = "open", limit: int = 20) -> list[dict[str, Any]]:
        query = {
            "state": state,
            "per_page": str(min(max(limit, 1), 50)),
            "sort": "updated",
            "direction": "desc",
        }
        data = self._get(f"/repos/{_repo_path(repository)}/pulls", query)
        if not isinstance(data, list):
            raise GitHubClientError("GitHub pulls response was not a list")
        pulls: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            head = item.get("head") if isinstance(item.get("head"), dict) else {}
            base = item.get("base") if isinstance(item.get("base"), dict) else {}
            pulls.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "draft": item.get("draft"),
                    "html_url": item.get("html_url"),
                    "body": item.get("body") or "",
                    "base": base.get("ref"),
                    "head": head.get("ref"),
                    "head_sha": head.get("sha"),
                    "updated_at": item.get("updated_at"),
                    "created_at": item.get("created_at"),
                }
            )
            if len(pulls) >= limit:
                break
        return pulls

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        data = self._get(f"/repos/{_repo_path(repository)}/pulls/{int(number)}")
        if not isinstance(data, dict):
            raise GitHubClientError("GitHub pull request response was not an object")
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "mergeable": data.get("mergeable"),
            "merged": data.get("merged"),
            "html_url": data.get("html_url"),
            "body": data.get("body") or "",
            "base": base.get("ref"),
            "head": head.get("ref"),
            "head_sha": head.get("sha"),
            "changed_files": data.get("changed_files"),
            "additions": data.get("additions"),
            "deletions": data.get("deletions"),
            "updated_at": data.get("updated_at"),
            "created_at": data.get("created_at"),
            "user": (data.get("user") or {}).get("login") if isinstance(data.get("user"), dict) else None,
        }

    def list_pull_request_files(self, repository: str, number: int, limit: int = 50) -> list[dict[str, Any]]:
        data = self._get(
            f"/repos/{_repo_path(repository)}/pulls/{int(number)}/files",
            {"per_page": str(min(max(limit, 1), 100))},
        )
        if not isinstance(data, list):
            raise GitHubClientError("GitHub pull request files response was not a list")
        files: list[dict[str, Any]] = []
        for item in data[:limit]:
            if not isinstance(item, dict):
                continue
            files.append(
                {
                    "filename": item.get("filename"),
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "changes": item.get("changes"),
                }
            )
        return files

    def get_commit_checks(self, repository: str, ref: str) -> dict[str, Any]:
        """Return normalized CI status for a commit SHA or ref.

        Combines the legacy commit status API and check-runs. Never reports
        "passing" when no checks are found.
        """
        if not ref:
            return {"status": "unknown", "summary": "No commit ref provided", "checks": []}

        statuses: list[dict[str, Any]] = []
        check_runs: list[dict[str, Any]] = []
        notes: list[str] = []

        try:
            combined = self._get(f"/repos/{_repo_path(repository)}/commits/{ref}/status")
            if isinstance(combined, dict):
                for item in combined.get("statuses") or []:
                    if not isinstance(item, dict):
                        continue
                    statuses.append(
                        {
                            "name": item.get("context") or item.get("description") or "status",
                            "state": item.get("state"),
                            "source": "status",
                        }
                    )
                if combined.get("state") and not statuses:
                    # combined.state can be pending with empty statuses
                    notes.append(f"combined_state={combined.get('state')}")
        except GitHubClientError as exc:
            notes.append(f"status API: {exc}")

        try:
            checks_payload = self._get(
                f"/repos/{_repo_path(repository)}/commits/{ref}/check-runs",
                {"per_page": "50"},
            )
            if isinstance(checks_payload, dict):
                for item in checks_payload.get("check_runs") or []:
                    if not isinstance(item, dict):
                        continue
                    check_runs.append(
                        {
                            "name": item.get("name") or "check",
                            "status": item.get("status"),
                            "conclusion": item.get("conclusion"),
                            "source": "check_run",
                        }
                    )
        except GitHubClientError as exc:
            notes.append(f"check-runs API: {exc}")

        return _normalize_ci(statuses=statuses, check_runs=check_runs, notes=notes)

    def _get(self, path: str, query: dict[str, str] | None = None) -> Any:
        url = self._url(path, query)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            safe_body = exc.read().decode("utf-8", errors="replace")[:500]
            code = exc.code
            if code == 401:
                raise GitHubClientError(
                    "GitHub authentication failed (HTTP 401). Check BUILDFORME_GITHUB_TOKEN for private repos."
                ) from exc
            if code == 403:
                raise GitHubClientError(
                    f"GitHub rate limit or forbidden (HTTP 403): {safe_body}"
                ) from exc
            if code == 404:
                raise GitHubClientError(
                    "GitHub resource not found (HTTP 404). Check repository name and access."
                ) from exc
            raise GitHubClientError(f"GitHub request failed with HTTP {code}: {safe_body}") from exc
        except urllib.error.URLError as exc:
            raise GitHubClientError(f"GitHub request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubClientError("GitHub returned invalid JSON") from exc
        except TimeoutError as exc:
            raise GitHubClientError("GitHub request timed out") from exc

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        normalized_path = "/" + path.lstrip("/")
        url = self.api_base.rstrip("/") + normalized_path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "buildforme-supervisor-mvp",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def _normalize_ci(
    *,
    statuses: list[dict[str, Any]],
    check_runs: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, Any]:
    checks = [*statuses, *check_runs]
    if not checks:
        summary = "No checks found"
        if notes:
            summary = f"No checks found ({'; '.join(notes[:2])})"
        return {"status": "unknown", "summary": summary, "checks": [], "notes": notes}

    states: list[str] = []
    for item in statuses:
        state = str(item.get("state") or "").lower()
        if state:
            states.append(state)

    for item in check_runs:
        status = str(item.get("status") or "").lower()
        conclusion = str(item.get("conclusion") or "").lower()
        if status in {"queued", "in_progress", "pending"} and not conclusion:
            states.append("pending")
        elif conclusion in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}:
            states.append("failure")
        elif conclusion in {"success", "neutral", "skipped"}:
            states.append("success")
        elif conclusion:
            states.append(conclusion)
        else:
            states.append("pending")

    if any(state in {"failure", "error"} for state in states):
        normalized = "failing"
    elif any(state in {"pending", "expected"} for state in states):
        normalized = "pending"
    elif states and all(state in {"success", "neutral", "skipped"} for state in states):
        normalized = "passing"
    else:
        normalized = "unknown"

    return {
        "status": normalized,
        "summary": f"{normalized} ({len(checks)} check(s))",
        "checks": checks[:40],
        "notes": notes,
    }


def _repo_path(repository: str) -> str:
    cleaned = repository.strip().removeprefix("https://github.com/").strip("/")
    parts = cleaned.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("repository must be in owner/name form")
    return "/".join(parts[:2])
