from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


seed_module = r'''"""Deterministic local repair seeds for Stage 7 governed repair runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence

SEED_PROOF_SCHEMA = "buildforme.repair_seed.v1"


def _run_git(
    root: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 120.0,
) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
        timeout=timeout,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout or '')[:400]}")
    return (proc.stdout or "").strip()


def _safe_path(root: Path, value: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ".." in Path(text).parts:
        raise ValueError(f"unsafe repair seed path: {value!r}")
    resolved = (root / text).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"repair seed path escapes source worktree: {text}") from exc
    return text


def _proof_fingerprint(material: dict[str, Any]) -> str:
    raw = json.dumps(
        {"schema": SEED_PROOF_SCHEMA, "material": material},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _verify_seed_manifest(
    repository_root: Path,
    *,
    seed_commit: str,
    baseline_commit: str,
    expected_manifest_fingerprint: str,
    expected_files_changed: list[str],
) -> dict[str, Any]:
    workspace = Path(tempfile.mkdtemp(prefix="buildforme-repair-seed-"))
    worktree = workspace / "worktree"
    added = False
    try:
        _run_git(repository_root, ["worktree", "add", "--detach", str(worktree), seed_commit])
        added = True
        manifest = collect_changed_file_manifest(worktree, baseline_commit=baseline_commit)
        patch = collect_patch_evidence(worktree, baseline_commit=baseline_commit)
        if not manifest.get("complete") or not patch.get("complete"):
            raise ValueError("repair seed verification sources are incomplete")
        if str(manifest.get("manifest_fingerprint") or "") != str(expected_manifest_fingerprint or ""):
            raise ValueError("repair seed manifest fingerprint does not match reviewed execution evidence")
        if list(manifest.get("files_changed") or []) != list(expected_files_changed or []):
            raise ValueError("repair seed changed-file list does not match reviewed execution evidence")
        return {
            "manifest_fingerprint": manifest.get("manifest_fingerprint"),
            "files_changed": list(manifest.get("files_changed") or []),
            "seed_patch_fingerprint": patch.get("patch_fingerprint"),
            "seed_patch_size": patch.get("patch_size"),
        }
    finally:
        if added:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(repository_root),
                capture_output=True,
                text=True,
                check=False,
            )
        shutil.rmtree(workspace, ignore_errors=True)


def create_repair_seed(
    *,
    repair_packet: dict[str, Any],
    source_run: dict[str, Any],
    source_evidence: dict[str, Any],
) -> dict[str, Any]:
    repository_root = Path(str(source_run.get("repository_local_path") or "")).resolve()
    source_root = Path(
        str(source_evidence.get("worktree_path") or source_run.get("worktree_path") or "")
    ).resolve()
    if not repository_root.is_dir() or not source_root.is_dir():
        raise ValueError("repair source repository/worktree is unavailable")
    common_source = Path(_run_git(source_root, ["rev-parse", "--git-common-dir"]))
    if not common_source.is_absolute():
        common_source = (source_root / common_source).resolve()
    common_registered = Path(_run_git(repository_root, ["rev-parse", "--git-common-dir"]))
    if not common_registered.is_absolute():
        common_registered = (repository_root / common_registered).resolve()
    if common_source != common_registered:
        raise ValueError("repair source worktree does not belong to registered repository")

    baseline = str(repair_packet.get("approved_baseline_commit") or "")
    expected_manifest = str(repair_packet.get("source_manifest_fingerprint") or "")
    expected_patch = str(repair_packet.get("source_patch_fingerprint") or "")
    expected_paths = list(source_evidence.get("files_changed") or [])
    source_manifest = collect_changed_file_manifest(source_root, baseline_commit=baseline)
    source_patch = collect_patch_evidence(source_root, baseline_commit=baseline)
    if not source_manifest.get("complete") or not source_patch.get("complete"):
        raise ValueError("repair source worktree proof is incomplete")
    if str(source_manifest.get("manifest_fingerprint") or "") != expected_manifest:
        raise ValueError("repair source manifest no longer matches immutable review evidence")
    if str(source_patch.get("patch_fingerprint") or "") != expected_patch:
        raise ValueError("repair source patch no longer matches immutable review evidence")
    if list(source_manifest.get("files_changed") or []) != expected_paths:
        raise ValueError("repair source changed-file list no longer matches immutable evidence")

    packet_id = str(repair_packet.get("repair_packet_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", packet_id):
        raise ValueError("repair packet id is unsafe for seed ref")
    seed_ref = f"refs/buildforme/repair-seeds/{packet_id}"
    created_at = str(repair_packet.get("created_at") or "1970-01-01T00:00:00+00:00")
    with tempfile.TemporaryDirectory(prefix="buildforme-repair-index-") as temp_dir:
        index_path = Path(temp_dir) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index_path)
        env["GIT_WORK_TREE"] = str(source_root)
        env["GIT_AUTHOR_NAME"] = "Buildforme Repair Authority"
        env["GIT_AUTHOR_EMAIL"] = "repair@buildforme.local"
        env["GIT_COMMITTER_NAME"] = "Buildforme Repair Authority"
        env["GIT_COMMITTER_EMAIL"] = "repair@buildforme.local"
        env["GIT_AUTHOR_DATE"] = created_at
        env["GIT_COMMITTER_DATE"] = created_at
        _run_git(repository_root, ["read-tree", baseline], env=env)
        for raw_path in expected_paths:
            relative = _safe_path(source_root, str(raw_path))
            full = source_root / relative
            if full.exists() or full.is_symlink():
                _run_git(repository_root, ["add", "-f", "--", relative], env=env)
            else:
                _run_git(
                    repository_root,
                    ["rm", "--cached", "--ignore-unmatch", "--", relative],
                    env=env,
                )
        seed_tree = _run_git(repository_root, ["write-tree"], env=env)
        message = f"Buildforme governed repair seed {packet_id}\n"
        seed_commit = _run_git(
            repository_root,
            ["commit-tree", seed_tree, "-p", baseline],
            env=env,
            input_text=message,
        )

    existing = ""
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", seed_ref],
        cwd=str(repository_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        existing = (probe.stdout or "").strip()
        if existing != seed_commit:
            raise ValueError("repair seed ref already exists with different authority")
    else:
        _run_git(repository_root, ["update-ref", seed_ref, seed_commit, "0" * 40])

    try:
        verified = _verify_seed_manifest(
            repository_root,
            seed_commit=seed_commit,
            baseline_commit=baseline,
            expected_manifest_fingerprint=expected_manifest,
            expected_files_changed=expected_paths,
        )
        material = {
            "repair_packet_id": packet_id,
            "source_run_id": repair_packet.get("source_run_id"),
            "source_evidence_id": repair_packet.get("source_evidence_id"),
            "repository_local_path": str(repository_root),
            "source_worktree_path": str(source_root),
            "approved_baseline_commit": baseline,
            "source_manifest_fingerprint": expected_manifest,
            "source_patch_fingerprint": expected_patch,
            "files_changed": expected_paths,
            "seed_ref": seed_ref,
            "seed_commit": seed_commit,
            "seed_tree": seed_tree,
            "seed_manifest_fingerprint": verified["manifest_fingerprint"],
            "seed_patch_fingerprint": verified["seed_patch_fingerprint"],
            "seed_patch_size": verified["seed_patch_size"],
        }
        return {
            "schema": SEED_PROOF_SCHEMA,
            **material,
            "seed_fingerprint": _proof_fingerprint(material),
            "immutable": True,
        }
    except Exception:
        if not existing:
            subprocess.run(
                ["git", "update-ref", "-d", seed_ref, seed_commit],
                cwd=str(repository_root),
                capture_output=True,
                text=True,
                check=False,
            )
        raise


def validate_repair_seed_for_storage(
    proof: dict[str, Any],
    *,
    repair_packet: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    if not isinstance(proof, dict):
        return ["repair seed proof must be an object"]
    material = {
        key: proof.get(key)
        for key in (
            "repair_packet_id",
            "source_run_id",
            "source_evidence_id",
            "repository_local_path",
            "source_worktree_path",
            "approved_baseline_commit",
            "source_manifest_fingerprint",
            "source_patch_fingerprint",
            "files_changed",
            "seed_ref",
            "seed_commit",
            "seed_tree",
            "seed_manifest_fingerprint",
            "seed_patch_fingerprint",
            "seed_patch_size",
        )
    }
    if proof.get("seed_fingerprint") != _proof_fingerprint(material):
        problems.append("repair seed fingerprint mismatch")
    bindings = {
        "repair_packet_id": repair_packet.get("repair_packet_id"),
        "source_run_id": repair_packet.get("source_run_id"),
        "source_evidence_id": repair_packet.get("source_evidence_id"),
        "approved_baseline_commit": repair_packet.get("approved_baseline_commit"),
        "source_manifest_fingerprint": repair_packet.get("source_manifest_fingerprint"),
        "source_patch_fingerprint": repair_packet.get("source_patch_fingerprint"),
    }
    for field, expected in bindings.items():
        if str(proof.get(field) or "") != str(expected or ""):
            problems.append(f"repair seed {field} mismatch")
    repository_root = Path(str(proof.get("repository_local_path") or "")).resolve()
    if not repository_root.is_dir():
        problems.append("repair seed repository root missing")
        return problems
    try:
        ref_commit = _run_git(repository_root, ["rev-parse", "--verify", str(proof.get("seed_ref"))])
        if ref_commit != str(proof.get("seed_commit") or ""):
            problems.append("repair seed ref does not resolve to seed commit")
        object_type = _run_git(repository_root, ["cat-file", "-t", str(proof.get("seed_commit"))])
        if object_type != "commit":
            problems.append("repair seed object is not a commit")
        parent_line = _run_git(
            repository_root, ["rev-list", "--parents", "-n", "1", str(proof.get("seed_commit"))]
        ).split()
        if len(parent_line) != 2 or parent_line[1] != str(proof.get("approved_baseline_commit") or ""):
            problems.append("repair seed commit parent is not the approved baseline")
        tree = _run_git(repository_root, ["rev-parse", f"{proof.get('seed_commit')}^{{tree}}"])
        if tree != str(proof.get("seed_tree") or ""):
            problems.append("repair seed tree mismatch")
        verified = _verify_seed_manifest(
            repository_root,
            seed_commit=str(proof.get("seed_commit") or ""),
            baseline_commit=str(proof.get("approved_baseline_commit") or ""),
            expected_manifest_fingerprint=str(proof.get("source_manifest_fingerprint") or ""),
            expected_files_changed=list(proof.get("files_changed") or []),
        )
        if verified.get("seed_patch_fingerprint") != proof.get("seed_patch_fingerprint"):
            problems.append("repair seed patch fingerprint mismatch")
    except Exception as exc:
        problems.append(f"repair seed validation failed: {exc}")
    return problems


def delete_repair_seed_ref(proof: dict[str, Any]) -> None:
    root = Path(str(proof.get("repository_local_path") or "")).resolve()
    if root.is_dir() and proof.get("seed_ref") and proof.get("seed_commit"):
        subprocess.run(
            ["git", "update-ref", "-d", str(proof["seed_ref"]), str(proof["seed_commit"])],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
'''
(ROOT / "buildforme" / "repair_seed.py").write_text(seed_module, encoding="utf-8")

