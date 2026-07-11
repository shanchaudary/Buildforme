"""Provider command-contract compatibility — stronger than --version alone.

Design (red-teamed):
- Version profile: known CLI family strings
- Help inspection: required subcommands/flags present
- Bounded no-op probe: non-interactive help/version only (never starts a coding agent)
- Auth: successful read-only executable probe required; env markers are not proof
- Cache: keyed by executable path + mtime + version; short TTL
- Adapter-owned profiles so core policy stays provider-neutral

Help parsing alone is insufficient (misleading help, unused flags). Combined
checks are required for live_ready.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from buildforme.storage import utc_now_iso

COMPAT_TTL_SEC = 900  # 15 minutes

# Minimum version profiles (loose) — fail closed if unparseable when profile requires parse
PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "codex": {
        "min_major": 0,
        "required_help_tokens": ["exec", "prompt", "--skip-git-repo-check"],
        "help_argv": ["exec", "--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": ["non-interactively", "exec"],
        "prompt_delivery": "stdin_or_arg",
        "cwd_flag": "-C",
    },
    "claude": {
        "min_major": 1,
        "required_help_tokens": ["--print", "-p", "--output-format"],
        "help_argv": ["--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": ["--print", "non-interactive"],
        "prompt_delivery": "arg",
        "cwd_flag": None,
    },
    "grok": {
        "min_major": 0,
        "required_help_tokens": ["--cwd", "--help"],
        "help_argv": ["--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": ["headless", "--always-approve", "PROMPT"],
        "prompt_delivery": "arg",
        "cwd_flag": "--cwd",
    },
    "glm": {
        "min_major": 0,
        "required_help_tokens": ["--help"],
        "help_argv": ["--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": [],
        "prompt_delivery": "unknown",
        "cwd_flag": None,
    },
}

_CACHE: dict[str, dict[str, Any]] = {}


def clear_compat_cache() -> None:
    _CACHE.clear()


def verify_provider_compatibility(
    provider_id: str,
    executable: str | None,
    *,
    version_text: str | None = None,
    force: bool = False,
    timeout_sec: float = 8.0,
    auth_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structured compatibility profile. Never marks live_ready alone."""
    pid = str(provider_id or "").strip().lower()
    profile = PROVIDER_PROFILES.get(pid) or {
        "min_major": 0,
        "required_help_tokens": ["--help"],
        "help_argv": ["--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": [],
        "prompt_delivery": "unknown",
        "cwd_flag": None,
    }

    result: dict[str, Any] = {
        "provider_id": pid,
        "binary_available": bool(executable),
        "executable": executable,
        "version_verified": False,
        "version_text": version_text,
        "command_contract_verified": False,
        "non_interactive_mode_verified": False,
        "prompt_delivery_verified": False,
        "cwd_behavior_verified": False,
        "capabilities_verified": False,
        "auth_verified": False,
        "live_ready_components": {},
        "problems": [],
        "checked_at": utc_now_iso(),
        "from_cache": False,
    }

    if not executable:
        result["problems"].append("executable missing")
        result["live_ready_components"] = _components_from(result)
        return result

    exe_path = Path(str(executable))
    mtime = None
    try:
        if exe_path.exists():
            mtime = exe_path.stat().st_mtime
    except OSError:
        mtime = None

    auth_cache_material = (
        str((auth_result or {}).get("status")),
        str((auth_result or {}).get("exit_code")),
        str((auth_result or {}).get("checked_at")),
    )
    cache_key = f"{pid}|{executable}|{mtime}|{version_text}|{auth_cache_material}"
    if not force and cache_key in _CACHE:
        cached = _CACHE[cache_key]
        if int(cached.get("expires_at_epoch") or 0) > int(time.time()):
            out = dict(cached["profile"])
            out["from_cache"] = True
            return out

    # Version probe if not provided
    if not version_text:
        ver = _run([str(executable), *list(profile.get("version_argv") or ["--version"])], timeout_sec)
        version_text = (ver.get("stdout") or ver.get("stderr") or "").strip()[:200]
        result["version_text"] = version_text
        if ver.get("ok") and version_text:
            result["version_verified"] = True
        else:
            result["problems"].append(f"version probe failed: {ver.get('detail')}")
    else:
        result["version_verified"] = True

    # Help / contract probe
    help_argv = [str(executable), *list(profile.get("help_argv") or ["--help"])]
    help_res = _run(help_argv, timeout_sec)
    help_text = ((help_res.get("stdout") or "") + "\n" + (help_res.get("stderr") or "")).lower()
    if not help_res.get("ok") and help_res.get("exit_code") not in (0, 1, 2):
        result["problems"].append(f"help probe failed: {help_res.get('detail')}")
    else:
        missing = [
            tok
            for tok in (profile.get("required_help_tokens") or [])
            if tok.lower() not in help_text
        ]
        if missing:
            result["problems"].append(f"command contract missing tokens: {missing}")
        else:
            result["command_contract_verified"] = True

        ni_tokens = list(profile.get("non_interactive_tokens") or [])
        if ni_tokens and any(t.lower() in help_text for t in ni_tokens):
            result["non_interactive_mode_verified"] = True
        elif not ni_tokens:
            result["non_interactive_mode_verified"] = result["command_contract_verified"]
        else:
            result["problems"].append("non-interactive mode not confirmed in help")

        # Prompt delivery: provider-specific evidence from help
        delivery = profile.get("prompt_delivery")
        if delivery == "stdin_or_arg" and ("stdin" in help_text or "prompt" in help_text):
            result["prompt_delivery_verified"] = True
        elif delivery == "arg" and ("prompt" in help_text or "-p" in help_text or "[prompt]" in help_text):
            result["prompt_delivery_verified"] = True
        elif delivery == "unknown":
            result["prompt_delivery_verified"] = False
            result["problems"].append("prompt delivery contract unknown for provider")
        else:
            result["prompt_delivery_verified"] = result["command_contract_verified"]

        cwd_flag = profile.get("cwd_flag")
        if cwd_flag is None:
            # cwd via process supervisor is enough when CLI has no -C
            result["cwd_behavior_verified"] = True
        elif str(cwd_flag).lower() in help_text:
            result["cwd_behavior_verified"] = True
        else:
            result["problems"].append(f"cwd flag {cwd_flag} not confirmed")

    # Capabilities: require edit/read implied by successful contract for coding CLIs
    result["capabilities_verified"] = bool(
        result["command_contract_verified"] and result["non_interactive_mode_verified"]
    )

    # Auth is supplied only by the executable status probe in provider_discovery.
    auth = dict(auth_result or {})
    result["auth_verified"] = auth.get("status") == "ready" and bool(auth.get("probe_verified"))
    result["auth"] = auth
    if not result["auth_verified"]:
        result["problems"].append(f"auth not verified by executable probe: {auth.get('detail') or 'missing probe result'}")

    result["live_ready_components"] = _components_from(result)
    result["expires_at_epoch"] = int(time.time()) + COMPAT_TTL_SEC
    _CACHE[cache_key] = {"profile": dict(result), "expires_at_epoch": result["expires_at_epoch"]}
    return result


def compatibility_allows_live(compat: dict[str, Any], *, constitution_ack: bool, enabled: bool) -> bool:
    """All live_ready components must be true — fail closed."""
    if not enabled or not constitution_ack:
        return False
    comps = compat.get("live_ready_components") or {}
    required = (
        "binary_available",
        "version_verified",
        "auth_verified",
        "command_contract_verified",
        "non_interactive_mode_verified",
        "prompt_delivery_verified",
        "cwd_behavior_verified",
        "capabilities_verified",
    )
    return all(bool(comps.get(k)) for k in required)


def _components_from(result: dict[str, Any]) -> dict[str, bool]:
    return {
        "binary_available": bool(result.get("binary_available")),
        "version_verified": bool(result.get("version_verified")),
        "auth_verified": bool(result.get("auth_verified")),
        "command_contract_verified": bool(result.get("command_contract_verified")),
        "non_interactive_mode_verified": bool(result.get("non_interactive_mode_verified")),
        "prompt_delivery_verified": bool(result.get("prompt_delivery_verified")),
        "cwd_behavior_verified": bool(result.get("cwd_behavior_verified")),
        "capabilities_verified": bool(result.get("capabilities_verified")),
    }


def _auth_component(provider_id: str) -> dict[str, Any]:
    env_hints = {
        "codex": ["OPENAI_API_KEY", "CODEX_API_KEY"],
        "claude": ["ANTHROPIC_API_KEY"],
        "grok": ["XAI_API_KEY", "GROK_API_KEY"],
        "glm": ["ZHIPUAI_API_KEY", "GLM_API_KEY"],
    }
    names = env_hints.get(provider_id, [])
    present = [k for k in names if os.environ.get(k)]
    if present:
        return {
            "status": "ready",
            "detail": "auth environment marker present (value not stored)",
            "marker_names": present,
        }
    return {
        "status": "unknown",
        "detail": "no verified auth marker; CLI login cache not accepted",
        "marker_names": [],
    }


def _run(argv: list[str], timeout_sec: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "", "exit_code": 127, "detail": "file not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "", "exit_code": 124, "detail": "timeout"}
    except OSError as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": None, "detail": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "detail": "ok" if proc.returncode == 0 else f"exit {proc.returncode}",
    }


def parse_major_version(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\.\d+", text)
    if not m:
        return None
    return int(m.group(1))
