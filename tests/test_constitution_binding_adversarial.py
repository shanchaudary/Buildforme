"""Adversarial tests for canonical Constitution lease and approval binding."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buildforme.execution_service import create_run, record_run_approval, run_preflight
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.packet_generator import generate_agent_packet
from buildforme.storage import LocalStore
from governance.constitution_binding_guard import validate_approval_binding
from governance.constitution_engine import get_engine
from governance.constitution_lease import (
    persist_lease_append_only,
    seal_lease,
    validate_lease_integrity,
)


class ConstitutionBindingAdversarialTests(unittest.TestCase):
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
            "buildforme",
            execution_status="enabled",
            reason="adversarial test",
        )
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
                    "constitution_acknowledged_at": refreshed[
                        "constitution_acknowledged_at"
                    ],
                    "constitution_ack_actor": "shan",
                },
            )
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "title": "Constitution adversarial run",
                "objective": "Change a bounded dashboard parser and add tests",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["public/**", "tests/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Tests pass"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/constitution-adversarial",
            }
        )
        self.packet = self.store.save_packet(packet)

    def create_bound_run(self) -> dict:
        return create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": self.packet,
                "packet_id": self.packet["id"],
                "target_branch": "feature/constitution-adversarial",
                "risk": "YELLOW",
                "requested_capabilities": [
                    "read_repository",
                    "edit_repository",
                    "run_tests",
                    "produce_patch",
                ],
            },
        )

    def test_lease_requires_exact_immutable_true(self) -> None:
        run = self.create_bound_run()
        broken = dict(run["constitution_lease"])
        broken.pop("immutable")
        broken = seal_lease(broken)
        problems = validate_lease_integrity(broken)
        self.assertIn("lease immutable must be exactly true", problems)

    def test_append_only_persistence_rejects_same_id_with_different_content(self) -> None:
        run = self.create_bound_run()
        forged = seal_lease(dict(run["constitution_lease"], provider_id="claude"))
        with self.assertRaisesRegex(ValueError, "collision or mutation"):
            persist_lease_append_only(self.store, forged)

    def test_resealed_embedded_forgery_fails_against_canonical_store(self) -> None:
        run = self.create_bound_run()
        forged_lease = seal_lease(dict(run["constitution_lease"], provider_id="claude"))
        forged_run = dict(run)
        forged_run["provider_id"] = "claude"
        forged_run["constitution_lease"] = forged_lease
        forged_run["constitution_lease_fingerprint"] = forged_lease["lease_fingerprint"]
        forged_run["scope_fingerprint"] = compute_run_scope_fingerprint(
            forged_run,
            forged_run["packet"],
        )
        self.store.save_run(forged_run)

        result = run_preflight(self.store, run["id"])
        self.assertFalse(result["preflight"]["passed"])
        joined = " ".join(result["preflight"]["blocking_reasons"])
        self.assertTrue(
            "canonical stored lease" in joined or "expected identity" in joined,
            joined,
        )

    def test_low_level_stored_lease_tamper_is_detected_at_preflight(self) -> None:
        run = self.create_bound_run()
        tampered = seal_lease(dict(run["constitution_lease"], packet_id="other-packet"))
        # Simulate hostile/manual runtime-file replacement through the low-level store.
        self.store.save_constitution_lease(tampered)

        result = run_preflight(self.store, run["id"])
        self.assertFalse(result["preflight"]["passed"])
        joined = " ".join(result["preflight"]["blocking_reasons"])
        self.assertIn("constitution_lease", joined)

    def test_stale_or_forged_packet_binding_is_rejected_at_run_creation(self) -> None:
        stale = dict(self.packet)
        stale_binding = dict(stale["constitution"])
        stale_binding["hash"] = "0" * 64
        stale["constitution"] = stale_binding
        stale["constitution_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "packet constitution binding invalid"):
            create_run(
                self.store,
                {
                    "project_id": "buildforme",
                    "provider_id": "codex",
                    "packet": stale,
                    "target_branch": "feature/constitution-adversarial",
                    "risk": "YELLOW",
                },
            )

    def test_scope_fingerprint_includes_lease_fingerprint(self) -> None:
        run = self.create_bound_run()
        before = compute_run_scope_fingerprint(run, run["packet"])
        mutated = dict(run)
        mutated["constitution_lease_fingerprint"] = "f" * 64
        after = compute_run_scope_fingerprint(mutated, mutated["packet"])
        self.assertNotEqual(before, after)

    def test_approval_is_bound_to_current_constitution_lease(self) -> None:
        run = self.create_bound_run()
        preflight = run_preflight(self.store, run["id"])
        self.assertTrue(preflight["preflight"]["passed"])
        self.assertIn(
            "shan_task_approval",
            preflight["preflight"]["required_approvals"],
        )
        result = record_run_approval(
            self.store,
            run["id"],
            requirement_type="shan_task_approval",
            decision="approved",
            actor="shan",
        )
        approval = result["approval"]
        current = self.store.get_run(run["id"])
        current_scope = compute_run_scope_fingerprint(current, current["packet"])
        self.assertEqual(
            validate_approval_binding(
                approval,
                current,
                expected_scope_fingerprint=current_scope,
            ),
            [],
        )

        changed = dict(current)
        changed["constitution_lease_id"] = "lease-forged"
        problems = validate_approval_binding(
            approval,
            changed,
            expected_scope_fingerprint=compute_run_scope_fingerprint(
                changed,
                changed["packet"],
            ),
        )
        self.assertTrue(problems)
        self.assertTrue(
            any("constitution_lease_id" in problem for problem in problems),
            problems,
        )


if __name__ == "__main__":
    unittest.main()
