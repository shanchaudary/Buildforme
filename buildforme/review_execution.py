"""Stage 7 Packet 7B — immutable blind-review packets and read-only reviewer execution.

Reviewer processes receive the exact bound worktree in a code-owned read-only command
contract.  Provider output is never trusted as authority: packet/worktree identity is
re-proved before and after execution, output must be one strict JSON review object,
and process evidence commits atomically with the report.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from buildforme.evidence import validate_evidence_for_storage
from buildforme.governance import (
    canonicalize_repository,
    compute_run_scope_fingerprint,
    normalize_repo_for_compare,
    validate_safe_id,
)
from buildforme.process_supervisor import get_process_supervisor
from buildforme.provider_discovery import health_check_provider
from buildforme.repository_binding import (
    normalize_remote_to_owner_name,
    resolve_registered_repository,
)
from buildforme.redaction import redact_argv, redact_hash, redact_text
from buildforme.review_contracts import build_review_report_record
from buildforme.storage import LocalStore, utc_now_iso
from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from governance.constitution_lease import validate_run_lease_against_store

REVIEW_PACKET_SCHEMA = "buildforme.review_packet.v1"
REVIEW_EXECUTION_SCHEMA = "buildforme.review_execution.v1"
REVIEW_PACKET_MAX_BYTES = 160_000
REVIEW_TIMEOUT_MAX_SECONDS = 1_800
REVIEW_FAILURE_CODES = frozenset(
    {
        "kill_switch_active",
        "provider_missing",
        "provider_disabled",
        "constitution_ack_missing",
        "constitution_ack_mismatch",
        "health_probe_failed",
        "provider_unavailable",
        "command_contract_unavailable",
        "supervisor_exception",
        "process_failed",
        "process_cleanup_unconfirmed",
        "post_snapshot_unproven",
        "worktree_mutated",
        "output_rejected",
    }
)

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


def _stable_file_metadata(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "path",
        "change_type",
        "tracked",
        "staged",
        "unstaged",
        "untracked",
        "ignored",
        "baseline_exists",
        "current_exists",
        "is_symlink",
        "symlink_target",
        "symlink_escapes",
        "size",
        "content_hash",
        "renamed_from",
    )
    out = []
    for item in files or []:
        if isinstance(item, dict):
            out.append({key: item.get(key) for key in keys})
    out.sort(key=lambda item: str(item.get("path") or ""))
    return out


def _file_metadata_fingerprint(files: list[dict[str, Any]]) -> str:
    return _fingerprint("buildforme.review_file_metadata.v1", {"files": _stable_file_metadata(files)})


def _snapshot_identity(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = snapshot or {}
    return {
        key: snapshot.get(key)
        for key in (
            "manifest_fingerprint",
            "patch_fingerprint",
            "file_metadata_fingerprint",
            "head_commit",
            "files_changed",
        )
    }


def _snapshots_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    return _snapshot_identity(left) == _snapshot_identity(right)


def _git_text(root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed: {redact_text((proc.stderr or proc.stdout or '')[:300])}"
        )
    return (proc.stdout or "").strip()


def _resolve_git_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _validate_repository_worktree_and_lease(
    store: LocalStore,
    *,
    run: dict[str, Any],
    evidence: dict[str, Any],
    root: Path,
) -> None:
    lease_result = validate_run_lease_against_store(run, store)
    if not lease_result.get("valid"):
        raise ValueError(
            "canonical Constitution lease invalid: "
            + "; ".join(lease_result.get("problems") or ["unknown"])
        )

    project = store.get_project(str(run.get("project_id") or ""))
    binding = resolve_registered_repository(store, project=project)
    expected_repo = canonicalize_repository(str(run.get("repository") or ""))
    if normalize_repo_for_compare(str(binding.get("repository") or "")) != normalize_repo_for_compare(
        expected_repo
    ):
        raise ValueError("review repository binding does not match run repository")
    registered_root = Path(str(binding.get("local_path") or "")).resolve()
    run_root = Path(str(run.get("repository_local_path") or "")).resolve()
    evidence_root = Path(str(evidence.get("repository_local_path") or "")).resolve()
    if registered_root != run_root or registered_root != evidence_root:
        raise ValueError("review repository local-path binding mismatch")

    toplevel = Path(_git_text(root, ["rev-parse", "--show-toplevel"])).resolve()
    if toplevel != root:
        raise ValueError("review worktree path is not its Git toplevel")
    common_review = _resolve_git_path(root, _git_text(root, ["rev-parse", "--git-common-dir"]))
    common_registered = _resolve_git_path(
        registered_root, _git_text(registered_root, ["rev-parse", "--git-common-dir"])
    )
    if common_review != common_registered:
        raise ValueError("review worktree does not belong to the registered repository")

    remote = normalize_remote_to_owner_name(
        _git_text(registered_root, ["config", "--get", "remote.origin.url"])
    )
    if normalize_repo_for_compare(remote) != normalize_repo_for_compare(expected_repo):
        raise ValueError("review repository remote identity mismatch")
    branch = _git_text(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    expected_branch = str(run.get("execution_branch") or evidence.get("execution_branch") or "")
    if not expected_branch or branch != expected_branch:
        raise ValueError("review worktree branch identity mismatch")
    if str(evidence.get("worktree_path") or "") and Path(
        str(evidence.get("worktree_path"))
    ).resolve() != root:
        raise ValueError("review worktree path differs from immutable execution evidence")


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
    raw_root = Path(str(worktree_raw))
    if raw_root.is_symlink():
        raise ValueError("review execution worktree cannot be a symlink")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValueError("review execution worktree does not exist")
    _validate_repository_worktree_and_lease(
        store,
        run=run,
        evidence=evidence,
        root=root,
    )
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
        "file_metadata_fingerprint": _file_metadata_fingerprint(
            list(manifest.get("files") or [])
        ),
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
    expected_metadata = _file_metadata_fingerprint(
        list((evidence.get("changed_file_manifest") or {}).get("files") or [])
    )
    if str(snapshot.get("file_metadata_fingerprint") or "") != expected_metadata:
        raise ValueError("review worktree file metadata does not match immutable execution evidence")


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
            "file_metadata_fingerprint": snapshot.get("file_metadata_fingerprint"),
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
    review_material = (
        packet.get("review_material")
        if isinstance(packet.get("review_material"), dict)
        else {}
    )
    evidence_manifest = (
        evidence.get("changed_file_manifest")
        if isinstance(evidence.get("changed_file_manifest"), dict)
        else {}
    )
    expected_review_material = {
        "manifest_fingerprint": evidence.get("manifest_fingerprint"),
        "patch_fingerprint": evidence.get("patch_fingerprint"),
        "file_metadata_fingerprint": _file_metadata_fingerprint(
            list(evidence_manifest.get("files") or [])
        ),
        "head_commit": evidence.get("final_head_sha") or evidence.get("post_run_head_sha"),
        "file_count": evidence.get("file_count"),
        "files_changed": list(evidence.get("files_changed") or []),
    }
    for field, expected in expected_review_material.items():
        if review_material.get(field) != expected:
            problems.append(f"review packet review_material {field} mismatch")
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
    post_snapshot: dict[str, Any] | None,
    status: str,
    claim_id: str,
    error: str = "",
    report_fingerprint: str | None = None,
    post_snapshot_proven: bool = False,
    failure_code: str | None = None,
    retry_safe: bool = False,
    process_started: bool = False,
) -> dict[str, Any]:
    process = process_result or {}
    execution_id = f"rx-{uuid.uuid4().hex[:18]}"
    health = health or {}
    auth = health.get("auth") if isinstance(health.get("auth"), dict) else {}
    unchanged = bool(post_snapshot_proven) and _snapshots_equal(pre_snapshot, post_snapshot)
    material = {
        "execution_id": execution_id,
        "claim_id": claim_id,
        "cycle_id": packet.get("cycle_id"),
        "assignment_id": assignment.get("assignment_id"),
        "packet_id": packet.get("packet_id"),
        "packet_fingerprint": packet.get("packet_fingerprint"),
        "provider_id": assignment.get("provider_id"),
        "reviewer_id": assignment.get("reviewer_id"),
        "command_contract_id": (command or {}).get("contract_id"),
        "read_only": bool((command or {}).get("read_only")),
        "provider_live_ready": bool(health.get("live_ready")),
        "auth_probe_verified": bool(auth.get("probe_verified")),
        "provider_version": health.get("version"),
        "provider_executable": Path(str(health.get("executable") or "")).name,
        "status": status,
        "failure_code": failure_code,
        "retry_safe": bool(retry_safe),
        "process_started": bool(process_started),
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
        "pre_snapshot": _snapshot_identity(pre_snapshot),
        "post_snapshot": _snapshot_identity(post_snapshot),
        "post_snapshot_proven": bool(post_snapshot_proven),
        "worktree_unchanged": unchanged,
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
        "claim_id",
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
        "claim_id": assignment.get("execution_claim_id"),
    }
    for field, expected in bindings.items():
        if str(record.get(field) or "") != str(expected or ""):
            problems.append(f"review execution {field} mismatch")

    status = str(record.get("status") or "")
    process = record.get("process") if isinstance(record.get("process"), dict) else {}
    contract = REVIEW_COMMAND_CONTRACTS.get(str(record.get("provider_id") or ""))
    if status == "succeeded":
        if not contract:
            problems.append("successful review execution provider has no approved command contract")
        else:
            if record.get("command_contract_id") != contract.get("contract_id"):
                problems.append("successful review execution command contract mismatch")
            argv = list(process.get("argv") or [])
            if len(argv) < 2 or argv[1:] != list(contract.get("argv_tail") or []):
                problems.append("successful review execution argv does not match approved contract")
        if process.get("exit_code") != 0:
            problems.append("successful review execution requires exit code zero")
        if process.get("cleanup_ok") is not True:
            problems.append("successful review execution requires confirmed process cleanup")
        if record.get("read_only") is not True:
            problems.append("successful review execution requires read-only command contract")
        if record.get("provider_live_ready") is not True:
            problems.append("successful review execution requires provider live readiness")
        if record.get("auth_probe_verified") is not True:
            problems.append("successful review execution requires verified authentication probe")
        if record.get("post_snapshot_proven") is not True:
            problems.append("successful review execution requires proven post-review snapshot")
        if record.get("worktree_unchanged") is not True:
            problems.append("successful review execution requires unchanged worktree")
        if record.get("failure_code") not in (None, ""):
            problems.append("successful review execution cannot carry a failure code")
        if record.get("retry_safe") is True:
            problems.append("successful review execution cannot be marked retry_safe")
        if not report or str(record.get("report_fingerprint") or "") != str(
            report.get("report_fingerprint") or ""
        ):
            problems.append("successful review execution report fingerprint mismatch")
    elif status == "failed":
        failure_code = str(record.get("failure_code") or "")
        if failure_code not in REVIEW_FAILURE_CODES:
            problems.append("failed review execution requires approved failure_code")
        if record.get("report_fingerprint") not in (None, ""):
            problems.append("failed review execution cannot bind a report")
        if record.get("retry_safe") is True and record.get("process_started") is True:
            if process.get("cleanup_ok") is not True:
                problems.append("retry-safe process failure requires confirmed cleanup")
            if record.get("post_snapshot_proven") is not True:
                problems.append("retry-safe process failure requires proven post-review snapshot")
            if record.get("worktree_unchanged") is not True:
                problems.append("retry-safe process failure requires unchanged worktree")
    else:
        problems.append("review execution status must be succeeded or failed")

    material = {
        key: record.get(key)
        for key in (
            "execution_id",
            "claim_id",
            "cycle_id",
            "assignment_id",
            "packet_id",
            "packet_fingerprint",
            "provider_id",
            "reviewer_id",
            "command_contract_id",
            "read_only",
            "provider_live_ready",
            "auth_probe_verified",
            "provider_version",
            "provider_executable",
            "status",
            "failure_code",
            "retry_safe",
            "process_started",
            "error",
            "process",
            "pre_snapshot",
            "post_snapshot",
            "post_snapshot_proven",
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
    claim_id = f"rclaim-{uuid.uuid4().hex[:18]}"
    claimed = store.claim_review_assignment_execution_atomic(
        cycle_id=cycle_id,
        assignment_id=assignment_id,
        packet_id=str(packet.get("packet_id") or ""),
        claim_id=claim_id,
        actor=actor,
    )
    cycle = claimed["cycle"]
    assignment = claimed["assignment"]
    provider_id = str(assignment.get("provider_id") or "")
    command: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    process_result: dict[str, Any] = {}
    process_started = False

    def fail(
        error: str,
        *,
        failure_code: str,
        post_snapshot: dict[str, Any] | None = None,
        post_snapshot_proven: bool = False,
        retry_safe: bool = False,
    ) -> None:
        execution = build_review_execution_record(
            packet=packet,
            assignment=assignment,
            command=command,
            health=health,
            process_result=process_result,
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
            status="failed",
            claim_id=claim_id,
            error=error,
            post_snapshot_proven=post_snapshot_proven,
            failure_code=failure_code,
            retry_safe=retry_safe,
            process_started=process_started,
        )
        store.record_review_execution_atomic(execution=execution, actor=actor)
        raise ValueError(error)

    if store.get_execution_control().get("kill_switch_active"):
        fail(
            "kill switch active; reviewer process not started",
            failure_code="kill_switch_active",
            retry_safe=True,
        )
    try:
        provider = store.get_provider_record(provider_id)
    except Exception as exc:
        fail(
            f"reviewer provider unavailable: {exc}",
            failure_code="provider_missing",
            retry_safe=True,
        )
    if not provider.get("enabled", True):
        fail("reviewer provider disabled", failure_code="provider_disabled", retry_safe=True)
    if not provider.get("constitution_acknowledged"):
        fail(
            "reviewer provider has not acknowledged the Constitution",
            failure_code="constitution_ack_missing",
            retry_safe=True,
        )
    if str(provider.get("constitution_hash") or "") != str(cycle.get("constitution_hash") or ""):
        fail(
            "reviewer provider Constitution hash does not match review cycle",
            failure_code="constitution_ack_mismatch",
            retry_safe=True,
        )

    try:
        health = health_check_provider(provider_id, provider, force_compat=True)
    except Exception as exc:
        fail(
            f"reviewer health probe failed: {exc}",
            failure_code="health_probe_failed",
            retry_safe=True,
        )
    if not health.get("live_ready"):
        fail(
            "reviewer provider not live-ready: "
            + "; ".join(health.get("unsupported_reasons") or ["unavailable"]),
            failure_code="provider_unavailable",
            retry_safe=True,
        )
    executable = str(health.get("executable") or "")
    try:
        command = build_review_command(provider_id, executable)
    except ValueError as exc:
        fail(
            str(exc),
            failure_code="command_contract_unavailable",
            retry_safe=True,
        )

    supervisor = get_process_supervisor()
    process_key = f"review-{assignment_id}"

    def on_event(_event: dict[str, Any]) -> None:
        if store.get_execution_control().get("kill_switch_active"):
            try:
                supervisor.cancel(process_key)
            except Exception:
                pass

    process_started = True
    try:
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
    except Exception as exc:
        fail(
            f"reviewer supervisor failed: {exc}",
            failure_code="supervisor_exception",
            retry_safe=False,
        )

    try:
        post_snapshot = _collect_snapshot(
            root, store.get_evidence_by_id(str(packet["evidence_id"]))
        )
    except Exception as exc:
        fail(
            f"post-review worktree proof failed: {exc}",
            failure_code="post_snapshot_unproven",
            retry_safe=False,
        )
    unchanged = _snapshots_equal(pre_snapshot, post_snapshot)
    if not unchanged:
        fail(
            "reviewer process mutated the governed worktree",
            failure_code="worktree_mutated",
            post_snapshot=post_snapshot,
            post_snapshot_proven=True,
            retry_safe=False,
        )
    if process_result.get("cleanup_ok") is not True:
        fail(
            "reviewer process-tree cleanup was not confirmed",
            failure_code="process_cleanup_unconfirmed",
            post_snapshot=post_snapshot,
            post_snapshot_proven=True,
            retry_safe=False,
        )
    if not process_result.get("ok") or process_result.get("exit_code") != 0:
        fail(
            "reviewer process did not exit successfully",
            failure_code="process_failed",
            post_snapshot=post_snapshot,
            post_snapshot_proven=True,
            retry_safe=True,
        )

    try:
        payload = parse_strict_review_output(provider_id, str(process_result.get("stdout") or ""))
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload=payload,
        )
    except Exception as exc:
        fail(
            f"reviewer output rejected: {exc}",
            failure_code="output_rejected",
            post_snapshot=post_snapshot,
            post_snapshot_proven=True,
            retry_safe=True,
        )

    execution = build_review_execution_record(
        packet=packet,
        assignment=assignment,
        command=command,
        health=health,
        process_result=process_result,
        pre_snapshot=pre_snapshot,
        post_snapshot=post_snapshot,
        status="succeeded",
        claim_id=claim_id,
        report_fingerprint=report.get("report_fingerprint"),
        post_snapshot_proven=True,
        process_started=True,
    )
    return store.submit_review_report_atomic(
        cycle_id=cycle_id,
        assignment_id=assignment_id,
        report=report,
        findings=findings,
        actor=str(assignment.get("reviewer_id") or actor),
        execution=execution,
    )
