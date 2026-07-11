from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7b_redteam.py"
text = path.read_text(encoding="utf-8")

old = '''text = replace_once(
    text,
    \'\'\'        self._git("config", "user.name", "review-test")
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
\'\'\',
    \'\'\'        self._git("config", "user.name", "review-test")
        self._git("remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
\'\'\',
    label="test remote identity",
)
'''
new = '''text = replace_once(
    text,
    r\'\'\'        self._git("config", "user.name", "review-test")
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
\'\'\',
    r\'\'\'        self._git("config", "user.name", "review-test")
        self._git("remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
\'\'\',
    label="test remote identity",
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"remote fixture template count={text.count(old)}")
text = text.replace(old, new, 1)

old = '''text = replace_once(
    text,
    \'\'\'        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n\\ndef sub(a, b):\\n    return a - b\\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        run = {
\'\'\',
    \'\'\'        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        self._git("checkout", "-b", "feature/stage7b-run")
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n\\ndef sub(a, b):\\n    return a - b\\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.upsert_project(
            {
                "id": "buildforme",
                "name": "Buildforme",
                "repository": "shanchaudary/Buildforme",
                "status": "active",
                "local_repository_root": str(self.root),
            }
        )
        self.store.register_repository_binding(
            {
                "repository": "shanchaudary/Buildforme",
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet(
            {
                "id": "pkt-stage7b",
                "objective": "Add subtraction function",
                "acceptance_criteria": ["sub returns a-b"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7b",
                "allowed_files": ["app.py"],
                "forbidden_files": [".env"],
            }
        )
        lease = engine.issue_run_lease(
            run_id="run-stage7b",
            provider_id="claude",
            packet_id=packet["id"],
            actor="test",
        )
        self.store.save_constitution_lease(lease)
        run = {
\'\'\',
    label="test governed setup",
)
'''
new = '''text = replace_once(
    text,
    r\'\'\'        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n\\ndef sub(a, b):\\n    return a - b\\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        run = {
\'\'\',
    r\'\'\'        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        self._git("checkout", "-b", "feature/stage7b-run")
        (self.root / "app.py").write_text("def add(a, b):\\n    return a + b\\n\\ndef sub(a, b):\\n    return a - b\\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.upsert_project(
            {
                "id": "buildforme",
                "name": "Buildforme",
                "repository": "shanchaudary/Buildforme",
                "status": "active",
                "local_repository_root": str(self.root),
            }
        )
        self.store.register_repository_binding(
            {
                "repository": "shanchaudary/Buildforme",
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet(
            {
                "id": "pkt-stage7b",
                "objective": "Add subtraction function",
                "acceptance_criteria": ["sub returns a-b"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7b",
                "allowed_files": ["app.py"],
                "forbidden_files": [".env"],
            }
        )
        lease = engine.issue_run_lease(
            run_id="run-stage7b",
            provider_id="claude",
            packet_id=packet["id"],
            actor="test",
        )
        self.store.save_constitution_lease(lease)
        run = {
\'\'\',
    label="test governed setup",
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"governed fixture template count={text.count(old)}")
text = text.replace(old, new, 1)

old = 'text = text[:start] + new_execute + "\\n"\n'
new = 'text = text[:start] + new_execute.rstrip() + "\\n"\n'
if text.count(old) != 1:
    raise RuntimeError(f"review_execution EOF template count={text.count(old)}")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("Packet 7B red-team fixture templates corrected")
