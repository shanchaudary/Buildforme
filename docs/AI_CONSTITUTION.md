# AI Constitution (Stage 5.6)

Canonical product documentation for the Buildforme AI Constitution.

## Purpose

Replace prompt-only governance with a **constitutional operating layer**:

- versioned
- immutable during a run
- inherited by every surface
- enforced and validated
- audited
- hash verified
- provider independent

## Sources

| Artifact | Role |
| --- | --- |
| `governance/AI_CONSTITUTION.json` | Machine-readable law (authority for hash) |
| `governance/AI_CONSTITUTION.md` | Human-readable summary beside code |
| `docs/ENGINEERING_LAWS.md` | Law catalog narrative |
| `docs/CONSTITUTION_ENGINE.md` | Engine architecture |
| `docs/CONSTITUTION_LEASES.md` | Lease + refresh rules |

## Single authority

- **Constitution domain:** `governance/constitution_engine.py`
- **Run/preflight validators (Stage 5.5):** `buildforme/governance.py`

These are separate modules (LAW-008 / LAW-013). The Constitution does not replace risk policy or kill-switch logic; it sits above them as permanent engineering law.

## CLI

```bash
python -m buildforme.cli constitution-status
python -m buildforme.cli constitution-validate
python -m buildforme.cli constitution-refresh
python -m buildforme.cli constitution-export --format json
```

## API

- `GET /api/constitution` — dashboard payload
- `GET /api/constitution/status`
- `GET /api/constitution/laws`
- `GET /api/constitution/violations`
- `GET /api/constitution/leases`
- `POST /api/constitution/validate`
- `POST /api/constitution/refresh`
- `POST /api/providers/{id}/acknowledge-constitution`

## Acceptance posture

Complete only when packets, runs, providers, and approvals inherit the Constitution; hash/lease systems work; bypass is impossible; Stage 1–5.5 behavior remains intact; no live execution introduced.
