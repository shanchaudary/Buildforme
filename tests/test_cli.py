import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from buildforme.cli import main


class CliTests(unittest.TestCase):
    def test_generate_packet_outputs_markdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task.json"
            path.write_text(
                json.dumps(
                    {
                        "task_id": "BF-CLI",
                        "objective": "Read-only audit of documentation",
                        "operating_mode": "READ_ONLY_AUDIT",
                        "allowed_files": ["docs/**"],
                        "forbidden_files": [".env"],
                        "acceptance_criteria": ["Report findings"],
                        "data_mutation_allowed": False,
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["generate-packet", str(path)])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            self.assertIn("# Agent Packet", out)
            self.assertIn("## Mission", out)
            self.assertIn("final report template", out.lower())

    def test_generate_packet_json_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task.json"
            path.write_text(
                json.dumps(
                    {
                        "task_id": "BF-CLI2",
                        "objective": "Read-only audit",
                        "operating_mode": "READ_ONLY_AUDIT",
                        "allowed_files": ["docs/**"],
                        "forbidden_files": [".env"],
                        "acceptance_criteria": ["Report"],
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["packet", str(path), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertIn("markdown", payload)
            self.assertEqual(payload["risk"], "GREEN")


if __name__ == "__main__":
    unittest.main()
