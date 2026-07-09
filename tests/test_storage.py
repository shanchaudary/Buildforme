import tempfile
import unittest
from pathlib import Path

from buildforme.storage import LocalStore


TASK = {
    "task_id": "BF-STORAGE",
    "objective": "Read-only audit",
    "operating_mode": "READ_ONLY_AUDIT",
    "allowed_files": ["docs/**"],
    "forbidden_files": [".env"],
    "acceptance_criteria": ["Report findings"],
    "data_mutation_allowed": False,
}

CLASSIFICATION = {
    "risk": "GREEN",
    "auto_run_allowed": True,
    "auto_merge_allowed": False,
    "required_human_approval": False,
    "reasons": ["Low-risk work detected: audit"],
    "required_actions": ["Run scoped checks"],
}


class LocalStoreTests(unittest.TestCase):
    def test_new_store_lists_no_tasks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            self.assertEqual(store.list_tasks(), [])

    def test_upsert_task_creates_and_updates_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            first = store.upsert_task(TASK, CLASSIFICATION)
            self.assertEqual(first["task"]["task_id"], "BF-STORAGE")
            self.assertEqual(first["classification"]["risk"], "GREEN")

            updated_task = dict(TASK)
            updated_task["objective"] = "Documentation review"
            second = store.upsert_task(updated_task, CLASSIFICATION)
            self.assertEqual(second["created_at"], first["created_at"])
            self.assertNotEqual(second["updated_at"], "")
            self.assertEqual(len(store.list_tasks()), 1)

    def test_set_decision_updates_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            store.upsert_task(TASK, CLASSIFICATION)
            record = store.set_decision("BF-STORAGE", {"status": "approved", "reason": "safe"})
            self.assertEqual(record["status"], "approved")
            self.assertEqual(record["decision"]["reason"], "safe")
            self.assertIn("recorded_at", record["decision"])

    def test_set_decision_rejects_missing_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            with self.assertRaises(KeyError):
                store.set_decision("missing", {"status": "approved"})


if __name__ == "__main__":
    unittest.main()
