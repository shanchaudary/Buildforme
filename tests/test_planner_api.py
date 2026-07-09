import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from buildforme.server import BuildformeRequestHandler


def _start(temp_dir: str):
    server = ThreadingHTTPServer(("127.0.0.1", 0), BuildformeRequestHandler)
    server.state_path = Path(temp_dir) / "state.json"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def _req(url, payload=None, method="GET"):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class PlannerApiTests(unittest.TestCase):
    def test_sample_project_plan_and_packet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, thread, base = _start(temp_dir)
            try:
                status, payload = _req(base + "/api/projects/sample", {"replace": True}, method="POST")
                self.assertEqual(status, 200)
                self.assertEqual(payload["project"]["id"], "buildforme")

                status, payload = _req(base + "/api/projects")
                self.assertTrue(any(p["id"] == "buildforme" for p in payload["projects"]))

                status, payload = _req(
                    base + "/api/projects/buildforme/plan/refresh",
                    {},
                    method="POST",
                )
                self.assertEqual(status, 200)
                plan = payload["plan"]
                self.assertIn("primary_recommendation", plan)
                self.assertIn("ranked_recommendations", plan)
                self.assertIn("blockers", plan)

                # pick a packet-capable recommendation if present
                target = None
                for rec in plan.get("ranked_recommendations") or []:
                    if rec.get("can_generate_packet") and rec.get("target_id"):
                        target = rec["target_id"]
                        break
                if target:
                    status, payload = _req(
                        base + f"/api/projects/buildforme/recommendation/{target}/packet",
                        {},
                        method="POST",
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("markdown", payload["packet"])

                status, payload = _req(base + "/api/briefing/generate", {}, method="POST")
                self.assertEqual(status, 200)
                self.assertIn("summary", payload["briefing"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
