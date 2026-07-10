"""Isolated Git worktrees for supervised runs (generic, repo-agnostic)."""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from buildforme.changed_files import collect_changed_file_manifest
from buildforme.storage import utc_now_iso

SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
FORBIDDEN_WORKTREE_BRANCHES = frozenset({"main", "master"})


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: float = 60.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "exit_code": 127, "stdout": "", "stderr": "git not found", "args": args}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": 124, "stdout": "", "stderr": "git timed out", "args": args}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "args": args,
    }


def resolve_repo_root(path: str | Path | None = None) -> Path:
    start = Path(path or Path.cwd()).resolve()
    probe = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    if not probe["ok"]:
        raise ValueError(f"not a git repository: {start} ({probe.get('stderr')})")
    return Path(probe["stdout"]).resolve()


def get_baseline_commit(repo_root: Path, ref: str = "HEAD") -> str:
    res = _run_git(["rev-parse", ref], cwd=repo_root)
    if not res["ok"] or not re.fullmatch(r"[0-9a-f]{7,40}", res["stdout"]):
        raise ValueError(f"cannot resolve baseline {ref}: {res.get('stderr')}")
    return res["stdout"]


def validate_feature_branch(branch: str) -> str:
    text = str(branch or "").strip()
    if not text or not SAFE_BRANCH_RE.fullmatch(text):
        raise ValueError("invalid feature branch")
    if text in FORBIDDEN_WORKTREE_BRANCHES:
        raise ValueError("implementation worktrees cannot target main/master")
    if ".." in text or text.startswith("/"):
        raise ValueError("invalid feature branch path")
    return text


def assert_clean_or_allow(repo_root: Path, *, allow_dirty: bool = False) -> dict[str, Any]:
    status = _run_git(["status", "--porcelain"], cwd=repo_root)
    dirty = bool(status.get("stdout"))
    if dirty and not allow_dirty:
        raise ValueError("repository working tree is dirty; refuse worktree create (fail closed)")
    return {"dirty": dirty, "status": status.get("stdout") or ""}


def default_workspace_root() -> Path:
    """Buildforme-owned workspace root — outside supervised repositories."""
    import os

    override = os.environ.get("BUILDFORME_WORKSPACE_ROOT")
    if override:
        return Path(override).resolve()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("HOME") or str(Path.home())
    return (Path(local) / "Buildforme" / "workspaces").resolve()


