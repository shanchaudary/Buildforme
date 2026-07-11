from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Review contracts: non-weakenable policy and independent finding validation
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_contracts.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''REVIEW_REPORT_SCHEMA = "buildforme.review_report.v1"
REVIEW_AGGREGATE_SCHEMA = "buildforme.review_aggregate.v1"
''',
    '''REVIEW_REPORT_SCHEMA = "buildforme.review_report.v1"
REVIEW_FINDING_SCHEMA = "buildforme.review_finding.v1"
REVIEW_AGGREGATE_SCHEMA = "buildforme.review_aggregate.v1"
''',
    label="finding schema constant",
)
text = replace_once(
    text,
    '''    policy_record = {
        "required_reviewer_count": len(normalized),
        "min_distinct_providers": len(normalized),
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
        **(policy or {}),
    }
''',
    '''    requested_policy = dict(policy or {})
    immutable_policy = {
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
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
''',
    label="non-weakenable review policy",
)
text = replace_once(
    text,
    '''    authority = {
        "run_id": cycle.get("run_id"),
''',
    '''    policy = cycle.get("policy") if isinstance(cycle.get("policy"), dict) else {}
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
''',
    label="cycle duplicate authority validation",
)
text = replace_once(
    text,
    '''        "policy": cycle.get("policy") if isinstance(cycle.get("policy"), dict) else {},
''',
    '''        "policy": policy,
''',
    label="cycle canonical policy",
)
text = replace_once(
    text,
    '''    if assignment.get("run_id") != cycle.get("run_id"):
        problems.append("review assignment run mismatch")
    if assignment.get("provider_id") == cycle.get("implementer_provider_id"):
''',
    '''    if assignment.get("run_id") != cycle.get("run_id"):
        problems.append("review assignment run mismatch")
    if assignment.get("evidence_id") != cycle.get("evidence_id"):
        problems.append("review assignment evidence id mismatch")
    if assignment.get("evidence_fingerprint") != cycle.get("evidence_fingerprint"):
        problems.append("review assignment evidence fingerprint mismatch")
    if assignment.get("provider_id") == cycle.get("implementer_provider_id"):
''',
    label="assignment evidence binding",
)
text = replace_once(
    text,
    '''        "finding_fingerprint": _fingerprint("buildforme.review_finding.v1", material),
''',
    '''        "finding_fingerprint": _fingerprint(REVIEW_FINDING_SCHEMA, material),
''',
    label="finding schema use",
)
insert_before = '''def build_review_report_record(
'''
finding_validation = r'''def validate_finding_for_storage(
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


'''
text = replace_once(text, insert_before, finding_validation + insert_before, label="finding storage validator")
text = replace_once(
    text,
    '''    if report.get("provider_id") != assignment.get("provider_id"):
        problems.append("review report provider mismatch")
''',
    '''    if report.get("provider_id") != assignment.get("provider_id"):
        problems.append("review report provider mismatch")
    if report.get("role") != assignment.get("role"):
        problems.append("review report role mismatch")
    if report.get("blind_review") is not True:
        problems.append("review report must remain blind")
    if report.get("provider_may_self_accept") is not False:
        problems.append("review report cannot grant provider self-acceptance")
''',
    label="report immutable authority flags",
)
text = replace_once(
    text,
    '''    material = {
        key: report.get(key)
''',
    '''    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
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
''',
    label="storage report semantic validation",
)
text = replace_once(
    text,
    '''    provider_ids = sorted({str(r.get("provider_id") or "") for r in reports})
''',
    '''    submitted_assignment_ids = {
        str(a.get("assignment_id") or "") for a in submitted
    }
    report_assignment_ids = [str(r.get("assignment_id") or "") for r in reports]
    if len(report_assignment_ids) != len(set(report_assignment_ids)):
        raise ValueError("duplicate review report assignment")
    if not set(report_assignment_ids).issubset(submitted_assignment_ids):
        raise ValueError("review report does not belong to a submitted assignment")
    provider_ids = sorted({str(r.get("provider_id") or "") for r in reports})
''',
    label="aggregate assignment binding",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Storage independently validates all cycle and report authority
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''        from buildforme.review_contracts import validate_assignment_record, validate_cycle_record
''',
    '''        from buildforme.evidence import EVIDENCE_KIND_EXECUTION, validate_evidence_for_storage
        from buildforme.review_contracts import validate_assignment_record, validate_cycle_record
''',
    label="cycle evidence imports",
)
text = replace_once(
    text,
    '''        for item in assignment_records:
            problems = validate_assignment_record(item, cycle_record)
            if problems:
                raise ValueError("review assignment rejected: " + "; ".join(problems))
        cycle_id = str(cycle_record["cycle_id"])
''',
    '''        for item in assignment_records:
            problems = validate_assignment_record(item, cycle_record)
            if problems:
                raise ValueError("review assignment rejected: " + "; ".join(problems))
        declared_reviewers = {
            (str(item.get("reviewer_id")), str(item.get("provider_id")), str(item.get("role")))
            for item in (cycle_record.get("reviewers") or [])
        }
        assigned_reviewers = {
            (str(item.get("reviewer_id")), str(item.get("provider_id")), str(item.get("role")))
            for item in assignment_records
        }
        if declared_reviewers != assigned_reviewers or len(assignment_records) != len(
            cycle_record.get("reviewers") or []
        ):
            raise ValueError("review assignments do not exactly match declared reviewers")
        cycle_id = str(cycle_record["cycle_id"])
''',
    label="assignment set equality",
)
old_evidence = '''            evidence_row = conn.execute(
                "SELECT run_id, evidence_fingerprint FROM evidence WHERE evidence_id=?",
                (str(cycle_record["evidence_id"]),),
            ).fetchone()
            if not evidence_row:
                raise ValueError("bound execution evidence not found")
            if str(evidence_row[0]) != run_id:
                raise ValueError("bound execution evidence belongs to another run")
            if str(evidence_row[1] or "") != str(cycle_record["evidence_fingerprint"]):
                raise ValueError("bound execution evidence fingerprint mismatch")
'''
new_evidence = '''            if str(cycle_record.get("scope_fingerprint") or "") != str(
                run.get("scope_fingerprint") or ""
            ):
                raise ValueError("review cycle scope does not match canonical run")
            if str(cycle_record.get("constitution_hash") or "") != str(
                run.get("constitution_hash") or ""
            ):
                raise ValueError("review cycle Constitution does not match canonical run")
            if str(cycle_record.get("constitution_lease_id") or "") != str(
                run.get("constitution_lease_id") or ""
            ):
                raise ValueError("review cycle Constitution lease does not match canonical run")
            if str(cycle_record.get("implementer_provider_id") or "") != str(
                run.get("provider_id") or ""
            ):
                raise ValueError("review cycle implementer provider does not match canonical run")
            evidence_rows = conn.execute(
                "SELECT evidence_id, payload_json, evidence_fingerprint FROM evidence WHERE run_id=? ORDER BY sequence DESC",
                (run_id,),
            ).fetchall()
            latest_execution = None
            for candidate in evidence_rows:
                payload = loads(candidate[1], {})
                if str(payload.get("evidence_kind") or "") == EVIDENCE_KIND_EXECUTION:
                    latest_execution = (candidate, payload)
                    break
            if latest_execution is None:
                raise ValueError("latest execution evidence not found")
            evidence_row, evidence_payload = latest_execution
            if str(evidence_row[0]) != str(cycle_record["evidence_id"]):
                raise ValueError("review cycle must bind the latest execution evidence")
            evidence_problems = validate_evidence_for_storage(evidence_payload)
            if evidence_problems:
                raise ValueError("bound execution evidence invalid: " + "; ".join(evidence_problems))
            actual_evidence_fp = str(
                evidence_payload.get("evidence_fingerprint") or evidence_row[2] or ""
            )
            if actual_evidence_fp != str(cycle_record["evidence_fingerprint"]):
                raise ValueError("bound execution evidence fingerprint mismatch")
            evidence_constitution = (
                evidence_payload.get("constitution")
                if isinstance(evidence_payload.get("constitution"), dict)
                else {}
            )
            if str(evidence_constitution.get("hash") or "") != str(run.get("constitution_hash") or ""):
                raise ValueError("execution evidence Constitution is stale")
'''
text = replace_once(text, old_evidence, new_evidence, label="canonical cycle authority checks")
text = replace_once(
    text,
    '''        from buildforme.review_contracts import validate_report_for_storage
''',
    '''        from buildforme.review_contracts import (
            validate_finding_for_storage,
            validate_report_for_storage,
        )
''',
    label="finding storage import",
)
text = replace_once(
    text,
    '''            if str(cycle_row[1]) not in {"open", "collecting"}:
''',
    '''            if str(cycle_row[1]) not in {"open", "collecting", "ready_to_aggregate"}:
''',
    label="accept additional reports before finalization",
)
text = replace_once(
    text,
    '''            report_id = str(report["report_id"])
            if conn.execute(
''',
    '''            report_findings = report.get("findings") if isinstance(report.get("findings"), list) else []
            if findings != report_findings:
                raise ValueError("separate review findings diverge from report findings")
            finding_ids: set[str] = set()
            for finding in findings:
                finding_problems = validate_finding_for_storage(
                    finding,
                    report=report,
                    cycle=cycle,
                    assignment=assignment,
                )
                if finding_problems:
                    raise ValueError("review finding rejected: " + "; ".join(finding_problems))
                finding_id = str(finding.get("finding_id") or "")
                if finding_id in finding_ids:
                    raise ValueError("duplicate finding id within review report")
                finding_ids.add(finding_id)
            report_id = str(report["report_id"])
            if conn.execute(
''',
    label="independent finding validation",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Blind view helper and API withholding
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_service.py"
text = path.read_text(encoding="utf-8")
text += r'''


def get_independent_review_cycle_view(store: LocalStore, cycle_id: str) -> dict[str, Any]:
    """Return a blind-safe cycle view.

    Submitted report/finding content is withheld until the cycle is finalized so a
    pending reviewer cannot anchor on another reviewer's conclusions.
    """
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignments = store.list_review_assignments(cycle_id)
    assignment_view = [
        {
            "assignment_id": item.get("assignment_id"),
            "reviewer_id": item.get("reviewer_id"),
            "provider_id": item.get("provider_id"),
            "role": item.get("role"),
            "status": item.get("status"),
            "submitted_at": item.get("submitted_at"),
        }
        for item in assignments
    ]
    finalized = str(cycle.get("status") or "") in {"clear", "repair_required", "blocked"}
    return {
        "cycle": cycle,
        "assignments": assignment_view,
        "reports": store.list_review_reports(cycle_id) if finalized else [],
        "findings": store.list_review_findings(cycle_id) if finalized else [],
        "blind_material_withheld": not finalized,
    }
'''
path.write_text(text, encoding="utf-8")

path = ROOT / "buildforme" / "server.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    submit_independent_review_report,
''',
    '''    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
    submit_independent_review_report,
''',
    label="blind view server import",
)
old_view = '''                self._json(
                    HTTPStatus.OK,
                    {
                        "cycle": self._store().get_review_cycle(cycle_id),
                        "assignments": self._store().list_review_assignments(cycle_id),
                        "reports": self._store().list_review_reports(cycle_id),
                        "findings": self._store().list_review_findings(cycle_id),
                    },
                )
'''
new_view = '''                self._json(
                    HTTPStatus.OK,
                    get_independent_review_cycle_view(self._store(), cycle_id),
                )
'''
text = replace_once(text, old_view, new_view, label="blind safe review API")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Adversarial regressions for independent storage and blind review
# ---------------------------------------------------------------------------
path = ROOT / "tests" / "test_stage7_review_authority.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    require_clear_independent_review,
    submit_independent_review_report,
)
''',
    '''from buildforme.review_contracts import (
    build_review_cycle_record,
    build_review_report_record,
    validate_finding_for_storage,
)
from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
    require_clear_independent_review,
    submit_independent_review_report,
)
''',
    label="redteam test imports",
)
insert_before = '''    def test_review_service_has_no_unrestricted_run_write(self):
'''
new_tests = r'''    def test_policy_cannot_disable_blind_or_blocking_laws(self):
        for policy in (
            {"blind_review": False},
            {"implementer_provider_forbidden": False},
            {"critical_high_always_blocking": False},
            {"founder_override_blocking_findings": True},
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    create_independent_review_cycle(
                        self.store,
                        self.run["id"],
                        reviewers=self.reviewers,
                        policy=policy,
                    )

    def test_storage_rejects_self_consistent_forged_cycle_authority(self):
        forged_cases = (
            ("scope_fingerprint", "forged-scope", "scope"),
            ("constitution_hash", "f" * 64, "Constitution"),
            ("constitution_lease_id", "forged-lease", "lease"),
            ("provider_id", "glm", "implementer"),
        )
        for field, value, message in forged_cases:
            with self.subTest(field=field):
                forged_run = dict(self.run)
                forged_run[field] = value
                cycle, assignments = build_review_cycle_record(
                    run=forged_run,
                    evidence=self.evidence,
                    reviewers=self.reviewers,
                    actor="shan",
                )
                with self.assertRaisesRegex(ValueError, message):
                    self.store.create_review_cycle_atomic(
                        cycle=cycle,
                        assignments=assignments,
                        actor="shan",
                    )

    def test_storage_rejects_assignment_set_not_equal_to_cycle_reviewers(self):
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.store.create_review_cycle_atomic(
                cycle=cycle,
                assignments=assignments[:-1],
                actor="shan",
            )

    def test_storage_rejects_cycle_bound_to_superseded_execution_evidence(self):
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        newer = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 456,
                "stdout": "new",
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
                    "files": [{"path": "buildforme/y.py"}],
                    "files_changed": ["buildforme/y.py"],
                    "manifest_fingerprint": "n" * 64,
                },
                "patch_fingerprint": "q" * 64,
            },
            provider_health={"version": "test", "executable": "codex"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.run["baseline_commit"],
            final_head_sha=self.run["baseline_commit"],
            execution_branch=self.run["execution_branch"],
            patch_fingerprint="q" * 64,
            manifest_fingerprint="n" * 64,
        )
        self.store.save_run_evidence(newer)
        with self.assertRaisesRegex(ValueError, "latest execution evidence"):
            self.store.create_review_cycle_atomic(
                cycle=cycle,
                assignments=assignments,
                actor="shan",
            )

    def test_storage_rejects_findings_divergent_from_report(self):
        result = self._cycle()
        cycle = result["cycle"]
        assignment = result["assignments"][0]
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "summary": "bug",
                        "evidence": "line 10",
                        "recommendation": "fix",
                    }
                ],
            },
        )
        divergent = [dict(findings[0])]
        divergent[0]["summary"] = "different row"
        with self.assertRaisesRegex(ValueError, "diverge"):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=divergent,
                actor="reviewer",
            )

    def test_finding_fingerprint_is_independently_validated(self):
        result = self._cycle()
        cycle = result["cycle"]
        assignment = result["assignments"][0]
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "high",
                        "category": "security",
                        "summary": "defect",
                        "evidence": "proof",
                        "recommendation": "repair",
                    }
                ],
            },
        )
        finding = dict(findings[0])
        finding["finding_fingerprint"] = "0" * 64
        problems = validate_finding_for_storage(
            finding,
            report=report,
            cycle=cycle,
            assignment=assignment,
        )
        self.assertIn("review finding fingerprint mismatch", problems)

    def test_blind_cycle_view_withholds_reports_until_finalized(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        active = get_independent_review_cycle_view(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertTrue(active["blind_material_withheld"])
        self.assertEqual(active["reports"], [])
        self.assertEqual(active["findings"], [])
        self._pass_report(result["assignments"][1])
        aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        final = get_independent_review_cycle_view(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertFalse(final["blind_material_withheld"])
        self.assertEqual(len(final["reports"]), 2)

'''
text = replace_once(text, insert_before, new_tests + insert_before, label="redteam review tests")
path.write_text(text, encoding="utf-8")

# Document the red-team hardening.
path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''

## Packet 7A red-team hardening

- Storage independently revalidates the canonical run scope, Constitution, lease,
  implementer provider, latest execution evidence kind, evidence fingerprint, and
  evidence Constitution before creating a review cycle.
- Persisted assignments must exactly equal the cycle's declared reviewer set.
- Governance policy flags for blind review, self-review prohibition, blocking
  critical/high findings, and no founder override cannot be weakened by input.
- Finding rows must exactly match the report and each finding fingerprint is
  independently recomputed before insertion.
- Reports are withheld from the read API until the cycle is finalized, preserving
  blind independence during collection.
'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7A red-team remediation applied")
