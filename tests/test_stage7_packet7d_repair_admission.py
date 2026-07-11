from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.db import SCHEMA_VERSION, dumps
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.repair_service import admit_governed_repair_run, create_governed_repair_packet
from buildforme.review_contracts import aggregate_review_reports, build_review_cycle_record, build_review_report_record
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


class Stage7RepairAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "repair@test.local")
        self._git("config", "user.name", "repair-test")
        self._git("remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "baseline")
        self.baseline = self._git_out("rev-parse", "HEAD")
        self._git("checkout", "-b", "feature/source-run")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.repo / "new.py").write_text("new_value = 3\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "runtime" / "state.json")
        self.store.upsert_project({
            "id": "repair-project", "name": "Repair", "repository": "shanchaudary/Buildforme",
            "status": "active", "local_repository_root": str(self.repo),
        })
        self.store.register_repository_binding({
            "repository": "shanchaudary/Buildforme", "local_path": str(self.repo), "project_id": "repair-project",
        })
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet({
            "id": "pkt-source", "objective": "Implement change", "target_repository": "shanchaudary/Buildforme",
            "target_branch": "feature/source", "operating_mode": "IMPLEMENTATION", "risk": "YELLOW",
            "allowed_files": ["app.py", "new.py"], "forbidden_files": [".env"],
            "acceptance_criteria": ["value updated"],
        })
        lease = engine.issue_run_lease(run_id="run-source", provider_id="glm", packet_id=packet["id"], actor="test")
        self.store.save_constitution_lease(lease)
        run = {
            "id": "run-source", "project_id": "repair-project", "task_id": "task-repair",
            "packet_id": packet["id"], "packet": packet, "provider_id": "glm",
            "repository": "shanchaudary/Buildforme", "repository_local_path": str(self.repo),
            "baseline_ref": self.baseline, "baseline_commit": self.baseline,
            "requested_target_branch": "feature/source", "execution_branch": "feature/source-run",
            "target_branch": "feature/source-run", "operating_mode": "IMPLEMENTATION", "risk": "YELLOW",
            "status": "needs_review", "execution_mode": "live_supervised", "mode": "live_supervised",
            "transport": "cli", "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "attempt": 0, "max_attempts": 2, "timeout_minutes": 30, "budget": {"max_cost_usd": 0},
            "review": {"hard_blocks": []}, "worktree_path": str(self.repo), "evidence_ids": [],
        }
        run = engine.attach_to_run(run, lease=lease, actor="test")
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
        self.run = self.store.save_run_for_setup(run)
        self.lock = self.store.create_task_lock({
            "task_key": "task-repair", "project_id": "repair-project", "run_id": self.run["id"], "reason": "source run",
        })
        with self.store.s6.db.transaction() as conn:
            row = conn.execute("SELECT row_version, payload_json FROM runs WHERE id=?", (self.run["id"],)).fetchone()
            payload = __import__("json").loads(row[1]); payload["task_lock_id"] = self.lock["id"]
            conn.execute("UPDATE runs SET task_lock_id=?, payload_json=?, row_version=? WHERE id=?", (self.lock["id"], dumps(payload), int(row[0]) + 1, self.run["id"]))
        self.run = self.store.get_run(self.run["id"])
        manifest = collect_changed_file_manifest(self.repo, baseline_commit=self.baseline)
        patch = collect_patch_evidence(self.repo, baseline_commit=self.baseline)
        evidence = build_evidence_bundle(
            run=self.run, packet=packet,
            process_result={"ok": True, "exit_code": 0, "pid": 11, "cleanup_ok": True, "process_group_isolated": True},
            worktree={"worktree_path": str(self.repo), "baseline_commit": self.baseline, "head_commit": self.baseline, "branch": "feature/source-run"},
            diff={"manifest": manifest, "patch_fingerprint": patch["patch_fingerprint"]},
            provider_health={"version": "test", "executable": "glm"},
            verification={"passed": True, "blocking_reasons": [], "checks": []}, constitution_result={"passed": True},
            approved_baseline_sha=self.baseline, final_head_sha=self.baseline, execution_branch="feature/source-run",
            patch_fingerprint=patch["patch_fingerprint"], manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        self.evidence = self.store.save_run_evidence(evidence)
        for provider_id in ("codex", "claude", "glm"):
            self.store.set_provider_constitution_ack(provider_id, {
                "constitution_supported": True, "constitution_acknowledged": True,
                "constitution_version": engine.version(), "constitution_hash": engine.content_hash(),
                "constitution_last_refresh": "now", "constitution_acknowledged_at": "now", "constitution_ack_actor": "test",
            })
        cycle, assignments = build_review_cycle_record(
            run=self.run, evidence=self.evidence,
            reviewers=[
                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "claude-reviewer", "provider_id": "claude", "role": "security"},
            ], actor="shan",
        )
        created = self.store.create_review_cycle_atomic(cycle=cycle, assignments=assignments, actor="shan")
        reports, all_findings, report_map = [], [], {}
        for index, assignment in enumerate(created["assignments"]):
            payload = ({
                "verdict": "changes_required", "summary": "repair", "findings": [{
                    "severity": "high", "category": "correctness", "summary": "wrong value",
                    "evidence": "app.py value", "recommendation": "set correct value",
                }],
            } if index == 0 else {"verdict": "pass", "summary": "clear", "findings": []})
            report, findings = build_review_report_record(cycle=created["cycle"], assignment={**assignment, "status": "pending"}, payload=payload)
            reports.append(report); all_findings.extend(findings); report_map[assignment["assignment_id"]] = report["report_id"]
        aggregate = aggregate_review_reports(
            cycle=created["cycle"], assignments=[{**a, "status": "submitted"} for a in created["assignments"]], reports=reports,
        )
        with self.store.s6.db.transaction() as conn:
            for assignment, report in zip(created["assignments"], reports):
                conn.execute("UPDATE review_assignments SET status='submitted', payload_json=? WHERE id=?", (dumps({**assignment, "status": "submitted", "report_id": report["report_id"]}), assignment["assignment_id"]))
                conn.execute("INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)", (report["report_id"], created["cycle"]["cycle_id"], assignment["assignment_id"], report["verdict"], report["report_fingerprint"], dumps(report), report["created_at"]))
            for finding in all_findings:
                conn.execute("INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)", (finding["finding_id"], report_map[finding["assignment_id"]], created["cycle"]["cycle_id"], finding["assignment_id"], finding["severity"], finding["category"], 1 if finding["blocking"] else 0, finding["finding_fingerprint"], dumps(finding), "now"))
            cycle_payload = dict(created["cycle"]); cycle_payload["status"] = "repair_required"; cycle_payload["aggregate"] = aggregate
            conn.execute("UPDATE review_cycles SET status='repair_required', aggregate_json=?, payload_json=?, finalized_at='now' WHERE id=?", (dumps(aggregate), dumps(cycle_payload), created["cycle"]["cycle_id"]))
            row = conn.execute("SELECT row_version, payload_json FROM runs WHERE id=?", (self.run["id"],)).fetchone()
            source = __import__("json").loads(row[1]); source["stage7_review_cycle_id"] = created["cycle"]["cycle_id"]; source["stage7_review_required"] = True; source["row_version"] = int(row[0]) + 1
            conn.execute("UPDATE runs SET payload_json=?, row_version=? WHERE id=?", (dumps(source), source["row_version"], self.run["id"]))
        self.cycle = self.store.get_review_cycle(created["cycle"]["cycle_id"])
        self.repair_packet = create_governed_repair_packet(self.store, self.cycle["cycle_id"], repair_provider_id="glm", actor="shan")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def _git_out(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def test_schema_v7(self):
        self.assertEqual(SCHEMA_VERSION, 7)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 7)

    def test_exact_dirty_state_seeded_without_mutating_source_branch(self):
        source_head = self._git_out("rev-parse", "HEAD")
        source_status = self._git_out("status", "--porcelain")
        result = admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        child = result["run"]; admission = result["admission"]
        self.assertEqual(self._git_out("rev-parse", "HEAD"), source_head)
        self.assertEqual(self._git_out("status", "--porcelain"), source_status)
        self.assertEqual(child["baseline_commit"], self.baseline)
        self.assertNotEqual(child["execution_seed_commit"], self.baseline)
        self.assertEqual(self._git_out("rev-parse", admission["seed_ref"]), admission["seed_commit"])
        self.assertEqual(child["scope_fingerprint"], compute_run_scope_fingerprint(child, child["packet"]))
        self.assertEqual(child["task_lock_id"], self.lock["id"])
        self.assertIsNone(result["source_run"].get("task_lock_id"))
        lock = next(item for item in self.store.list_task_locks(active_only=True) if item["id"] == self.lock["id"])
        self.assertEqual(lock["run_id"], child["id"])

    def test_source_drift_blocks_admission_without_child(self):
        (self.repo / "app.py").write_text("tampered = True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        with self.assertRaises(KeyError):
            self.store.get_repair_admission(self.repair_packet["repair_packet_id"])

    def test_admission_is_idempotent(self):
        first = admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        second = admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])

    def test_execution_service_uses_seed_but_verifies_original_baseline(self):
        source = Path("buildforme/execution_service.py").read_text(encoding="utf-8")
        self.assertIn('execution_seed = str(run.get("execution_seed_commit") or approved_baseline)', source)
        self.assertIn("baseline_commit=execution_seed", source)
        self.assertIn("baseline_commit=approved_baseline", source)


if __name__ == "__main__":
    unittest.main()
