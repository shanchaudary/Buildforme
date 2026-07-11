"""Static and policy contracts for race-free Windows process containment."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class WindowsSuspendedLaunchContractTests(unittest.TestCase):
    def test_process_starts_suspended_before_job_assignment_and_resume(self):
        source = Path("buildforme/process_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("CREATE_SUSPENDED", source)
        self.assertIn("create_group | create_suspended", source)
        popen_index = source.index("proc = subprocess.Popen")
        assign_index = source.index("WindowsJob.create_and_assign", popen_index)
        resume_index = source.index("WindowsJob.resume_process", assign_index)
        stdin_index = source.index("if stdin_bytes is not None", resume_index)
        self.assertLess(popen_index, assign_index)
        self.assertLess(assign_index, resume_index)
        self.assertLess(resume_index, stdin_index)

    def test_native_resume_contract_is_explicit_and_fail_closed(self):
        source = Path("buildforme/windows_job.py").read_text(encoding="utf-8")
        self.assertIn("NtResumeProcess", source)
        self.assertIn("subprocess native process handle unavailable", source)
        self.assertIn("status != 0", source)

    def test_every_termination_escalation_receives_windows_job(self):
        source = Path("buildforme/process_supervisor.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "terminate_process_tree"
        ]
        self.assertGreaterEqual(len(calls), 5)
        for call in calls:
            keywords = {keyword.arg for keyword in call.keywords}
            self.assertIn(
                "windows_job",
                keywords,
                msg=f"terminate_process_tree call at line {call.lineno} lacks Windows Job proof",
            )

    def test_no_legacy_unsuspended_windows_launch_remains(self):
        source = Path("buildforme/process_supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
