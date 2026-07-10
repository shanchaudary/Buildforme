"""Local Buildforme supervisor server.

Dependency-free HTTP server for the MVP. Serves the static dashboard and JSON
APIs for policy classification, local storage, read-only GitHub inspection, and
the Stage 2 work queue. Never mutates GitHub objects.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from buildforme.briefing import build_founder_briefing
from buildforme.execution_service import (
    cancel_run,
    create_run,
    execute_dry_run,
    execute_supervised,
    founder_review_decision,
    record_run_approval,
    retry_run,
    run_preflight,
)
from buildforme.github_client import GitHubClient, GitHubClientError
from buildforme.packet_generator import generate_agent_packet
from buildforme.planner import plan_project, recommendation_to_packet_input
from buildforme.policy import classify_task, validate_task_packet
from buildforme.storage import DEFAULT_STATE_PATH, LocalStore
from buildforme.work_queue import build_pr_status, build_work_queue

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ROOT = PROJECT_ROOT / "public"
SAMPLE_PROJECT_PATH = PROJECT_ROOT / "data" / "sample_project.json"


class BuildformeRequestHandler(BaseHTTPRequestHandler):
    server_version = "BuildformeMVP/0.7.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in {"/", "/public", "/public/", "/index.html"}:
            self._serve_file(PUBLIC_ROOT / "index.html")
            return
        if path.startswith("/public/"):
            relative = path.removeprefix("/public/")
            self._serve_file(PUBLIC_ROOT / relative)
            return

        if path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "buildforme",
                    "version": self.server_version,
                    "github_token_configured": bool(self._github().token),
                },
            )
            return
        if path == "/api/tasks":
            self._json(HTTPStatus.OK, {"tasks": self._store().list_tasks()})
            return
        if path == "/api/repos":
            self._json(HTTPStatus.OK, {"repositories": self._store().list_repos()})
            return
        if path == "/api/approvals":
            self._json(HTTPStatus.OK, {"approvals": self._store().list_approvals()})
            return
        if path == "/api/packets":
            self._json(HTTPStatus.OK, {"packets": self._store().list_packets()})
            return
        if path.startswith("/api/packets/"):
            packet_id = urllib.parse.unquote(path.removeprefix("/api/packets/").strip("/"))
            if packet_id and packet_id not in {"generate", "from-pr", "from-issue"}:
                try:
                    self._json(HTTPStatus.OK, {"packet": self._store().get_packet(packet_id)})
                except KeyError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
        if path == "/api/work-queue":
            self._work_queue(parsed)
            return
        if path == "/api/github/repo":
            self._github_repo(parsed)
            return
        if path == "/api/github/issues":
            self._github_issues(parsed)
            return
        if path == "/api/github/pr":
            self._github_pr(parsed)
            return
        if path.startswith("/api/pr/") and path.endswith("/status"):
            self._pr_status(path)
            return
        if path == "/api/projects":
            self._json(HTTPStatus.OK, {"projects": self._store().list_projects()})
            return
        if path == "/api/events":
            limit = _safe_int(_first_query_value(parsed, "limit"), default=100, maximum=500)
            self._json(HTTPStatus.OK, {"events": self._store().list_events(limit=limit)})
            return
        if path == "/api/briefing":
            self._json(HTTPStatus.OK, {"last_generated_at": self._store().last_briefing_at()})
            return
        if path == "/api/execution/control":
            self._json(HTTPStatus.OK, {"control": self._store().get_execution_control()})
            return
        if path == "/api/execution/policy":
            self._json(HTTPStatus.OK, {"policy": self._store().get_execution_policy()})
            return
        if path == "/api/repository-locks":
            active = (_first_query_value(parsed, "active") or "").lower() in {"1", "true", "yes"}
            self._json(
                HTTPStatus.OK,
                {"locks": self._store().list_repository_locks(active_only=active)},
            )
            return
        if path == "/api/providers":
            self._json(HTTPStatus.OK, {"providers": self._store().list_providers()})
            return
        if path == "/api/providers/health":
            from buildforme.provider_discovery import discover_all_providers

            health = discover_all_providers(self._store().list_providers())
            self._json(HTTPStatus.OK, {"providers": health})
            return
        if path == "/api/providers/recommend":
            from buildforme.provider_discovery import discover_all_providers
            from buildforme.provider_recommend import recommend_provider

            risk = _first_query_value(parsed, "risk") or "YELLOW"
            mode = _first_query_value(parsed, "mode") or "IMPLEMENTATION"
            prefer = _first_query_value(parsed, "prefer")
            caps_raw = _first_query_value(parsed, "capabilities") or ""
            caps = [c.strip() for c in caps_raw.split(",") if c.strip()]
            health = discover_all_providers(self._store().list_providers())
            result = recommend_provider(
                health=health,
                risk=risk,
                operating_mode=mode,
                requested_capabilities=caps
                or ["read_repository", "edit_repository", "run_tests", "produce_patch"],
                founder_preferences={"preferred_provider": prefer} if prefer else {},
            )
            self._json(HTTPStatus.OK, result)
            return
        if path.startswith("/api/providers/"):
            provider_id = path.removeprefix("/api/providers/").strip("/")
            if provider_id.endswith("/acknowledge-constitution"):
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "use POST"})
                return
            try:
                self._json(HTTPStatus.OK, {"provider": self._store().get_provider_record(provider_id)})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if path == "/api/constitution":
            from governance.constitution_engine import get_engine

            engine = get_engine()
            self._json(HTTPStatus.OK, engine.dashboard_payload(self._store()))
            return
        if path == "/api/constitution/status":
            from governance.constitution_engine import get_engine

            self._json(HTTPStatus.OK, {"status": get_engine().status()})
            return
        if path == "/api/constitution/laws":
            from governance.constitution_engine import get_engine

            engine = get_engine()
            self._json(
                HTTPStatus.OK,
                {
                    "version": engine.version(),
                    "hash": engine.content_hash(),
                    "laws": engine.laws(),
                },
            )
            return
        if path == "/api/constitution/violations":
            limit = _safe_int(_first_query_value(parsed, "limit"), default=100, maximum=500)
            self._json(
                HTTPStatus.OK,
                {"violations": self._store().list_constitution_violations(limit=limit)},
            )
            return
        if path == "/api/constitution/leases":
            limit = _safe_int(_first_query_value(parsed, "limit"), default=50, maximum=200)
            self._json(
                HTTPStatus.OK,
                {"leases": self._store().list_constitution_leases(limit=limit)},
            )
            return
        if path == "/api/runs":
            project_id = _first_query_value(parsed, "project_id")
            self._json(HTTPStatus.OK, {"runs": self._store().list_runs(project_id=project_id)})
            return
        if path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.removeprefix("/api/runs/").removesuffix("/events").strip("/")
            self._json(HTTPStatus.OK, {"events": self._store().list_run_events(run_id)})
            return
        if path.startswith("/api/runs/") and path.endswith("/evidence"):
            run_id = path.removeprefix("/api/runs/").removesuffix("/evidence").strip("/")
            try:
                self._json(HTTPStatus.OK, {"evidence": self._store().get_run_evidence(run_id)})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/").strip("/")
            try:
                run = self._store().get_run(run_id)
                evidence = None
                try:
                    evidence = self._store().get_run_evidence(run_id)
                except KeyError:
                    evidence = None
                self._json(
                    HTTPStatus.OK,
                    {
                        "run": run,
                        "events": self._store().list_run_events(run_id),
                        "approvals": self._store().list_run_approvals(run_id),
                        "evidence": evidence,
                    },
                )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if path.startswith("/api/projects/"):
            if self._get_project_routes(path, parsed):
                return
        if path.startswith("/api/planned-tasks/"):
            task_id = urllib.parse.unquote(path.removeprefix("/api/planned-tasks/").strip("/"))
            try:
                self._json(HTTPStatus.OK, {"task": self._store().get_planned_task(task_id)})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return

        if self._try_serve_public_asset(path):
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/classify":
            self._classify(save=False)
            return
        if path == "/api/constitution/validate":
            self._constitution_validate()
            return
        if path == "/api/constitution/refresh":
            self._constitution_refresh()
            return
        if path.startswith("/api/providers/") and path.endswith("/acknowledge-constitution"):
            provider_id = path.removeprefix("/api/providers/").removesuffix("/acknowledge-constitution").strip("/")
            self._provider_acknowledge_constitution(provider_id)
            return
        if path == "/api/tasks":
            self._classify(save=True)
            return
        if path == "/api/decisions":
            self._record_decision()
            return
        if path == "/api/repos":
            self._add_repo()
            return
        if path == "/api/approvals":
            self._add_approval()
            return
        if path == "/api/packets/generate":
            self._generate_packet()
            return
        if path == "/api/packets":
            self._save_packet()
            return
        if path == "/api/packets/from-pr":
            self._packet_from_pr()
            return
        if path == "/api/packets/from-issue":
            self._packet_from_issue()
            return
        if path == "/api/projects":
            self._upsert_project()
            return
        if path == "/api/projects/sample":
            self._load_sample_project()
            return
        if path == "/api/briefing/generate":
            self._generate_briefing()
            return
        if path.startswith("/api/projects/") and path.endswith("/stages"):
            project_id = path.removeprefix("/api/projects/").removesuffix("/stages").strip("/")
            self._upsert_stage(project_id)
            return
        if path.startswith("/api/projects/") and path.endswith("/planned-tasks"):
            project_id = path.removeprefix("/api/projects/").removesuffix("/planned-tasks").strip("/")
            self._upsert_planned_task(project_id)
            return
        if path.startswith("/api/projects/") and path.endswith("/truth"):
            project_id = path.removeprefix("/api/projects/").removesuffix("/truth").strip("/")
            self._upsert_truth(project_id)
            return
        if path.startswith("/api/projects/") and path.endswith("/plan/refresh"):
            project_id = path.removeprefix("/api/projects/").removesuffix("/plan/refresh").strip("/")
            self._plan_project(project_id, refresh=True)
            return
        if "/recommendation/" in path and path.endswith("/packet"):
            # /api/projects/{id}/recommendation/{target_id}/packet
            self._packet_from_recommendation(path)
            return
        if path == "/api/repository-locks":
            self._create_lock()
            return
        if path == "/api/runs":
            self._create_run()
            return
        if path.startswith("/api/runs/") and path.endswith("/preflight"):
            self._run_action(path, "preflight")
            return
        if path.startswith("/api/runs/") and path.endswith("/approve"):
            self._run_action(path, "approve")
            return
        if path.startswith("/api/runs/") and path.endswith("/reject"):
            self._run_action(path, "reject")
            return
        if path.startswith("/api/runs/") and path.endswith("/dry-run"):
            self._run_action(path, "dry-run")
            return
        if path.startswith("/api/runs/") and path.endswith("/execute"):
            self._run_action(path, "execute")
            return
        if path.startswith("/api/runs/") and path.endswith("/review"):
            self._run_action(path, "review")
            return
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            self._run_action(path, "cancel")
            return
        if path.startswith("/api/runs/") and path.endswith("/retry"):
            self._run_action(path, "retry")
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/execution/control":
            self._put_execution_control()
            return
        if path.startswith("/api/projects/") and path.endswith("/execution-control"):
            project_id = path.removeprefix("/api/projects/").removesuffix("/execution-control").strip("/")
            self._put_project_execution_control(project_id)
            return
        if path.startswith("/api/repository-locks/") and path.endswith("/release"):
            lock_id = path.removeprefix("/api/repository-locks/").removesuffix("/release").strip("/")
            self._release_lock(lock_id)
            return
        if path.startswith("/api/providers/"):
            provider_id = path.removeprefix("/api/providers/").strip("/")
            self._put_provider(provider_id)
            return
        if path.startswith("/api/projects/") and path.count("/") == 3:
            project_id = path.removeprefix("/api/projects/").strip("/")
            self._upsert_project(project_id=project_id)
            return
        if path.startswith("/api/stages/"):
            stage_id = path.removeprefix("/api/stages/").strip("/")
            self._upsert_stage(None, stage_id=stage_id)
            return
        if path.startswith("/api/planned-tasks/"):
            task_id = path.removeprefix("/api/planned-tasks/").strip("/")
            self._upsert_planned_task(None, task_id=task_id)
            return
        if path.startswith("/api/truth/"):
            truth_id = path.removeprefix("/api/truth/").strip("/")
            self._upsert_truth(None, truth_id=truth_id)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/packets/"):
            packet_id = urllib.parse.unquote(path.removeprefix("/api/packets/").strip("/"))
            try:
                self._store().delete_packet(packet_id)
                self._json(HTTPStatus.OK, {"deleted": packet_id})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/projects/") and path.count("/") == 3:
            project_id = path.removeprefix("/api/projects/").strip("/")
            try:
                project = self._store().set_project_status(project_id, "archived")
                self._json(HTTPStatus.OK, {"project": project, "note": "Archived (local only)."})
            except (KeyError, ValueError) as exc:
                code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
                self._json(code, {"error": str(exc)})
            return
        if path.startswith("/api/repos/"):
            encoded = path.removeprefix("/api/repos/")
            repository = urllib.parse.unquote(encoded)
            try:
                repos = self._store().remove_repo(repository)
                self._json(HTTPStatus.OK, {"repositories": repos, "removed": repository})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/repos":
            repository = _first_query_value(parsed, "repository")
            if not repository:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "repository query parameter is required"})
                return
            try:
                repos = self._store().remove_repo(repository)
                self._json(HTTPStatus.OK, {"repositories": repos, "removed": repository})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _classify(self, save: bool) -> None:
        try:
            task = self._read_json()
            if not isinstance(task, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "task packet must be a JSON object"})
                return
            problems = validate_task_packet(task)
            classification = classify_task(task).to_dict()
            payload: dict[str, Any] = {
                "valid": not problems,
                "validation_problems": problems,
                "classification": classification,
            }
            if save:
                payload["record"] = self._store().upsert_task(task, classification)
            self._json(HTTPStatus.OK, payload)
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _record_decision(self) -> None:
        try:
            payload = self._read_json()
            task_id = str(payload.get("task_id", "")).strip()
            decision = payload.get("decision")
            if not task_id or not isinstance(decision, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "task_id and decision object are required"})
                return
            record = self._store().set_decision(task_id, decision)
            self._json(HTTPStatus.OK, {"record": record})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})

    def _add_repo(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            repository = str(payload.get("repository") or "").strip()
            if not repository:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "repository is required"})
                return
            repos = self._store().add_repo(repository)
            self._json(HTTPStatus.OK, {"repositories": repos, "added": repository})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _add_approval(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            record = self._store().add_approval(payload)
            self._json(
                HTTPStatus.OK,
                {
                    "record": record,
                    "note": "Local Buildforme decision only — not a GitHub review or merge.",
                },
            )
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _generate_packet(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            packet = generate_agent_packet(payload)
            self._json(
                HTTPStatus.OK,
                {
                    "packet": packet,
                    "note": "Generated packet is an instruction set only. It does not execute agents or mutate GitHub.",
                },
            )
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"packet generation failed: {exc}"})

    def _save_packet(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else payload
            if not isinstance(packet, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "packet object required"})
                return
            # Ensure markdown present
            if not packet.get("markdown"):
                packet = generate_agent_packet({**packet, "source_type": packet.get("source_type") or "manual"})
            record = self._store().save_packet(packet)
            self._json(HTTPStatus.OK, {"packet": record})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _packet_from_pr(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            repository = str(payload.get("repository") or "").strip()
            number = int(payload.get("number") or 0)
            if not repository or number <= 0:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "repository and number are required"})
                return
            status = build_pr_status(self._github(), self._store(), repository, number)
            pr = {
                **(status.get("pull_request") or {}),
                "repository": repository,
                "files": status.get("files") or [],
                "ci": status.get("ci") or {},
                "classification": status.get("classification"),
                "recommended_action": status.get("recommended_action"),
            }
            packet = generate_agent_packet(
                {
                    "source_type": "pull_request",
                    "pull_request": pr,
                    "target_repository": repository,
                    "target_branch": payload.get("target_branch") or pr.get("head") or "main",
                    "title": payload.get("title"),
                    "objective": payload.get("objective"),
                    "context": payload.get("context"),
                    "operating_mode": payload.get("operating_mode") or "REVIEW",
                    "allowed_files": payload.get("allowed_files"),
                    "forbidden_files": payload.get("forbidden_files"),
                    "acceptance_criteria": payload.get("acceptance_criteria"),
                }
            )
            self._json(HTTPStatus.OK, {"packet": packet, "source": status})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY if isinstance(exc, GitHubClientError) else HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _packet_from_issue(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            repository = str(payload.get("repository") or "").strip()
            number = int(payload.get("number") or 0)
            if not repository or number <= 0:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "repository and number are required"})
                return
            issues = self._github().list_issues(repository, state="all", limit=50)
            match = next((item for item in issues if int(item.get("number") or 0) == number), None)
            if match is None:
                # Fallback: treat as sparse issue from number/title only
                match = {
                    "number": number,
                    "title": payload.get("title") or f"Issue #{number}",
                    "body": payload.get("body") or "",
                    "labels": payload.get("labels") or [],
                    "state": "open",
                    "repository": repository,
                }
            match = {**match, "repository": repository}
            packet = generate_agent_packet(
                {
                    "source_type": "issue",
                    "issue": match,
                    "target_repository": repository,
                    "target_branch": payload.get("target_branch") or "main",
                    "title": payload.get("title"),
                    "objective": payload.get("objective"),
                    "context": payload.get("context"),
                    "operating_mode": payload.get("operating_mode"),
                    "allowed_files": payload.get("allowed_files"),
                    "forbidden_files": payload.get("forbidden_files"),
                    "acceptance_criteria": payload.get("acceptance_criteria"),
                }
            )
            self._json(HTTPStatus.OK, {"packet": packet, "source": match})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except (GitHubClientError, ValueError) as exc:
            self._json(
                HTTPStatus.BAD_GATEWAY if isinstance(exc, GitHubClientError) else HTTPStatus.BAD_REQUEST,
                {"error": str(exc)},
            )

    def _get_project_routes(self, path: str, parsed: urllib.parse.ParseResult) -> bool:
        parts = [p for p in path.strip("/").split("/") if p]
        # api projects {id} ...
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "projects":
            return False
        project_id = urllib.parse.unquote(parts[2])
        if len(parts) == 3:
            try:
                self._json(HTTPStatus.OK, {"project": self._store().get_project(project_id)})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return True
        resource = parts[3]
        try:
            if resource == "stages":
                self._json(HTTPStatus.OK, {"stages": self._store().list_stages(project_id)})
                return True
            if resource == "planned-tasks":
                self._json(HTTPStatus.OK, {"planned_tasks": self._store().list_planned_tasks(project_id)})
                return True
            if resource == "truth":
                self._json(HTTPStatus.OK, {"truth": self._store().list_truth(project_id)})
                return True
            if resource == "events":
                self._json(HTTPStatus.OK, {"events": self._store().list_events(project_id)})
                return True
            if resource == "plan":
                self._plan_project(project_id, refresh=False)
                return True
            if resource == "recommendation":
                self._plan_project(project_id, refresh=False, recommendation_only=True)
                return True
            if resource == "execution-control":
                self._json(
                    HTTPStatus.OK,
                    {"control": self._store().get_project_execution_control(project_id)},
                )
                return True
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return True
        return False

    def _put_execution_control(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            from buildforme.governance import parse_bool_strict

            control = self._store().set_execution_control(
                kill_switch_active=parse_bool_strict(
                    payload.get("kill_switch_active"), field="kill_switch_active"
                ),
                reason=str(payload.get("reason") or ""),
                actor=str(payload.get("actor") or "shan"),
            )
            self._json(HTTPStatus.OK, {"control": control})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _put_project_execution_control(self, project_id: str) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            control = self._store().set_project_execution_control(
                project_id,
                execution_status=str(payload.get("execution_status") or "enabled"),
                reason=str(payload.get("reason") or ""),
            )
            self._json(HTTPStatus.OK, {"control": control})
        except (KeyError, ValueError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})

    def _create_lock(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            lock = self._store().create_repository_lock(payload)
            self._json(HTTPStatus.OK, {"lock": lock})
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _release_lock(self, lock_id: str) -> None:
        try:
            payload = self._read_json() if int(self.headers.get("Content-Length") or "0") else {}
            if not isinstance(payload, dict):
                payload = {}
            lock = self._store().release_repository_lock(lock_id, reason=str(payload.get("reason") or ""))
            self._json(HTTPStatus.OK, {"lock": lock})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _constitution_validate(self) -> None:
        from governance.constitution_engine import get_engine

        try:
            body = self._read_json() if self.headers.get("Content-Length") else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        engine = get_engine()
        if body.get("output") is not None:
            result = engine.validate_output(body.get("output"), context=body.get("context") or {})
            if not result.get("passed", True):
                engine.record_validation_violations(
                    self._store(),
                    result,
                    run_id=body.get("run_id"),
                    packet_id=body.get("packet_id"),
                    provider_id=body.get("provider_id"),
                    lease_id=body.get("lease_id"),
                )
            self._json(HTTPStatus.OK, result)
            return
        self._json(HTTPStatus.OK, engine.full_validation_suite(self._store()))

    def _constitution_refresh(self) -> None:
        from governance.constitution_engine import get_engine

        try:
            body = self._read_json() if int(self.headers.get("Content-Length") or 0) else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        engine = get_engine()
        store = self._store()
        provider_id = body.get("provider_id")
        targets = [provider_id] if provider_id else [p.get("provider_id") for p in store.list_providers()]
        results = []
        for pid in targets:
            if not pid:
                continue
            try:
                provider = store.get_provider_record(str(pid))
            except KeyError:
                continue
            refreshed = engine.refresh_provider(provider, actor=str(body.get("actor") or "shan"))
            saved = store.set_provider_constitution_ack(
                str(pid),
                {
                    "constitution_supported": True,
                    "constitution_acknowledged": True,
                    "constitution_version": refreshed.get("constitution_version"),
                    "constitution_hash": refreshed.get("constitution_hash"),
                    "constitution_last_refresh": refreshed.get("constitution_last_refresh"),
                    "constitution_acknowledged_at": refreshed.get("constitution_acknowledged_at"),
                    "constitution_ack_actor": refreshed.get("constitution_ack_actor"),
                },
            )
            results.append(
                {
                    "provider_id": pid,
                    "acknowledged": saved.get("constitution_acknowledged"),
                    "version": saved.get("constitution_version"),
                    "hash": saved.get("constitution_hash"),
                }
            )
        self._json(
            HTTPStatus.OK,
            {
                "version": engine.version(),
                "hash": engine.content_hash(),
                "refreshed": results,
                "reminder_sample": engine.reminder(phase="provider_refresh"),
            },
        )

    def _provider_acknowledge_constitution(self, provider_id: str) -> None:
        from governance.constitution_engine import get_engine

        try:
            body = self._read_json() if int(self.headers.get("Content-Length") or 0) else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        engine = get_engine()
        store = self._store()
        try:
            provider = store.get_provider_record(provider_id)
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        refreshed = engine.acknowledge_provider(provider, actor=str(body.get("actor") or "shan"))
        saved = store.set_provider_constitution_ack(
            provider_id,
            {
                "constitution_supported": True,
                "constitution_acknowledged": True,
                "constitution_version": refreshed.get("constitution_version"),
                "constitution_hash": refreshed.get("constitution_hash"),
                "constitution_last_refresh": refreshed.get("constitution_last_refresh"),
                "constitution_acknowledged_at": refreshed.get("constitution_acknowledged_at"),
                "constitution_ack_actor": refreshed.get("constitution_ack_actor"),
            },
        )
        self._json(
            HTTPStatus.OK,
            {
                "provider": saved,
                "full_constitution_delivered": True,
                "policy": "subsequent_executions_receive_reminder_only",
            },
        )

    def _put_provider(self, provider_id: str) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            # Reject credential fields
            for key in payload:
                low = str(key).lower()
                if any(x in low for x in ("token", "secret", "password", "credential", "api_key")):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "credential fields are not accepted"})
                    return
            provider = self._store().update_provider(provider_id, payload)
            self._json(HTTPStatus.OK, {"provider": provider})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _create_run(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            run = create_run(self._store(), payload)
            mode = run.get("execution_mode") or run.get("mode") or "dry_run"
            self._json(
                HTTPStatus.OK,
                {
                    "run": run,
                    "note": (
                        "Draft supervised run created (live_supervised). No merge/deploy authority."
                        if mode == "live_supervised"
                        else "Draft supervised run created. Dry-run default. No merge/deploy authority."
                    ),
                },
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})

    def _run_action(self, path: str, action: str) -> None:
        run_id = path.removeprefix("/api/runs/").rsplit("/", 1)[0].strip("/")
        try:
            payload = self._read_json() if int(self.headers.get("Content-Length") or "0") else {}
            if not isinstance(payload, dict):
                payload = {}
            store = self._store()
            if action == "preflight":
                result = run_preflight(store, run_id)
                self._json(HTTPStatus.OK, result)
                return
            if action == "approve":
                result = record_run_approval(
                    store,
                    run_id,
                    requirement_type=str(payload.get("requirement_type") or "shan_task_approval"),
                    decision="approved",
                    note=str(payload.get("note") or ""),
                    actor=str(payload.get("actor") or "shan"),
                )
                self._json(HTTPStatus.OK, result)
                return
            if action == "reject":
                result = record_run_approval(
                    store,
                    run_id,
                    requirement_type=str(payload.get("requirement_type") or "shan_task_approval"),
                    decision="rejected",
                    note=str(payload.get("note") or ""),
                    actor=str(payload.get("actor") or "shan"),
                )
                self._json(HTTPStatus.OK, result)
                return
            if action == "dry-run":
                result = execute_dry_run(store, run_id)
                self._json(HTTPStatus.OK, result)
                return
            if action in {"execute", "supervised"}:
                result = execute_supervised(store, run_id, repo_root=payload.get("repo_root"))
                self._json(HTTPStatus.OK, result)
                return
            if action == "review":
                result = founder_review_decision(
                    store,
                    run_id,
                    decision=str(payload.get("decision") or ""),
                    note=str(payload.get("note") or ""),
                    actor=str(payload.get("actor") or "shan"),
                    cleanup_worktree=bool(payload.get("cleanup_worktree")),
                )
                self._json(HTTPStatus.OK, result)
                return
            if action == "cancel":
                run = cancel_run(
                    store,
                    run_id,
                    actor=str(payload.get("actor") or "shan"),
                    reason=str(payload.get("reason") or ""),
                )
                self._json(HTTPStatus.OK, {"run": run})
                return
            if action == "retry":
                run = retry_run(store, run_id)
                self._json(HTTPStatus.OK, {"run": run})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown action"})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _upsert_project(self, project_id: str | None = None) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            if project_id:
                payload["id"] = project_id
            if payload.get("status") in {"paused", "active", "archived", "blocked", "completed"} and set(
                payload.keys()
            ) <= {"id", "status"}:
                project = self._store().set_project_status(str(payload["id"]), str(payload["status"]))
            else:
                project = self._store().upsert_project(payload)
            self._json(HTTPStatus.OK, {"project": project})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except (KeyError, ValueError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})

    def _upsert_stage(self, project_id: str | None, stage_id: str | None = None) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            if project_id:
                payload["project_id"] = project_id
            if stage_id:
                payload["id"] = stage_id
            stage = self._store().upsert_stage(payload)
            self._json(HTTPStatus.OK, {"stage": stage})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except (KeyError, ValueError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})

    def _upsert_planned_task(self, project_id: str | None, task_id: str | None = None) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            if project_id:
                payload["project_id"] = project_id
            if task_id:
                payload["id"] = task_id
                if "project_id" not in payload:
                    existing = self._store().get_planned_task(task_id)
                    payload["project_id"] = existing.get("project_id")
            task = self._store().upsert_planned_task(payload)
            self._json(HTTPStatus.OK, {"task": task})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except (KeyError, ValueError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})

    def _upsert_truth(self, project_id: str | None, truth_id: str | None = None) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            if project_id:
                payload["project_id"] = project_id
            if truth_id:
                payload["id"] = truth_id
            record = self._store().upsert_truth(payload)
            self._json(HTTPStatus.OK, {"truth": record})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        except (KeyError, ValueError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})

    def _github_data_for_project(self, project: dict[str, Any]) -> dict[str, Any]:
        repository = str(project.get("repository") or "").strip()
        if not repository:
            return {"available": False, "note": "No repository configured"}
        try:
            queue = build_work_queue(self._store(), self._github(), repos=[repository], pr_limit=15, issue_limit=15)
            return {
                "available": True,
                "pull_requests": queue.get("pull_requests") or [],
                "issues": queue.get("issues") or [],
                "errors": queue.get("errors") or [],
                "note": queue.get("note"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "errors": [{"error": str(exc)}], "note": "GitHub unavailable"}

    def _plan_project(self, project_id: str, *, refresh: bool, recommendation_only: bool = False) -> None:
        try:
            project = self._store().get_project(project_id)
            github = self._github_data_for_project(project) if refresh or True else {"available": False}
            plan = plan_project(project_id, self._store(), github_data=github)
            if recommendation_only:
                self._json(HTTPStatus.OK, {"recommendation": plan.get("primary_recommendation"), "plan": plan})
            else:
                self._json(HTTPStatus.OK, {"plan": plan})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _packet_from_recommendation(self, path: str) -> None:
        # /api/projects/{id}/recommendation/{target_id}/packet
        parts = [p for p in path.strip("/").split("/") if p]
        try:
            project_id = parts[2]
            target_id = parts[4]
            project = self._store().get_project(project_id)
            github = self._github_data_for_project(project)
            plan = plan_project(project_id, self._store(), github_data=github)
            match = None
            for rec in plan.get("ranked_recommendations") or []:
                if str(rec.get("target_id")) == str(target_id) or str(rec.get("id")) == str(target_id):
                    match = rec
                    break
            if match is None and str((plan.get("primary_recommendation") or {}).get("target_id")) == str(target_id):
                match = plan.get("primary_recommendation")
            if match is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"recommendation target not found: {target_id}"})
                return
            packet_input = recommendation_to_packet_input(project, match)
            packet = generate_agent_packet(packet_input)
            self._store().append_event(
                {
                    "event_type": "packet_generated_from_planner",
                    "project_id": project_id,
                    "target_type": match.get("target_type"),
                    "target_id": target_id,
                    "summary": f"Packet from planner: {match.get('headline')}",
                }
            )
            self._json(HTTPStatus.OK, {"packet": packet, "recommendation": match})
        except (KeyError, ValueError, IndexError) as exc:
            code = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
            self._json(code, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _load_sample_project(self) -> None:
        try:
            payload = self._read_json() if int(self.headers.get("Content-Length") or "0") else {}
            if not isinstance(payload, dict):
                payload = {}
            replace = bool(payload.get("replace", True))
            sample = json.loads(SAMPLE_PROJECT_PATH.read_text(encoding="utf-8"))
            project = self._store().load_sample_project(sample, replace=replace)
            self._json(HTTPStatus.OK, {"project": project, "note": "Sample/demo data loaded locally."})
        except FileNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "sample project file missing"})
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _generate_briefing(self) -> None:
        try:
            payload = self._read_json() if int(self.headers.get("Content-Length") or "0") else {}
            if not isinstance(payload, dict):
                payload = {}
            project_ids = payload.get("project_ids")
            queues: dict[str, Any] = {}
            for project in self._store().list_projects(include_archived=False):
                pid = str(project.get("id"))
                if project_ids and pid not in project_ids:
                    continue
                queues[pid] = self._github_data_for_project(project)
            briefing = build_founder_briefing(self._store(), project_ids=project_ids, queues=queues)
            self._json(HTTPStatus.OK, {"briefing": briefing})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _work_queue(self, parsed: urllib.parse.ParseResult) -> None:
        repos_param = _first_query_value(parsed, "repos")
        repos: list[str] | None = None
        if repos_param:
            repos = [part.strip() for part in repos_param.split(",") if part.strip()]
        try:
            payload = build_work_queue(self._store(), self._github(), repos=repos)
            self._json(HTTPStatus.OK, payload)
        except Exception as exc:  # noqa: BLE001 — never crash the supervisor process
            self._json(
                HTTPStatus.OK,
                {
                    "repos": [],
                    "watched_repositories": repos or [],
                    "summary": {
                        "open_prs": 0,
                        "open_issues": 0,
                        "ci_failures": 0,
                        "blocked": 0,
                        "ready_for_review": 0,
                        "safe_next_tasks": 0,
                    },
                    "pull_requests": [],
                    "issues": [],
                    "recommended_next_task": {
                        "priority": 7,
                        "headline": "Work queue unavailable",
                        "detail": str(exc),
                        "recommended_action": "Retry refresh or check GitHub connectivity.",
                    },
                    "errors": [{"error": str(exc)}],
                    "github_token_configured": bool(self._github().token),
                },
            )

    def _pr_status(self, path: str) -> None:
        # /api/pr/{owner}/{repo}/{number}/status
        parts = path.strip("/").split("/")
        if len(parts) != 6 or parts[0] != "api" or parts[1] != "pr" or parts[5] != "status":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        owner, repo_name, number_raw = parts[2], parts[3], parts[4]
        repository = f"{owner}/{repo_name}"
        try:
            number = int(number_raw)
            payload = build_pr_status(self._github(), self._store(), repository, number)
            self._json(HTTPStatus.OK, payload)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except GitHubClientError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _github_repo(self, parsed: urllib.parse.ParseResult) -> None:
        repository = _first_query_value(parsed, "repository")
        if not repository:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "repository query parameter is required"})
            return
        try:
            self._json(HTTPStatus.OK, {"repo": self._github().get_repo(repository)})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _github_issues(self, parsed: urllib.parse.ParseResult) -> None:
        repository = _first_query_value(parsed, "repository")
        state = _first_query_value(parsed, "state") or "open"
        limit = _safe_int(_first_query_value(parsed, "limit"), default=20, maximum=50)
        if not repository:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "repository query parameter is required"})
            return
        try:
            issues = self._github().list_issues(repository, state=state, limit=limit)
            self._json(HTTPStatus.OK, {"issues": issues})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _github_pr(self, parsed: urllib.parse.ParseResult) -> None:
        repository = _first_query_value(parsed, "repository")
        number = _first_query_value(parsed, "number")
        if not repository or not number:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "repository and number query parameters are required"},
            )
            return
        try:
            pr_number = int(number)
            pr = self._github().get_pull_request(repository, pr_number)
            files = self._github().list_pull_request_files(repository, pr_number)
            self._json(HTTPStatus.OK, {"pull_request": pr, "files": files})
        except (GitHubClientError, ValueError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _try_serve_public_asset(self, request_path: str) -> bool:
        relative = request_path.lstrip("/")
        if not relative or relative.startswith("api/") or ".." in relative.split("/"):
            return False
        candidate = (PUBLIC_ROOT / relative).resolve()
        public_root = PUBLIC_ROOT.resolve()
        try:
            candidate.relative_to(public_root)
        except ValueError:
            return False
        if not candidate.is_file():
            return False
        self._serve_file(candidate)
        return True

    def _serve_file(self, path: Path) -> None:
        resolved = path.resolve()
        public_root = PUBLIC_ROOT.resolve()
        if public_root not in resolved.parents and resolved != (public_root / "index.html"):
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not resolved.exists() or not resolved.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        content_type = _content_type_for(resolved)
        body = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "null")

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _store(self) -> LocalStore:
        state_path = getattr(self.server, "state_path", DEFAULT_STATE_PATH)  # type: ignore[attr-defined]
        return LocalStore(state_path)

    def _github(self) -> GitHubClient:
        return GitHubClient.from_env()


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    explicit = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }
    if suffix in explicit:
        return explicit[suffix]
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


def _first_query_value(parsed: urllib.parse.ParseResult, key: str) -> str | None:
    values = urllib.parse.parse_qs(parsed.query).get(key)
    if not values:
        return None
    return values[0]


def _safe_int(value: str | None, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return min(max(parsed, 1), maximum)


def run(host: str = "127.0.0.1", port: int = 8787, state_path: str | Path = DEFAULT_STATE_PATH) -> None:
    server = ThreadingHTTPServer((host, port), BuildformeRequestHandler)
    server.state_path = Path(state_path)  # type: ignore[attr-defined]
    print(f"Buildforme running at http://{host}:{port}")
    print(f"State file: {Path(state_path)}")
    print("GitHub access: read-only (no merge, labels, comments, or PR writes)")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Buildforme supervisor server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    args = parser.parse_args(argv)
    run(host=args.host, port=args.port, state_path=args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
