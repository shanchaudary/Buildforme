"""Local Buildforme supervisor server.

This is a dependency-free HTTP server for testing the MVP locally. It serves the
static dashboard and exposes small JSON APIs for policy classification, local
approval/task storage, and read-only GitHub inspection.
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ROOT = PROJECT_ROOT / "public"


class BuildformeRequestHandler(BaseHTTPRequestHandler):
    server_version = "BuildformeMVP/0.2"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/public"}:
            self._serve_file(PUBLIC_ROOT / "index.html")
            return
        if parsed.path.startswith("/public/"):
            relative = parsed.path.removeprefix("/public/")
            self._serve_file(PUBLIC_ROOT / relative)
            return
        if parsed.path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok", "service": "buildforme"})
            return
        if parsed.path == "/api/tasks":
            self._json(HTTPStatus.OK, {"tasks": self._store().list_tasks()})
            return
        if parsed.path == "/api/github/repo":
            self._github_repo(parsed)
            return
        if parsed.path == "/api/github/issues":
            self._github_issues(parsed)
            return
        if parsed.path == "/api/github/pr":
            self._github_pr(parsed)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/classify":
            self._classify(save=False)
            return
        if parsed.path == "/api/tasks":
            self._classify(save=True)
            return
        if parsed.path == "/api/decisions":
            self._record_decision()
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Keep local testing output concise. Request details are not suppressed on errors.
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
            self._json(HTTPStatus.BAD_REQUEST, {"error": "repository and number query parameters are required"})
            return
        try:
            pr_number = int(number)
            pr = self._github().get_pull_request(repository, pr_number)
            files = self._github().list_pull_request_files(repository, pr_number)
            self._json(HTTPStatus.OK, {"pull_request": pr, "files": files})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _serve_file(self, path: Path) -> None:
        resolved = path.resolve()
        if PUBLIC_ROOT.resolve() not in resolved.parents and resolved != (PUBLIC_ROOT / "index.html").resolve():
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not resolved.exists() or not resolved.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        body = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
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
