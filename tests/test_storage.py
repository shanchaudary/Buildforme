import json
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
            # Stage 2 tasks mirror
            mirror = Path(temp_dir) / "tasks.json"
            self.assertTrue(mirror.exists())

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

    def test_save_list_repos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            self.assertEqual(store.list_repos(), [])
            repos = store.add_repo("shanchaudary/Buildforme")
            self.assertEqual(repos, ["shanchaudary/Buildforme"])
            repos = store.add_repo("https://github.com/shanchaudary/Buildforme")
            self.assertEqual(repos, ["shanchaudary/Buildforme"])
            repos = store.remove_repo("shanchaudary/Buildforme")
            self.assertEqual(repos, [])

    def test_save_list_approvals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            record = store.add_approval(
                {
                    "target_type": "pull_request",
                    "repository": "shanchaudary/Buildforme",
                    "number": 1,
                    "decision": "reviewed",
                    "note": "Looks fine locally",
                }
            )
            self.assertEqual(record["decision"], "reviewed")
            self.assertFalse(record["github_write"])
            self.assertEqual(record["scope"], "local_only")
            listed = store.list_approvals()
            self.assertEqual(len(listed), 1)
            found = store.find_approval("pull_request", "shanchaudary/Buildforme", 1)
            self.assertIsNotNone(found)
            self.assertEqual(found["note"], "Looks fine locally")

    def test_missing_runtime_files_ok(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "nested" / "state.json")
            self.assertEqual(store.list_tasks(), [])
            self.assertEqual(store.list_repos(), [])
            self.assertEqual(store.list_approvals(), [])

    def test_malformed_runtime_file_recovers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("{not-json", encoding="utf-8")
            store = LocalStore(path)
            self.assertEqual(store.list_tasks(), [])

            repos_path = Path(temp_dir) / "repos.json"
            repos_path.write_text("[]", encoding="utf-8")
            self.assertEqual(store.list_repos(), [])

    def test_no_token_stored_in_approvals_or_repos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            store.add_repo("owner/name")
            store.add_approval(
                {
                    "target_type": "issue",
                    "repository": "owner/name",
                    "number": 2,
                    "decision": "blocked",
                    "note": "wait",
                }
            )
            raw = (Path(temp_dir) / "approvals.json").read_text(encoding="utf-8")
            repos_raw = (Path(temp_dir) / "repos.json").read_text(encoding="utf-8")
            self.assertNotIn("token", raw.lower())
            self.assertNotIn("Bearer", raw)
            self.assertNotIn("token", repos_raw.lower())
            data = json.loads(raw)
            self.assertIn("approvals", data)

    def test_save_list_get_delete_packets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            self.assertEqual(store.list_packets(), [])
            saved = store.save_packet(
                {
                    "id": "pkt_test1",
                    "title": "Demo",
                    "risk": "GREEN",
                    "source_type": "manual",
                    "objective": "audit",
                    "markdown": "# Demo",
                    "api_token": "should-redact",
                }
            )
            self.assertEqual(saved["id"], "pkt_test1")
            self.assertEqual(saved["api_token"], "[redacted]")
            listed = store.list_packets()
            self.assertEqual(len(listed), 1)
            got = store.get_packet("pkt_test1")
            self.assertEqual(got["title"], "Demo")
            store.delete_packet("pkt_test1")
            self.assertEqual(store.list_packets(), [])
            with self.assertRaises(KeyError):
                store.get_packet("pkt_test1")

    def test_missing_packets_file_handled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            self.assertEqual(store.list_packets(), [])

    def test_runtime_storage_separation_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStore(Path(temp_dir) / "state.json")
            store.upsert_task(TASK, CLASSIFICATION)
            store.add_repo("owner/name")
            store.add_approval(
                {
                    "target_type": "pull_request",
                    "repository": "owner/name",
                    "number": 1,
                    "decision": "reviewed",
                    "note": "ok",
                }
            )
            store.save_packet({"id": "pkt_x", "title": "t", "markdown": "m"})
            self.assertEqual(len(store.list_tasks()), 1)
            self.assertEqual(store.list_repos(), ["owner/name"])
            self.assertEqual(len(store.list_approvals()), 1)
            self.assertEqual(len(store.list_packets()), 1)
            self.assertTrue((Path(temp_dir) / "packets.json").exists())
            self.assertTrue((Path(temp_dir) / "repos.json").exists())
            self.assertTrue((Path(temp_dir) / "approvals.json").exists())


if __name__ == "__main__":
    unittest.main()

