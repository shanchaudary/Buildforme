"""Isolated Git worktrees for supervised runs (generic, repo-agnostic)."""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

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


def create_isolated_worktree(
    *,
    repo_root: Path | None = None,
    branch: str,
    baseline_ref: str = "HEAD",
    worktrees_root: Path | None = None,
    run_id: str | None = None,
    allow_dirty_main: bool = False,
) -> dict[str, Any]:
    """Create a new feature branch worktree pinned to baseline commit.

    Never operates on main checkout as the work directory for provider edits.
    """
    root = resolve_repo_root(repo_root)
    branch = validate_feature_branch(branch)
    assert_clean_or_allow(root, allow_dirty=allow_dirty_main)
    baseline = get_baseline_commit(root, baseline_ref)

    wt_root = Path(worktrees_root or (root / "runtime" / "worktrees")).resolve()
    # Ensure worktrees stay under runtime and not inside sensitive paths
    wt_root.mkdir(parents=True, exist_ok=True)
    rid = re.sub(r"[^A-Za-z0-9._-]", "-", str(run_id or uuid.uuid4().hex[:10]))[:40]
    worktree_path = wt_root / f"{rid}-{branch.replace('/', '-')}"
    if worktree_path.exists():
        raise ValueError(f"worktree path already exists: {worktree_path}")

    # Branch may already exist — create worktree from existing or new branch at baseline
    branch_exists = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=root)
    if branch_exists["ok"]:
        # Ensure worktree for existing branch
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
    path = Path(worktree_path)
    args = ["diff", "--stat"]
    if baseline_commit:
        args = ["diff", "--stat", f"{baseline_commit}...HEAD"]
    stat = _run_git(args, cwd=path)
    name_only_args = ["diff", "--name-only"]
    if baseline_commit:
        name_only_args = ["diff", "--name-only", f"{baseline_commit}...HEAD"]
    names = _run_git(name_only_args, cwd=path)
    patch_args = ["diff"]
    if baseline_commit:
        patch_args = ["diff", f"{baseline_commit}...HEAD"]
    # Also include unstaged
    unstaged = _run_git(["diff", "--name-only"], cwd=path)
    unstaged_stat = _run_git(["diff", "--stat"], cwd=path)
    files = sorted(
        {
            ln.strip()
            for ln in ((names.get("stdout") or "") + "\n" + (unstaged.get("stdout") or "")).splitlines()
            if ln.strip()
        }
    )
    return {
        "files_changed": files,
        "diff_stat": (stat.get("stdout") or "") + ("\n" + (unstaged_stat.get("stdout") or "")).rstrip(),
        "file_count": len(files),
        "baseline_commit": baseline_commit,
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
