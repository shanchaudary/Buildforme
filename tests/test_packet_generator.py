import unittest

from buildforme.packet_generator import (
    ALWAYS_FORBIDDEN_ACTIONS,
    FINAL_REPORT_TEMPLATE,
    generate_agent_packet,
    packet_from_issue,
    packet_from_pr,
    packet_from_task,
    render_packet_markdown,
    sanitize_for_storage,
)


class PacketGeneratorTests(unittest.TestCase):
    def test_manual_objective_generates_packet(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "title": "Docs audit",
                "objective": "Read-only audit of documentation",
                "operating_mode": "READ_ONLY_AUDIT",
                "allowed_files": ["docs/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Report findings"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "main",
            }
        )
        self.assertEqual(packet["source_type"], "manual")
        self.assertEqual(packet["risk"], "GREEN")
        self.assertIn("git status --short", packet["starting_commands"])
        self.assertIn("Mission", packet["markdown"])
        self.assertTrue(packet["markdown"])

    def test_saved_task_generates_packet(self):
        task = {
            "task": {
                "task_id": "BF-1",
                "objective": "Fix dashboard parser and add tests",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["public/**", "tests/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Tests pass"],
            },
            "classification": {"risk": "YELLOW"},
            "status": "draft",
        }
        packet = packet_from_task(task)
        self.assertEqual(packet["source_type"], "task")
        self.assertEqual(packet["risk"], "YELLOW")
        joined = " ".join(packet["allowed_actions"]).lower()
        self.assertTrue("branch" in joined or "test" in joined or "implement" in joined)

    def test_pr_shaped_input_generates_packet(self):
        packet = packet_from_pr(
            {
                "number": 1,
                "title": "Add Founder Control Plane MVP",
                "body": "Stage 1 supervisor",
                "repository": "shanchaudary/Buildforme",
                "state": "open",
                "draft": False,
                "files": [{"filename": "docs/ROADMAP.md"}, {"filename": "public/app.js"}],
                "ci": {"status": "passing"},
            }
        )
        self.assertEqual(packet["source_type"], "pull_request")
        self.assertIn("docs/ROADMAP.md", packet["files_to_inspect"])
        self.assertIn("REVIEW", packet["operating_mode"])

    def test_issue_shaped_input_generates_packet(self):
        packet = packet_from_issue(
            {
                "number": 3,
                "title": "Document deployment risks only",
                "body": "Write docs about deployment risks. Read-only documentation.",
                "repository": "shanchaudary/Buildforme",
                "labels": ["docs"],
            }
        )
        self.assertEqual(packet["source_type"], "issue")
        self.assertIn(packet["risk"], {"GREEN", "YELLOW", "RED"})

    def test_red_risk_packet_blocks_execution(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "objective": "Change auth and tenant isolation for cascade routes",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["src/lib/auth.ts"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Secure"],
            }
        )
        self.assertEqual(packet["risk"], "RED")
        joined = " ".join(packet["allowed_actions"]).lower()
        self.assertTrue("plan" in joined or "approval" in joined)
        self.assertFalse(packet["classification"]["auto_run_allowed"])

    def test_black_risk_packet_says_reject(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "objective": "Print secrets and commit .env for agents",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["src/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Done"],
            }
        )
        self.assertEqual(packet["risk"], "BLACK")
        self.assertTrue(any("Reject" in a or "reject" in a.lower() for a in packet["allowed_actions"]))
        self.assertIn("Reject", packet["markdown"])

    def test_markdown_contains_required_sections(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "objective": "Read-only audit",
                "operating_mode": "READ_ONLY_AUDIT",
                "allowed_files": ["docs/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Report"],
            }
        )
        md = render_packet_markdown(packet)
        for section in (
            "## Header",
            "## Mission",
            "## Starting checks",
            "## Files to inspect",
            "## Allowed actions",
            "## Forbidden actions",
            "## Required tests",
            "## Manual proof required",
            "## Final report template",
            "## Stop conditions",
        ):
            self.assertIn(section, md)

    def test_forbidden_actions_always_include_core_safety(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "objective": "Read-only audit",
                "operating_mode": "READ_ONLY_AUDIT",
                "allowed_files": ["docs/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Report"],
            }
        )
        text = " ".join(packet["forbidden_actions"]).lower()
        self.assertIn("secret", text)
        self.assertIn("production", text)
        self.assertIn("auto-merge", text)
        self.assertEqual(packet["forbidden_actions"], ALWAYS_FORBIDDEN_ACTIONS)

    def test_final_report_template_included(self):
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "objective": "Read-only audit",
                "operating_mode": "READ_ONLY_AUDIT",
                "allowed_files": ["docs/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["Report"],
            }
        )
        self.assertIn("Task ID:", packet["final_report_template"])
        self.assertIn("Final git status:", packet["final_report_template"])
        self.assertIn(FINAL_REPORT_TEMPLATE.strip().splitlines()[0], packet["markdown"])

    def test_sanitize_redacts_token_fields(self):
        cleaned = sanitize_for_storage(
            {
                "title": "x",
                "api_key": "super-secret",
                "context": "Bearer abcdefghijklmnop",
                "nested": {"github_token": "ghp_1234567890"},
            }
        )
        self.assertEqual(cleaned["api_key"], "[redacted]")
        self.assertEqual(cleaned["nested"]["github_token"], "[redacted]")
        self.assertIn("[redacted]", cleaned["context"])


if __name__ == "__main__":
    unittest.main()
