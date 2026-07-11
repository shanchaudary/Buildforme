from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# Schema v8 repair re-review links.
path = ROOT / "buildforme" / "db.py"
text = path.read_text(encoding="utf-8")
text = replace_once(text, "SCHEMA_VERSION = 7", "SCHEMA_VERSION = 8", label="schema v8")
anchor = '''CREATE INDEX IF NOT EXISTS idx_repair_admissions_source
  ON repair_admissions(source_run_id, created_at);
"""
'''
replacement = '''CREATE INDEX IF NOT EXISTS idx_repair_admissions_source
  ON repair_admissions(source_run_id, created_at);

CREATE TABLE IF NOT EXISTS repair_review_links (
  repair_packet_id TEXT PRIMARY KEY REFERENCES repair_packets(repair_packet_id),
  repair_admission_id TEXT NOT NULL REFERENCES repair_admissions(repair_admission_id),
  source_run_id TEXT NOT NULL REFERENCES runs(id),
  child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
  fresh_evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(evidence_id),
  review_cycle_id TEXT NOT NULL UNIQUE REFERENCES review_cycles(id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1
);
"""
'''
text = replace_once(text, anchor, replacement, label="repair review link schema")
text = replace_once(
    text,
    '''                    if current < 7:
                        self._migrate_to_v7(conn)
                    if current < SCHEMA_VERSION:
''',
    '''                    if current < 7:
                        self._migrate_to_v7(conn)
                    if current < 8:
                        self._migrate_to_v8(conn)
                    if current < SCHEMA_VERSION:
''',
    label="v8 dispatch",
)
anchor = '''    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
migration = '''    def _migrate_to_v8(self, conn: sqlite3.Connection) -> None:
        """Bind repair child fresh evidence to the mandatory re-review cycle."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_review_links (
              repair_packet_id TEXT PRIMARY KEY REFERENCES repair_packets(repair_packet_id),
              repair_admission_id TEXT NOT NULL REFERENCES repair_admissions(repair_admission_id),
              source_run_id TEXT NOT NULL REFERENCES runs(id),
              child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
              fresh_evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(evidence_id),
              review_cycle_id TEXT NOT NULL UNIQUE REFERENCES review_cycles(id),
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
text = replace_once(text, anchor, migration, label="v8 migration")
path.write_text(text, encoding="utf-8")

# Repair children are founder-blocked from admission onward.
path = ROOT / "buildforme" / "repair_service.py"
text = path.read_text(encoding="utf-8")
anchor = '''            "requires_independent_review_after_execution": True,
            "dry_run_result": None,
'''
replacement = '''            "requires_independent_review_after_execution": True,
            "stage7_review_required": True,
            "stage7_review_cycle_id": None,
            "independent_review": {
                "status": "awaiting_fresh_execution_and_review_cycle",
                "source_cycle_id": repair_packet.get("source_cycle_id"),
            },
            "dry_run_result": None,
'''
text = replace_once(text, anchor, replacement, label="repair founder gate")
text += r'''


def create_repair_review_cycle(
    store: LocalStore,
    repair_packet_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    from buildforme.review_service import create_independent_review_cycle

    packet_id = validate_safe_id(repair_packet_id, field="repair_packet_id")
    actor = validate_actor(actor)
    try:
        link = store.get_repair_review_link(packet_id)
        return {
            "cycle": store.get_review_cycle(str(link.get("review_cycle_id") or "")),
            "assignments": store.list_review_assignments(str(link.get("review_cycle_id") or "")),
            "repair_review_link": link,
            "replayed": True,
        }
    except KeyError:
        pass
    packet = store.get_repair_packet(packet_id)
    admission = store.get_repair_admission(packet_id)
    child_run_id = str(admission.get("child_run_id") or "")
    source_cycle_id = str(packet.get("source_cycle_id") or "")
    source_assignments = store.list_review_assignments(source_cycle_id)
    reviewers = [
        {
            "reviewer_id": str(item.get("reviewer_id") or ""),
            "provider_id": str(item.get("provider_id") or ""),
            "role": str(item.get("role") or "general"),
        }
        for item in source_assignments
    ]
    result = create_independent_review_cycle(
        store,
        child_run_id,
        reviewers=reviewers,
        actor=actor,
    )
    result["repair_review_link"] = store.get_repair_review_link(packet_id)
    result["replayed"] = False
    return result


def execute_governed_repair_and_open_review(
    store: LocalStore,
    repair_packet_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    from buildforme.execution_service import execute_supervised

    packet_id = validate_safe_id(repair_packet_id, field="repair_packet_id")
    admission = store.get_repair_admission(packet_id)
    child_run_id = str(admission.get("child_run_id") or "")
    child = store.get_run(child_run_id)
    if str(child.get("status") or "") != "approved":
        raise ValueError("repair child must be approved before supervised repair execution")
    execution = execute_supervised(store, child_run_id)
    saved = execution.get("run") if isinstance(execution, dict) else None
    if not isinstance(saved, dict) or str(saved.get("status") or "") != "needs_review":
        raise ValueError("repair execution did not reach needs_review")
    verification = saved.get("verification") if isinstance(saved.get("verification"), dict) else {}
    if not verification.get("passed"):
        raise ValueError("repair execution deterministic verification did not pass")
    review = create_repair_review_cycle(store, packet_id, actor=actor)
    return {"execution": execution, "review": review, "run": store.get_run(child_run_id)}
'''
path.write_text(text, encoding="utf-8")

# Add repair-cycle validation/linking to the single review cycle authority.
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
anchor = '''            if str(evidence_constitution.get("hash") or "") != str(run.get("constitution_hash") or ""):
                raise ValueError("execution evidence Constitution is stale")
            prior_same_evidence = conn.execute(
'''
replacement = '''            if str(evidence_constitution.get("hash") or "") != str(run.get("constitution_hash") or ""):
                raise ValueError("execution evidence Constitution is stale")
            repair_context = None
            repair_packet_id = str(run.get("repair_packet_id") or "")
            if repair_packet_id:
                if run.get("stage7_review_required") is not True or run.get(
                    "requires_independent_review_after_execution"
                ) is not True:
                    raise ValueError("repair child is missing mandatory independent-review authority")
                admission_row = conn.execute(
                    "SELECT repair_admission_id, source_run_id, child_run_id, payload_json FROM repair_admissions WHERE repair_packet_id=?",
                    (repair_packet_id,),
                ).fetchone()
                if not admission_row:
                    raise ValueError("repair review cycle requires canonical repair admission")
                if str(admission_row[2] or "") != run_id:
                    raise ValueError("repair admission child run mismatch")
                packet_row = conn.execute(
                    "SELECT payload_json FROM repair_packets WHERE repair_packet_id=?",
                    (repair_packet_id,),
                ).fetchone()
                if not packet_row:
                    raise ValueError("repair review cycle requires canonical repair packet")
                repair_packet = loads(packet_row[0], {})
                if str(repair_packet.get("repair_provider_id") or "") != str(run.get("provider_id") or ""):
                    raise ValueError("repair review implementer does not match repair packet")
                if str(evidence_row[0]) == str(repair_packet.get("source_evidence_id") or ""):
                    raise ValueError("repair review requires fresh child execution evidence")
                actual_provider_ids = sorted(str(item.get("provider_id") or "") for item in assignment_records)
                expected_provider_ids = sorted(
                    str(item) for item in (repair_packet.get("source_reviewer_provider_ids") or [])
                )
                if actual_provider_ids != expected_provider_ids:
                    raise ValueError("repair re-review assignments must exactly reuse source reviewer providers")
                if str(run.get("provider_id") or "") in actual_provider_ids:
                    raise ValueError("repair implementer cannot participate in repair re-review")
                if conn.execute(
                    "SELECT repair_packet_id FROM repair_review_links WHERE repair_packet_id=? OR child_run_id=? OR fresh_evidence_id=?",
                    (repair_packet_id, run_id, str(evidence_row[0])),
                ).fetchone():
                    raise ValueError("repair re-review link already exists")
                repair_context = {
                    "repair_packet": repair_packet,
                    "repair_admission_id": str(admission_row[0]),
                    "source_run_id": str(admission_row[1]),
                    "fresh_evidence_id": str(evidence_row[0]),
                }
            prior_same_evidence = conn.execute(
'''
text = replace_once(text, anchor, replacement, label="repair re-review validation")
anchor = '''            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_cycle_created", "Blind independent review cycle created", actor, dumps({"assignment_count": len(assignment_records)}), now),
            )
'''
replacement = '''            if repair_context is not None:
                repair_packet = repair_context["repair_packet"]
                repair_link = {
                    "schema": "buildforme.repair_review_link.v1",
                    "repair_packet_id": repair_packet_id,
                    "repair_admission_id": repair_context["repair_admission_id"],
                    "source_run_id": repair_context["source_run_id"],
                    "child_run_id": run_id,
                    "fresh_evidence_id": repair_context["fresh_evidence_id"],
                    "fresh_evidence_fingerprint": actual_evidence_fp,
                    "review_cycle_id": cycle_id,
                    "reviewer_provider_ids": sorted(
                        str(item.get("provider_id") or "") for item in assignment_records
                    ),
                    "repair_provider_id": run.get("provider_id"),
                    "created_at": now,
                    "immutable": True,
                }
                conn.execute(
                    "INSERT INTO repair_review_links(repair_packet_id, repair_admission_id, source_run_id, child_run_id, fresh_evidence_id, review_cycle_id, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,1)",
                    (
                        repair_packet_id,
                        repair_context["repair_admission_id"],
                        repair_context["source_run_id"],
                        run_id,
                        repair_context["fresh_evidence_id"],
                        cycle_id,
                        dumps(repair_link),
                        now,
                    ),
                )
                source_row = conn.execute(
                    "SELECT row_version, payload_json FROM runs WHERE id=?",
                    (repair_context["source_run_id"],),
                ).fetchone()
                if not source_row:
                    raise ValueError("repair source run missing while linking re-review")
                source_run = loads(source_row[1], {})
                source_run["stage7_repair_status"] = "re_review_collecting"
                source_run["stage7_repair_review_cycle_id"] = cycle_id
                source_run["stage7_repair_fresh_evidence_id"] = repair_context["fresh_evidence_id"]
                source_run["updated_at"] = now
                source_version = int(source_row[0] or 1) + 1
                source_run["row_version"] = source_version
                source_cur = conn.execute(
                    "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                    (
                        dumps(source_run),
                        now,
                        source_version,
                        repair_context["source_run_id"],
                        int(source_row[0] or 1),
                    ),
                )
                if source_cur.rowcount != 1:
                    raise ValueError("stale repair source race while linking re-review")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_cycle_created", "Blind independent review cycle created", actor, dumps({"assignment_count": len(assignment_records), "repair_packet_id": repair_packet_id or None}), now),
            )
'''
text = replace_once(text, anchor, replacement, label="repair review link insert")
# Getter before get_review_cycle.
anchor = '''    def get_review_cycle(self, cycle_id: str) -> dict[str, Any]:
'''
getter = '''    def get_repair_review_link(self, repair_packet_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM repair_review_links WHERE repair_packet_id=?",
                (str(repair_packet_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Repair review link not found: {repair_packet_id}")
        return loads(row[0], {})

    def get_review_cycle(self, cycle_id: str) -> dict[str, Any]:
'''
text = replace_once(text, anchor, getter, label="repair review getter")
path.write_text(text, encoding="utf-8")

# LocalStore wrapper.
path = ROOT / "buildforme" / "storage.py"
text = path.read_text(encoding="utf-8")
anchor = '''    # —— Internals ——
'''
replacement = '''    def get_repair_review_link(self, repair_packet_id: str) -> dict[str, Any]:
        return self.s6.get_repair_review_link(repair_packet_id)

    # —— Internals ——
'''
text = replace_once(text, anchor, replacement, label="repair review wrapper")
path.write_text(text, encoding="utf-8")

# Schema assertions v8.
for name in (
    "tests/test_stage6_execution.py",
    "tests/test_stage6_redteam_round2.py",
    "tests/test_stage7_packet7a_contract.py",
    "tests/test_stage7_review_authority.py",
    "tests/test_stage7_review_execution.py",
    "tests/test_stage7_packet7d_repair_authority.py",
    "tests/test_stage7_packet7d_repair_admission.py",
):
    p = ROOT / name
    if p.exists():
        t = p.read_text(encoding="utf-8")
        t = t.replace("SCHEMA_VERSION, 7", "SCHEMA_VERSION, 8")
        t = t.replace('["schema_version"], 7', '["schema_version"], 8')
        p.write_text(t, encoding="utf-8")

# Re-review tests build on the real Packet 7D-B fixture.
test = r'''from __future__ import annotations

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
        fixture = Stage7RepairAdmissionTests(methodName="test_schema_v8")
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
'''
(ROOT / "tests" / "test_stage7_packet7d_repair_rereview.py").write_text(test, encoding="utf-8")

contract = r'''from __future__ import annotations

import unittest
from pathlib import Path


class Stage7Packet7DRereviewContractTests(unittest.TestCase):
    def test_repair_child_founder_gate_is_permanent(self):
        source = Path("buildforme/repair_service.py").read_text(encoding="utf-8")
        self.assertIn('"stage7_review_required": True', source)
        self.assertIn("create_repair_review_cycle", source)
        self.assertIn("execute_governed_repair_and_open_review", source)

    def test_single_review_cycle_authority_owns_repair_link(self):
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn("repair_review_links", source)
        self.assertIn("exactly reuse source reviewer providers", source)
        self.assertIn("repair implementer cannot participate", source)


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_stage7_packet7d_rereview_contract.py").write_text(contract, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n## Packet 7D-C — repair execution and mandatory fresh re-review\n\n- Repair children are marked `stage7_review_required` at admission, so founder acceptance fails closed before a fresh clear cycle exists.\n- After approved supervised repair execution reaches `needs_review` with deterministic verification passed, Buildforme opens a new cycle through the existing review-cycle authority.\n- SQLite requires fresh child execution evidence, the exact source reviewer-provider set, and exclusion of the repair implementer.\n- The repair packet, admission, child, fresh evidence, and new cycle are linked append-only; duplicate orchestration replays the same cycle.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7D repair re-review applied")
