"""Founder authentication — admin secret exchange, hashed sessions, CSRF, Host policy."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from buildforme.storage import utc_now_iso

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
SECRET_ENV = "BUILDFORME_ADMIN_SECRET"
SESSION_TTL_DEFAULT = 3600


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def admin_secret_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / ".buildforme_admin_secret"


def load_or_create_admin_secret(runtime_dir: Path) -> str:
    """Return admin secret from env or restricted local file (generate once)."""
    env = os.environ.get(SECRET_ENV)
    if env and len(env) >= 16:
        return env.strip()
    path = admin_secret_path(runtime_dir)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    try:
        # Best-effort restricted permissions (POSIX); Windows ACLs left to OS defaults
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return secret


def verify_admin_secret(runtime_dir: Path, provided: str | None) -> bool:
    if not provided:
        return False
    expected = load_or_create_admin_secret(runtime_dir)
    return hmac.compare_digest(str(provided), expected)


def mint_session_tokens() -> tuple[str, str, str, str]:
    """Return (session_token, session_hash, csrf_token, csrf_hash)."""
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    return session, _sha(session), csrf, _sha(csrf)


def parse_host(host_header: str | None, *, configured_port: int = 8787) -> tuple[str, int | None]:
    raw = str(host_header or "").strip().lower()
    if not raw:
        raise ValueError("Host header required")
    # Reject userinfo / tricks
    if "@" in raw or "\\" in raw or raw.count(":") > 1 and not raw.startswith("["):
        # allow [::1]:port
        if not (raw.startswith("[") and "]" in raw):
            raise ValueError(f"invalid Host: {host_header!r}")
    if raw.startswith("["):
        # [::1] or [::1]:port
        m = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", raw)
        if not m:
            raise ValueError(f"invalid Host: {host_header!r}")
        name, port_s = m.group(1), m.group(2)
        port = int(port_s) if port_s else None
        return name, port
    if ":" in raw:
        name, port_s = raw.rsplit(":", 1)
        if not port_s.isdigit():
            raise ValueError(f"invalid Host: {host_header!r}")
        return name, int(port_s)
    return raw, None


def validate_loopback_host(host_header: str | None, *, configured_port: int = 8787) -> str:
    name, port = parse_host(host_header, configured_port=configured_port)
    # Normalize ipv6
    if name == "0:0:0:0:0:0:0:1":
        name = "::1"
    if name not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"non-loopback Host rejected: {host_header!r}")
    if port is not None and port != configured_port:
        # allow only configured port when present
        raise ValueError(f"Host port {port} does not match configured port {configured_port}")
    return name


def validate_origin(origin: str | None, referer: str | None, *, host_header: str, configured_port: int) -> None:
    """Exact same-origin against loopback host — never build allowlist from untrusted Host alone beyond validation."""
    name = validate_loopback_host(host_header, configured_port=configured_port)
    allowed = {
        f"http://{name}:{configured_port}",
        f"http://127.0.0.1:{configured_port}",
        f"http://localhost:{configured_port}",
        f"http://[::1]:{configured_port}",
    }
    # Also without port for default (rare for 8787)
    if not origin and not referer:
        return  # CLI tools without browser origin
    for value in (origin, referer):
        if not value:
            continue
        v = value.strip().rstrip("/")
        # Reject suffix tricks
        if "localhost." in v or "127.0.0.1." in v:
            raise ValueError("DNS-rebinding-style origin rejected")
        if any(v == a or v.startswith(a + "/") for a in allowed):
            return
    raise ValueError("cross-origin request rejected")


def require_mutation_headers(
    *,
    content_type: str | None,
    method: str,
    csrf_header: str | None,
    session_csrf_hash: str | None,
) -> None:
    if method.upper() == "GET":
        raise ValueError("GET must not mutate execution authority")
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in {"application/json"}:
        raise ValueError("mutations require Content-Type: application/json")
    if not csrf_header or not session_csrf_hash:
        raise ValueError("CSRF token required")
    if not hmac.compare_digest(_sha(csrf_header), session_csrf_hash):
        raise ValueError("invalid CSRF token")


def session_record(
    *,
    actor: str,
    token_hash: str,
    csrf_hash: str,
    ttl_seconds: int = SESSION_TTL_DEFAULT,
) -> dict[str, Any]:
    import time

    return {
        "token_hash": token_hash,
        "csrf_token_hash": csrf_hash,
        "actor": actor,
        "created_at": utc_now_iso(),
        "expires_at_epoch": int(time.time()) + max(60, int(ttl_seconds)),
        "revoked": False,
        "active": True,
    }
