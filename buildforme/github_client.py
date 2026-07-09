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
        query = {"state": state, "per_page": str(min(max(limit, 1), 50))}
        data = self._get(f"/repos/{_repo_path(repository)}/issues", query)
        issues: list[dict[str, Any]] = []
        for item in data[:limit]:
            issues.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "html_url": item.get("html_url"),
                    "labels": [label.get("name") for label in item.get("labels", [])],
                    "is_pull_request": "pull_request" in item,
                    "updated_at": item.get("updated_at"),
                }
            )
        return issues

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        data = self._get(f"/repos/{_repo_path(repository)}/pulls/{int(number)}")
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "mergeable": data.get("mergeable"),
            "merged": data.get("merged"),
            "html_url": data.get("html_url"),
            "base": data.get("base", {}).get("ref"),
            "head": data.get("head", {}).get("ref"),
            "head_sha": data.get("head", {}).get("sha"),
            "changed_files": data.get("changed_files"),
            "additions": data.get("additions"),
            "deletions": data.get("deletions"),
        }

    def list_pull_request_files(self, repository: str, number: int, limit: int = 50) -> list[dict[str, Any]]:
        data = self._get(
            f"/repos/{_repo_path(repository)}/pulls/{int(number)}/files",
            {"per_page": str(min(max(limit, 1), 100))},
        )
        return [
            {
                "filename": item.get("filename"),
                "status": item.get("status"),
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "changes": item.get("changes"),
            }
            for item in data[:limit]
        ]

    def _get(self, path: str, query: dict[str, str] | None = None) -> Any:
        url = self._url(path, query)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            safe_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise GitHubClientError(f"GitHub request failed with HTTP {exc.code}: {safe_body}") from exc
        except urllib.error.URLError as exc:
            raise GitHubClientError(f"GitHub request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubClientError("GitHub returned invalid JSON") from exc

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


def _repo_path(repository: str) -> str:
    cleaned = repository.strip().removeprefix("https://github.com/").strip("/")
    parts = cleaned.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("repository must be in owner/name form")
    return "/".join(parts[:2])
