"""Deterministic local repair seeds for Stage 7 governed repair runs."""

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
