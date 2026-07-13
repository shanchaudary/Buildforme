from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.repair_service import (
    admit_governed_repair_run,
    create_governed_repair_packet,
    create_repair_review_cycle,
)
from buildforme.review_execution import execute_independent_review_assignment
from buildforme.review_service import aggregate_independent_review_cycle, create_independent_review_cycle
from buildforme.stage7_smoke import evaluate_stage7_repair_smoke
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine

AUTOMATION_ACTOR = "system"
REVIEWER_ACTOR = "reviewer"
HARNESS_ACTORS = frozenset({AUTOMATION_ACTOR, REVIEWER_ACTOR})

SOURCE_AUTH_IMPLEMENTATION = (
    "def is_authorized(role):\n"
    "    return True  # BUG: guests receive admin authority\n"
)
SOURCE_TEST_SUITE = (
    "import unittest\n"
    "import auth\n\n"
    "class AuthTests(unittest.TestCase):\n"
    "    def test_admin_allowed(self): self.assertTrue(auth.is_authorized('admin'))\n\n"
    "if __name__ == '__main__': unittest.main()\n"
)
REPAIRED_AUTH_IMPLEMENTATION = (
    "def is_authorized(role):\n"
    "    return role == 'admin'\n"
)
REPAIRED_TEST_SUITE = (
    "import unittest\n"
    "import auth\n\n"
    "class AuthTests(unittest.TestCase):\n"
    "    def test_admin_allowed(self): self.assertTrue(auth.is_authorized('admin'))\n"
    "    def test_guest_rejected(self): self.assertFalse(auth.is_authorized('guest'))\n\n"
    "if __name__ == '__main__': unittest.main()\n"
)
SOURCE_ACCEPTANCE_CRITERIA = (
    "admin is authorized",
    "guest is rejected",
    "the provided deterministic unit tests pass",
    "guest authorization must be reported as high-severity blocking",
)


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
    return (proc.stdout or "").strip()


