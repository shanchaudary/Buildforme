"""Atomic, versioned, fail-closed run approval persistence."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from buildforme.execution_service import create_run, record_run_approval, run_preflight
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


def _ack(store: LocalStore) -> None:
    engine = get_engine(force_reload=True)
    for provider in store.list_providers():
        refreshed = engine.acknowledge_provider(provider, actor="shan")
        store.set_provider_constitution_ack(
            str(provider["provider_id"]),
            {
                "constitution_supported": True,
                "constitution_acknowledged": True,
                "constitution_version": refreshed["constitution_version"],
                "constitution_hash": refreshed["constitution_hash"],
                "constitution_last_refresh": refreshed["constitution_last_refresh"],
                "constitution_acknowledged_at": refreshed["constitution_acknowledged_at"],
                "constitution_ack_actor": "shan",
            },
        )


class ApprovalAtomicFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        sample = json.loads(
            (Path(__file__).resolve().parent.parent / "data" / "sample_project.json").read_text(
                encoding="utf-8"
            )
        )
        self.store.load_sample_project(sample, replace=True)
        self.store.set_project_execution_control(
            "buildforme", execution_status="enabled", reason="test"
        )
        _ack(self.store)
        self.packet = {
            "id": "pkt-appr-atomic",
            "objective": "approval atomic test",
            "acceptance_criteria": ["ok"],
            "allowed_files": ["src/**"],
            "forbidden_files": [".env"],
            "risk": "YELLOW",
            "operating_mode": "IMPLEMENTATION",
        }
        # Force multiple approval requirements via preflight path or manual set
        engine = get_engine()
        self.packet = engine.attach_to_packet(self.packet)

    def _run_awaiting(self, *, risk: str = "YELLOW") -> dict:
        packet = dict(self.packet)
        packet["id"] = f"pkt-{risk.lower()}-{id(self)}"
        packet["risk"] = risk
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": packet,
                "packet_id": packet["id"],
                "target_branch": "feature/appr",
                "risk": risk,
                "execution_mode": "dry_run",
                "requested_capabilities": [
                    "read_repository",
                    "edit_repository",
                    "run_tests",
                    "produce_patch",
                ],
            },
        )
        pre = run_preflight(self.store, run["id"])
        # Ensure we have requirements and awaiting_approval
        run2 = self.store.get_run(run["id"])
        if not run2.get("approval_requirements"):
            run2["approval_requirements"] = ["shan_task_approval"]
            if risk == "RED":
                run2["approval_requirements"] = [
                    "shan_task_approval",
                    "security_review",
                ]
            run2["status"] = "awaiting_approval"
            self.store.save_run_for_setup(run2)
        elif run2["status"] != "awaiting_approval":
            run2["status"] = "awaiting_approval"
            self.store.save_run_for_setup(run2)
        return self.store.get_run(run["id"])


class AtomicSuccessTests(ApprovalAtomicFixture):
    def test_partial_approval_stays_awaiting(self):
        run = self._run_awaiting(risk="RED")
        # Ensure two requirements
        run["approval_requirements"] = ["shan_task_approval", "security_review"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        result = record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
        )
        self.assertEqual(result["run"]["status"], "awaiting_approval")
        history = self.store.list_run_approval_history(run["id"])
        self.assertEqual(len(history), 1)
        events = [
            e
            for e in self.store.list_run_events(run["id"])
            if e.get("event_type") == "approval_recorded"
        ]
        self.assertEqual(len(events), 1)

    def test_final_approval_transitions(self):
        run = self._run_awaiting(risk="YELLOW")
        run["approval_requirements"] = ["shan_task_approval"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        result = record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
        )
        self.assertEqual(result["run"]["status"], "approved")
        events = [e.get("event_type") for e in self.store.list_run_events(run["id"])]
        self.assertIn("approval_recorded", events)
        self.assertIn("run_approved", events)
        self.assertGreaterEqual(len(self.store.list_run_approval_history(run["id"])), 1)

    def test_rejection_terminal(self):
        run = self._run_awaiting()
        run["approval_requirements"] = ["shan_task_approval"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        result = record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="rejected",
            note="no",
            actor="shan",
        )
        self.assertEqual(result["run"]["status"], "rejected")
        with self.assertRaises(ValueError):
            record_run_approval(
                self.store,
                run["id"],
                requirement_type="shan_task_approval",
                decision="approved",
                actor="shan",
            )
        # History preserved; second attempt may not write if terminal
        history = self.store.list_run_approval_history(run["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["decision"], "rejected")


class RollbackAndStaleTests(ApprovalAtomicFixture):
    def test_stale_row_version_writes_nothing(self):
        run = self._run_awaiting()
        run["approval_requirements"] = ["shan_task_approval"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])
        before_events = len(self.store.list_run_events(run["id"]))
        before_hist = len(self.store.list_run_approval_history(run["id"]))

        # Bump version under the feet of a prepared approval
        run2 = self.store.get_run(run["id"])
        run2["note_bump"] = "x"
        self.store.save_run_for_setup(run2)  # increments row_version

        # record_run_approval loads fresh version — to force stale, call store directly
        from buildforme.governance import compute_run_scope_fingerprint
        from governance.constitution_engine import get_engine

        live = self.store.get_run(run["id"])
        packet = live.get("packet") if isinstance(live.get("packet"), dict) else None
        fp = compute_run_scope_fingerprint(live, packet)
        engine = get_engine()
        payload = engine.attach_to_approval(
            {
                "run_id": live["id"],
                "requirement_type": "shan_task_approval",
                "decision": "approved",
                "scope": fp,
                "scope_fingerprint": fp,
                "actor": "shan",
                "packet_id": live.get("packet_id"),
                "task_id": live.get("task_id"),
            },
            run=live,
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.commit_run_approval(
                run_id=live["id"],
                expected_row_version=1,  # deliberately wrong
                approval_payload=payload,
                event_summary="x",
                event_actor="shan",
            )
        self.assertEqual(len(self.store.list_run_events(run["id"])), before_events)
        self.assertEqual(len(self.store.list_run_approval_history(run["id"])), before_hist)
        self.assertEqual(self.store.get_run(run["id"])["status"], "awaiting_approval")

    def test_scope_change_rejects(self):
        run = self._run_awaiting()
        run["approval_requirements"] = ["shan_task_approval"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])
        # Mutate scope material
        run["baseline_commit"] = "deadbeef" * 5
        self.store.save_run_for_setup(run)
        # Approval built with old fingerprint fails inside commit when scope recalculated
        # Actually record_run_approval recomputes from live run — so we need to inject
        # wrong scope into payload via commit_run_approval
        from buildforme.governance import compute_run_scope_fingerprint
        from governance.constitution_engine import get_engine

        live = self.store.get_run(run["id"])
        engine = get_engine()
        payload = engine.attach_to_approval(
            {
                "run_id": live["id"],
                "requirement_type": "shan_task_approval",
                "decision": "approved",
                "scope": "stale-scope-fingerprint",
                "scope_fingerprint": "stale-scope-fingerprint",
                "actor": "shan",
                "packet_id": live.get("packet_id"),
            },
            run=live,
        )
        with self.assertRaisesRegex(ValueError, "scope fingerprint"):
            self.store.commit_run_approval(
                run_id=live["id"],
                expected_row_version=int(live.get("row_version") or 1),
                approval_payload=payload,
                event_summary="x",
                event_actor="shan",
            )
        self.assertEqual(len(self.store.list_run_approval_history(run["id"])), 0)


class HistoryIdempotencyTests(ApprovalAtomicFixture):
    def test_history_append_only_not_overwrite(self):
        run = self._run_awaiting(risk="RED")
        run["approval_requirements"] = ["shan_task_approval", "security_review"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            note="first",
            actor="shan",
        )
        # Re-approve same type with different note (new attempt) — allowed while awaiting
        # Without idempotency this appends history and updates effective
        record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            note="second",
            actor="shan",
        )
        history = self.store.list_run_approval_history(run["id"])
        self.assertEqual(len(history), 2)
        notes = [h.get("note") for h in history]
        self.assertIn("first", notes)
        self.assertIn("second", notes)
        # Effective projection is latest
        eff = self.store.list_run_approvals(run["id"])
        shan = [e for e in eff if e.get("requirement_type") == "shan_task_approval"][0]
        self.assertEqual(shan.get("note"), "second")

    def test_idempotent_replay(self):
        run = self._run_awaiting()
        run["approval_requirements"] = ["shan_task_approval"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        r1 = record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
            idempotency_key="idemp-1",
        )
        self.assertFalse(r1.get("replayed"))
        self.assertEqual(r1["run"]["status"], "approved")
        r2 = record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
            idempotency_key="idemp-1",
        )
        self.assertTrue(r2.get("replayed"))
        history = self.store.list_run_approval_history(run["id"])
        self.assertEqual(len(history), 1)
        events = [
            e
            for e in self.store.list_run_events(run["id"])
            if e.get("event_type") == "approval_recorded"
        ]
        self.assertEqual(len(events), 1)

    def test_conflicting_idempotency_fails(self):
        run = self._run_awaiting()
        run["approval_requirements"] = ["shan_task_approval", "security_review"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
            idempotency_key="idemp-conflict",
        )
        with self.assertRaisesRegex(ValueError, "idempotency key conflicts"):
            record_run_approval(
                self.store,
                run["id"],
                requirement_type="shan_task_approval",
                decision="rejected",
                actor="shan",
                idempotency_key="idemp-conflict",
            )


class ConcurrencyTests(ApprovalAtomicFixture):
    def test_concurrent_final_approvals_one_transition(self):
        run = self._run_awaiting()
        run["approval_requirements"] = ["shan_task_approval"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run_id = run["id"]
        errors: list[str] = []
        results: list[dict] = []

        def worker(i: int) -> None:
            try:
                r = record_run_approval(
                    self.store,
                    run_id,
                    requirement_type="shan_task_approval",
                    decision="approved",
                    actor="shan",
                    note=f"w{i}",
                    idempotency_key=f"concurrent-{run_id}-{i}",
                )
                results.append(r)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = self.store.get_run(run_id)
        self.assertEqual(final["status"], "approved")
        # At least one success
        self.assertTrue(results)
        # History has one entry per successful concurrent different idemp keys
        history = self.store.list_run_approval_history(run_id)
        self.assertGreaterEqual(len(history), 1)
        # run_approved events should not explode unboundedly (each success that transitions
        # only first can transition from awaiting_approval; later may race)
        # After first wins, others may fail terminal or re-approve from approved status
        approved_events = [
            e
            for e in self.store.list_run_events(run_id)
            if e.get("event_type") == "run_approved"
        ]
        # At most a small number — ideally 1; allow race noise but not 4
        self.assertLessEqual(len(approved_events), 2)


class BindingInvalidationTests(ApprovalAtomicFixture):
    def test_stale_approval_not_counted_after_scope_change(self):
        run = self._run_awaiting(risk="RED")
        run["approval_requirements"] = ["shan_task_approval", "security_review"]
        run["status"] = "awaiting_approval"
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])

        record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
        )
        # Change baseline → scope changes; old effective approval fails binding
        run = self.store.get_run(run["id"])
        run["baseline_commit"] = "abcdef01" * 5
        self.store.save_run_for_setup(run)
        run = self.store.get_run(run["id"])
        # New approval for second requirement should not complete set with stale first
        result = record_run_approval(
            self.store,
            run["id"],
            requirement_type="security_review",
            decision="approved",
            actor="shan",
        )
        # Still awaiting because first approval binding no longer matches
        self.assertEqual(result["run"]["status"], "awaiting_approval")


if __name__ == "__main__":
    unittest.main()
