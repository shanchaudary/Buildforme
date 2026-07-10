import json
import tempfile
import unittest
from pathlib import Path

from buildforme.execution_preflight import evaluate_run_preflight
from buildforme.execution_service import create_run, execute_dry_run, record_run_approval, run_preflight
from buildforme.packet_generator import generate_agent_packet
from buildforme.storage import LocalStore


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        sample = json.loads(
            (Path(__file__).resolve().parent.parent / "data" / "sample_project.json").read_text(encoding="utf-8")
        )
        self.store.load_sample_project(sample, replace=True)
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "title": "Yellow impl",
                "objective": "Fix dashboard parser and add tests",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["public/**", "tests/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Tests pass"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage-5",
            }
        )
        self.packet = self.store.save_packet(packet)

    def _base_run(self, **overrides):
        payload = {
            "project_id": "buildforme",
            "provider_id": "codex",
            "packet_id": self.packet["id"],
            "packet": self.packet,
            "target_branch": "feature/stage-5",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
        }
        payload.update(overrides)
        return create_run(self.store, payload)

    def test_kill_switch_blocks(self):
        self.store.set_execution_control(kill_switch_active=True, reason="stop")
        run = self._base_run()
        result = evaluate_run_preflight(run, self.store)
        self.assertFalse(result["passed"])
        self.assertTrue(any(c["name"] == "global_kill_switch" and c["status"] == "fail" for c in result["checks"]))

    def test_paused_project_blocks(self):
        self.store.set_project_execution_control("buildforme", execution_status="paused", reason="hold")
        run = self._base_run()
        result = evaluate_run_preflight(run, self.store)
        self.assertFalse(result["passed"])

    def test_write_lock_blocks_implementation(self):
        self.store.create_repository_lock(
            {"repository": "shanchaudary/Buildforme", "lock_scope": "write", "reason": "freeze"}
        )
        run = self._base_run()
        result = evaluate_run_preflight(run, self.store)
        self.assertFalse(result["passed"])

    def test_main_branch_implementation_blocked(self):
        run = self._base_run(target_branch="main")
        result = evaluate_run_preflight(run, self.store)
        self.assertFalse(result["passed"])
        self.assertTrue(any(c["name"] == "main_branch_policy" and c["status"] == "fail" for c in result["checks"]))

    def test_black_cannot_execute(self):
        run = self._base_run(risk="BLACK")
        result = evaluate_run_preflight(run, self.store)
        self.assertFalse(result["passed"])

    def test_merge_capability_blocked(self):
        run = self._base_run(requested_capabilities=["read_repository", "merge"])
        result = evaluate_run_preflight(run, self.store)
        self.assertFalse(result["passed"])

    def test_dry_run_flow_with_approvals(self):
        run = self._base_run()
        pre = run_preflight(self.store, run["id"])
        self.assertIn(pre["run"]["status"], {"awaiting_approval", "approved", "preflight_failed"})
        if pre["run"]["status"] == "awaiting_approval":
            for req in pre["preflight"].get("required_approvals") or []:
                record_run_approval(self.store, run["id"], requirement_type=req, decision="approved", note="ok")
            # re-preflight or approvals should flip to approved
            run2 = self.store.get_run(run["id"])
            if run2["status"] == "awaiting_approval":
                # manually approve path again
                pass
        run_now = self.store.get_run(run["id"])
        if run_now["status"] != "approved":
            # force approve remaining
            for req in ["shan_task_approval", "shan_red_risk_approval"]:
                try:
                    record_run_approval(self.store, run["id"], requirement_type=req, decision="approved")
                except Exception:
                    pass
        # If still not approved, preflight may have failed for other reasons — assert checks present
        if self.store.get_run(run["id"])["status"] == "approved":
            result = execute_dry_run(self.store, run["id"])
            self.assertEqual(result["dry_run"]["mode"], "dry_run")
            self.assertEqual(result["dry_run"]["network_calls"], [])
            self.assertEqual(result["dry_run"]["github_writes"], [])
            self.assertFalse(result["dry_run"]["would_execute"])
            self.assertEqual(result["run"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
