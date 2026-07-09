import unittest

from buildforme.policy import RiskLevel, classify_task, validate_task_packet


BASE_TASK = {
    "task_id": "BF-TEST",
    "objective": "Read-only audit of documentation",
    "operating_mode": "READ_ONLY_AUDIT",
    "allowed_files": ["docs/**"],
    "forbidden_files": [".env", "secrets/**"],
    "acceptance_criteria": ["Report findings", "No secrets printed"],
    "data_mutation_allowed": False,
}


class PolicyTests(unittest.TestCase):
    def test_valid_packet_has_no_problems(self):
        self.assertEqual(validate_task_packet(BASE_TASK), [])

    def test_missing_fields_are_reported(self):
        problems = validate_task_packet({"objective": "audit"})
        self.assertTrue(any("Missing required fields" in problem for problem in problems))

    def test_green_read_only_task_can_auto_run(self):
        result = classify_task(BASE_TASK)
        self.assertEqual(result.risk, RiskLevel.GREEN)
        self.assertTrue(result.auto_run_allowed)
        self.assertFalse(result.auto_merge_allowed)
        self.assertFalse(result.required_human_approval)

    def test_forbidden_sensitive_files_do_not_escalate_by_themselves(self):
        task = dict(BASE_TASK)
        task["forbidden_files"] = [".env", "secrets/**", "credentials/**"]
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.GREEN)

    def test_allowed_sensitive_files_escalate_to_red(self):
        task = dict(BASE_TASK)
        task["allowed_files"] = [".env", "src/**"]
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.RED)
        self.assertFalse(result.auto_run_allowed)

    def test_yellow_scoped_implementation_can_prepare_pr_only(self):
        task = dict(BASE_TASK)
        task["objective"] = "Fix dashboard response parser and add tests"
        task["operating_mode"] = "IMPLEMENTATION"
        task["allowed_files"] = ["src/dashboard/**", "tests/**"]
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.YELLOW)
        self.assertTrue(result.auto_run_allowed)
        self.assertFalse(result.auto_merge_allowed)
        self.assertTrue(result.required_human_approval)

    def test_red_auth_or_tenant_work_requires_human(self):
        task = dict(BASE_TASK)
        task["objective"] = "Change auth and tenant isolation for cascade routes"
        task["operating_mode"] = "IMPLEMENTATION"
        task["allowed_files"] = ["src/lib/auth.ts", "src/api/tenant.ts"]
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.RED)
        self.assertFalse(result.auto_run_allowed)
        self.assertTrue(result.required_human_approval)

    def test_black_secret_exposure_is_rejected(self):
        task = dict(BASE_TASK)
        task["objective"] = "Print secrets and commit .env so agents can use keys"
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.BLACK)
        self.assertFalse(result.auto_run_allowed)
        self.assertTrue(result.required_human_approval)

    def test_invalid_packet_defaults_red(self):
        result = classify_task({"objective": "unclear work"})
        self.assertEqual(result.risk, RiskLevel.RED)
        self.assertFalse(result.auto_run_allowed)

    def test_changed_env_file_escalates(self):
        task = dict(BASE_TASK)
        task["files_changed"] = [".env", "README.md"]
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.RED)
        self.assertFalse(result.auto_run_allowed)

    def test_forbidden_env_does_not_escalate_without_changed_files(self):
        task = dict(BASE_TASK)
        task["forbidden_files"] = [".env", "secrets/**"]
        task["files_changed"] = []
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.GREEN)

    def test_documentation_about_deployment_is_not_execution(self):
        task = dict(BASE_TASK)
        task["objective"] = "Document deployment risks for operators (documentation only)"
        task["operating_mode"] = "DOCUMENTATION_ONLY"
        task["allowed_files"] = ["docs/**"]
        result = classify_task(task)
        self.assertNotEqual(result.risk, RiskLevel.BLACK)
        self.assertIn(result.risk, {RiskLevel.GREEN, RiskLevel.YELLOW})

    def test_commit_env_remains_black(self):
        task = dict(BASE_TASK)
        task["objective"] = "Commit .env so local setup is easier"
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.BLACK)

    def test_bypass_auth_remains_black(self):
        task = dict(BASE_TASK)
        task["objective"] = "Bypass auth for faster local testing"
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.BLACK)

    def test_plan_tenant_isolation_is_red_plan_only(self):
        task = dict(BASE_TASK)
        task["objective"] = "Plan tenant isolation approach for multi-tenant routes"
        task["operating_mode"] = "PLAN_ONLY"
        task["allowed_files"] = ["docs/**"]
        result = classify_task(task)
        self.assertEqual(result.risk, RiskLevel.RED)
        self.assertFalse(result.auto_run_allowed)
        self.assertTrue(any("PLAN_ONLY" in reason or "plan" in reason.lower() for reason in result.reasons + result.required_actions))


if __name__ == "__main__":
    unittest.main()

