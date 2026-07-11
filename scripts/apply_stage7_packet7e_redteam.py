from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# Founder auth semantics + strict body allowlist.
path = ROOT / "buildforme" / "server.py"
text = path.read_text(encoding="utf-8")
old = '''            auth = self._require_founder_mutation(payload)
            actor = str(payload.get("actor") or auth.get("actor") or "shan")
            forbidden = {
                "argv",
                "command",
                "executable",
                "repo_root",
                "repository_local_path",
                "local_path",
                "allowed_files",
                "forbidden_files",
                "reviewers",
                "policy",
                "scope_fingerprint",
                "repair_fingerprint",
                "seed_commit",
                "seed_ref",
                "child_run",
                "lease",
            }
            supplied = sorted(key for key in forbidden if key in payload)
            if supplied:
                raise ValueError(
                    "repair authority is storage-owned; forbidden fields supplied: "
                    + ", ".join(supplied)
                )
            if action == "create":
'''
new = '''            try:
                auth = self._require_founder_mutation(payload)
            except ValueError as exc:
                self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                return
            actor = str(auth.get("actor") or "shan")
            allowed = {"founder_token", "csrf_token"}
            if action == "create":
                allowed.add("repair_provider_id")
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise ValueError(
                    "repair action accepts only storage-bounded identifiers; unknown fields: "
                    + ", ".join(unknown)
                )
            if action == "create":
'''
text = replace_once(text, old, new, "repair auth and allowlist")
path.write_text(text, encoding="utf-8")

# Browser no longer supplies actor authority.
path = ROOT / "public" / "app.js"
text = path.read_text(encoding="utf-8")
old = '''    csrf_token: document.querySelector("#rp-csrf-token")?.value || "",
    actor: "shan",
    ...extra,
'''
new = '''    csrf_token: document.querySelector("#rp-csrf-token")?.value || "",
    ...extra,
'''
text = replace_once(text, old, new, "browser actor removal")
path.write_text(text, encoding="utf-8")

# Strengthen smoke acceptance to storage-bound reports/cycle/evidence and actual merge count.
path = ROOT / "buildforme" / "stage7_smoke.py"
text = path.read_text(encoding="utf-8")
old = '''    attempts = observed.get("review_execution_attempts") or []
    provider_ids = sorted({str(item.get("provider_id") or "") for item in attempts if item.get("status") == "succeeded"})
    checks = {
'''
new = '''    attempts = observed.get("review_execution_attempts") or []
    succeeded = [item for item in attempts if item.get("status") == "succeeded"]
    provider_ids = sorted({str(item.get("provider_id") or "") for item in succeeded})
    execution_report_fingerprints = sorted(
        str(item.get("report_fingerprint") or "")
        for item in succeeded
        if item.get("report_fingerprint")
    )
    persisted_report_fingerprints = sorted(str(item) for item in (observed.get("persisted_report_fingerprints") or []))
    aggregate_report_fingerprints = sorted(str(item) for item in (observed.get("aggregate_report_fingerprints") or []))
    checks = {
'''
text = replace_once(text, old, new, "smoke derived fingerprints")
old = '''        "codex_and_claude_succeeded": provider_ids == ["claude", "codex"],
        "auth_probes_verified": bool(attempts)
'''
new = '''        "codex_and_claude_succeeded": provider_ids == ["claude", "codex"] and len(succeeded) == 2,
        "reviewers_distinct_from_implementer": str(observed.get("implementer_provider_id") or "")
        not in provider_ids,
        "auth_probes_verified": bool(attempts)
'''
text = replace_once(text, old, new, "smoke implementer separation")
old = '''        "two_provider_quorum": observed.get("distinct_provider_count") == 2
        and sorted(observed.get("provider_ids") or []) == ["claude", "codex"],
        "aggregate_clear": observed.get("aggregate_status") == "clear",
        "verification_passed": observed.get("verification_passed") is True,
'''
new = '''        "two_provider_quorum": observed.get("distinct_provider_count") == 2
        and sorted(observed.get("provider_ids") or []) == ["claude", "codex"],
        "two_persisted_reports": int(observed.get("persisted_report_count") or 0) == 2
        and len(persisted_report_fingerprints) == 2,
        "execution_reports_match_storage_and_aggregate": bool(execution_report_fingerprints)
        and execution_report_fingerprints
        == persisted_report_fingerprints
        == aggregate_report_fingerprints,
        "cycle_bound_to_exact_evidence": str(observed.get("cycle_evidence_id") or "")
        == str(observed.get("expected_evidence_id") or "")
        and str(observed.get("cycle_evidence_fingerprint") or "")
        == str(observed.get("expected_evidence_fingerprint") or ""),
        "run_bound_to_cycle": str(observed.get("run_review_cycle_id") or "")
        == str(observed.get("cycle_id") or ""),
        "aggregate_clear": observed.get("aggregate_status") == "clear",
        "verification_passed": observed.get("verification_passed") is True,
'''
text = replace_once(text, old, new, "smoke report and evidence binding")
old = '''        "merge_not_performed": observed.get("merge_performed") is False,
        "no_synthetic_report_submission": observed.get("direct_report_submission_used") is False,
'''
new = '''        "merge_not_performed": int(observed.get("merge_commit_count") or 0) == 0,
        "no_synthetic_report_submission": execution_report_fingerprints
        == persisted_report_fingerprints,
'''
text = replace_once(text, old, new, "smoke derived no merge no synthetic")
path.write_text(text, encoding="utf-8")

