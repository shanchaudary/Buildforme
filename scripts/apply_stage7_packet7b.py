from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# New Packet 7B review execution authority
# ---------------------------------------------------------------------------
(ROOT / "buildforme" / "review_execution.py").write_text(
    '''"""Stage 7 Packet 7B — immutable blind-review packets and read-only reviewer execution.

Reviewer processes receive the exact bound worktree in a code-owned read-only command
contract.  Provider output is never trusted as authority: packet/worktree identity is
re-proved before and after execution, output must be one strict JSON review object,
and process evidence commits atomically with the report.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from buildforme.evidence import validate_evidence_for_storage
from buildforme.governance import compute_run_scope_fingerprint, validate_safe_id
from buildforme.process_supervisor import get_process_supervisor
from buildforme.provider_discovery import health_check_provider
from buildforme.redaction import redact_argv, redact_hash, redact_text
from buildforme.review_contracts import build_review_report_record
from buildforme.storage import LocalStore, utc_now_iso
from buildforme.verification_manifest import collect_changed_file_manifest, collect_patch_evidence

REVIEW_PACKET_SCHEMA = "buildforme.review_packet.v1"
REVIEW_EXECUTION_SCHEMA = "buildforme.review_execution.v1"
REVIEW_PACKET_MAX_BYTES = 160_000
REVIEW_TIMEOUT_MAX_SECONDS = 1_800

# Reviewed code authority only. Provider records and API payloads cannot alter argv.
REVIEW_COMMAND_CONTRACTS: dict[str, dict[str, Any]] = {
    "codex": {
        "contract_id": "codex.exec.read-only.v1",
        "argv_tail": [
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--json",
            "-s",
            "read-only",
            "-",
        ],
        "prompt_transport": "stdin",
        "output_protocol": "codex_jsonl_agent_message",
        "read_only": True,
    },
}

_ALLOWED_REPORT_KEYS = frozenset({"verdict", "summary", "findings"})
_ALLOWED_FINDING_KEYS = frozenset(
    {
        "severity",
        "category",
        "blocking",
        "summary",
        "evidence",
        "recommendation",
        "file",
        "line",
        "law_ids",
    }
)
_FINAL_CYCLE_STATUSES = frozenset({"clear", "repair_required", "blocked"})
_ACTIVE_CYCLE_STATUSES = frozenset({"open", "collecting"})


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=lambda x: str(x))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
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


def _stable_verification(verification: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for item in verification.get("checks") or []:
        if not isinstance(item, dict):
            continue
        checks.append(
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "detail": item.get("detail") or item.get("message") or item.get("summary"),
            }
        )
    checks.sort(key=lambda item: str(item.get("name") or ""))
    return {
        "passed": bool(verification.get("passed")),
        "blocking_reasons": list(verification.get("blocking_reasons") or []),
        "warnings": list(verification.get("warnings") or []),
        "checks": checks,
    }


def _require_bound_review_material(
    store: LocalStore,
    cycle_id: str,
    assignment_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignment = store.get_review_assignment(
        validate_safe_id(assignment_id, field="assignment_id")
    )
    if str(assignment.get("cycle_id") or "") != str(cycle.get("cycle_id") or ""):
        raise ValueError("review assignment does not belong to cycle")
    if str(assignment.get("status") or "") != "pending":
        raise ValueError("review assignment is not pending")
    if str(cycle.get("status") or "") not in _ACTIVE_CYCLE_STATUSES:
        raise ValueError("review cycle is not accepting reviewer execution")

    run = store.get_run(str(cycle.get("run_id") or ""))
    if str(run.get("status") or "") != "needs_review":
        raise ValueError("reviewer execution requires run status needs_review")
    if str(run.get("stage7_review_cycle_id") or "") != str(cycle.get("cycle_id") or ""):
        raise ValueError("run is not bound to this Stage 7 review cycle")
    if str(run.get("provider_id") or "") == str(assignment.get("provider_id") or ""):
        raise ValueError("implementer provider cannot execute its own review")

    computed_scope = compute_run_scope_fingerprint(
        run, run.get("packet") if isinstance(run.get("packet"), dict) else None
    )
    if not computed_scope or computed_scope != str(run.get("scope_fingerprint") or ""):
        raise ValueError("run scope fingerprint is stale")
    if computed_scope != str(cycle.get("scope_fingerprint") or ""):
        raise ValueError("review cycle scope fingerprint is stale")
    if str(run.get("constitution_hash") or "") != str(cycle.get("constitution_hash") or ""):
        raise ValueError("review cycle Constitution hash is stale")
    if str(run.get("constitution_lease_id") or "") != str(
        cycle.get("constitution_lease_id") or ""
    ):
        raise ValueError("review cycle Constitution lease is stale")

    evidence = store.get_latest_execution_evidence(str(run.get("id") or ""))
    problems = validate_evidence_for_storage(evidence)
    if problems:
        raise ValueError("execution evidence invalid: " + "; ".join(problems))
    if str(evidence.get("evidence_id") or "") != str(cycle.get("evidence_id") or ""):
        raise ValueError("review cycle is not bound to latest execution evidence")
    if str(evidence.get("evidence_fingerprint") or "") != str(
        cycle.get("evidence_fingerprint") or ""
    ):
        raise ValueError("review cycle evidence fingerprint is stale")

    worktree_raw = evidence.get("worktree_path") or run.get("worktree_path")
    if not worktree_raw:
        raise ValueError("review execution worktree missing")
    root = Path(str(worktree_raw)).resolve()
    if not root.is_dir():
        raise ValueError("review execution worktree does not exist")
    if root.is_symlink():
        raise ValueError("review execution worktree cannot be a symlink")
    return cycle, assignment, run, evidence, root


def _collect_snapshot(root: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    baseline = str(evidence.get("approved_baseline_commit") or evidence.get("baseline_commit") or "")
    if not baseline:
        raise ValueError("execution evidence missing approved baseline")
    manifest = collect_changed_file_manifest(root, baseline_commit=baseline)
    patch = collect_patch_evidence(root, baseline_commit=baseline)
    if not manifest.get("complete"):
        raise ValueError(
            "changed-file manifest recollection failed: "
            + "; ".join(manifest.get("blocking_reasons") or ["unknown"])
        )
    if not patch.get("complete"):
        raise ValueError(
            "patch recollection failed: "
            + "; ".join(patch.get("blocking_reasons") or ["unknown"])
        )
    return {
        "manifest_fingerprint": manifest.get("manifest_fingerprint"),
        "patch_fingerprint": patch.get("patch_fingerprint"),
        "head_commit": manifest.get("head_commit"),
        "file_count": manifest.get("file_count"),
        "files_changed": list(manifest.get("files_changed") or []),
        "files": list(manifest.get("files") or []),
        "diff_stat": manifest.get("diff_stat") or "",
        "patch_size": patch.get("patch_size"),
    }


def _assert_snapshot_matches_evidence(snapshot: dict[str, Any], evidence: dict[str, Any]) -> None:
    expected = {
        "manifest_fingerprint": evidence.get("manifest_fingerprint"),
        "patch_fingerprint": evidence.get("patch_fingerprint"),
        "head_commit": evidence.get("final_head_sha") or evidence.get("post_run_head_sha"),
    }
    for key, value in expected.items():
        if str(snapshot.get(key) or "") != str(value or ""):
            raise ValueError(f"review worktree {key} does not match immutable execution evidence")
    if list(snapshot.get("files_changed") or []) != list(evidence.get("files_changed") or []):
        raise ValueError("review worktree changed-file list does not match execution evidence")


def build_review_packet_record(
    *,
    cycle: dict[str, Any],
    assignment: dict[str, Any],
    run: dict[str, Any],
    evidence: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    packet = run.get("packet") if isinstance(run.get("packet"), dict) else {}
    packet_id = "rp-" + hashlib.sha256(
        (
            str(cycle.get("cycle_id"))
            + "|"
            + str(assignment.get("assignment_id"))
            + "|"
            + str(evidence.get("evidence_fingerprint"))
        ).encode("utf-8")
    ).hexdigest()[:20]
    material = {
        "packet_id": packet_id,
        "cycle_id": cycle.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "run_id": run.get("id"),
        "reviewer_id": assignment.get("reviewer_id"),
        "reviewer_provider_id": assignment.get("provider_id"),
        "reviewer_role": assignment.get("role"),
        "implementer_provider_id": cycle.get("implementer_provider_id"),
        "evidence_id": evidence.get("evidence_id"),
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "scope_fingerprint": cycle.get("scope_fingerprint"),
        "constitution_hash": cycle.get("constitution_hash"),
        "constitution_lease_id": cycle.get("constitution_lease_id"),
        "repository": run.get("repository"),
        "approved_baseline_commit": evidence.get("approved_baseline_commit"),
        "final_head_sha": evidence.get("final_head_sha"),
        "execution_branch": evidence.get("execution_branch"),
        "objective": packet.get("objective") or run.get("result_summary") or "Independent review",
        "acceptance_criteria": list(packet.get("acceptance_criteria") or []),
        "allowed_files": list(evidence.get("allowed_files") or packet.get("allowed_files") or []),
        "forbidden_files": list(evidence.get("forbidden_files") or packet.get("forbidden_files") or []),
        "review_material": {
            "manifest_fingerprint": snapshot.get("manifest_fingerprint"),
            "patch_fingerprint": snapshot.get("patch_fingerprint"),
            "head_commit": snapshot.get("head_commit"),
            "file_count": snapshot.get("file_count"),
            "files_changed": list(snapshot.get("files_changed") or []),
            "files": list(snapshot.get("files") or []),
            "diff_stat": snapshot.get("diff_stat") or "",
            "patch_size": snapshot.get("patch_size"),
            "verification": _stable_verification(
                evidence.get("verification") if isinstance(evidence.get("verification"), dict) else {}
            ),
        },
        "blind_context": {
            "other_reports_included": False,
            "other_findings_included": False,
            "aggregate_included": False,
            "founder_decision_included": False,
        },
        "instructions": [
            "Inspect the repository in the current read-only working directory.",
            "Review the exact changed files and acceptance criteria independently.",
            "Do not edit files, create files, commit, merge, deploy, or access secrets.",
            "Do not claim consensus or infer other reviewer conclusions.",
            "Return exactly one JSON object and no markdown or prose.",
        ],
        "output_schema": {
            "verdict": "pass | changes_required | block",
            "summary": "string",
            "findings": [
                {
                    "severity": "critical | high | medium | low | info",
                    "category": "string",
                    "blocking": "boolean (critical/high are forced blocking)",
                    "summary": "string",
                    "evidence": "specific code/path/behavior evidence",
                    "recommendation": "specific repair",
                    "file": "optional repository path",
                    "line": "optional integer",
                    "law_ids": "optional list of Constitution law IDs",
                }
            ],
        },
    }
    record = {
        "schema": REVIEW_PACKET_SCHEMA,
        **material,
        "created_at": utc_now_iso(),
        "immutable": True,
    }
    record["packet_fingerprint"] = _fingerprint(REVIEW_PACKET_SCHEMA, material)
    size = len(json.dumps(record, sort_keys=True, default=str).encode("utf-8"))
    if size > REVIEW_PACKET_MAX_BYTES:
        raise ValueError(f"review packet exceeds bounded size: {size} > {REVIEW_PACKET_MAX_BYTES}")
    record["packet_size_bytes"] = size
    return record


def validate_review_packet_for_storage(
    packet: dict[str, Any],
    *,
    cycle: dict[str, Any],
    assignment: dict[str, Any],
    run: dict[str, Any],
    evidence: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    for field in (
        "packet_id",
        "cycle_id",
        "assignment_id",
        "run_id",
        "reviewer_id",
        "reviewer_provider_id",
        "evidence_id",
        "evidence_fingerprint",
        "scope_fingerprint",
        "constitution_hash",
        "constitution_lease_id",
        "review_material",
        "blind_context",
        "packet_fingerprint",
    ):
        if packet.get(field) in (None, ""):
            problems.append(f"review packet missing {field}")
    bindings = {
        "cycle_id": cycle.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "run_id": run.get("id"),
        "reviewer_id": assignment.get("reviewer_id"),
        "reviewer_provider_id": assignment.get("provider_id"),
        "implementer_provider_id": cycle.get("implementer_provider_id"),
        "evidence_id": evidence.get("evidence_id"),
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "scope_fingerprint": cycle.get("scope_fingerprint"),
        "constitution_hash": cycle.get("constitution_hash"),
        "constitution_lease_id": cycle.get("constitution_lease_id"),
    }
    for field, expected in bindings.items():
        if str(packet.get(field) or "") != str(expected or ""):
            problems.append(f"review packet {field} mismatch")
    blind = packet.get("blind_context") if isinstance(packet.get("blind_context"), dict) else {}
    if any(bool(blind.get(key)) for key in blind):
        problems.append("review packet contains non-blind context")
    material = {
        key: packet.get(key)
        for key in (
            "packet_id",
            "cycle_id",
            "assignment_id",
            "run_id",
            "reviewer_id",
            "reviewer_provider_id",
            "reviewer_role",
            "implementer_provider_id",
            "evidence_id",
            "evidence_fingerprint",
            "scope_fingerprint",
            "constitution_hash",
            "constitution_lease_id",
            "repository",
            "approved_baseline_commit",
            "final_head_sha",
            "execution_branch",
            "objective",
            "acceptance_criteria",
            "allowed_files",
            "forbidden_files",
            "review_material",
            "blind_context",
            "instructions",
            "output_schema",
        )
    }
    if packet.get("packet_fingerprint") != _fingerprint(REVIEW_PACKET_SCHEMA, material):
        problems.append("review packet fingerprint mismatch")
    size = len(json.dumps(packet, sort_keys=True, default=str).encode("utf-8"))
    if size > REVIEW_PACKET_MAX_BYTES + 1_024:
        problems.append("review packet exceeds bounded storage size")
    return problems


def build_verified_blind_review_packet(
    store: LocalStore,
    cycle_id: str,
    assignment_id: str,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    cycle, assignment, run, evidence, root = _require_bound_review_material(
        store, cycle_id, assignment_id
    )
    snapshot = _collect_snapshot(root, evidence)
    _assert_snapshot_matches_evidence(snapshot, evidence)
    packet = build_review_packet_record(
        cycle=cycle,
        assignment=assignment,
        run=run,
        evidence=evidence,
        snapshot=snapshot,
    )
    return packet, snapshot, root


def build_review_command(provider_id: str, executable: str) -> dict[str, Any]:
    pid = str(provider_id or "").strip().lower()
    contract = REVIEW_COMMAND_CONTRACTS.get(pid)
    if not contract or contract.get("read_only") is not True:
        raise ValueError(f"no approved read-only review command contract for provider {pid}")
    return {
        "provider_id": pid,
        "contract_id": contract["contract_id"],
        "argv": [str(executable), *list(contract["argv_tail"])],
        "prompt_transport": contract["prompt_transport"],
        "output_protocol": contract["output_protocol"],
        "read_only": True,
    }


def _validate_payload_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("review output must be a JSON object")
    unknown = set(payload) - _ALLOWED_REPORT_KEYS
    if unknown:
        raise ValueError(f"review output contains unknown keys: {sorted(unknown)}")
    if "verdict" not in payload or "findings" not in payload:
        raise ValueError("review output requires verdict and findings")
    if not isinstance(payload.get("findings"), list):
        raise ValueError("review findings must be a list")
    if "summary" in payload and not isinstance(payload.get("summary"), str):
        raise ValueError("review summary must be a string")
    for index, finding in enumerate(payload.get("findings") or []):
        if not isinstance(finding, dict):
            raise ValueError(f"review finding {index} must be an object")
        unknown_finding = set(finding) - _ALLOWED_FINDING_KEYS
        if unknown_finding:
            raise ValueError(
                f"review finding {index} contains unknown keys: {sorted(unknown_finding)}"
            )
    return payload


def parse_strict_review_output(provider_id: str, stdout: str) -> dict[str, Any]:
    text = str(stdout or "")
    if not text.strip():
        raise ValueError("reviewer produced no structured output")
    candidates: list[Any] = []
    direct = text.strip()
    try:
        parsed = json.loads(direct)
        if isinstance(parsed, dict) and "verdict" in parsed:
            candidates.append(parsed)
    except json.JSONDecodeError:
        pass

    if str(provider_id).lower() == "codex":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if str(item.get("type") or "") != "agent_message":
                continue
            message = item.get("text") or item.get("content")
            if not isinstance(message, str):
                continue
            if message.strip().startswith("```"):
                raise ValueError("review output must not use markdown fences")
            try:
                candidate = json.loads(message.strip())
            except json.JSONDecodeError as exc:
                raise ValueError("reviewer agent_message is not strict JSON") from exc
            candidates.append(candidate)

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = _validate_payload_shape(candidate)
        key = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    if len(unique) != 1:
        raise ValueError(f"review output must contain exactly one unambiguous review object, found {len(unique)}")
    return unique[0]


def build_review_execution_record(
    *,
    packet: dict[str, Any],
    assignment: dict[str, Any],
    command: dict[str, Any] | None,
    health: dict[str, Any] | None,
    process_result: dict[str, Any] | None,
    pre_snapshot: dict[str, Any],
    post_snapshot: dict[str, Any],
    status: str,
    error: str = "",
    report_fingerprint: str | None = None,
) -> dict[str, Any]:
    process = process_result or {}
    execution_id = f"rx-{uuid.uuid4().hex[:18]}"
    material = {
        "execution_id": execution_id,
        "cycle_id": packet.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "packet_id": packet.get("packet_id"),
        "packet_fingerprint": packet.get("packet_fingerprint"),
        "provider_id": assignment.get("provider_id"),
        "reviewer_id": assignment.get("reviewer_id"),
        "command_contract_id": (command or {}).get("contract_id"),
        "read_only": bool((command or {}).get("read_only")),
        "provider_version": (health or {}).get("version"),
        "provider_executable": Path(str((health or {}).get("executable") or "")).name,
        "status": status,
        "error": redact_text(error)[:500],
        "process": {
            "exit_code": process.get("exit_code"),
            "pid": process.get("pid"),
            "timed_out": bool(process.get("timed_out")),
            "cancelled": bool(process.get("cancelled")),
            "cleanup_ok": process.get("cleanup_ok"),
            "process_group_isolated": process.get("process_group_isolated"),
            "stdout_sha256": process.get("stdout_sha256") or redact_hash(process.get("stdout") or ""),
            "stderr_sha256": process.get("stderr_sha256") or redact_hash(process.get("stderr") or ""),
            "argv": redact_argv(process.get("argv") or (command or {}).get("argv") or []),
        },
        "pre_snapshot": {
            key: pre_snapshot.get(key)
            for key in ("manifest_fingerprint", "patch_fingerprint", "head_commit", "files_changed")
        },
        "post_snapshot": {
            key: post_snapshot.get(key)
            for key in ("manifest_fingerprint", "patch_fingerprint", "head_commit", "files_changed")
        },
        "worktree_unchanged": all(
            pre_snapshot.get(key) == post_snapshot.get(key)
            for key in ("manifest_fingerprint", "patch_fingerprint", "head_commit", "files_changed")
        ),
        "report_fingerprint": report_fingerprint,
    }
    record = {
        "schema": REVIEW_EXECUTION_SCHEMA,
        **material,
        "created_at": utc_now_iso(),
        "immutable": True,
    }
    record["execution_fingerprint"] = _fingerprint(REVIEW_EXECUTION_SCHEMA, material)
    return record


def validate_review_execution_record(
    record: dict[str, Any],
    *,
    packet: dict[str, Any],
    assignment: dict[str, Any],
    report: dict[str, Any] | None = None,
) -> list[str]:
    problems: list[str] = []
    for field in (
        "execution_id",
        "cycle_id",
        "assignment_id",
        "packet_id",
        "packet_fingerprint",
        "provider_id",
        "reviewer_id",
        "status",
        "pre_snapshot",
        "post_snapshot",
        "execution_fingerprint",
    ):
        if record.get(field) in (None, ""):
            problems.append(f"review execution missing {field}")
    bindings = {
        "cycle_id": packet.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "packet_id": packet.get("packet_id"),
        "packet_fingerprint": packet.get("packet_fingerprint"),
        "provider_id": assignment.get("provider_id"),
        "reviewer_id": assignment.get("reviewer_id"),
    }
    for field, expected in bindings.items():
        if str(record.get(field) or "") != str(expected or ""):
            problems.append(f"review execution {field} mismatch")
    if record.get("status") == "succeeded":
        process = record.get("process") if isinstance(record.get("process"), dict) else {}
        if process.get("exit_code") != 0:
            problems.append("successful review execution requires exit code zero")
        if process.get("cleanup_ok") is not True:
            problems.append("successful review execution requires confirmed process cleanup")
        if record.get("read_only") is not True:
            problems.append("successful review execution requires read-only command contract")
        if record.get("worktree_unchanged") is not True:
            problems.append("successful review execution requires unchanged worktree")
        if not report or str(record.get("report_fingerprint") or "") != str(
            report.get("report_fingerprint") or ""
        ):
            problems.append("successful review execution report fingerprint mismatch")
    material = {
        key: record.get(key)
        for key in (
            "execution_id",
            "cycle_id",
            "assignment_id",
            "packet_id",
            "packet_fingerprint",
            "provider_id",
            "reviewer_id",
            "command_contract_id",
            "read_only",
            "provider_version",
            "provider_executable",
            "status",
            "error",
            "process",
            "pre_snapshot",
            "post_snapshot",
            "worktree_unchanged",
            "report_fingerprint",
        )
    }
    if record.get("execution_fingerprint") != _fingerprint(REVIEW_EXECUTION_SCHEMA, material):
        problems.append("review execution fingerprint mismatch")
    return problems


def _prompt_bytes(packet: dict[str, Any]) -> bytes:
    prompt = {
        "system": "Independent blind code review. The working directory is read-only.",
        "review_packet": packet,
        "response_rule": "Return exactly one JSON object matching output_schema; no markdown or prose.",
    }
    return json.dumps(prompt, sort_keys=True, separators=(",", ":")).encode("utf-8")


def execute_independent_review_assignment(
    store: LocalStore,
    cycle_id: str,
    assignment_id: str,
    *,
    actor: str = "system",
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    packet, pre_snapshot, root = build_verified_blind_review_packet(
        store, cycle_id, assignment_id
    )
    packet = store.save_review_packet_atomic(packet=packet, actor=actor)
    cycle = store.get_review_cycle(cycle_id)
    assignment = store.get_review_assignment(assignment_id)
    provider_id = str(assignment.get("provider_id") or "")
    provider = store.get_provider_record(provider_id)
    command: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    process_result: dict[str, Any] = {}

    def fail(error: str, *, post_snapshot: dict[str, Any] | None = None) -> None:
        execution = build_review_execution_record(
            packet=packet,
            assignment=assignment,
            command=command,
            health=health,
            process_result=process_result,
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot or pre_snapshot,
            status="failed",
            error=error,
        )
        store.record_review_execution_atomic(execution=execution, actor=actor)
        raise ValueError(error)

    if store.get_execution_control().get("kill_switch_active"):
        fail("kill switch active; reviewer process not started")
    if not provider.get("enabled", True):
        fail("reviewer provider disabled")
    if not provider.get("constitution_acknowledged"):
        fail("reviewer provider has not acknowledged the Constitution")
    if str(provider.get("constitution_hash") or "") != str(cycle.get("constitution_hash") or ""):
        fail("reviewer provider Constitution hash does not match review cycle")

    health = health_check_provider(provider_id, provider, force_compat=True)
    if not health.get("live_ready"):
        fail(
            "reviewer provider not live-ready: "
            + "; ".join(health.get("unsupported_reasons") or ["unavailable"])
        )
    executable = str(health.get("executable") or "")
    try:
        command = build_review_command(provider_id, executable)
    except ValueError as exc:
        fail(str(exc))

    supervisor = get_process_supervisor()
    process_key = f"review-{assignment_id}"

    def on_event(_event: dict[str, Any]) -> None:
        if store.get_execution_control().get("kill_switch_active"):
            try:
                supervisor.cancel(process_key)
            except Exception:
                pass

    process_result = supervisor.run(
        run_id=process_key,
        argv=list(command["argv"]),
        cwd=root,
        timeout_seconds=max(30, min(REVIEW_TIMEOUT_MAX_SECONDS, int(timeout_seconds))),
        provider_id=provider_id,
        on_event=on_event,
        use_provider_env_allowlist=True,
        stdin_bytes=_prompt_bytes(packet),
    )

    try:
        post_snapshot = _collect_snapshot(root, store.get_evidence_by_id(str(packet["evidence_id"])))
    except Exception as exc:
        fail(f"post-review worktree proof failed: {exc}")
    unchanged = all(
        pre_snapshot.get(key) == post_snapshot.get(key)
        for key in ("manifest_fingerprint", "patch_fingerprint", "head_commit", "files_changed")
    )
    if not unchanged:
        fail("reviewer process mutated the governed worktree", post_snapshot=post_snapshot)
    if not process_result.get("ok") or process_result.get("exit_code") != 0:
        fail("reviewer process did not exit successfully", post_snapshot=post_snapshot)
    if process_result.get("cleanup_ok") is not True:
        fail("reviewer process-tree cleanup was not confirmed", post_snapshot=post_snapshot)

    try:
        payload = parse_strict_review_output(provider_id, str(process_result.get("stdout") or ""))
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload=payload,
        )
    except Exception as exc:
        fail(f"reviewer output rejected: {exc}", post_snapshot=post_snapshot)

    execution = build_review_execution_record(
        packet=packet,
        assignment=assignment,
        command=command,
        health=health,
        process_result=process_result,
        pre_snapshot=pre_snapshot,
        post_snapshot=post_snapshot,
        status="succeeded",
        report_fingerprint=report.get("report_fingerprint"),
    )
    return store.submit_review_report_atomic(
        cycle_id=cycle_id,
        assignment_id=assignment_id,
        report=report,
        findings=findings,
        actor=str(assignment.get("reviewer_id") or actor),
        execution=execution,
    )
''',
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# SQLite schema v5: immutable review packets and reviewer process evidence
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "db.py"
text = path.read_text(encoding="utf-8")
text = replace_once(text, "SCHEMA_VERSION = 4", "SCHEMA_VERSION = 5", label="schema version")
text = replace_once(
    text,
    '''CREATE INDEX IF NOT EXISTS idx_review_events_cycle ON review_events(cycle_id, created_at);
"""
''',
    '''CREATE INDEX IF NOT EXISTS idx_review_events_cycle ON review_events(cycle_id, created_at);

CREATE TABLE IF NOT EXISTS review_packets (
  packet_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
  assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
  packet_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1,
  UNIQUE(assignment_id)
);

CREATE TABLE IF NOT EXISTS review_executions (
  execution_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
  assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
  packet_id TEXT NOT NULL REFERENCES review_packets(packet_id),
  provider_id TEXT NOT NULL,
  status TEXT NOT NULL,
  execution_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_review_executions_assignment
  ON review_executions(assignment_id, created_at);
"""
''',
    label="schema tables anchor",
)
text = replace_once(
    text,
    '''                    if current < 4:
                        self._migrate_to_v4(conn)
                    if current < SCHEMA_VERSION:
''',
    '''                    if current < 4:
                        self._migrate_to_v4(conn)
                    if current < 5:
                        self._migrate_to_v5(conn)
                    if current < SCHEMA_VERSION:
''',
    label="migration dispatch",
)
# Insert migration before transaction context manager.
anchor = "    @contextmanager\n    def transaction(self) -> Iterator[sqlite3.Connection]:\n"
migration = '''    def _migrate_to_v5(self, conn: sqlite3.Connection) -> None:
        """Add immutable review packets and reviewer execution evidence."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS review_packets (
              packet_id TEXT PRIMARY KEY,
              cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
              assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
              packet_fingerprint TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1,
              UNIQUE(assignment_id)
            );
            CREATE TABLE IF NOT EXISTS review_executions (
              execution_id TEXT PRIMARY KEY,
              cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
              assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
              packet_id TEXT NOT NULL REFERENCES review_packets(packet_id),
              provider_id TEXT NOT NULL,
              status TEXT NOT NULL,
              execution_fingerprint TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_review_executions_assignment
              ON review_executions(assignment_id, created_at);
            """
        )

'''
if text.count(anchor) != 1:
    raise RuntimeError("transaction anchor missing")
text = text.replace(anchor, migration + anchor, 1)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stage7 storage methods and atomic report + process commit
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
anchor = "    # —— Stage 7 independent reviews ——\n"
methods = '''    # —— Stage 7 Packet 7B review packets/executions ——
    def save_review_packet_atomic(self, *, packet: dict[str, Any], actor: str) -> dict[str, Any]:
        from buildforme.review_execution import validate_review_packet_for_storage

        record = dict(packet)
        cycle_id = str(record.get("cycle_id") or "")
        assignment_id = str(record.get("assignment_id") or "")
        packet_id = str(record.get("packet_id") or "")
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, payload_json, status FROM review_cycles WHERE id=?",
                (cycle_id,),
            ).fetchone()
            assignment_row = conn.execute(
                "SELECT payload_json, status, cycle_id FROM review_assignments WHERE id=?",
                (assignment_id,),
            ).fetchone()
            if not cycle_row or not assignment_row:
                raise ValueError("review packet cycle or assignment not found")
            if str(assignment_row[2]) != cycle_id:
                raise ValueError("review packet assignment cycle mismatch")
            if str(assignment_row[1]) != "pending":
                raise ValueError("review packet requires pending assignment")
            cycle = loads(cycle_row[1], {})
            cycle["status"] = cycle_row[2]
            assignment = loads(assignment_row[0], {})
            assignment["status"] = assignment_row[1]
            run_row = conn.execute(
                "SELECT payload_json FROM runs WHERE id=?", (str(cycle_row[0]),)
            ).fetchone()
            evidence_row = conn.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id=?",
                (str(cycle.get("evidence_id") or ""),),
            ).fetchone()
            if not run_row or not evidence_row:
                raise ValueError("review packet run or evidence not found")
            run = loads(run_row[0], {})
            evidence = loads(evidence_row[0], {})
            problems = validate_review_packet_for_storage(
                record,
                cycle=cycle,
                assignment=assignment,
                run=run,
                evidence=evidence,
            )
            if problems:
                raise ValueError("review packet rejected: " + "; ".join(problems))
            existing = conn.execute(
                "SELECT payload_json, packet_fingerprint FROM review_packets WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if existing:
                prior = loads(existing[0], {})
                if str(existing[1] or "") != str(record.get("packet_fingerprint") or ""):
                    raise ValueError("review packet mutation forbidden")
                return prior
            conn.execute(
                "INSERT INTO review_packets(packet_id, cycle_id, assignment_id, packet_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,1)",
                (
                    packet_id,
                    cycle_id,
                    assignment_id,
                    record.get("packet_fingerprint"),
                    dumps(record),
                    record.get("created_at") or utc_now_iso(),
                ),
            )
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("rve"),
                    cycle_id,
                    "review_packet_bound",
                    "Immutable blind-review packet bound to assignment",
                    actor,
                    dumps({"assignment_id": assignment_id, "packet_id": packet_id}),
                    utc_now_iso(),
                ),
            )
        return record

    def get_review_packet_for_assignment(self, assignment_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE assignment_id=?",
                (str(assignment_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Review packet not found for assignment: {assignment_id}")
        return loads(row[0], {})

    def list_review_execution_attempts(self, assignment_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM review_executions WHERE assignment_id=? ORDER BY created_at, execution_id",
                (str(assignment_id),),
            ).fetchall()
        return [loads(row[0], {}) for row in rows]

    def record_review_execution_atomic(self, *, execution: dict[str, Any], actor: str) -> dict[str, Any]:
        from buildforme.review_execution import validate_review_execution_record

        record = dict(execution)
        if str(record.get("status") or "") == "succeeded":
            raise ValueError("successful reviewer execution must commit atomically with its report")
        assignment_id = str(record.get("assignment_id") or "")
        with self.db.transaction() as conn:
            assignment_row = conn.execute(
                "SELECT payload_json FROM review_assignments WHERE id=?",
                (assignment_id,),
            ).fetchone()
            packet_row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE packet_id=? AND assignment_id=?",
                (str(record.get("packet_id") or ""), assignment_id),
            ).fetchone()
            if not assignment_row or not packet_row:
                raise ValueError("review execution assignment or packet not found")
            assignment = loads(assignment_row[0], {})
            packet = loads(packet_row[0], {})
            problems = validate_review_execution_record(
                record, packet=packet, assignment=assignment, report=None
            )
            if problems:
                raise ValueError("review execution rejected: " + "; ".join(problems))
            if conn.execute(
                "SELECT execution_id FROM review_executions WHERE execution_id=?",
                (str(record.get("execution_id") or ""),),
            ).fetchone():
                raise ValueError("review execution evidence is append-only")
            conn.execute(
                "INSERT INTO review_executions(execution_id, cycle_id, assignment_id, packet_id, provider_id, status, execution_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    record["execution_id"],
                    record["cycle_id"],
                    assignment_id,
                    record["packet_id"],
                    record["provider_id"],
                    record["status"],
                    record["execution_fingerprint"],
                    dumps(record),
                    record.get("created_at") or utc_now_iso(),
                ),
            )
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("rve"),
                    record["cycle_id"],
                    "review_execution_failed",
                    "Automated reviewer execution failed closed",
                    actor,
                    dumps({"assignment_id": assignment_id, "execution_id": record["execution_id"]}),
                    utc_now_iso(),
                ),
            )
        return record

'''
if text.count(anchor) != 1:
    raise RuntimeError("Stage 7 storage anchor missing")
text = text.replace(anchor, methods + anchor, 1)

# Require execution argument on report commit.
text = replace_once(
    text,
    '''        findings: list[dict[str, Any]],
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
            raise ValueError("direct review report submission disabled; authenticated reviewer execution required")
        execution_record = dict(execution)
        now = utc_now_iso()
''',
    label="report signature",
)
# Insert packet/execution validation immediately after report validation.
text = replace_once(
    text,
    '''            problems = validate_report_for_storage(report, cycle, assignment)
            if problems:
                raise ValueError("review report rejected: " + "; ".join(problems))
            canonical_findings = list(report.get("findings") or [])
''',
    '''            problems = validate_report_for_storage(report, cycle, assignment)
            if problems:
                raise ValueError("review report rejected: " + "; ".join(problems))
            packet_row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE assignment_id=?",
                (str(assignment_id),),
            ).fetchone()
            if not packet_row:
                raise ValueError("authenticated reviewer execution requires immutable review packet")
            review_packet = loads(packet_row[0], {})
            execution_problems = validate_review_execution_record(
                execution_record,
                packet=review_packet,
                assignment=assignment,
                report=report,
            )
            if execution_problems:
                raise ValueError("review execution rejected: " + "; ".join(execution_problems))
            if str(execution_record.get("status") or "") != "succeeded":
                raise ValueError("review report requires successful reviewer execution")
            if conn.execute(
                "SELECT execution_id FROM review_executions WHERE execution_id=?",
                (str(execution_record.get("execution_id") or ""),),
            ).fetchone():
                raise ValueError("review execution evidence is append-only")
            canonical_findings = list(report.get("findings") or [])
''',
    label="report execution validation",
)
# Insert execution row before report row, preserving atomicity.
text = replace_once(
    text,
    '''            conn.execute(
                "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
''',
    '''            conn.execute(
                "INSERT INTO review_executions(execution_id, cycle_id, assignment_id, packet_id, provider_id, status, execution_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    execution_record["execution_id"],
                    execution_record["cycle_id"],
                    execution_record["assignment_id"],
                    execution_record["packet_id"],
                    execution_record["provider_id"],
                    execution_record["status"],
                    execution_record["execution_fingerprint"],
                    dumps(execution_record),
                    execution_record.get("created_at") or now,
                ),
            )
            conn.execute(
                "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
''',
    label="atomic execution insert",
)
# Include execution id in event metadata.
text = text.replace(
    'dumps({"assignment_id": assignment_id, "report_id": report_id, "submitted": submitted}),',
    'dumps({"assignment_id": assignment_id, "report_id": report_id, "execution_id": execution_record.get("execution_id"), "submitted": submitted}),',
    1,
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# LocalStore wrappers
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "storage.py"
text = path.read_text(encoding="utf-8")
anchor = "    # —— Internals ——\n"
wrappers = '''    # —— Stage 7 Packet 7B review packets/executions ——
    def save_review_packet_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.save_review_packet_atomic(**kwargs)

    def get_review_packet_for_assignment(self, assignment_id: str) -> dict[str, Any]:
        return self.s6.get_review_packet_for_assignment(assignment_id)

    def list_review_execution_attempts(self, assignment_id: str) -> list[dict[str, Any]]:
        return self.s6.list_review_execution_attempts(assignment_id)

    def record_review_execution_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.record_review_execution_atomic(**kwargs)

'''
if text.count(anchor) != 1:
    raise RuntimeError("storage internals anchor missing")
text = text.replace(anchor, wrappers + anchor, 1)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Review service: direct/manual report ingestion is no longer an authority path
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_service.py"
text = path.read_text(encoding="utf-8")
start = text.index("def submit_independent_review_report(")
end = text.index("\n\ndef aggregate_independent_review_cycle(", start)
replacement = '''def submit_independent_review_report(
    store: LocalStore,
    cycle_id: str,
    assignment_id: str,
    *,
    payload: dict[str, Any],
    actor: str = "reviewer",
) -> dict[str, Any]:
    """Direct report ingestion is intentionally disabled in Packet 7B.

    Reports must originate from execute_independent_review_assignment(), which
    binds a code-owned read-only process, immutable packet, before/after worktree
    proof, and reviewer process evidence in the same transaction as the report.
    """
    del store, cycle_id, assignment_id, payload, actor
    raise ValueError(
        "direct review report submission disabled; execute the bound reviewer assignment"
    )
'''
text = text[:start] + replacement + text[end:]
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# API: replace manual submit endpoint with founder-gated execute endpoint
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "server.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
    submit_independent_review_report,
)
''',
    '''from buildforme.review_execution import execute_independent_review_assignment
from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
)
''',
    label="server review imports",
)
text = replace_once(
    text,
    '''        if path.startswith("/api/review-cycles/") and "/assignments/" in path and path.endswith("/submit"):
            self._stage7_review_action(path, "submit")
            return
