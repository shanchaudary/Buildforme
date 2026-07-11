"""Stage 7 governed repair packet service."""

from __future__ import annotations

from typing import Any

from buildforme.governance import validate_actor, validate_safe_id
from buildforme.repair_contracts import build_repair_packet_record
from buildforme.storage import LocalStore


def create_governed_repair_packet(
    store: LocalStore,
    cycle_id: str,
    *,
    repair_provider_id: str,
    actor: str = "shan",
) -> dict[str, Any]:
    cycle_id = validate_safe_id(cycle_id, field="cycle_id")
    actor = validate_actor(actor)
    provider_id = validate_safe_id(repair_provider_id, field="repair_provider_id").lower()
    cycle = store.get_review_cycle(cycle_id)
    run = store.get_run(str(cycle.get("run_id") or ""))
    evidence = store.get_evidence_by_id(str(cycle.get("evidence_id") or ""))
    reports = store.list_review_reports(cycle_id)
    findings = store.list_review_findings(cycle_id)
    provider = store.get_provider_record(provider_id)
    if not provider.get("enabled", True):
        raise ValueError("repair provider disabled")
    provider_ack = store.s6.get_provider_ack(provider_id) or {}
    packet = build_repair_packet_record(
        cycle=cycle,
        run=run,
        evidence=evidence,
        reports=reports,
        findings=findings,
        repair_provider_id=provider_id,
        actor=actor,
        provider_ack=provider_ack,
    )
    return store.create_repair_packet_atomic(packet=packet, actor=actor)


def get_governed_repair_packet(store: LocalStore, repair_packet_id: str) -> dict[str, Any]:
    return store.get_repair_packet(validate_safe_id(repair_packet_id, field="repair_packet_id"))
