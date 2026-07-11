from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Harden review execution trust boundaries.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_execution.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''import json
import uuid
from pathlib import Path
''',
    '''import json
import subprocess
import uuid
from pathlib import Path
''',
    label="review execution imports",
)
text = replace_once(
    text,
    '''from buildforme.governance import compute_run_scope_fingerprint, validate_safe_id
''',
    '''from buildforme.governance import (
    canonicalize_repository,
    compute_run_scope_fingerprint,
    normalize_repo_for_compare,
    validate_safe_id,
)
''',
    label="governance imports",
)
text = replace_once(
    text,
    '''from buildforme.provider_discovery import health_check_provider
''',
    '''from buildforme.provider_discovery import health_check_provider
from buildforme.repository_binding import (
    normalize_remote_to_owner_name,
    resolve_registered_repository,
)
''',
    label="repository imports",
)
text = replace_once(
    text,
    '''from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
''',
    '''from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from governance.constitution_lease import validate_run_lease_against_store
''',
    label="lease import",
)
text = replace_once(
    text,
    '''REVIEW_TIMEOUT_MAX_SECONDS = 1_800
''',
    '''REVIEW_TIMEOUT_MAX_SECONDS = 1_800
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
''',
    label="failure constants",
)

helper_anchor = '''def _require_bound_review_material(
'''
helpers = '''def _stable_file_metadata(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


'''
if text.count(helper_anchor) != 1:
    raise RuntimeError("review material helper anchor missing")
text = text.replace(helper_anchor, helpers + helper_anchor, 1)

text = replace_once(
    text,
    '''    worktree_raw = evidence.get("worktree_path") or run.get("worktree_path")
    if not worktree_raw:
        raise ValueError("review execution worktree missing")
    root = Path(str(worktree_raw)).resolve()
    if not root.is_dir():
        raise ValueError("review execution worktree does not exist")
    if root.is_symlink():
        raise ValueError("review execution worktree cannot be a symlink")
    return cycle, assignment, run, evidence, root
''',
    '''    worktree_raw = evidence.get("worktree_path") or run.get("worktree_path")
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
''',
    label="canonical worktree checks",
)
text = replace_once(
    text,
    '''        "files": list(manifest.get("files") or []),
        "diff_stat": manifest.get("diff_stat") or "",
''',
    '''        "files": list(manifest.get("files") or []),
        "file_metadata_fingerprint": _file_metadata_fingerprint(
            list(manifest.get("files") or [])
        ),
        "diff_stat": manifest.get("diff_stat") or "",
''',
    label="snapshot metadata fingerprint",
)
text = replace_once(
    text,
    '''    if list(snapshot.get("files_changed") or []) != list(evidence.get("files_changed") or []):
        raise ValueError("review worktree changed-file list does not match execution evidence")
''',
    '''    if list(snapshot.get("files_changed") or []) != list(evidence.get("files_changed") or []):
        raise ValueError("review worktree changed-file list does not match execution evidence")
    expected_metadata = _file_metadata_fingerprint(
        list((evidence.get("changed_file_manifest") or {}).get("files") or [])
    )
    if str(snapshot.get("file_metadata_fingerprint") or "") != expected_metadata:
        raise ValueError("review worktree file metadata does not match immutable execution evidence")
''',
    label="snapshot metadata comparison",
)
text = replace_once(
    text,
    '''            "files": list(snapshot.get("files") or []),
            "diff_stat": snapshot.get("diff_stat") or "",
''',
    '''            "files": list(snapshot.get("files") or []),
            "file_metadata_fingerprint": snapshot.get("file_metadata_fingerprint"),
            "diff_stat": snapshot.get("diff_stat") or "",
''',
    label="packet metadata fingerprint",
)
text = replace_once(
    text,
    '''    blind = packet.get("blind_context") if isinstance(packet.get("blind_context"), dict) else {}
    if any(bool(blind.get(key)) for key in blind):
        problems.append("review packet contains non-blind context")
    material = {
''',
    '''    blind = packet.get("blind_context") if isinstance(packet.get("blind_context"), dict) else {}
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
''',
    label="storage packet evidence binding",
)

# Replace execution record builder in one bounded block.
start = text.index("def build_review_execution_record(")
end = text.index("\n\ndef validate_review_execution_record(", start)
new_builder = '''def build_review_execution_record(
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
'''
text = text[:start] + new_builder + text[end:]

# Replace validation function.
start = text.index("def validate_review_execution_record(")
end = text.index("\n\ndef _prompt_bytes(", start)
new_validator = '''def validate_review_execution_record(
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
'''
text = text[:start] + new_validator + text[end:]

# Replace execute function fully.
start = text.index("def execute_independent_review_assignment(")
new_execute = '''def execute_independent_review_assignment(
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
'''
text = text[:start] + new_execute + "\n"
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Assignment execution claims and failure-state authority.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
anchor = "    # —— Stage 7 Packet 7B review packets/executions ——\n"
claim_method = '''    def claim_review_assignment_execution_atomic(
        self,
        *,
        cycle_id: str,
        assignment_id: str,
        packet_id: str,
        claim_id: str,
        actor: str,
    ) -> dict[str, Any]:
        """Atomically reserve one pending assignment for exactly one reviewer process."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT payload_json, status FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
            assignment_row = conn.execute(
                "SELECT payload_json, status, cycle_id FROM review_assignments WHERE id=?",
                (str(assignment_id),),
            ).fetchone()
            packet_row = conn.execute(
                "SELECT packet_id FROM review_packets WHERE packet_id=? AND assignment_id=?",
                (str(packet_id), str(assignment_id)),
            ).fetchone()
            if not cycle_row or not assignment_row or not packet_row:
                raise ValueError("review execution claim cycle, assignment, or packet not found")
            if str(cycle_row[1]) not in {"open", "collecting"}:
                raise ValueError("review cycle is not accepting reviewer execution")
            if str(assignment_row[2]) != str(cycle_id):
                raise ValueError("review execution claim assignment cycle mismatch")
            if str(assignment_row[1]) != "pending":
                raise ValueError("review assignment already claimed or unavailable")
            assignment = loads(assignment_row[0], {})
            assignment["status"] = "executing"
            assignment["execution_claim_id"] = str(claim_id)
            assignment["execution_packet_id"] = str(packet_id)
            assignment["execution_started_at"] = now
            assignment["execution_claim_actor"] = str(actor)
            cur = conn.execute(
                "UPDATE review_assignments SET status='executing', payload_json=? WHERE id=? AND status='pending'",
                (dumps(assignment), str(assignment_id)),
            )
            if cur.rowcount != 1:
                raise ValueError("review assignment claim race rejected")
            cycle = loads(cycle_row[0], {})
            cycle["status"] = cycle_row[1]
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("rve"),
                    str(cycle_id),
                    "review_execution_claimed",
                    "Reviewer assignment claimed for one process",
                    str(actor),
                    dumps(
                        {
                            "assignment_id": assignment_id,
                            "packet_id": packet_id,
                            "claim_id": claim_id,
                        }
                    ),
                    now,
                ),
            )
        return {"cycle": cycle, "assignment": assignment}

'''
if text.count(anchor) != 1:
    raise RuntimeError("Packet 7B storage anchor missing")
text = text.replace(anchor, anchor + claim_method, 1)

# Strengthen packet storage with current authority checks.
text = replace_once(
    text,
    '''            run = loads(run_row[0], {})
            evidence = loads(evidence_row[0], {})
            problems = validate_review_packet_for_storage(
''',
    '''            run = loads(run_row[0], {})
            evidence = loads(evidence_row[0], {})
            from buildforme.evidence import EVIDENCE_KIND_EXECUTION
            from buildforme.governance import compute_run_scope_fingerprint

            if str(cycle.get("status") or "") not in {"open", "collecting"}:
                raise ValueError("review packet requires active cycle")
            live_scope = compute_run_scope_fingerprint(
                run, run.get("packet") if isinstance(run.get("packet"), dict) else None
            )
            if live_scope != str(run.get("scope_fingerprint") or "") or live_scope != str(
                cycle.get("scope_fingerprint") or ""
            ):
                raise ValueError("review packet run/cycle scope is stale")
            latest_execution = None
            for row in conn.execute(
                "SELECT payload_json FROM evidence WHERE run_id=? ORDER BY sequence DESC",
                (str(run.get("id") or ""),),
            ).fetchall():
                candidate = loads(row[0], {})
                kind = str(candidate.get("evidence_kind") or "")
                if kind == EVIDENCE_KIND_EXECUTION or (
                    not kind and isinstance(candidate.get("process"), dict)
                ):
                    latest_execution = candidate
                    break
            if not latest_execution or str(latest_execution.get("evidence_id") or "") != str(
                evidence.get("evidence_id") or ""
            ):
                raise ValueError("review packet is not bound to latest execution evidence")
            problems = validate_review_packet_for_storage(
''',
    label="packet current authority validation",
)

# Replace failure execution recorder fully.
start = text.index("    def record_review_execution_atomic(")
end = text.index("\n    # —— Stage 7 independent reviews ——", start)
new_failure_method = '''    def record_review_execution_atomic(self, *, execution: dict[str, Any], actor: str) -> dict[str, Any]:
        from buildforme.review_execution import validate_review_execution_record

        record = dict(execution)
        if str(record.get("status") or "") != "failed":
            raise ValueError("only failed reviewer execution may commit without a report")
        assignment_id = str(record.get("assignment_id") or "")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            assignment_row = conn.execute(
                "SELECT payload_json, status, cycle_id FROM review_assignments WHERE id=?",
                (assignment_id,),
            ).fetchone()
            packet_row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE packet_id=? AND assignment_id=?",
                (str(record.get("packet_id") or ""), assignment_id),
            ).fetchone()
            if not assignment_row or not packet_row:
                raise ValueError("review execution assignment or packet not found")
            if str(assignment_row[1]) != "executing":
                raise ValueError("failed review execution requires executing assignment")
            assignment = loads(assignment_row[0], {})
            assignment["status"] = assignment_row[1]
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
                    record.get("created_at") or now,
                ),
            )

            retry_safe = bool(record.get("retry_safe"))
            cycle_id = str(assignment_row[2])
            if retry_safe:
                assignment["status"] = "pending"
                assignment["last_execution_failure_id"] = record["execution_id"]
                assignment["last_execution_failure_code"] = record.get("failure_code")
                for key in (
                    "execution_claim_id",
                    "execution_packet_id",
                    "execution_started_at",
                    "execution_claim_actor",
                ):
                    assignment.pop(key, None)
                conn.execute(
                    "UPDATE review_assignments SET status='pending', payload_json=? WHERE id=? AND status='executing'",
                    (dumps(assignment), assignment_id),
                )
                event_type = "review_execution_retry_safe_failure"
                summary = "Reviewer execution failed with proven retry-safe state"
            else:
                assignment["status"] = "blocked"
                assignment["blocked_execution_id"] = record["execution_id"]
                assignment["blocked_failure_code"] = record.get("failure_code")
                conn.execute(
                    "UPDATE review_assignments SET status='blocked', payload_json=? WHERE id=? AND status='executing'",
                    (dumps(assignment), assignment_id),
                )
                cycle_row = conn.execute(
                    "SELECT run_id, payload_json, row_version FROM review_cycles WHERE id=?",
                    (cycle_id,),
                ).fetchone()
                if not cycle_row:
                    raise ValueError("review cycle missing while recording integrity failure")
                cycle = loads(cycle_row[1], {})
                cycle["status"] = "blocked"
                cycle["integrity_failure_execution_id"] = record["execution_id"]
                cycle["integrity_failure_code"] = record.get("failure_code")
                cycle["updated_at"] = now
                cycle_version = int(cycle_row[2] or 1) + 1
                cycle["row_version"] = cycle_version
                conn.execute(
                    "UPDATE review_cycles SET status='blocked', payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                    (dumps(cycle), now, cycle_version, cycle_id, int(cycle_row[2] or 1)),
                )
                run_row = conn.execute(
                    "SELECT payload_json, row_version FROM runs WHERE id=?",
                    (str(cycle_row[0]),),
                ).fetchone()
                if not run_row:
                    raise ValueError("review run missing while recording integrity failure")
                run = loads(run_row[0], {})
                run["independent_review"] = {
                    **(run.get("independent_review") or {}),
                    "cycle_id": cycle_id,
                    "status": "blocked",
                    "integrity_failure_execution_id": record["execution_id"],
                    "integrity_failure_code": record.get("failure_code"),
                }
                run["updated_at"] = now
                run_version = int(run_row[1] or 1) + 1
                run["row_version"] = run_version
                conn.execute(
                    "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                    (dumps(run), now, run_version, str(cycle_row[0]), int(run_row[1] or 1)),
                )
                event_type = "review_execution_integrity_blocked"
                summary = "Reviewer execution integrity failure blocked the cycle"

            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("rve"),
                    cycle_id,
                    event_type,
                    summary,
                    actor,
                    dumps(
                        {
                            "assignment_id": assignment_id,
                            "execution_id": record["execution_id"],
                            "failure_code": record.get("failure_code"),
                            "retry_safe": retry_safe,
                        }
                    ),
                    now,
                ),
            )
        return record
'''
text = text[:start] + new_failure_method + text[end:]

# Production report path must require an active claim.
text = replace_once(
    text,
    '''            if str(assignment_row[1]) != "pending":
                raise ValueError("review assignment already submitted or unavailable")
            assignment = loads(assignment_row[2], {})
            assignment["status"] = assignment_row[1]
''',
    '''            if str(assignment_row[1]) != "executing":
                raise ValueError("review report requires an executing assignment claim")
            assignment = loads(assignment_row[2], {})
            assignment["status"] = assignment_row[1]
''',
    label="report claim requirement",
)
text = replace_once(
    text,
    '''            assignment["status"] = "submitted"
            assignment["submitted_at"] = now
            assignment["report_id"] = report_id
            assignment["report_fingerprint"] = report["report_fingerprint"]
''',
    '''            assignment["status"] = "submitted"
            assignment["submitted_at"] = now
            assignment["report_id"] = report_id
            assignment["report_fingerprint"] = report["report_fingerprint"]
            for key in (
                "execution_claim_id",
                "execution_packet_id",
                "execution_started_at",
                "execution_claim_actor",
            ):
                assignment.pop(key, None)
''',
    label="clear successful claim",
)
path.write_text(text, encoding="utf-8")


# LocalStore claim wrapper.
path = ROOT / "buildforme" / "storage.py"
text = path.read_text(encoding="utf-8")
anchor = '''    def save_review_packet_atomic(self, **kwargs: Any) -> dict[str, Any]:
'''
wrapper = '''    def claim_review_assignment_execution_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.claim_review_assignment_execution_atomic(**kwargs)

'''
if text.count(anchor) != 1:
    raise RuntimeError("review packet wrapper anchor missing")
text = text.replace(anchor, wrapper + anchor, 1)
path.write_text(text, encoding="utf-8")


# Automated report construction permits the atomically claimed assignment.
path = ROOT / "buildforme" / "review_contracts.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    if str(assignment.get("status") or "") != "pending":
        raise ValueError("review assignment is not pending")
''',
    '''    if str(assignment.get("status") or "") not in {"pending", "executing"}:
        raise ValueError("review assignment is not pending or executing")
''',
    label="review report claimed assignment",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Packet 7A tests use direct fixture persistence, never production execution claims.
# ---------------------------------------------------------------------------
path = ROOT / "tests" / "test_stage7_review_authority.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.db import SCHEMA_VERSION
''',
    '''from buildforme.db import SCHEMA_VERSION, dumps
''',
    label="Packet7A db fixture imports",
)
text = replace_once(
    text,
    '''from buildforme.review_execution import (
    build_review_execution_record,
    build_review_packet_record,
)
from buildforme.storage import LocalStore
''',
    '''from buildforme.storage import LocalStore, utc_now_iso
''',
    label="remove Packet7B synthetic execution imports",
)
start = text.index("def submit_independent_review_report(")
end = text.index("\n\nclass Stage7ReviewAuthorityTests", start)
fixture_helper = '''def submit_independent_review_report(
    store,
    cycle_id,
    assignment_id,
    *,
    payload,
    actor="reviewer",
):
    """Test-fixture persistence below Packet 7B process authority."""
    cycle = store.get_review_cycle(cycle_id)
    assignment = store.get_review_assignment(assignment_id)
    report, findings = build_review_report_record(
        cycle=cycle,
        assignment=assignment,
        payload=payload,
    )
    now = utc_now_iso()
    with store.s6.db.transaction() as conn:
        row = conn.execute(
            "SELECT status, payload_json FROM review_assignments WHERE id=?",
            (assignment_id,),
        ).fetchone()
        if not row or str(row[0]) != "pending":
            raise ValueError("fixture review assignment is not pending")
        conn.execute(
            "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
            (
                report["report_id"],
                cycle_id,
                assignment_id,
                report["verdict"],
                report["report_fingerprint"],
                dumps(report),
                now,
            ),
        )
        for finding in findings:
            conn.execute(
                "INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (
                    finding["finding_id"],
                    report["report_id"],
                    cycle_id,
                    assignment_id,
                    finding["severity"],
                    finding["category"],
                    1 if finding.get("blocking") else 0,
                    finding["finding_fingerprint"],
                    dumps(finding),
                    now,
                ),
            )
        assignment_record = loads = __import__("buildforme.db", fromlist=["loads"]).loads
        saved_assignment = loads(row[1], {})
        saved_assignment["status"] = "submitted"
        saved_assignment["submitted_at"] = now
        saved_assignment["report_id"] = report["report_id"]
        saved_assignment["report_fingerprint"] = report["report_fingerprint"]
        conn.execute(
            "UPDATE review_assignments SET status='submitted', payload_json=?, submitted_at=? WHERE id=?",
            (dumps(saved_assignment), now, assignment_id),
        )
        submitted = int(
            conn.execute(
                "SELECT COUNT(*) FROM review_assignments WHERE cycle_id=? AND status='submitted'",
                (cycle_id,),
            ).fetchone()[0]
        )
        cycle_row = conn.execute(
            "SELECT payload_json, required_reviewer_count, row_version FROM review_cycles WHERE id=?",
            (cycle_id,),
        ).fetchone()
        saved_cycle = loads(cycle_row[0], {})
        status = "ready_to_aggregate" if submitted >= int(cycle_row[1]) else "collecting"
        saved_cycle["status"] = status
        saved_cycle["submitted_reviewer_count"] = submitted
        saved_cycle["updated_at"] = now
        saved_cycle["row_version"] = int(cycle_row[2]) + 1
        conn.execute(
            "UPDATE review_cycles SET status=?, payload_json=?, updated_at=?, row_version=? WHERE id=?",
            (status, dumps(saved_cycle), now, saved_cycle["row_version"], cycle_id),
        )
    return {
        "cycle": saved_cycle,
        "assignment": saved_assignment,
        "report": report,
        "findings": findings,
    }
'''
text = text[:start] + fixture_helper + text[end:]
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Upgrade Packet 7B fixtures and add red-team tests.
# ---------------------------------------------------------------------------
path = ROOT / "tests" / "test_stage7_review_execution.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.governance import compute_run_scope_fingerprint
''',
    '''from buildforme.governance import compute_run_scope_fingerprint
from governance.constitution_engine import get_engine
''',
    label="test Constitution import",
)
text = replace_once(
    text,
    '''        self._git("config", "user.name", "review-test")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
''',
    '''        self._git("config", "user.name", "review-test")
        self._git("remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
''',
    label="test remote identity",
)
text = replace_once(
    text,
    '''        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        run = {
''',
    '''        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        self._git("checkout", "-b", "feature/stage7b-run")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.upsert_project(
            {
                "id": "buildforme",
                "name": "Buildforme",
                "repository": "shanchaudary/Buildforme",
                "status": "active",
                "local_repository_root": str(self.root),
            }
        )
        self.store.register_repository_binding(
            {
                "repository": "shanchaudary/Buildforme",
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet(
            {
                "id": "pkt-stage7b",
                "objective": "Add subtraction function",
                "acceptance_criteria": ["sub returns a-b"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7b",
                "allowed_files": ["app.py"],
                "forbidden_files": [".env"],
            }
        )
        lease = engine.issue_run_lease(
            run_id="run-stage7b",
            provider_id="claude",
            packet_id=packet["id"],
            actor="test",
        )
        self.store.save_constitution_lease(lease)
        run = {
''',
    label="test governed setup",
)
# Replace fake constitution + packet block and attach canonical run.
text = replace_once(
    text,
    '''            "constitution_version": "1.0.0",
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
''',
    '''            "packet_id": packet["id"],
            "packet": packet,
''',
    label="test canonical Constitution fields",
)
text = replace_once(
    text,
    '''        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, run["packet"])
        self.run = self.store.save_run_for_setup(run)
''',
    '''        run = engine.attach_to_run(run, lease=lease, actor="test")
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, run["packet"])
        self.run = self.store.save_run_for_setup(run)
''',
    label="test canonical run lease",
)
text = replace_once(
    text,
    '''                "constitution_version": "1.0.0",
                "constitution_hash": "c" * 64,
''',
    '''                "constitution_version": engine.version(),
                "constitution_hash": engine.content_hash(),
''',
    label="reviewer actual Constitution ack",
)
# Current success assertions include new proof.
text = replace_once(
    text,
    '''        self.assertTrue(attempts[0]["worktree_unchanged"])
''',
    '''        self.assertTrue(attempts[0]["worktree_unchanged"])
        self.assertTrue(attempts[0]["post_snapshot_proven"])
        self.assertTrue(attempts[0]["auth_probe_verified"])
''',
    label="success proof assertions",
)
# Mutation now blocks cycle.
text = replace_once(
    text,
    '''        self.assertFalse(attempts[-1]["worktree_unchanged"])
''',
    '''        self.assertFalse(attempts[-1]["worktree_unchanged"])
        self.assertFalse(attempts[-1]["retry_safe"])
        self.assertEqual(self.store.get_review_cycle(self.cycle["cycle_id"])["status"], "blocked")
''',
    label="mutation block assertions",
)
# Malformed and unavailable are retry-safe pending.
text = replace_once(
    text,
    '''        self.assertEqual(self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]["status"], "failed")

    def test_unavailable_provider_records_failure_and_no_report(self):
''',
    '''        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertEqual(attempt["status"], "failed")
        self.assertTrue(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_assignment(self.assignment["assignment_id"])["status"], "pending")

    def test_unavailable_provider_records_failure_and_no_report(self):
''',
    label="malformed retry assertions",
)
text = replace_once(
    text,
    '''        self.assertEqual(self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]["status"], "failed")

    def test_authenticated_storage_rejects_divergent_findings(self):
''',
    '''        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertEqual(attempt["status"], "failed")
        self.assertTrue(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_assignment(self.assignment["assignment_id"])["status"], "pending")

    def test_authenticated_storage_rejects_divergent_findings(self):
''',
    label="unavailable retry assertions",
)
# Authenticated storage divergence uses a real claim and exact command contract.
text = replace_once(
    text,
    '''        cycle = self.store.get_review_cycle(self.cycle["cycle_id"])
        assignment = self.store.get_review_assignment(self.assignment["assignment_id"])
        report, findings = build_review_report_record(
''',
    '''        claim_id = "claim-divergence"
        claimed = self.store.claim_review_assignment_execution_atomic(
            cycle_id=self.cycle["cycle_id"],
            assignment_id=self.assignment["assignment_id"],
            packet_id=packet["packet_id"],
            claim_id=claim_id,
            actor="test",
        )
        cycle = claimed["cycle"]
        assignment = claimed["assignment"]
        report, findings = build_review_report_record(
''',
    label="divergence execution claim",
)
text = replace_once(
    text,
    '''            "argv": ["test-reviewer", "--read-only"],
        }
        execution = build_review_execution_record(
''',
    '''            "argv": build_review_command("codex", "test-reviewer")["argv"],
        }
        command = build_review_command("codex", "test-reviewer")
        execution = build_review_execution_record(
''',
    label="divergence exact argv",
)
text = replace_once(
    text,
    '''            command={
                "contract_id": "test.read-only.v1",
                "read_only": True,
                "argv": process["argv"],
            },
            health={"version": "test", "executable": "test-reviewer"},
''',
    '''            command=command,
            health={
                "version": "test",
                "executable": "test-reviewer",
                "live_ready": True,
                "auth": {"probe_verified": True},
            },
''',
    label="divergence command proof",
)
text = replace_once(
    text,
    '''            status="succeeded",
            report_fingerprint=report["report_fingerprint"],
        )
''',
    '''            status="succeeded",
            claim_id=claim_id,
            report_fingerprint=report["report_fingerprint"],
            post_snapshot_proven=True,
            process_started=True,
        )
''',
    label="divergence claim proof",
)
# Add red-team tests before direct submission test.
anchor = '''    def test_direct_report_submission_is_disabled(self):
'''
new_tests = '''    def test_second_execution_claim_is_rejected_atomically(self):
        packet, _snapshot, _root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        packet = self.store.save_review_packet_atomic(packet=packet, actor="test")
        self.store.claim_review_assignment_execution_atomic(
            cycle_id=self.cycle["cycle_id"],
            assignment_id=self.assignment["assignment_id"],
            packet_id=packet["packet_id"],
            claim_id="claim-one",
            actor="test",
        )
        with self.assertRaisesRegex(ValueError, "already claimed|unavailable"):
            self.store.claim_review_assignment_execution_atomic(
                cycle_id=self.cycle["cycle_id"],
                assignment_id=self.assignment["assignment_id"],
                packet_id=packet["packet_id"],
                claim_id="claim-two",
                actor="test",
            )

    def test_repository_remote_mismatch_blocks_packet(self):
        self._git("remote", "set-url", "origin", "https://github.com/other/wrong.git")
        with self.assertRaisesRegex(ValueError, "repository mismatch|remote identity"):
            build_verified_blind_review_packet(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_post_snapshot_failure_records_unproven_integrity_block(self):
        executable = self._fake_codex()
        from buildforme import review_execution as module

        original = module._collect_snapshot
        calls = {"count": 0}

        def fail_second(root, evidence):
            calls["count"] += 1
            if calls["count"] == 1:
                return original(root, evidence)
            raise ValueError("post proof unavailable")

        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)), patch(
            "buildforme.review_execution._collect_snapshot", side_effect=fail_second
        ):
            with self.assertRaisesRegex(ValueError, "post-review worktree proof failed"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertFalse(attempt["post_snapshot_proven"])
        self.assertFalse(attempt["worktree_unchanged"])
        self.assertFalse(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_cycle(self.cycle["cycle_id"])["status"], "blocked")

    def test_health_probe_exception_records_retry_safe_failure(self):
        with patch(
            "buildforme.review_execution.health_check_provider",
            side_effect=RuntimeError("probe crashed"),
        ):
            with self.assertRaisesRegex(ValueError, "health probe failed"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertEqual(attempt["failure_code"], "health_probe_failed")
        self.assertTrue(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_assignment(self.assignment["assignment_id"])["status"], "pending")

    def test_storage_rejects_forged_success_command_contract(self):
        packet, snapshot, _root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        packet = self.store.save_review_packet_atomic(packet=packet, actor="test")
        claim_id = "claim-forged"
        claimed = self.store.claim_review_assignment_execution_atomic(
            cycle_id=self.cycle["cycle_id"],
            assignment_id=self.assignment["assignment_id"],
            packet_id=packet["packet_id"],
            claim_id=claim_id,
            actor="test",
        )
        assignment = claimed["assignment"]
        cycle = claimed["cycle"]
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={"verdict": "pass", "summary": "fake", "findings": []},
        )
        forged = build_review_execution_record(
            packet=packet,
            assignment=assignment,
            command={
                "contract_id": "forged.write.v1",
                "read_only": True,
                "argv": ["codex", "exec", "-s", "workspace-write"],
            },
            health={
                "version": "test",
                "executable": "codex",
                "live_ready": True,
                "auth": {"probe_verified": True},
            },
            process_result={
                "exit_code": 0,
                "cleanup_ok": True,
                "argv": ["codex", "exec", "-s", "workspace-write"],
            },
            pre_snapshot=snapshot,
            post_snapshot=snapshot,
            status="succeeded",
            claim_id=claim_id,
            report_fingerprint=report["report_fingerprint"],
            post_snapshot_proven=True,
            process_started=True,
        )
        with self.assertRaisesRegex(ValueError, "command contract|argv"):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=findings,
                actor="reviewer",
                execution=forged,
            )
        self.assertEqual(self.store.list_review_reports(cycle["cycle_id"]), [])

'''
if text.count(anchor) != 1:
    raise RuntimeError("Packet7B red-team test anchor missing")
text = text.replace(anchor, new_tests + anchor, 1)
path.write_text(text, encoding="utf-8")


# Permanent source contracts for red-team authority.
(ROOT / "tests" / "test_stage7_packet7b_contract.py").write_text(
    '''"""Permanent source contracts for accepted Packet 7B reviewer execution authority."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Stage7Packet7BContractTests(unittest.TestCase):
    def test_runtime_uses_atomic_claim_and_no_direct_report_authority(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        self.assertIn("claim_review_assignment_execution_atomic", source)
        self.assertIn("post_snapshot_proven", source)
        service = Path("buildforme/review_service.py").read_text(encoding="utf-8")
        self.assertIn("direct review report submission disabled", service)

    def test_success_validation_binds_code_owned_contract_and_auth(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        for phrase in (
            "successful review execution command contract mismatch",
            "argv does not match approved contract",
            "requires verified authentication probe",
            "requires proven post-review snapshot",
        ):
            self.assertIn(phrase, source)

    def test_no_runtime_setup_review_submission_api(self):
        forbidden = []
        for path in Path("buildforme").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "submit_review_report_for_setup":
                        forbidden.append((str(path), node.lineno))
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)

# Documentation and hygiene.
path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''

## Packet 7B red-team hardening

- Reviewer assignments are claimed atomically before process launch; concurrent launches
  for one assignment are rejected.
- The exact registered repository, remote identity, Git common directory, worktree branch,
  and canonical Constitution lease are revalidated before review.
- Snapshot equality includes full changed-file metadata, including symlink target/escape facts.
- Post-review proof failure never substitutes the pre-review snapshot or claims unchanged state.
- Retry-safe failures require confirmed cleanup and a proven unchanged post-review snapshot.
  Integrity failures block the assignment and cycle atomically.
- Successful process evidence must match the code-owned provider command contract, exact argv,
  live-ready health, and verified authentication probe.
- Provider lookup and health-probe exceptions produce immutable failure evidence after claim.
'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7B red-team hardening applied")
