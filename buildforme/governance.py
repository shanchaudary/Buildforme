"""Shared governance validators for Stage 5.5.

Deterministic, fail-closed helpers used by preflight, execution, storage, and API.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
FORBIDDEN_CAPABILITIES = frozenset({"merge", "deploy", "production_write"})
ALLOWED_ACTORS = frozenset({"shan", "system", "cli", "reviewer"})
SENSITIVE_PATH_MARKERS = (".env", "secret", "credential", "id_rsa", "private-key", "private_key")


def parse_bool_strict(value: Any, *, field: str = "value") -> bool:
    """Parse booleans without unsafe truthiness.

    Accepts: True/False, 1/0, "true"/"false", "yes"/"no", "on"/"off" (case-insensitive).
    Rejects: null, "", objects, and any non-enumerated representation.
    Note: Python bool("false") is True — this function must not use that.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    raise ValueError(f"{field} must be a boolean (true/false), got {value!r}")


def validate_safe_id(value: Any, *, field: str = "id") -> str:
    text = str(value or "").strip()
    if not text or not SAFE_ID_RE.fullmatch(text):
        raise ValueError(f"{field} must match {SAFE_ID_RE.pattern}")
    if ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"{field} must not contain path separators")
    return text


def validate_branch(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not SAFE_BRANCH_RE.fullmatch(text):
        raise ValueError("target_branch is invalid")
    if ".." in text or text.startswith("/") or "\\" in text:
        raise ValueError("target_branch must not contain path traversal")
    return text


def canonicalize_repository(repository: str) -> str:
    cleaned = repository.strip().removeprefix("https://github.com/").removeprefix("http://github.com/")
    cleaned = cleaned.strip().strip("/")
    parts = cleaned.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("repository must be owner/name")
    owner, name = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("repository must be owner/name")
    if ".." in owner or ".." in name:
        raise ValueError("repository invalid")
    return f"{owner}/{name}"


def normalize_repo_for_compare(repository: str) -> str:
    try:
        return canonicalize_repository(repository).lower()
    except ValueError:
        return str(repository or "").strip().lower()


def validate_capabilities(capabilities: list[str]) -> list[str]:
    cleaned: list[str] = []
    for cap in capabilities:
        name = str(cap or "").strip()
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError(f"invalid capability: {cap!r}")
        if name in FORBIDDEN_CAPABILITIES:
            raise ValueError(f"forbidden capability: {name}")
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def validate_actor(actor: Any) -> str:
    text = str(actor or "system").strip().lower()
    if text not in ALLOWED_ACTORS:
        raise ValueError(f"actor must be one of {sorted(ALLOWED_ACTORS)}")
    return text


def compute_run_scope_fingerprint(run: dict[str, Any], packet: dict[str, Any] | None = None) -> str:
    """Deterministic cryptographic fingerprint of execution and governance scope."""
    packet = packet if isinstance(packet, dict) else (
        run.get("packet") if isinstance(run.get("packet"), dict) else {}
    )
    material = {
        "run_id": str(run.get("id") or ""),
        "project_id": str(run.get("project_id") or ""),
        "task_id": str(run.get("task_id") or ""),
        "packet_id": str(run.get("packet_id") or packet.get("id") or ""),
        "provider_id": str(run.get("provider_id") or ""),
        "repository": normalize_repo_for_compare(str(run.get("repository") or "")),
        "repository_local_path": str(run.get("repository_local_path") or ""),
        "baseline_commit": str(run.get("baseline_commit") or ""),
        "baseline_ref": str(run.get("baseline_ref") or ""),
        "target_branch": str(run.get("target_branch") or ""),
        "operating_mode": str(run.get("operating_mode") or "").upper(),
        "risk": str(run.get("risk") or "").upper(),
        "execution_mode": str(run.get("execution_mode") or run.get("mode") or ""),
        "requested_capabilities": sorted(str(c) for c in (run.get("requested_capabilities") or [])),
        "timeout_minutes": int(run.get("timeout_minutes") or 0),
        "max_attempts": int(run.get("max_attempts") or 0),
        "budget": _canonical(run.get("budget") or {}),
        "constitution_version": str(run.get("constitution_version") or ""),
        "constitution_hash": str(run.get("constitution_hash") or ""),
        "constitution_lease_id": str(run.get("constitution_lease_id") or ""),
        "constitution_lease_fingerprint": str(
            run.get("constitution_lease_fingerprint") or ""
        ),
        "packet_constitution_version": str(packet.get("constitution_version") or ""),
        "packet_constitution_hash": str(packet.get("constitution_hash") or ""),
        "packet_objective": str(packet.get("objective") or ""),
        "packet_allowed_files": sorted(str(x) for x in (packet.get("allowed_files") or [])),
        "packet_forbidden_files": sorted(str(x) for x in (packet.get("forbidden_files") or [])),
        "packet_acceptance": sorted(str(x) for x in (packet.get("acceptance_criteria") or [])),
        "packet_context": str(packet.get("context") or ""),
        "packet_risk": str(packet.get("risk") or "").upper(),
        "packet_mode": str(packet.get("operating_mode") or "").upper(),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value.keys(), key=lambda x: str(x))}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def material_text_blob(run: dict[str, Any], packet: dict[str, Any] | None = None) -> str:
    """Concatenate all material text fields for policy scanning."""
    packet = packet if isinstance(packet, dict) else (
        run.get("packet") if isinstance(run.get("packet"), dict) else {}
    )
    parts: list[str] = []
    for key in (
        "objective",
        "context",
        "title",
        "operating_mode",
        "risk",
    ):
        parts.append(str(run.get(key) or ""))
        parts.append(str(packet.get(key) or ""))
    for key in ("allowed_files", "forbidden_files", "acceptance_criteria", "required_tests", "manual_proof"):
        for item in packet.get(key) or run.get(key) or []:
            parts.append(str(item))
    for cap in run.get("requested_capabilities") or []:
        parts.append(str(cap))
    # Nested metadata
    meta = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    parts.append(json.dumps(meta, sort_keys=True, default=str))
    text = "\n".join(parts).lower()
    # Collapse whitespace for evasion resistance
    text = re.sub(r"[\s_]+", " ", text)
    text = re.sub(r"[^\w\s./-]", " ", text)
    return text


def contains_black_instruction(text: str) -> list[str]:
    patterns = [
        "print secret",
        "print secrets",
        "show api key",
        "commit .env",
        "commit env",
        "bypass auth",
        "disable auth",
        "fake success",
        "pretend it works",
        "skip tests and merge",
        "merge without review",
        "production write without approval",
        "hide failing tests",
        "delete audit log",
    ]
    return [p for p in patterns if p in text]


def contains_sensitive_allowed_path(paths: list[str]) -> list[str]:
    hits = []
    for path in paths:
        lowered = str(path).lower().replace("\\", "/")
        if any(marker in lowered for marker in SENSITIVE_PATH_MARKERS):
            hits.append(path)
    return hits
