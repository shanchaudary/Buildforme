from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


job_path = ROOT / "buildforme" / "windows_job.py"
job = job_path.read_text(encoding="utf-8")
job = replace_once(
    job,
    '''    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
''',
    '''    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
''',
    label="NtResumeProcess declaration",
)
job = replace_once(
    job,
    '''    def active_processes(self) -> int:
''',
    '''    @staticmethod
    def resume_process(process: Any) -> None:
        """Resume a subprocess launched with CREATE_SUSPENDED.

        The root process cannot execute or spawn children until Job Object
        assignment has completed.  Any missing/invalid native handle fails closed.
        """
        if os.name != "nt":
            return
        native_handle = getattr(process, "_handle", None)
        if not native_handle:
            raise WindowsJobError("subprocess native process handle unavailable")
        status = int(ntdll.NtResumeProcess(native_handle))
        if status != 0:
            raise WindowsJobError(
                f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}"
            )

    def active_processes(self) -> int:
''',
    label="resume process method",
)
job_path.write_text(job, encoding="utf-8")


supervisor_path = ROOT / "buildforme" / "process_supervisor.py"
supervisor = supervisor_path.read_text(encoding="utf-8")
supervisor = replace_once(
    supervisor,
    '''        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP = 0x00000200
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
''',
    '''        if os.name == "nt":
            # The process must not execute before Job Object assignment.  Starting
            # suspended closes the child-spawn escape window between Popen and
            # AssignProcessToJobObject.
            create_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            create_suspended = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            popen_kwargs["creationflags"] = create_group | create_suspended
''',
    label="suspended launch flags",
)
supervisor = replace_once(
    supervisor,
    '''            if os.name == "nt":
                try:
                    windows_job = WindowsJob.create_and_assign(proc.pid)
                except Exception as exc:
                    terminated = terminate_process_tree(
                        proc,
                        reason="windows_job_assignment_failed",
                    )
                    confirmation = dict(terminated.get("confirmation") or {})
                    confirmation["confirmed"] = False
                    problems = list(confirmation.get("problems") or [])
                    problems.append(redact_text(str(exc))[:300])
                    confirmation["problems"] = problems
                    return redact_process_result(
                        {
                            "ok": False,
                            "exit_code": proc.returncode,
                            "stdout": "",
                            "stderr": "Windows Job Object assignment failed",
                            "timed_out": False,
                            "cancelled": False,
                            "duration_seconds": 0,
                            "error": "Windows Job Object assignment failed",
                            "argv": argv,
                            "cwd": str(cwd_path),
                            "env_names": env_names,
                            "env_policy": env_policy_summary(provider_id, env_names),
                            "termination_log": terminated.get("log") or [],
                            "termination_confirmation": confirmation,
                            "cleanup_ok": False,
                            "process_group_isolated": False,
                            "pid": proc.pid,
                        }
                    )
''',
    '''            if os.name == "nt":
                try:
                    windows_job = WindowsJob.create_and_assign(proc.pid)
                    WindowsJob.resume_process(proc)
                except Exception as exc:
                    if windows_job is not None:
                        try:
                            windows_job.terminate(exit_code=1)
                        except Exception:
                            pass
                    terminated = terminate_process_tree(
                        proc,
                        reason="windows_job_assignment_or_resume_failed",
                        windows_job=windows_job,
                    )
                    confirmation = dict(terminated.get("confirmation") or {})
                    confirmation["confirmed"] = False
                    problems = list(confirmation.get("problems") or [])
                    problems.append(redact_text(str(exc))[:300])
                    confirmation["problems"] = problems
                    result = redact_process_result(
                        {
                            "ok": False,
                            "exit_code": proc.returncode,
                            "stdout": "",
                            "stderr": "Windows suspended-launch containment failed",
                            "timed_out": False,
                            "cancelled": False,
                            "duration_seconds": 0,
                            "error": "Windows suspended-launch containment failed",
                            "argv": argv,
                            "cwd": str(cwd_path),
                            "env_names": env_names,
                            "env_policy": env_policy_summary(provider_id, env_names),
                            "termination_log": terminated.get("log") or [],
                            "termination_confirmation": confirmation,
                            "cleanup_ok": False,
                            "process_group_isolated": False,
                            "pid": proc.pid,
                        }
                    )
                    if windows_job is not None:
                        windows_job.close()
                    return result
''',
    label="assign then resume",
)
supervisor = replace_once(
    supervisor,
    '''            terminated = terminate_process_tree(
                proc,
                reason="wait_timeout_escalate",
                graceful_wait_sec=0.1,
                force_wait_sec=FORCE_WAIT_SEC,
            )
''',
    '''            terminated = terminate_process_tree(
                proc,
                reason="wait_timeout_escalate",
                graceful_wait_sec=0.1,
                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
''',
    label="wait escalation job proof",
)
supervisor_path.write_text(supervisor, encoding="utf-8")


test_path = ROOT / "tests" / "test_stage6_windows_suspended_launch.py"
test_path.write_text(
    '''"""Static and policy contracts for race-free Windows process containment."""

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
''',
    encoding="utf-8",
)


doc_path = ROOT / "docs" / "STAGE_6_MULTI_PROVIDER_EXECUTION.md"
doc = doc_path.read_text(encoding="utf-8")
doc += '''

## Windows suspended-launch containment

Windows provider processes are created with `CREATE_SUSPENDED`, assigned to a kill-on-close Job Object, and resumed only after assignment succeeds. This prevents a provider from spawning an uncontained child in the interval between process creation and Job Object assignment. Every termination escalation carries the same Job Object proof authority.
'''
doc_path.write_text(doc, encoding="utf-8")

print("Stage 6 Windows suspended-launch hardening applied")
