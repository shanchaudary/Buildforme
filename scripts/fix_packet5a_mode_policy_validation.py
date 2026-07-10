from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


store_path = ROOT / "buildforme" / "execution_store.py"
store = store_path.read_text(encoding="utf-8")
store = replace_once(
    store,
    '''            if mutation_type == "preflight_result" and "approval_requirements" in changed:
                requirement_edges = {
                    ("awaiting_preflight", "awaiting_approval"),
                    ("awaiting_preflight", "approved"),
                    ("awaiting_approval", "approved"),
                }
                if len(edges) != 1 or edges[0] not in requirement_edges:
                    raise ValueError(
                        "approval_requirements may change only on an authorized "
                        "preflight admission edge"
                    )
''',
    '''            if (
                mutation_type == "preflight_result"
                and "approval_requirements" in changed
                and not edges
            ):
                raise ValueError(
                    "approval_requirements may change only with an authorized "
                    "preflight state edge"
                )
''',
    label="preflight requirement edge policy",
)
store_path.write_text(store, encoding="utf-8")


test_path = ROOT / "tests" / "test_run_mutation_authority_hardening.py"
tests = test_path.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    '''        self._make_run("run-terminal-path", status="running")
''',
    '''        self._make_run(
            "run-terminal-path",
            status="running",
            execution_mode="dry_run",
            mode="dry_run",
            transport="dry_run",
        )
''',
    label="dry-run terminal fixture",
)
test_path.write_text(tests, encoding="utf-8")

print("Packet 5A mode-policy validation corrections applied")
