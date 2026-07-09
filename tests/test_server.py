import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from buildforme.server import BuildformeRequestHandler


def _start_server(temp_dir: str) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BuildformeRequestHandler)
    server.state_path = Path(temp_dir) / "state.json"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base_url


def _json_request(url: str, payload: dict | None = None, method: str = "GET") -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return exc.code, body


class ServerTests(unittest.TestCase):
    def test_health_and_classify_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, thread, base_url = _start_server(temp_dir)
            try:
                status, payload = _json_request(base_url + "/api/health")
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], "ok")

                task = {
                    "task_id": "BF-SERVER",
                    "objective": "Read-only audit",
                    "operating_mode": "READ_ONLY_AUDIT",
                    "allowed_files": ["docs/**"],
                    "forbidden_files": [".env"],
                    "acceptance_criteria": ["Report findings"],
                    "data_mutation_allowed": False,
                }
                status, payload = _json_request(base_url + "/api/tasks", task, method="POST")
                self.assertEqual(status, 200)
                self.assertEqual(payload["classification"]["risk"], "GREEN")
                self.assertEqual(payload["record"]["task"]["task_id"], "BF-SERVER")

                status, payload = _json_request(base_url + "/api/tasks")
                self.assertEqual(status, 200)
                self.assertEqual(len(payload["tasks"]), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_repos_and_approvals_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, thread, base_url = _start_server(temp_dir)
            try:
                status, payload = _json_request(
                    base_url + "/api/repos",
                    {"repository": "shanchaudary/Buildforme"},
                    method="POST",
                )
                self.assertEqual(status, 200)
                self.assertIn("shanchaudary/Buildforme", payload["repositories"])

                status, payload = _json_request(base_url + "/api/repos")
                self.assertEqual(status, 200)
                self.assertEqual(payload["repositories"], ["shanchaudary/Buildforme"])

                status, payload = _json_request(
                    base_url + "/api/approvals",
                    {
                        "target_type": "pull_request",
                        "repository": "shanchaudary/Buildforme",
                        "number": 1,
                        "decision": "reviewed",
                        "note": "local only",
                    },
                    method="POST",
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["record"]["decision"], "reviewed")
                self.assertFalse(payload["record"]["github_write"])

                status, payload = _json_request(base_url + "/api/approvals")
                self.assertEqual(status, 200)
                self.assertEqual(len(payload["approvals"]), 1)

                status, payload = _json_request(
                    base_url + "/api/repos/" + urllib_quote("shanchaudary/Buildforme"),
                    method="DELETE",
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["repositories"], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_work_queue_handles_github_errors_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, thread, base_url = _start_server(temp_dir)
            try:
                with patch("buildforme.server.build_work_queue") as mock_queue:
                    mock_queue.side_effect = RuntimeError("simulated failure")
                    status, payload = _json_request(base_url + "/api/work-queue")
                    self.assertEqual(status, 200)
                    self.assertIn("errors", payload)
                    self.assertEqual(payload["summary"]["open_prs"], 0)

                # Also exercise success-shaped empty path via mocked builder
                with patch("buildforme.server.build_work_queue") as mock_queue:
                    mock_queue.return_value = {
                        "repos": [],
                        "watched_repositories": ["shanchaudary/Buildforme"],
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
                        "recommended_next_task": {"priority": 7, "headline": "Create next"},
                        "errors": [{"repository": "x/y", "error": "rate limited"}],
                        "github_token_configured": False,
                    }
                    status, payload = _json_request(base_url + "/api/work-queue?repos=x/y")
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["errors"][0]["error"], "rate limited")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_dashboard_static_assets_are_served(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, thread, base_url = _start_server(temp_dir)
            try:
                with urllib.request.urlopen(base_url + "/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                    self.assertIn("Buildforme", html)
                    self.assertIn("/public/styles.css", html)
                    self.assertIn("Work queue", html)
                    self.assertIn("Approvals", html)

                with urllib.request.urlopen(base_url + "/public/styles.css", timeout=5) as response:
                    css = response.read().decode("utf-8", errors="replace")
                    self.assertIn("--bg", css)
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))

                with urllib.request.urlopen(base_url + "/styles.css", timeout=5) as response:
                    css = response.read().decode("utf-8", errors="replace")
                    self.assertIn(".sidebar", css)

                with urllib.request.urlopen(base_url + "/public/app.js", timeout=5) as response:
                    js = response.read().decode("utf-8")
                    self.assertIn("work-queue", js.lower().replace("_", "-") or "refreshWorkQueue")
                    self.assertIn("refreshWorkQueue", js)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def urllib_quote(value: str) -> str:
    return urllib.request.quote(value, safe="")


if __name__ == "__main__":
    unittest.main()
