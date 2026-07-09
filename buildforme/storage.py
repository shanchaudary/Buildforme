"""Local JSON storage for Buildforme task packets and approval decisions.

This module is intentionally dependency-free and file-based for the MVP. It is
not a production database. It provides a deterministic local store so the
supervisor app can be tested without external services, credentials, or a cloud
deployment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = Path("runtime/buildforme_state.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class TaskRecord:
    task: dict[str, Any]
    classification: dict[str, Any]
    created_at: str
    updated_at: str
    status: str = "draft"
    decision: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "task": self.task,
            "classification": self.classification,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
        }
        if self.decision is not None:
            data["decision"] = self.decision
        return data


class LocalStore:
    """Small JSON-backed store for local supervisor testing."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"tasks": []}
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            return {"tasks": []}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Buildforme state file must contain a JSON object")
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("Buildforme state field 'tasks' must be a list")
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.path)

    def list_tasks(self) -> list[dict[str, Any]]:
        return list(self.load().get("tasks", []))

    def upsert_task(self, task: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        tasks: list[dict[str, Any]] = list(data.get("tasks", []))
        task_id = str(task.get("task_id", "")).strip()
        if not task_id:
            raise ValueError("task_id is required before saving a task")

        now = utc_now_iso()
        existing_index = next(
            (index for index, item in enumerate(tasks) if item.get("task", {}).get("task_id") == task_id),
            None,
        )
        if existing_index is None:
            record = TaskRecord(
                task=task,
                classification=classification,
                created_at=now,
                updated_at=now,
            ).to_dict()
            tasks.append(record)
        else:
            previous = tasks[existing_index]
            record = TaskRecord(
                task=task,
                classification=classification,
                created_at=str(previous.get("created_at") or now),
                updated_at=now,
                status=str(previous.get("status") or "draft"),
                decision=previous.get("decision"),
            ).to_dict()
            tasks[existing_index] = record

        data["tasks"] = tasks
        self.save(data)
        return record

    def set_decision(self, task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        tasks: list[dict[str, Any]] = list(data.get("tasks", []))
        for index, item in enumerate(tasks):
            if item.get("task", {}).get("task_id") == task_id:
                updated = dict(item)
                updated["status"] = str(decision.get("status") or "decision_recorded")
                updated["decision"] = {**decision, "recorded_at": utc_now_iso()}
                updated["updated_at"] = utc_now_iso()
                tasks[index] = updated
                data["tasks"] = tasks
                self.save(data)
                return updated
        raise KeyError(f"Task not found: {task_id}")
