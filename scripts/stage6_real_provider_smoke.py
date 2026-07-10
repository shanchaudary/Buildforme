"""Real provider smoke for Stage 6 — disposable local repo only.

Does not modify Buildforme main. Does not merge. Uses installed Codex when live_ready.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# repo root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildforme.execution_service import (  # noqa: E402
    create_run,
    execute_supervised,
    founder_review_decision,
    record_run_approval,
    run_preflight,
)
from buildforme.provider_discovery import health_check_provider  # noqa: E402
from buildforme.storage import LocalStore  # noqa: E402
from governance.constitution_engine import get_engine  # noqa: E402


def main() -> int:
    os.environ["BUILDFORME_ALLOW_DIRTY_PARENT"] = "1"
    td = tempfile.mkdtemp(prefix="bf-smoke-")
    root = Path(td) / "disposable"
    root.mkdir()
    _git(root, ["init"])
    _git(root, ["config", "user.email", "smoke@buildforme.local"])
    _git(root, ["config", "user.name", "smoke"])
    (root / "README.md").write_text("# smoke\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    _git(root, ["add", "."])
    _git(root, ["commit", "-m", "init"])
    _git(root, ["remote", "add", "origin", "https://github.com/example/disposable-smoke.git"])
    base = _git_out(root, ["rev-parse", "HEAD"]).strip()
    print("REPO", root)
    print("BASE", base)

    store = LocalStore(Path(td) / "state.json")
    sample = json.loads((ROOT / "data" / "sample_project.json").read_text(encoding="utf-8"))
    sample["project"]["id"] = "smokeproj"
    sample["project"]["repository"] = "example/disposable-smoke"
    store.load_sample_project(sample, replace=True)
    store.set_project_execution_control("smokeproj", execution_status="enabled", reason="smoke")
    proj = store.get_project("smokeproj")
    proj["verification_profile"] = {
        "profile_id": "smoke",
        "test_command": ["python", "-c", "print('ok')"],
        "lint_command": None,
        "build_command": None,
        "forbidden_paths": [".env", "secrets/**"],
        "protected_branches": ["main", "master"],
    }
    store.upsert_project(proj)
    store.register_repository_binding(
        {
            "repository": "example/disposable-smoke",
            "local_path": str(root),
            "project_id": "smokeproj",
        }
    )
    engine = get_engine(force_reload=True)
    for p in store.list_providers():
        if p.get("provider_id") == "codex":
            r = engine.acknowledge_provider(p, actor="shan")
            store.set_provider_constitution_ack(
                "codex",
                {
                    "constitution_supported": True,
                    "constitution_acknowledged": True,
                    "constitution_version": r["constitution_version"],
                    "constitution_hash": r["constitution_hash"],
                    "constitution_last_refresh": r.get("constitution_last_refresh"),
                    "constitution_acknowledged_at": r.get("constitution_acknowledged_at"),
                    "constitution_ack_actor": "shan",
                },
            )
    health = health_check_provider(
        "codex", store.get_provider_record("codex"), force_compat=True
    )
    print(
        "HEALTH",
        json.dumps(
            {
                k: health.get(k)
                for k in (
                    "available",
                    "live_ready",
                    "version",
                    "executable",
                    "unsupported_reasons",
                    "compatibility",
                    "auth",
                )
            },
            indent=2,
        ),
    )
    if not health.get("live_ready"):
        print("SMOKE_ABORT not live_ready")
        return 2

    packet = {
        "id": "pkt-smoke-real",
        "objective": (
            "Create a small deterministic function add_one(x) in src/math_util.py that returns x+1. "
            "Add unit test tests/test_math_util.py. Update README with one usage example. "
            "Stay in allowed paths only. Do not merge or deploy."
        ),
        "acceptance_criteria": [
            "src/math_util.py defines add_one(x) returning x+1",
            "tests/test_math_util.py covers add_one",
            "README.md includes one usage example",
        ],
        "allowed_files": ["src/**", "tests/**", "README.md"],
        "forbidden_files": [".env", "secrets/**", ".git/**"],
        "risk": "YELLOW",
        "operating_mode": "IMPLEMENTATION",
        "target_repository": "example/disposable-smoke",
        "target_branch": "feature/smoke-real",
    }
    run = create_run(
        store,
        {
            "project_id": "smokeproj",
            "provider_id": "codex",
            "packet": packet,
            "packet_id": packet["id"],
            "target_branch": "feature/smoke-real",
            "risk": "YELLOW",
            "execution_mode": "live_supervised",
            "requested_capabilities": [
                "read_repository",
                "edit_repository",
                "run_tests",
                "produce_patch",
            ],
            "timeout_minutes": 10,
        },
    )
    print(
        "RUN",
        run["id"],
        "baseline",
        run.get("baseline_commit"),
        "branch",
        run.get("execution_branch"),
    )
    pre = run_preflight(store, run["id"])
    print(
        "PREFLIGHT",
        pre["preflight"].get("passed"),
        pre["preflight"].get("blocking_reasons"),
    )
    if not pre["preflight"].get("passed"):
        return 3
    run2 = store.get_run(run["id"])
    for req in run2.get("approval_requirements") or ["shan_task_approval"]:
        record_run_approval(
            store, run["id"], requirement_type=req, decision="approved", actor="shan"
        )
    print("STATUS", store.get_run(run["id"])["status"])
    print("EXECUTING real codex...")
    result = execute_supervised(store, run["id"])
    process = result.get("process") or {}
    evidence = result.get("evidence") or {}
    verification = result.get("verification") or {}
    review = result.get("review") or {}
    print("STATUS_AFTER", result["run"].get("status"))
    print("PROCESS_OK", process.get("ok"), "exit", process.get("exit_code"))
    print("PROCESS_PID", process.get("pid"))
    print("ARGV", process.get("argv"))
    print("VERIF", verification.get("passed"), verification.get("blocking_reasons"))
    print("FILES", evidence.get("files_changed"))
    print("PATCH_FP", evidence.get("patch_fingerprint"))
    print("EVIDENCE_ID", evidence.get("evidence_id"))
    print(
        "HEAD",
        evidence.get("final_head_sha") or evidence.get("post_run_head_sha"),
    )
    print("REVIEW_ACCEPT", review.get("accept_for_pr_prep_allowed"))
    if result["run"].get("status") == "needs_review" and review.get(
        "accept_for_pr_prep_allowed"
    ):
        d = founder_review_decision(
            store,
            run["id"],
            decision="accept_for_pr_prep",
            note="smoke accept",
            actor="shan",
        )
        print("FOUNDER_KEYS", list(d.keys()) if isinstance(d, dict) else type(d))
        if isinstance(d, dict):
            run_after = d.get("run") if isinstance(d.get("run"), dict) else {}
            print("FOUNDER_STATUS", run_after.get("status") or d.get("decision"))
    else:
        print("FOUNDER skipped; status=", result["run"].get("status"))
        if process.get("stderr"):
            print("STDERR_PREVIEW", str(process.get("stderr"))[:800])
        if process.get("stdout"):
            print("STDOUT_PREVIEW", str(process.get("stdout"))[:800])
    print("SMOKE_DIR", td)
    print("MERGE", "no")
    # Success criteria for smoke: real process + verification path completed.
    # File production is provider-dependent; report honestly.
    ok = result["run"].get("status") in {"needs_review", "completed"} and process.get("exit_code") == 0
    return 0 if ok else 4


def _git(cwd: Path, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(cwd: Path, args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True)


if __name__ == "__main__":
    raise SystemExit(main())