''',
    '''        if path.startswith("/api/review-cycles/") and "/assignments/" in path and path.endswith("/execute"):
            self._stage7_review_action(path, "execute")
            return
''',
    label="server execute route",
)
text = replace_once(
    text,
    '''            elif action == "submit":
                rest = path.removeprefix("/api/review-cycles/").removesuffix("/submit").strip("/")
                cycle_id, assignment_id = rest.split("/assignments/", 1)
                result = submit_independent_review_report(
                    self._store(),
                    cycle_id,
                    assignment_id,
                    payload=payload.get("report") if isinstance(payload.get("report"), dict) else payload,
                    actor=actor,
                )
''',
    '''            elif action == "execute":
                rest = path.removeprefix("/api/review-cycles/").removesuffix("/execute").strip("/")
                cycle_id, assignment_id = rest.split("/assignments/", 1)
                if payload.get("argv") or payload.get("command") or payload.get("executable"):
                    raise ValueError("reviewer command authority is code-owned and cannot be supplied")
                result = execute_independent_review_assignment(
                    self._store(),
                    cycle_id,
                    assignment_id,
                    actor=actor,
                    timeout_seconds=max(30, min(1800, int(payload.get("timeout_seconds") or 900))),
                )
''',
    label="server execute action",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Non-weakenable automated execution policy on new cycles
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_contracts.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''        "founder_override_blocking_findings": False,
        **(policy or {}),
    }
''',
    '''        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
        **(policy or {}),
    }
''',
    label="review policy automated flag",
)
text = replace_once(
    text,
    '''        "founder_override_blocking_findings": False,
    }
''',
    '''        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
''',
    label="required policy values",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: schema expectations and Packet 7A helper now uses synthetic process proof
# ---------------------------------------------------------------------------
for rel in ("tests/test_stage6_execution.py", "tests/test_stage7_review_authority.py"):
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    text = text.replace('self.assertEqual(p["schema_version"], 4)', 'self.assertEqual(p["schema_version"], 5)')
    text = text.replace('self.assertEqual(SCHEMA_VERSION, 4)', 'self.assertEqual(SCHEMA_VERSION, 5)')
    text = text.replace('self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 4)', 'self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 5)')
    text = text.replace('def test_schema_v4(self):', 'def test_schema_v5(self):')
    path.write_text(text, encoding="utf-8")

# Shadow the old direct service helper only inside Packet 7A tests with a test-built
# packet/execution record that still goes through all Packet 7B storage validation.
path = ROOT / "tests" / "test_stage7_review_authority.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
    require_clear_independent_review,
    submit_independent_review_report,
)
''',
    '''from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
    require_clear_independent_review,
)
from buildforme.review_execution import (
    build_review_execution_record,
    build_review_packet_record,
)
''',
    label="Packet7A test imports",
)
helper_anchor = "\n\nclass Stage7ReviewAuthorityTests(unittest.TestCase):\n"
helper = '''

