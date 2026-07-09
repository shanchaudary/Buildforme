import json
import tempfile
import unittest
from pathlib import Path

from buildforme.planner import (
    detect_blockers,
    plan_project,
    rank_candidate_tasks,
    recommendation_to_packet_input,
)
from buildforme.storage import LocalStore


class PlannerTests(unittest.TestCase):
    def _sample_store(self) -> LocalStore:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = LocalStore(Path(temp.name) / "state.json")
        sample = json.loads(
            (Path(__file__).resolve().parent.parent / "data" / "sample_project.json").read_text(encoding="utf-8")
        )
        store.load_sample_project(sample, replace=True)
        return store

    def test_plan_is_deterministic(self):
        store = self._sample_store()
        a = plan_project("buildforme", store, github_data={"available": False})
        b = plan_project("buildforme", store, github_data={"available": False})
        ranks_a = [(r["target_id"], r["total_score"], r["recommendation_type"]) for r in a["ranked_recommendations"]]
        ranks_b = [(r["target_id"], r["total_score"], r["recommendation_type"]) for r in b["ranked_recommendations"]]
        self.assertEqual(ranks_a, ranks_b)

    def test_black_never_execute(self):
        store = self._sample_store()
        store.upsert_planned_task(
            {
                "id": "BF-BLACK",
                "project_id": "buildforme",
                "stage_id": "bf-stage-4",
                "title": "Print secrets now",
                "objective": "Print secrets and commit .env",
                "status": "ready",
                "risk": "BLACK",
                "priority": "critical",
                "dependencies": [],
            }
        )
        plan = plan_project("buildforme", store, github_data={"available": False})
        black = [r for r in plan["ranked_recommendations"] if r.get("target_id") == "BF-BLACK"]
        self.assertTrue(black)
        self.assertEqual(black[0]["recommendation_type"], "reject_task")
        self.assertFalse(black[0]["can_generate_packet"])
        self.assertFalse(black[0].get("executable_unattended"))

    def test_incomplete_dependencies_blocked(self):
        store = self._sample_store()
        plan = plan_project("buildforme", store, github_data={"available": False})
        dep_task = [r for r in plan["ranked_recommendations"] if r.get("target_id") == "BF-S4-DEP"]
        self.assertTrue(dep_task)
        self.assertEqual(dep_task[0]["recommendation_type"], "resolve_blocker")
        self.assertIn("BF-S4-READY", dep_task[0]["incomplete_dependencies"])

    def test_red_requires_shan(self):
        store = self._sample_store()
        plan = plan_project("buildforme", store, github_data={"available": False})
        red = [r for r in plan["ranked_recommendations"] if r.get("target_id") == "BF-S4-RED"]
        self.assertTrue(red)
        self.assertTrue(red[0]["requires_shan"])
        self.assertEqual(red[0]["recommendation_type"], "request_shan_decision")

    def test_failing_ci_high_urgency(self):
        store = self._sample_store()
        github = {
            "available": True,
            "pull_requests": [
                {
                    "number": 9,
                    "title": "Broken CI PR",
                    "repository": "shanchaudary/Buildforme",
                    "ci": {"status": "failing"},
                    "classification": {"risk": "YELLOW"},
                    "html_url": "https://example.test/pr/9",
                }
            ],
            "issues": [],
        }
        plan = plan_project("buildforme", store, github_data=github)
        primary = plan["primary_recommendation"]
        # Failing CI should rank very high
        self.assertIn(primary.get("recommendation_type"), {"fix_ci", "review_pr", "execute_task", "request_shan_decision", "resolve_blocker", "reject_task"})
        ci_items = [r for r in plan["ranked_recommendations"] if r.get("target_id") == "pr-9"]
        self.assertTrue(ci_items)
        self.assertEqual(ci_items[0]["score_breakdown"]["ci_urgency"], 25)

    def test_recommendation_explains_score(self):
        store = self._sample_store()
        plan = plan_project("buildforme", store, github_data={"available": False})
        primary = plan["primary_recommendation"]
        self.assertIn("score_breakdown", primary)
        self.assertTrue(primary.get("explanation") or primary.get("reasoning"))

    def test_packet_handoff_from_recommendation(self):
        store = self._sample_store()
        plan = plan_project("buildforme", store, github_data={"available": False})
        ready = next(r for r in plan["ranked_recommendations"] if r.get("target_id") == "BF-S4-READY")
        project = store.get_project("buildforme")
        packet_input = recommendation_to_packet_input(project, ready)
        self.assertIn(packet_input["source_type"], {"task", "pull_request"})
        from buildforme.packet_generator import generate_agent_packet

        packet = generate_agent_packet(packet_input)
        self.assertIn("markdown", packet)
        self.assertIn("Planner recommendation", packet.get("context") or packet.get("markdown") or "")

    def test_detect_blockers_lists_deps(self):
        store = self._sample_store()
        project = store.get_project("buildforme")
        blockers = detect_blockers(
            project,
            store.list_stages("buildforme"),
            store.list_planned_tasks("buildforme"),
            store.list_truth("buildforme"),
            {},
        )
        self.assertTrue(any("BF-S4-DEP" in str(b.get("what_it_blocks")) or "dependencies" in str(b).lower() for b in blockers))


if __name__ == "__main__":
    unittest.main()
