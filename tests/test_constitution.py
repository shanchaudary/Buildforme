"""Stage 5.6 AI Constitution & Governance Engine tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buildforme.execution_service import create_run, execute_dry_run, record_run_approval, run_preflight
from buildforme.packet_generator import generate_agent_packet, render_packet_markdown
from buildforme.storage import LocalStore
from governance.constitution_engine import ConstitutionEngine, get_engine, load_constitution
from governance.constitution_hash import compute_constitution_hash, verify_constitution_hash
from governance.constitution_inheritance import build_reminder, inherit_for_packet, inherit_for_run
from governance.constitution_lease import issue_lease, lease_matches_constitution, validate_lease_integrity
from governance.constitution_validator import (
    validate_constitution_document,
    validate_no_duplicate_governance,
    validate_output,
    validate_packet_binding,
    validate_provider_acknowledgement,
)


class ConstitutionDocumentTests(unittest.TestCase):
    def setUp(self):
        self.engine = get_engine(force_reload=True)
        self.constitution = self.engine.constitution

    def test_loads_twenty_laws(self):
        self.assertGreaterEqual(len(self.engine.laws()), 20)
        self.assertEqual(self.engine.version(), "1.0.0")

    def test_document_valid(self):
        result = validate_constitution_document(self.constitution)
        self.assertTrue(result["valid"], result["problems"])
        self.assertIn("LAW-020", result["law_ids"])

    def test_hash_stable_and_verifiable(self):
        h1 = compute_constitution_hash(self.constitution)
        h2 = self.engine.content_hash()
        self.assertEqual(h1, h2)
        self.assertTrue(verify_constitution_hash(self.constitution, h1))
        self.assertEqual(len(h1), 64)

    def test_hash_changes_when_law_changes(self):
        base = compute_constitution_hash(self.constitution)
        mutated = dict(self.constitution)
        laws = [dict(x) for x in mutated["laws"]]
        laws[0] = dict(laws[0], description=laws[0]["description"] + " MUTATED")
        mutated["laws"] = laws
        self.assertNotEqual(base, compute_constitution_hash(mutated))

    def test_required_law_fields(self):
        for law in self.engine.laws():
            for field in (
                "id",
                "name",
                "description",
                "applies_to",
                "severity",
                "validation",
                "evidence_required",
                "violation_response",
            ):
                self.assertTrue(law.get(field), f"{law.get('id')} missing {field}")


class InheritanceAndLeaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = get_engine(force_reload=True)
        self.constitution = self.engine.constitution

    def test_packet_inherits_constitution(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "title": "Const packet",
                "objective": "Read-only audit of documentation",
                "operating_mode": "READ_ONLY_AUDIT",
                "allowed_files": ["docs/**"],
                "acceptance_criteria": ["Report findings"],
            }
        )
        self.assertEqual(packet["constitution_version"], self.engine.version())
        self.assertEqual(packet["constitution_hash"], self.engine.content_hash())
        self.assertTrue(packet["constitution"]["bypass_forbidden"])
        self.assertIn("Constitution", packet["markdown"])
        self.assertIn(self.engine.content_hash(), packet["markdown"])
        # Prompt minimization: full law catalog not dumped into every packet markdown
        self.assertNotIn("LAW-016", packet["markdown"])  # non-critical law body not required

    def test_run_lease_immutable_when_constitution_conceptually_changes(self):
        run = {"id": "run-test-1", "provider_id": "codex", "packet_id": "pkt-1"}
        bound = inherit_for_run(self.constitution, run)
        lease = bound["constitution_lease"]
        self.assertEqual(validate_lease_integrity(lease), [])
        # Simulate new constitution version for *new* runs only
        newer = dict(self.constitution, version="1.0.1")
        self.assertFalse(lease_matches_constitution(lease, newer) or lease.get("constitution_version") == "1.0.1")
        # Existing bound run keeps original
        rebound = inherit_for_run(newer, bound)
        self.assertEqual(rebound["constitution_hash"], lease["constitution_hash"])
        self.assertEqual(rebound["constitution_lease_id"], lease["lease_id"])

    def test_reminder_does_not_resend_full_constitution(self):
        reminder = build_reminder(
            {
                "version": self.engine.version(),
                "hash": self.engine.content_hash(),
                "critical_law_ids": list(self.constitution.get("critical_law_ids") or []),
            },
            laws=self.engine.laws(),
            phase="run_start",
        )
        self.assertFalse(reminder["full_constitution_resent"])
        self.assertIn("CONSTITUTION REMINDER", reminder["text"])
        self.assertIn(self.engine.content_hash(), reminder["text"])
        # Full 20-law dump not present as individual expanded catalog
        self.assertLess(len(reminder["text"]), 4000)

    def test_provider_ack_required(self):
        provider = {"provider_id": "codex", "constitution_acknowledged": False}
        result = validate_provider_acknowledgement(provider, self.constitution)
        self.assertFalse(result["valid"])
        acked = self.engine.acknowledge_provider(provider, actor="shan")
        result2 = validate_provider_acknowledgement(acked, self.constitution)
        self.assertTrue(result2["valid"], result2["problems"])

    def test_no_duplicate_governance_authority(self):
        result = validate_no_duplicate_governance(
            ["governance.constitution_engine", "governance.constitution_engine"]
        )
        self.assertFalse(result["valid"])
        result_ok = validate_no_duplicate_governance(
            ["governance.constitution_engine", "buildforme.governance"]
        )
        self.assertTrue(result_ok["valid"])


class ValidationAndViolationTests(unittest.TestCase):
    def setUp(self):
        self.engine = get_engine(force_reload=True)

    def test_fabrication_rejected(self):
        result = validate_output(
            {"text": "I fabricated evidence and mock success for this task", "claims_complete": True},
            constitution=self.engine.constitution,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any(v["law_id"] == "LAW-002" for v in result["violations"]))

    def test_fake_success_rejected(self):
        result = validate_output(
            "success because it compiles and no error means success",
            constitution=self.engine.constitution,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any(v["law_id"] == "LAW-005" for v in result["violations"]))

    def test_bypass_rejected(self):
        result = validate_output(
            {"text": "we should bypass the constitution for speed", "bypass_constitution": True},
            constitution=self.engine.constitution,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any(v["law_id"] == "LAW-020" for v in result["violations"]))

    def test_completion_without_evidence_rejected(self):
        result = validate_output(
            {"claims_complete": True, "text": "done complete"},
            constitution=self.engine.constitution,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any(v["law_id"] == "LAW-001" for v in result["violations"]))

    def test_honest_completion_with_evidence_passes(self):
        result = validate_output(
            {
                "claims_complete": True,
                "text": "Tests passed. Verification complete. Final state documented.",
                "evidence": ["unittest OK"],
                "tests": ["python -m unittest"],
            },
            constitution=self.engine.constitution,
            context={"verified_capabilities": ["dry_run"]},
        )
        self.assertTrue(result["passed"], result["violations"])


class IntegrationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        sample = json.loads(
            (Path(__file__).resolve().parent.parent / "data" / "sample_project.json").read_text(encoding="utf-8")
        )
        self.store.load_sample_project(sample, replace=True)
        self.store.set_project_execution_control("buildforme", execution_status="enabled", reason="test")
        self.engine = get_engine(force_reload=True)
        for provider in self.store.list_providers():
            refreshed = self.engine.acknowledge_provider(provider, actor="shan")
            self.store.set_provider_constitution_ack(
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
                "target_branch": "feature/stage-5-6",
            }
        )
        self.packet = self.store.save_packet(packet)

    def test_create_run_requires_provider_ack(self):
        # Clear ack
        self.store.set_provider_constitution_ack(
            "codex",
            {
                "constitution_supported": True,
                "constitution_acknowledged": False,
                "constitution_version": None,
                "constitution_hash": None,
                "constitution_last_refresh": None,
                "constitution_acknowledged_at": None,
            },
        )
        with self.assertRaises(ValueError) as ctx:
            create_run(
                self.store,
                {
                    "project_id": "buildforme",
                    "provider_id": "codex",
                    "packet": self.packet,
                    "target_branch": "feature/stage-5-6",
                    "risk": "YELLOW",
                },
            )
        self.assertIn("Constitution", str(ctx.exception))

    def test_run_binds_lease_and_approval_hash(self):
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": self.packet,
                "packet_id": self.packet["id"],
                "target_branch": "feature/stage-5-6",
                "risk": "YELLOW",
                "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            },
        )
        self.assertTrue(run.get("constitution_lease_id"))
        self.assertEqual(run.get("constitution_hash"), self.engine.content_hash())
        leases = self.store.list_constitution_leases(run_id=run["id"])
        self.assertTrue(leases)
        pre = run_preflight(self.store, run["id"])
        self.assertTrue(pre["preflight"]["passed"], pre["preflight"].get("blocking_reasons"))
        # Force approval path if needed
        run2 = self.store.get_run(run["id"])
        if str(run2.get("status")) == "awaiting_approval":
            for req in run2.get("approval_requirements") or []:
                result = record_run_approval(
                    self.store, run["id"], requirement_type=req, decision="approved", actor="shan"
                )
                self.assertEqual(result["approval"].get("constitution_hash"), self.engine.content_hash())
        # Ensure approved
        run3 = self.store.get_run(run["id"])
        if str(run3.get("status")) != "approved":
            run3["status"] = "approved"
            self.store.save_run(run3)
        dry = execute_dry_run(self.store, run["id"])
        self.assertEqual(dry["run"]["constitution_compliance"]["status"], "compliant")
        self.assertEqual(dry["run"]["status"], "completed")

    def test_packet_binding_validation(self):
        result = validate_packet_binding(self.packet, self.engine.constitution)
        self.assertTrue(result["valid"], result["problems"])

    def test_engine_full_validation_suite(self):
        payload = self.engine.full_validation_suite(self.store)
        self.assertTrue(payload["passed"], payload["checks"])

    def test_dashboard_payload(self):
        payload = self.engine.dashboard_payload(self.store)
        self.assertEqual(payload["status"]["version"], "1.0.0")
        self.assertEqual(len(payload["laws"]), 20)
        self.assertTrue(payload["provider_acknowledgements"])

    def test_violation_persistence(self):
        validation = self.engine.validate_output(
            {"text": "bypass the constitution now", "bypass_constitution": True}
        )
        events = self.engine.record_validation_violations(
            self.store, validation, run_id="run-x", provider_id="codex"
        )
        self.assertTrue(events)
        listed = self.store.list_constitution_violations()
        self.assertTrue(any(v.get("law_id") == "LAW-020" for v in listed))


class BackwardCompatibilityTests(unittest.TestCase):
    def test_previous_stage_modules_import(self):
        from buildforme import policy, planner, packet_generator, providers, governance

        self.assertTrue(callable(policy.classify_task))
        self.assertTrue(callable(planner.plan_project))
        self.assertTrue(callable(packet_generator.generate_agent_packet))
        self.assertTrue(callable(providers.default_provider_registry))
        self.assertTrue(callable(governance.parse_bool_strict))

    def test_stage_5_5_governance_not_replaced(self):
        from buildforme.governance import parse_bool_strict, compute_run_scope_fingerprint

        self.assertFalse(parse_bool_strict("false"))
        fp = compute_run_scope_fingerprint({"id": "r1", "project_id": "p"}, {})
        self.assertEqual(len(fp), 64)


if __name__ == "__main__":
    unittest.main()
