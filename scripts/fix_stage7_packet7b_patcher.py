from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7b.py"
text = path.read_text(encoding="utf-8")

old = """    '''        findings: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import validate_finding_for_storage, validate_report_for_storage

        now = utc_now_iso()
''',
    '''        findings: list[dict[str, Any]],
        actor: str,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import validate_finding_for_storage, validate_report_for_storage
        from buildforme.review_execution import validate_review_execution_record

        if not isinstance(execution, dict):
            raise ValueError(\"direct review report submission disabled; authenticated reviewer execution required\")
        execution_record = dict(execution)
        now = utc_now_iso()
''',"""
new = """    '''        findings: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import (
            validate_finding_for_storage,
            validate_report_for_storage,
        )

        now = utc_now_iso()
''',
    '''        findings: list[dict[str, Any]],
        actor: str,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import (
            validate_finding_for_storage,
            validate_report_for_storage,
        )
        from buildforme.review_execution import validate_review_execution_record

        if not isinstance(execution, dict):
            raise ValueError(\"direct review report submission disabled; authenticated reviewer execution required\")
        execution_record = dict(execution)
        now = utc_now_iso()
''',"""
if text.count(old) != 1:
    raise RuntimeError(f"report signature patch block count={text.count(old)}")
text = text.replace(old, new, 1)

old = """    '''            problems = validate_report_for_storage(report, cycle, assignment)
            if problems:
                raise ValueError(\"review report rejected: \" + \"; \".join(problems))
            canonical_findings = list(report.get(\"findings\") or [])
''',"""
new = """    '''            problems = validate_report_for_storage(report, cycle, assignment)
            if problems:
                raise ValueError(\"review report rejected: \" + \"; \".join(problems))
            report_findings = report.get(\"findings\") if isinstance(report.get(\"findings\"), list) else []
''',"""
if text.count(old) != 1:
    raise RuntimeError(f"report validation old block count={text.count(old)}")
text = text.replace(old, new, 1)

old = '''            canonical_findings = list(report.get("findings") or [])
'''
new = '''            report_findings = report.get("findings") if isinstance(report.get("findings"), list) else []
'''
if text.count(old) != 1:
    raise RuntimeError(f"report validation replacement variable count={text.count(old)}")
text = text.replace(old, new, 1)

old = '''    'dumps({"assignment_id": assignment_id, "report_id": report_id, "submitted": submitted}),',
    'dumps({"assignment_id": assignment_id, "report_id": report_id, "execution_id": execution_record.get("execution_id"), "submitted": submitted}),',
'''
new = '''    'dumps({"assignment_id": assignment_id, "report_id": report_id, "submitted": submitted, "required": required}),',
    'dumps({"assignment_id": assignment_id, "report_id": report_id, "execution_id": execution_record.get("execution_id"), "submitted": submitted, "required": required}),',
'''
if text.count(old) != 1:
    raise RuntimeError(f"event metadata patch block count={text.count(old)}")
text = text.replace(old, new, 1)

old = '''text = replace_once(
    text,
    \'\'\'        "founder_override_blocking_findings": False,
        **(policy or {}),
    }
\'\'\',
    \'\'\'        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
        **(policy or {}),
    }
\'\'\',
    label="review policy automated flag",
)
text = replace_once(
    text,
    \'\'\'        "founder_override_blocking_findings": False,
    }
\'\'\',
    \'\'\'        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
\'\'\',
    label="required policy values",
)
'''
new = '''text = replace_once(
    text,
    \'\'\'    immutable_policy = {
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
    }
\'\'\',
    \'\'\'    immutable_policy = {
        "blind_review": True,
        "implementer_provider_forbidden": True,
        "critical_high_always_blocking": True,
        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
\'\'\',
    label="immutable automated review policy",
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"Packet 7B policy patch block count={text.count(old)}")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("Packet 7B patch anchors corrected")
