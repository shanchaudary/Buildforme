"""Canonical changed-file manifest for Stage 6 verification and evidence."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from buildforme.storage import utc_now_iso


def _git(args: list[str], *, cwd: Path, timeout: float = 60.0) -> dict[str, Any]:
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
        return {"ok": False, "stdout": "", "stderr": "git not found", "exit_code": 127}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "git timed out", "exit_code": 124}
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
    }


def collect_changed_file_manifest(
    worktree_path: str | Path,
    *,
    baseline_commit: str | None = None,
) -> dict[str, Any]:
    """Build one canonical manifest of all repository mutations.

    Includes committed-vs-baseline, staged, unstaged, untracked, deleted, renamed.
    """
    root = Path(worktree_path).resolve()
    if not root.is_dir():
        raise ValueError(f"worktree path missing: {root}")

    by_path: dict[str, dict[str, Any]] = {}

    def upsert(path: str, **fields: Any) -> None:
        path = path.replace("\\", "/").strip()
        if not path or path == ".":
            return
        rec = by_path.setdefault(
            path,
            {
                "path": path,
                "change_type": "unknown",
                "tracked": None,
                "staged": False,
                "unstaged": False,
                "untracked": False,
                "baseline_exists": None,
                "current_exists": None,
                "is_symlink": False,
                "size": None,
                "content_hash": None,
            },
        )
        for key, value in fields.items():
            if value is not None:
                rec[key] = value

    # Untracked not ignored
    others = _git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    for line in (others.get("stdout") or "").splitlines():
        p = line.strip()
        if not p:
            continue
        upsert(
            p,
            change_type="added",
            tracked=False,
            untracked=True,
            unstaged=True,
            baseline_exists=False,
            current_exists=True,
        )
    # Ignored untracked (e.g. .env) — must still be visible to security gates
    ignored = _git(["ls-files", "--others", "-i", "--exclude-standard"], cwd=root)
    for line in (ignored.get("stdout") or "").splitlines():
        p = line.strip()
        if not p:
            continue
        upsert(
            p,
            change_type="added",
            tracked=False,
            untracked=True,
            unstaged=True,
            baseline_exists=False,
            current_exists=True,
            ignored=True,
        )

    # Status porcelain v2 — rich machine output
    status = _git(["status", "--porcelain=v2", "--untracked-files=all"], cwd=root)
    for line in (status.get("stdout") or "").splitlines():
        _parse_porcelain_v2_line(line, upsert)

    # Diff name-status vs baseline (committed divergence)
    if baseline_commit and re.fullmatch(r"[0-9a-f]{7,40}", baseline_commit):
        ns = _git(["diff", "--name-status", f"{baseline_commit}...HEAD"], cwd=root)
        _parse_name_status(ns.get("stdout") or "", upsert, staged=False, vs_baseline=True)
        cached = _git(["diff", "--cached", "--name-status", baseline_commit], cwd=root)
        _parse_name_status(cached.get("stdout") or "", upsert, staged=True, vs_baseline=True)
    else:
        ns = _git(["diff", "--name-status", "HEAD"], cwd=root)
        _parse_name_status(ns.get("stdout") or "", upsert, staged=False, vs_baseline=False)
        cached = _git(["diff", "--cached", "--name-status"], cwd=root)
        _parse_name_status(cached.get("stdout") or "", upsert, staged=True, vs_baseline=False)

    # Unstaged working tree vs index
    wt = _git(["diff", "--name-status"], cwd=root)
    _parse_name_status(wt.get("stdout") or "", upsert, staged=False, vs_baseline=False)

    # Enrich filesystem facts
    for path, rec in list(by_path.items()):
        full = root / path
        exists = full.exists() or full.is_symlink()
        rec["current_exists"] = exists
        if exists:
            try:
                rec["is_symlink"] = full.is_symlink()
                if full.is_symlink():
                    try:
                        target = full.resolve()
                        rec["symlink_target"] = str(target)
                        rec["symlink_escapes"] = not str(target).startswith(str(root))
                    except OSError:
                        rec["symlink_escapes"] = True
                elif full.is_file():
                    size = full.stat().st_size
                    rec["size"] = size
                    # Hash small text-ish files only
                    if size <= 256_000 and not rec.get("is_symlink"):
                        try:
                            data = full.read_bytes()
                            rec["content_hash"] = hashlib.sha256(data).hexdigest()
                        except OSError:
                            pass
            except OSError:
                pass
        if baseline_commit and rec.get("baseline_exists") is None:
            show = _git(["cat-file", "-e", f"{baseline_commit}:{path}"], cwd=root)
            rec["baseline_exists"] = bool(show.get("ok"))

    files = [by_path[k] for k in sorted(by_path.keys())]
    paths = [f["path"] for f in files]
    stat_parts = []
    for f in files:
        stat_parts.append(f"{f.get('change_type', '?')}\t{f['path']}")
    manifest = {
        "schema": "buildforme.changed_files.v1",
        "worktree_path": str(root),
        "baseline_commit": baseline_commit,
        "collected_at": utc_now_iso(),
        "files": files,
        "files_changed": paths,
        "file_count": len(files),
        "diff_stat": "\n".join(stat_parts),
        "complete": True,
        "sources": [
            "status --porcelain=v2 --untracked-files=all",
            "diff --name-status",
            "diff --cached --name-status",
            "ls-files --others --exclude-standard",
        ],
    }
    raw = "\n".join(f"{f.get('change_type')}:{f['path']}:{f.get('content_hash') or ''}" for f in files)
    manifest["manifest_fingerprint"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return manifest


def _parse_name_status(text: str, upsert: Any, *, staged: bool, vs_baseline: bool) -> None:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        code = parts[0]
        if code.startswith("R") or code.startswith("C"):
            # rename/copy: R100 old new
            if len(parts) >= 3:
                old, new = parts[1], parts[2]
                upsert(
                    new,
                    change_type="renamed",
                    staged=staged or None,
                    unstaged=(not staged) or None,
                    tracked=True,
                    renamed_from=old,
                )
                upsert(old, change_type="deleted", tracked=True, current_exists=False)
            continue
        path = parts[1] if len(parts) > 1 else ""
        if not path:
            continue
        change = {
            "A": "added",
            "M": "modified",
            "D": "deleted",
            "T": "typechange",
            "U": "unmerged",
        }.get(code[:1], "modified")
        upsert(
            path,
            change_type=change,
            staged=True if staged else None,
            unstaged=True if not staged else None,
            tracked=True,
            baseline_exists=False if change == "added" else None,
            current_exists=False if change == "deleted" else None,
        )


def _parse_porcelain_v2_line(line: str, upsert: Any) -> None:
    if not line:
        return
    if line.startswith("1 ") or line.startswith("2 "):
        # ordinary or rename
        parts = line.split(" ")
        if len(parts) < 9:
            return
        xy = parts[1]
        # path is last field (may include rename arrow)
        rest = line.split("\t")
        if len(rest) >= 2:
            path_field = rest[-1].strip()
            if " -> " in path_field:
                old, new = path_field.split(" -> ", 1)
                upsert(new, change_type="renamed", tracked=True, renamed_from=old, staged="R" in xy or "C" in xy)
                upsert(old, change_type="deleted", tracked=True)
            else:
                staged = xy[0] not in {".", " "}
                unstaged = xy[1] not in {".", " "} if len(xy) > 1 else False
                change = "modified"
                if "A" in xy:
                    change = "added"
                if "D" in xy:
                    change = "deleted"
                upsert(
                    path_field,
                    change_type=change,
                    tracked=True,
                    staged=staged,
                    unstaged=unstaged,
                )
    elif line.startswith("? "):
        path = line[2:].strip()
        upsert(path, change_type="added", tracked=False, untracked=True, unstaged=True, baseline_exists=False)
    elif line.startswith("! "):
        # ignored — skip
        return
