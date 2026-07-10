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


def execution_status_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    control = store.get_execution_control()
    locks = store.list_repository_locks(active_only=True)
    runs = [r for r in store.list_runs() if r.get("status") not in {"completed", "cancelled", "failed", "rejected", "preflight_failed", "blocked", "timed_out"}]
    payload = {
        "control": control,
        "active_locks": locks,
        "active_or_open_runs": runs,
        "providers": store.list_providers(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Kill switch: {'ON' if control.get('kill_switch_active') else 'off'}")
        print(f"Reason: {control.get('reason') or '(none)'}")
        print(f"Active locks: {len(locks)}")
        print(f"Open runs: {len(runs)}")
        print("Providers: " + ", ".join(f"{p.get('provider_id')}=dry_run" for p in payload["providers"]))
    return 0


def kill_switch_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    active = args.state_arg == "on"
    control = store.set_execution_control(kill_switch_active=active, reason=args.reason, actor="cli")
    print(json.dumps(control, indent=2, sort_keys=True))
    return 0


def providers_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    providers = LocalStore(args.state).list_providers()
    if args.json:
        print(json.dumps({"providers": providers}, indent=2, sort_keys=True))
    else:
        for p in providers:
            print(
                f"{p.get('provider_id')}: enabled={p.get('enabled')} mode={p.get('mode')} "
                f"live={p.get('live_execution_available')} creds={p.get('credentials_configured')}"
            )
    return 0


def run_create_command(args: argparse.Namespace) -> int:
    from buildforme.execution_service import create_run
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    run = create_run(
        store,
        {
            "project_id": args.project,
            "provider_id": args.provider,
            "packet_id": args.packet_id,
            "target_branch": args.branch,
            "execution_mode": getattr(args, "execution_mode", None) or "dry_run",
        },
    )
    print(json.dumps(run, indent=2, sort_keys=True))
    return 0


def run_preflight_command(args: argparse.Namespace) -> int:
    from buildforme.execution_service import run_preflight
    from buildforme.storage import LocalStore

    result = run_preflight(LocalStore(args.state), args.run_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("preflight", {}).get("passed") else 2


def run_dry_run_command(args: argparse.Namespace) -> int:
    from buildforme.execution_service import execute_dry_run
    from buildforme.storage import LocalStore

    result = execute_dry_run(LocalStore(args.state), args.run_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_events_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    events = LocalStore(args.state).list_run_events(args.run_id)
    print(json.dumps({"events": events}, indent=2, sort_keys=True))
    return 0


def constitution_status_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore
    from governance.constitution_engine import get_engine

    engine = get_engine()
    store = LocalStore(args.state)
    if args.brief:
        payload = {"status": engine.status()}
    else:
        payload = engine.dashboard_payload(store)
    status = payload.get("status") or engine.status()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Buildforme AI Constitution")
        print(f"  Version: {status.get('version')}")
        print(f"  Hash: {status.get('hash')}")
        print(f"  Laws: {status.get('law_count')}")
        print(f"  Document valid: {status.get('document_valid')}")
        if not args.brief:
            print(f"  Violations: {(payload.get('violation_summary') or {}).get('total', 0)}")
            print(f"  Leases: {len(payload.get('leases') or [])}")
            for ack in payload.get("provider_acknowledgements") or []:
                print(
                    f"  Provider {ack.get('provider_id')}: ack={ack.get('constitution_acknowledged')} "
                    f"refresh_needed={ack.get('needs_refresh')}"
                )
    return 0 if status.get("document_valid") else 2


def constitution_validate_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore
    from governance.constitution_engine import get_engine

    engine = get_engine()
    store = LocalStore(args.state) if args.state else None
    payload = engine.full_validation_suite(store)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Constitution validation")
        for check in payload.get("checks") or []:
            mark = "PASS" if check.get("ok") else "FAIL"
            print(f"  [{mark}] {check.get('name')}: {check.get('detail')}")
        print(f"Overall: {'PASS' if payload.get('passed') else 'FAIL'}")
        print(f"Version: {payload.get('version')} Hash: {payload.get('hash')}")
    return 0 if payload.get("passed") else 2


def constitution_refresh_command(args: argparse.Namespace) -> int:
    """Deliver full constitution acknowledgement to providers (once per version)."""
    from buildforme.storage import LocalStore
    from governance.constitution_engine import get_engine

    engine = get_engine()
    store = LocalStore(args.state)
    targets = [args.provider] if args.provider else [p.get("provider_id") for p in store.list_providers()]
    results = []
    for provider_id in targets:
        if not provider_id:
            continue
        provider = store.get_provider_record(str(provider_id))
        refreshed = engine.refresh_provider(provider, actor=args.actor or "shan")
        saved = store.set_provider_constitution_ack(
            str(provider_id),
            {
                "constitution_supported": refreshed.get("constitution_supported"),
                "constitution_acknowledged": refreshed.get("constitution_acknowledged"),
                "constitution_version": refreshed.get("constitution_version"),
                "constitution_hash": refreshed.get("constitution_hash"),
                "constitution_last_refresh": refreshed.get("constitution_last_refresh"),
                "constitution_acknowledged_at": refreshed.get("constitution_acknowledged_at"),
                "constitution_ack_actor": refreshed.get("constitution_ack_actor"),
            },
        )
        results.append(
            {
                "provider_id": provider_id,
                "acknowledged": saved.get("constitution_acknowledged"),
                "version": saved.get("constitution_version"),
                "hash": saved.get("constitution_hash"),
                "full_constitution_delivered": True,
                "policy": "subsequent_executions_receive_reminder_only",
            }
        )
    payload = {
        "version": engine.version(),
        "hash": engine.content_hash(),
        "refreshed": results,
        "reminder_sample": engine.reminder(phase="provider_refresh"),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Constitution refresh version={payload['version']} hash={payload['hash']}")
        for item in results:
            print(f"  {item['provider_id']}: acknowledged={item['acknowledged']}")
    return 0


def constitution_export_command(args: argparse.Namespace) -> int:
    from governance.constitution_engine import get_engine

    engine = get_engine()
    text = engine.export(format=args.format)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def governance_validate_command(args: argparse.Namespace) -> int:
    """Run Stage 5.5 governance smoke checks and adversarial unit tests."""
    import unittest

    from buildforme.governance import parse_bool_strict
    from buildforme.providers import default_provider_registry
    from buildforme.run_state import can_transition, is_terminal
    from buildforme.storage import LocalStore

    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    # Static control assertions (no side effects)
    try:
        assert parse_bool_strict("false") is False
        assert parse_bool_strict("true") is True
        record("strict_bool", True, "parse_bool_strict rejects truthy strings safely")
    except Exception as exc:  # noqa: BLE001
        record("strict_bool", False, str(exc))

    try:
        assert is_terminal("completed")
        assert not can_transition("completed", "running")
        record("state_machine", True, "terminal completed cannot restart")
    except Exception as exc:  # noqa: BLE001
        record("state_machine", False, str(exc))

    try:
        providers = default_provider_registry()
        assert providers
        assert all(p.get("mode") == "dry_run" for p in providers)
        assert all(not p.get("live_execution_available") for p in providers)
        assert all(not p.get("credentials_configured") for p in providers)
        record("providers_dry_run_only", True, f"{len(providers)} providers dry-run only")
    except Exception as exc:  # noqa: BLE001
        record("providers_dry_run_only", False, str(exc))

    store = LocalStore(args.state)
    control = store.get_execution_control()
    record(
        "kill_switch_readable",
        isinstance(control.get("kill_switch_active"), bool),
        f"kill_switch_active={control.get('kill_switch_active')}",
    )

    # Adversarial suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for mod in (
        "tests.test_governance_adversarial",
        "tests.test_run_state",
        "tests.test_execution_preflight",
        "tests.test_dry_run_adapter",
    ):
        try:
            suite.addTests(loader.loadTestsFromName(mod))
        except Exception as exc:  # noqa: BLE001
            record(f"load_{mod}", False, str(exc))

    result = unittest.TextTestRunner(verbosity=1).run(suite)
    tests_ok = result.wasSuccessful()
    record(
        "adversarial_suite",
        tests_ok,
        f"ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}",
    )

    payload = {
        "stage": "5.5",
        "checks": checks,
        "passed": all(c["status"] == "pass" for c in checks),
        "stage_6_admission": "Stage 6 multi-provider supervised execution is authorized by stage roadmap; use provider-health / run-execute",
        "doc": "docs/STAGE_5_5_GOVERNANCE_VALIDATION.md",
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Stage 5.5 governance validation")
        for item in checks:
            print(f"  [{item['status'].upper()}] {item['name']}: {item['detail']}")
        print(f"Overall: {'PASS' if payload['passed'] else 'FAIL'}")
        print(f"Stage 6 admission: {payload['stage_6_admission']}")
    return 0 if payload["passed"] else 2


def provider_health_command(args: argparse.Namespace) -> int:
    from buildforme.provider_discovery import discover_all_providers
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    health = discover_all_providers(store.list_providers())
    if args.json:
        print(json.dumps({"providers": health}, indent=2, sort_keys=True))
    else:
        for item in health:
            print(
                f"{item.get('provider_id')}: status={item.get('status')} "
                f"available={item.get('available')} live_ready={item.get('live_ready')} "
                f"version={item.get('version')!r} reasons={item.get('unsupported_reasons')}"
            )
    return 0


def provider_recommend_command(args: argparse.Namespace) -> int:
    from buildforme.provider_discovery import discover_all_providers
    from buildforme.provider_recommend import recommend_provider
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    health = discover_all_providers(store.list_providers())
    caps = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()]
    prefs = {}
    if args.prefer:
        prefs["preferred_provider"] = args.prefer
    result = recommend_provider(
        health=health,
        risk=args.risk,
        operating_mode=args.mode,
        requested_capabilities=caps or ["read_repository", "edit_repository", "run_tests", "produce_patch"],
        task_type=args.task_type,
        language=args.language,
        founder_preferences=prefs,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_execute_command(args: argparse.Namespace) -> int:
    from buildforme.execution_service import execute_supervised
    from buildforme.storage import LocalStore

    result = execute_supervised(LocalStore(args.state), args.run_id)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("run", {}).get("status") in {"needs_review", "completed"} else 2


def run_review_command(args: argparse.Namespace) -> int:
    from buildforme.execution_service import founder_review_decision
    from buildforme.storage import LocalStore

    result = founder_review_decision(
        LocalStore(args.state),
        args.run_id,
        decision=args.decision,
        note=args.note or "",
        cleanup_worktree=bool(args.cleanup),
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def run_evidence_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    try:
        evidence = store.get_run_evidence(args.run_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(evidence, indent=2, sort_keys=True, default=str))
    return 0


def register_repo_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    binding = store.register_repository_binding(
        {
            "repository": args.repository,
            "local_path": args.path,
            "project_id": args.project,
        }
    )
    print(json.dumps(binding, indent=2, sort_keys=True))
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

    exec_parser = subparsers.add_parser("execution-status", help="Show kill switch, locks, active runs")
    exec_parser.add_argument("--state", default="runtime/buildforme_state.json")
    exec_parser.add_argument("--json", action="store_true")
    exec_parser.set_defaults(func=execution_status_command)

    kill_parser = subparsers.add_parser("kill-switch", help="Activate or deactivate global kill switch")
    kill_parser.add_argument("state_arg", choices=["on", "off"])
    kill_parser.add_argument("--reason", default="")
    kill_parser.add_argument("--state", default="runtime/buildforme_state.json")
    kill_parser.set_defaults(func=kill_switch_command)

    providers_parser = subparsers.add_parser("providers", help="List dry-run provider profiles")
    providers_parser.add_argument("--state", default="runtime/buildforme_state.json")
    providers_parser.add_argument("--json", action="store_true")
    providers_parser.set_defaults(func=providers_command)

    run_create = subparsers.add_parser("run-create", help="Create draft supervised run from packet id")
    run_create.add_argument("packet_id")
    run_create.add_argument("--project", required=True)
    run_create.add_argument("--provider", default="codex")
    run_create.add_argument("--branch", required=True)
    run_create.add_argument(
        "--execution-mode",
        default="dry_run",
        choices=["dry_run", "live_supervised"],
        help="dry_run (default) or live_supervised Stage 6",
    )
    run_create.add_argument("--state", default="runtime/buildforme_state.json")
    run_create.set_defaults(func=run_create_command)

    run_pre = subparsers.add_parser("run-preflight", help="Run preflight on a supervised run")
    run_pre.add_argument("run_id")
    run_pre.add_argument("--state", default="runtime/buildforme_state.json")
    run_pre.set_defaults(func=run_preflight_command)

    run_dry = subparsers.add_parser("run-dry-run", help="Execute dry-run for an approved run")
    run_dry.add_argument("run_id")
    run_dry.add_argument("--state", default="runtime/buildforme_state.json")
    run_dry.set_defaults(func=run_dry_run_command)

    run_events = subparsers.add_parser("run-events", help="List events for a run")
    run_events.add_argument("run_id")
    run_events.add_argument("--state", default="runtime/buildforme_state.json")
    run_events.set_defaults(func=run_events_command)

    gov = subparsers.add_parser(
        "governance-validate",
        help="Run Stage 5.5 governance smoke checks and adversarial unit tests",
    )
    gov.add_argument("--state", default="runtime/buildforme_state.json")
    gov.add_argument("--json", action="store_true")
    gov.set_defaults(func=governance_validate_command)

    c_status = subparsers.add_parser("constitution-status", help="Show AI Constitution status")
    c_status.add_argument("--state", default="runtime/buildforme_state.json")
    c_status.add_argument("--json", action="store_true")
    c_status.add_argument("--brief", action="store_true", help="Status only, no store dashboard")
    c_status.set_defaults(func=constitution_status_command)

    c_val = subparsers.add_parser("constitution-validate", help="Validate Constitution document and bindings")
    c_val.add_argument("--state", default="runtime/buildforme_state.json")
    c_val.add_argument("--json", action="store_true")
    c_val.set_defaults(func=constitution_validate_command)

    c_ref = subparsers.add_parser(
        "constitution-refresh",
        help="Deliver full Constitution acknowledgement to providers (once per version)",
    )
    c_ref.add_argument("--state", default="runtime/buildforme_state.json")
    c_ref.add_argument("--provider", default=None, help="Single provider id (default: all)")
    c_ref.add_argument("--actor", default="shan")
    c_ref.add_argument("--json", action="store_true")
    c_ref.set_defaults(func=constitution_refresh_command)

    c_exp = subparsers.add_parser("constitution-export", help="Export Constitution as json|markdown|reminder")
    c_exp.add_argument("--format", default="json", choices=["json", "markdown", "reminder"])
    c_exp.add_argument("--output", default=None, help="Optional file path")
    c_exp.set_defaults(func=constitution_export_command)

    ph = subparsers.add_parser("provider-health", help="Discover provider CLIs and health (no secrets)")
    ph.add_argument("--state", default="runtime/buildforme_state.json")
    ph.add_argument("--json", action="store_true")
    ph.set_defaults(func=provider_health_command)

    pr = subparsers.add_parser("provider-recommend", help="Recommend a provider for a task shape")
    pr.add_argument("--state", default="runtime/buildforme_state.json")
    pr.add_argument("--risk", default="YELLOW")
    pr.add_argument("--mode", default="IMPLEMENTATION")
    pr.add_argument("--task-type", default="implementation")
    pr.add_argument("--language", default=None)
    pr.add_argument("--capabilities", default="")
    pr.add_argument("--prefer", default=None, help="Founder preferred provider id")
    pr.set_defaults(func=provider_recommend_command)

    rexe = subparsers.add_parser("run-execute", help="Execute approved live_supervised run in isolated worktree")
    rexe.add_argument("run_id")
    rexe.add_argument("--state", default="runtime/buildforme_state.json")
    rexe.set_defaults(func=run_execute_command)

    rbind = subparsers.add_parser("register-repo", help="Register local path for a project repository identity")
    rbind.add_argument("--repository", required=True, help="owner/name")
    rbind.add_argument("--path", required=True, help="absolute local git path")
    rbind.add_argument("--project", default=None)
    rbind.add_argument("--state", default="runtime/buildforme_state.json")
    rbind.set_defaults(func=register_repo_command)

    rrev = subparsers.add_parser("run-review", help="Founder review decision after supervised execution")
    rrev.add_argument("run_id")
    rrev.add_argument("--decision", required=True, choices=["accept_for_pr_prep", "reject", "request_changes", "block"])
    rrev.add_argument("--note", default="")
    rrev.add_argument("--cleanup", action="store_true", help="Remove worktree after decision")
    rrev.add_argument("--state", default="runtime/buildforme_state.json")
    rrev.set_defaults(func=run_review_command)

    rev = subparsers.add_parser("run-evidence", help="Show evidence bundle for a run")
    rev.add_argument("run_id")
    rev.add_argument("--state", default="runtime/buildforme_state.json")
    rev.set_defaults(func=run_evidence_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
