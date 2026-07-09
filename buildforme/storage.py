"""Local JSON storage for Buildforme tasks, watched repos, and approvals.

Dependency-free and file-based for the MVP. Not a production database.
Secrets must never be stored here.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = Path("runtime/buildforme_state.json")
DEFAULT_REPOS_NAME = "repos.json"
DEFAULT_APPROVALS_NAME = "approvals.json"
DEFAULT_TASKS_NAME = "tasks.json"
DEFAULT_PACKETS_NAME = "packets.json"

VALID_APPROVAL_DECISIONS = {
    "reviewed",
    "blocked",
    "ready_for_shan",
    "approved_local_only",
}
VALID_TARGET_TYPES = {"pull_request", "issue", "task"}


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
    """JSON-backed store for local supervisor testing.

    Task records remain in the primary state file (default
    ``runtime/buildforme_state.json``) for backward compatibility.

    Watched repositories and work-queue approvals are stored beside that file:

    - ``repos.json``
    - ``approvals.json``
    - ``tasks.json`` (mirror of task list for Stage 2 layout)
    """

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.runtime_dir = self.path.parent if self.path.parent != Path("") else Path("runtime")
        self.repos_path = self.runtime_dir / DEFAULT_REPOS_NAME
        self.approvals_path = self.runtime_dir / DEFAULT_APPROVALS_NAME
        self.tasks_mirror_path = self.runtime_dir / DEFAULT_TASKS_NAME
        self.packets_path = self.runtime_dir / DEFAULT_PACKETS_NAME

    # —— Tasks (existing API) ——

    def load(self) -> dict[str, Any]:
        return self._load_object(self.path, default={"tasks": []}, list_key="tasks")

    def save(self, data: dict[str, Any]) -> None:
        self._atomic_write(self.path, data)
        # Keep Stage 2 tasks.json mirror in sync (tasks only).
        tasks = data.get("tasks", [])
        if isinstance(tasks, list):
            self._atomic_write(self.tasks_mirror_path, {"tasks": tasks})

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

    # —— Watched repositories ——

    def list_repos(self) -> list[str]:
        data = self._load_object(self.repos_path, default={"repositories": []}, list_key="repositories")
        repos = data.get("repositories", [])
        cleaned: list[str] = []
        for item in repos:
            value = str(item).strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned

    def add_repo(self, repository: str) -> list[str]:
        repo = _normalize_repo(repository)
        repos = self.list_repos()
        if repo not in repos:
            repos.append(repo)
        self._atomic_write(self.repos_path, {"repositories": repos})
        return repos

    def remove_repo(self, repository: str) -> list[str]:
        repo = _normalize_repo(repository)
        repos = [item for item in self.list_repos() if item != repo]
        self._atomic_write(self.repos_path, {"repositories": repos})
        return repos

    # —— Local work-queue approvals ——

    def list_approvals(self) -> list[dict[str, Any]]:
        data = self._load_object(self.approvals_path, default={"approvals": []}, list_key="approvals")
        approvals = data.get("approvals", [])
        return list(approvals) if isinstance(approvals, list) else []

    def add_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_type = str(payload.get("target_type") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        if target_type not in VALID_TARGET_TYPES:
            raise ValueError(f"target_type must be one of {sorted(VALID_TARGET_TYPES)}")
        if decision not in VALID_APPROVAL_DECISIONS:
            raise ValueError(f"decision must be one of {sorted(VALID_APPROVAL_DECISIONS)}")

        repository = str(payload.get("repository") or "").strip()
        if target_type in {"pull_request", "issue"}:
            repository = _normalize_repo(repository)

        number_raw = payload.get("number")
        number: int | None
        if number_raw is None or number_raw == "":
            number = None
        else:
            number = int(number_raw)

        if target_type in {"pull_request", "issue"} and number is None:
            raise ValueError("number is required for pull_request and issue approvals")

        now = utc_now_iso()
        record = {
            "id": str(payload.get("id") or uuid.uuid4()),
            "target_type": target_type,
            "repository": repository or None,
            "number": number,
            "decision": decision,
            "note": str(payload.get("note") or "").strip(),
            "created_at": now,
            "updated_at": now,
            "scope": "local_only",
            "github_write": False,
            "disclaimer": "Local Buildforme decision only — not a GitHub review, approval, or merge.",
        }

        approvals = self.list_approvals()
        # Update existing row for same target if present.
        updated = False
        for index, existing in enumerate(approvals):
            if (
                existing.get("target_type") == record["target_type"]
                and existing.get("repository") == record["repository"]
                and existing.get("number") == record["number"]
            ):
                record["id"] = existing.get("id") or record["id"]
                record["created_at"] = existing.get("created_at") or now
                approvals[index] = record
                updated = True
                break
        if not updated:
            approvals.append(record)

        self._atomic_write(self.approvals_path, {"approvals": approvals})
        return record

    def find_approval(
        self,
        target_type: str,
        repository: str | None,
        number: int | None,
    ) -> dict[str, Any] | None:
        repo = None
        if repository:
            try:
                repo = _normalize_repo(repository)
            except ValueError:
                repo = repository
        for item in reversed(self.list_approvals()):
            if item.get("target_type") != target_type:
                continue
            if repo is not None and item.get("repository") != repo:
                continue
            if number is not None and item.get("number") != number:
                continue
            return item
        return None

    # —— Agent packets (Stage 3) ——

    def list_packets(self) -> list[dict[str, Any]]:
        data = self._load_object(self.packets_path, default={"packets": []}, list_key="packets")
        packets = data.get("packets", [])
        return list(packets) if isinstance(packets, list) else []

    def get_packet(self, packet_id: str) -> dict[str, Any]:
        pid = str(packet_id or "").strip()
        if not pid:
            raise ValueError("packet id is required")
        for item in self.list_packets():
            if str(item.get("id") or "") == pid:
                return item
        raise KeyError(f"Packet not found: {pid}")

    def save_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, dict):
            raise ValueError("packet must be an object")
        from buildforme.packet_generator import sanitize_for_storage

        cleaned = sanitize_for_storage(packet)
        packet_id = str(cleaned.get("id") or "").strip() or f"pkt_{uuid.uuid4().hex[:12]}"
        cleaned["id"] = packet_id
        now = utc_now_iso()
        packets = self.list_packets()
        updated = False
        for index, existing in enumerate(packets):
            if str(existing.get("id") or "") == packet_id:
                cleaned["created_at"] = existing.get("created_at") or now
                cleaned["updated_at"] = now
                packets[index] = cleaned
                updated = True
                break
        if not updated:
            cleaned.setdefault("created_at", now)
            cleaned["updated_at"] = now
            packets.append(cleaned)
        self._atomic_write(self.packets_path, {"packets": packets})
        return cleaned

    def delete_packet(self, packet_id: str) -> None:
        pid = str(packet_id or "").strip()
        if not pid:
            raise ValueError("packet id is required")
        packets = self.list_packets()
        remaining = [item for item in packets if str(item.get("id") or "") != pid]
        if len(remaining) == len(packets):
            raise KeyError(f"Packet not found: {pid}")
        self._atomic_write(self.packets_path, {"packets": remaining})

    # —— Internals ——

    def _load_object(self, path: Path, *, default: dict[str, Any], list_key: str) -> dict[str, Any]:
        if not path.exists():
            return dict(default)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return dict(default)
        if not raw.strip():
            return dict(default)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Recover from corruption without crashing the supervisor.
            return dict(default)
        if not isinstance(data, dict):
            return dict(default)
        values = data.get(list_key, default.get(list_key, []))
        if not isinstance(values, list):
            data = dict(default)
        return data

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(path)


def _normalize_repo(repository: str) -> str:
    cleaned = repository.strip().removeprefix("https://github.com/").strip("/")
    parts = cleaned.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("repository must be in owner/name form")
    owner, name = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("repository must be in owner/name form")
    return f"{owner}/{name}"