def submit_independent_review_report(
    store,
    cycle_id,
    assignment_id,
    *,
    payload,
    actor="reviewer",
):
    cycle = store.get_review_cycle(cycle_id)
    assignment = store.get_review_assignment(assignment_id)
    run = store.get_run(str(cycle["run_id"]))
    evidence = store.get_evidence_by_id(str(cycle["evidence_id"]))
    snapshot = {
        "manifest_fingerprint": evidence.get("manifest_fingerprint"),
        "patch_fingerprint": evidence.get("patch_fingerprint"),
        "head_commit": evidence.get("final_head_sha"),
        "file_count": evidence.get("file_count"),
        "files_changed": list(evidence.get("files_changed") or []),
        "files": list((evidence.get("changed_file_manifest") or {}).get("files") or []),
        "diff_stat": evidence.get("diff_stat") or "",
        "patch_size": 1,
    }
    packet = build_review_packet_record(
        cycle=cycle,
        assignment=assignment,
        run=run,
        evidence=evidence,
        snapshot=snapshot,
    )
    packet = store.save_review_packet_atomic(packet=packet, actor="test")
    report, findings = build_review_report_record(
        cycle=cycle,
        assignment=assignment,
        payload=payload,
    )
    process = {
        "ok": True,
        "exit_code": 0,
        "pid": 123,
        "stdout": "{}",
        "stderr": "",
        "cleanup_ok": True,
        "process_group_isolated": True,
        "argv": ["test-reviewer", "--read-only"],
    }
    execution = build_review_execution_record(
        packet=packet,
        assignment=assignment,
        command={"contract_id": "test.read-only.v1", "read_only": True, "argv": process["argv"]},
        health={"version": "test", "executable": "test-reviewer"},
        process_result=process,
        pre_snapshot=snapshot,
        post_snapshot=snapshot,
        status="succeeded",
        report_fingerprint=report["report_fingerprint"],
    )
    return store.submit_review_report_atomic(
        cycle_id=cycle_id,
        assignment_id=assignment_id,
        report=report,
        findings=findings,
        actor=actor,
        execution=execution,
    )
