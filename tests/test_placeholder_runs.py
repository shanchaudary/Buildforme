"""Fail-closed: no fabricated placeholder runs for missing run_id."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from buildforme.evidence import (
    build_evidence_bundle,
    build_founder_decision_evidence,
    compute_founder_decision_fingerprint,
)
from buildforme.storage import LocalStore


class MissingRunEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.save_run_for_setup(
            {
                "id": "run-real",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "owner/repo",
                "status": "draft",
                "execution_mode": "dry_run",
            }
        )

    def test_event_for_existing_run_persists(self):
        ev = self.store.append_run_event("run-real", "test_event", "ok", actor="system")
        self.assertEqual(ev["run_id"], "run-real")
        events = self.store.list_run_events("run-real")
        self.assertEqual(len(events), 1)

    def test_missing_run_event_raises_and_creates_nothing(self):
        with self.assertRaisesRegex(ValueError, "run not found"):
            self.store.append_run_event("run-missing", "ghost", "nope")
        with self.assertRaises(KeyError):
            self.store.get_run("run-missing")
        self.assertEqual(self.store.list_run_events("run-missing"), [])
        # No placeholder repositories
        for run in self.store.list_runs():
            self.assertNotEqual(run.get("repository"), "unknown/unknown")
            self.assertNotEqual(run.get("project_id"), "unknown")

    def test_empty_run_id_raises(self):
        with self.assertRaisesRegex(ValueError, "run_id required"):
            self.store.append_run_event("", "x", "y")

    def test_concurrent_missing_run_events_no_placeholder(self):
        errors: list[str] = []

        def worker(i: int) -> None:
            try:
                self.store.append_run_event(f"ghost-{i}", "x", "y")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 8)
        ids = {r.get("id") for r in self.store.list_runs()}
        self.assertEqual(ids, {"run-real"})


class MissingRunEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.save_run_for_setup(
            {
                "id": "run-ev",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "owner/repo",
                "status": "needs_review",
                "execution_mode": "live_supervised",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "L",
                "constitution_lease_fingerprint": "lf",
                "baseline_commit": "b" * 40,
            }
        )

    def _execution_bundle(self, run_id: str) -> dict:
        run = {
            "id": run_id,
            "project_id": "p",
            "provider_id": "codex",
            "repository": "owner/repo",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "L",
            "constitution_lease_fingerprint": "lf",
            "baseline_commit": "b" * 40,
            "execution_branch": "feature/x",
            "transport": "cli",
        }
        return build_evidence_bundle(
            run=run,
            packet={"id": "pk", "allowed_files": [], "forbidden_files": []},
            process_result={
                "exit_code": 0,
                "pid": 1,
                "timed_out": False,
                "cancelled": False,
                "cleanup_ok": True,
                "stdout": "ok",
                "stderr": "",
                "argv": ["x"],
            },
            worktree={
                "worktree_path": "/t",
                "head_commit": "h" * 40,
                "branch": "feature/x",
                "baseline_commit": "b" * 40,
            },
            diff={
                "manifest": {
                    "files_changed": ["a.py"],
                    "files": [{"path": "a.py"}],
                    "manifest_fingerprint": "m" * 64,
                    "complete": True,
                },
                "files_changed": ["a.py"],
            },
            provider_health={"version": "1"},
            verification={
                "passed": True,
                "blocking_reasons": [],
                "checks": [{"name": "tests", "status": "pass"}],
            },
            constitution_result={"passed": True, "problems": []},
            patch_fingerprint="p" * 64,
            final_head_sha="h" * 40,
            execution_branch="feature/x",
            approved_baseline_sha="b" * 40,
            manifest_fingerprint="m" * 64,
        )

    def test_evidence_for_existing_run_persists(self):
        bundle = self._execution_bundle("run-ev")
        saved = self.store.save_run_evidence(bundle)
        self.assertEqual(saved["run_id"], "run-ev")
        loaded = self.store.get_run_evidence("run-ev")
        self.assertEqual(loaded["evidence_id"], saved["evidence_id"])

    def test_missing_run_execution_evidence_raises(self):
        bundle = self._execution_bundle("run-nope")
        with self.assertRaisesRegex(ValueError, "run not found"):
            self.store.save_run_evidence(bundle)
        with self.assertRaises(KeyError):
            self.store.get_run("run-nope")
        self.assertEqual(self.store.list_run_evidence(run_id="run-nope"), [])

    def test_missing_run_founder_evidence_raises(self):
        # Parent execution on real run
        parent = self.store.save_run_evidence(self._execution_bundle("run-ev"))
        fd = build_founder_decision_evidence(
            run={
                "id": "run-missing-fd",
                "project_id": "p",
                "packet_id": "pk",
                "task_id": "t",
                "repository": "owner/repo",
                "scope_fingerprint": "s",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "L",
                "constitution_lease_fingerprint": "lf",
            },
            parent_evidence=parent,
            decision="accept_for_pr_prep",
            actor="shan",
            note="",
            review={"status": "accepted_for_pr_prep"},
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=1,
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        with self.assertRaisesRegex(ValueError, "run not found"):
            self.store.save_run_evidence(fd)
        with self.assertRaises(KeyError):
            self.store.get_run("run-missing-fd")

    def test_fingerprint_still_validated(self):
        bundle = self._execution_bundle("run-ev")
        bundle["evidence_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint|mismatch"):
            self.store.save_run_evidence(bundle)

    def test_append_only_intact(self):
        bundle = self._execution_bundle("run-ev")
        self.store.save_run_evidence(bundle)
        with self.assertRaisesRegex(ValueError, "append-only"):
            self.store.save_run_evidence(bundle)


class MigrationOrphanTests(unittest.TestCase):
    def test_orphan_event_detected_no_placeholder_cutover_withheld(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name)
        (runtime / "runs.json").write_text(
            json.dumps(
                {
                    "runs": [
                        {
                            "id": "run-ok",
                            "project_id": "p",
                            "provider_id": "codex",
                            "repository": "a/b",
                            "status": "draft",
                            "execution_mode": "dry_run",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (runtime / "run_events.json").write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "ev-orphan",
                            "run_id": "run-does-not-exist",
                            "event_type": "ghost",
                            "summary": "orphan",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (runtime / "run_evidence.json").write_text(
            json.dumps(
                {
                    "evidence": [
                        {
                            "evidence_id": "evid-orphan",
                            "run_id": "run-missing-ev",
                            "evidence_kind": "execution",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = LocalStore(runtime / "state.json")
        report = store.s6.migrate_from_json(runtime, dry_run=False, cutover=True)
        self.assertFalse(report.get("cutover"))
        self.assertTrue(report.get("orphans"))
        types = {o["record_type"] for o in report["orphans"]}
        self.assertIn("event", types)
        self.assertIn("evidence", types)
        # No placeholder runs
        for run in store.list_runs():
            self.assertNotEqual(run.get("repository"), "unknown/unknown")
            self.assertNotIn(run.get("id"), {"run-does-not-exist", "run-missing-ev"})
        self.assertEqual(store.list_run_events("run-does-not-exist"), [])
        # Valid run still imported
        store.get_run("run-ok")

    def test_valid_migration_still_succeeds(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name)
        (runtime / "runs.json").write_text(
            json.dumps(
                {
                    "runs": [
                        {
                            "id": "run-mig",
                            "project_id": "p",
                            "provider_id": "codex",
                            "repository": "a/b",
                            "status": "draft",
                            "execution_mode": "dry_run",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (runtime / "run_events.json").write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "ev1",
                            "run_id": "run-mig",
                            "event_type": "run_created",
                            "summary": "ok",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = LocalStore(runtime / "state.json")
        report = store.s6.migrate_from_json(runtime, dry_run=False, cutover=True)
        self.assertEqual(report["orphans"], [])
        self.assertTrue(report.get("cutover"))
        self.assertEqual(report["imported"].get("events"), 1)
        self.assertEqual(len(store.list_run_events("run-mig")), 1)


if __name__ == "__main__":
    unittest.main()
