import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from buildforme.server import BuildformeRequestHandler


class ServerTests(unittest.TestCase):
    def test_health_and_classify_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = ThreadingHTTPServer(("127.0.0.1", 0), BuildformeRequestHandler)
            server.state_path = Path(temp_dir) / "state.json"  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base_url + "/api/health", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
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
                request = urllib.request.Request(
                    base_url + "/api/tasks",
                    data=json.dumps(task).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["classification"]["risk"], "GREEN")
                    self.assertEqual(payload["record"]["task"]["task_id"], "BF-SERVER")

                with urllib.request.urlopen(base_url + "/api/tasks", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(len(payload["tasks"]), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