'''
if text.count(helper_anchor) != 1:
    raise RuntimeError("Packet7A helper anchor missing")
text = text.replace(helper_anchor, helper + helper_anchor, 1)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Packet 7B adversarial tests
# ---------------------------------------------------------------------------
(ROOT / "tests" / "test_stage7_review_execution.py").write_text(
    '''"""Adversarial tests for Packet 7B automated blind reviewer execution."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildforme.db import SCHEMA_VERSION
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.review_execution import (
    REVIEW_COMMAND_CONTRACTS,
    build_review_command,
    build_verified_blind_review_packet,
    execute_independent_review_assignment,
    parse_strict_review_output,
)
from buildforme.review_service import create_independent_review_cycle, submit_independent_review_report
from buildforme.storage import LocalStore
from buildforme.verification_manifest import collect_changed_file_manifest, collect_patch_evidence


class Stage7ReviewExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self._git("init")
        self._git("config", "user.email", "review@test.local")
        self._git("config", "user.name", "review-test")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "baseline")
        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        run = {
            "id": "run-stage7b",
            "project_id": "buildforme",
            "provider_id": "claude",
            "repository": "shanchaudary/Buildforme",
            "repository_local_path": str(self.root),
            "baseline_ref": "HEAD",
            "baseline_commit": self.baseline,
            "requested_target_branch": "feature/stage7b",
            "execution_branch": "feature/stage7b-run",
            "target_branch": "feature/stage7b-run",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-stage7b",
            "constitution_lease_fingerprint": "l" * 64,
            "packet": {
                "id": "pkt-stage7b",
                "objective": "Add subtraction function",
                "acceptance_criteria": ["sub returns a-b"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7b",
                "allowed_files": ["app.py"],
                "forbidden_files": [".env"],
            },
            "review": {"hard_blocks": [], "accept_for_pr_prep_allowed": True},
            "worktree_path": str(self.root),
            "row_version": 1,
        }
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, run["packet"])
        self.run = self.store.save_run_for_setup(run)
        manifest = collect_changed_file_manifest(self.root, baseline_commit=self.baseline)
        patch_ev = collect_patch_evidence(self.root, baseline_commit=self.baseline)
        evidence = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 100,
                "stdout": "ok",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": str(self.root),
                "baseline_commit": self.baseline,
                "head_commit": self.baseline,
                "branch": self.run["execution_branch"],
            },
            diff={"manifest": manifest, "patch_fingerprint": patch_ev["patch_fingerprint"]},
            provider_health={"version": "test", "executable": "claude"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.baseline,
            final_head_sha=self.baseline,
            execution_branch=self.run["execution_branch"],
            patch_fingerprint=patch_ev["patch_fingerprint"],
            manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        self.evidence = self.store.save_run_evidence(evidence)
        self.store.set_provider_constitution_ack(
            "codex",
            {
                "constitution_supported": True,
                "constitution_acknowledged": True,
                "constitution_version": "1.0.0",
                "constitution_hash": "c" * 64,
                "constitution_last_refresh": "now",
                "constitution_acknowledged_at": "now",
                "constitution_ack_actor": "test",
            },
        )
        result = create_independent_review_cycle(
            self.store,
            self.run["id"],
            reviewers=[
                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "grok-reviewer", "provider_id": "grok", "role": "security"},
            ],
            actor="shan",
        )
        self.cycle = result["cycle"]
        self.assignment = next(a for a in result["assignments"] if a["provider_id"] == "codex")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def _git_out(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True)

    def _fake_codex(self, *, payload=None, raw_output=None, mutate=False):
        path = Path(self.temp.name) / "codex"
        if payload is None:
            payload = {"verdict": "pass", "summary": "clear", "findings": []}
        agent_text = json.dumps(payload)
        event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": agent_text}})
        output = raw_output if raw_output is not None else event
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "_ = sys.stdin.read()\n"
            + ("pathlib.Path('reviewer-wrote.txt').write_text('bad')\n" if mutate else "")
            + f"print({output!r})\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _health(self, executable):
        return {
            "provider_id": "codex",
            "live_ready": True,
            "available": True,
            "executable": executable,
            "version": "codex-test",
            "unsupported_reasons": [],
            "auth": {"status": "ready", "probe_verified": True},
        }

    def test_schema_v5(self):
        self.assertEqual(SCHEMA_VERSION, 5)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 5)

    def test_packet_reproves_exact_worktree_and_is_blind(self):
        packet, snapshot, root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(snapshot["patch_fingerprint"], self.evidence["patch_fingerprint"])
        self.assertFalse(any(packet["blind_context"].values()))
        self.assertNotIn("reports", packet)
        self.assertNotIn("findings", packet)

    def test_packet_rejects_worktree_drift(self):
        (self.root / "app.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "immutable execution evidence"):
            build_verified_blind_review_packet(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )

    def test_command_contract_is_code_owned_read_only(self):
        command = build_review_command("codex", "/tmp/codex")
        self.assertTrue(command["read_only"])
        self.assertIn("read-only", command["argv"])
        self.assertNotIn("workspace-write", command["argv"])
        self.assertEqual(set(REVIEW_COMMAND_CONTRACTS), {"codex"})
        with self.assertRaisesRegex(ValueError, "no approved"):
            build_review_command("claude", "/tmp/claude")

    def test_strict_parser_rejects_prose_fences_and_ambiguity(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_strict_review_output("codex", "looks good")
        fenced = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "```json\\n{}\\n```"}})
        with self.assertRaisesRegex(ValueError, "markdown fences"):
            parse_strict_review_output("codex", fenced)
        one = {"verdict": "pass", "summary": "a", "findings": []}
        two = {"verdict": "pass", "summary": "b", "findings": []}
        lines = "\n".join(
            json.dumps({"item": {"type": "agent_message", "text": json.dumps(item)}})
            for item in (one, two)
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_strict_review_output("codex", lines)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_real_read_only_process_commits_execution_and_report_atomically(self):
        executable = self._fake_codex()
        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)):
            result = execute_independent_review_assignment(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )
        self.assertEqual(result["report"]["verdict"], "pass")
        attempts = self.store.list_review_execution_attempts(self.assignment["assignment_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "succeeded")
        self.assertTrue(attempts[0]["worktree_unchanged"])
        self.assertEqual(len(self.store.list_review_reports(self.cycle["cycle_id"])), 1)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_mutating_reviewer_fails_closed_without_report(self):
        executable = self._fake_codex(mutate=True)
        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)):
            with self.assertRaisesRegex(ValueError, "mutated"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        self.assertEqual(self.store.list_review_reports(self.cycle["cycle_id"]), [])
        attempts = self.store.list_review_execution_attempts(self.assignment["assignment_id"])
        self.assertEqual(attempts[-1]["status"], "failed")
        self.assertFalse(attempts[-1]["worktree_unchanged"])

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_malformed_reviewer_output_fails_closed_without_report(self):
        executable = self._fake_codex(raw_output="not-json")
        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)):
            with self.assertRaisesRegex(ValueError, "output rejected"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        self.assertEqual(self.store.list_review_reports(self.cycle["cycle_id"]), [])
        self.assertEqual(self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]["status"], "failed")

    def test_unavailable_provider_records_failure_and_no_report(self):
        health = {
            "provider_id": "codex",
            "live_ready": False,
            "available": True,
            "executable": "codex",
            "version": "test",
            "unsupported_reasons": ["authentication unknown"],
        }
        with patch("buildforme.review_execution.health_check_provider", return_value=health):
            with self.assertRaisesRegex(ValueError, "not live-ready"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        self.assertEqual(self.store.list_review_reports(self.cycle["cycle_id"]), [])
        self.assertEqual(self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]["status"], "failed")

    def test_direct_report_submission_is_disabled(self):
        with self.assertRaisesRegex(ValueError, "direct review report submission disabled"):
            submit_independent_review_report(
                self.store,
                self.cycle["cycle_id"],
                self.assignment["assignment_id"],
                payload={"verdict": "pass", "summary": "fake", "findings": []},
            )
        source = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertNotIn('path.endswith("/submit")', source)
        self.assertIn('path.endswith("/execute")', source)


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)

# Documentation truth.
path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''

## Packet 7B — automated blind reviewer execution

- Each assignment receives one immutable, fingerprinted blind-review packet.
- Before execution, Buildforme re-collects the worktree manifest and patch identity and
  requires exact equality with the bound Stage 6 execution evidence.
- Reviewer commands are code-owned. Packet 7B initially enables only the verified
  Codex `exec` read-only sandbox contract; providers without an approved auth and
  read-only command contract remain unavailable.
- The process runs through the Stage 6 supervisor with environment allowlisting,
  timeout, cancellation, process-tree cleanup proof, and kill-switch observation.
- The worktree is re-collected after review. Any change blocks the report and records
  immutable failed reviewer-execution evidence.
- Provider output must contain exactly one strict JSON review object. Markdown,
  ambiguous output, unknown fields, and authority claims are rejected.
- Successful reviewer process evidence and the report/findings commit atomically.
- Direct/manual report submission is disabled; the API exposes assignment execution.
'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7B review execution applied")