def run_tests(root: Path) -> dict:
    proc = subprocess.run(
        ["python", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return {"passed": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr}


def write_source_fixture(root: Path) -> None:
    (root / "auth.py").write_text(SOURCE_AUTH_IMPLEMENTATION, encoding="utf-8")
    (root / "test_auth.py").write_text(SOURCE_TEST_SUITE, encoding="utf-8")


def write_repaired_fixture(root: Path) -> None:
    (root / "auth.py").write_text(REPAIRED_AUTH_IMPLEMENTATION, encoding="utf-8")
    (root / "test_auth.py").write_text(REPAIRED_TEST_SUITE, encoding="utf-8")


def provider_ack(store: LocalStore, engine, provider_id: str) -> None:
    store.set_provider_constitution_ack(
        provider_id,
        {
            "constitution_supported": True,
            "constitution_acknowledged": True,
            "constitution_version": engine.version(),
            "constitution_hash": engine.content_hash(),
            "constitution_last_refresh": "stage7-repair-smoke",
            "constitution_acknowledged_at": "stage7-repair-smoke",
            "constitution_ack_actor": AUTOMATION_ACTOR,
        },
    )


def real_review_cycle(store: LocalStore, run_id: str, reviewers: list[dict]) -> dict:
    created = create_independent_review_cycle(
        store, run_id, reviewers=reviewers, actor=AUTOMATION_ACTOR
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
    reports = store.list_review_reports(created["cycle"]["cycle_id"])
    findings = store.list_review_findings(created["cycle"]["cycle_id"])
    return {
        "created": created,
        "finalized": finalized,
        "attempts": attempts,
        "reports": reports,
        "findings": findings,
    }


def main() -> int:
    smoke_root = Path(tempfile.mkdtemp(prefix="buildforme-stage7-repair-smoke-"))
    repo = smoke_root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "stage7-repair-smoke@buildforme.local")
    git(repo, "config", "user.name", "Buildforme Stage 7 Repair Smoke")
    git(repo, "remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
    (repo / "README.md").write_text("# Stage 7 repair smoke fixture\n", encoding="utf-8")
    (repo / "auth.py").write_text(
        "def is_authorized(role):\n    return role == 'admin'\n", encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature/stage7-repair-source")
    # Deliberate high-severity authorization bypass with limited tests that miss guests.
    write_source_fixture(repo)
    source_verification = run_tests(repo)
    if not source_verification["passed"]:
        raise RuntimeError(
            "controlled vulnerable source verification failed unexpectedly: "
            f"{source_verification['stdout']}\n{source_verification['stderr']}"
        )

    source_head_before = git(repo, "rev-parse", "HEAD")
    source_branch_before = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    source_patch_before = collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"]

    store = LocalStore(smoke_root / "runtime" / "state.json")
    store.upsert_project(
        {
            "id": "stage7-repair-smoke",
            "name": "Stage 7 real repair-loop smoke",
            "repository": "shanchaudary/Buildforme",
            "status": "active",
            "local_repository_root": str(repo),
        }
    )
    store.register_repository_binding(
        {
            "repository": "shanchaudary/Buildforme",
            "local_path": str(repo),
            "project_id": "stage7-repair-smoke",
        }
    )
    engine = get_engine(force_reload=True)
    packet = engine.attach_to_packet(
        {
            "id": "pkt-stage7-repair-source",
            "objective": (
                "Review an authorization implementation. Only administrators may be authorized. "
                "A guest receiving authorization is a high-severity blocking security defect even "
                "when the limited provided tests pass."
            ),
            "target_repository": "shanchaudary/Buildforme",
            "target_branch": "feature/stage7-repair-source",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "allowed_files": ["README.md", "auth.py", "test_auth.py"],
            "forbidden_files": [".env", "secrets/**"],
            "acceptance_criteria": list(SOURCE_ACCEPTANCE_CRITERIA),
            "required_tests": ["python -m unittest discover -s . -p test_*.py"],
        }
    )
    source_run_id = "run-stage7-repair-source"
    lease = engine.issue_run_lease(
        run_id=source_run_id,
        provider_id="glm",
        packet_id=packet["id"],
        actor=AUTOMATION_ACTOR,
    )
    store.save_constitution_lease(lease)
    source_run = {
        "id": source_run_id,
        "project_id": "stage7-repair-smoke",
        "task_id": "stage7-repair-smoke",
        "packet_id": packet["id"],
        "packet": packet,
        "provider_id": "glm",
        "repository": "shanchaudary/Buildforme",
        "repository_local_path": str(repo),
        "baseline_ref": baseline,
        "baseline_commit": baseline,
        "requested_target_branch": "feature/stage7-repair-source",
        "execution_branch": "feature/stage7-repair-source",
        "target_branch": "feature/stage7-repair-source",
        "operating_mode": "IMPLEMENTATION",
        "risk": "YELLOW",
        "status": "needs_review",
        "execution_mode": "live_supervised",
        "mode": "live_supervised",
        "transport": "controlled_fixture",
        "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
        "attempt": 0,
        "max_attempts": 2,
        "timeout_minutes": 30,
        "budget": {"max_cost_usd": 0},
        "review": {"hard_blocks": []},
        "worktree_path": str(repo),
        "evidence_ids": [],
        "controlled_source_fixture": True,
    }
    source_run = engine.attach_to_run(source_run, lease=lease, actor=AUTOMATION_ACTOR)
    source_run["scope_fingerprint"] = compute_run_scope_fingerprint(source_run, packet)
    source_run = store.save_run_for_setup(source_run)
    manifest = collect_changed_file_manifest(repo, baseline_commit=baseline)
    patch = collect_patch_evidence(repo, baseline_commit=baseline)
    source_evidence = build_evidence_bundle(
        run=source_run,
        packet=packet,
        process_result={
            "ok": True,
            "exit_code": 0,
            "pid": 1,
            "stdout": source_verification["stdout"],
            "stderr": source_verification["stderr"],
            "cleanup_ok": True,
            "process_group_isolated": True,
            "argv": ["controlled-source-fixture"],
        },
        worktree={
            "worktree_path": str(repo),
            "baseline_commit": baseline,
            "head_commit": baseline,
            "branch": "feature/stage7-repair-source",
        },
        diff={"manifest": manifest, "patch_fingerprint": patch["patch_fingerprint"]},
        provider_health={"version": "controlled-fixture", "executable": "controlled-fixture"},
        verification={
            "passed": True,
            "blocking_reasons": [],
            "checks": [
                {
                    "name": "unittest",
                    "status": "pass",
                    "detail": source_verification["stdout"] + source_verification["stderr"],
                }
            ],
        },
        constitution_result={"passed": True},
        approved_baseline_sha=baseline,
        final_head_sha=baseline,
        execution_branch="feature/stage7-repair-source",
        patch_fingerprint=patch["patch_fingerprint"],
        manifest_fingerprint=manifest["manifest_fingerprint"],
    )
    source_evidence = store.save_run_evidence(source_evidence)
    for provider_id in ("codex", "claude", "glm"):
        provider_ack(store, engine, provider_id)
    reviewers = [
        {"reviewer_id": "codex-real-reviewer", "provider_id": "codex", "role": "correctness"},
        {"reviewer_id": "claude-real-reviewer", "provider_id": "claude", "role": "security"},
    ]
    initial = real_review_cycle(store, source_run_id, reviewers)
    initial_aggregate = initial["finalized"].get("aggregate") or {}
    if initial_aggregate.get("status") != "repair_required":
        raise RuntimeError(
            "real reviewers did not produce repair_required for the deliberate authorization bypass"
        )
    blocking = [item for item in initial["findings"] if item.get("blocking") is True]
    if not blocking:
        raise RuntimeError("real reviewers did not persist a blocking finding for the authorization bypass")

    repair_packet = create_governed_repair_packet(
        store,
        initial["created"]["cycle"]["cycle_id"],
        repair_provider_id="glm",
        actor=AUTOMATION_ACTOR,
    )
    admitted = admit_governed_repair_run(
        store, repair_packet["repair_packet_id"], actor=AUTOMATION_ACTOR
    )
    child = admitted["run"]
    admission = admitted["admission"]
    repair_worktree = smoke_root / "repair-worktree"
    git(
        repo,
        "worktree",
        "add",
        "-b",
        child["execution_branch"],
        str(repair_worktree),
        child["execution_seed_commit"],
    )
    # Disclosed controlled repair execution fixture.
    write_repaired_fixture(repair_worktree)
    repair_verification = run_tests(repair_worktree)
    if not repair_verification["passed"]:
        raise RuntimeError(
            f"controlled repair fixture verification failed: {repair_verification['stdout']}\n{repair_verification['stderr']}"
        )
    repair_manifest = collect_changed_file_manifest(
        repair_worktree, baseline_commit=child["baseline_commit"]
    )
    repair_patch = collect_patch_evidence(
        repair_worktree, baseline_commit=child["baseline_commit"]
    )
    repair_patch_before_review = repair_patch["patch_fingerprint"]
    fresh_evidence = build_evidence_bundle(
        run=child,
        packet=child["packet"],
        process_result={
            "ok": True,
            "exit_code": 0,
            "pid": 2,
            "stdout": repair_verification["stdout"],
            "stderr": repair_verification["stderr"],
            "cleanup_ok": True,
            "process_group_isolated": True,
            "argv": ["controlled-repair-execution-fixture"],
        },
        worktree={
            "worktree_path": str(repair_worktree),
            "baseline_commit": child["execution_seed_commit"],
            "head_commit": child["execution_seed_commit"],
            "branch": child["execution_branch"],
        },
        diff={
            "manifest": repair_manifest,
            "patch_fingerprint": repair_patch["patch_fingerprint"],
        },
        provider_health={"version": "controlled-fixture", "executable": "controlled-fixture"},
        verification={
            "passed": True,
            "blocking_reasons": [],
            "checks": [{"name": "unittest", "status": "pass"}],
        },
        constitution_result={"passed": True},
        approved_baseline_sha=child["baseline_commit"],
        final_head_sha=child["execution_seed_commit"],
        execution_branch=child["execution_branch"],
        patch_fingerprint=repair_patch["patch_fingerprint"],
        manifest_fingerprint=repair_manifest["manifest_fingerprint"],
    )
    fresh_evidence = store.save_run_evidence(fresh_evidence)
    child = store.get_run(child["id"])
    child["status"] = "needs_review"
    child["verification"] = fresh_evidence["verification"]
    child["worktree_path"] = str(repair_worktree)
    child["worktree"] = {
        "worktree_path": str(repair_worktree),
        "baseline_commit": child["execution_seed_commit"],
        "head_commit": child["execution_seed_commit"],
        "branch": child["execution_branch"],
    }
    child["evidence"] = {
        "evidence_id": fresh_evidence["evidence_id"],
        "evidence_fingerprint": fresh_evidence["evidence_fingerprint"],
    }
    child["evidence_ids"] = [fresh_evidence["evidence_id"]]
    child["controlled_repair_execution_fixture"] = True
    store.save_run_for_setup(child)
    final_created = create_repair_review_cycle(
        store, repair_packet["repair_packet_id"], actor=AUTOMATION_ACTOR
    )
    final_attempts = []
    for assignment in final_created["assignments"]:
        execute_independent_review_assignment(
            store,
            final_created["cycle"]["cycle_id"],
            assignment["assignment_id"],
            actor=REVIEWER_ACTOR,
            timeout_seconds=900,
        )
        final_attempts.extend(store.list_review_execution_attempts(assignment["assignment_id"]))
    final = aggregate_independent_review_cycle(
        store, final_created["cycle"]["cycle_id"], actor=AUTOMATION_ACTOR
    )
    final_aggregate = final.get("aggregate") or {}
    final_reports = store.list_review_reports(final_created["cycle"]["cycle_id"])
    link = store.get_repair_review_link(repair_packet["repair_packet_id"])
    source_head_after = git(repo, "rev-parse", "HEAD")
    source_branch_after = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    source_patch_after = collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"]
    repair_patch_after_review = collect_patch_evidence(
        repair_worktree, baseline_commit=child["baseline_commit"]
    )["patch_fingerprint"]
    merge_count = int(
        git(repo, "rev-list", "--count", "--merges", f"{baseline}..{admission['seed_commit']}")
        or "0"
    )
    observed = {
        "controlled_source_fixture": True,
        "controlled_repair_execution_fixture": True,
        "initial_review_attempts": initial["attempts"],
        "initial_report_fingerprints": [item.get("report_fingerprint") for item in initial["reports"]],
        "initial_aggregate_report_fingerprints": initial_aggregate.get("report_fingerprints") or [],
        "initial_aggregate_status": initial_aggregate.get("status"),
        "blocking_finding_count": len(blocking),
        "initial_cycle_id": initial["created"]["cycle"]["cycle_id"],
        "source_evidence_id": source_evidence["evidence_id"],
        "repair_packet_id": repair_packet["repair_packet_id"],
        "repair_packet_source_cycle_id": repair_packet["source_cycle_id"],
        "repair_packet_source_evidence_id": repair_packet["source_evidence_id"],
        "repair_admission_id": admission["repair_admission_id"],
        "repair_admission_packet_id": admission["repair_packet_id"],
        "repair_child_run_id": child["id"],
        "repair_admission_child_run_id": admission["child_run_id"],
        "seed_commit": admission["seed_commit"],
        "seed_fingerprint": admission["seed_fingerprint"],
        "child_execution_seed_commit": child["execution_seed_commit"],
        "child_original_baseline": child["baseline_commit"],
        "source_original_baseline": baseline,
        "fresh_evidence_id": fresh_evidence["evidence_id"],
        "repair_verification_passed": fresh_evidence["verification"]["passed"],
        "repair_review_link_packet_id": link["repair_packet_id"],
        "repair_review_link_evidence_id": link["fresh_evidence_id"],
        "repair_review_link_cycle_id": link["review_cycle_id"],
        "final_cycle_id": final_created["cycle"]["cycle_id"],
        "final_review_attempts": final_attempts,
        "final_report_fingerprints": [item.get("report_fingerprint") for item in final_reports],
        "final_aggregate_report_fingerprints": final_aggregate.get("report_fingerprints") or [],
        "final_aggregate_status": final_aggregate.get("status"),
        "repair_provider_id": repair_packet["repair_provider_id"],
        "source_head_before": source_head_before,
        "source_head_after": source_head_after,
        "source_branch_before": source_branch_before,
        "source_branch_after": source_branch_after,
        "source_patch_before": source_patch_before,
        "source_patch_after": source_patch_after,
        "repair_patch_before_review": repair_patch_before_review,
        "repair_patch_after_review": repair_patch_after_review,
        "merge_commit_count": merge_count,
    }
    acceptance = evaluate_stage7_repair_smoke(observed)
    print("STAGE7_REPAIR_SMOKE_DIR", smoke_root)
    print("STAGE7_REPAIR_SMOKE_ACCEPTANCE_JSON", json.dumps(acceptance, sort_keys=True))
    print("MERGE no")
    return 0 if acceptance["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
