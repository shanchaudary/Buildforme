from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parents[1] / "tests" / "test_placeholder_runs.py"
text = path.read_text(encoding="utf-8")
old = '''        # Valid run still imported
        store.get_run("run-ok")
'''
new = '''        # Atomic migration is all-or-nothing: the valid row is rolled back with
        # the orphan records and the existing authority remains unchanged.
        self.assertTrue(report.get("rolled_back"))
        self.assertFalse(report.get("atomic_commit"))
        with self.assertRaises(KeyError):
            store.get_run("run-ok")
        self.assertEqual(store.list_runs(), [])
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one legacy partial-import assertion, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Atomic migration regression expectation updated")
