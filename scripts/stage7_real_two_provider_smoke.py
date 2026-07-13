from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.review_execution import execute_independent_review_assignment
from buildforme.review_service import aggregate_independent_review_cycle, create_independent_review_cycle
from buildforme.stage7_smoke import evaluate_stage7_smoke
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine

AUTOMATION_ACTOR = "system"
REVIEWER_ACTOR = "reviewer"
HARNESS_ACTORS = frozenset({AUTOMATION_ACTOR, REVIEWER_ACTOR})


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
    return (proc.stdout or "").strip()


def main() -> int:
    smoke_root = Path(tempfile.mkdtemp(prefix="buildforme-stage7-real-smoke-"))
    repo = smoke_root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "stage7-smoke@buildforme.local")
    git(repo, "config", "user.name", "Buildforme Stage 7 Smoke")
    git(repo, "remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
    (repo / "README.md").write_text("# Stage 7 smoke fixture\n", encoding="utf-8")
    (repo / "math_util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature/stage7-real-review")
    (repo / "math_util.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_math_util.py").write_text(
        "import unittest\nimport math_util\n\nclass MathTests(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(math_util.add(2, 3), 5)\n"
        "    def test_subtract(self): self.assertEqual(math_util.subtract(5, 3), 2)\n\n"
        "if __name__ == '__main__': unittest.main()\n",
        encoding="utf-8",
    )
    verify = subprocess.run(
        ["python", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        raise RuntimeError(f"controlled fixture verification failed: {verify.stdout}\n{verify.stderr}")

    source_head_before = git(repo, "rev-parse", "HEAD")
    source_branch_before = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    source_patch_before = collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"]

    store = LocalStore(smoke_root / "runtime" / "state.json")
    store.upsert_project(
        {
            "id": "stage7-smoke",
            "name": "Stage 7 real reviewer smoke",
            "repository": "shanchaudary/Buildforme",
            "status": "active",
            "local_repository_root": str(repo),
        }
    )
    store.register_repository_binding(
        {
            "repository": "shanchaudary/Buildforme",
            "local_path": str(repo),
            "project_id": "stage7-smoke",
        }
    )
    engine = get_engine(force_reload=True)
    packet = engine.attach_to_packet(
        {
            "id": "pkt-stage7-real-smoke",
            "objective": "Independently review a small verified Python subtraction implementation.",
            "target_repository": "shanchaudary/Buildforme",
            "target_branch": "feature/stage7-real-review",
            "operating_mode": "REVIEW",
            "risk": "GREEN",
            "allowed_files": ["README.md", "math_util.py", "test_math_util.py"],
            "forbidden_files": [".env", "secrets/**"],
            "acceptance_criteria": [
                "subtract(5, 3) returns 2",
                "unit tests pass",
                "no file mutation during review",
            ],
        }
    )
    lease = engine.issue_run_lease(
        run_id="run-stage7-real-smoke",
        provider_id="glm",
        packet_id=packet["id"],
        actor=AUTOMATION_ACTOR,
    )
    store.save_constitution_lease(lease)
    run = {
        "id": "run-stage7-real-smoke",
        "project_id": "stage7-smoke",
        "task_id": "stage7-real-review",
        "packet_id": packet["id"],
        "packet": packet,
        "provider_id": "glm",
        "repository": "shanchaudary/Buildforme",
        "repository_local_path": str(repo),
        "baseline_ref": baseline,
        "baseline_commit": baseline,
        "requested_target_branch": "feature/stage7-real-review",
        "execution_branch": "feature/stage7-real-review",
        "target_branch": "feature/stage7-real-review",
        "operating_mode": "REVIEW",
        "risk": "GREEN",
        "status": "needs_review",
        "execution_mode": "live_supervised",
        "mode": "live_supervised",
        "transport": "controlled_fixture",
        "requested_capabilities": ["read_repository", "run_tests"],
        "attempt": 0,
        "max_attempts": 1,
        "timeout_minutes": 30,
        "budget": {"max_cost_usd": 0},
        "review": {"hard_blocks": []},
        "worktree_path": str(repo),
        "evidence_ids": [],
        "controlled_implementation_fixture": True,
    }
    run = engine.attach_to_run(run, lease=lease, actor=AUTOMATION_ACTOR)
    run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
    run = store.save_run_for_setup(run)
    manifest = collect_changed_file_manifest(repo, baseline_commit=baseline)
    patch = collect_patch_evidence(repo, baseline_commit=baseline)
    evidence = build_evidence_bundle(
        run=run,
        packet=packet,
        process_result={
            "ok": True,
            "exit_code": 0,
            "pid": 1,
            "stdout": verify.stdout,
            "stderr": verify.stderr,
            "cleanup_ok": True,
            "process_group_isolated": True,
            "argv": ["controlled-fixture-verification"],
        },
        worktree={
            "worktree_path": str(repo),
            "baseline_commit": baseline,
            "head_commit": baseline,
            "branch": "feature/stage7-real-review",
        },
        diff={"manifest": manifest, "patch_fingerprint": patch["patch_fingerprint"]},
        provider_health={"version": "controlled-fixture", "executable": "controlled-fixture"},
        verification={"passed": True, "blocking_reasons": [], "checks": [{"name": "unittest", "status": "pass"}]},
        constitution_result={"passed": True},
        approved_baseline_sha=baseline,
        final_head_sha=baseline,
        execution_branch="feature/stage7-real-review",
        patch_fingerprint=patch["patch_fingerprint"],
        manifest_fingerprint=manifest["manifest_fingerprint"],
    )
    evidence = store.save_run_evidence(evidence)
    for provider_id in ("codex", "claude"):
        store.set_provider_constitution_ack(
            provider_id,
            {
                "constitution_supported": True,
                "constitution_acknowledged": True,
                "constitution_version": engine.version(),
                "constitution_hash": engine.content_hash(),
                "constitution_last_refresh": "stage7-smoke",
                "constitution_acknowledged_at": "stage7-smoke",
                "constitution_ack_actor": AUTOMATION_ACTOR,
            },
        )
    created = create_independent_review_cycle(
        store,
        run["id"],
        reviewers=[
            {"reviewer_id": "codex-real-reviewer", "provider_id": "codex", "role": "correctness"},
            {"reviewer_id": "claude-real-reviewer", "provider_id": "claude", "role": "security"},
        ],
        actor=AUTOMATION_ACTOR,
    )
    attempts = []
    for assignment in created["assignments"]:
        execute_independent_review_assignment(
            store,
            created["cycle"]["cycle_id"],
            assignment["assignment_id"],
            actor=REVIEWER_ACTOR,
            timeout_seconds=900,
        )
        attempts.extend(store.list_review_execution_attempts(assignment["assignment_id"]))
    finalized = aggregate_independent_review_cycle(
        store, created["cycle"]["cycle_id"], actor=AUTOMATION_ACTOR
    )
    aggregate = finalized.get("aggregate") or {}
    finalized_cycle = store.get_review_cycle(created["cycle"]["cycle_id"])
    saved_run = store.get_run(run["id"])
    reports = store.list_review_reports(created["cycle"]["cycle_id"])
    merge_count_text = git(repo, "rev-list", "--count", "--merges", f"{baseline}..HEAD")
    observed = {
        "controlled_implementation_fixture": True,
        "implementer_provider_id": run["provider_id"],
        "review_execution_attempts": attempts,
        "persisted_report_count": len(reports),
        "persisted_report_fingerprints": [item.get("report_fingerprint") for item in reports],
        "aggregate_report_fingerprints": aggregate.get("report_fingerprints") or [],
        "cycle_id": finalized_cycle.get("cycle_id"),
        "cycle_evidence_id": finalized_cycle.get("evidence_id"),
        "cycle_evidence_fingerprint": finalized_cycle.get("evidence_fingerprint"),
        "expected_evidence_id": evidence.get("evidence_id"),
        "expected_evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "run_review_cycle_id": saved_run.get("stage7_review_cycle_id"),
        "distinct_provider_count": aggregate.get("distinct_provider_count"),
        "provider_ids": aggregate.get("provider_ids"),
        "aggregate_status": aggregate.get("status"),
        "verification_passed": True,
        "source_head_before": source_head_before,
        "source_head_after": git(repo, "rev-parse", "HEAD"),
        "source_branch_before": source_branch_before,
        "source_branch_after": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "source_patch_before": source_patch_before,
        "source_patch_after": collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"],
        "merge_commit_count": int(merge_count_text or "0"),
    }
    acceptance = evaluate_stage7_smoke(observed)
    print("STAGE7_SMOKE_DIR", smoke_root)
    print("STAGE7_SMOKE_ACCEPTANCE_JSON", json.dumps(acceptance, sort_keys=True))
    print("MERGE no")
    return 0 if acceptance["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
