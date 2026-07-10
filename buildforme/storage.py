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
DEFAULT_PROJECTS_NAME = "projects.json"
DEFAULT_STAGES_NAME = "stages.json"
DEFAULT_PLANNED_TASKS_NAME = "planned_tasks.json"
DEFAULT_TRUTH_NAME = "project_truth.json"
DEFAULT_RECOMMENDATIONS_NAME = "planner_recommendations.json"
DEFAULT_EVENTS_NAME = "events.json"
DEFAULT_BRIEFINGS_NAME = "briefings.json"
DEFAULT_EXECUTION_CONTROL_NAME = "execution_control.json"
DEFAULT_PROJECT_EXEC_CONTROLS_NAME = "project_execution_controls.json"
DEFAULT_REPO_LOCKS_NAME = "repository_locks.json"
DEFAULT_PROVIDERS_NAME = "providers.json"
DEFAULT_RUNS_NAME = "runs.json"
DEFAULT_RUN_EVENTS_NAME = "run_events.json"
DEFAULT_RUN_APPROVALS_NAME = "run_approvals.json"
DEFAULT_EXECUTION_POLICIES_NAME = "execution_policies.json"
DEFAULT_CONSTITUTION_LEASES_NAME = "constitution_leases.json"
DEFAULT_CONSTITUTION_VIOLATIONS_NAME = "constitution_violations.json"
DEFAULT_TASK_LOCKS_NAME = "task_locks.json"
DEFAULT_EVIDENCE_NAME = "run_evidence.json"
DEFAULT_REPO_BINDINGS_NAME = "repository_bindings.json"
DEFAULT_FOUNDER_SESSIONS_NAME = "founder_sessions.json"

LOCK_SCOPES = {"all", "write", "merge", "production", "branch"}
PROJECT_EXEC_STATUSES = {"enabled", "paused", "locked"}
RUN_APPROVAL_DECISIONS = {"approved", "rejected", "pending", "expired"}
RUN_APPROVAL_TYPES = {
    "shan_task_approval",
    "shan_red_risk_approval",
    "security_review",
    "architecture_review",
    "budget_approval",
    "provider_approval",
    "merge_approval",
    "deployment_approval",
}
# Only in-flight execution statuses count against concurrency (not draft/approval).
ACTIVE_RUN_STATUSES = frozenset({"queued", "starting", "running", "cancel_requested"})

