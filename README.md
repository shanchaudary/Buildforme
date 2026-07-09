# Buildforme

Buildforme is a founder-control-plane MVP for supervising AI coding agents without giving them unrestricted authority.

The goal is to let strong agents such as Claude, Codex, GLM, or future coding systems keep moving a software project while the owner is away, while preserving explicit safety gates for secrets, production writes, payments, regulatory/legal claims, database migrations, deployment, and merges.

This repository starts with a dependency-light implementation so it can be inspected, run, and extended without fragile setup.

## Current MVP

Implemented in this branch:

- Python policy engine for classifying AI engineering tasks as `GREEN`, `YELLOW`, `RED`, or `BLACK` risk.
- CLI for validating and classifying task packets.
- Dependency-free local supervisor server.
- Local JSON task and approval storage under `runtime/`.
- Static browser dashboard backed by the local server when running.
- Optional read-only GitHub inspection for repositories, issues, pull requests, and changed PR files.
- GitHub issue template for agent tasks.
- Pull request template with supervision gates.
- CI workflow for Python tests and policy smoke checks.
- Operating docs for architecture, roadmap, and human approval boundaries.

## What Buildforme Does

Buildforme does not replace the founder. It replaces screen-watching.

It helps answer:

- What should the agent work on next?
- Is the task safe to run unattended?
- Which files are allowed?
- Which actions require human approval?
- Did the agent provide enough proof?
- Should a PR be merged, reviewed, reworked, or blocked?

## What Buildforme Must Not Do

Buildforme must not automatically approve:

- production writes
- live payment capture
- production deployments
- database migrations
- secret handling
- legal/regulatory conclusions
- cross-tenant or auth-sensitive changes
- broad refactors
- merges to `main`

## Risk Levels

| Risk | Meaning | Automation rule |
| --- | --- | --- |
| `GREEN` | Read-only, docs-only, tests-only, or harmless analysis | May run unattended. Merge still depends on repo policy. |
| `YELLOW` | Scoped implementation with tests and no high-risk data effects | May prepare a PR. Requires review before merge. |
| `RED` | Security, tenancy, migrations, payments, deployments, production data, or legal/regulatory behavior | Plan only or prepare blocked PR. Requires Shan approval. |
| `BLACK` | Secrets exposure, auth bypass, fake success, production mutation without approval, or unsafe instructions | Reject automatically. |

## Run Locally

Requires Python 3.11+.

Run verification:

```bash
python -m unittest discover -s tests -p 'test_*.py'
python -m buildforme.cli classify data/sample_task.json
```

Run the local supervisor app:

```bash
python -m buildforme.cli serve
```

Then open:

```text
http://127.0.0.1:8787
```

The app will save local tasks and approval decisions to:

```text
runtime/buildforme_state.json
```

`runtime/` is intentionally ignored by git.

## Optional GitHub Read-Only Inspection

Public repositories can be checked without a token, subject to GitHub public API rate limits.

For private repositories or higher limits, set one local environment variable before starting the server:

```bash
export BUILDFORME_GITHUB_TOKEN=...
# or
export GITHUB_TOKEN=...
```

The token is used only as an Authorization header for read-only API calls. It is not shown in the UI, not saved to the runtime state file, and must not be committed.

The local server currently exposes read-only endpoints:

```text
GET  /api/health
GET  /api/tasks
POST /api/classify
POST /api/tasks
POST /api/decisions
GET  /api/github/repo?repository=owner/name
GET  /api/github/issues?repository=owner/name&state=open&limit=20
GET  /api/github/pr?repository=owner/name&number=1
```

## Repository Layout

```text
buildforme/                Python policy engine, CLI, server, storage, GitHub client
public/                    Browser dashboard
runtime/                   Local ignored state files
data/                      Example task packets
docs/                      Architecture, operating model, and roadmap
tests/                     Unit and local server tests
.github/                   CI, issue template, PR template
```

## Intended Next Build Steps

1. Add dashboard views for open PRs, failed CI, blocked approvals, and next recommended task.
2. Add GitHub issue/PR synchronization into local task records.
3. Add provider adapter contracts for Claude, Codex, GLM, and other agents.
4. Add scheduled digest generation.
5. Add a kill switch and repository lock state.
6. Add owner authentication before any hosted deployment.

## Safety Position

The app is intentionally conservative. It should prefer blocking work over approving dangerous work silently.

If the classification is uncertain, the task must be treated as `RED` until a human approves it.
