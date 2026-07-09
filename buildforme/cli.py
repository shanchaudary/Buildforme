"""Command-line interface for Buildforme policy checks and local server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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


def serve_command(args: argparse.Namespace) -> int:
    from buildforme.server import run

    run(host=args.host, port=args.port, state_path=args.state)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Buildforme supervisor CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser("classify", help="Classify a task packet JSON file")
    classify_parser.add_argument("path", nargs="?", default="-", help="Path to JSON task packet, or '-' for stdin")
    classify_parser.set_defaults(func=classify_command)

    serve_parser = subparsers.add_parser("serve", help="Run the local supervisor server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    serve_parser.add_argument("--port", type=int, default=8787, help="Port to bind")
    serve_parser.add_argument("--state", default="runtime/buildforme_state.json", help="Local JSON state path")
    serve_parser.set_defaults(func=serve_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
