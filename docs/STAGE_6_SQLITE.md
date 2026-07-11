# Stage 6 SQLite execution authority

Authoritative Stage 6 state lives in:

```text
runtime/buildforme_execution.db
```

## Configuration

- Journal mode: **WAL**
- Foreign keys: **ON**
- Schema version: `2` (`schema_meta.version`)
- Optimistic concurrency: `runs.row_version`
- Cutover marker: `schema_meta.migration_cutover`

## Authoritative entities (SQLite only)

| Fact | Table |
|------|--------|
| Runs + row_version | `runs` |
| Run audit events | `run_events` |
| Run approvals | `run_approvals` |
| Constitution leases | `constitution_leases` |
| Task locks | `task_locks` |
| Repository locks | `repository_locks` |
| Repository bindings | `repository_bindings` |
| Evidence (append-only) | `evidence` |
| Provider constitution acks | `provider_acks` |
| Founder sessions | `founder_sessions` |
| Global kill switch | `execution_control` |
| Project execution controls | `project_execution_controls` |
| Provider compat cache (optional) | `provider_compat_cache` |

## JSON (not Stage 6 concurrency authority)

Projects, packets, tasks, stages, truth, general audit events, and constitution
**violation** logs may remain JSON. They are configuration / product history, not
live execution concurrency authority.

After migration cutover, Stage 6 execution facts must not be treated as authoritative
in JSON even if export mirrors exist.

## Atomic admission

`Stage6Store.admit_run_atomic` commits in one `BEGIN IMMEDIATE` transaction:

1. optional active task lock
2. constitution lease (append-only)
3. run row
4. initial `run_created` event

BLACK/sensitive validation runs **before** any SQLite write.

## Atomic transitions

`Stage6Store.transition_run_with_event` updates run payload + increments
`row_version` + appends audit event in one transaction. Stale
`expected_row_version` writers fail closed.

## Migration

```python
from buildforme.storage import LocalStore
store = LocalStore("runtime/buildforme_state.json")
preview = store.s6.migrate_from_json(store.runtime_dir, dry_run=True, cutover=False)
report = store.s6.migrate_from_json(store.runtime_dir, dry_run=False, cutover=True)
```

Supports: backup, dry-run preview, idempotent apply, malformed reporting,
integrity check, cutover marker (withheld if errors).

## Integrity

```python
store.s6.db.pragmas()
# journal_mode=wal, foreign_keys=True, integrity_check=ok, schema_version=2
```
