"""Cryptographic hash of the immutable constitution body."""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Fields that may appear on runtime snapshots but never enter the hash.
_RUNTIME_ONLY_KEYS = frozenset(
    {
        "loaded_at",
        "content_hash",
        "hash",
        "lease_id",
        "acknowledged_at",
        "last_refresh_at",
    }
)


def canonicalize_constitution(constitution: dict[str, Any]) -> Any:
    """Return a JSON-serializable structure with stable key order for hashing."""
    return _canonical(constitution)


def compute_constitution_hash(constitution: dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical constitution body.

    Excludes runtime-only keys. Laws are sorted by id for stability.
    """
    body = _body_for_hash(constitution)
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_constitution_hash(constitution: dict[str, Any], expected_hash: str) -> bool:
    actual = compute_constitution_hash(constitution)
    return actual == str(expected_hash or "").strip().lower() or actual == str(expected_hash or "").strip()


def short_hash(full_hash: str, *, length: int = 12) -> str:
    text = str(full_hash or "")
    return text[: max(4, min(length, len(text)))]


def _body_for_hash(constitution: dict[str, Any]) -> dict[str, Any]:
    cleaned = {k: v for k, v in dict(constitution or {}).items() if k not in _RUNTIME_ONLY_KEYS}
    laws = list(cleaned.get("laws") or [])
    cleaned["laws"] = sorted(
        (_canonical(law) for law in laws if isinstance(law, dict)),
        key=lambda law: str(law.get("id") or ""),
    )
    if "critical_law_ids" in cleaned:
        cleaned["critical_law_ids"] = sorted(str(x) for x in (cleaned.get("critical_law_ids") or []))
    if "applies_to" in cleaned:
        cleaned["applies_to"] = sorted(str(x) for x in (cleaned.get("applies_to") or []))
    return _canonical(cleaned)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value.keys(), key=lambda x: str(x))}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
