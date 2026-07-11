"""Stage 7 independent-review schemas, fingerprints, and deterministic aggregation.

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
REVIEW_FINDING_SCHEMA = "buildforme.review_finding.v1"
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
    requested_policy = dict(policy or {})
    immutable_policy = {
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
    for key, required_value in immutable_policy.items():
        if key in requested_policy and requested_policy[key] != required_value:
            raise ValueError(f"review policy cannot weaken {key}")
    policy_record = {
        **requested_policy,
        "required_reviewer_count": int(
            requested_policy.get("required_reviewer_count") or len(normalized)
        ),
        "min_distinct_providers": int(
            requested_policy.get("min_distinct_providers") or len(normalized)
        ),
        **immutable_policy,
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
    policy = cycle.get("policy") if isinstance(cycle.get("policy"), dict) else {}
    required = int(cycle.get("required_reviewer_count") or 0)
    distinct = int(cycle.get("min_distinct_providers") or 0)
    if required != int(policy.get("required_reviewer_count") or 0):
        problems.append("review cycle required_reviewer_count does not match policy")
    if distinct != int(policy.get("min_distinct_providers") or 0):
        problems.append("review cycle min_distinct_providers does not match policy")
    immutable_policy = {
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
    for key, required_value in immutable_policy.items():
        if policy.get(key) != required_value:
            problems.append(f"review cycle policy weakened: {key}")
    if required < 2 or required > len(normalized):
        problems.append("review cycle required reviewer count invalid")
    if distinct < 2 or distinct > required:
        problems.append("review cycle distinct provider count invalid")
    authority = {
        "run_id": cycle.get("run_id"),
        "evidence_id": cycle.get("evidence_id"),
        "evidence_fingerprint": cycle.get("evidence_fingerprint"),
        "scope_fingerprint": cycle.get("scope_fingerprint"),
        "constitution_hash": cycle.get("constitution_hash"),
        "constitution_lease_id": cycle.get("constitution_lease_id"),
        "implementer_provider_id": cycle.get("implementer_provider_id"),
        "reviewers": normalized,
        "policy": policy,
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
    if assignment.get("evidence_id") != cycle.get("evidence_id"):
        problems.append("review assignment evidence id mismatch")
    if assignment.get("evidence_fingerprint") != cycle.get("evidence_fingerprint"):
        problems.append("review assignment evidence fingerprint mismatch")
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
        "finding_fingerprint": _fingerprint(REVIEW_FINDING_SCHEMA, material),
        "immutable": True,
    }


def validate_finding_for_storage(
    finding: dict[str, Any],
    *,
    report: dict[str, Any],
    cycle: dict[str, Any],
    assignment: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    if not isinstance(finding, dict):
        return ["review finding must be an object"]
    for field in (
        "finding_id",
        "cycle_id",
        "assignment_id",
        "reviewer_id",
        "provider_id",
        "severity",
        "category",
        "summary",
        "finding_fingerprint",
    ):
        if finding.get(field) in (None, ""):
            problems.append(f"review finding missing {field}")
    if finding.get("cycle_id") != cycle.get("cycle_id"):
        problems.append("review finding cycle mismatch")
    if finding.get("assignment_id") != assignment.get("assignment_id"):
        problems.append("review finding assignment mismatch")
    if finding.get("reviewer_id") != assignment.get("reviewer_id"):
        problems.append("review finding reviewer mismatch")
    if finding.get("provider_id") != assignment.get("provider_id"):
        problems.append("review finding provider mismatch")
    severity = str(finding.get("severity") or "")
    if severity not in SEVERITIES:
        problems.append("review finding severity invalid")
    if severity in {"critical", "high"} and not finding.get("blocking"):
        problems.append("critical/high review finding must be blocking")
    if severity in {"critical", "high"} and not str(finding.get("evidence") or "").strip():
        problems.append("critical/high review finding requires evidence")
    material = {
        key: finding.get(key)
        for key in (
            "finding_id",
            "cycle_id",
            "assignment_id",
            "reviewer_id",
            "provider_id",
            "severity",
            "category",
            "blocking",
            "summary",
            "evidence",
            "recommendation",
            "file",
            "line",
            "law_ids",
        )
    }
    if finding.get("finding_fingerprint") != _fingerprint(REVIEW_FINDING_SCHEMA, material):
        problems.append("review finding fingerprint mismatch")
    if finding not in (report.get("findings") or []):
        problems.append("review finding is not bound into report")
    return problems


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
    if report.get("role") != assignment.get("role"):
        problems.append("review report role mismatch")
    if report.get("blind_review") is not True:
        problems.append("review report must remain blind")
    if report.get("provider_may_self_accept") is not False:
        problems.append("review report cannot grant provider self-acceptance")
    if report.get("reviewed_evidence_id") != cycle.get("evidence_id"):
        problems.append("review report evidence id mismatch")
    if report.get("reviewed_evidence_fingerprint") != cycle.get("evidence_fingerprint"):
        problems.append("review report evidence fingerprint mismatch")
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    blocking = [item for item in findings if isinstance(item, dict) and item.get("blocking")]
    verdict = str(report.get("verdict") or "")
    if verdict == "pass" and blocking:
        problems.append("pass review report contains blocking findings")
    if verdict in {"changes_required", "block"} and not findings:
        problems.append(f"{verdict} review report requires findings")
    if verdict == "block" and not blocking:
        problems.append("block review report requires a blocking finding")
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
    submitted_assignment_ids = {
        str(a.get("assignment_id") or "") for a in submitted
    }
    report_assignment_ids = [str(r.get("assignment_id") or "") for r in reports]
    if len(report_assignment_ids) != len(set(report_assignment_ids)):
        raise ValueError("duplicate review report assignment")
    if not set(report_assignment_ids).issubset(submitted_assignment_ids):
        raise ValueError("review report does not belong to a submitted assignment")
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
