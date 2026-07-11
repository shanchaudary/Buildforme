from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage6_redteam_round2.py"
text = path.read_text(encoding="utf-8")

old = '''text = replace_once(
    text,
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
            )
\'\'\',
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
\'\'\',
    label="cancel passes Windows job",
)
'''
new = '''cancel_start = text.index("    def cancel(self, run_id: str)")
cancel_prefix = text[:cancel_start]
cancel_text = text[cancel_start:]
cancel_text = replace_once(
    cancel_text,
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
            )
\'\'\',
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
\'\'\',
    label="cancel passes Windows job",
)
text = cancel_prefix + cancel_text
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one generic cancel patch block, found {text.count(old)}")
text = text.replace(old, new, 1)

old_auth = '        combined = stdout + "\\n" + stderr\n'
new_auth = '        combined = stdout + "\\\\n" + stderr\n'
if text.count(old_auth) != 1:
    raise RuntimeError(f"expected one auth newline template, found {text.count(old_auth)}")
text = text.replace(old_auth, new_auth, 1)

old_import = '''from buildforme.storage import LocalStore


class CancellationRegistryLossTests'''
new_import = '''from buildforme.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]


class CancellationRegistryLossTests'''
if text.count(old_import) != 1:
    raise RuntimeError(f"expected one generated-test ROOT anchor, found {text.count(old_import)}")
text = text.replace(old_import, new_import, 1)

old_assert = 'self.assertIn("fingerprint mismatch", validate_run_outcome_evidence(evidence))'
new_assert = 'self.assertTrue(any("fingerprint mismatch" in problem for problem in validate_run_outcome_evidence(evidence)))'
count = text.count(old_assert)
if count != 3:
    raise RuntimeError(f"expected three fingerprint assertions, found {count}")
text = text.replace(old_assert, new_assert)

old_process = '''        with store.db.maintenance_lock(timeout_seconds=5):
            proc = subprocess.Popen(
                [sys.executable, str(script), str(db_path)],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "writer bypassed exclusive migration lock")
        stdout, stderr = proc.communicate(timeout=10)
'''
new_process = '''        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(ROOT) + (
            os.pathsep + child_env["PYTHONPATH"]
            if child_env.get("PYTHONPATH")
            else ""
        )
        with store.db.maintenance_lock(timeout_seconds=5):
            proc = subprocess.Popen(
                [sys.executable, str(script), str(db_path)],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
            )
            time.sleep(0.5)
            if proc.poll() is not None:
                early_stdout, early_stderr = proc.communicate(timeout=5)
                self.fail(
                    "writer exited before lock release; "
                    f"stdout={early_stdout!r} stderr={early_stderr!r}"
                )
        stdout, stderr = proc.communicate(timeout=10)
'''
if text.count(old_process) != 1:
    raise RuntimeError(f"expected one migration subprocess fixture, found {text.count(old_process)}")
text = text.replace(old_process, new_process, 1)

path.write_text(text, encoding="utf-8")
print("Stage 6 red-team patcher and regression-test corrections applied")
