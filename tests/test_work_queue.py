import unittest
from unittest.mock import MagicMock

from buildforme.policy import RiskLevel, classify_github_item, recommended_action_for
from buildforme.work_queue import pick_recommended_next_task


class GitHubPolicyTests(unittest.TestCase):
    def test_docs_only_pr_is_not_red(self):
        result = classify_github_item(
            item_type="pull_request",
            repository="owner/repo",
            number=1,
            title="Update documentation",
            body="Docs-only improvements",
            files_changed=["docs/README.md", "docs/guide.md"],
        )
        self.assertIn(result.risk, {RiskLevel.GREEN, RiskLevel.YELLOW})
        self.assertNotEqual(result.risk, RiskLevel.RED)

    def test_auth_migration_pr_is_red(self):
        result = classify_github_item(
            item_type="pull_request",
            repository="owner/repo",
            number=2,
            title="Harden auth session handling",
            body="Touches tenant isolation",
            files_changed=["src/lib/auth.ts", "prisma/migrations/001_init.sql"],
        )
        self.assertEqual(result.risk, RiskLevel.RED)

    def test_secret_exposure_issue_is_black(self):
        result = classify_github_item(
            item_type="issue",
            repository="owner/repo",
            number=3,
            title="Print secrets and commit .env for agents",
            body="Show api key in logs",
        )
        self.assertEqual(result.risk, RiskLevel.BLACK)

    def test_changed_env_file_is_high_risk(self):
        result = classify_github_item(
            item_type="pull_request",
            repository="owner/repo",
            number=4,
            title="Adjust local configuration",
            body="Config tweak",
            files_changed=[".env", "README.md"],
        )
        self.assertEqual(result.risk, RiskLevel.RED)

    def test_forbidden_env_in_packet_not_via_github_helper(self):
        # GitHub helper always sets forbidden_files to include .env as a control.
        result = classify_github_item(
            item_type="issue",
            repository="owner/repo",
            number=5,
            title="Read-only audit of documentation",
            body="Audit docs only",
            labels=["docs"],
            files_changed=[],
        )
        self.assertEqual(result.risk, RiskLevel.GREEN)

    def test_recommended_actions_by_risk(self):
        self.assertIn("Reject", recommended_action_for(RiskLevel.BLACK))
        self.assertIn("Shan", recommended_action_for(RiskLevel.RED))
        self.assertIn("Review required", recommended_action_for(RiskLevel.YELLOW, ci_status="passing"))
        self.assertIn("unattended", recommended_action_for(RiskLevel.GREEN).lower())

    def test_unknown_ci_is_not_called_passing_in_action_text_for_failing(self):
        text = recommended_action_for(RiskLevel.YELLOW, target_type="pull_request", ci_status="failing")
        self.assertIn("CI failing", text)


class RecommendNextTests(unittest.TestCase):
    def test_black_outranks_green(self):
        prs = [
            {
                "target_type": "pull_request",
                "repository": "a/b",
                "number": 1,
                "title": "green",
                "classification": {"risk": "GREEN"},
                "ci": {"status": "passing"},
                "recommended_action": "ok",
            }
        ]
        issues = [
            {
                "target_type": "issue",
                "repository": "a/b",
                "number": 9,
                "title": "bad",
                "classification": {"risk": "BLACK"},
                "recommended_action": "reject",
            }
        ]
        next_item = pick_recommended_next_task(prs, issues)
        self.assertEqual(next_item["priority"], 1)
        self.assertEqual(next_item["number"], 9)

    def test_empty_queue_suggests_create_task(self):
        next_item = pick_recommended_next_task([], [])
        self.assertEqual(next_item["priority"], 7)
        self.assertIn("Create", next_item["headline"])


class WorkQueueBuildSmoke(unittest.TestCase):
    def test_build_work_queue_handles_github_errors(self):
        from buildforme.storage import LocalStore
        from buildforme.work_queue import build_work_queue
        import tempfile
        from pathlib import Path

        client = MagicMock()
        client.token = None
        client.get_repo.side_effect = Exception("boom")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            # Force get_repo path that catches GitHubClientError — Exception is outer
            # build_work_queue catches GitHubClientError and ValueError only on get_repo
            from buildforme.github_client import GitHubClientError

            client.get_repo.side_effect = GitHubClientError("boom")
            payload = build_work_queue(store, client, repos=["owner/repo"])
            self.assertEqual(payload["summary"]["open_prs"], 0)
            self.assertTrue(payload["errors"])


if __name__ == "__main__":
    unittest.main()
