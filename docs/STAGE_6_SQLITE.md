# Stage 6 SQLite execution authority

Authoritative Stage 6 state lives in:

```text
runtime/buildforme_execution.db
```

## Configuration

- Journal mode: **WAL**
- Foreign keys: **ON**
- Schema version: `1` (`schema_meta.version`)

## Authoritative entities

runs, run_events, run_approvals, constitution_leases, task_locks, repository_locks,
repository_bindings, evidence, provider_acks, founder_sessions, execution_control

JSON under `runtime/` may still hold projects/packets/tasks and optional export mirrors.
JSON is **not** the concurrency authority for live execution.

## Migration

```python
from buildforme.storage import LocalStore
store = LocalStore("runtime/buildforme_state.json")
report = store.s6.migrate_from_json(store.runtime_dir)
```

Backup directory is created under `runtime/json_backup_<timestamp>/`.

## Integrity

```python
store.s6.db.pragmas()
# journal_mode=wal, foreign_keys=True, integrity_check=ok
```
