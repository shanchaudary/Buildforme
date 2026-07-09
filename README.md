# Buildforme

Buildforme is a founder-control-plane MVP for supervising AI coding agents without giving them unrestricted authority.

The goal is to let strong agents such as Claude, Codex, GLM, or future coding systems keep moving a software project while the owner is away, while preserving explicit safety gates for secrets, production writes, payments, regulatory/legal claims, database migrations, deployment, and merges.

This repository starts with a dependency-light implementation so it can be inspected, run, and extended without fragile setup.

## Current MVP

Implemented in this branch:

- Python policy engine for classifying AI engineering tasks as `GREEN`, `YELLOW`, `RED`, or `BLACK` risk.
- CLI for validating and classifying task packets.
- Static browser dashboard for drafting task packets and seeing the same approval model.
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

```bash
python -m unittest
python -m buildforme.cli classify data/sample_task.json
```

Open the static dashboard:

```bash
python -m http.server 8000
```

Then open:

```text
http://localhost:8000/public/
```

## Repository Layout

```text
buildforme/                Python policy engine and CLI
public/                    Static local dashboard
data/                      Example task packets
docs/                      Architecture, operating model, and roadmap
tests/                     Unit tests
.github/                   CI, issue template, PR template
```

## Intended Next Build Steps

1. Add GitHub API ingestion for issues, PRs, labels, and CI status.
2. Add local persistent storage for tasks and approvals.
3. Add provider adapters for Claude, Codex, GLM, and other agents.
4. Add scheduled digest generation.
5. Add a kill switch and repository lock state.
6. Add deployment behind owner authentication.

## Safety Position

The app is intentionally conservative. It should prefer blocking work over approving dangerous work silently.

If the classification is uncertain, the task must be treated as `RED` until a human approves it.