PROJECT_STATUSES = {"active", "paused", "blocked", "completed", "archived"}
STAGE_STATUSES = {"not_started", "in_progress", "blocked", "ready_for_review", "complete"}
PLANNED_TASK_STATUSES = {"backlog", "ready", "in_progress", "review", "blocked", "complete", "rejected"}
TRUTH_CATEGORIES = {
    "working",
    "partial",
    "broken",
    "unsafe",
    "unverified",
    "not_implemented",
    "blocked",
}
PRIORITIES = {"critical", "high", "medium", "low"}
EFFORTS = {"small", "medium", "large", "unknown"}
RISKS = {"GREEN", "YELLOW", "RED", "BLACK"}

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
        self.projects_path = self.runtime_dir / DEFAULT_PROJECTS_NAME
        self.stages_path = self.runtime_dir / DEFAULT_STAGES_NAME
        self.planned_tasks_path = self.runtime_dir / DEFAULT_PLANNED_TASKS_NAME
        self.truth_path = self.runtime_dir / DEFAULT_TRUTH_NAME
        self.recommendations_path = self.runtime_dir / DEFAULT_RECOMMENDATIONS_NAME
        self.events_path = self.runtime_dir / DEFAULT_EVENTS_NAME
        self.briefings_path = self.runtime_dir / DEFAULT_BRIEFINGS_NAME
        self.execution_control_path = self.runtime_dir / DEFAULT_EXECUTION_CONTROL_NAME
        self.project_exec_controls_path = self.runtime_dir / DEFAULT_PROJECT_EXEC_CONTROLS_NAME
        self.repo_locks_path = self.runtime_dir / DEFAULT_REPO_LOCKS_NAME
        self.providers_path = self.runtime_dir / DEFAULT_PROVIDERS_NAME
        self.runs_path = self.runtime_dir / DEFAULT_RUNS_NAME
        self.run_events_path = self.runtime_dir / DEFAULT_RUN_EVENTS_NAME
        self.run_approvals_path = self.runtime_dir / DEFAULT_RUN_APPROVALS_NAME
        self.execution_policies_path = self.runtime_dir / DEFAULT_EXECUTION_POLICIES_NAME
        self.constitution_leases_path = self.runtime_dir / DEFAULT_CONSTITUTION_LEASES_NAME
        self.constitution_violations_path = self.runtime_dir / DEFAULT_CONSTITUTION_VIOLATIONS_NAME
        self.task_locks_path = self.runtime_dir / DEFAULT_TASK_LOCKS_NAME
        self.evidence_path = self.runtime_dir / DEFAULT_EVIDENCE_NAME
        self.repo_bindings_path = self.runtime_dir / DEFAULT_REPO_BINDINGS_NAME
        self.founder_sessions_path = self.runtime_dir / DEFAULT_FOUNDER_SESSIONS_NAME
        self._io_lock = __import__("threading").RLock()
        # Stage 6 transactional authority (SQLite WAL)
        from buildforme.execution_store import Stage6Store

        self.s6 = Stage6Store(self.runtime_dir / "buildforme_execution.db")
        self.db_path = self.runtime_dir / "buildforme_execution.db"

    def close(self) -> None:
        """Release SQLite handles (important for Windows temp cleanup)."""
        try:
            self.s6.db.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

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

    # —— Stage 4: projects / stages / planned tasks / truth / events ——

    def list_projects(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        data = self._load_object(self.projects_path, default={"projects": []}, list_key="projects")
        projects = list(data.get("projects") or [])
        if include_archived:
            return projects
        return [p for p in projects if str(p.get("status")) != "archived"]

    def get_project(self, project_id: str) -> dict[str, Any]:
        pid = str(project_id or "").strip()
        for item in self.list_projects():
            if str(item.get("id")) == pid:
                return item
        raise KeyError(f"Project not found: {pid}")

    def upsert_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("project must be an object")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        repository = _normalize_repo(str(payload.get("repository") or ""))
        status = str(payload.get("status") or "active").strip()
        if status not in PROJECT_STATUSES:
            raise ValueError(f"status must be one of {sorted(PROJECT_STATUSES)}")
        now = utc_now_iso()
        project_id = str(payload.get("id") or re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or uuid.uuid4().hex[:8])
        projects = self.list_projects()
        # Prevent duplicate repository for different projects
        for existing in projects:
            if str(existing.get("repository")) == repository and str(existing.get("id")) != project_id:
                raise ValueError(f"repository already registered to project {existing.get('id')}")
        record = {
            "id": project_id,
            "name": name,
            "repository": repository,
            "default_branch": str(payload.get("default_branch") or "main").strip() or "main",
            "status": status,
            "objective": str(payload.get("objective") or "").strip(),
            "current_stage_id": payload.get("current_stage_id"),
            "sample": bool(payload.get("sample", False)),
            "primary_language": payload.get("primary_language"),
            "verification_profile": payload.get("verification_profile"),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            "local_repository_root": payload.get("local_repository_root"),
            "created_at": now,
            "updated_at": now,
        }
        updated = False
        for index, existing in enumerate(projects):
            if str(existing.get("id")) == project_id:
                record["created_at"] = existing.get("created_at") or now
                record["updated_at"] = now
                # Preserve verification_profile/metadata if not provided
                if record.get("verification_profile") is None:
                    record["verification_profile"] = existing.get("verification_profile")
                if not record.get("metadata"):
                    record["metadata"] = existing.get("metadata") or {}
                if record.get("local_repository_root") is None:
                    record["local_repository_root"] = existing.get("local_repository_root")
                projects[index] = record
                updated = True
                break
        if not updated:
            projects.append(record)
        self._atomic_write(self.projects_path, {"projects": projects})
        self.append_event(
            {
                "event_type": "project_upserted" if updated else "project_created",
                "project_id": project_id,
                "target_type": "project",
                "target_id": project_id,
                "summary": f"Project {name} ({status})",
            }
        )
        # Ensure explicit execution-control record exists (fail-closed if ever removed)
        if not updated:
            try:
                existing_ctrl = self.get_project_execution_control(project_id)
                if not existing_ctrl.get("explicit"):
                    self.set_project_execution_control(
                        project_id,
                        execution_status="enabled" if status == "active" else "locked",
                        reason="auto-created with project",
                    )
            except Exception:
                self.set_project_execution_control(
                    project_id,
                    execution_status="enabled" if status == "active" else "locked",
                    reason="auto-created with project",
                )
        return record

    def set_project_status(self, project_id: str, status: str) -> dict[str, Any]:
        if status not in PROJECT_STATUSES:
            raise ValueError(f"status must be one of {sorted(PROJECT_STATUSES)}")
        project = self.get_project(project_id)
        project["status"] = status
        project["updated_at"] = utc_now_iso()
        projects = self.list_projects()
        for index, item in enumerate(projects):
            if str(item.get("id")) == str(project_id):
                projects[index] = project
                break
        self._atomic_write(self.projects_path, {"projects": projects})
        self.append_event(
            {
                "event_type": "project_status_changed",
                "project_id": project_id,
                "target_type": "project",
                "target_id": project_id,
                "summary": f"Project status → {status}",
            }
        )
        return project

    def list_stages(self, project_id: str | None = None) -> list[dict[str, Any]]:
        data = self._load_object(self.stages_path, default={"stages": []}, list_key="stages")
        stages = list(data.get("stages") or [])
        if project_id is None:
            return stages
        return [s for s in stages if str(s.get("project_id")) == str(project_id)]

    def upsert_stage(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        self.get_project(project_id)  # ensure exists
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("stage name is required")
        status = str(payload.get("status") or "not_started")
        if status not in STAGE_STATUSES:
            raise ValueError(f"status must be one of {sorted(STAGE_STATUSES)}")
        now = utc_now_iso()
        stage_id = str(payload.get("id") or f"stage-{uuid.uuid4().hex[:8]}")
        stages = self.list_stages()
        order = payload.get("order")
        if order is None:
            existing = self.list_stages(project_id)
            order = (max((int(s.get("order") or 0) for s in existing), default=0) + 1)
        record = {
            "id": stage_id,
            "project_id": project_id,
            "name": name,
            "order": int(order),
            "status": status,
            "objective": str(payload.get("objective") or "").strip(),
            "entry_criteria": _as_list(payload.get("entry_criteria")),
            "exit_criteria": _as_list(payload.get("exit_criteria")),
            "blocked_by": _as_list(payload.get("blocked_by")),
            "created_at": now,
            "updated_at": now,
        }
        updated = False
        for index, existing in enumerate(stages):
            if str(existing.get("id")) == stage_id:
                record["created_at"] = existing.get("created_at") or now
                record["updated_at"] = now
                stages[index] = record
                updated = True
                break
        if not updated:
            stages.append(record)
        self._atomic_write(self.stages_path, {"stages": stages})
        self.append_event(
            {
                "event_type": "stage_upserted",
                "project_id": project_id,
                "target_type": "stage",
                "target_id": stage_id,
                "summary": f"Stage {name}",
            }
        )
        return record

    def list_planned_tasks(self, project_id: str | None = None) -> list[dict[str, Any]]:
        data = self._load_object(self.planned_tasks_path, default={"planned_tasks": []}, list_key="planned_tasks")
        tasks = list(data.get("planned_tasks") or [])
        if project_id is None:
            return tasks
        return [t for t in tasks if str(t.get("project_id")) == str(project_id)]

    def get_planned_task(self, task_id: str) -> dict[str, Any]:
        tid = str(task_id or "").strip()
        for item in self.list_planned_tasks():
            if str(item.get("id")) == tid:
                return item
        raise KeyError(f"Planned task not found: {tid}")

    def upsert_planned_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        self.get_project(project_id)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        status = str(payload.get("status") or "backlog")
        if status not in PLANNED_TASK_STATUSES:
            raise ValueError(f"status must be one of {sorted(PLANNED_TASK_STATUSES)}")
        risk = str(payload.get("risk") or "YELLOW").upper()
        if risk not in RISKS:
            raise ValueError(f"risk must be one of {sorted(RISKS)}")
        priority = str(payload.get("priority") or "medium")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(PRIORITIES)}")
        effort = str(payload.get("estimated_effort") or "unknown")
        if effort not in EFFORTS:
            raise ValueError(f"estimated_effort must be one of {sorted(EFFORTS)}")
        now = utc_now_iso()
        task_id = str(payload.get("id") or f"TASK-{uuid.uuid4().hex[:8].upper()}")
        deps = _as_list(payload.get("dependencies"))
        # Reject self-dependency
        deps = [d for d in deps if d != task_id]
        record = {
            "id": task_id,
            "project_id": project_id,
            "stage_id": payload.get("stage_id"),
            "title": title,
            "objective": str(payload.get("objective") or title).strip(),
            "status": status,
            "risk": risk,
            "priority": priority,
            "estimated_effort": effort,
            "dependencies": deps,
            "blocks": _as_list(payload.get("blocks")),
            "allowed_files": _as_list(payload.get("allowed_files")) or ["docs/**", "tests/**"],
            "forbidden_files": _as_list(payload.get("forbidden_files")) or [".env", "secrets/**"],
            "acceptance_criteria": _as_list(payload.get("acceptance_criteria"))
            or ["Complete objective", "No secrets exposed"],
            "required_tests": _as_list(payload.get("required_tests")),
            "human_approval_required": bool(
                payload.get("human_approval_required", risk in {"RED", "BLACK"})
            ),
            "source_type": str(payload.get("source_type") or "manual"),
            "source_ref": payload.get("source_ref") if isinstance(payload.get("source_ref"), dict) else {},
            "created_at": now,
            "updated_at": now,
        }
        tasks = self.list_planned_tasks()
        updated = False
        for index, existing in enumerate(tasks):
            if str(existing.get("id")) == task_id:
                record["created_at"] = existing.get("created_at") or now
                record["updated_at"] = now
                tasks[index] = record
                updated = True
                break
        if not updated:
            tasks.append(record)
        self._atomic_write(self.planned_tasks_path, {"planned_tasks": tasks})
        self.append_event(
            {
                "event_type": "planned_task_upserted",
                "project_id": project_id,
                "target_type": "planned_task",
                "target_id": task_id,
                "summary": f"Task {task_id} → {status}/{risk}",
            }
        )
        return record

    def list_truth(self, project_id: str | None = None) -> list[dict[str, Any]]:
        data = self._load_object(self.truth_path, default={"truth": []}, list_key="truth")
        items = list(data.get("truth") or [])
        if project_id is None:
            return items
        return [t for t in items if str(t.get("project_id")) == str(project_id)]

    def upsert_truth(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        self.get_project(project_id)
        category = str(payload.get("category") or "unverified")
        if category not in TRUTH_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(TRUTH_CATEGORIES)}")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        confidence = int(payload.get("confidence") if payload.get("confidence") is not None else 50)
        confidence = max(0, min(100, confidence))
        now = utc_now_iso()
        truth_id = str(payload.get("id") or f"truth-{uuid.uuid4().hex[:8]}")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        record = {
            "id": truth_id,
            "project_id": project_id,
            "category": category,
            "title": title,
            "description": str(payload.get("description") or "").strip(),
            "evidence": evidence,
            "confidence": confidence,
            "source": str(payload.get("source") or "manual"),
            "created_at": now,
            "updated_at": now,
        }
        items = self.list_truth()
        updated = False
        for index, existing in enumerate(items):
            if str(existing.get("id")) == truth_id:
                record["created_at"] = existing.get("created_at") or now
                record["updated_at"] = now
                items[index] = record
                updated = True
                break
        if not updated:
            items.append(record)
        self._atomic_write(self.truth_path, {"truth": items})
        self.append_event(
            {
                "event_type": "truth_upserted",
                "project_id": project_id,
                "target_type": "truth",
                "target_id": truth_id,
                "summary": f"Truth ({category}): {title}",
            }
        )
        return record

    def append_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        event = {
            "id": str(payload.get("id") or f"evt_{uuid.uuid4().hex[:10]}"),
            "event_type": str(payload.get("event_type") or "unknown"),
            "project_id": payload.get("project_id"),
            "target_type": payload.get("target_type"),
            "target_id": payload.get("target_id"),
            "summary": str(payload.get("summary") or ""),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            "created_at": now,
        }
        data = self._load_object(self.events_path, default={"events": []}, list_key="events")
        events = list(data.get("events") or [])
        events.append(event)
        # Keep log bounded
        if len(events) > 2000:
            events = events[-2000:]
        self._atomic_write(self.events_path, {"events": events})
        return event

    def list_events(self, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        data = self._load_object(self.events_path, default={"events": []}, list_key="events")
        events = list(data.get("events") or [])
        if project_id is not None:
            events = [e for e in events if str(e.get("project_id")) == str(project_id)]
        return events[-max(1, min(limit, 500)) :]

    def save_recommendation_snapshot(self, project_id: str, plan: dict[str, Any]) -> None:
        data = self._load_object(
            self.recommendations_path,
            default={"recommendations": {}},
            list_key="recommendations",
        )
        # recommendations may be dict map
        raw = data.get("recommendations")
        if not isinstance(raw, dict):
            raw = {}
        raw[str(project_id)] = {
            "project_id": project_id,
            "saved_at": utc_now_iso(),
            "primary": plan.get("primary_recommendation"),
            "ranked": plan.get("ranked_recommendations"),
            "confidence": plan.get("confidence"),
        }
        path = self.recommendations_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps({"recommendations": raw}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)

    def save_briefing(self, briefing: dict[str, Any]) -> dict[str, Any]:
        data = self._load_object(self.briefings_path, default={"briefings": []}, list_key="briefings")
        briefings = list(data.get("briefings") or [])
        record = dict(briefing)
        record.setdefault("id", f"brief_{uuid.uuid4().hex[:10]}")
        record.setdefault("generated_at", utc_now_iso())
        briefings.append(record)
        if len(briefings) > 50:
            briefings = briefings[-50:]
        self._atomic_write(self.briefings_path, {"briefings": briefings, "last_generated_at": record["generated_at"]})
        self.append_event(
            {
                "event_type": "briefing_generated",
                "project_id": None,
                "target_type": "briefing",
                "target_id": record["id"],
                "summary": "Founder briefing generated",
            }
        )
        return record

    def last_briefing_at(self) -> str | None:
        if not self.briefings_path.exists():
            return None
        try:
            data = json.loads(self.briefings_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data.get("last_generated_at")
        return None

    def load_sample_project(self, sample: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
        """Load sample project payload. Will not overwrite existing project unless replace=True."""
        project = sample.get("project") or {}
        project_id = str(project.get("id") or "buildforme")
        existing = None
        try:
            existing = self.get_project(project_id)
        except KeyError:
            pass
        if existing and not replace and not existing.get("sample"):
            raise ValueError(f"project {project_id} already exists; pass replace to overwrite sample only")
        saved_project = self.upsert_project({**project, "sample": True})
        for stage in sample.get("stages") or []:
            self.upsert_stage({**stage, "project_id": project_id})
        for task in sample.get("planned_tasks") or []:
            self.upsert_planned_task({**task, "project_id": project_id})
        for truth in sample.get("truth") or []:
            self.upsert_truth({**truth, "project_id": project_id})
        # Explicit execution control required (fail-closed otherwise)
        self.set_project_execution_control(project_id, execution_status="enabled", reason="sample load")
        return saved_project

    # —— Stage 5: execution safety ——

    def get_execution_control(self) -> dict[str, Any]:
        record = self.s6.get_execution_control()
        record.setdefault("id", "global")
        return record

    def set_execution_control(self, *, kill_switch_active: bool, reason: str = "", actor: str = "shan") -> dict[str, Any]:
        from buildforme.governance import parse_bool_strict, validate_actor

        kill = parse_bool_strict(kill_switch_active, field="kill_switch_active")
        actor = validate_actor(actor)
        record = self.s6.set_execution_control(kill_switch_active=kill, reason=str(reason or ""), actor=actor)
        record["id"] = "global"
        record["activated_by"] = actor if kill else ""
        self.append_event(
            {
                "event_type": "kill_switch_activated" if kill else "kill_switch_deactivated",
                "project_id": None,
                "target_type": "execution_control",
                "target_id": "global",
                "summary": reason or ("activated" if kill else "deactivated"),
            }
        )
        return record

    def get_project_execution_control(self, project_id: str) -> dict[str, Any]:
        # SQLite is Stage 6 authority for project execution controls.
        record = self.s6.get_project_execution_control(str(project_id))
        if record:
            out = dict(record)
            out["explicit"] = True
            return out
        # One-time JSON compatibility import (legacy Stage 5 files only).
        data = self._load_object(
            self.project_exec_controls_path,
            default={"controls": []},
            list_key="controls",
        )
        for item in data.get("controls") or []:
            if str(item.get("project_id")) == str(project_id):
                status = str(item.get("execution_status") or "locked")
                if status in PROJECT_EXEC_STATUSES:
                    return self.s6.set_project_execution_control(
                        str(project_id),
                        execution_status=status,
                        reason=str(item.get("reason") or "imported from JSON"),
                        actor=str(item.get("actor") or "system"),
                    )
        # Fail closed: missing control is not enabled.
        return {
            "project_id": project_id,
            "execution_status": "locked",
            "reason": "no explicit execution-control record",
            "updated_at": utc_now_iso(),
            "explicit": False,
        }

    def set_project_execution_control(
        self,
        project_id: str,
        *,
        execution_status: str,
        reason: str = "",
        actor: str = "shan",
    ) -> dict[str, Any]:
        self.get_project(project_id)
        if execution_status not in PROJECT_EXEC_STATUSES:
            raise ValueError(f"execution_status must be one of {sorted(PROJECT_EXEC_STATUSES)}")
        record = self.s6.set_project_execution_control(
            project_id,
            execution_status=execution_status,
            reason=str(reason or ""),
            actor=actor,
        )
        self.append_event(
            {
                "event_type": "project_execution_control_changed",
                "project_id": project_id,
                "target_type": "project_execution_control",
                "target_id": project_id,
                "summary": f"execution_status → {execution_status}",
            }
        )
        return record

    def list_repository_locks(
        self,
        *,
        active_only: bool = False,
        repository: str | None = None,
    ) -> list[dict[str, Any]]:
        locks = self.s6.list_repository_locks(active_only=active_only, repository=repository)
        # Normalize status field for preflight (active bool → status string)
        out = []
        for lock in locks:
            item = dict(lock)
            if item.get("active") is False or item.get("released_at"):
                item["status"] = "released"
            else:
                item["status"] = "active"
                item["active"] = True
            out.append(item)
        if active_only:
            out = [x for x in out if str(x.get("status")) == "active"]
        return out

    def create_repository_lock(self, payload: dict[str, Any]) -> dict[str, Any]:
        repository = _normalize_repo(str(payload.get("repository") or ""))
        scope = str(payload.get("lock_scope") or "all")
        if scope not in LOCK_SCOPES:
            raise ValueError(f"lock_scope must be one of {sorted(LOCK_SCOPES)}")
        record = self.s6.create_repository_lock(
            {
                **payload,
                "repository": repository,
                "lock_scope": scope,
                "status": "active",
            }
        )
        record["status"] = "active"
        self.append_event(
            {
                "event_type": "repository_lock_created",
                "project_id": record.get("project_id"),
                "target_type": "repository_lock",
                "target_id": record["id"],
                "summary": f"{repository} scope={scope}",
            }
        )
        return record

    def release_repository_lock(self, lock_id: str, *, reason: str = "") -> dict[str, Any]:
        found = self.s6.release_repository_lock(lock_id, reason=reason)
        found["status"] = "released"
        self.append_event(
            {
                "event_type": "repository_lock_released",
                "project_id": found.get("project_id"),
                "target_type": "repository_lock",
                "target_id": lock_id,
                "summary": reason or "released",
            }
        )
        return found

    def list_providers(self) -> list[dict[str, Any]]:
        from buildforme.providers import default_provider_registry

        if not self.providers_path.exists():
            providers = default_provider_registry()
            self._atomic_write(self.providers_path, {"providers": providers})
        else:
            data = self._load_object(self.providers_path, default={"providers": []}, list_key="providers")
            providers = list(data.get("providers") or [])
            if not providers:
                providers = default_provider_registry()
                self._atomic_write(self.providers_path, {"providers": providers})
        # Force dry-run invariants + merge SQLite constitution acks (authoritative)
        for item in providers:
            item["mode"] = "dry_run"
            item["live_execution_available"] = False
            item["credentials_configured"] = False
            item.setdefault("constitution_supported", True)
            item.setdefault("constitution_acknowledged", False)
            item.setdefault("constitution_version", None)
            item.setdefault("constitution_hash", None)
            item.setdefault("constitution_last_refresh", None)
            item.setdefault("constitution_acknowledged_at", None)
            ack = self.s6.get_provider_ack(str(item.get("provider_id")))
            if ack:
                for key in (
                    "constitution_acknowledged",
                    "constitution_version",
                    "constitution_hash",
                    "constitution_last_refresh",
                    "constitution_acknowledged_at",
                    "constitution_ack_actor",
                    "constitution_supported",
                ):
                    if key in ack and ack[key] is not None:
                        item[key] = ack[key]
        return providers

    def get_provider_record(self, provider_id: str) -> dict[str, Any]:
        from buildforme.providers import get_provider

        provider = get_provider(self.list_providers(), provider_id)
        if not provider:
            raise KeyError(f"Provider not found: {provider_id}")
        return provider

    def update_provider(self, provider_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        from buildforme.providers import sanitize_provider_update

        providers = self.list_providers()
        updated_list = []
        found = None
        for item in providers:
            if str(item.get("provider_id")) == str(provider_id):
                found = sanitize_provider_update(item, patch or {})
                updated_list.append(found)
            else:
                updated_list.append(item)
        if not found:
            raise KeyError(f"Provider not found: {provider_id}")
        self._atomic_write(self.providers_path, {"providers": updated_list})
        return found

    def set_provider_constitution_ack(self, provider_id: str, ack: dict[str, Any]) -> dict[str, Any]:
        """Persist constitution acknowledgement (SQLite authority + provider registry mirror)."""
        # Ensure provider exists in registry
        self.get_provider_record(provider_id)
        s6_ack = self.s6.set_provider_constitution_ack(provider_id, ack or {})
        # Mirror into providers.json for UI listing compatibility
        providers = self.list_providers()
        updated_list = []
        found = None
        allowed = {
            "constitution_supported",
            "constitution_acknowledged",
            "constitution_version",
            "constitution_hash",
            "constitution_last_refresh",
            "constitution_acknowledged_at",
            "constitution_ack_actor",
        }
        for item in providers:
            if str(item.get("provider_id")) == str(provider_id):
                record = dict(item)
                for key in allowed:
                    if key in s6_ack:
                        record[key] = s6_ack[key]
                    elif key in (ack or {}):
                        record[key] = ack[key]
                record["mode"] = "dry_run"
                record["live_execution_available"] = False
                record["credentials_configured"] = False
                record["constitution_supported"] = True
                record["updated_at"] = utc_now_iso()
                found = record
                updated_list.append(record)
            else:
                updated_list.append(item)
        if not found:
            raise KeyError(f"Provider not found: {provider_id}")
        self._atomic_write(self.providers_path, {"providers": updated_list})
        # Merge s6 + registry
        found.update({k: s6_ack.get(k, found.get(k)) for k in allowed})
        return found

    def save_constitution_lease(self, lease: dict[str, Any]) -> dict[str, Any]:
        return self.s6.save_constitution_lease(lease)

    def list_constitution_leases(self, *, limit: int = 100, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.s6.list_constitution_leases(limit=limit, run_id=run_id)

    def get_constitution_lease(self, lease_id: str) -> dict[str, Any]:
        return self.s6.get_constitution_lease(lease_id)

    def append_constitution_violation(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._load_object(
            self.constitution_violations_path, default={"violations": []}, list_key="violations"
        )
        violations = list(data.get("violations") or [])
        record = dict(payload)
        record.setdefault("id", f"viol-{uuid.uuid4().hex[:12]}")
        record.setdefault("created_at", utc_now_iso())
        record.setdefault("type", "constitution_violation")
        violations.insert(0, record)
        self._atomic_write(self.constitution_violations_path, {"violations": violations[:1000]})
        # Also mirror into general events for timeline continuity
        try:
            self.append_event(
                {
                    "project_id": record.get("project_id"),
                    "event_type": "constitution_violation",
                    "summary": f"{record.get('law_id')}: {record.get('name')}",
                    "metadata": {
                        "law_id": record.get("law_id"),
                        "severity": record.get("severity"),
                        "run_id": record.get("run_id"),
                        "provider_id": record.get("provider_id"),
                    },
                }
            )
        except Exception:
            pass
        return record

    def list_constitution_violations(
        self,
        *,
        limit: int = 100,
        law_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        data = self._load_object(
            self.constitution_violations_path, default={"violations": []}, list_key="violations"
        )
        items = list(data.get("violations") or [])
        if law_id is not None:
            items = [x for x in items if str(x.get("law_id")) == str(law_id)]
        if run_id is not None:
            items = [x for x in items if str(x.get("run_id")) == str(run_id)]
        return items[: max(1, min(1000, int(limit)))]

    def list_task_locks(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        return self.s6.list_task_locks(active_only=active_only)

    def create_task_lock(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.s6.create_task_lock(payload)

    def release_task_lock(self, lock_id: str, *, reason: str = "") -> dict[str, Any]:
        return self.s6.release_task_lock(lock_id, reason=reason)

    def save_run_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return self.s6.save_run_evidence(evidence)

    def get_run_evidence(self, run_id: str) -> dict[str, Any]:
        return self.s6.get_run_evidence(run_id)

    def list_run_evidence(self, *, run_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.s6.list_run_evidence(run_id=run_id, limit=limit)

    def register_repository_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.s6.register_repository_binding(payload)

    def list_repository_bindings(self) -> list[dict[str, Any]]:
        return self.s6.list_repository_bindings()

    def get_repository_binding(self, repository: str) -> dict[str, Any] | None:
        return self.s6.get_repository_binding(repository)

    def create_founder_session(
        self,
        *,
        actor: str = "shan",
        ttl_seconds: int = 3600,
        admin_secret: str | None = None,
    ) -> dict[str, Any]:
        from buildforme.founder_auth import (
            load_or_create_admin_secret,
            mint_session_tokens,
            session_record,
            verify_admin_secret,
        )

        if not verify_admin_secret(self.runtime_dir, admin_secret):
            raise ValueError("invalid admin secret — founder authority cannot be self-minted")
        token, token_hash, csrf, csrf_hash = mint_session_tokens()
        record = session_record(
            actor=actor,
            token_hash=token_hash,
            csrf_hash=csrf_hash,
            ttl_seconds=ttl_seconds,
        )
        self.s6.create_founder_session_record(record)
        # Ensure secret file exists for operators
        load_or_create_admin_secret(self.runtime_dir)
        return {
            "token": token,
            "csrf_token": csrf,
            "actor": actor,
            "expires_in": ttl_seconds,
        }

    def validate_founder_token(self, token: str | None) -> dict[str, Any]:
        return self.s6.validate_founder_token(token)

    def get_execution_policy(self) -> dict[str, Any]:
        default = {
            "max_concurrent_global_runs": 1,
            "max_concurrent_per_project": 1,
            "default_timeout_minutes": 30,
            "hard_max_timeout_minutes": 120,
            "default_max_attempts": 1,
            "hard_max_attempts": 3,
            "updated_at": utc_now_iso(),
        }
        if not self.execution_policies_path.exists():
            self._atomic_write(self.execution_policies_path, default)
            return default
        try:
            data = json.loads(self.execution_policies_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return default
        if not isinstance(data, dict):
            return default
        merged = dict(default)
        merged.update(data)
        return merged

    def list_runs(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.s6.list_runs(project_id=project_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.s6.get_run(run_id)

    def save_run(self, run: dict[str, Any], *, expected_row_version: int | None = None) -> dict[str, Any]:
        return self.s6.save_run(run, expected_row_version=expected_row_version)

    def admit_run_atomic(self, **kwargs: Any) -> dict[str, Any]:
        return self.s6.admit_run_atomic(**kwargs)

    def transition_run_with_event(self, run: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.s6.transition_run_with_event(run, **kwargs)

    def save_run_legacy_json(self, run: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(run, dict) or not run.get("id"):
            raise ValueError("run with id required")
        # Never persist provider secrets
        cleaned = dict(run)
        for key in list(cleaned.keys()):
            low = str(key).lower()
            if any(x in low for x in ("token", "password", "api_key", "secret", "credential")):
                cleaned.pop(key, None)
        cleaned["updated_at"] = utc_now_iso()
        runs = self.list_runs()
        updated = False
        for index, item in enumerate(runs):
            if str(item.get("id")) == str(cleaned.get("id")):
                cleaned["created_at"] = item.get("created_at") or cleaned.get("created_at")
                runs[index] = cleaned
                updated = True
                break
        if not updated:
            cleaned.setdefault("created_at", utc_now_iso())
            runs.append(cleaned)
        self._atomic_write(self.runs_path, {"runs": runs})
        return cleaned

    def count_active_runs(self, *, provider_id: str | None = None, project_id: str | None = None) -> int:
        count = 0
        for run in self.list_runs():
            if str(run.get("status")) not in ACTIVE_RUN_STATUSES:
                continue
            if provider_id and str(run.get("provider_id")) != str(provider_id):
                continue
            if project_id and str(run.get("project_id")) != str(project_id):
                continue
            count += 1
        return count

    def append_run_event(
        self,
        run_id: str,
        event_type: str,
        summary: str,
        *,
        actor: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.s6.append_run_event(
            run_id, event_type, summary, actor=actor, metadata=metadata
        )

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.s6.list_run_events(run_id)

    def list_run_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.s6.list_run_approvals(run_id=run_id)

    def save_run_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        from buildforme.governance import validate_actor, validate_safe_id

        requirement = str(payload.get("requirement_type") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        if requirement not in RUN_APPROVAL_TYPES:
            raise ValueError(f"requirement_type must be one of {sorted(RUN_APPROVAL_TYPES)}")
        if decision not in RUN_APPROVAL_DECISIONS:
            raise ValueError(f"decision must be one of {sorted(RUN_APPROVAL_DECISIONS)}")
        if requirement in {"merge_approval", "deployment_approval"} and decision == "approved":
            raise ValueError("merge/deployment approvals cannot be granted in Stage 5")
        record = {
            **payload,
            "run_id": validate_safe_id(payload.get("run_id"), field="run_id"),
            "requirement_type": requirement,
            "decision": decision,
            "scope": str(payload.get("scope") or ""),
            "scope_fingerprint": str(payload.get("scope_fingerprint") or payload.get("scope") or ""),
            "note": str(payload.get("note") or ""),
            "actor": validate_actor(payload.get("actor") or "shan"),
            "packet_id": payload.get("packet_id"),
            "task_id": payload.get("task_id"),
            "constitution_version": payload.get("constitution_version"),
            "constitution_hash": payload.get("constitution_hash"),
            "constitution_lease_id": payload.get("constitution_lease_id"),
        }
        return self.s6.save_run_approval(record)

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


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []
