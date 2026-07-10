"""Discover provider CLI executables and health without exposing secrets."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from buildforme.storage import utc_now_iso

# Provider → candidate executable names (Windows + POSIX).
PROVIDER_EXECUTABLES: dict[str, list[str]] = {
    "codex": ["codex", "codex.exe"],
    "claude": ["claude", "claude.exe"],
    "grok": ["grok", "grok.exe", "xai"],
    "glm": ["glm", "glm.exe", "zhipu"],
}

# Safe, non-secret version probes (argv lists only).
VERSION_ARGS: dict[str, list[str]] = {
    "codex": ["--version"],
    "claude": ["--version"],
    "grok": ["--version"],
    "glm": ["--version"],
}

# Auth-readiness probes that must never print tokens; we only keep exit codes / coarse status.
AUTH_PROBES: dict[str, list[str] | None] = {
    "codex": None,  # environment / login state opaque
    "claude": None,
    "grok": None,
    "glm": None,
}


def discover_executable(provider_id: str, *, extra_paths: list[str] | None = None) -> dict[str, Any]:
    """Locate a provider CLI on PATH (or extra paths). Never invent success."""
    pid = str(provider_id or "").strip().lower()
    candidates = list(PROVIDER_EXECUTABLES.get(pid) or [pid])
    search_path = os.environ.get("PATH", "")
    if extra_paths:
        search_path = os.pathsep.join(list(extra_paths) + [search_path])

    found: str | None = None
    for name in candidates:
        path = shutil.which(name, path=search_path)
        if path:
            found = path
            break
        # Also check explicit extra path files
        for root in extra_paths or []:
            for name2 in candidates:
                candidate = Path(root) / name2
                if candidate.is_file():
                    found = str(candidate)
                    break
            if found:
                break
        if found:
            break

    return {
        "provider_id": pid,
        "executable": found,
        "available": bool(found),
        "transport": "cli",
        "discovered_at": utc_now_iso(),
        "candidates": candidates,
    }


def probe_version(executable: str | None, provider_id: str, *, timeout_sec: float = 8.0) -> dict[str, Any]:
    if not executable:
        return {"version": None, "version_ok": False, "detail": "executable missing"}
    args = [executable] + list(VERSION_ARGS.get(provider_id) or ["--version"])
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return {"version": None, "version_ok": False, "detail": "file not found"}
    except subprocess.TimeoutExpired:
        return {"version": None, "version_ok": False, "detail": "version probe timed out"}
    except OSError as exc:
        return {"version": None, "version_ok": False, "detail": f"os error: {exc}"}

    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    # Bound and scrub possible secret-like lines
    lines = [ln for ln in out.splitlines() if not _looks_secret(ln)][:5]
    version_text = " | ".join(lines)[:200] if lines else None
    return {
        "version": version_text,
        "version_ok": proc.returncode == 0 and bool(version_text),
        "exit_code": proc.returncode,
        "detail": "ok" if proc.returncode == 0 else f"exit {proc.returncode}",
    }


def health_check_provider(
    provider_id: str,
    provider_record: dict[str, Any] | None = None,
    *,
    extra_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Full health snapshot for one provider (no secrets)."""
    pid = str(provider_id or "").strip().lower()
    disc = discover_executable(pid, extra_paths=extra_paths)
    version = probe_version(disc.get("executable"), pid)
    record = provider_record or {}
    enabled = bool(record.get("enabled", True))
    constitution_ack = bool(record.get("constitution_acknowledged"))
    auth_ready = _auth_readiness(pid, disc.get("executable"))

    # Live readiness requires proven auth readiness — unknown is NOT live-ready.
    live_ready = bool(
        disc["available"]
        and version.get("version_ok")
        and enabled
        and constitution_ack
        and auth_ready.get("status") == "ready"
    )

    unsupported_reasons: list[str] = []
    if not disc["available"]:
        unsupported_reasons.append("executable not found on PATH")
    if disc["available"] and not version.get("version_ok"):
        unsupported_reasons.append(f"version probe failed: {version.get('detail')}")
    if not enabled:
        unsupported_reasons.append("provider disabled")
    if not constitution_ack:
        unsupported_reasons.append("constitution not acknowledged")
    if auth_ready.get("status") == "missing":
        unsupported_reasons.append("authentication not ready")
    elif auth_ready.get("status") == "unknown":
        unsupported_reasons.append("authentication unknown (not treated as live-ready)")

    if live_ready:
        status = "available"
    elif disc["available"]:
        status = "discovered"
    else:
        status = "unavailable"

    return {
        "provider_id": pid,
        "display_name": record.get("display_name") or pid,
        "transport": "cli",
        "status": status,
        "available": disc["available"],
        "live_ready": live_ready,
        "enabled": enabled,
        "executable": disc.get("executable"),
        "version": version.get("version"),
        "version_ok": version.get("version_ok"),
        "auth": auth_ready,
        "constitution_acknowledged": constitution_ack,
        "constitution_version": record.get("constitution_version"),
        "capabilities": list(record.get("capabilities") or []),
        "mode": record.get("mode") or "dry_run",
        "unsupported_reasons": unsupported_reasons,
        "checked_at": utc_now_iso(),
        # Never include env tokens
        "secrets_exposed": False,
    }


def discover_all_providers(
    provider_records: list[dict[str, Any]],
    *,
    extra_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    by_id = {str(p.get("provider_id")): p for p in provider_records}
    results = []
    for pid in ("codex", "claude", "grok", "glm"):
        results.append(health_check_provider(pid, by_id.get(pid), extra_paths=extra_paths))
    return results


def _auth_readiness(provider_id: str, executable: str | None) -> dict[str, Any]:
    """Auth readiness without secret material.

    ready   = approved env marker present (value never stored)
    missing = no executable
    unknown = executable present but auth not verified — NOT live-ready
    """
    if not executable:
        return {"status": "missing", "detail": "no executable to authenticate"}
    env_hints = {
        "codex": ["OPENAI_API_KEY", "CODEX_API_KEY"],
        "claude": ["ANTHROPIC_API_KEY"],
        "grok": ["XAI_API_KEY", "GROK_API_KEY"],
        "glm": ["ZHIPUAI_API_KEY", "GLM_API_KEY"],
    }
    present = any(bool(os.environ.get(k)) for k in env_hints.get(provider_id, []))
    if present:
        return {
            "status": "ready",
            "detail": "auth environment marker present (value not read into storage)",
            "marker_names": [k for k in env_hints.get(provider_id, []) if os.environ.get(k)],
        }
    return {
        "status": "unknown",
        "detail": "no verified auth marker; CLI login cache not accepted as live-ready",
        "marker_names": [],
    }


def _looks_secret(line: str) -> bool:
    low = line.lower()
    return any(
        x in low
        for x in (
            "api_key",
            "apikey",
            "token=",
            "secret=",
            "password",
            "authorization:",
            "bearer ",
        )
    )
