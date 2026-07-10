"""Central redaction authority for Stage 6 — apply before any persistence or display."""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Patterns for common secret material. Not claimed exhaustive; fail closed on hits.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?key|secret[_-]?key)\s*[:=]\s*['\"]?([^\s'\"\\]{8,})"),
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\"\\]{4,})"),
    re.compile(r"(?i)\b(authorization)\s*:\s*(bearer\s+)?([A-Za-z0-9\-._~+/]+=*)"),
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{20,}=*)"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\b(openai|anthropic|xai|zhipu|glm)[_-]?api[_-]?key\s*[:=]\s*['\"]?([^\s'\"]+)"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(postgres|mysql|mongodb|redis)://[^\s:]+:[^\s@]+@[^\s]+"),
    re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*([^\s;]{8,})"),
    re.compile(r"(?i)\b(session[_-]?id|sessionid)\s*[:=]\s*([A-Za-z0-9+/=_\-]{16,})"),
    re.compile(r"(?i)\b(token)\s*[:=]\s*['\"]?([A-Za-z0-9\-._~+/]{16,}=*)"),
]

REDACTED = "[REDACTED]"


def redact_text(value: Any) -> str:
    """Return text with secret-like material replaced."""
    text = "" if value is None else str(value)
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def redact_hash(value: Any) -> str:
    """Stable hash of original text for correlation without storing secrets."""
    raw = "" if value is None else str(value)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def redact_argv(argv: list[str] | None) -> list[str]:
    return [redact_text(a) for a in (argv or [])]


def redact_mapping(data: dict[str, Any] | None, *, keys_only_for_env: bool = False) -> dict[str, Any]:
    """Recursively redact string values. For env maps, prefer names-only."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        k = str(key)
        if keys_only_for_env:
            out[k] = "<present>" if value not in (None, "") else "<empty>"
            continue
        if isinstance(value, dict):
            out[k] = redact_mapping(value)
        elif isinstance(value, list):
            out[k] = [
                redact_mapping(v) if isinstance(v, dict) else redact_text(v) if isinstance(v, str) else v
                for v in value
            ]
        elif isinstance(value, str):
            # Never store values for secret-looking keys
            if _key_looks_secret(k):
                out[k] = REDACTED
            else:
                out[k] = redact_text(value)
        else:
            out[k] = value
    return out


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(event or {})
    for field in ("message", "summary", "detail", "stderr", "stdout", "error", "reason", "note"):
        if field in cleaned:
            cleaned[field] = redact_text(cleaned.get(field))
    if "argv" in cleaned and isinstance(cleaned["argv"], list):
        cleaned["argv"] = redact_argv(cleaned["argv"])
    if "metadata" in cleaned and isinstance(cleaned["metadata"], dict):
        cleaned["metadata"] = redact_mapping(cleaned["metadata"])
    return cleaned


def redact_process_result(result: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(result or {})
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    return {
        **result,
        "stdout": redact_text(stdout),
        "stderr": redact_text(stderr),
        "stdout_sha256": redact_hash(stdout),
        "stderr_sha256": redact_hash(stderr),
        "argv": redact_argv(result.get("argv") if isinstance(result.get("argv"), list) else []),
        "error": redact_text(result.get("error") or "") if result.get("error") else result.get("error"),
        "env_names": list(result.get("env_names") or []),
    }


def contains_secret_marker(text: str) -> bool:
    """True if redaction would alter the text (secret-like material present)."""
    return redact_text(text) != str(text or "")


def _key_looks_secret(key: str) -> bool:
    low = key.lower()
    return any(
        x in low
        for x in (
            "token",
            "secret",
            "password",
            "passwd",
            "api_key",
            "apikey",
            "authorization",
            "credential",
            "private_key",
            "cookie",
            "session",
        )
    )
