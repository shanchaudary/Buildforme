"""Provider process environment allowlist — no full parent-env inheritance."""

from __future__ import annotations

import os
from typing import Any

# Safe runtime variables required for process startup on Windows/POSIX.
SAFE_RUNTIME_VARS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "USERNAME",
        "USER",
        "LOGNAME",
        "LOCALAPPDATA",
        "APPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SYSTEMDRIVE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)

# Provider-specific auth env names that may be passed by name when present.
# Values are never logged; presence alone is recorded.
PROVIDER_AUTH_ENV: dict[str, frozenset[str]] = {
    "codex": frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"}),
    "claude": frozenset({"ANTHROPIC_API_KEY"}),
    "grok": frozenset({"XAI_API_KEY", "GROK_API_KEY"}),
    "glm": frozenset({"ZHIPUAI_API_KEY", "GLM_API_KEY"}),
}

# Always blocked even if someone tries to allowlist them broadly.
BLOCKED_ENV_SUBSTRINGS = (
    "STRIPE",
    "DATABASE_URL",
    "POSTGRES",
    "MYSQL",
    "MONGO",
    "REDIS_URL",
    "AWS_SECRET",
    "AWS_ACCESS",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SMTP",
    "SENDGRID",
    "DEPLOY",
    "KUBECONFIG",
    "PRIVATE_KEY",
    "SSH_AUTH",
)


def build_provider_env(
    provider_id: str,
    *,
    extra_allow: list[str] | None = None,
    include_auth: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """Build isolated env for a provider process.

    Returns (env_map, env_names_supplied).
    Never copies the full parent environment.
    """
    pid = str(provider_id or "").strip().lower()
    allowed_names = set(SAFE_RUNTIME_VARS)
    if include_auth:
        allowed_names |= set(PROVIDER_AUTH_ENV.get(pid) or ())
    for name in extra_allow or []:
        if name and not _blocked(name):
            allowed_names.add(str(name))

    env: dict[str, str] = {}
    for name in sorted(allowed_names):
        if _blocked(name):
            continue
        # Only the selected provider's auth keys — never other providers'.
        if _is_any_provider_auth(name) and name not in (PROVIDER_AUTH_ENV.get(pid) or ()):
            continue
        if name in os.environ:
            env[name] = os.environ[name]

    # Ensure PATH exists so executables resolve
    if "PATH" not in env and "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]

    return env, sorted(env.keys())


def env_policy_summary(provider_id: str, env_names: list[str]) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "strategy": "explicit_allowlist",
        "parent_env_inherited": False,
        "env_names": list(env_names),
        "auth_names_eligible": sorted(PROVIDER_AUTH_ENV.get(str(provider_id).lower()) or []),
        "blocked_substrings": list(BLOCKED_ENV_SUBSTRINGS),
    }


def _blocked(name: str) -> bool:
    up = str(name).upper()
    return any(b in up for b in BLOCKED_ENV_SUBSTRINGS)


def _is_any_provider_auth(name: str) -> bool:
    for names in PROVIDER_AUTH_ENV.values():
        if name in names:
            return True
    return False
