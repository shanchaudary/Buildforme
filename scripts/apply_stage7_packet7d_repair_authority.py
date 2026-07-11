from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


repair_contracts = r'''"""Stage 7 Packet 7D immutable repair-packet authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from buildforme.storage import utc_now_iso

REPAIR_PACKET_SCHEMA = "buildforme.repair_packet.v1"


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint(material: dict[str, Any]) -> str:
    raw = json.dumps(
        {"schema": REPAIR_PACKET_SCHEMA, "material": _canonical(material)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _blocking_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [dict(item) for item in findings if isinstance(item, dict) and item.get("blocking") is True]
    return sorted(items, key=lambda item: str(item.get("finding_id") or ""))


def _report_fingerprints(reports: list[dict[str, Any]]) -> list[str]:
    return sorted(str(item.get("report_fingerprint") or "") for item in reports if item.get("report_fingerprint"))


def build_repair_packet_record(
    *,
    cycle: dict[str, Any],
    run: dict[str, Any],
    evidence: dict[str, Any],
    reports: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    repair_provider_id: str,
    actor: str,
    provider_ack: dict[str, Any],
    repair_packet_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if str(cycle.get("status") or "") != "repair_required":
        raise ValueError("repair packet requires finalized repair_required review cycle")
    aggregate = cycle.get("aggregate") if isinstance(cycle.get("aggregate"), dict) else {}
    if str(aggregate.get("status") or "") != "repair_required" or aggregate.get("quorum_met") is not True:
        raise ValueError("repair packet requires quorum-backed repair_required aggregate")
    if str(run.get("status") or "") != "needs_review":
        raise ValueError("repair packet requires source run status needs_review")
    if str(run.get("stage7_review_cycle_id") or "") != str(cycle.get("cycle_id") or ""):
        raise ValueError("source run is not bound to repair review cycle")
    if str(evidence.get("evidence_id") or "") != str(cycle.get("evidence_id") or ""):
        raise ValueError("repair packet source evidence id mismatch")
    if str(evidence.get("evidence_fingerprint") or "") != str(cycle.get("evidence_fingerprint") or ""):
        raise ValueError("repair packet source evidence fingerprint mismatch")

    provider_id = str(repair_provider_id or "").strip().lower()
    if not provider_id:
        raise ValueError("repair_provider_id required")
    reviewer_providers = sorted(str(item) for item in (aggregate.get("provider_ids") or []))
    if provider_id in reviewer_providers:
        raise ValueError("a source reviewer provider cannot author the governed repair")
    if provider_ack.get("constitution_acknowledged") is not True:
        raise ValueError("repair provider has not acknowledged the Constitution")
    if str(provider_ack.get("constitution_hash") or "") != str(cycle.get("constitution_hash") or ""):
        raise ValueError("repair provider Constitution acknowledgement mismatch")

    blocking = _blocking_findings(findings)
    if not blocking:
        raise ValueError("repair_required cycle has no persisted blocking findings")
    blocking_ids = [str(item.get("finding_id") or "") for item in blocking]
    aggregate_blocking_ids = sorted(str(item) for item in (aggregate.get("blocking_finding_ids") or []))
    if blocking_ids != aggregate_blocking_ids:
        raise ValueError("persisted blocking findings do not match aggregate")

    report_fps = _report_fingerprints(reports)
    aggregate_report_fps = sorted(str(item) for item in (aggregate.get("report_fingerprints") or []))
    if report_fps != aggregate_report_fps:
        raise ValueError("persisted review reports do not match aggregate")

    source_packet = run.get("packet") if isinstance(run.get("packet"), dict) else {}
    allowed_files = sorted(str(item) for item in (source_packet.get("allowed_files") or evidence.get("allowed_files") or []))
    forbidden_files = sorted(str(item) for item in (source_packet.get("forbidden_files") or evidence.get("forbidden_files") or []))
    acceptance = [
        f"Resolve blocking finding {item.get('finding_id')}: {str(item.get('summary') or '').strip()}"
        for item in blocking
    ]
    packet_id = repair_packet_id or f"rpair-{uuid.uuid4().hex[:18]}"
    timestamp = created_at or utc_now_iso()
    material = {
        "repair_packet_id": packet_id,
        "source_cycle_id": cycle.get("cycle_id"),
        "source_run_id": run.get("id"),
        "source_evidence_id": evidence.get("evidence_id"),
        "source_evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "source_scope_fingerprint": cycle.get("scope_fingerprint"),
        "source_constitution_hash": cycle.get("constitution_hash"),
        "source_constitution_lease_id": cycle.get("constitution_lease_id"),
        "source_aggregate_fingerprint": aggregate.get("aggregate_fingerprint"),
        "source_report_fingerprints": report_fps,
        "source_blocking_findings": blocking,
        "repair_provider_id": provider_id,
        "source_implementer_provider_id": cycle.get("implementer_provider_id"),
        "source_reviewer_provider_ids": reviewer_providers,
        "next_review_excluded_provider_id": provider_id,
        "repository": run.get("repository"),
        "repository_local_path": run.get("repository_local_path"),
        "source_worktree_path": evidence.get("worktree_path") or run.get("worktree_path"),
        "approved_baseline_commit": evidence.get("approved_baseline_commit") or run.get("baseline_commit"),
        "source_final_head_sha": evidence.get("final_head_sha"),
        "source_execution_branch": evidence.get("execution_branch"),
        "source_manifest_fingerprint": evidence.get("manifest_fingerprint"),
        "source_patch_fingerprint": evidence.get("patch_fingerprint"),
        "allowed_files": allowed_files,
        "forbidden_files": forbidden_files,
        "repair_acceptance_criteria": acceptance,
        "repair_scope_expansion_forbidden": True,
        "fresh_execution_evidence_required": True,
        "new_independent_review_cycle_required": True,
        "created_by": str(actor or "shan"),
    }
    record = {
        "schema": REPAIR_PACKET_SCHEMA,
        **material,
        "status": "packet_ready",
        "created_at": timestamp,
        "immutable": True,
    }
    record["repair_fingerprint"] = _fingerprint(material)
    return record


def validate_repair_packet_for_storage(
    packet: dict[str, Any],
    *,
    cycle: dict[str, Any],
    run: dict[str, Any],
    evidence: dict[str, Any],
    reports: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    provider_ack: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    if not isinstance(packet, dict):
        return ["repair packet must be an object"]
    for field in (
        "repair_packet_id",
        "source_cycle_id",
        "source_run_id",
        "source_evidence_id",
        "source_evidence_fingerprint",
        "source_scope_fingerprint",
        "source_constitution_hash",
        "source_constitution_lease_id",
        "source_aggregate_fingerprint",
        "source_report_fingerprints",
        "source_blocking_findings",
        "repair_provider_id",
        "allowed_files",
        "forbidden_files",
        "repair_acceptance_criteria",
        "repair_fingerprint",
    ):
        if packet.get(field) in (None, ""):
            problems.append(f"repair packet missing {field}")
    if packet.get("immutable") is not True:
        problems.append("repair packet immutable must be exactly true")
    if str(packet.get("status") or "") != "packet_ready":
        problems.append("repair packet status must be packet_ready")
    if packet.get("repair_scope_expansion_forbidden") is not True:
        problems.append("repair packet must forbid scope expansion")
    if packet.get("fresh_execution_evidence_required") is not True:
        problems.append("repair packet must require fresh execution evidence")
    if packet.get("new_independent_review_cycle_required") is not True:
        problems.append("repair packet must require a new independent review cycle")
    try:
        expected = build_repair_packet_record(
            cycle=cycle,
            run=run,
            evidence=evidence,
            reports=reports,
            findings=findings,
            repair_provider_id=str(packet.get("repair_provider_id") or ""),
            actor=str(packet.get("created_by") or "shan"),
            provider_ack=provider_ack,
            repair_packet_id=str(packet.get("repair_packet_id") or ""),
            created_at=str(packet.get("created_at") or ""),
        )
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    for field, value in expected.items():
        if _canonical(packet.get(field)) != _canonical(value):
            problems.append(f"repair packet {field} mismatch")
    return problems
'''
(ROOT / "buildforme" / "repair_contracts.py").write_text(repair_contracts, encoding="utf-8")

