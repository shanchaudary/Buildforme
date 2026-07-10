"""Generic repository verification profiles (data-driven, not product-specific)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Default profile for this Buildforme repository itself — still generic keys only.
DEFAULT_PYTHON_PROFILE: dict[str, Any] = {
    "profile_id": "generic",
    "install_command": None,
    "test_command": ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
    "lint_command": None,
    "typecheck_command": None,
    "build_command": None,
    "integration_test_command": None,
    "migration_policy": "forbidden_without_approval",
    "forbidden_paths": [".env", "secrets/**", "credentials/**", "**/*token*", "**/*secret*"],
    "sensitive_modules": [],
    "protected_branches": ["main", "master"],
    "framework_checks": [],
}


def normalize_verification_profile(raw: Any) -> dict[str, Any]:
    """Merge project-supplied profile with defaults; reject unknown dangerous keys."""
    base = deepcopy(DEFAULT_PYTHON_PROFILE)
    if not isinstance(raw, dict):
        return base
    allowed = set(base.keys()) | {"notes", "language", "stack_hints", "extra_checks"}
    for key, value in raw.items():
        if key not in allowed:
            continue
        if key.endswith("_command") and value is not None:
            base[key] = _as_command(value)
        else:
            base[key] = value
    if not base.get("profile_id"):
        base["profile_id"] = "generic"
    return base


def profile_from_project(project: dict[str, Any] | None) -> dict[str, Any]:
    project = project or {}
    # Prefer explicit verification_profile; fall back to project metadata.
    raw = project.get("verification_profile")
    if not isinstance(raw, dict):
        meta = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
        raw = meta.get("verification_profile") if isinstance(meta, dict) else None
    profile = normalize_verification_profile(raw)
    # Stack hints from generic project fields only
    if project.get("primary_language") and not profile.get("language"):
        profile["language"] = str(project.get("primary_language"))
    return profile


def _as_command(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(x) for x in value if str(x).strip()]
        return parts or None
    text = str(value).strip()
    if not text:
        return None
    # Do not shell-split complex quoting; store as single argv when plain string is multi-word
    # callers should prefer argv lists in config.
    return text.split()