# Schema v7 repair admission table.
path = ROOT / "buildforme" / "db.py"
text = path.read_text(encoding="utf-8")
text = replace_once(text, "SCHEMA_VERSION = 6", "SCHEMA_VERSION = 7", label="schema version 7")
anchor = '''CREATE INDEX IF NOT EXISTS idx_repair_packets_run
  ON repair_packets(source_run_id, created_at);
"""
'''
replacement = '''CREATE INDEX IF NOT EXISTS idx_repair_packets_run
  ON repair_packets(source_run_id, created_at);

CREATE TABLE IF NOT EXISTS repair_admissions (
  repair_admission_id TEXT PRIMARY KEY,
  repair_packet_id TEXT NOT NULL UNIQUE REFERENCES repair_packets(repair_packet_id),
  source_run_id TEXT NOT NULL REFERENCES runs(id),
  child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
  seed_commit TEXT NOT NULL,
  seed_ref TEXT NOT NULL,
  seed_fingerprint TEXT NOT NULL,
  child_scope_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_repair_admissions_source
  ON repair_admissions(source_run_id, created_at);
"""
'''
text = replace_once(text, anchor, replacement, label="repair admissions schema")
text = replace_once(
    text,
    '''                    if current < 6:
                        self._migrate_to_v6(conn)
                    if current < SCHEMA_VERSION:
''',
    '''                    if current < 6:
                        self._migrate_to_v6(conn)
                    if current < 7:
                        self._migrate_to_v7(conn)
                    if current < SCHEMA_VERSION:
''',
    label="v7 migration dispatch",
)
anchor = '''    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
migration = '''    def _migrate_to_v7(self, conn: sqlite3.Connection) -> None:
        """Add immutable governed repair-run admissions."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_admissions (
              repair_admission_id TEXT PRIMARY KEY,
              repair_packet_id TEXT NOT NULL UNIQUE REFERENCES repair_packets(repair_packet_id),
              source_run_id TEXT NOT NULL REFERENCES runs(id),
              child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
              seed_commit TEXT NOT NULL,
              seed_ref TEXT NOT NULL,
              seed_fingerprint TEXT NOT NULL,
              child_scope_fingerprint TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_repair_admissions_source
              ON repair_admissions(source_run_id, created_at);
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
'''
text = replace_once(text, anchor, migration, label="v7 migration method")
path.write_text(text, encoding="utf-8")

# Add repair authority to scope fingerprint.
path = ROOT / "buildforme" / "governance.py"
text = path.read_text(encoding="utf-8")
anchor = '''        "constitution_lease_fingerprint": str(
            run.get("constitution_lease_fingerprint") or ""
        ),
'''
replacement = '''        "constitution_lease_fingerprint": str(
            run.get("constitution_lease_fingerprint") or ""
        ),
        "repair_packet_id": str(run.get("repair_packet_id") or ""),
        "repair_fingerprint": str(run.get("repair_fingerprint") or ""),
        "repair_source_cycle_id": str(run.get("repair_source_cycle_id") or ""),
        "repair_source_evidence_id": str(run.get("repair_source_evidence_id") or ""),
        "execution_seed_commit": str(run.get("execution_seed_commit") or ""),
        "execution_seed_ref": str(run.get("execution_seed_ref") or ""),
'''
text = replace_once(text, anchor, replacement, label="repair scope fields")
path.write_text(text, encoding="utf-8")

# Protect repair authority fields.
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
anchor = '''        "idempotency_key",
        "created_at",
'''
replacement = '''        "idempotency_key",
        "repair_packet_id",
        "repair_fingerprint",
        "repair_source_cycle_id",
        "repair_source_evidence_id",
        "execution_seed_commit",
        "execution_seed_ref",
        "requires_independent_review_after_execution",
        "created_at",
'''
text = replace_once(text, anchor, replacement, label="protected repair fields")

# Add dedicated atomic repair admission before execution control.
anchor = '''    # —— Execution control ——
'''
methods = r'''    def admit_repair_run_atomic(
        self,
        *,
        repair_packet_id: str,
        child_run: dict[str, Any],
        lease: dict[str, Any],
        seed_proof: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.governance import compute_run_scope_fingerprint
        from buildforme.repair_seed import validate_repair_seed_for_storage
        from governance.constitution_lease import validate_lease_integrity

        packet_id = str(repair_packet_id or "")
        child = dict(child_run)
        child_id = str(child.get("id") or "")
        if not packet_id or not child_id:
            raise ValueError("repair packet and child run id required")
        packet = self.get_repair_packet(packet_id)
        seed_problems = validate_repair_seed_for_storage(seed_proof, repair_packet=packet)
        if seed_problems:
            raise ValueError("repair seed rejected: " + "; ".join(seed_problems))
        scope = compute_run_scope_fingerprint(
            child, child.get("packet") if isinstance(child.get("packet"), dict) else None
        )
        if not scope or scope != str(child.get("scope_fingerprint") or ""):
            raise ValueError("repair child scope fingerprint mismatch")
        lease_problems = validate_lease_integrity(
            lease,
            expected_run_id=child_id,
            expected_provider_id=str(child.get("provider_id") or ""),
            expected_packet_id=str(child.get("packet_id") or ""),
        )
        if lease_problems:
            raise ValueError("repair child lease invalid: " + "; ".join(lease_problems))
        now = utc_now_iso()
        admission_id = f"radm-{uuid.uuid5(uuid.NAMESPACE_URL, packet_id).hex[:18]}"
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM repair_admissions WHERE repair_packet_id=?",
                (packet_id,),
            ).fetchone()
            if existing:
                admission = loads(existing[0], {})
                saved_child = conn.execute(
                    "SELECT payload_json FROM runs WHERE id=?", (str(admission.get("child_run_id") or ""),)
                ).fetchone()
                if not saved_child:
                    raise ValueError("repair admission child run is missing")
                return {"admission": admission, "run": loads(saved_child[0], {}), "replayed": True}

            packet_row = conn.execute(
                "SELECT source_cycle_id, source_run_id, source_evidence_id, repair_provider_id, repair_fingerprint, payload_json FROM repair_packets WHERE repair_packet_id=?",
                (packet_id,),
            ).fetchone()
            if not packet_row:
                raise KeyError(f"Repair packet not found: {packet_id}")
            canonical_packet = loads(packet_row[5], {})
            if canonical_packet != packet:
                raise ValueError("repair packet storage payload mismatch")
            source_run_id = str(packet_row[1])
            source_row = conn.execute(
                "SELECT row_version, status, payload_json FROM runs WHERE id=?", (source_run_id,)
            ).fetchone()
            if not source_row:
                raise KeyError(f"Source run not found: {source_run_id}")
            source = loads(source_row[2], {})
            source["status"] = source_row[1]
            source["row_version"] = int(source_row[0] or 1)
            if str(source_row[1]) != "needs_review":
                raise ValueError("repair admission requires source run status needs_review")
            if str(source.get("stage7_repair_packet_id") or "") != packet_id:
                raise ValueError("source run is not bound to repair packet")
            if str(child.get("parent_run_id") or "") != source_run_id:
                raise ValueError("repair child parent_run_id mismatch")
            if str(child.get("repair_packet_id") or "") != packet_id:
                raise ValueError("repair child packet authority mismatch")
            if str(child.get("repair_fingerprint") or "") != str(packet_row[4] or ""):
                raise ValueError("repair child fingerprint mismatch")
            if str(child.get("provider_id") or "") != str(packet_row[3] or ""):
                raise ValueError("repair child provider mismatch")
            if str(child.get("baseline_commit") or "") != str(packet.get("approved_baseline_commit") or ""):
                raise ValueError("repair child approved baseline mismatch")
            if str(child.get("execution_seed_commit") or "") != str(seed_proof.get("seed_commit") or ""):
                raise ValueError("repair child execution seed mismatch")
            if str(child.get("execution_seed_ref") or "") != str(seed_proof.get("seed_ref") or ""):
                raise ValueError("repair child execution seed ref mismatch")
            child_packet = child.get("packet") if isinstance(child.get("packet"), dict) else {}
            if sorted(str(x) for x in (child_packet.get("allowed_files") or [])) != list(packet.get("allowed_files") or []):
                raise ValueError("repair child allowed files differ from repair packet")
            if sorted(str(x) for x in (child_packet.get("forbidden_files") or [])) != list(packet.get("forbidden_files") or []):
                raise ValueError("repair child forbidden files differ from repair packet")
            if str(child.get("constitution_hash") or "") != str(packet.get("source_constitution_hash") or ""):
                raise ValueError("repair child Constitution hash mismatch")
            ack = conn.execute(
                "SELECT constitution_acknowledged, constitution_hash FROM provider_acks WHERE provider_id=?",
                (str(child.get("provider_id") or ""),),
            ).fetchone()
            if not ack or not bool(ack[0]) or str(ack[1] or "") != str(child.get("constitution_hash") or ""):
                raise ValueError("repair child provider acknowledgement invalid")
            if conn.execute("SELECT id FROM runs WHERE id=?", (child_id,)).fetchone():
                raise ValueError(f"repair child run already exists without admission: {child_id}")

            lock_id = str(source.get("task_lock_id") or "")
            if lock_id:
                lock_row = conn.execute(
                    "SELECT run_id, active FROM task_locks WHERE id=?", (lock_id,)
                ).fetchone()
                if not lock_row or not bool(lock_row[1]) or str(lock_row[0] or "") != source_run_id:
                    raise ValueError("source task lock cannot be transferred to repair child")
                conn.execute(
                    "UPDATE task_locks SET run_id=?, reason=? WHERE id=? AND run_id=? AND active=1",
                    (child_id, f"Stage 7 repair child for {source_run_id}", lock_id, source_run_id),
                )
            else:
                lock_id = new_id("tlock")
                task_key = str(source.get("task_id") or source.get("packet_id") or source_run_id)
                conn.execute(
                    "INSERT INTO task_locks(id, task_key, project_id, run_id, reason, active, created_at, released_at) VALUES (?,?,?,?,?,1,?,NULL)",
                    (lock_id, task_key, source.get("project_id"), child_id, "Stage 7 governed repair run", now),
                )
            child["task_lock_id"] = lock_id
            child["created_at"] = child.get("created_at") or now
            child["updated_at"] = now
            child["row_version"] = 1

            lease_record = dict(lease)
            lease_record["stored_at"] = now
            if conn.execute(
                "SELECT lease_id FROM constitution_leases WHERE lease_id=?", (str(lease["lease_id"]),)
            ).fetchone():
                raise ValueError("repair child lease id already exists")
            conn.execute(
                "INSERT INTO constitution_leases(lease_id, run_id, provider_id, packet_id, constitution_version, constitution_hash, lease_fingerprint, payload_json, stored_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    lease["lease_id"], child_id, child.get("provider_id"), child.get("packet_id"),
                    lease.get("constitution_version"), lease.get("constitution_hash"),
                    lease.get("lease_fingerprint"), dumps(lease_record), now,
                ),
            )
            conn.execute(
                """INSERT INTO runs(
                id, project_id, task_id, packet_id, provider_id, repository, repository_local_path,
                baseline_ref, baseline_commit, requested_target_branch, execution_branch, operating_mode,
                risk, status, execution_mode, scope_fingerprint, constitution_version, constitution_hash,
                constitution_lease_id, constitution_lease_fingerprint, task_lock_id, payload_json,
                created_at, updated_at, started_at, finished_at, idempotency_key, row_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    child_id, child.get("project_id"), child.get("task_id"), child.get("packet_id"),
                    child.get("provider_id"), child.get("repository"), child.get("repository_local_path"),
                    child.get("baseline_ref"), child.get("baseline_commit"), child.get("requested_target_branch"),
                    child.get("execution_branch"), child.get("operating_mode"), child.get("risk"),
                    child.get("status") or "draft", child.get("execution_mode") or "live_supervised",
                    child.get("scope_fingerprint"), child.get("constitution_version"), child.get("constitution_hash"),
                    child.get("constitution_lease_id"), child.get("constitution_lease_fingerprint"), lock_id,
                    dumps(child), child["created_at"], now, child.get("started_at"), child.get("finished_at"),
                    child.get("idempotency_key"), 1,
                ),
            )
            admission = {
                "schema": "buildforme.repair_admission.v1",
                "repair_admission_id": admission_id,
                "repair_packet_id": packet_id,
                "repair_fingerprint": packet.get("repair_fingerprint"),
                "source_run_id": source_run_id,
                "source_cycle_id": packet.get("source_cycle_id"),
                "source_evidence_id": packet.get("source_evidence_id"),
                "child_run_id": child_id,
                "repair_provider_id": child.get("provider_id"),
                "seed_commit": seed_proof.get("seed_commit"),
                "seed_ref": seed_proof.get("seed_ref"),
                "seed_tree": seed_proof.get("seed_tree"),
                "seed_fingerprint": seed_proof.get("seed_fingerprint"),
                "child_scope_fingerprint": child.get("scope_fingerprint"),
                "original_approved_baseline": child.get("baseline_commit"),
                "execution_seed_commit": child.get("execution_seed_commit"),
                "task_lock_id": lock_id,
                "created_at": now,
                "immutable": True,
            }
            conn.execute(
                "INSERT INTO repair_admissions(repair_admission_id, repair_packet_id, source_run_id, child_run_id, seed_commit, seed_ref, seed_fingerprint, child_scope_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (
                    admission_id, packet_id, source_run_id, child_id, admission["seed_commit"],
                    admission["seed_ref"], admission["seed_fingerprint"], admission["child_scope_fingerprint"],
                    dumps(admission), now,
                ),
            )
            source["task_lock_id"] = None
            source["stage7_repair_status"] = "child_admitted"
            source["stage7_repair_child_run_id"] = child_id
            source["stage7_repair_admission_id"] = admission_id
            source["updated_at"] = now
            source["row_version"] = int(source_row[0] or 1) + 1
            cur = conn.execute(
                "UPDATE runs SET task_lock_id=NULL, payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (dumps(source), now, source["row_version"], source_run_id, int(source_row[0] or 1)),
            )
            if cur.rowcount != 1:
                raise ValueError("stale source run race during repair admission")
            metadata = {
                "repair_packet_id": packet_id,
                "repair_admission_id": admission_id,
                "child_run_id": child_id,
                "seed_commit": admission["seed_commit"],
                "seed_fingerprint": admission["seed_fingerprint"],
            }
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), source_run_id, "stage7_repair_child_admitted", "Governed repair child admitted", actor, dumps(metadata), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), child_id, "repair_run_created", "Repair run admitted from exact reviewed seed", actor, dumps(metadata), now),
            )
        return {"admission": admission, "run": child, "source_run": source, "replayed": False}

    def get_repair_admission(self, repair_packet_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM repair_admissions WHERE repair_packet_id=?",
                (str(repair_packet_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Repair admission not found: {repair_packet_id}")
        return loads(row[0], {})

    # —— Execution control ——
'''
text = replace_once(text, anchor, methods, label="repair admission storage")
path.write_text(text, encoding="utf-8")

# LocalStore wrappers.
path = ROOT / "buildforme" / "storage.py"
text = path.read_text(encoding="utf-8")
anchor = '''    # —— Internals ——
'''
replacement = '''    def admit_repair_run_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.admit_repair_run_atomic(**kwargs)

    def get_repair_admission(self, repair_packet_id: str) -> dict[str, Any]:
        return self.s6.get_repair_admission(repair_packet_id)

    # —— Internals ——
'''
text = replace_once(text, anchor, replacement, label="repair admission wrappers")
path.write_text(text, encoding="utf-8")

# Build deterministic child run in repair service.
path = ROOT / "buildforme" / "repair_service.py"
text = path.read_text(encoding="utf-8")
text = text.replace("from typing import Any\n", "from typing import Any\nimport hashlib\nimport json\n\n")
text = text.replace(
    "from buildforme.governance import validate_actor, validate_safe_id\n",
    "from buildforme.governance import compute_run_scope_fingerprint, validate_actor, validate_branch, validate_safe_id\n",
)
text = text.replace(
    "from buildforme.repair_contracts import build_repair_packet_record\n",
    "from buildforme.repair_contracts import build_repair_packet_record\nfrom buildforme.repair_seed import create_repair_seed, delete_repair_seed_ref\nfrom governance.constitution_engine import get_engine\nfrom governance.constitution_lease import issue_lease\n",
)
text += r'''


def admit_governed_repair_run(
    store: LocalStore,
    repair_packet_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    packet_id = validate_safe_id(repair_packet_id, field="repair_packet_id")
    actor = validate_actor(actor)
    try:
        existing = store.get_repair_admission(packet_id)
        return {
            "admission": existing,
            "run": store.get_run(str(existing.get("child_run_id") or "")),
            "source_run": store.get_run(str(existing.get("source_run_id") or "")),
            "replayed": True,
        }
    except KeyError:
        pass
    repair_packet = store.get_repair_packet(packet_id)
    source_run = store.get_run(str(repair_packet.get("source_run_id") or ""))
    source_evidence = store.get_evidence_by_id(str(repair_packet.get("source_evidence_id") or ""))
    seed = create_repair_seed(
        repair_packet=repair_packet,
        source_run=source_run,
        source_evidence=source_evidence,
    )
    try:
        digest = hashlib.sha256(packet_id.encode("utf-8")).hexdigest()
        run_id = f"run-repair-{digest[:16]}"
        execution_branch = validate_branch(f"feature/repair-{digest[:16]}")
        child_packet_id = f"pkt-repair-{digest[:16]}"
        engine = get_engine()
        source_packet = source_run.get("packet") if isinstance(source_run.get("packet"), dict) else {}
        child_packet = engine.attach_to_packet(
            {
                "id": child_packet_id,
                "objective": "Resolve every blocking finding from the bound independent review",
                "context": json.dumps(
                    {
                        "repair_packet_id": packet_id,
                        "repair_fingerprint": repair_packet.get("repair_fingerprint"),
                        "source_cycle_id": repair_packet.get("source_cycle_id"),
                        "source_evidence_id": repair_packet.get("source_evidence_id"),
                        "blocking_finding_ids": [
                            item.get("finding_id")
                            for item in (repair_packet.get("source_blocking_findings") or [])
                        ],
                    },
                    sort_keys=True,
                ),
                "target_repository": repair_packet.get("repository"),
                "target_branch": source_run.get("requested_target_branch") or source_run.get("target_branch"),
                "operating_mode": source_run.get("operating_mode") or "IMPLEMENTATION",
                "risk": source_run.get("risk") or "YELLOW",
                "allowed_files": list(repair_packet.get("allowed_files") or []),
                "forbidden_files": list(repair_packet.get("forbidden_files") or []),
                "acceptance_criteria": list(repair_packet.get("repair_acceptance_criteria") or []),
                "required_tests": list(source_packet.get("required_tests") or []),
                "manual_proof": list(source_packet.get("manual_proof") or []),
            }
        )
        provider_id = str(repair_packet.get("repair_provider_id") or "")
        lease_id = f"lease-repair-{digest[:16]}"
        lease = issue_lease(
            engine.constitution,
            run_id=run_id,
            provider_id=provider_id,
            packet_id=child_packet_id,
            actor=actor,
            lease_id=lease_id,
        )
        attempt = int(source_run.get("attempt") or 0) + 1
        max_attempts = min(3, max(int(source_run.get("max_attempts") or 1), attempt + 1))
        child = {
            "id": run_id,
            "project_id": source_run.get("project_id"),
            "task_id": source_run.get("task_id"),
            "packet_id": child_packet_id,
            "packet": child_packet,
            "provider_id": provider_id,
            "repository": repair_packet.get("repository"),
            "repository_local_path": repair_packet.get("repository_local_path"),
            "baseline_ref": repair_packet.get("approved_baseline_commit"),
            "baseline_commit": repair_packet.get("approved_baseline_commit"),
            "execution_seed_commit": seed.get("seed_commit"),
            "execution_seed_ref": seed.get("seed_ref"),
            "requested_target_branch": source_run.get("requested_target_branch") or source_run.get("target_branch"),
            "execution_branch": execution_branch,
            "target_branch": execution_branch,
            "operating_mode": source_run.get("operating_mode") or "IMPLEMENTATION",
            "risk": source_run.get("risk") or "YELLOW",
            "status": "draft",
            "requested_capabilities": list(source_run.get("requested_capabilities") or []),
            "approval_requirements": [],
            "approval_records": [],
            "preflight": None,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "timeout_minutes": int(source_run.get("timeout_minutes") or 30),
            "budget": dict(source_run.get("budget") or {}),
            "parent_run_id": source_run.get("id"),
            "repair_packet_id": packet_id,
            "repair_fingerprint": repair_packet.get("repair_fingerprint"),
            "repair_source_cycle_id": repair_packet.get("source_cycle_id"),
            "repair_source_evidence_id": repair_packet.get("source_evidence_id"),
            "requires_independent_review_after_execution": True,
            "dry_run_result": None,
            "result_summary": None,
            "status_history": [],
            "started_at": None,
            "finished_at": None,
            "live_execution": True,
            "mode": "live_supervised",
            "execution_mode": "live_supervised",
            "transport": "cli",
            "worktree": None,
            "evidence": None,
            "verification": None,
            "review": None,
            "task_lock_id": None,
            "evidence_ids": [],
            "idempotency_key": f"stage7-repair:{packet_id}",
        }
        child = engine.attach_to_run(child, lease=lease, actor=actor)
        child["scope_fingerprint"] = compute_run_scope_fingerprint(child, child_packet)
        return store.admit_repair_run_atomic(
            repair_packet_id=packet_id,
            child_run=child,
            lease=lease,
            seed_proof=seed,
            actor=actor,
        )
    except Exception:
        delete_repair_seed_ref(seed)
        raise
'''
path.write_text(text, encoding="utf-8")

# Use seed commit for worktree start while preserving original baseline authority.
path = ROOT / "buildforme" / "execution_service.py"
text = path.read_text(encoding="utf-8")
anchor = '''    approved_baseline = str(run["baseline_commit"])
    run_branch = str(run.get("execution_branch") or "")
'''
replacement = '''    approved_baseline = str(run["baseline_commit"])
    execution_seed = str(run.get("execution_seed_commit") or approved_baseline)
    run_branch = str(run.get("execution_branch") or "")
'''
text = replace_once(text, anchor, replacement, label="execution seed selection")
text = replace_once(
    text,
    '''        baseline_commit=approved_baseline,
        run_id=run_id,
''',
    '''        baseline_commit=execution_seed,
        run_id=run_id,
''',
    label="worktree seed commit",
)
text = replace_once(
    text,
    '''    if str(worktree_meta.get("baseline_commit")) != approved_baseline:
        raise ValueError("worktree baseline does not match approved baseline")
''',
    '''    if str(worktree_meta.get("baseline_commit")) != execution_seed:
        raise ValueError("worktree seed does not match governed execution seed")
''',
    label="worktree seed verification",
)
text = replace_once(
    text,
    '''            "baseline": approved_baseline,
            "execution_branch": run_branch,
''',
    '''            "approved_baseline": approved_baseline,
            "execution_seed_commit": execution_seed,
            "execution_branch": run_branch,
''',
    label="seed event metadata",
)
path.write_text(text, encoding="utf-8")

# Advance schema assertions to 7.
for name in (
    "tests/test_stage6_execution.py",
    "tests/test_stage6_redteam_round2.py",
    "tests/test_stage7_packet7a_contract.py",
    "tests/test_stage7_review_authority.py",
    "tests/test_stage7_review_execution.py",
    "tests/test_stage7_packet7d_repair_authority.py",
):
    p = ROOT / name
    if not p.exists():
        continue
    t = p.read_text(encoding="utf-8")
    t = t.replace("SCHEMA_VERSION, 6", "SCHEMA_VERSION, 7")
    t = t.replace('["schema_version"], 6', '["schema_version"], 7')
    p.write_text(t, encoding="utf-8")

# Packet 7D-B tests.
test = r'''from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.db import SCHEMA_VERSION, dumps
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.repair_service import admit_governed_repair_run, create_governed_repair_packet
from buildforme.review_contracts import aggregate_review_reports, build_review_cycle_record, build_review_report_record
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


class Stage7RepairAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "repair@test.local")
        self._git("config", "user.name", "repair-test")
        self._git("remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "baseline")
        self.baseline = self._git_out("rev-parse", "HEAD")
        self._git("checkout", "-b", "feature/source-run")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.repo / "new.py").write_text("new_value = 3\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "runtime" / "state.json")
        self.store.upsert_project({
            "id": "repair-project", "name": "Repair", "repository": "shanchaudary/Buildforme",
            "status": "active", "local_repository_root": str(self.repo),
        })
        self.store.register_repository_binding({
            "repository": "shanchaudary/Buildforme", "local_path": str(self.repo), "project_id": "repair-project",
        })
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet({
            "id": "pkt-source", "objective": "Implement change", "target_repository": "shanchaudary/Buildforme",
            "target_branch": "feature/source", "operating_mode": "IMPLEMENTATION", "risk": "YELLOW",
            "allowed_files": ["app.py", "new.py"], "forbidden_files": [".env"],
            "acceptance_criteria": ["value updated"],
        })
        lease = engine.issue_run_lease(run_id="run-source", provider_id="glm", packet_id=packet["id"], actor="test")
        self.store.save_constitution_lease(lease)
        run = {
            "id": "run-source", "project_id": "repair-project", "task_id": "task-repair",
            "packet_id": packet["id"], "packet": packet, "provider_id": "glm",
            "repository": "shanchaudary/Buildforme", "repository_local_path": str(self.repo),
            "baseline_ref": self.baseline, "baseline_commit": self.baseline,
            "requested_target_branch": "feature/source", "execution_branch": "feature/source-run",
            "target_branch": "feature/source-run", "operating_mode": "IMPLEMENTATION", "risk": "YELLOW",
            "status": "needs_review", "execution_mode": "live_supervised", "mode": "live_supervised",
            "transport": "cli", "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "attempt": 0, "max_attempts": 2, "timeout_minutes": 30, "budget": {"max_cost_usd": 0},
            "review": {"hard_blocks": []}, "worktree_path": str(self.repo), "evidence_ids": [],
        }
        run = engine.attach_to_run(run, lease=lease, actor="test")
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
        self.run = self.store.save_run_for_setup(run)
        self.lock = self.store.create_task_lock({
            "task_key": "task-repair", "project_id": "repair-project", "run_id": self.run["id"], "reason": "source run",
        })
        with self.store.s6.db.transaction() as conn:
            row = conn.execute("SELECT row_version, payload_json FROM runs WHERE id=?", (self.run["id"],)).fetchone()
            payload = __import__("json").loads(row[1]); payload["task_lock_id"] = self.lock["id"]
            conn.execute("UPDATE runs SET task_lock_id=?, payload_json=?, row_version=? WHERE id=?", (self.lock["id"], dumps(payload), int(row[0]) + 1, self.run["id"]))
        self.run = self.store.get_run(self.run["id"])
        manifest = collect_changed_file_manifest(self.repo, baseline_commit=self.baseline)
        patch = collect_patch_evidence(self.repo, baseline_commit=self.baseline)
        evidence = build_evidence_bundle(
            run=self.run, packet=packet,
            process_result={"ok": True, "exit_code": 0, "pid": 11, "cleanup_ok": True, "process_group_isolated": True},
            worktree={"worktree_path": str(self.repo), "baseline_commit": self.baseline, "head_commit": self.baseline, "branch": "feature/source-run"},
            diff={"manifest": manifest, "patch_fingerprint": patch["patch_fingerprint"]},
            provider_health={"version": "test", "executable": "glm"},
            verification={"passed": True, "blocking_reasons": [], "checks": []}, constitution_result={"passed": True},
            approved_baseline_sha=self.baseline, final_head_sha=self.baseline, execution_branch="feature/source-run",
            patch_fingerprint=patch["patch_fingerprint"], manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        self.evidence = self.store.save_run_evidence(evidence)
        for provider_id in ("codex", "claude", "glm"):
            self.store.set_provider_constitution_ack(provider_id, {
                "constitution_supported": True, "constitution_acknowledged": True,
                "constitution_version": engine.version(), "constitution_hash": engine.content_hash(),
                "constitution_last_refresh": "now", "constitution_acknowledged_at": "now", "constitution_ack_actor": "test",
            })
        cycle, assignments = build_review_cycle_record(
            run=self.run, evidence=self.evidence,
            reviewers=[
                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "claude-reviewer", "provider_id": "claude", "role": "security"},
            ], actor="shan",
        )
        created = self.store.create_review_cycle_atomic(cycle=cycle, assignments=assignments, actor="shan")
        reports, all_findings, report_map = [], [], {}
        for index, assignment in enumerate(created["assignments"]):
            payload = ({
                "verdict": "changes_required", "summary": "repair", "findings": [{
                    "severity": "high", "category": "correctness", "summary": "wrong value",
                    "evidence": "app.py value", "recommendation": "set correct value",
                }],
            } if index == 0 else {"verdict": "pass", "summary": "clear", "findings": []})
            report, findings = build_review_report_record(cycle=created["cycle"], assignment={**assignment, "status": "pending"}, payload=payload)
            reports.append(report); all_findings.extend(findings); report_map[assignment["assignment_id"]] = report["report_id"]
        aggregate = aggregate_review_reports(
            cycle=created["cycle"], assignments=[{**a, "status": "submitted"} for a in created["assignments"]], reports=reports,
        )
        with self.store.s6.db.transaction() as conn:
            for assignment, report in zip(created["assignments"], reports):
                conn.execute("UPDATE review_assignments SET status='submitted', payload_json=? WHERE id=?", (dumps({**assignment, "status": "submitted", "report_id": report["report_id"]}), assignment["assignment_id"]))
                conn.execute("INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)", (report["report_id"], created["cycle"]["cycle_id"], assignment["assignment_id"], report["verdict"], report["report_fingerprint"], dumps(report), report["created_at"]))
            for finding in all_findings:
                conn.execute("INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)", (finding["finding_id"], report_map[finding["assignment_id"]], created["cycle"]["cycle_id"], finding["assignment_id"], finding["severity"], finding["category"], 1 if finding["blocking"] else 0, finding["finding_fingerprint"], dumps(finding), "now"))
            cycle_payload = dict(created["cycle"]); cycle_payload["status"] = "repair_required"; cycle_payload["aggregate"] = aggregate
            conn.execute("UPDATE review_cycles SET status='repair_required', aggregate_json=?, payload_json=?, finalized_at='now' WHERE id=?", (dumps(aggregate), dumps(cycle_payload), created["cycle"]["cycle_id"]))
            row = conn.execute("SELECT row_version, payload_json FROM runs WHERE id=?", (self.run["id"],)).fetchone()
            source = __import__("json").loads(row[1]); source["stage7_review_cycle_id"] = created["cycle"]["cycle_id"]; source["stage7_review_required"] = True; source["row_version"] = int(row[0]) + 1
            conn.execute("UPDATE runs SET payload_json=?, row_version=? WHERE id=?", (dumps(source), source["row_version"], self.run["id"]))
        self.cycle = self.store.get_review_cycle(created["cycle"]["cycle_id"])
        self.repair_packet = create_governed_repair_packet(self.store, self.cycle["cycle_id"], repair_provider_id="glm", actor="shan")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def _git_out(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def test_schema_v7(self):
        self.assertEqual(SCHEMA_VERSION, 7)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 7)

    def test_exact_dirty_state_seeded_without_mutating_source_branch(self):
        source_head = self._git_out("rev-parse", "HEAD")
        source_status = self._git_out("status", "--porcelain")
        result = admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        child = result["run"]; admission = result["admission"]
        self.assertEqual(self._git_out("rev-parse", "HEAD"), source_head)
        self.assertEqual(self._git_out("status", "--porcelain"), source_status)
        self.assertEqual(child["baseline_commit"], self.baseline)
        self.assertNotEqual(child["execution_seed_commit"], self.baseline)
        self.assertEqual(self._git_out("rev-parse", admission["seed_ref"]), admission["seed_commit"])
        self.assertEqual(child["scope_fingerprint"], compute_run_scope_fingerprint(child, child["packet"]))
        self.assertEqual(child["task_lock_id"], self.lock["id"])
        self.assertIsNone(result["source_run"].get("task_lock_id"))
        lock = next(item for item in self.store.list_task_locks(active_only=True) if item["id"] == self.lock["id"])
        self.assertEqual(lock["run_id"], child["id"])

    def test_source_drift_blocks_admission_without_child(self):
        (self.repo / "app.py").write_text("tampered = True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        with self.assertRaises(KeyError):
            self.store.get_repair_admission(self.repair_packet["repair_packet_id"])

    def test_admission_is_idempotent(self):
        first = admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        second = admit_governed_repair_run(self.store, self.repair_packet["repair_packet_id"], actor="shan")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])

    def test_execution_service_uses_seed_but_verifies_original_baseline(self):
        source = Path("buildforme/execution_service.py").read_text(encoding="utf-8")
        self.assertIn('execution_seed = str(run.get("execution_seed_commit") or approved_baseline)', source)
        self.assertIn("baseline_commit=execution_seed", source)
        self.assertIn("baseline_commit=approved_baseline", source)


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_stage7_packet7d_repair_admission.py").write_text(test, encoding="utf-8")

contract = r'''from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Stage7Packet7DAdmissionContractTests(unittest.TestCase):
    def test_repair_seed_does_not_run_shell_or_push(self):
        source = Path("buildforme/repair_seed.py").read_text(encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertNotIn('"push"', source)
        self.assertIn("refs/buildforme/repair-seeds/", source)

    def test_repair_service_uses_only_dedicated_atomic_admission(self):
        source = Path("buildforme/repair_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertIn("admit_repair_run_atomic", calls)
        self.assertNotIn("save_run", calls)
        self.assertNotIn("save_run_for_setup", calls)

    def test_scope_and_protected_authority_include_seed(self):
        governance = Path("buildforme/governance.py").read_text(encoding="utf-8")
        storage = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        for token in ("execution_seed_commit", "repair_packet_id", "repair_fingerprint"):
            self.assertIn(token, governance)
            self.assertIn(token, storage)


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_stage7_packet7d_admission_contract.py").write_text(contract, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n## Packet 7D-B — exact repair seed and child admission\n\n- Buildforme creates a deterministic local seed commit from only the exact changed paths in the immutable source evidence. It does not move the source branch, modify the reviewed worktree, push a ref, or use the seed as the approved baseline.\n- The seed is retained under a local `refs/buildforme/repair-seeds/` ref and independently revalidated against the source manifest before storage admission.\n- The child run keeps the original approved baseline for complete diff/evidence verification while `execution_seed_commit` controls only the initial repair worktree state.\n- Repair packet, seed proof, child run, fresh Constitution lease, scope fingerprint, task-lock transfer, admission record, source-run binding, and audit events commit through one dedicated SQLite transaction.\n- A failed admission deletes a newly created seed ref; duplicate admission replays the one canonical child.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7D repair admission applied")