repair_service = r'''"""Stage 7 governed repair packet service."""

from __future__ import annotations

from typing import Any

from buildforme.governance import validate_actor, validate_safe_id
from buildforme.repair_contracts import build_repair_packet_record
from buildforme.storage import LocalStore


def create_governed_repair_packet(
    store: LocalStore,
    cycle_id: str,
    *,
    repair_provider_id: str,
    actor: str = "shan",
) -> dict[str, Any]:
    cycle_id = validate_safe_id(cycle_id, field="cycle_id")
    actor = validate_actor(actor)
    provider_id = validate_safe_id(repair_provider_id, field="repair_provider_id").lower()
    cycle = store.get_review_cycle(cycle_id)
    run = store.get_run(str(cycle.get("run_id") or ""))
    evidence = store.get_evidence_by_id(str(cycle.get("evidence_id") or ""))
    reports = store.list_review_reports(cycle_id)
    findings = store.list_review_findings(cycle_id)
    provider = store.get_provider_record(provider_id)
    if not provider.get("enabled", True):
        raise ValueError("repair provider disabled")
    provider_ack = store.s6.get_provider_ack(provider_id) or {}
    packet = build_repair_packet_record(
        cycle=cycle,
        run=run,
        evidence=evidence,
        reports=reports,
        findings=findings,
        repair_provider_id=provider_id,
        actor=actor,
        provider_ack=provider_ack,
    )
    return store.create_repair_packet_atomic(packet=packet, actor=actor)


def get_governed_repair_packet(store: LocalStore, repair_packet_id: str) -> dict[str, Any]:
    return store.get_repair_packet(validate_safe_id(repair_packet_id, field="repair_packet_id"))
'''
(ROOT / "buildforme" / "repair_service.py").write_text(repair_service, encoding="utf-8")

