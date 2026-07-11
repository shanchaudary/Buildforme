from pathlib import Path

root = Path(__file__).resolve().parent.parent

path = root / "tests" / "test_stage7_packet7e_operator_surfaces.py"
text = path.read_text(encoding="utf-8")
old = '''        for forbidden in ("argv", "repo_root", "reviewers", "seed_commit", "scope_fingerprint"):
            self.assertIn(f'"{forbidden}"', source)
'''
new = '''        self.assertIn("unknown = sorted(set(payload) - allowed)", source)
        self.assertIn("repair action accepts only storage-bounded identifiers", source)
        self.assertIn('allowed = {"founder_token", "csrf_token"}', source)
'''
if text.count(old) != 1:
    raise RuntimeError("operator repair allowlist test anchor mismatch")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

path = root / "tests" / "test_stage7_packet7e_redteam_contract.py"
text = path.read_text(encoding="utf-8")
old = '''        server = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertIn('actor = str(auth.get("actor") or "shan")', server)
        self.assertNotIn('payload.get("actor") or auth.get("actor")', server)
'''
new = '''        server = Path("buildforme/server.py").read_text(encoding="utf-8")
        repair_handler = server.split("def _stage7_repair_action", 1)[1].split(
            "def _stage7_review_action", 1
        )[0]
        self.assertIn('actor = str(auth.get("actor") or "shan")', repair_handler)
        self.assertNotIn('payload.get("actor")', repair_handler)
'''
if text.count(old) != 1:
    raise RuntimeError("repair actor contract anchor mismatch")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Packet 7E red-team tests corrected")
