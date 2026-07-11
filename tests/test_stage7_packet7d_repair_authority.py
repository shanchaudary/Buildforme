from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from buildforme.db import SCHEMA_VERSION, dumps
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.repair_contracts import build_repair_packet_record
from buildforme.repair_service import create_governed_repair_packet
from buildforme.review_contracts import (
    aggregate_review_reports,
    build_review_cycle_record,
    build_review_report_record,
)
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


class Stage7RepairAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.upsert_project(
            {
                "id": "project-repair",
                "name": "Repair",
                "repository": "shanchaudary/Buildforme",
                "status": "active",
                "local_repository_root": self.temp.name,
            }
        )
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet(
            {
                "id": "pkt-repair",
                "objective": "Repair reviewed implementation",
                "acceptance_criteria": ["all blocking findings resolved"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/repair",
                "allowed_files": ["app.py"],
                "forbidden_files": [".env"],
            }
        )
        lease = engine.issue_run_lease(
            run_id="run-repair-source",
            provider_id="glm",
            packet_id=packet["id"],
            actor="test",
        )
        self.store.save_constitution_lease(lease)
        run = {
            "id": "run-repair-source",
            "project_id": "project-repair",
            "provider_id": "glm",
            "repository": "shanchaudary/Buildforme",
            "repository_local_path": self.temp.name,
            "baseline_ref": "HEAD",
            "baseline_commit": "a" * 40,
            "requested_target_branch": "feature/repair",
            "execution_branch": "feature/repair-source",
            "target_branch": "feature/repair-source",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "packet_id": packet["id"],
            "packet": packet,
            "attempt": 0,
            "max_attempts": 2,
            "timeout_minutes": 30,
            "budget": {"max_cost_usd": 0},
            "review": {"hard_blocks": []},
            "worktree_path": self.temp.name,
            "evidence_ids": [],
        }
        run = engine.attach_to_run(run, lease=lease, actor="test")
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
        self.run = self.store.save_run_for_setup(run)
        evidence = build_evidence_bundle(
            run=self.run,
            packet=packet,
            process_result={"ok": True, "exit_code": 0, "pid": 1, "cleanup_ok": True},
            worktree={"worktree_path": self.temp.name, "baseline_commit": "a" * 40, "head_commit": "b" * 40, "branch": "feature/repair-source"},
            diff={
                "manifest": {
                    "files": [{"path": "app.py", "content_hash": "c" * 64}],
                    "files_changed": ["app.py"],
                    "manifest_fingerprint": "d" * 64,
                    "complete": True,
                },
                "patch_fingerprint": "e" * 64,
            },
            provider_health={"version": "test", "executable": "glm"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha="a" * 40,
            final_head_sha="b" * 40,
            execution_branch="feature/repair-source",
            patch_fingerprint="e" * 64,
            manifest_fingerprint="d" * 64,
        )
        self.evidence = self.store.save_run_evidence(evidence)
        for provider_id in ("codex", "claude", "glm"):
            self.store.set_provider_constitution_ack(
                provider_id,
                {
                    "constitution_supported": True,
                    "constitution_acknowledged": True,
                    "constitution_version": engine.version(),
                    "constitution_hash": engine.content_hash(),
                    "constitution_last_refresh": "now",
                    "constitution_acknowledged_at": "now",
                    "constitution_ack_actor": "test",
                },
            )
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=[
                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "claude-reviewer", "provider_id": "claude", "role": "security"},
            ],
            actor="shan",
        )
        created = self.store.create_review_cycle_atomic(cycle=cycle, assignments=assignments, actor="shan")
        self.cycle = created["cycle"]
        reports = []
        findings = []
        report_by_assignment = {}
        for index, assignment in enumerate(created["assignments"]):
            assignment = dict(assignment)
            assignment["status"] = "pending"
            payload = (
                {
                    "verdict": "changes_required",
                    "summary": "repair required",
                    "findings": [
                        {
                            "severity": "high",
                            "category": "governance",
                            "summary": "blocking authority defect",
                            "evidence": "app.py exact path",
                            "recommendation": "repair authority handling",
                        }
                    ],
                }
                if index == 0
                else {"verdict": "pass", "summary": "otherwise clear", "findings": []}
            )
            report, report_findings = build_review_report_record(
                cycle=self.cycle,
                assignment=assignment,
                payload=payload,
            )
            reports.append(report)
            report_by_assignment[assignment["assignment_id"]] = report["report_id"]
            findings.extend(report_findings)
        submitted_assignments = []
        for assignment in created["assignments"]:
            item = dict(assignment)
            item["status"] = "submitted"
            submitted_assignments.append(item)
        aggregate = aggregate_review_reports(
            cycle=self.cycle,
            assignments=submitted_assignments,
            reports=reports,
        )
        self.assertEqual(aggregate["status"], "repair_required")
        with self.store.s6.db.transaction() as conn:
            for assignment, report in zip(created["assignments"], reports):
                conn.execute(
                    "UPDATE review_assignments SET status='submitted', submitted_at='now', payload_json=? WHERE id=?",
                    (dumps({**assignment, "status": "submitted", "submitted_at": "now", "report_id": report["report_id"]}), assignment["assignment_id"]),
                )
                conn.execute(
                    "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
                    (report["report_id"], self.cycle["cycle_id"], assignment["assignment_id"], report["verdict"], report["report_fingerprint"], dumps(report), report["created_at"]),
                )
            for finding in findings:
                conn.execute(
                    "INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (finding["finding_id"], report_by_assignment[finding["assignment_id"]], self.cycle["cycle_id"], finding["assignment_id"], finding["severity"], finding["category"], 1 if finding["blocking"] else 0, finding["finding_fingerprint"], dumps(finding), "now"),
                )
            cycle_payload = dict(self.cycle)
            cycle_payload["status"] = "repair_required"
            cycle_payload["aggregate"] = aggregate
            conn.execute(
                "UPDATE review_cycles SET status='repair_required', aggregate_json=?, payload_json=?, finalized_at='now' WHERE id=?",
                (dumps(aggregate), dumps(cycle_payload), self.cycle["cycle_id"]),
            )
            run_row = conn.execute("SELECT row_version, payload_json FROM runs WHERE id=?", (self.run["id"],)).fetchone()
            run_payload = __import__("json").loads(run_row[1])
            run_payload["stage7_review_cycle_id"] = self.cycle["cycle_id"]
            run_payload["stage7_review_required"] = True
            run_payload["independent_review"] = {"cycle_id": self.cycle["cycle_id"], "status": "repair_required", "aggregate_fingerprint": aggregate["aggregate_fingerprint"]}
            run_payload["row_version"] = int(run_row[0]) + 1
            conn.execute("UPDATE runs SET payload_json=?, row_version=? WHERE id=?", (dumps(run_payload), int(run_row[0]) + 1, self.run["id"]))
        self.cycle = self.store.get_review_cycle(self.cycle["cycle_id"])

    def test_schema_v6(self):
        self.assertEqual(SCHEMA_VERSION, 7)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 7)

    def test_create_packet_binds_every_blocking_authority(self):
        packet = create_governed_repair_packet(
            self.store,
            self.cycle["cycle_id"],
            repair_provider_id="glm",
            actor="shan",
        )
        self.assertEqual(packet["source_cycle_id"], self.cycle["cycle_id"])
        self.assertEqual(packet["repair_provider_id"], "glm")
        self.assertEqual(packet["allowed_files"], ["app.py"])
        self.assertEqual(len(packet["source_blocking_findings"]), 1)
        self.assertTrue(packet["repair_scope_expansion_forbidden"])
        saved_run = self.store.get_run(self.run["id"])
        self.assertEqual(saved_run["stage7_repair_packet_id"], packet["repair_packet_id"])
        self.assertEqual(saved_run["stage7_repair_status"], "packet_ready")

    def test_storage_rejects_finding_omission_and_scope_expansion(self):
        cycle = self.store.get_review_cycle(self.cycle["cycle_id"])
        run = self.store.get_run(self.run["id"])
        reports = self.store.list_review_reports(self.cycle["cycle_id"])
        findings = self.store.list_review_findings(self.cycle["cycle_id"])
        ack = self.store.s6.get_provider_ack("glm") or {}
        packet = build_repair_packet_record(
            cycle=cycle,
            run=run,
            evidence=self.evidence,
            reports=reports,
            findings=findings,
            repair_provider_id="glm",
            actor="shan",
            provider_ack=ack,
        )
        omitted = dict(packet)
        omitted["source_blocking_findings"] = []
        with self.assertRaisesRegex(ValueError, "blocking|mismatch"):
            self.store.create_repair_packet_atomic(packet=omitted, actor="shan")
        expanded = dict(packet)
        expanded["allowed_files"] = ["app.py", "secrets.py"]
        with self.assertRaisesRegex(ValueError, "allowed_files mismatch"):
            self.store.create_repair_packet_atomic(packet=expanded, actor="shan")

    def test_source_reviewer_cannot_be_repair_provider(self):
        with self.assertRaisesRegex(ValueError, "reviewer provider"):
            create_governed_repair_packet(
                self.store,
                self.cycle["cycle_id"],
                repair_provider_id="codex",
                actor="shan",
            )

    def test_one_packet_per_source_cycle_append_only(self):
        first = create_governed_repair_packet(
            self.store,
            self.cycle["cycle_id"],
            repair_provider_id="glm",
            actor="shan",
        )
        replay = self.store.create_repair_packet_atomic(packet=dict(first), actor="shan")
        self.assertEqual(replay, first)
        changed = dict(first)
        changed["repair_acceptance_criteria"] = ["different"]
        with self.assertRaisesRegex(ValueError, "append-only|mismatch"):
            self.store.create_repair_packet_atomic(packet=changed, actor="shan")

    def test_non_repair_cycle_is_rejected(self):
        with self.store.s6.db.transaction() as conn:
            conn.execute("UPDATE review_cycles SET status='clear' WHERE id=?", (self.cycle["cycle_id"],))
        with self.assertRaisesRegex(ValueError, "repair_required"):
            create_governed_repair_packet(
                self.store,
                self.cycle["cycle_id"],
                repair_provider_id="glm",
                actor="shan",
            )


if __name__ == "__main__":
    unittest.main()