# db.py schema v6
path = ROOT / "buildforme" / "db.py"
text = path.read_text(encoding="utf-8")
text = replace_once(text, "SCHEMA_VERSION = 5", "SCHEMA_VERSION = 6", label="schema version")
anchor = '''CREATE INDEX IF NOT EXISTS idx_review_executions_assignment
  ON review_executions(assignment_id, created_at);
"""
'''
replacement = '''CREATE INDEX IF NOT EXISTS idx_review_executions_assignment
  ON review_executions(assignment_id, created_at);

CREATE TABLE IF NOT EXISTS repair_packets (
  repair_packet_id TEXT PRIMARY KEY,
  source_cycle_id TEXT NOT NULL UNIQUE REFERENCES review_cycles(id),
  source_run_id TEXT NOT NULL REFERENCES runs(id),
  source_evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  repair_provider_id TEXT NOT NULL,
  aggregate_fingerprint TEXT NOT NULL,
  repair_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_repair_packets_run
  ON repair_packets(source_run_id, created_at);
"""
'''
text = replace_once(text, anchor, replacement, label="schema SQL repair table")
text = replace_once(
    text,
    '''                    if current < 5:
                        self._migrate_to_v5(conn)
                    if current < SCHEMA_VERSION:
''',
    '''                    if current < 5:
                        self._migrate_to_v5(conn)
                    if current < 6:
                        self._migrate_to_v6(conn)
                    if current < SCHEMA_VERSION:
''',
    label="migration dispatch",
)
anchor = '''    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
migration = '''    def _migrate_to_v6(self, conn: sqlite3.Connection) -> None:
        """Add immutable Stage 7 governed repair packets."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_packets (
              repair_packet_id TEXT PRIMARY KEY,
              source_cycle_id TEXT NOT NULL UNIQUE REFERENCES review_cycles(id),
              source_run_id TEXT NOT NULL REFERENCES runs(id),
              source_evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
              repair_provider_id TEXT NOT NULL,
              aggregate_fingerprint TEXT NOT NULL,
              repair_fingerprint TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_repair_packets_run
              ON repair_packets(source_run_id, created_at);
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
text = replace_once(text, anchor, migration, label="v6 migration method")
path.write_text(text, encoding="utf-8")

# execution_store.py dedicated storage authority
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
anchor = '''    # —— Execution control ——
'''
methods = r'''    # —— Stage 7 governed repair packets ——
    def create_repair_packet_atomic(
        self,
        *,
        packet: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.repair_contracts import validate_repair_packet_for_storage

        record = dict(packet)
        packet_id = str(record.get("repair_packet_id") or "")
        cycle_id = str(record.get("source_cycle_id") or "")
        if not packet_id or not cycle_id:
            raise ValueError("repair_packet_id and source_cycle_id required")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, evidence_id, evidence_fingerprint, scope_fingerprint, constitution_hash, status, aggregate_json, payload_json FROM review_cycles WHERE id=?",
                (cycle_id,),
            ).fetchone()
            if not cycle_row:
                raise KeyError(f"Review cycle not found: {cycle_id}")
            cycle = loads(cycle_row[7], {})
            cycle["status"] = cycle_row[5]
            cycle["aggregate"] = loads(cycle_row[6], {}) if cycle_row[6] else cycle.get("aggregate")
            if str(cycle_row[5]) != "repair_required":
                raise ValueError("repair packet requires finalized repair_required cycle")
            run_id = str(cycle_row[0])
            run_row = conn.execute(
                "SELECT row_version, status, payload_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run not found: {run_id}")
            run = loads(run_row[2], {})
            run["status"] = run_row[1]
            run["row_version"] = int(run_row[0] or 1)
            if str(run_row[1]) != "needs_review":
                raise ValueError("repair packet requires source run status needs_review")
            if str(run.get("stage7_review_cycle_id") or "") != cycle_id:
                raise ValueError("source run is not bound to repair review cycle")
            evidence_row = conn.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id=? AND run_id=?",
                (str(cycle_row[1]), run_id),
            ).fetchone()
            if not evidence_row:
                raise ValueError("repair packet source evidence not found")
            evidence = loads(evidence_row[0], {})
            reports = [
                loads(row[0], {})
                for row in conn.execute(
                    "SELECT payload_json FROM review_reports WHERE cycle_id=? ORDER BY created_at, report_id",
                    (cycle_id,),
                ).fetchall()
            ]
            findings = [
                loads(row[0], {})
                for row in conn.execute(
                    "SELECT payload_json FROM review_findings WHERE cycle_id=? ORDER BY created_at, finding_id",
                    (cycle_id,),
                ).fetchall()
            ]
            provider_id = str(record.get("repair_provider_id") or "")
            ack_row = conn.execute(
                "SELECT payload_json, constitution_acknowledged, constitution_hash FROM provider_acks WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
            if not ack_row:
                raise ValueError("repair provider Constitution acknowledgement missing")
            provider_ack = loads(ack_row[0], {})
            provider_ack["constitution_acknowledged"] = bool(ack_row[1])
            provider_ack["constitution_hash"] = ack_row[2]
            problems = validate_repair_packet_for_storage(
                record,
                cycle=cycle,
                run=run,
                evidence=evidence,
                reports=reports,
                findings=findings,
                provider_ack=provider_ack,
            )
            if problems:
                raise ValueError("repair packet rejected: " + "; ".join(problems))
            existing = conn.execute(
                "SELECT payload_json, repair_fingerprint FROM repair_packets WHERE source_cycle_id=? OR repair_packet_id=?",
                (cycle_id, packet_id),
            ).fetchone()
            if existing:
                prior = loads(existing[0], {})
                if str(existing[1] or "") == str(record.get("repair_fingerprint") or "") and prior == record:
                    return prior
                raise ValueError("repair packet is append-only and source cycle may create only one")
            aggregate = cycle.get("aggregate") if isinstance(cycle.get("aggregate"), dict) else {}
            conn.execute(
                "INSERT INTO repair_packets(repair_packet_id, source_cycle_id, source_run_id, source_evidence_id, repair_provider_id, aggregate_fingerprint, repair_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    packet_id,
                    cycle_id,
                    run_id,
                    str(record.get("source_evidence_id") or ""),
                    provider_id,
                    str(aggregate.get("aggregate_fingerprint") or ""),
                    str(record.get("repair_fingerprint") or ""),
                    dumps(record),
                    record.get("created_at") or now,
                ),
            )
            run["stage7_repair_packet_id"] = packet_id
            run["stage7_repair_status"] = "packet_ready"
            run["stage7_repair_provider_id"] = provider_id
            run["updated_at"] = now
            new_version = int(run_row[0] or 1) + 1
            run["row_version"] = new_version
            cur = conn.execute(
                "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (dumps(run), now, new_version, run_id, int(run_row[0] or 1)),
            )
            if cur.rowcount != 1:
                raise ValueError("stale run race while binding repair packet")
            metadata = {
                "repair_packet_id": packet_id,
                "source_cycle_id": cycle_id,
                "repair_provider_id": provider_id,
                "repair_fingerprint": record.get("repair_fingerprint"),
            }
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "repair_packet_created", "Governed repair packet created", actor, dumps(metadata), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), run_id, "stage7_repair_packet_created", "Governed repair packet bound to source run", actor, dumps(metadata), now),
            )
        return record

    def get_repair_packet(self, repair_packet_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM repair_packets WHERE repair_packet_id=?",
                (str(repair_packet_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Repair packet not found: {repair_packet_id}")
        return loads(row[0], {})

    def get_repair_packet_for_cycle(self, cycle_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM repair_packets WHERE source_cycle_id=?",
                (str(cycle_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Repair packet not found for cycle: {cycle_id}")
        return loads(row[0], {})

    def list_repair_packets(self, source_run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if source_run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM repair_packets WHERE source_run_id=? ORDER BY created_at DESC",
                    (str(source_run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM repair_packets ORDER BY created_at DESC"
                ).fetchall()
        return [loads(row[0], {}) for row in rows]

    # —— Execution control ——
'''
text = replace_once(text, anchor, methods, label="repair storage methods")
path.write_text(text, encoding="utf-8")

# LocalStore wrappers
path = ROOT / "buildforme" / "storage.py"
text = path.read_text(encoding="utf-8")
anchor = '''    # —— Internals ——
'''
wrappers = '''    # —— Stage 7 Packet 7D governed repair packets ——
    def create_repair_packet_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.create_repair_packet_atomic(**kwargs)

    def get_repair_packet(self, repair_packet_id: str) -> dict[str, Any]:
        return self.s6.get_repair_packet(repair_packet_id)

    def get_repair_packet_for_cycle(self, cycle_id: str) -> dict[str, Any]:
        return self.s6.get_repair_packet_for_cycle(cycle_id)

    def list_repair_packets(self, source_run_id: str | None = None) -> list[dict[str, Any]]:
        return self.s6.list_repair_packets(source_run_id=source_run_id)

    # —— Internals ——
'''
text = replace_once(text, anchor, wrappers, label="repair store wrappers")
path.write_text(text, encoding="utf-8")

# Schema assertions advance to v6.
for name in (
    "tests/test_stage6_execution.py",
    "tests/test_stage6_redteam_round2.py",
    "tests/test_stage7_packet7a_contract.py",
    "tests/test_stage7_review_authority.py",
    "tests/test_stage7_review_execution.py",
):
    p = ROOT / name
    if not p.exists():
        continue
    t = p.read_text(encoding="utf-8")
    t = t.replace('self.assertEqual(SCHEMA_VERSION, 5)', 'self.assertEqual(SCHEMA_VERSION, 6)')
    t = t.replace('self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 5)', 'self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 6)')
    t = t.replace('self.assertEqual(p["schema_version"], 5)', 'self.assertEqual(p["schema_version"], 6)')
    p.write_text(t, encoding="utf-8")

# Adversarial tests.
test = r'''from __future__ import annotations

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
                    (finding["finding_id"], finding["report_id"], self.cycle["cycle_id"], finding["assignment_id"], finding["severity"], finding["category"], 1 if finding["blocking"] else 0, finding["finding_fingerprint"], dumps(finding), "now"),
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
        self.assertEqual(SCHEMA_VERSION, 6)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 6)

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
'''
(ROOT / "tests" / "test_stage7_packet7d_repair_authority.py").write_text(test, encoding="utf-8")

# Permanent source contract.
contract = r'''from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Stage7Packet7DContractTests(unittest.TestCase):
    def test_repair_service_has_no_unrestricted_run_write(self):
        source = Path("buildforme/repair_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"save_run", "save_run_for_setup", "save_run_legacy_json"}:
                    forbidden.append((node.func.attr, node.lineno))
        self.assertEqual(forbidden, [])

    def test_storage_owns_repair_packet_validation(self):
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn("validate_repair_packet_for_storage", source)
        self.assertIn("source cycle may create only one", source)
        self.assertIn("stage7_repair_packet_id", source)


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_stage7_packet7d_contract.py").write_text(contract, encoding="utf-8")

# Documentation.
path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n## Packet 7D-A — governed repair-packet authority\n\n- Exactly one append-only repair packet may be created from a finalized `repair_required` cycle.\n- SQLite independently binds the source run, execution evidence, scope, Constitution, aggregate, every report fingerprint, and every persisted blocking finding.\n- Allowed and forbidden files are copied exactly from the source execution packet; callers cannot expand repair scope.\n- A provider that participated in the source review cannot author the repair. The selected repair provider becomes the implementer identity and is excluded from the next independent review.\n- Packet creation only establishes immutable repair authority. The seed-commit and child-run admission seam remains the next Packet 7D implementation slice.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7D repair authority applied")
