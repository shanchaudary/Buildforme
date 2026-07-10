"""Canonical changed-file manifest — fail-closed on any required Git source failure."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from buildforme.redaction import redact_hash, redact_text
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
        return {
            "ok": False,
            "stdout": "",
            "stderr": "git not found",
            "exit_code": 127,
            "command": ["git", *args],
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "git timed out",
            "exit_code": 124,
            "command": ["git", *args],
        }
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "command": ["git", *args],
    }


def _record_cmd(result: dict[str, Any], purpose: str) -> dict[str, Any]:
    return {
        "command": result.get("command"),
        "exit_code": result.get("exit_code"),
        "stdout_hash": redact_hash(result.get("stdout") or ""),
        "stderr_preview": redact_text((result.get("stderr") or "")[:400]),
        "success": bool(result.get("ok")),
        "purpose": purpose,
    }


def collect_changed_file_manifest(
    worktree_path: str | Path,
    *,
    baseline_commit: str | None = None,
) -> dict[str, Any]:
    """Build one canonical manifest. complete=false if any required source fails."""
    root = Path(worktree_path).resolve()
    sources: list[dict[str, Any]] = []
    blocking: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}

    def fail(purpose: str, detail: str) -> None:
        blocking.append(f"{purpose}: {detail}")

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

    if not root.is_dir():
        return {
            "schema": "buildforme.changed_files.v1",
            "worktree_path": str(root),
            "baseline_commit": baseline_commit,
            "collected_at": utc_now_iso(),
            "files": [],
            "files_changed": [],
            "file_count": 0,
            "diff_stat": "",
            "complete": False,
            "blocking_reasons": [f"worktree path missing: {root}"],
            "sources": sources,
        }

    # HEAD resolution (required)
    head = _git(["rev-parse", "HEAD"], cwd=root)
    sources.append(_record_cmd(head, "HEAD resolution"))
    if not head["ok"] or not re.fullmatch(r"[0-9a-f]{7,40}", (head.get("stdout") or "").strip()):
        fail("HEAD resolution", head.get("stderr") or "failed")

    # Baseline object validation (required when provided)
    if baseline_commit:
        if not re.fullmatch(r"[0-9a-f]{7,40}", str(baseline_commit)):
            fail("baseline commit object validation", "invalid SHA format")
        else:
            base_obj = _git(["cat-file", "-t", str(baseline_commit)], cwd=root)
            sources.append(_record_cmd(base_obj, "baseline commit object validation"))
            if not base_obj["ok"] or "commit" not in (base_obj.get("stdout") or ""):
                fail("baseline commit object validation", base_obj.get("stderr") or "missing object")

    # Required sources
    status = _git(["status", "--porcelain=v2", "--untracked-files=all"], cwd=root)
    sources.append(_record_cmd(status, "git status --porcelain=v2"))
    if not status["ok"]:
        fail("git status", status.get("stderr") or f"exit {status.get('exit_code')}")

    others = _git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    sources.append(_record_cmd(others, "git ls-files --others --exclude-standard"))
    if not others["ok"]:
        fail("ls-files others", others.get("stderr") or "failed")

    ignored = _git(["ls-files", "--others", "-i", "--exclude-standard"], cwd=root)
    sources.append(_record_cmd(ignored, "git ls-files --others -i --exclude-standard"))
    if not ignored["ok"]:
        fail("ls-files ignored", ignored.get("stderr") or "failed")

    wt_diff = _git(["diff", "--name-status"], cwd=root)
    sources.append(_record_cmd(wt_diff, "git diff --name-status"))
    if not wt_diff["ok"] and wt_diff.get("exit_code") not in (0, 1):
        # git diff returns 1 when differences exist with some configs; treat only hard errors
        if wt_diff.get("exit_code") not in (0, 1):
            fail("git diff", wt_diff.get("stderr") or "failed")
    # name-status with differences still exit 0 typically

    if baseline_commit and re.fullmatch(r"[0-9a-f]{7,40}", str(baseline_commit)):
        ns = _git(["diff", "--name-status", f"{baseline_commit}...HEAD"], cwd=root)
        sources.append(_record_cmd(ns, "git diff --name-status baseline...HEAD"))
        if not ns["ok"] and ns.get("exit_code") not in (0, 1):
            fail("diff baseline...HEAD", ns.get("stderr") or "failed")
        else:
            _parse_name_status(ns.get("stdout") or "", upsert, staged=False, vs_baseline=True)

        cached = _git(["diff", "--cached", "--name-status", str(baseline_commit)], cwd=root)
        sources.append(_record_cmd(cached, "git diff --cached --name-status baseline"))
        if not cached["ok"] and cached.get("exit_code") not in (0, 1):
            fail("diff cached baseline", cached.get("stderr") or "failed")
        else:
            _parse_name_status(cached.get("stdout") or "", upsert, staged=True, vs_baseline=True)
    else:
        if baseline_commit:
            fail("baseline commit", "missing or invalid baseline for required diffs")
        ns = _git(["diff", "--name-status", "HEAD"], cwd=root)
        sources.append(_record_cmd(ns, "git diff --name-status HEAD"))
        if not ns["ok"] and ns.get("exit_code") not in (0, 1):
            fail("diff HEAD", ns.get("stderr") or "failed")
        else:
            _parse_name_status(ns.get("stdout") or "", upsert, staged=False, vs_baseline=False)
        cached = _git(["diff", "--cached", "--name-status"], cwd=root)
        sources.append(_record_cmd(cached, "git diff --cached --name-status"))
        if not cached["ok"] and cached.get("exit_code") not in (0, 1):
            fail("diff cached", cached.get("stderr") or "failed")
        else:
            _parse_name_status(cached.get("stdout") or "", upsert, staged=True, vs_baseline=False)

    # Parse successful sources into paths
    if status.get("ok"):
        for line in (status.get("stdout") or "").splitlines():
            _parse_porcelain_v2_line(line, upsert)
    if others.get("ok"):
        for line in (others.get("stdout") or "").splitlines():
            p = line.strip()
            if p:
                upsert(
                    p,
                    change_type="added",
                    tracked=False,
                    untracked=True,
                    unstaged=True,
                    baseline_exists=False,
                    current_exists=True,
                )
    if ignored.get("ok"):
        for line in (ignored.get("stdout") or "").splitlines():
            p = line.strip()
            if p:
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
    if wt_diff.get("ok") or wt_diff.get("exit_code") in (0, 1):
        _parse_name_status(wt_diff.get("stdout") or "", upsert, staged=False, vs_baseline=False)

    # Enrich filesystem facts only if complete so far
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
                    if size <= 256_000:
                        try:
                            rec["content_hash"] = hashlib.sha256(full.read_bytes()).hexdigest()
                        except OSError:
                            pass
            except OSError:
                pass

    complete = len(blocking) == 0
    files = [by_path[k] for k in sorted(by_path.keys())] if complete else []
    paths = [f["path"] for f in files]
    stat_parts = [f"{f.get('change_type', '?')}\t{f['path']}" for f in files]
    raw = "\n".join(f"{f.get('change_type')}:{f['path']}:{f.get('content_hash') or ''}" for f in files)
    return {
        "schema": "buildforme.changed_files.v1",
        "worktree_path": str(root),
        "baseline_commit": baseline_commit,
        "head_commit": (head.get("stdout") or "").strip() if head.get("ok") else None,
        "collected_at": utc_now_iso(),
        "files": files,
        "files_changed": paths,
        "file_count": len(files),
        "diff_stat": "\n".join(stat_parts),
        "complete": complete,
        "blocking_reasons": blocking,
        "sources": sources,
        "manifest_fingerprint": hashlib.sha256(raw.encode("utf-8")).hexdigest() if complete else None,
    }


def collect_patch_evidence(
    worktree_path: str | Path,
    *,
    baseline_commit: str,
) -> dict[str, Any]:
    """Canonical patch evidence distinct from manifest fingerprint."""
    root = Path(worktree_path).resolve()
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    blocking: list[str] = []

    def run(args: list[str], purpose: str) -> str:
        res = _git(args, cwd=root)
        sources.append(_record_cmd(res, purpose))
        if not res["ok"] and res.get("exit_code") not in (0, 1):
            blocking.append(f"{purpose}: {res.get('stderr') or res.get('exit_code')}")
            return ""
        return res.get("stdout") or ""

    committed = run(["diff", f"{baseline_commit}...HEAD"], "committed baseline...HEAD patch")
    staged = run(["diff", "--cached", baseline_commit], "staged vs baseline patch")
    unstaged = run(["diff"], "unstaged patch")
    parts.extend([committed, staged, unstaged])

    # Untracked content hashes
    others = _git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    sources.append(_record_cmd(others, "untracked for patch"))
    ignored = _git(["ls-files", "--others", "-i", "--exclude-standard"], cwd=root)
    sources.append(_record_cmd(ignored, "ignored untracked for patch"))
    untracked_lines = []
    for block in (others.get("stdout") or "", ignored.get("stdout") or ""):
        for line in block.splitlines():
            p = line.strip()
            if not p:
                continue
            full = root / p
            if full.is_file() and not full.is_symlink():
                try:
                    data = full.read_bytes()
                    untracked_lines.append(f"UNTRACKED {p} size={len(data)} sha256={hashlib.sha256(data).hexdigest()}")
                except OSError as exc:
                    blocking.append(f"untracked read {p}: {exc}")
            else:
                untracked_lines.append(f"UNTRACKED {p} special")
    parts.append("\n".join(untracked_lines))
    blob = "\n---\n".join(parts)
    complete = len(blocking) == 0
    return {
        "complete": complete,
        "blocking_reasons": blocking,
        "sources": sources,
        "patch_fingerprint": hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest() if complete else None,
        "patch_size": len(blob),
        "baseline_commit": baseline_commit,
    }


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
            if len(parts) >= 3:
                old, new = parts[1], parts[2]
                upsert(new, change_type="renamed", staged=staged or None, unstaged=(not staged) or None, tracked=True, renamed_from=old)
                upsert(old, change_type="deleted", tracked=True, current_exists=False)
            continue
        path = parts[1] if len(parts) > 1 else ""
        if not path:
            continue
        change = {"A": "added", "M": "modified", "D": "deleted", "T": "typechange", "U": "unmerged"}.get(code[:1], "modified")
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
        parts = line.split(" ")
        if len(parts) < 9:
            return
        xy = parts[1]
        rest = line.split("\t")
        if len(rest) >= 2:
            path_field = rest[-1].strip()
            if " -> " in path_field:
                old, new = path_field.split(" -> ", 1)
                upsert(new, change_type="renamed", tracked=True, renamed_from=old)
                upsert(old, change_type="deleted", tracked=True)
            else:
                staged = xy[0] not in {".", " "}
                unstaged = xy[1] not in {".", " "} if len(xy) > 1 else False
                change = "modified"
                if "A" in xy:
                    change = "added"
                if "D" in xy:
                    change = "deleted"
                upsert(path_field, change_type=change, tracked=True, staged=staged, unstaged=unstaged)
    elif line.startswith("? "):
        path = line[2:].strip()
        upsert(path, change_type="added", tracked=False, untracked=True, unstaged=True, baseline_exists=False)