# Populate strengthened observations from persisted storage and actual Git history.
path = ROOT / "scripts" / "stage7_real_two_provider_smoke.py"
text = path.read_text(encoding="utf-8")
old = '''    aggregate = finalized.get("aggregate") or {}
    observed = {
        "controlled_implementation_fixture": True,
        "review_execution_attempts": attempts,
        "distinct_provider_count": aggregate.get("distinct_provider_count"),
        "provider_ids": aggregate.get("provider_ids"),
        "aggregate_status": aggregate.get("status"),
        "verification_passed": True,
'''
new = '''    aggregate = finalized.get("aggregate") or {}
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
'''
text = replace_once(text, old, new, "smoke persisted observations")
old = '''        "source_patch_after": collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"],
        "merge_performed": False,
        "direct_report_submission_used": False,
'''
new = '''        "source_patch_after": collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"],
        "merge_commit_count": int(merge_count_text or "0"),
'''
text = replace_once(text, old, new, "smoke actual merge count")
path.write_text(text, encoding="utf-8")

# Tests align with derived storage proof and auth semantics.
path = ROOT / "tests" / "test_stage7_packet7e_operator_surfaces.py"
text = path.read_text(encoding="utf-8")
old = '''            "controlled_implementation_fixture": True,
            "review_execution_attempts": [attempt("codex"), attempt("claude")],
            "distinct_provider_count": 2,
'''
new = '''            "controlled_implementation_fixture": True,
            "implementer_provider_id": "glm",
            "review_execution_attempts": [attempt("codex"), attempt("claude")],
            "persisted_report_count": 2,
            "persisted_report_fingerprints": ["r1", "r2"],
            "aggregate_report_fingerprints": ["r1", "r2"],
            "cycle_id": "rc-1",
            "cycle_evidence_id": "ev-1",
            "cycle_evidence_fingerprint": "efp-1",
            "expected_evidence_id": "ev-1",
            "expected_evidence_fingerprint": "efp-1",
            "run_review_cycle_id": "rc-1",
            "distinct_provider_count": 2,
'''
text = replace_once(text, old, new, "smoke test storage proof")
old = '''            "merge_performed": False,
            "direct_report_submission_used": False,
'''
new = '''            "merge_commit_count": 0,
'''
text = replace_once(text, old, new, "smoke test merge count")
# Make fake attempts carry matching report fingerprints.
old = '''        attempt = lambda provider: {
            "provider_id": provider,
'''
new = '''        attempt = lambda provider: {
            "provider_id": provider,
            "report_fingerprint": "r1" if provider == "codex" else "r2",
'''
text = replace_once(text, old, new, "smoke test attempt fingerprints")
# Add endpoint assertions.
old = '''        self.assertIn("_require_founder_mutation(payload)", source)
'''
new = '''        self.assertIn("_require_founder_mutation(payload)", source)
        self.assertIn("HTTPStatus.FORBIDDEN", source)
        self.assertIn('actor = str(auth.get("actor") or "shan")', source)
        self.assertIn("unknown fields", source)
'''
text = replace_once(text, old, new, "server auth test")
path.write_text(text, encoding="utf-8")

# Contract source test ensures UI cannot submit audit actor.
contract = '''from __future__ import annotations\n\nimport unittest\nfrom pathlib import Path\n\n\nclass Stage7Packet7ERedTeamContracts(unittest.TestCase):\n    def test_repair_api_actor_comes_only_from_founder_session(self):\n        server = Path("buildforme/server.py").read_text(encoding="utf-8")\n        self.assertIn('actor = str(auth.get("actor") or "shan")', server)\n        self.assertNotIn('payload.get("actor") or auth.get("actor")', server)\n\n    def test_smoke_no_merge_and_report_truth_are_derived(self):\n        evaluator = Path("buildforme/stage7_smoke.py").read_text(encoding="utf-8")\n        script = Path("scripts/stage7_real_two_provider_smoke.py").read_text(encoding="utf-8")\n        self.assertIn("execution_reports_match_storage_and_aggregate", evaluator)\n        self.assertIn("cycle_bound_to_exact_evidence", evaluator)\n        self.assertIn("merge_commit_count", evaluator)\n        self.assertIn('"rev-list", "--count", "--merges"', script)\n        self.assertNotIn('"merge_performed": False', script)\n        self.assertNotIn('"direct_report_submission_used": False', script)\n\n\nif __name__ == "__main__":\n    unittest.main()\n'''
(ROOT / "tests" / "test_stage7_packet7e_redteam_contract.py").write_text(contract, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n### Packet 7E red-team hardening\n\n- Repair HTTP mutations return forbidden on failed founder authentication, derive the audit actor only from the validated session, and reject every non-allowlisted request field.\n- Smoke acceptance binds the two successful execution attempts to the two persisted report fingerprints and aggregate report fingerprints, the exact cycle/evidence/run binding, distinct implementer identity, and the actual Git merge-commit count. It no longer accepts caller-provided `no synthetic report` or `no merge` booleans.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7E red-team hardening applied")
