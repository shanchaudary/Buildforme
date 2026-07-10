"""Validate agent outputs and platform bindings against the Constitution."""

from __future__ import annotations

import re
from typing import Any

from governance.constitution_hash import compute_constitution_hash, verify_constitution_hash
from governance.constitution_lease import validate_lease_integrity

FABRICATION_MARKERS = (
    r"\bfake\s+test\b",
    r"\bfabricated\b",
    r"\binvented\s+evidence\b",
    r"\bpretend\s+(?:tests?|logs?|commands?)\s+passed\b",
    r"\bmock(?:ed)?\s+success\b",
)
TRUNCATION_MARKERS = (
    r"\[truncated\]",
    r"\.\.\.\s*remaining\s+omitted",
    r"\boutput\s+truncated\b",
    r"\bsilently\s+truncated\b",
)
BYPASS_MARKERS = (
    r"\bdisable\s+governance\b",
    r"\bbypass\s+(?:the\s+)?constitution\b",
    r"\bignore\s+(?:the\s+)?constitution\b",
    r"\bskip\s+(?:the\s+)?constitution\b",
    r"\bweaken\s+(?:the\s+)?constitution\b",
)
FAKE_SUCCESS_MARKERS = (
    r"\bsuccess\s+because\s+(?:it\s+)?compiles\b",
    r"\bpartial(?:ly)?\s+pass(?:es|ed)?\s+as\s+complete\b",
    r"\bno\s+error\s+(?:so|means)\s+success\b",
)
CAPABILITY_CLAIM_MARKERS = (
    r"\bi\s+(?:have|can)\s+(?:live\s+)?(?:github\s+write|deploy|merge|production\s+write)\b",
    r"\bcredentials?\s+configured\b",
    r"\blive\s+execution\s+(?:enabled|available|active)\b",
)

REQUIRED_COMPLETION_HINTS = (
    "acceptance",
    "test",
    "verif",
    "final",
    "evidence",
    "remaining",
    "risk",
)


def validate_constitution_document(constitution: dict[str, Any]) -> dict[str, Any]:
    """Structural validation of the constitution itself."""
    problems: list[str] = []
    if not isinstance(constitution, dict):
        return {"valid": False, "problems": ["constitution must be an object"], "law_count": 0}

    for field in ("constitution_id", "version", "laws", "critical_law_ids"):
        if field not in constitution:
            problems.append(f"missing field: {field}")

    laws = constitution.get("laws") or []
    if not isinstance(laws, list) or not laws:
        problems.append("laws must be a non-empty list")
        laws = []

    ids: list[str] = []
    required_law_fields = (
        "id",
        "name",
        "description",
        "applies_to",
        "severity",
        "validation",
        "evidence_required",
        "violation_response",
    )
    for index, law in enumerate(laws):
        if not isinstance(law, dict):
            problems.append(f"laws[{index}] must be an object")
            continue
        for field in required_law_fields:
            if field not in law or law[field] in (None, "", []):
                problems.append(f"laws[{index}] missing {field}")
        law_id = str(law.get("id") or "")
        if law_id:
            if law_id in ids:
                problems.append(f"duplicate law id: {law_id}")
            ids.append(law_id)
        severity = str(law.get("severity") or "").lower()
        if severity and severity not in {"critical", "high", "medium", "low"}:
            problems.append(f"{law_id or index}: invalid severity {severity!r}")

    if len(ids) < 20:
        problems.append(f"expected at least 20 laws, found {len(ids)}")

    for required in (
        "LAW-001",
        "LAW-002",
        "LAW-004",
        "LAW-005",
        "LAW-009",
        "LAW-012",
        "LAW-020",
    ):
        if required not in ids:
            problems.append(f"missing required law {required}")

    content_hash = compute_constitution_hash(constitution)
    return {
        "valid": not problems,
        "problems": problems,
        "law_count": len(ids),
        "version": constitution.get("version"),
        "hash": content_hash,
        "law_ids": ids,
    }


