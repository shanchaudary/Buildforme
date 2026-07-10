import unittest

from buildforme.adapters.dry_run import DryRunAdapter


class DryRunAdapterTests(unittest.TestCase):
    def test_dry_run_has_no_network_or_github_writes(self):
        adapter = DryRunAdapter("codex")
        result = adapter.dry_run(
            {
                "id": "run-1",
                "provider_id": "codex",
                "target_branch": "feature/x",
                "timeout_minutes": 30,
                "requested_capabilities": ["read_repository", "edit_repository", "run_tests"],
            },
            {
                "allowed_files": ["docs/**"],
                "forbidden_files": [".env"],
                "required_tests": ["python -m unittest"],
                "starting_commands": ["git status --short"],
            },
        )
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["would_execute"])
        self.assertEqual(result["network_calls"], [])
        self.assertEqual(result["github_writes"], [])
        self.assertEqual(result["shell_commands_executed"], [])
        self.assertTrue(result["planned_steps"])

    def test_blocks_merge_capability(self):
        adapter = DryRunAdapter("codex")
        with self.assertRaises(ValueError):
            adapter.dry_run(
                {
                    "id": "run-2",
                    "requested_capabilities": ["merge"],
                },
                {"objective": "x"},
            )


if __name__ == "__main__":
    unittest.main()
