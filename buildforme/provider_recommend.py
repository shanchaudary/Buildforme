"""Deterministic provider recommendation (no LLM planner)."""

from __future__ import annotations

from typing import Any


def recommend_provider(
    *,
    health: list[dict[str, Any]],
    risk: str,
    operating_mode: str,
    requested_capabilities: list[str],
    task_type: str = "implementation",
    language: str | None = None,
    stack_hints: list[str] | None = None,
    founder_preferences: dict[str, Any] | None = None,
    cost_policy: dict[str, Any] | None = None,
    reliability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score providers and return ranked recommendation.

    Founder preferences may force a provider_id override when available.
    """
    prefs = founder_preferences or {}
    cost = cost_policy or {}
    rel = reliability or {}
    risk_u = str(risk or "YELLOW").upper()
    mode_u = str(operating_mode or "IMPLEMENTATION").upper()
    caps = [str(c) for c in (requested_capabilities or [])]
    forced = str(prefs.get("preferred_provider") or prefs.get("provider_id") or "").strip().lower()

    ranked: list[dict[str, Any]] = []
    for item in health:
        pid = str(item.get("provider_id") or "")
        score = 0.0
        reasons: list[str] = []
        blockers: list[str] = []

        if not item.get("available"):
            blockers.append("executable unavailable")
        if not item.get("enabled", True):
            blockers.append("disabled")
        if not item.get("constitution_acknowledged"):
            blockers.append("constitution not acknowledged")
        if item.get("auth", {}).get("status") == "missing":
            blockers.append("authentication missing")

        if risk_u in {"RED", "BLACK"} and not prefs.get("allow_red_live"):
            # Prefer dry-run recommendation for RED unless founder overrides
            reasons.append("high risk prefers careful provider with strong review posture")
            score -= 5

        if item.get("live_ready"):
            score += 40
            reasons.append("live-ready")
        elif item.get("available"):
            score += 15
            reasons.append("discovered but not fully ready")
        else:
            score -= 100

        # Capability coverage (from registry health snapshot)
        supported = set(item.get("capabilities") or [])
        missing = [c for c in caps if c not in supported and c not in {"merge", "deploy", "production_write"}]
        if missing:
            score -= 5 * len(missing)
            reasons.append(f"missing caps: {', '.join(missing)}")
        else:
            score += 10
            reasons.append("capabilities covered")

        # Mode affinity
        if mode_u in {"READ_ONLY_AUDIT", "REVIEW", "PLAN_ONLY"} and pid in {"claude", "codex"}:
            score += 5
            reasons.append("strong for review/audit modes")
        if mode_u == "IMPLEMENTATION" and pid in {"codex", "claude"}:
            score += 8
            reasons.append("strong for implementation")
        if language and str(language).lower() in {"python", "typescript", "go"}:
            score += 2

        # Reliability evidence (generic counters)
        stats = rel.get(pid) if isinstance(rel.get(pid), dict) else {}
        success = int(stats.get("successes") or 0)
        failure = int(stats.get("failures") or 0)
        if success + failure > 0:
            rate = success / (success + failure)
            score += 10 * rate
            reasons.append(f"reliability {rate:.0%} ({success}/{success + failure})")

        # Cost / duration preference
        if cost.get("prefer_low_cost") and pid in {"glm", "grok"}:
            score += 3
            reasons.append("cost preference")
        if cost.get("prefer_quality") and pid in {"claude", "codex"}:
            score += 5
            reasons.append("quality preference")

        if forced and pid == forced:
            score += 100
            reasons.append("founder preference override")

        if blockers:
            score = min(score, -50)

        ranked.append(
            {
                "provider_id": pid,
                "display_name": item.get("display_name") or pid,
                "score": round(score, 2),
                "reasons": reasons,
                "blockers": blockers,
                "live_ready": bool(item.get("live_ready")),
                "available": bool(item.get("available")),
                "status": item.get("status"),
                "eligible": not blockers and bool(item.get("available")),
            }
        )

    ranked.sort(key=lambda r: (-r["score"], r["provider_id"]))
    top = ranked[0] if ranked else None
    override_applied = bool(forced and top and top["provider_id"] == forced)

    return {
        "recommendation": top,
        "ranked": ranked,
        "risk": risk_u,
        "operating_mode": mode_u,
        "task_type": task_type,
        "language": language,
        "stack_hints": list(stack_hints or []),
        "founder_override": forced or None,
        "override_applied": override_applied,
        "policy": "deterministic scoring; founder may override via preferences",
    }
