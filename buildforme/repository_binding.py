"""Bind supervised runs to authorized project repository identity (not arbitrary paths)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from buildforme.governance import canonicalize_repository, normalize_repo_for_compare
from buildforme.storage import utc_now_iso

SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./:\\ -]+$")


def _git(args: list[str], *, cwd: Path, timeout: float = 30.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "git not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "git timed out"}
    return {"ok": proc.returncode == 0, "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip()}


def normalize_remote_to_owner_name(remote: str) -> str:
    text = str(remote or "").strip()
    if not text:
        raise ValueError("empty remote")
    # git@github.com:owner/name.git or https://github.com/owner/name.git
    text = text.removesuffix(".git")
    if "github.com:" in text:
        text = text.split("github.com:", 1)[1]
    elif "github.com/" in text:
        text = text.split("github.com/", 1)[1]
    text = text.strip("/")
    return canonicalize_repository(text)


def resolve_registered_repository(
    store: Any,
    *,
    project: dict[str, Any],
    repository_id: str | None = None,
) -> dict[str, Any]:
    """Resolve local root + remote identity from project registration only."""
    expected = canonicalize_repository(str(project.get("repository") or ""))
    # Optional registered path from project metadata (data-driven, not request path)
    meta = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
    registered_root = (
        project.get("local_repository_root")
        or meta.get("local_repository_root")
        or (store.get_repository_binding(expected) if hasattr(store, "get_repository_binding") else None)
    )
    if isinstance(registered_root, dict):
        registered_root = registered_root.get("local_path")

    if not registered_root:
        # Fallback: only allow exact repo name match under a configured workspace roots list
        roots = []
        if hasattr(store, "list_repository_bindings"):
            roots = store.list_repository_bindings()
        for item in roots:
            if normalize_repo_for_compare(str(item.get("repository") or "")) == normalize_repo_for_compare(expected):
                registered_root = item.get("local_path")
                break

    if not registered_root:
        raise ValueError(
            f"no registered local_repository_root for {expected}; "
            "register repository binding before live supervised execution"
        )

    root = Path(str(registered_root)).resolve()
    # Path traversal / safety
    if ".." in Path(str(registered_root)).parts:
        raise ValueError("repository path must not contain ..")
    if not root.exists() or not root.is_dir():
        raise ValueError(f"registered repository path does not exist: {root}")
    if root.is_symlink():
        # resolve already followed; record and continue if still a git repo
        pass

    git_dir = _git(["rev-parse", "--is-inside-work-tree"], cwd=root)
    if not git_dir.get("ok") or git_dir.get("stdout") != "true":
        raise ValueError(f"path is not a git work tree: {root}")

    toplevel = _git(["rev-parse", "--show-toplevel"], cwd=root)
    if not toplevel.get("ok"):
        raise ValueError("cannot resolve git toplevel")
    root = Path(toplevel["stdout"]).resolve()

    remote = _git(["config", "--get", "remote.origin.url"], cwd=root)
    if not remote.get("ok") or not remote.get("stdout"):
        raise ValueError("repository missing remote.origin.url")
    try:
        remote_id = normalize_remote_to_owner_name(remote["stdout"])
    except ValueError as exc:
        raise ValueError(f"cannot parse remote.origin.url: {exc}") from exc

    if normalize_repo_for_compare(remote_id) != normalize_repo_for_compare(expected):
        raise ValueError(
            f"repository mismatch: project expects {expected}, local remote is {remote_id}"
        )

    return {
        "repository": expected,
        "remote_url_redacted": remote_id,  # owner/name only, not credentials
        "local_path": str(root),
        "project_id": project.get("id"),
        "repository_id": repository_id or expected,
        "bound_at": utc_now_iso(),
        "match": True,
    }


def pin_baseline(
    repo_root: Path,
    *,
    baseline_ref: str = "HEAD",
) -> dict[str, Any]:
    """Resolve exact baseline SHA before approval."""
    root = Path(repo_root).resolve()
    ref = str(baseline_ref or "HEAD").strip() or "HEAD"
    if ".." in ref or ref.startswith("-"):
        raise ValueError("invalid baseline ref")
    res = _git(["rev-parse", ref], cwd=root)
    if not res.get("ok") or not re.fullmatch(r"[0-9a-f]{7,40}", res.get("stdout") or ""):
        raise ValueError(f"cannot resolve baseline ref {ref}: {res.get('stderr')}")
    sha = res["stdout"]
    # Prove object exists
    exists = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=root)
    if not exists.get("ok"):
        # some git versions
        exists2 = _git(["cat-file", "-t", sha], cwd=root)
        if not exists2.get("ok") or "commit" not in (exists2.get("stdout") or ""):
            raise ValueError(f"baseline commit object missing: {sha}")
    return {
        "baseline_ref": ref,
        "baseline_commit": sha,
        "pinned_at": utc_now_iso(),
    }


def assert_worktree_matches_baseline(worktree_path: Path, baseline_commit: str) -> None:
    head = _git(["rev-parse", "HEAD"], cwd=Path(worktree_path))
    if not head.get("ok") or head.get("stdout") != baseline_commit:
        raise ValueError(
            f"worktree HEAD {head.get('stdout')!r} does not match approved baseline {baseline_commit!r}"
        )
