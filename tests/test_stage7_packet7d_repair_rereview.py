from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.db import SCHEMA_VERSION, dumps
from buildforme.evidence import build_evidence_bundle
from buildforme.repair_service import (
    admit_governed_repair_run,
    create_repair_review_cycle,
    execute_governed_repair_and_open_review,
)
from buildforme.review_contracts import build_review_cycle_record
from buildforme.review_service import require_clear_independent_review
from test_stage7_packet7d_repair_admission import Stage7RepairAdmissionTests


class Stage7RepairRereviewTests(unittest.TestCase):
    def setUp(self):
        fixture = Stage7RepairAdmissionTests(methodName="test_schema_v7")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.store = fixture.store
        self.repo = fixture.repo
        self.packet = fixture.repair_packet
        self.admitted = admit_governed_repair_run(
            self.store, self.packet["repair_packet_id"], actor="shan"
        )
        self.child = self.admitted["run"]

    def _finish_child_with_fresh_evidence(self, *, verification_passed: bool = True):
        child = self.store.get_run(self.child["id"])
        manifest = collect_changed_file_manifest(self.repo, baseline_commit=child["baseline_commit"])
        patch_ev = collect_patch_evidence(self.repo, baseline_commit=child["baseline_commit"])
        evidence = build_evidence_bundle(
            run=child,
            packet=child["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 22,
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": str(self.repo),
                "baseline_commit": child["execution_seed_commit"],
                "head_commit": child["baseline_commit"],
                "branch": child["execution_branch"],
            },
            diff={"manifest": manifest, "patch_fingerprint": patch_ev["patch_fingerprint"]},
            provider_health={"version": "test", "executable": child["provider_id"]},
            verification={
                "passed": verification_passed,
                "blocking_reasons": [] if verification_passed else ["failed"],
                "checks": [],
            },
            constitution_result={"passed": True},
            approved_baseline_sha=child["baseline_commit"],
            final_head_sha=child["baseline_commit"],
            execution_branch=child["execution_branch"],
            patch_fingerprint=patch_ev["patch_fingerprint"],
            manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        saved_evidence = self.store.save_run_evidence(evidence)
        with self.store.s6.db.transaction() as conn:
            row = conn.execute(
                "SELECT row_version, payload_json FROM runs WHERE id=?", (child["id"],)
            ).fetchone()
            payload = json.loads(row[1])
            payload["status"] = "needs_review"
            payload["verification"] = evidence["verification"]
            payload["worktree_path"] = str(self.repo)
            payload["evidence"] = {
                "evidence_id": saved_evidence["evidence_id"],
                "evidence_fingerprint": saved_evidence["evidence_fingerprint"],
            }
            payload["evidence_ids"] = [saved_evidence["evidence_id"]]
            payload["row_version"] = int(row[0]) + 1
            conn.execute(
                "UPDATE runs SET status='needs_review', payload_json=?, row_version=? WHERE id=?",
                (dumps(payload), payload["row_version"], child["id"]),
            )
        self.child = self.store.get_run(child["id"])
        return saved_evidence

    def test_schema_v8(self):
        self.assertEqual(SCHEMA_VERSION, 8)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 8)

    def test_repair_child_is_founder_blocked_before_fresh_cycle(self):
        child = self.store.get_run(self.child["id"])
        self.assertTrue(child["stage7_review_required"])
        with self.assertRaisesRegex(ValueError, "clear Stage 7"):
            require_clear_independent_review(self.store, child)

    def test_fresh_repair_review_reuses_exact_source_provider_set(self):
        fresh = self._finish_child_with_fresh_evidence()
        result = create_repair_review_cycle(
            self.store, self.packet["repair_packet_id"], actor="shan"
        )
        providers = {item["provider_id"] for item in result["assignments"]}
        self.assertEqual(providers, {"codex", "claude"})
        self.assertNotIn(self.child["provider_id"], providers)
        link = result["repair_review_link"]
        self.assertEqual(link["fresh_evidence_id"], fresh["evidence_id"])
        self.assertNotEqual(link["fresh_evidence_id"], self.packet["source_evidence_id"])
        saved_child = self.store.get_run(self.child["id"])
        self.assertEqual(saved_child["stage7_review_cycle_id"], result["cycle"]["cycle_id"])
        source = self.store.get_run(self.packet["source_run_id"])
        self.assertEqual(source["stage7_repair_status"], "re_review_collecting")

    def test_substituted_reviewer_provider_set_is_rejected_by_storage(self):
        self._finish_child_with_fresh_evidence()
        child = self.store.get_run(self.child["id"])
        evidence = self.store.get_latest_execution_evidence(child["id"])
        cycle, assignments = build_review_cycle_record(
            run=child,
            evidence=evidence,
            reviewers=[
                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "other-reviewer", "provider_id": "grok", "role": "security"},
            ],
            actor="shan",
        )
        with self.assertRaisesRegex(ValueError, "exactly reuse"):
            self.store.create_review_cycle_atomic(cycle=cycle, assignments=assignments, actor="shan")

    def test_failed_verification_cannot_open_repair_review(self):
        self._finish_child_with_fresh_evidence(verification_passed=False)
        with self.assertRaisesRegex(ValueError, "verification must pass"):
            create_repair_review_cycle(
                self.store, self.packet["repair_packet_id"], actor="shan"
            )

    def test_execute_orchestrator_requires_approved_child(self):
        with self.assertRaisesRegex(ValueError, "must be approved"):
            execute_governed_repair_and_open_review(
                self.store, self.packet["repair_packet_id"], actor="shan"
            )

    def test_repair_review_link_is_idempotent(self):
        self._finish_child_with_fresh_evidence()
        first = create_repair_review_cycle(self.store, self.packet["repair_packet_id"], actor="shan")
        second = create_repair_review_cycle(self.store, self.packet["repair_packet_id"], actor="shan")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["cycle"]["cycle_id"], second["cycle"]["cycle_id"])


if __name__ == "__main__":
    unittest.main()
