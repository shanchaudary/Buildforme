# Constitution Engine

Stage 5.6 operating-system layer for AI governance.

## Modules

| Module | Responsibility |
| --- | --- |
| `governance/constitution_hash.py` | Canonical SHA-256 of constitution body |
| `governance/constitution_lease.py` | Issue / integrity of run leases |
| `governance/constitution_inheritance.py` | Bind packets, runs, providers, approvals |
| `governance/constitution_validator.py` | Document, binding, and output validation |
| `governance/constitution_audit.py` | Violation event shapes + summaries |
| `governance/constitution_engine.py` | Single facade / authority |

## Runtime flow

1. Load `AI_CONSTITUTION.json` → compute hash  
2. Provider `constitution-refresh` / acknowledge → full constitution delivered once per version  
3. Packet generate → inherit version + hash + critical laws (no full dump)  
4. Run create → require provider ack; issue immutable lease; persist lease  
5. Approval → bind constitution hash (and lease id)  
6. Preflight / dry-run → revalidate lease + ack  
7. Output validation → violations become audit events  

## Prompt minimization

| Event | Content sent |
| --- | --- |
| Provider acknowledge/refresh | Full constitution (once) |
| Run start / phase / completion / review | Compact reminder only |
| Packet handoff | Binding + critical laws |

Never re-send full constitution on every prompt.

## Integration points

- `buildforme/packet_generator.py` — automatic inheritance  
- `buildforme/execution_service.py` — lease, ack gate, approval bind, dry-run compliance  
- `buildforme/execution_preflight.py` — fail-closed constitution checks  
- `buildforme/providers.py` + storage — ack fields  
- `buildforme/cli.py` — constitution-* commands  
- `buildforme/server.py` — REST API  
- `public/` — Constitution page  

## Non-bypass rule (LAW-020)

No module may set `bypass_forbidden=false` or execute without acknowledgement and lease. Provider patches cannot clear constitution fields to fake compliance without going through the acknowledge path with the current hash.
