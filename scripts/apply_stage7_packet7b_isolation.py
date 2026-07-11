from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


path = ROOT / "buildforme" / "review_execution.py"
text = path.read_text(encoding="utf-8")

text = replace_once(
    text,
    "import hashlib\nimport json\nimport subprocess\nimport uuid\n",
    "import hashlib\nimport json\nimport os\nimport shutil\nimport subprocess\nimport tempfile\nimport uuid\n",
    "imports",
)
text = replace_once(
    text,
    "from governance.constitution_lease import validate_run_lease_against_store\n",
    "from governance.constitution_engine import get_engine\nfrom governance.constitution_lease import validate_run_lease_against_store\n",
    "constitution engine import",
)
text = replace_once(
    text,
    '        "post_snapshot_unproven",\n        "worktree_mutated",\n',
    '        "post_snapshot_unproven",\n        "workspace_isolation_failed",\n        "worktree_mutated",\n',
    "failure code",
)
text = replace_once(
    text,
    '''def _snapshots_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:\n    return _snapshot_identity(left) == _snapshot_identity(right)\n\n\n''',
    '''def _snapshots_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:\n    return _snapshot_identity(left) == _snapshot_identity(right)\n\n\ndef _workspace_tree_snapshot(root: Path) -> dict[str, Any]:\n    entries: list[dict[str, Any]] = []\n    total_bytes = 0\n    for item in sorted(root.rglob("*"), key=lambda p: p.as_posix()):\n        rel = item.relative_to(root).as_posix()\n        if item.is_symlink():\n            entries.append({"path": rel, "kind": "symlink", "target": os.readlink(item)})\n            continue\n        if item.is_dir():\n            entries.append({"path": rel, "kind": "dir"})\n            continue\n        if item.is_file():\n            data = item.read_bytes()\n            total_bytes += len(data)\n            entries.append({\n                "path": rel,\n                "kind": "file",\n                "size": len(data),\n                "sha256": hashlib.sha256(data).hexdigest(),\n            })\n    raw = json.dumps(entries, sort_keys=True, separators=(",", ":"))\n    return {\n        "workspace_tree_fingerprint": hashlib.sha256(raw.encode("utf-8")).hexdigest(),\n        "workspace_file_count": sum(1 for item in entries if item.get("kind") == "file"),\n        "workspace_total_bytes": total_bytes,\n    }\n\n\ndef _assert_no_symlink_escape(root: Path) -> None:\n    resolved_root = root.resolve()\n    prefix = str(resolved_root) + os.sep\n    for item in root.rglob("*"):\n        if not item.is_symlink():\n            continue\n        target = item.resolve()\n        if target != resolved_root and not str(target).startswith(prefix):\n            raise ValueError(f"review workspace source contains escaping symlink: {item}")\n\n\ndef _create_isolated_review_workspace(root: Path) -> tuple[tempfile.TemporaryDirectory[str], Path, dict[str, Any]]:\n    _assert_no_symlink_escape(root)\n    holder = tempfile.TemporaryDirectory(prefix="buildforme-review-")\n    target = Path(holder.name) / "workspace"\n    try:\n        shutil.copytree(\n            root,\n            target,\n            symlinks=False,\n            ignore=shutil.ignore_patterns(".git"),\n        )\n        snapshot = _workspace_tree_snapshot(target)\n        return holder, target, snapshot\n    except Exception:\n        holder.cleanup()\n        raise\n\n\n''',
    "workspace helpers",
)
text = replace_once(
    text,
    '''            "head_commit",\n            "files_changed",\n        )\n''',
    '''            "head_commit",\n            "files_changed",\n            "workspace_tree_fingerprint",\n            "workspace_file_count",\n            "workspace_total_bytes",\n        )\n''',
    "snapshot identity",
)
text = replace_once(
    text,
    '''        "constitution_lease_id": cycle.get("constitution_lease_id"),\n        "repository": run.get("repository"),\n''',
    '''        "constitution_lease_id": cycle.get("constitution_lease_id"),\n        "constitution_reminder": get_engine().reminder(\n            phase="independent_review",\n            lease=run.get("constitution_lease") if isinstance(run.get("constitution_lease"), dict) else None,\n        ),\n        "repository": run.get("repository"),\n''',
    "packet constitution reminder",
)
text = replace_once(
    text,
    '''            "constitution_lease_id",\n            "repository",\n''',
    '''            "constitution_lease_id",\n            "constitution_reminder",\n            "repository",\n''',
    "packet fingerprint constitution reminder",
)
text = replace_once(
    text,
    '''        "constitution_lease_id",\n        "review_material",\n''',
    '''        "constitution_lease_id",\n        "constitution_reminder",\n        "review_material",\n''',
    "packet required constitution reminder",
)
text = replace_once(
    text,
    '''    packet, pre_snapshot, root = build_verified_blind_review_packet(\n        store, cycle_id, assignment_id\n    )\n''',
    '''    packet, source_pre_snapshot, authoritative_root = build_verified_blind_review_packet(\n        store, cycle_id, assignment_id\n    )\n    pre_snapshot = dict(source_pre_snapshot)\n''',
    "execute root names",
)
text = replace_once(
    text,
    '''    process_result: dict[str, Any] = {}\n    process_started = False\n\n    def fail(\n''',
    '''    process_result: dict[str, Any] = {}\n    process_started = False\n    workspace_holder: tempfile.TemporaryDirectory[str] | None = None\n    review_root = authoritative_root\n\n    def fail(\n''',
    "workspace state",
)
text = replace_once(
    text,
    '''        store.record_review_execution_atomic(execution=execution, actor=actor)\n        raise ValueError(error)\n\n    if store.get_execution_control().get("kill_switch_active"):\n''',
    '''        store.record_review_execution_atomic(execution=execution, actor=actor)\n        if workspace_holder is not None:\n            workspace_holder.cleanup()\n        raise ValueError(error)\n\n    try:\n        workspace_holder, review_root, workspace_pre = _create_isolated_review_workspace(\n            authoritative_root\n        )\n        pre_snapshot.update(workspace_pre)\n    except Exception as exc:\n        fail(\n            f"review workspace isolation failed: {exc}",\n            failure_code="workspace_isolation_failed",\n            retry_safe=True,\n        )\n\n    if store.get_execution_control().get("kill_switch_active"):\n''',
    "workspace creation",
)
text = replace_once(
    text,
    '''            cwd=root,\n''',
    '''            cwd=review_root,\n''',
    "review cwd",
)
text = replace_once(
    text,
    '''        post_snapshot = _collect_snapshot(\n            root, store.get_evidence_by_id(str(packet["evidence_id"]))\n        )\n''',
    '''        source_post_snapshot = _collect_snapshot(\n            authoritative_root, store.get_evidence_by_id(str(packet["evidence_id"]))\n        )\n        workspace_post = _workspace_tree_snapshot(review_root)\n        post_snapshot = {**source_post_snapshot, **workspace_post}\n''',
    "post snapshots",
)
text = replace_once(
    text,
    '''    return store.submit_review_report_atomic(\n        cycle_id=cycle_id,\n        assignment_id=assignment_id,\n        report=report,\n        findings=findings,\n        actor=str(assignment.get("reviewer_id") or actor),\n        execution=execution,\n    )\n''',
    '''    try:\n        return store.submit_review_report_atomic(\n            cycle_id=cycle_id,\n            assignment_id=assignment_id,\n            report=report,\n            findings=findings,\n            actor=str(assignment.get("reviewer_id") or actor),\n            execution=execution,\n        )\n    finally:\n        if workspace_holder is not None:\n            workspace_holder.cleanup()\n''',
    "success cleanup",
)
path.write_text(text, encoding="utf-8")