def validate_provider_acknowledgement(
    provider: dict[str, Any],
    constitution: dict[str, Any],
) -> dict[str, Any]:
    problems: list[str] = []
    if not provider.get("constitution_supported", True):
        problems.append("provider marks constitution_supported=false (forbidden)")
    if not provider.get("constitution_acknowledged"):
        problems.append("provider has not acknowledged the Constitution")
    current = compute_constitution_hash(constitution)
    if str(provider.get("constitution_hash") or "") != current:
        problems.append("provider constitution_hash does not match current Constitution")
    if str(provider.get("constitution_version") or "") != str(constitution.get("version") or ""):
        problems.append("provider constitution_version does not match current Constitution")
    return {
        "valid": not problems,
        "problems": problems,
        "provider_id": provider.get("provider_id"),
        "required_hash": current,
    }


def validate_run_binding(
    run: dict[str, Any],
    constitution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the embedded run/lease relationship.

    Canonical persisted-lease comparison is performed by
    validate_run_lease_against_store at preflight and execution boundaries.
    """
    problems: list[str] = []
    lease = run.get("constitution_lease") if isinstance(run.get("constitution_lease"), dict) else None
    if not lease:
        problems.append("run missing constitution_lease")
    else:
        problems.extend(
            validate_lease_integrity(
                lease,
                expected_run_id=str(run.get("id") or ""),
                expected_provider_id=str(run.get("provider_id") or ""),
                expected_packet_id=str(run.get("packet_id") or ""),
            )
        )
        if str(run.get("constitution_hash") or "") != str(lease.get("constitution_hash") or ""):
            problems.append("run constitution_hash does not match lease")
        if str(run.get("constitution_version") or "") != str(lease.get("constitution_version") or ""):
            problems.append("run constitution_version does not match lease")
        if str(run.get("constitution_lease_id") or "") != str(lease.get("lease_id") or ""):
            problems.append("run constitution_lease_id does not match lease")
        if str(run.get("constitution_lease_fingerprint") or "") != str(
            lease.get("lease_fingerprint") or ""
        ):
            problems.append("run constitution_lease_fingerprint does not match lease")
    if not str(run.get("constitution_version") or "").strip():
        problems.append("run missing constitution_version")
    if not str(run.get("constitution_hash") or "").strip():
        problems.append("run missing constitution_hash")
    return {"valid": not problems, "problems": problems, "run_id": run.get("id")}


def validate_packet_binding(packet: dict[str, Any], constitution: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    version = str(
        packet.get("constitution_version")
        or (packet.get("constitution") or {}).get("version")
        or ""
    ).strip()
    if not version:
        problems.append("packet missing constitution_version")
    elif version != str(constitution.get("version") or ""):
        problems.append("packet constitution_version does not match current Constitution")
    packet_hash = str(
        packet.get("constitution_hash")
        or (packet.get("constitution") or {}).get("hash")
        or ""
    )
    if not packet_hash:
        problems.append("packet missing constitution_hash")
    elif not verify_constitution_hash(constitution, packet_hash):
        problems.append("packet constitution_hash does not match current Constitution")
    binding = packet.get("constitution") if isinstance(packet.get("constitution"), dict) else {}
    if not binding:
        problems.append("packet missing constitution binding object")
    else:
        if str(binding.get("version") or "") != version:
            problems.append("packet binding version does not match top-level version")
        if str(binding.get("hash") or "") != packet_hash:
            problems.append("packet binding hash does not match top-level hash")
        if binding.get("bypass_forbidden") is not True:
            problems.append("packet must explicitly forbid constitution bypass")
    return {"valid": not problems, "problems": problems, "packet_id": packet.get("id")}


def validate_output(
    output: dict[str, Any] | str,
    *,
    constitution: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate claimed completion / agent output against constitutional laws.

    Deterministic heuristics only — no LLM. Fail closed on clear violations.
    """
    context = context or {}
    if isinstance(output, str):
        text = output
        data: dict[str, Any] = {"text": output}
    else:
        data = dict(output or {})
        text = " ".join(
            str(data.get(key) or "")
            for key in (
                "text",
                "summary",
                "result_summary",
                "final_report",
                "notes",
                "status_message",
            )
        )

    violations: list[dict[str, Any]] = []
    lower = text.lower()

    def add(law_id: str, name: str, severity: str, evidence: str, response: str) -> None:
        violations.append(
            {
                "law_id": law_id,
                "name": name,
                "severity": severity,
                "evidence": evidence,
                "response": response,
            }
        )

    for pattern in FABRICATION_MARKERS:
        if re.search(pattern, lower, re.I):
            add(
                "LAW-002",
                "No Fabrication",
                "critical",
                f"matched fabrication marker: {pattern}",
                "Reject output; record violation; block completion.",
            )
            break

    for pattern in TRUNCATION_MARKERS:
        if re.search(pattern, lower, re.I):
            add(
                "LAW-003",
                "No Truncation",
                "high",
                f"matched truncation marker: {pattern}",
                "Reject incomplete required sections; require continuation.",
            )
            break

    for pattern in CAPABILITY_CLAIM_MARKERS:
        if re.search(pattern, lower, re.I):
            verified = set(str(capability) for capability in (context.get("verified_capabilities") or []))
            if not verified:
                add(
                    "LAW-004",
                    "No Fake Capability",
                    "critical",
                    f"unverified capability claim matched: {pattern}",
                    "Reject capability claim; fail closed.",
                )
                break

    for pattern in FAKE_SUCCESS_MARKERS:
        if re.search(pattern, lower, re.I):
            add(
                "LAW-005",
                "No Fake Success",
                "critical",
                f"matched fake success marker: {pattern}",
                "Reject success claim; require objective completion evidence.",
            )
            break

    for pattern in BYPASS_MARKERS:
        if re.search(pattern, lower, re.I):
            add(
                "LAW-020",
                "Governance Cannot Be Bypassed",
                "critical",
                f"matched bypass marker: {pattern}",
                "Hard block execution and completion; record critical violation.",
            )
            break

    claims_complete = bool(
        data.get("claims_complete")
        or data.get("completed")
        or re.search(r"\bcomplete[d]?\b", lower)
    )
    if claims_complete:
        evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
        tests = data.get("tests") or data.get("tests_run") or []
        acceptance = data.get("acceptance_criteria") or context.get("acceptance_criteria") or []
        if not evidence and not tests and not any(
            hint in lower for hint in ("test", "verif", "evidence", "acceptance")
        ):
            add(
                "LAW-001",
                "Truth Before Completion",
                "critical",
                "completion claimed without evidence, tests, or verification notes",
                "Reject completion claim; require evidence before success.",
            )
        if acceptance and not evidence and "acceptance" not in lower:
            add(
                "LAW-001",
                "Truth Before Completion",
                "critical",
                "acceptance criteria present in context but not addressed",
                "Reject completion claim; require evidence before success.",
            )
        if data.get("partial_success") and data.get("claims_complete"):
            add(
                "LAW-005",
                "No Fake Success",
                "critical",
                "partial_success with claims_complete",
                "Reject success claim; require objective completion evidence.",
            )

    if data.get("bypass_constitution") or data.get("disable_governance"):
        add(
            "LAW-020",
            "Governance Cannot Be Bypassed",
            "critical",
            "explicit bypass/disable flag in output",
            "Hard block execution and completion; record critical violation.",
        )

    if data.get("removed_capability") and not data.get("removal_approved"):
        add(
            "LAW-009",
            "No Capability Removal",
            "critical",
            "capability removal without approval",
            "Reject completion; restore removed capability or obtain approval.",
        )

    if data.get("product_weakened_for_tests"):
        add(
            "LAW-012",
            "No Test-Oriented Degradation",
            "critical",
            "product_weakened_for_tests=true",
            "Reject degraded product-for-tests changes.",
        )

    critical = [violation for violation in violations if violation.get("severity") == "critical"]
    return {
        "valid": len(critical) == 0 and len(violations) == 0,
        "passed": len(critical) == 0,
        "violations": violations,
        "critical_count": len(critical),
        "constitution_version": constitution.get("version"),
        "constitution_hash": compute_constitution_hash(constitution),
        "response": "accept" if not violations else ("reject_completion" if critical else "flag"),
    }


def validate_no_duplicate_governance(modules_declaring_authority: list[str]) -> dict[str, Any]:
    """LAW-013 helper: detect duplicate authority declarations for constitution domain."""
    counts: dict[str, int] = {}
    for name in modules_declaring_authority:
        key = str(name).strip().lower()
        counts[key] = counts.get(key, 0) + 1
    duplicates = [key for key, count in counts.items() if count > 1]
    return {
        "valid": not duplicates,
        "duplicates": duplicates,
        "authority": "governance/constitution_engine.py",
        "note": "Stage 5.5 buildforme.governance remains run/preflight validators only",
    }
