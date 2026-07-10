"""Supervised run state machine for Stage 5.

No arbitrary statuses. Invalid transitions raise ValueError.
"""

from __future__ import annotations

from typing import Any

from buildforme.storage import utc_now_iso

RUN_STATUSES = frozenset(
    {
        "draft",
        "awaiting_preflight",
        "preflight_failed",
        "awaiting_approval",
        "approved",
        "queued",
        "starting",
        "running",
        "cancel_requested",
        "cancelled",
        "timed_out",
        "failed",
        "needs_review",
        "completed",
        "rejected",
        "blocked",
    }
)

TERMINAL_STATUSES = frozenset(
    {
        "preflight_failed",
        "cancelled",
        "timed_out",
        "failed",
        "completed",
        "rejected",
        "blocked",
    }
)

# Explicit allowed edges
_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"awaiting_preflight", "blocked", "rejected"}),
    "awaiting_preflight": frozenset({"preflight_failed", "awaiting_approval", "approved", "blocked"}),
    "preflight_failed": frozenset(),  # terminal
    "awaiting_approval": frozenset({"approved", "rejected", "blocked"}),
    "approved": frozenset({"queued", "blocked", "rejected"}),
    "queued": frozenset({"starting", "cancel_requested", "blocked", "failed"}),
    "starting": frozenset({"running", "failed", "cancel_requested"}),
    "running": frozenset(
        {"cancel_requested", "timed_out", "failed", "needs_review", "completed", "cancelled"}
    ),
    "cancel_requested": frozenset({"cancelled", "failed"}),
    "cancelled": frozenset(),
    "timed_out": frozenset(),
    "failed": frozenset(),
    "needs_review": frozenset({"completed", "rejected", "blocked"}),
    "completed": frozenset(),
    "rejected": frozenset(),
    "blocked": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    current = str(current or "").strip()
    target = str(target or "").strip()
    if current not in RUN_STATUSES or target not in RUN_STATUSES:
        return False
    return target in _TRANSITIONS.get(current, frozenset())


def allowed_transitions(status: str) -> list[str]:
    status = str(status or "").strip()
    return sorted(_TRANSITIONS.get(status, frozenset()))


def is_terminal(status: str) -> bool:
    return str(status or "").strip() in TERMINAL_STATUSES


def transition_run(
    run: dict[str, Any],
    target: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Return a new run dict with status transition applied."""
    if not isinstance(run, dict):
        raise ValueError("run must be an object")
    current = str(run.get("status") or "draft")
    target = str(target or "").strip()
    if is_terminal(current):
        raise ValueError(f"terminal run cannot transition from {current}")
    if not can_transition(current, target):
        raise ValueError(f"invalid transition {current} → {target}")

    updated = dict(run)
    updated["status"] = target
    updated["updated_at"] = utc_now_iso()
    history = list(updated.get("status_history") or [])
    history.append(
        {
            "from": current,
            "to": target,
            "actor": str(actor or "system"),
            "reason": str(reason or ""),
            "at": utc_now_iso(),
        }
    )
    updated["status_history"] = history

    if target in {"running", "starting"} and not updated.get("started_at"):
        updated["started_at"] = utc_now_iso()
    if target in TERMINAL_STATUSES:
        updated["finished_at"] = utc_now_iso()
    return updated
