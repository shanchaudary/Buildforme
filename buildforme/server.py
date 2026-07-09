"""Local Buildforme supervisor server.

Dependency-free HTTP server for the MVP. Serves the static dashboard and JSON
APIs for policy classification, local storage, read-only GitHub inspection, and
the Stage 2 work queue. Never mutates GitHub objects.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from buildforme.github_client import GitHubClient, GitHubClientError
from buildforme.policy import classify_task, validate_task_packet
from buildforme.storage import DEFAULT_STATE_PATH, LocalStore
from buildforme.work_queue import build_pr_status, build_work_queue

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ROOT = PROJECT_ROOT / "public"


class BuildformeRequestHandler(BaseHTTPRequestHandler):
    server_version = "BuildformeMVP/0.3"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in {"/", "/public", "/public/", "/index.html"}:
            self._serve_file(PUBLIC_ROOT / "index.html")
            return
        if path.startswith("/public/"):
            relative = path.removeprefix("/public/")
            self._serve_file(PUBLIC_ROOT / relative)
            return

        if path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "buildforme",
                    "version": self.server_version,
                    "github_token_configured": bool(self._github().token),
                },
            )
            return
        if path == "/api/tasks":
            self._json(HTTPStatus.OK, {"tasks": self._store().list_tasks()})
            return
        if path == "/api/repos":
            self._json(HTTPStatus.OK, {"repositories": self._store().list_repos()})
            return
        if path == "/api/approvals":
            self._json(HTTPStatus.OK, {"approvals": self._store().list_approvals()})
            return
        if path == "/api/work-queue":
            self._work_queue(parsed)
            return
        if path == "/api/github/repo":
            self._github_repo(parsed)
            return
        if path == "/api/github/issues":
            self._github_issues(parsed)
            return
        if path == "/api/github/pr":
            self._github_pr(parsed)
            return
        if path.startswith("/api/pr/") and path.endswith("/status"):
            self._pr_status(path)
            return

        if self._try_serve_public_asset(path):
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/classify":
            self._classify(save=False)
            return
        if path == "/api/tasks":
            self._classify(save=True)
            return
        if path == "/api/decisions":
            self._record_decision()
            return
        if path == "/api/repos":
            self._add_repo()
            return
        if path == "/api/approvals":
            self._add_approval()
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/repos/"):
            encoded = path.removeprefix("/api/repos/")
            repository = urllib.parse.unquote(encoded)
            try:
                repos = self._store().remove_repo(repository)
                self._json(HTTPStatus.OK, {"repositories": repos, "removed": repository})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/repos":
            repository = _first_query_value(parsed, "repository")
            if not repository:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "repository query parameter is required"})
                return
            try:
                repos = self._store().remove_repo(repository)
                self._json(HTTPStatus.OK, {"repositories": repos, "removed": repository})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _classify(self, save: bool) -> None:
        try:
            task = self._read_json()
            if not isinstance(task, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "task packet must be a JSON object"})
                return
            problems = validate_task_packet(task)
            classification = classify_task(task).to_dict()
            payload: dict[str, Any] = {
                "valid": not problems,
                "validation_problems": problems,
                "classification": classification,
            }
            if save:
                payload["record"] = self._store().upsert_task(task, classification)
            self._json(HTTPStatus.OK, payload)
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _record_decision(self) -> None:
        try:
            payload = self._read_json()
            task_id = str(payload.get("task_id", "")).strip()
            decision = payload.get("decision")
            if not task_id or not isinstance(decision, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "task_id and decision object are required"})
                return
            record = self._store().set_decision(task_id, decision)
            self._json(HTTPStatus.OK, {"record": record})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})

    def _add_repo(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            repository = str(payload.get("repository") or "").strip()
            if not repository:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "repository is required"})
                return
            repos = self._store().add_repo(repository)
            self._json(HTTPStatus.OK, {"repositories": repos, "added": repository})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _add_approval(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            record = self._store().add_approval(payload)
            self._json(
                HTTPStatus.OK,
                {
                    "record": record,
                    "note": "Local Buildforme decision only — not a GitHub review or merge.",
                },
            )
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _work_queue(self, parsed: urllib.parse.ParseResult) -> None:
        repos_param = _first_query_value(parsed, "repos")
        repos: list[str] | None = None
        if repos_param:
            repos = [part.strip() for part in repos_param.split(",") if part.strip()]
        try:
            payload = build_work_queue(self._store(), self._github(), repos=repos)
            self._json(HTTPStatus.OK, payload)
        except Exception as exc:  # noqa: BLE001 — never crash the supervisor process
            self._json(
                HTTPStatus.OK,
                {
                    "repos": [],
                    "watched_repositories": repos or [],
                    "summary": {
                        "open_prs": 0,
                        "open_issues": 0,
                        "ci_failures": 0,
                        "blocked": 0,
                        "ready_for_review": 0,
                        "safe_next_tasks": 0,
                    },
                    "pull_requests": [],
                    "issues": [],
                    "recommended_next_task": {
                        "priority": 7,
                        "headline": "Work queue unavailable",
                        "detail": str(exc),
                        "recommended_action": "Retry refresh or check GitHub connectivity.",
                    },
                    "errors": [{"error": str(exc)}],
                    "github_token_configured": bool(self._github().token),
                },
            )

    def _pr_status(self, path: str) -> None:
        # /api/pr/{owner}/{repo}/{number}/status
        parts = path.strip("/").split("/")
        if len(parts) != 6 or parts[0] != "api" or parts[1] != "pr" or parts[5] != "status":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        owner, repo_name, number_raw = parts[2], parts[3], parts[4]
        repository = f"{owner}/{repo_name}"
        try:
            number = int(number_raw)
            payload = build_pr_status(self._github(), self._store(), repository, number)
            self._json(HTTPStatus.OK, payload)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except GitHubClientError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _github_repo(self, parsed: urllib.parse.ParseResult) -> None:
        repository = _first_query_value(parsed, "repository")
        if not repository:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "repository query parameter is required"})
            return
        try:
            self._json(HTTPStatus.OK, {"repo": self._github().get_repo(repository)})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _github_issues(self, parsed: urllib.parse.ParseResult) -> None:
        repository = _first_query_value(parsed, "repository")
        state = _first_query_value(parsed, "state") or "open"
        limit = _safe_int(_first_query_value(parsed, "limit"), default=20, maximum=50)
        if not repository:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "repository query parameter is required"})
            return
        try:
            issues = self._github().list_issues(repository, state=state, limit=limit)
            self._json(HTTPStatus.OK, {"issues": issues})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _github_pr(self, parsed: urllib.parse.ParseResult) -> None:
        repository = _first_query_value(parsed, "repository")
        number = _first_query_value(parsed, "number")
        if not repository or not number:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "repository and number query parameters are required"},
            )
            return
        try:
            pr_number = int(number)
            pr = self._github().get_pull_request(repository, pr_number)
            files = self._github().list_pull_request_files(repository, pr_number)
            self._json(HTTPStatus.OK, {"pull_request": pr, "files": files})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _try_serve_public_asset(self, request_path: str) -> bool:
        relative = request_path.lstrip("/")
        if not relative or relative.startswith("api/") or ".." in relative.split("/"):
            return False
        candidate = (PUBLIC_ROOT / relative).resolve()
        public_root = PUBLIC_ROOT.resolve()
        try:
            candidate.relative_to(public_root)
        except ValueError:
            return False
        if not candidate.is_file():
            return False
        self._serve_file(candidate)
        return True

    def _serve_file(self, path: Path) -> None:
        resolved = path.resolve()
        public_root = PUBLIC_ROOT.resolve()
        if public_root not in resolved.parents and resolved != (public_root / "index.html"):
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not resolved.exists() or not resolved.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        content_type = _content_type_for(resolved)
        body = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "null")

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _store(self) -> LocalStore:
        state_path = getattr(self.server, "state_path", DEFAULT_STATE_PATH)  # type: ignore[attr-defined]
        return LocalStore(state_path)

    def _github(self) -> GitHubClient:
        return GitHubClient.from_env()


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    explicit = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }
    if suffix in explicit:
        return explicit[suffix]
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


def _first_query_value(parsed: urllib.parse.ParseResult, key: str) -> str | None:
    values = urllib.parse.parse_qs(parsed.query).get(key)
    if not values:
        return None
    return values[0]


def _safe_int(value: str | None, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return min(max(parsed, 1), maximum)


def run(host: str = "127.0.0.1", port: int = 8787, state_path: str | Path = DEFAULT_STATE_PATH) -> None:
    server = ThreadingHTTPServer((host, port), BuildformeRequestHandler)
    server.state_path = Path(state_path)  # type: ignore[attr-defined]
    print(f"Buildforme running at http://{host}:{port}")
    print(f"State file: {Path(state_path)}")
    print("GitHub access: read-only (no merge, labels, comments, or PR writes)")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Buildforme supervisor server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    args = parser.parse_args(argv)
    run(host=args.host, port=args.port, state_path=args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