path = ROOT / "tests" / "test_stage7_review_execution.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''        self.assertFalse(any(packet["blind_context"].values()))\n        self.assertNotIn("reports", packet)\n''',
    '''        self.assertFalse(any(packet["blind_context"].values()))\n        self.assertIn("constitution_reminder", packet)\n        self.assertIn("text", packet["constitution_reminder"])\n        self.assertNotIn("reports", packet)\n''',
    "packet constitution test",
)
text = replace_once(
    text,
    '''        self.assertFalse(attempts[-1]["worktree_unchanged"])\n        self.assertFalse(attempts[-1]["retry_safe"])\n''',
    '''        self.assertFalse(attempts[-1]["worktree_unchanged"])\n        self.assertFalse((self.root / "reviewer-wrote.txt").exists())\n        self.assertFalse(attempts[-1]["retry_safe"])\n''',
    "authoritative worktree protected test",
)
path.write_text(text, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n### Packet 7B red-team isolation hardening\n\nReviewer processes execute only in a disposable copied workspace. The governed execution\nworktree is never used as reviewer cwd. The full disposable tree is fingerprinted before and\nafter review, the authoritative worktree is separately re-proved unchanged, escaping symlinks\nare rejected, and the copy is destroyed on every outcome. Review packets also carry the\ncanonical Constitution reminder bound to the run lease.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7B isolation hardening applied")
