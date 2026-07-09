"""Command-line interface for Buildforme policy checks, packets, and local server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from buildforme.packet_generator import generate_agent_packet, packet_from_task
from buildforme.policy import classify_task, validate_task_packet


def load_task(path: str | None) -> dict[str, Any]:
    if path in (None, "-"):
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Task packet must be a JSON object")
    return data


def classify_command(args: argparse.Namespace) -> int:
    task = load_task(args.path)
    problems = validate_task_packet(task)
    classification = classify_task(task)
    output = {
        "valid": not problems,
        "validation_problems": problems,
        "classification": classification.to_dict(),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if classification.risk.value in {"GREEN", "YELLOW"} else 2


def generate_packet_command(args: argparse.Namespace) -> int:
    task = load_task(args.path)
    packet = packet_from_task(task)
    if args.json:
        print(json.dumps(packet, indent=2, sort_keys=True))
    else:
        print(packet.get("markdown") or "")
    risk = str(packet.get("risk") or "RED")
    return 0 if risk in {"GREEN", "YELLOW"} else 2


def serve_command(args: argparse.Namespace) -> int:
    from buildforme.server import run

    run(host=args.host, port=args.port, state_path=args.state)
    return 0


def load_sample_project_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    sample_path = Path(__file__).resolve().parent.parent / "data" / "sample_project.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    store = LocalStore(args.state)
    project = store.load_sample_project(sample, replace=bool(args.replace))
    print(json.dumps({"project": project, "note": "Sample project loaded locally"}, indent=2))
    return 0


def plan_command(args: argparse.Namespace) -> int:
    from buildforme.planner import plan_project
    from buildforme.storage import LocalStore
    from buildforme.work_queue import build_work_queue
    from buildforme.github_client import GitHubClient

    store = LocalStore(args.state)
    project = store.get_project(args.project_id)
    github = {"available": False}
    if not args.local_only:
        try:
            queue = build_work_queue(store, GitHubClient.from_env(), repos=[project["repository"]])
            github = {
                "available": True,
                "pull_requests": queue.get("pull_requests") or [],
                "issues": queue.get("issues") or [],
                "errors": queue.get("errors") or [],
            }
        except Exception as exc:  # noqa: BLE001
            github = {"available": False, "errors": [{"error": str(exc)}]}
    plan = plan_project(args.project_id, store, github_data=github)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        primary = plan.get("primary_recommendation") or {}
        print(f"Project: {args.project_id}")
        print(f"Confidence: {plan.get('confidence')}")
        print(f"Primary: {primary.get('headline')}")
        print(f"Risk: {primary.get('risk')} · Score: {primary.get('total_score')}")
        print(f"Requires Shan: {primary.get('requires_shan')}")
        for line in primary.get("explanation") or primary.get("reasoning") or []:
            print(f"  - {line}")
        print("\nTop ranked:")
        for rec in (plan.get("ranked_recommendations") or [])[:5]:
            print(f"  {rec.get('rank')}. [{rec.get('total_score')}] {rec.get('headline')} ({rec.get('risk')})")
    return 0


def briefing_command(args: argparse.Namespace) -> int:
    from buildforme.briefing import build_founder_briefing
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    briefing = build_founder_briefing(store, project_ids=args.project_id)
    if args.json:
        print(json.dumps(briefing, indent=2, sort_keys=True))
    else:
        summary = briefing.get("summary") or {}
        print("Founder briefing")
        print(f"Generated: {briefing.get('generated_at')}")
        print(f"Active projects: {summary.get('active_projects')} · Needs Shan: {summary.get('needs_shan')}")
        print(f"Open PRs: {summary.get('open_prs')} · Failing CI: {summary.get('failing_ci')}")
        print(briefing.get("completed_note"))
        print("\nRecommended next:")
        for item in briefing.get("recommended_next") or []:
            print(f"  - [{item.get('project_id')}] {item.get('headline')}")
        print("\nNeeds Shan:")
        for item in briefing.get("needs_shan") or []:
            print(f"  - [{item.get('project_id')}] {item.get('headline')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Buildforme supervisor CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser("classify", help="Classify a task packet JSON file")
    classify_parser.add_argument("path", nargs="?", default="-", help="Path to JSON task packet, or '-' for stdin")
    classify_parser.set_defaults(func=classify_command)

    packet_parser = subparsers.add_parser(
        "generate-packet",
        aliases=["packet"],
        help="Generate a tool-neutral agent handoff packet (Markdown) from a task JSON file",
    )
    packet_parser.add_argument("path", nargs="?", default="-", help="Path to JSON task packet, or '-' for stdin")
    packet_parser.add_argument("--json", action="store_true", help="Print full packet JSON instead of Markdown")
    packet_parser.set_defaults(func=generate_packet_command)

    serve_parser = subparsers.add_parser("serve", help="Run the local supervisor server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    serve_parser.add_argument("--port", type=int, default=8787, help="Port to bind")
    serve_parser.add_argument("--state", default="runtime/buildforme_state.json", help="Local JSON state path")
    serve_parser.set_defaults(func=serve_command)

    sample_parser = subparsers.add_parser("load-sample-project", help="Load Buildforme sample project into local runtime")
    sample_parser.add_argument("--state", default="runtime/buildforme_state.json")
    sample_parser.add_argument("--replace", action="store_true", default=True)
    sample_parser.set_defaults(func=load_sample_project_command)

    plan_parser = subparsers.add_parser("plan", help="Run chief planner for a project")
    plan_parser.add_argument("project_id", help="Project id (e.g. buildforme)")
    plan_parser.add_argument("--state", default="runtime/buildforme_state.json")
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.add_argument("--local-only", action="store_true", help="Skip GitHub read-only fetch")
    plan_parser.set_defaults(func=plan_command)

    briefing_parser = subparsers.add_parser("briefing", help="Generate founder briefing")
    briefing_parser.add_argument("--project-id", action="append", dest="project_id")
    briefing_parser.add_argument("--state", default="runtime/buildforme_state.json")
    briefing_parser.add_argument("--json", action="store_true")
    briefing_parser.set_defaults(func=briefing_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