def create_isolated_worktree(
    *,
    repo_root: Path | None = None,
    branch: str,
    baseline_commit: str | None = None,
    baseline_ref: str = "HEAD",
    worktrees_root: Path | None = None,
    run_id: str | None = None,
    allow_dirty_main: bool = False,
    allow_existing_branch: bool = False,
    require_clean_parent: bool = True,
) -> dict[str, Any]:
    """Create a new feature branch worktree pinned to an approved baseline SHA.

    Workspaces are Buildforme-owned (outside target repos) by default.
    Existing branches fail closed unless allow_existing_branch and SHA matches.
    """
    root = resolve_repo_root(repo_root)
    branch = validate_feature_branch(branch)
    import os

    if str(os.environ.get("BUILDFORME_ALLOW_DIRTY_PARENT") or "").lower() in {"1", "true", "yes"}:
        allow_dirty_main = True
        require_clean_parent = False
    parent_state = assert_clean_or_allow(root, allow_dirty=not require_clean_parent or allow_dirty_main)
    if parent_state.get("dirty") and require_clean_parent and not allow_dirty_main:
        raise ValueError("repository working tree is dirty; refuse worktree create (fail closed)")

    if baseline_commit:
        if not re.fullmatch(r"[0-9a-f]{7,40}", str(baseline_commit)):
            raise ValueError("baseline_commit must be a full/short git SHA")
        baseline = str(baseline_commit)
        full = _run_git(["rev-parse", baseline], cwd=root)
        if not full["ok"]:
            raise ValueError(f"baseline commit not found: {baseline}")
        baseline = full["stdout"]
    else:
        baseline = get_baseline_commit(root, baseline_ref)

    # Buildforme-owned workspace outside the supervised repository
    if worktrees_root is None:
        repo_key = re.sub(r"[^A-Za-z0-9._-]+", "-", str(root).lower())[:48]
        wt_root = default_workspace_root() / repo_key / str(run_id or uuid.uuid4().hex[:12])
    else:
        wt_root = Path(worktrees_root).resolve()
        # Refuse putting workspaces inside the supervised repo
        try:
            wt_root.relative_to(root)
            raise ValueError("worktrees_root must be outside the supervised repository")
        except ValueError as exc:
            if "outside the supervised" in str(exc):
                raise
            # relative_to failed → path is outside root — OK
            pass
    wt_root.mkdir(parents=True, exist_ok=True)
    worktree_path = wt_root / "worktree"
    if worktree_path.exists():
        raise ValueError(f"worktree path already exists: {worktree_path}")

    branch_exists = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=root)
    if branch_exists["ok"]:
        tip = _run_git(["rev-parse", branch], cwd=root)
        tip_sha = tip.get("stdout") or ""
        if not allow_existing_branch:
            raise ValueError(
                f"branch collision: {branch} already exists at {tip_sha[:12]}; "
                "refusing silent reuse (fail closed)"
            )
        if tip_sha != baseline:
            raise ValueError(
                f"existing branch {branch} at {tip_sha[:12]} does not match approved baseline {baseline[:12]}"
            )
        # Ensure not checked out elsewhere
        add = _run_git(["worktree", "add", str(worktree_path), branch], cwd=root, timeout=120)
    else:
        add = _run_git(
            ["worktree", "add", "-b", branch, str(worktree_path), baseline],
            cwd=root,
            timeout=120,
        )
    if not add["ok"]:
        raise ValueError(f"worktree create failed: {add.get('stderr') or add.get('stdout')}")

    head = get_baseline_commit(worktree_path, "HEAD")
    if head != baseline:
        # Fail closed — remove broken worktree
        remove_worktree(repo_root=root, worktree_path=worktree_path, force=True)
        raise ValueError(f"worktree HEAD {head} != approved baseline {baseline}")

    return {
        "worktree_path": str(worktree_path),
        "repository_root": str(root),
        "branch": branch,
        "baseline_commit": baseline,
        "head_commit": head,
        "created_at": utc_now_iso(),
        "run_id": run_id,
        "isolated": True,
        "on_main": False,
        "fresh_branch": not branch_exists["ok"],
        "parent_dirty": bool(parent_state.get("dirty")),
        "workspace_root": str(wt_root),
        "buildforme_owned_workspace": True,
    }


def worktree_status(worktree_path: Path) -> dict[str, Any]:
    path = Path(worktree_path)
    if not path.exists():
        return {"exists": False, "path": str(path)}
    status = _run_git(["status", "--porcelain"], cwd=path)
    head = _run_git(["rev-parse", "HEAD"], cwd=path)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    return {
        "exists": True,
        "path": str(path),
        "dirty": bool(status.get("stdout")),
        "status_porcelain": status.get("stdout") or "",
        "head_commit": head.get("stdout") if head.get("ok") else None,
        "branch": branch.get("stdout") if branch.get("ok") else None,
    }


def collect_diff(worktree_path: Path, *, baseline_commit: str | None = None) -> dict[str, Any]:
    """Backward-compatible wrapper — prefer collect_changed_file_manifest."""
    manifest = collect_changed_file_manifest(worktree_path, baseline_commit=baseline_commit)
    return {
        "files_changed": list(manifest.get("files_changed") or []),
        "diff_stat": manifest.get("diff_stat") or "",
        "file_count": int(manifest.get("file_count") or 0),
        "baseline_commit": baseline_commit,
        "manifest": manifest,
        "manifest_fingerprint": manifest.get("manifest_fingerprint"),
        "complete": True,
    }


def remove_worktree(
    *,
    repo_root: Path,
    worktree_path: Path,
    force: bool = True,
) -> dict[str, Any]:
    root = resolve_repo_root(repo_root)
    path = Path(worktree_path)
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    res = _run_git(args, cwd=root, timeout=120)
    leftover = path.exists()
    if leftover and force:
        shutil.rmtree(path, ignore_errors=True)
        leftover = path.exists()
    return {
        "removed": not leftover,
        "git_ok": res["ok"],
        "stderr": res.get("stderr") or "",
        "path": str(path),
        "at": utc_now_iso(),
    }
