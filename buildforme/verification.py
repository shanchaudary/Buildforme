"""Deterministic independent verification of supervised run results."""

from __future__ import annotations

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from buildforme.storage import utc_now_iso
from buildforme.verification_profile import profile_from_project
from buildforme.worktree import collect_diff, worktree_status

SECRET_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)secret\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"(?i)password\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?i)ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)xox[baprs]-[A-Za-z0-9-]{10,}"),
]

FAKE_SUCCESS_MARKERS = [
    re.compile(r"(?i)fake\s+success"),
    re.compile(r"(?i)tests?\s+skipped\s+to\s+pass"),
    re.compile(r"(?i)TODO:\s*implement\s+later"),
]

UNWIRED_MARKERS = [
    re.compile(r"(?i)not\s+implemented"),
    re.compile(r"(?i)raise\s+NotImplementedError"),
    re.compile(r"(?i)pass\s*#\s*stub"),
]


def verify_run_result(
    *,
    run: dict[str, Any],
    packet: dict[str, Any] | None,
    project: dict[str, Any] | None,
    worktree_path: str | Path | None,
    baseline_commit: str | None,
    process_result: dict[str, Any] | None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Independently inspect repo state and run configured checks."""
    packet = packet or {}
    process_result = process_result or {}
    budget = budget or run.get("budget") or {}
    profile = profile_from_project(project)
    checks: list[dict[str, Any]] = []
    blocking: list[str] = []
    warnings: list[str] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})
        if status == "fail":
            blocking.append(f"{name}: {detail}")
        elif status == "warning":
            warnings.append(f"{name}: {detail}")

    # Process outcome
    if process_result.get("cancelled"):
        add("process_exit", "fail", "process was cancelled")
    elif process_result.get("timed_out"):
        add("process_exit", "fail", "process timed out")
    elif process_result.get("exit_code") not in (0, None) and process_result.get("exit_code") is not None:
        add("process_exit", "fail", f"nonzero exit {process_result.get('exit_code')}")
    elif process_result.get("exit_code") == 0:
        add("process_exit", "pass", "exit 0")
    else:
        add("process_exit", "warning", "no process result")

    # Worktree / branch integrity
    wt = Path(worktree_path) if worktree_path else None
    if not wt or not wt.exists():
        add("worktree_exists", "fail", "worktree path missing")
        return _result(checks, blocking, warnings, {}, profile)

    add("worktree_exists", "pass", str(wt))
    st = worktree_status(wt)
    branch = str(st.get("branch") or "")
    expected_branch = str(run.get("target_branch") or "")
    if expected_branch and branch and branch != expected_branch:
        add("branch_integrity", "fail", f"expected {expected_branch}, got {branch}")
    elif branch in {"main", "master"}:
        add("branch_integrity", "fail", "worktree on protected branch")
    else:
        add("branch_integrity", "pass", branch or "unknown")

    if baseline_commit and st.get("head_commit"):
        add("baseline_recorded", "pass", f"baseline={baseline_commit[:12]} head={str(st['head_commit'])[:12]}")
    else:
        add("baseline_recorded", "warning", "baseline/head incomplete")

    diff = collect_diff(wt, baseline_commit=baseline_commit)
    files = list(diff.get("files_changed") or [])
    # Also dirty unstaged already included in collect_diff

    # Provider claimed completion without diff
    claims_complete = bool(run.get("provider_claims_complete") or process_result.get("claims_complete"))
    if claims_complete and not files and process_result.get("exit_code") == 0:
        add("completion_without_diff", "fail", "provider claims complete but no files changed")
    else:
        add("completion_without_diff", "pass", f"files_changed={len(files)}")

    # Scope / path policy
    allowed = list(packet.get("allowed_files") or ["**"])
    forbidden = list(packet.get("forbidden_files") or profile.get("forbidden_paths") or [])
    for path in files:
        if _matches_any(path, forbidden):
            add("forbidden_path", "fail", f"changed forbidden path: {path}")
        elif allowed and allowed != ["**"] and not _matches_any(path, allowed):
            add("allowed_path", "fail", f"changed path outside allowed globs: {path}")
    if not any(c["name"] == "forbidden_path" and c["status"] == "fail" for c in checks):
        add("forbidden_path", "pass", "no forbidden path hits")
    if not any(c["name"] == "allowed_path" and c["status"] == "fail" for c in checks):
        add("allowed_path", "pass", "paths within scope or unrestricted")

    # Diff limits / budget
    max_files = int(budget.get("max_files_changed") or 50)
    if len(files) > max_files:
        add("diff_budget", "fail", f"{len(files)} files > max {max_files}")
    else:
        add("diff_budget", "pass", f"{len(files)}/{max_files} files")

    # Secret detection in changed files (bounded)
    secret_hits = []
    for rel in files[:80]:
        full = wt / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                secret_hits.append(rel)
                break
        for pat in FAKE_SUCCESS_MARKERS:
            if pat.search(text):
                add("fake_success_marker", "fail", f"{rel} matched fake-success marker")
                break
        for pat in UNWIRED_MARKERS:
            if pat.search(text) and "test" not in rel.lower():
                add("unwired_marker", "warning", f"{rel} may contain stub/unwired code")
                break
    if secret_hits:
        add("secret_detection", "fail", f"possible secrets in: {', '.join(secret_hits[:5])}")
    else:
        add("secret_detection", "pass", "no secret patterns in sampled changed files")

    # Symlink escape (worktree-local)
    for rel in files[:80]:
        full = wt / rel
        try:
            if full.is_symlink():
                target = full.resolve()
                if not str(target).startswith(str(wt.resolve())):
                    add("symlink_escape", "fail", f"symlink escapes worktree: {rel}")
        except OSError:
            pass
    if not any(c["name"] == "symlink_escape" and c["status"] == "fail" for c in checks):
        add("symlink_escape", "pass", "no symlink escape detected")

    # Dependency change awareness
    dep_files = [f for f in files if Path(f).name in {
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock", "go.mod", "go.sum",
        "Cargo.toml", "Cargo.lock",
    }]
    if dep_files:
        add("dependency_changes", "warning", f"dependency manifests changed: {', '.join(dep_files)}")
    else:
        add("dependency_changes", "pass", "no dependency manifests changed")

    # Run repository verification commands from profile (generic)
    test_cmd = profile.get("test_command")
    test_result = None
    if test_cmd:
        test_result = _run_command(list(test_cmd), cwd=wt, timeout=min(600, int(run.get("timeout_minutes") or 30) * 60))
        if test_result["ok"]:
            add("tests", "pass", "test_command exited 0")
        else:
            add("tests", "fail", f"test_command failed exit={test_result.get('exit_code')}")
    else:
        add("tests", "warning", "no test_command in verification profile")

    for label, key in (
        ("build", "build_command"),
        ("lint", "lint_command"),
        ("typecheck", "typecheck_command"),
    ):
        cmd = profile.get(key)
        if not cmd:
            add(label, "pass", f"no {key} configured")
            continue
        res = _run_command(list(cmd), cwd=wt, timeout=300)
        if res["ok"]:
            add(label, "pass", f"{key} exited 0")
        else:
            add(label, "fail", f"{key} failed exit={res.get('exit_code')}")

    # Provider refusal / malformed heuristics
    stderr = str(process_result.get("stderr") or "")
    stdout = str(process_result.get("stdout") or "")
    combined = (stdout + "\n" + stderr).lower()
    if "refus" in combined or "i can't help with that" in combined:
        add("provider_refusal", "fail", "provider refusal detected in output")
    else:
        add("provider_refusal", "pass", "no refusal markers")

    if process_result.get("truncated_stdout") or process_result.get("truncated_stderr"):
        add("output_truncation", "warning", "process output hit capture limits (marked, not silent)")

    return _result(checks, blocking, warnings, diff, profile, test_result=test_result)


def _result(
    checks: list[dict[str, Any]],
    blocking: list[str],
    warnings: list[str],
    diff: dict[str, Any],
    profile: dict[str, Any],
    test_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    passed = not blocking
    return {
        "passed": passed,
        "checks": checks,
        "blocking_reasons": blocking,
        "warnings": warnings,
        "diff": diff,
        "profile_id": profile.get("profile_id"),
        "test_result": test_result,
        "verified_at": utc_now_iso(),
        "independent_of_provider_claims": True,
    }


def _matches_any(path: str, globs: list[str]) -> bool:
    path = path.replace("\\", "/")
    for g in globs:
        g = str(g).replace("\\", "/")
        if g == "**" or g == "/**":
            return True
        if fnmatch(path, g) or fnmatch(path, g.lstrip("/")):
            return True
        # prefix directory style "docs/**"
        if g.endswith("/**") and path.startswith(g[:-3]):
            return True
    return False


def _run_command(argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    if not argv:
        return {"ok": False, "exit_code": None, "error": "empty command"}
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout_preview": (proc.stdout or "")[:2000],
            "stderr_preview": (proc.stderr or "")[:2000],
            "argv": argv,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": 124, "error": "timeout", "argv": argv}
    except OSError as exc:
        return {"ok": False, "exit_code": None, "error": str(exc), "argv": argv}
