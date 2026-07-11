from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / "buildforme" / "review_execution.py"
text = path.read_text(encoding="utf-8")
old = '''    if str(run.get("constitution_lease_id") or "") != str(
        cycle.get("constitution_lease_id") or ""
    ):
        raise ValueError("review cycle Constitution lease is stale")

    evidence = store.get_latest_execution_evidence(str(run.get("id") or ""))
'''
new = '''    if str(run.get("constitution_lease_id") or "") != str(
        cycle.get("constitution_lease_id") or ""
    ):
        raise ValueError("review cycle Constitution lease is stale")
    engine = get_engine()
    if str(engine.content_hash() or "") != str(cycle.get("constitution_hash") or ""):
        raise ValueError(
            "canonical Constitution text does not match the review cycle hash; "
            "historical law text is unavailable and reviewer execution is blocked"
        )

    evidence = store.get_latest_execution_evidence(str(run.get("id") or ""))
'''
if text.count(old) != 1:
    raise RuntimeError(f"constitution binding anchor count={text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

path = root / "tests" / "test_stage7_packet7b_isolation_contract.py"
text = path.read_text(encoding="utf-8")
old = '''        self.assertIn('phase="independent_review"', source)
        self.assertIn('"constitution_reminder",', source)
'''
new = '''        self.assertIn('phase="independent_review"', source)
        self.assertIn('"constitution_reminder",', source)
        self.assertIn('engine.content_hash()', source)
        self.assertIn('canonical Constitution text does not match', source)
'''
if text.count(old) != 1:
    raise RuntimeError(f"contract anchor count={text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Stage 7 Constitution text binding applied")
