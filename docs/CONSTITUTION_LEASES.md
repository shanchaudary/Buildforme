# Constitution Leases

## Definition

A **lease** is an immutable record binding a run to a specific Constitution version and hash for the life of that run.

```json
{
  "lease_id": "lease-…",
  "constitution_version": "1.0.0",
  "constitution_hash": "sha256…",
  "run_id": "run-…",
  "provider_id": "codex",
  "immutable": true,
  "status": "active",
  "issued_at": "…"
}
```

## Rules

1. **Issue on run create** — every supervised run gets a lease.  
2. **Immutable during run** — Constitution file may change later; existing runs keep original lease.  
3. **New runs** — receive the current version/hash.  
4. **Approvals** — store constitution hash (+ lease id when present).  
5. **Dry-run / preflight** — revalidate lease integrity (not “must match latest”).  
6. **Storage** — `runtime/constitution_leases.json` (gitignored).

## Refresh vs lease

| Action | Effect |
| --- | --- |
| `constitution-refresh` | Providers re-acknowledge current full Constitution |
| Existing run lease | Unchanged |
| New run after refresh | New lease with new hash if Constitution changed |

## Compliance fields on runs

- `constitution_lease` / `constitution_lease_id`  
- `constitution_version` / `constitution_hash`  
- `constitution_compliance` (`bound` | `compliant` | `violations`)  
- `constitution_reminder` (compact phase reminder)  
