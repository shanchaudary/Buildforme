from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Stage 7 review contracts
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "review_contracts.py").write_text(
    r'''"""Stage 7 independent-review schemas, fingerprints, and deterministic aggregation.

Reviewers may report findings. They may not mutate execution authority, view other
reviewers' reports before submission, accept their own implementation, merge, or deploy.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from buildforme.storage import utc_now_iso

REVIEW_CYCLE_SCHEMA = "buildforme.review_cycle.v1"
REVIEW_ASSIGNMENT_SCHEMA = "buildforme.review_assignment.v1"
REVIEW_REPORT_SCHEMA = "buildforme.review_report.v1"
REVIEW_AGGREGATE_SCHEMA = "buildforme.review_aggregate.v1"

SEVERITIES = ("critical", "high", "medium", "low", "info")
VERDICTS = frozenset({"pass", "changes_required", "block"})
FINAL_CYCLE_STATUSES = frozenset({"clear", "repair_required", "blocked"})
ACTIVE_CYCLE_STATUSES = frozenset({"open", "collecting", "ready_to_aggregate"})


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=lambda x: str(x))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint(schema: str, material: dict[str, Any]) -> str:
    raw = json.dumps(
        {"schema": schema, "material": _canonical(material)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_reviewers(
    reviewers: list[dict[str, Any]],
    *,
    implementer_provider_id: str,
    min_reviewers: int = 2,
    max_reviewers: int = 4,
) -> list[dict[str, str]]:
    if not isinstance(reviewers, list):
        raise ValueError("reviewers must be a list")
    if len(reviewers) < min_reviewers or len(reviewers) > max_reviewers:
        raise ValueError(f"reviewers must contain {min_reviewers}..{max_reviewers} entries")
    normalized: list[dict[str, str]] = []
    seen_reviewers: set[str] = set()
    seen_providers: set[str] = set()
    implementer = str(implementer_provider_id or "").strip().lower()
    for index, item in enumerate(reviewers):
        if not isinstance(item, dict):
            raise ValueError(f"reviewer {index} must be an object")
        reviewer_id = str(item.get("reviewer_id") or item.get("id") or "").strip()
        provider_id = str(item.get("provider_id") or "").strip().lower()
        role = str(item.get("role") or "general").strip().lower()
        if not reviewer_id or not provider_id or not role:
            raise ValueError(f"reviewer {index} requires reviewer_id, provider_id, and role")
        if provider_id == implementer:
            raise ValueError("implementer provider cannot review its own execution")
        if reviewer_id in seen_reviewers:
            raise ValueError(f"duplicate reviewer_id: {reviewer_id}")
        if provider_id in seen_providers:
            raise ValueError(f"duplicate reviewer provider: {provider_id}")
        seen_reviewers.add(reviewer_id)
        seen_providers.add(provider_id)
        normalized.append(
            {"reviewer_id": reviewer_id, "provider_id": provider_id, "role": role}
        )
    return sorted(normalized, key=lambda r: (r["provider_id"], r["reviewer_id"]))


def build_review_cycle_record(
    *,
    run: dict[str, Any],
    evidence: dict[str, Any],
    reviewers: list[dict[str, Any]],
    actor: str,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = normalize_reviewers(
        reviewers,
        implementer_provider_id=str(run.get("provider_id") or ""),
    )
    policy_record = {
        "required_reviewer_count": len(normalized),
        "min_distinct_providers": len(normalized),
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
        **(policy or {}),
    }
    required = int(policy_record.get("required_reviewer_count") or len(normalized))
    distinct = int(policy_record.get("min_distinct_providers") or len(normalized))
    if required < 2 or required > len(normalized):
        raise ValueError("required_reviewer_count must be between 2 and assigned reviewers")
    if distinct < 2 or distinct > required:
        raise ValueError("min_distinct_providers must be between 2 and required reviewers")
    now = utc_now_iso()
    cycle_id = f"rc-{uuid.uuid4().hex[:16]}"
    authority = {
        "run_id": run.get("id"),
        "evidence_id": evidence.get("evidence_id"),
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "scope_fingerprint": run.get("scope_fingerprint"),
        "constitution_hash": run.get("constitution_hash"),
        "constitution_lease_id": run.get("constitution_lease_id"),
        "implementer_provider_id": run.get("provider_id"),
        "reviewers": normalized,
        "policy": policy_record,
    }
    cycle = {
        "schema": REVIEW_CYCLE_SCHEMA,
        "id": cycle_id,
        "cycle_id": cycle_id,
        **authority,
        "status": "open",
        "required_reviewer_count": required,
        "min_distinct_providers": distinct,
        "aggregate": None,
        "created_by": str(actor or "shan"),
        "created_at": now,
        "updated_at": now,
        "finalized_at": None,
        "row_version": 1,
    }
    cycle["cycle_fingerprint"] = _fingerprint(REVIEW_CYCLE_SCHEMA, authority)
    assignments: list[dict[str, Any]] = []
    for reviewer in normalized:
        assignment_id = f"ra-{uuid.uuid4().hex[:16]}"
        assignment_authority = {
            "assignment_id": assignment_id,
            "cycle_id": cycle_id,
            "run_id": run.get("id"),
            "evidence_id": evidence.get("evidence_id"),
            "evidence_fingerprint": evidence.get("evidence_fingerprint"),
            **reviewer,
            "blind": True,
        }
        assignment = {
            "schema": REVIEW_ASSIGNMENT_SCHEMA,
            **assignment_authority,
            "id": assignment_id,
            "status": "pending",
            "created_at": now,
            "submitted_at": None,
            "report_id": None,
        }
        assignment["assignment_fingerprint"] = _fingerprint(
            REVIEW_ASSIGNMENT_SCHEMA, assignment_authority
        )
        assignments.append(assignment)
    return cycle, assignments


def validate_cycle_record(cycle: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not isinstance(cycle, dict):
        return ["review cycle must be an object"]
    for field in (
        "cycle_id",
        "run_id",
        "evidence_id",
        "evidence_fingerprint",
        "scope_fingerprint",
        "constitution_hash",
        "implementer_provider_id",
        "reviewers",
        "policy",
        "cycle_fingerprint",
    ):
        if cycle.get(field) in (None, ""):
            problems.append(f"review cycle missing {field}")
    reviewers = cycle.get("reviewers") if isinstance(cycle.get("reviewers"), list) else []
    try:
        normalized = normalize_reviewers(
            reviewers,
            implementer_provider_id=str(cycle.get("implementer_provider_id") or ""),
        )
    except ValueError as exc:
        problems.append(str(exc))
        normalized = []
    authority = {
        "run_id": cycle.get("run_id"),
        "evidence_id": cycle.get("evidence_id"),
        "evidence_fingerprint": cycle.get("evidence_fingerprint"),
        "scope_fingerprint": cycle.get("scope_fingerprint"),
        "constitution_hash": cycle.get("constitution_hash"),
        "constitution_lease_id": cycle.get("constitution_lease_id"),
        "implementer_provider_id": cycle.get("implementer_provider_id"),
        "reviewers": normalized,
        "policy": cycle.get("policy") if isinstance(cycle.get("policy"), dict) else {},
    }
    expected = _fingerprint(REVIEW_CYCLE_SCHEMA, authority)
    if cycle.get("cycle_fingerprint") != expected:
        problems.append("review cycle fingerprint mismatch")
    return problems


def validate_assignment_record(assignment: dict[str, Any], cycle: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in (
        "assignment_id",
        "cycle_id",
        "run_id",
        "evidence_id",
        "evidence_fingerprint",
        "reviewer_id",
        "provider_id",
        "role",
        "assignment_fingerprint",
    ):
        if assignment.get(field) in (None, ""):
            problems.append(f"review assignment missing {field}")
    if assignment.get("cycle_id") != cycle.get("cycle_id"):
        problems.append("review assignment cycle mismatch")
    if assignment.get("run_id") != cycle.get("run_id"):
        problems.append("review assignment run mismatch")
    if assignment.get("provider_id") == cycle.get("implementer_provider_id"):
        problems.append("implementer provider cannot hold reviewer assignment")
    authority = {
        "assignment_id": assignment.get("assignment_id"),
        "cycle_id": assignment.get("cycle_id"),
        "run_id": assignment.get("run_id"),
        "evidence_id": assignment.get("evidence_id"),
        "evidence_fingerprint": assignment.get("evidence_fingerprint"),
        "reviewer_id": assignment.get("reviewer_id"),
        "provider_id": assignment.get("provider_id"),
        "role": assignment.get("role"),
        "blind": bool(assignment.get("blind")),
    }
    if assignment.get("assignment_fingerprint") != _fingerprint(
        REVIEW_ASSIGNMENT_SCHEMA, authority
    ):
        problems.append("review assignment fingerprint mismatch")
    return problems


def _build_finding(
    *,
    cycle: dict[str, Any],
    assignment: dict[str, Any],
    finding: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    severity = str(finding.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        raise ValueError(f"finding {index} severity must be one of {SEVERITIES}")
    summary = str(finding.get("summary") or "").strip()
    evidence = str(finding.get("evidence") or "").strip()
    recommendation = str(finding.get("recommendation") or "").strip()
    category = str(finding.get("category") or "general").strip().lower()
    if not summary:
        raise ValueError(f"finding {index} summary required")
    if severity in {"critical", "high"} and not evidence:
        raise ValueError(f"finding {index} critical/high severity requires evidence")
    blocking = bool(finding.get("blocking")) or severity in {"critical", "high"}
    finding_id = str(finding.get("finding_id") or f"rf-{uuid.uuid4().hex[:16]}")
    material = {
        "finding_id": finding_id,
        "cycle_id": cycle.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "reviewer_id": assignment.get("reviewer_id"),
        "provider_id": assignment.get("provider_id"),
        "severity": severity,
        "category": category,
        "blocking": blocking,
        "summary": summary,
        "evidence": evidence,
        "recommendation": recommendation,
        "file": finding.get("file"),
        "line": finding.get("line"),
        "law_ids": sorted(str(x) for x in (finding.get("law_ids") or [])),
    }
    return {
        **material,
        "finding_fingerprint": _fingerprint("buildforme.review_finding.v1", material),
        "immutable": True,
    }


def build_review_report_record(
    *,
    cycle: dict[str, Any],
    assignment: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if str(assignment.get("status") or "") != "pending":
        raise ValueError("review assignment is not pending")
    if any(key in payload for key in ("other_reviews", "consensus", "merge_allowed", "founder_decision")):
        raise ValueError("review report contains forbidden authority or non-blind context")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"review verdict must be one of {sorted(VERDICTS)}")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("review findings must be a list")
    findings = [
        _build_finding(cycle=cycle, assignment=assignment, finding=item, index=index)
        for index, item in enumerate(raw_findings)
        if isinstance(item, dict)
    ]
    if len(findings) != len(raw_findings):
        raise ValueError("every review finding must be an object")
    blocking = [f for f in findings if f.get("blocking")]
    if verdict == "pass" and blocking:
        raise ValueError("pass verdict cannot contain blocking findings")
    if verdict in {"changes_required", "block"} and not findings:
        raise ValueError(f"{verdict} verdict requires at least one finding")
    if verdict == "block" and not blocking:
        raise ValueError("block verdict requires at least one blocking finding")
    report_id = str(payload.get("report_id") or f"rr-{uuid.uuid4().hex[:16]}")
    material = {
        "report_id": report_id,
        "cycle_id": cycle.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "run_id": cycle.get("run_id"),
        "reviewer_id": assignment.get("reviewer_id"),
        "provider_id": assignment.get("provider_id"),
        "role": assignment.get("role"),
        "reviewed_evidence_id": cycle.get("evidence_id"),
        "reviewed_evidence_fingerprint": cycle.get("evidence_fingerprint"),
        "scope_fingerprint": cycle.get("scope_fingerprint"),
        "constitution_hash": cycle.get("constitution_hash"),
        "verdict": verdict,
        "summary": str(payload.get("summary") or "").strip(),
        "findings": findings,
        "blind_review": True,
        "provider_may_self_accept": False,
    }
    report = {
        "schema": REVIEW_REPORT_SCHEMA,
        **material,
        "created_at": utc_now_iso(),
        "immutable": True,
    }
    report["report_fingerprint"] = _fingerprint(REVIEW_REPORT_SCHEMA, material)
    return report, findings


def validate_report_for_storage(
    report: dict[str, Any],
    cycle: dict[str, Any],
    assignment: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    for field in (
        "report_id",
        "cycle_id",
        "assignment_id",
        "run_id",
        "reviewer_id",
        "provider_id",
        "reviewed_evidence_id",
        "reviewed_evidence_fingerprint",
        "scope_fingerprint",
        "constitution_hash",
        "verdict",
        "findings",
        "report_fingerprint",
    ):
        if report.get(field) in (None, ""):
            problems.append(f"review report missing {field}")
    for field in ("cycle_id", "run_id", "scope_fingerprint", "constitution_hash"):
        if str(report.get(field) or "") != str(cycle.get(field) or ""):
            problems.append(f"review report {field} mismatch")
    if report.get("assignment_id") != assignment.get("assignment_id"):
        problems.append("review report assignment mismatch")
    if report.get("reviewer_id") != assignment.get("reviewer_id"):
        problems.append("review report reviewer mismatch")
    if report.get("provider_id") != assignment.get("provider_id"):
        problems.append("review report provider mismatch")
    if report.get("reviewed_evidence_id") != cycle.get("evidence_id"):
        problems.append("review report evidence id mismatch")
    if report.get("reviewed_evidence_fingerprint") != cycle.get("evidence_fingerprint"):
        problems.append("review report evidence fingerprint mismatch")
    material = {
        key: report.get(key)
        for key in (
            "report_id",
            "cycle_id",
            "assignment_id",
            "run_id",
            "reviewer_id",
            "provider_id",
            "role",
            "reviewed_evidence_id",
            "reviewed_evidence_fingerprint",
            "scope_fingerprint",
            "constitution_hash",
            "verdict",
            "summary",
            "findings",
            "blind_review",
            "provider_may_self_accept",
        )
    }
    if report.get("report_fingerprint") != _fingerprint(REVIEW_REPORT_SCHEMA, material):
        problems.append("review report fingerprint mismatch")
    return problems


def aggregate_review_reports(
    *,
    cycle: dict[str, Any],
    assignments: list[dict[str, Any]],
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    required = int(cycle.get("required_reviewer_count") or 0)
    submitted = [a for a in assignments if a.get("status") == "submitted"]
    if len(submitted) < required or len(reports) < required:
        raise ValueError("review quorum not met")
    provider_ids = sorted({str(r.get("provider_id") or "") for r in reports})
    if len(provider_ids) < int(cycle.get("min_distinct_providers") or required):
        raise ValueError("distinct provider quorum not met")
    implementer = str(cycle.get("implementer_provider_id") or "")
    if implementer in provider_ids:
        raise ValueError("implementer provider appears in independent review quorum")
    findings = [f for report in reports for f in (report.get("findings") or [])]
    blocking = [f for f in findings if f.get("blocking")]
    verdicts = [str(r.get("verdict") or "") for r in reports]
    disagreement = len(set(verdicts)) > 1
    status = "repair_required" if blocking or any(v != "pass" for v in verdicts) else "clear"
    material = {
        "cycle_id": cycle.get("cycle_id"),
        "run_id": cycle.get("run_id"),
        "evidence_id": cycle.get("evidence_id"),
        "evidence_fingerprint": cycle.get("evidence_fingerprint"),
        "scope_fingerprint": cycle.get("scope_fingerprint"),
        "constitution_hash": cycle.get("constitution_hash"),
        "status": status,
        "quorum_met": True,
        "required_reviewer_count": required,
        "submitted_reviewer_count": len(reports),
        "distinct_provider_count": len(provider_ids),
        "provider_ids": provider_ids,
        "verdicts": sorted(verdicts),
        "blocking_finding_ids": sorted(str(f.get("finding_id")) for f in blocking),
        "finding_count": len(findings),
        "blocking_finding_count": len(blocking),
        "disagreement": disagreement,
        "report_fingerprints": sorted(str(r.get("report_fingerprint")) for r in reports),
    }
    aggregate = {
        "schema": REVIEW_AGGREGATE_SCHEMA,
        **material,
        "created_at": utc_now_iso(),
        "founder_may_override_blocking_findings": False,
    }
    aggregate["aggregate_fingerprint"] = _fingerprint(REVIEW_AGGREGATE_SCHEMA, material)
    return aggregate
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# Stage 7 service
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "review_service.py").write_text(
    r'''"""Stage 7 independent multi-agent review authority.

Packet 7A establishes immutable review cycles, blind assignments, append-only
reports/findings, deterministic aggregation, and founder acceptance gating.
"""

from __future__ import annotations

from typing import Any

from buildforme.evidence import validate_evidence_for_storage
from buildforme.governance import compute_run_scope_fingerprint, validate_actor, validate_safe_id
from buildforme.review_contracts import (
    aggregate_review_reports,
    build_review_cycle_record,
    build_review_report_record,
)
from buildforme.storage import LocalStore


def _require_reviewable_run(store: LocalStore, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    if str(run.get("execution_mode") or run.get("mode")) != "live_supervised":
        raise ValueError("independent review requires live_supervised execution evidence")
    if str(run.get("status") or "") != "needs_review":
        raise ValueError(f"independent review requires needs_review status, got {run.get('status')}")
    stored_scope = str(run.get("scope_fingerprint") or "")
    computed_scope = compute_run_scope_fingerprint(
        run, run.get("packet") if isinstance(run.get("packet"), dict) else None
    )
    if not stored_scope or stored_scope != computed_scope:
        raise ValueError("run scope fingerprint is missing or stale")
    evidence = store.get_latest_execution_evidence(str(run.get("id")))
    problems = validate_evidence_for_storage(evidence)
    if problems:
        raise ValueError("execution evidence invalid: " + "; ".join(problems))
    if str(evidence.get("run_id") or "") != str(run.get("id") or ""):
        raise ValueError("execution evidence run mismatch")
    if str(evidence.get("constitution", {}).get("hash") or "") != str(
        run.get("constitution_hash") or ""
    ):
        raise ValueError("execution evidence Constitution mismatch")
    verification = evidence.get("verification") if isinstance(evidence.get("verification"), dict) else {}
    if not verification.get("passed"):
        raise ValueError("deterministic verification must pass before independent review")
    return run, evidence


def create_independent_review_cycle(
    store: LocalStore,
    run_id: str,
    *,
    reviewers: list[dict[str, Any]],
    actor: str = "shan",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run, evidence = _require_reviewable_run(store, run_id)
    actor = validate_actor(actor)
    known = {str(p.get("provider_id")): p for p in store.list_providers()}
    for reviewer in reviewers:
        provider_id = str((reviewer or {}).get("provider_id") or "")
        provider = known.get(provider_id)
        if not provider:
            raise ValueError(f"unknown reviewer provider: {provider_id}")
        if not provider.get("enabled", True):
            raise ValueError(f"reviewer provider disabled: {provider_id}")
    cycle, assignments = build_review_cycle_record(
        run=run,
        evidence=evidence,
        reviewers=reviewers,
        actor=actor,
        policy=policy,
    )
    return store.create_review_cycle_atomic(cycle=cycle, assignments=assignments, actor=actor)


def submit_independent_review_report(
    store: LocalStore,
    cycle_id: str,
    assignment_id: str,
    *,
    payload: dict[str, Any],
    actor: str = "reviewer",
) -> dict[str, Any]:
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignment = store.get_review_assignment(
        validate_safe_id(assignment_id, field="assignment_id")
    )
    if assignment.get("cycle_id") != cycle.get("cycle_id"):
        raise ValueError("assignment does not belong to review cycle")
    report, findings = build_review_report_record(
        cycle=cycle,
        assignment=assignment,
        payload=payload,
    )
    return store.submit_review_report_atomic(
        cycle_id=cycle_id,
        assignment_id=assignment_id,
        report=report,
        findings=findings,
        actor=str(actor or assignment.get("reviewer_id") or "reviewer"),
    )


def aggregate_independent_review_cycle(
    store: LocalStore,
    cycle_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignments = store.list_review_assignments(cycle_id)
    reports = store.list_review_reports(cycle_id)
    aggregate = aggregate_review_reports(
        cycle=cycle,
        assignments=assignments,
        reports=reports,
    )
    return store.finalize_review_cycle_atomic(
        cycle_id=cycle_id,
        expected_row_version=int(cycle.get("row_version") or 1),
        aggregate=aggregate,
        actor=validate_actor(actor),
    )


def require_clear_independent_review(store: LocalStore, run: dict[str, Any]) -> dict[str, Any]:
    if not run.get("stage7_review_required"):
        return {"required": False, "status": "not_required"}
    cycle_id = str(run.get("stage7_review_cycle_id") or "")
    review = run.get("independent_review") if isinstance(run.get("independent_review"), dict) else {}
    if not cycle_id or review.get("status") != "clear":
        raise ValueError("founder acceptance requires a clear Stage 7 independent review cycle")
    cycle = store.get_review_cycle(cycle_id)
    if cycle.get("status") != "clear":
        raise ValueError("bound Stage 7 review cycle is not clear")
    evidence = store.get_latest_execution_evidence(str(run.get("id")))
    for field, cycle_field in (
        ("evidence_id", "evidence_id"),
        ("evidence_fingerprint", "evidence_fingerprint"),
    ):
        if str(cycle.get(cycle_field) or "") != str(evidence.get(field) or ""):
            raise ValueError(f"Stage 7 review cycle {field} is stale")
    if str(cycle.get("scope_fingerprint") or "") != str(run.get("scope_fingerprint") or ""):
        raise ValueError("Stage 7 review cycle scope is stale")
    if str(cycle.get("constitution_hash") or "") != str(run.get("constitution_hash") or ""):
        raise ValueError("Stage 7 review cycle Constitution is stale")
    aggregate = cycle.get("aggregate") if isinstance(cycle.get("aggregate"), dict) else {}
    if aggregate.get("blocking_finding_count"):
        raise ValueError("Stage 7 review contains blocking findings")
    if not aggregate.get("quorum_met"):
        raise ValueError("Stage 7 review quorum is not met")
    return cycle
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# SQLite schema v4
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "db.py"
text = path.read_text(encoding="utf-8")
text = replace_once(text, "SCHEMA_VERSION = 3", "SCHEMA_VERSION = 4", label="schema version")
old_schema_tail = '''CREATE TABLE IF NOT EXISTS provider_compat_cache (
  provider_id TEXT PRIMARY KEY,
  executable TEXT,
  version_text TEXT,
  profile_json TEXT NOT NULL,
  live_ready INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL,
  expires_at_epoch INTEGER NOT NULL
);
"""
'''
new_schema_tail = '''CREATE TABLE IF NOT EXISTS provider_compat_cache (
  provider_id TEXT PRIMARY KEY,
  executable TEXT,
  version_text TEXT,
  profile_json TEXT NOT NULL,
  live_ready INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL,
  expires_at_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS review_cycles (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  evidence_fingerprint TEXT NOT NULL,
  scope_fingerprint TEXT NOT NULL,
  constitution_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  required_reviewer_count INTEGER NOT NULL,
  min_distinct_providers INTEGER NOT NULL,
  policy_json TEXT NOT NULL,
  aggregate_json TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finalized_at TEXT,
  row_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_review_cycles_run ON review_cycles(run_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_cycles_active_run
  ON review_cycles(run_id) WHERE status IN ('open','collecting','ready_to_aggregate');

CREATE TABLE IF NOT EXISTS review_assignments (
  id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
  reviewer_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,
  blind INTEGER NOT NULL DEFAULT 1,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  UNIQUE(cycle_id, reviewer_id),
  UNIQUE(cycle_id, provider_id)
);

CREATE TABLE IF NOT EXISTS review_reports (
  report_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
  assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
  verdict TEXT NOT NULL,
  report_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1,
  UNIQUE(assignment_id)
);

CREATE TABLE IF NOT EXISTS review_findings (
  finding_id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES review_reports(report_id),
  cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
  assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
  severity TEXT NOT NULL,
  category TEXT NOT NULL,
  blocking INTEGER NOT NULL DEFAULT 0,
  finding_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_review_findings_cycle ON review_findings(cycle_id, created_at);

CREATE TABLE IF NOT EXISTS review_events (
  id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
  event_type TEXT NOT NULL,
  summary TEXT,
  actor TEXT,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_events_cycle ON review_events(cycle_id, created_at);
"""
'''
text = replace_once(text, old_schema_tail, new_schema_tail, label="schema tables")
text = replace_once(
    text,
    '''                    if current < 3:
                        self._migrate_to_v3(conn)
                    if current < SCHEMA_VERSION:
''',
    '''                    if current < 3:
                        self._migrate_to_v3(conn)
                    if current < 4:
                        self._migrate_to_v4(conn)
                    if current < SCHEMA_VERSION:
''',
    label="schema migration dispatch",
)
insert_before = '''    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
migration_method = r'''    def _migrate_to_v4(self, conn: sqlite3.Connection) -> None:
        """Add Stage 7 immutable independent-review authority."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS review_cycles (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL REFERENCES runs(id),
              evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
              evidence_fingerprint TEXT NOT NULL,
              scope_fingerprint TEXT NOT NULL,
              constitution_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              required_reviewer_count INTEGER NOT NULL,
              min_distinct_providers INTEGER NOT NULL,
              policy_json TEXT NOT NULL,
              aggregate_json TEXT,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              finalized_at TEXT,
              row_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_review_cycles_run ON review_cycles(run_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_review_cycles_active_run
              ON review_cycles(run_id) WHERE status IN ('open','collecting','ready_to_aggregate');
            CREATE TABLE IF NOT EXISTS review_assignments (
              id TEXT PRIMARY KEY,
              cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
              reviewer_id TEXT NOT NULL,
              provider_id TEXT NOT NULL,
              role TEXT NOT NULL,
              status TEXT NOT NULL,
              blind INTEGER NOT NULL DEFAULT 1,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              submitted_at TEXT,
              UNIQUE(cycle_id, reviewer_id),
              UNIQUE(cycle_id, provider_id)
            );
            CREATE TABLE IF NOT EXISTS review_reports (
              report_id TEXT PRIMARY KEY,
              cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
              assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
              verdict TEXT NOT NULL,
              report_fingerprint TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1,
              UNIQUE(assignment_id)
            );
            CREATE TABLE IF NOT EXISTS review_findings (
              finding_id TEXT PRIMARY KEY,
              report_id TEXT NOT NULL REFERENCES review_reports(report_id),
              cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
              assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
              severity TEXT NOT NULL,
              category TEXT NOT NULL,
              blocking INTEGER NOT NULL DEFAULT 0,
              finding_fingerprint TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_review_findings_cycle ON review_findings(cycle_id, created_at);
            CREATE TABLE IF NOT EXISTS review_events (
              id TEXT PRIMARY KEY,
              cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
              event_type TEXT NOT NULL,
              summary TEXT,
              actor TEXT,
              metadata_json TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_review_events_cycle ON review_events(cycle_id, created_at);
            """
        )

'''
text = replace_once(text, insert_before, migration_method + insert_before, label="v4 migration method")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stage 7 storage methods
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
insert_anchor = '''    # —— Evidence ——
    def save_run_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
'''
storage_methods = r'''    # —— Stage 7 independent reviews ——
    def create_review_cycle_atomic(
        self,
        *,
        cycle: dict[str, Any],
        assignments: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import validate_assignment_record, validate_cycle_record

        cycle_record = dict(cycle)
        problems = validate_cycle_record(cycle_record)
        if problems:
            raise ValueError("review cycle rejected: " + "; ".join(problems))
        assignment_records = [dict(item) for item in assignments]
        for item in assignment_records:
            problems = validate_assignment_record(item, cycle_record)
            if problems:
                raise ValueError("review assignment rejected: " + "; ".join(problems))
        cycle_id = str(cycle_record["cycle_id"])
        run_id = str(cycle_record["run_id"])
        now = utc_now_iso()
        with self.db.transaction() as conn:
            run_row = conn.execute(
                "SELECT row_version, payload_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run not found: {run_id}")
            run = loads(run_row[1], {})
            if str(run.get("status") or "") != "needs_review":
                raise ValueError("review cycle requires run status needs_review")
            evidence_row = conn.execute(
                "SELECT run_id, evidence_fingerprint FROM evidence WHERE evidence_id=?",
                (str(cycle_record["evidence_id"]),),
            ).fetchone()
            if not evidence_row:
                raise ValueError("bound execution evidence not found")
            if str(evidence_row[0]) != run_id:
                raise ValueError("bound execution evidence belongs to another run")
            if str(evidence_row[1] or "") != str(cycle_record["evidence_fingerprint"]):
                raise ValueError("bound execution evidence fingerprint mismatch")
            if conn.execute(
                "SELECT id FROM review_cycles WHERE run_id=? AND status IN ('open','collecting','ready_to_aggregate')",
                (run_id,),
            ).fetchone():
                raise ValueError("an active independent review cycle already exists for this run")
            conn.execute(
                """INSERT INTO review_cycles(
                   id, run_id, evidence_id, evidence_fingerprint, scope_fingerprint,
                   constitution_hash, status, required_reviewer_count, min_distinct_providers,
                   policy_json, aggregate_json, payload_json, created_at, updated_at,
                   finalized_at, row_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,1)""",
                (
                    cycle_id,
                    run_id,
                    cycle_record["evidence_id"],
                    cycle_record["evidence_fingerprint"],
                    cycle_record["scope_fingerprint"],
                    cycle_record["constitution_hash"],
                    "open",
                    int(cycle_record["required_reviewer_count"]),
                    int(cycle_record["min_distinct_providers"]),
                    dumps(cycle_record.get("policy") or {}),
                    None,
                    dumps(cycle_record),
                    cycle_record.get("created_at") or now,
                    now,
                ),
            )
            for assignment in assignment_records:
                conn.execute(
                    """INSERT INTO review_assignments(
                       id, cycle_id, reviewer_id, provider_id, role, status, blind,
                       payload_json, created_at, submitted_at)
                       VALUES (?,?,?,?,?,'pending',1,?,?,NULL)""",
                    (
                        assignment["assignment_id"],
                        cycle_id,
                        assignment["reviewer_id"],
                        assignment["provider_id"],
                        assignment["role"],
                        dumps(assignment),
                        assignment.get("created_at") or now,
                    ),
                )
            run["stage7_review_required"] = True
            run["stage7_review_cycle_id"] = cycle_id
            run["independent_review"] = {
                "cycle_id": cycle_id,
                "status": "collecting",
                "quorum_met": False,
                "evidence_id": cycle_record["evidence_id"],
                "evidence_fingerprint": cycle_record["evidence_fingerprint"],
                "required_reviewer_count": cycle_record["required_reviewer_count"],
            }
            run["updated_at"] = now
            new_run_version = int(run_row[0] or 1) + 1
            run["row_version"] = new_run_version
            cur = conn.execute(
                "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (dumps(run), now, new_run_version, run_id, int(run_row[0] or 1)),
            )
            if cur.rowcount != 1:
                raise ValueError("stale run race while binding review cycle")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_cycle_created", "Blind independent review cycle created", actor, dumps({"assignment_count": len(assignment_records)}), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), run_id, "stage7_review_cycle_created", "Stage 7 independent review required", actor, dumps({"cycle_id": cycle_id}), now),
            )
        return {"cycle": cycle_record, "assignments": assignment_records, "run": run}

    def get_review_cycle(self, cycle_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json, aggregate_json, status, row_version, finalized_at FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Review cycle not found: {cycle_id}")
        record = loads(row[0], {})
        record["status"] = row[2]
        record["row_version"] = int(row[3] or 1)
        record["finalized_at"] = row[4]
        record["aggregate"] = loads(row[1], None) if row[1] else record.get("aggregate")
        return record

    def list_review_cycles(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT id FROM review_cycles WHERE run_id=? ORDER BY created_at DESC",
                    (str(run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM review_cycles ORDER BY created_at DESC"
                ).fetchall()
        return [self.get_review_cycle(str(row[0])) for row in rows]

    def get_review_assignment(self, assignment_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json, status, submitted_at FROM review_assignments WHERE id=?",
                (str(assignment_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Review assignment not found: {assignment_id}")
        record = loads(row[0], {})
        record["status"] = row[1]
        record["submitted_at"] = row[2]
        return record

    def list_review_assignments(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM review_assignments WHERE cycle_id=? ORDER BY provider_id, reviewer_id",
                (str(cycle_id),),
            ).fetchall()
        return [self.get_review_assignment(str(row[0])) for row in rows]

    def list_review_reports(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM review_reports WHERE cycle_id=? ORDER BY created_at, report_id",
                (str(cycle_id),),
            ).fetchall()
        return [loads(row[0], {}) for row in rows]

    def list_review_findings(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM review_findings WHERE cycle_id=? ORDER BY created_at, finding_id",
                (str(cycle_id),),
            ).fetchall()
        return [loads(row[0], {}) for row in rows]

    def submit_review_report_atomic(
        self,
        *,
        cycle_id: str,
        assignment_id: str,
        report: dict[str, Any],
        findings: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import validate_report_for_storage

        now = utc_now_iso()
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, status, required_reviewer_count, payload_json, row_version FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
            if not cycle_row:
                raise KeyError(f"Review cycle not found: {cycle_id}")
            if str(cycle_row[1]) not in {"open", "collecting"}:
                raise ValueError("review cycle is not accepting reports")
            cycle = loads(cycle_row[3], {})
            cycle["status"] = cycle_row[1]
            cycle["row_version"] = int(cycle_row[4] or 1)
            assignment_row = conn.execute(
                "SELECT cycle_id, status, payload_json FROM review_assignments WHERE id=?",
                (str(assignment_id),),
            ).fetchone()
            if not assignment_row:
                raise KeyError(f"Review assignment not found: {assignment_id}")
            if str(assignment_row[0]) != str(cycle_id):
                raise ValueError("review assignment cycle mismatch")
            if str(assignment_row[1]) != "pending":
                raise ValueError("review assignment already submitted or unavailable")
            assignment = loads(assignment_row[2], {})
            assignment["status"] = assignment_row[1]
            problems = validate_report_for_storage(report, cycle, assignment)
            if problems:
                raise ValueError("review report rejected: " + "; ".join(problems))
            report_id = str(report["report_id"])
            if conn.execute(
                "SELECT report_id FROM review_reports WHERE report_id=? OR assignment_id=?",
                (report_id, str(assignment_id)),
            ).fetchone():
                raise ValueError("review report is append-only and assignment may submit only once")
            conn.execute(
                "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
                (report_id, cycle_id, assignment_id, report["verdict"], report["report_fingerprint"], dumps(report), report.get("created_at") or now),
            )
            for finding in findings:
                finding_id = str(finding["finding_id"])
                if conn.execute(
                    "SELECT finding_id FROM review_findings WHERE finding_id=?", (finding_id,)
                ).fetchone():
                    raise ValueError(f"review finding mutation forbidden: {finding_id}")
                conn.execute(
                    "INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (finding_id, report_id, cycle_id, assignment_id, finding["severity"], finding["category"], 1 if finding.get("blocking") else 0, finding["finding_fingerprint"], dumps(finding), now),
                )
            assignment["status"] = "submitted"
            assignment["submitted_at"] = now
            assignment["report_id"] = report_id
            assignment["report_fingerprint"] = report["report_fingerprint"]
            conn.execute(
                "UPDATE review_assignments SET status='submitted', payload_json=?, submitted_at=? WHERE id=? AND status='pending'",
                (dumps(assignment), now, assignment_id),
            )
            submitted = int(
                conn.execute(
                    "SELECT COUNT(*) FROM review_assignments WHERE cycle_id=? AND status='submitted'",
                    (cycle_id,),
                ).fetchone()[0]
            )
            required = int(cycle_row[2] or 0)
            new_status = "ready_to_aggregate" if submitted >= required else "collecting"
            cycle["status"] = new_status
            cycle["submitted_reviewer_count"] = submitted
            cycle["updated_at"] = now
            new_cycle_version = int(cycle_row[4] or 1) + 1
            cycle["row_version"] = new_cycle_version
            cur = conn.execute(
                "UPDATE review_cycles SET status=?, payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (new_status, dumps(cycle), now, new_cycle_version, cycle_id, int(cycle_row[4] or 1)),
            )
            if cur.rowcount != 1:
                raise ValueError("stale review cycle race while submitting report")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_report_submitted", "Blind reviewer report submitted", actor, dumps({"assignment_id": assignment_id, "report_id": report_id, "submitted": submitted, "required": required}), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), str(cycle_row[0]), "stage7_review_report_submitted", "Independent reviewer report submitted", actor, dumps({"cycle_id": cycle_id, "assignment_id": assignment_id, "report_id": report_id}), now),
            )
        return {"cycle": cycle, "assignment": assignment, "report": report, "findings": findings}

    def finalize_review_cycle_atomic(
        self,
        *,
        cycle_id: str,
        expected_row_version: int,
        aggregate: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import aggregate_review_reports

        now = utc_now_iso()
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, status, payload_json, row_version FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
            if not cycle_row:
                raise KeyError(f"Review cycle not found: {cycle_id}")
            current_version = int(cycle_row[3] or 1)
            if current_version != int(expected_row_version):
                raise ValueError("stale review cycle aggregation rejected")
            if str(cycle_row[1]) != "ready_to_aggregate":
                raise ValueError("review cycle is not ready to aggregate")
            cycle = loads(cycle_row[2], {})
            cycle["status"] = cycle_row[1]
            cycle["row_version"] = current_version
            assignment_rows = conn.execute(
                "SELECT payload_json, status FROM review_assignments WHERE cycle_id=? ORDER BY provider_id, reviewer_id",
                (cycle_id,),
            ).fetchall()
            assignments = []
            for row in assignment_rows:
                item = loads(row[0], {})
                item["status"] = row[1]
                assignments.append(item)
            reports = [
                loads(row[0], {})
                for row in conn.execute(
                    "SELECT payload_json FROM review_reports WHERE cycle_id=? ORDER BY created_at, report_id",
                    (cycle_id,),
                ).fetchall()
            ]
            canonical = aggregate_review_reports(cycle=cycle, assignments=assignments, reports=reports)
            if canonical.get("aggregate_fingerprint") != aggregate.get("aggregate_fingerprint"):
                raise ValueError("review aggregate fingerprint mismatch")
            if canonical.get("status") != aggregate.get("status"):
                raise ValueError("review aggregate status mismatch")
            final_status = str(canonical["status"])
            cycle["status"] = final_status
            cycle["aggregate"] = canonical
            cycle["updated_at"] = now
            cycle["finalized_at"] = now
            new_cycle_version = current_version + 1
            cycle["row_version"] = new_cycle_version
            cur = conn.execute(
                "UPDATE review_cycles SET status=?, aggregate_json=?, payload_json=?, updated_at=?, finalized_at=?, row_version=? WHERE id=? AND row_version=?",
                (final_status, dumps(canonical), dumps(cycle), now, now, new_cycle_version, cycle_id, current_version),
            )
            if cur.rowcount != 1:
                raise ValueError("stale review cycle finalization race")
            run_id = str(cycle_row[0])
            run_row = conn.execute(
                "SELECT row_version, payload_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run not found: {run_id}")
            run = loads(run_row[1], {})
            if str(run.get("status") or "") != "needs_review":
                raise ValueError("review cycle can finalize only while run needs_review")
            if str(run.get("stage7_review_cycle_id") or "") != str(cycle_id):
                raise ValueError("run is not bound to this review cycle")
            run["independent_review"] = {
                "cycle_id": cycle_id,
                "status": final_status,
                "quorum_met": canonical.get("quorum_met"),
                "evidence_id": cycle.get("evidence_id"),
                "evidence_fingerprint": cycle.get("evidence_fingerprint"),
                "aggregate_fingerprint": canonical.get("aggregate_fingerprint"),
                "required_reviewer_count": canonical.get("required_reviewer_count"),
                "submitted_reviewer_count": canonical.get("submitted_reviewer_count"),
                "distinct_provider_count": canonical.get("distinct_provider_count"),
                "blocking_finding_count": canonical.get("blocking_finding_count"),
                "finding_count": canonical.get("finding_count"),
                "disagreement": canonical.get("disagreement"),
            }
            run["updated_at"] = now
            run_version = int(run_row[0] or 1)
            run["row_version"] = run_version + 1
            run_cur = conn.execute(
                "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (dumps(run), now, run_version + 1, run_id, run_version),
            )
            if run_cur.rowcount != 1:
                raise ValueError("stale run race while binding review aggregate")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_cycle_finalized", f"Independent review verdict: {final_status}", actor, dumps({"aggregate_fingerprint": canonical.get("aggregate_fingerprint")}), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), run_id, "stage7_review_finalized", f"Independent review verdict: {final_status}", actor, dumps({"cycle_id": cycle_id, "aggregate_fingerprint": canonical.get("aggregate_fingerprint")}), now),
            )
        return {"cycle": cycle, "aggregate": canonical, "run": run}

'''
text = replace_once(text, insert_anchor, storage_methods + insert_anchor, label="review storage methods")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# LocalStore wrappers
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "storage.py"
text = path.read_text(encoding="utf-8")
anchor = '''    def save_run_legacy_json(self, run: dict[str, Any]) -> dict[str, Any]:
'''
wrappers = r'''    def create_review_cycle_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.create_review_cycle_atomic(**kwargs)

    def get_review_cycle(self, cycle_id: str) -> dict[str, Any]:
        return self.s6.get_review_cycle(cycle_id)

    def list_review_cycles(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.s6.list_review_cycles(run_id=run_id)

    def get_review_assignment(self, assignment_id: str) -> dict[str, Any]:
        return self.s6.get_review_assignment(assignment_id)

    def list_review_assignments(self, cycle_id: str) -> list[dict[str, Any]]:
        return self.s6.list_review_assignments(cycle_id)

    def list_review_reports(self, cycle_id: str) -> list[dict[str, Any]]:
        return self.s6.list_review_reports(cycle_id)

    def list_review_findings(self, cycle_id: str) -> list[dict[str, Any]]:
        return self.s6.list_review_findings(cycle_id)

    def submit_review_report_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.submit_review_report_atomic(**kwargs)

    def finalize_review_cycle_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.finalize_review_cycle_atomic(**kwargs)

'''
text = replace_once(text, anchor, wrappers + anchor, label="LocalStore review wrappers")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Founder gate understands bound Stage 7 verdicts
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_gate.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    if evidence.get("process", {}).get("cleanup_ok") is False:
        blocks.append("incomplete process cleanup")

    # Deduplicate preserve order
''',
    '''    if evidence.get("process", {}).get("cleanup_ok") is False:
        blocks.append("incomplete process cleanup")

    if str(run.get("execution_mode") or run.get("mode")) == "live_supervised" and run.get(
        "stage7_review_required"
    ):
        independent = (
            run.get("independent_review")
            if isinstance(run.get("independent_review"), dict)
            else {}
        )
        if independent.get("status") != "clear":
            blocks.append("Stage 7 independent review is not clear")
        if not independent.get("quorum_met"):
            blocks.append("Stage 7 independent review quorum is not met")
        if int(independent.get("blocking_finding_count") or 0) > 0:
            blocks.append("Stage 7 independent review contains blocking findings")

    # Deduplicate preserve order
''',
    label="review gate Stage7 blocks",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Founder decision invokes storage-backed Stage 7 proof when required
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_service.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    actor = validate_actor(actor)
    current = str(run.get("status") or "")
''',
    '''    actor = validate_actor(actor)
    current = str(run.get("status") or "")
    if decision == "accept_for_pr_prep" and run.get("stage7_review_required"):
        from buildforme.review_service import require_clear_independent_review

        require_clear_independent_review(store, run)
''',
    label="founder Stage7 gate",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# API integration for Packet 7A
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "server.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.storage import DEFAULT_STATE_PATH, LocalStore
''',
    '''from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    submit_independent_review_report,
)
from buildforme.storage import DEFAULT_STATE_PATH, LocalStore
''',
    label="server review imports",
)
text = replace_once(
    text,
    '''        if path.startswith("/api/runs/") and path.endswith("/evidence"):
''',
    '''        if path.startswith("/api/runs/") and path.endswith("/reviews"):
            run_id = path.removeprefix("/api/runs/").removesuffix("/reviews").strip("/")
            self._json(HTTPStatus.OK, {"review_cycles": self._store().list_review_cycles(run_id)})
            return
        if path.startswith("/api/review-cycles/"):
            cycle_id = path.removeprefix("/api/review-cycles/").strip("/")
            try:
                self._json(
                    HTTPStatus.OK,
                    {
                        "cycle": self._store().get_review_cycle(cycle_id),
                        "assignments": self._store().list_review_assignments(cycle_id),
                        "reports": self._store().list_review_reports(cycle_id),
                        "findings": self._store().list_review_findings(cycle_id),
                    },
                )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if path.startswith("/api/runs/") and path.endswith("/evidence"):
''',
    label="server review GET routes",
)
text = replace_once(
    text,
    '''        if path.startswith("/api/runs/") and path.endswith("/preflight"):
''',
    '''        if path.startswith("/api/runs/") and path.endswith("/reviews"):
            self._stage7_review_action(path, "create")
            return
        if path.startswith("/api/review-cycles/") and path.endswith("/aggregate"):
            self._stage7_review_action(path, "aggregate")
            return
        if path.startswith("/api/review-cycles/") and "/assignments/" in path and path.endswith("/submit"):
            self._stage7_review_action(path, "submit")
            return
        if path.startswith("/api/runs/") and path.endswith("/preflight"):
''',
    label="server review POST routes",
)
method_anchor = '''    def _run_action(self, path: str, action: str) -> None:
'''
review_method = r'''    def _stage7_review_action(self, path: str, action: str) -> None:
        try:
            payload = self._read_json() if int(self.headers.get("Content-Length") or "0") else {}
            if not isinstance(payload, dict):
                payload = {}
            auth = self._require_founder_mutation(payload)
            actor = str(payload.get("actor") or auth.get("actor") or "shan")
            if action == "create":
                run_id = path.removeprefix("/api/runs/").removesuffix("/reviews").strip("/")
                result = create_independent_review_cycle(
                    self._store(),
                    run_id,
                    reviewers=payload.get("reviewers") if isinstance(payload.get("reviewers"), list) else [],
                    actor=actor,
                    policy=payload.get("policy") if isinstance(payload.get("policy"), dict) else None,
                )
            elif action == "aggregate":
                cycle_id = path.removeprefix("/api/review-cycles/").removesuffix("/aggregate").strip("/")
                result = aggregate_independent_review_cycle(self._store(), cycle_id, actor=actor)
            elif action == "submit":
                rest = path.removeprefix("/api/review-cycles/").removesuffix("/submit").strip("/")
                cycle_id, assignment_id = rest.split("/assignments/", 1)
                result = submit_independent_review_report(
                    self._store(),
                    cycle_id,
                    assignment_id,
                    payload=payload.get("report") if isinstance(payload.get("report"), dict) else payload,
                    actor=actor,
                )
            else:
                raise ValueError("unknown Stage 7 review action")
            self._json(HTTPStatus.OK, result)
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

'''
text = replace_once(text, method_anchor, review_method + method_anchor, label="server review action method")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stage 7 Packet 7A tests
# ---------------------------------------------------------------------------
(ROOT / "tests" / "test_stage7_review_authority.py").write_text(
    r'''"""Adversarial tests for Stage 7 Packet 7A independent-review authority."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from buildforme.db import SCHEMA_VERSION
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.review_gate import collect_hard_blocks
from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    require_clear_independent_review,
    submit_independent_review_report,
)
from buildforme.storage import LocalStore


class Stage7ReviewAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.run = {
            "id": "run-stage7",
            "project_id": "buildforme",
            "provider_id": "codex",
            "repository": "shanchaudary/Buildforme",
            "repository_local_path": self.temp.name,
            "baseline_ref": "HEAD",
            "baseline_commit": "a" * 40,
            "requested_target_branch": "feature/stage7",
            "execution_branch": "feature/stage7-run",
            "target_branch": "feature/stage7-run",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-stage7",
            "constitution_lease_fingerprint": "l" * 64,
            "packet": {
                "id": "pkt-stage7",
                "objective": "review test",
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7",
                "allowed_files": ["buildforme/**", "tests/**"],
                "forbidden_files": [".env"],
            },
            "review": {"hard_blocks": [], "accept_for_pr_prep_allowed": True},
            "row_version": 1,
        }
        self.run["scope_fingerprint"] = compute_run_scope_fingerprint(self.run, self.run["packet"])
        self.run = self.store.save_run_for_setup(self.run)
        evidence = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 123,
                "stdout": "ok",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": self.temp.name,
                "baseline_commit": self.run["baseline_commit"],
                "head_commit": self.run["baseline_commit"],
                "branch": self.run["execution_branch"],
            },
            diff={
                "manifest": {
                    "complete": True,
                    "files": [{"path": "buildforme/x.py"}],
                    "files_changed": ["buildforme/x.py"],
                    "manifest_fingerprint": "m" * 64,
                    "diff_stat": "modified buildforme/x.py",
                },
                "patch_fingerprint": "p" * 64,
            },
            provider_health={"version": "test", "executable": "codex"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.run["baseline_commit"],
            final_head_sha=self.run["baseline_commit"],
            execution_branch=self.run["execution_branch"],
            patch_fingerprint="p" * 64,
            manifest_fingerprint="m" * 64,
        )
        self.evidence = self.store.save_run_evidence(evidence)
        # Default provider registry contains all four; no live claim is made in Packet 7A.
        self.reviewers = [
            {"reviewer_id": "security-reviewer", "provider_id": "claude", "role": "security"},
            {"reviewer_id": "correctness-reviewer", "provider_id": "grok", "role": "correctness"},
        ]

    def _cycle(self):
        return create_independent_review_cycle(
            self.store, self.run["id"], reviewers=self.reviewers, actor="shan"
        )

    def _pass_report(self, assignment):
        return submit_independent_review_report(
            self.store,
            assignment["cycle_id"],
            assignment["assignment_id"],
            payload={"verdict": "pass", "summary": "No blocking defect", "findings": []},
            actor=assignment["reviewer_id"],
        )

    def test_schema_v4(self):
        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 4)

    def test_implementer_cannot_review_own_execution(self):
        reviewers = [
            {"reviewer_id": "self", "provider_id": "codex", "role": "general"},
            {"reviewer_id": "other", "provider_id": "claude", "role": "security"},
        ]
        with self.assertRaisesRegex(ValueError, "cannot review its own"):
            create_independent_review_cycle(self.store, self.run["id"], reviewers=reviewers)

    def test_duplicate_provider_rejected(self):
        reviewers = [
            {"reviewer_id": "a", "provider_id": "claude", "role": "security"},
            {"reviewer_id": "b", "provider_id": "claude", "role": "correctness"},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate reviewer provider"):
            create_independent_review_cycle(self.store, self.run["id"], reviewers=reviewers)

    def test_cycle_binds_run_evidence_scope_and_constitution_atomically(self):
        result = self._cycle()
        cycle = result["cycle"]
        run = result["run"]
        self.assertEqual(cycle["evidence_id"], self.evidence["evidence_id"])
        self.assertEqual(cycle["evidence_fingerprint"], self.evidence["evidence_fingerprint"])
        self.assertEqual(cycle["scope_fingerprint"], self.run["scope_fingerprint"])
        self.assertEqual(cycle["constitution_hash"], self.run["constitution_hash"])
        self.assertTrue(run["stage7_review_required"])
        self.assertEqual(run["stage7_review_cycle_id"], cycle["cycle_id"])

    def test_second_active_cycle_rejected(self):
        self._cycle()
        with self.assertRaisesRegex(ValueError, "active independent review cycle"):
            self._cycle()

    def test_blind_report_cannot_claim_consensus_or_founder_authority(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        with self.assertRaisesRegex(ValueError, "forbidden authority or non-blind"):
            submit_independent_review_report(
                self.store,
                assignment["cycle_id"],
                assignment["assignment_id"],
                payload={"verdict": "pass", "findings": [], "consensus": "all pass"},
            )

    def test_critical_finding_is_forced_blocking(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        submitted = submit_independent_review_report(
            self.store,
            assignment["cycle_id"],
            assignment["assignment_id"],
            payload={
                "verdict": "block",
                "summary": "critical defect",
                "findings": [
                    {
                        "severity": "critical",
                        "category": "governance",
                        "blocking": False,
                        "summary": "authority bypass",
                        "evidence": "call path bypasses storage authority",
                        "recommendation": "route through atomic authority",
                    }
                ],
            },
        )
        self.assertTrue(submitted["findings"][0]["blocking"])

    def test_report_is_append_only(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        self._pass_report(assignment)
        with self.assertRaisesRegex(ValueError, "not pending|append-only"):
            self._pass_report(assignment)

    def test_quorum_required_before_aggregation(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        with self.assertRaisesRegex(ValueError, "quorum not met"):
            aggregate_independent_review_cycle(self.store, result["cycle"]["cycle_id"])

    def test_clear_quorum_binds_run_and_removes_stage7_hard_block(self):
        result = self._cycle()
        for assignment in result["assignments"]:
            self._pass_report(assignment)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"], actor="shan"
        )
        self.assertEqual(finalized["cycle"]["status"], "clear")
        run = self.store.get_run(self.run["id"])
        self.assertEqual(run["independent_review"]["status"], "clear")
        self.assertTrue(run["independent_review"]["quorum_met"])
        require_clear_independent_review(self.store, run)
        blocks = collect_hard_blocks(
            run=run,
            evidence=self.evidence,
            verification=self.evidence["verification"],
            constitution_validation={"passed": True, "valid": True},
        )
        self.assertFalse(any("Stage 7" in block for block in blocks), blocks)

    def test_blocking_finding_produces_repair_required_and_founder_block(self):
        result = self._cycle()
        first, second = result["assignments"]
        submit_independent_review_report(
            self.store,
            first["cycle_id"],
            first["assignment_id"],
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "high",
                        "category": "security",
                        "summary": "unsafe path",
                        "evidence": "file escapes allowed path",
                        "recommendation": "constrain path",
                    }
                ],
            },
        )
        self._pass_report(second)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "repair_required")
        run = self.store.get_run(self.run["id"])
        with self.assertRaisesRegex(ValueError, "clear Stage 7"):
            require_clear_independent_review(self.store, run)

    def test_review_service_has_no_unrestricted_run_write(self):
        source = Path("buildforme/review_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"save_run", "save_run_for_setup", "save_run_legacy_json"}:
                    forbidden.append((node.func.attr, node.lineno))
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# Stage 7 Packet 7A documentation
# ---------------------------------------------------------------------------
(ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md").write_text(
    '''# Stage 7 — Independent Multi-Agent Review Loop

## Status

Packet 7A implements the non-bypassable review authority foundation.
Stage 7 is not complete until automated reviewer execution, repair orchestration,
and re-verification are implemented and independently accepted.

## Packet 7A delivered authority

- Review cycles bind to one execution run, exact immutable execution evidence,
  scope fingerprint, Constitution hash, lease, and implementer provider.
- Minimum two blind reviewers with distinct provider identities.
- The implementation provider cannot review its own work.
- Reviewer assignments are immutable and one provider may submit only once.
- Reports and findings are append-only and fingerprinted.
- Critical and high findings are always blocking.
- Reviewers cannot claim founder, merge, deploy, or acceptance authority.
- Aggregation is deterministic and storage independently recomputes it.
- Quorum failure cannot produce a verdict.
- A clear verdict is bound atomically to the run.
- Once a run enters Stage 7 review, founder acceptance is blocked until the
  exact bound cycle is clear, quorum is met, evidence is current, and no
  blocking findings remain.

## Remaining Stage 7 packets

1. Automated blind reviewer execution using distinct live-ready providers.
2. Structured review packet construction from exact patch/evidence material.
3. Governed repair run generation from blocking findings.
4. Fresh re-verification and a new independent review cycle after repair.
5. CLI and browser control-plane surfaces.
6. End-to-end multi-provider smoke and adversarial red-team acceptance.

## Boundaries

- No reviewer may merge, deploy, mutate production, approve its own work, or
  change run authority.
- No same-provider quorum by default.
- No consensus sharing before each reviewer submits.
- No finding is closed without fresh repair evidence and re-verification.
''',
    encoding="utf-8",
)

print("Stage 7 Packet 7A review authority applied")
