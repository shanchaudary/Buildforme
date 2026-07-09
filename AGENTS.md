# Buildforme Agent Operating Law

This repository supervises AI engineering agents. Its own agents must be stricter than the agents it supervises.

## Core Rules

1. Do not expose secrets.
2. Do not commit `.env`, tokens, keys, logs, databases, or generated junk.
3. Do not claim a task is safe unless the policy engine or a human approval gate says so.
4. Do not merge or enable auto-merge without explicit user approval.
5. Do not add production write capability without an approval gate and tests.
6. Do not create fake success states. If an adapter is not implemented, mark it unavailable.
7. Do not silently broaden scope.
8. Do not build provider-specific logic directly into core policy code. Use adapters.
9. Keep risk classification deterministic, explainable, and test-covered.
10. Prefer blocking uncertain work over approving dangerous work.

## Operating Modes

Every task must declare one mode:

- `READ_ONLY_AUDIT`
- `PLAN_ONLY`
- `DOCUMENTATION_ONLY`
- `IMPLEMENTATION`
- `REVIEW`
- `RELEASE`

Only `IMPLEMENTATION` and `RELEASE` may change runtime behavior. `RELEASE` requires explicit user approval.

## Required Start Checks

Before changes:

```bash
git branch --show-current
git log -1 --oneline
git status --short
```

If the tree is dirty unexpectedly, stop and report.

## Required Final Report

Every task must end with:

```text
Mode:
Objective:
Files changed:
Commands run:
Tests run:
Risk classification impact:
Secrets touched: none / describe
Data mutation: none / describe
Git diff summary:
Final git status:
Remaining risks:
Recommended next task:
```

## Safety Gates

Human approval is required for:

- production writes
- payment capture
- deployment
- database migrations
- auth/tenant isolation changes
- secret storage or rotation
- autonomous merge rules
- external provider credentials
- legal/regulatory conclusion logic

## Blacklisted Behavior

Reject automatically if any task asks an agent to:

- print secrets
- commit secrets
- bypass auth
- fake a feature
- mark untested work as verified
- run production mutation without approval
- merge without review
- delete audit evidence
- hide failing tests

## Verification

For this MVP, run:

```bash
python -m unittest
python -m buildforme.cli classify data/sample_task.json
```

If browser UI changes, also open `public/index.html` through a local static server and verify basic classification manually.
