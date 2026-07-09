# Buildforme

Buildforme is a founder-control-plane MVP for supervising AI coding agents without giving them unrestricted authority.

The goal is to let strong agents such as Claude, Codex, GLM, or future coding systems keep moving a software project while the owner is away, while preserving explicit safety gates for secrets, production writes, payments, regulatory/legal claims, database migrations, deployment, and merges.

This repository starts with a dependency-light implementation so it can be inspected, run, and extended without fragile setup.

## Current MVP

Implemented in this branch:

- Python policy engine for classifying AI engineering tasks as `GREEN`, `YELLOW`, `RED`, or `BLACK` risk.
- CLI for validating and classifying task packets.
- Dependency-free local supervisor server.
- Local JSON task, watched-repo, and approval storage under `runtime/`.
- Polished browser dashboard (dark control-plane UI).
- **Stage 2 GitHub Work Queue**: open PRs/issues, CI status, risk, recommended next task, local-only approvals.
- **Stage 3 Agent Packet Generator**: tool-neutral handoff packets for Grok/Codex/Claude/GLM (no live execution).
- **Stage 4 Chief Planner**: projects, roadmap, project truth, deterministic next-action ranking, founder briefing.
- Optional read-only GitHub inspection for repositories, issues, pull requests, changed files, and commit checks.
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
- Which PRs/issues need attention right now?
- Did CI pass, fail, pend, or is status unknown?
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

**Stage 2–3 still do not:**

- call AI providers (Claude/Codex/Grok/GLM)
- launch autonomous coding runs
- merge or auto-merge PRs
- edit GitHub issues, labels, or reviews
- deploy or host multi-user auth

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

### Dashboard pages

1. **Classify task** — draft a task packet and classify risk  
2. **Saved tasks** — local task history  
3. **GitHub inspect** — single PR/issue read-only check  
4. **Work queue** — watched repos, open PRs/issues, CI, risk, next action  
5. **Approvals** — local-only decisions (not GitHub reviews)  
6. **Agent packets** — generate / copy / save / download handoff packets  
7. **Chief planner** — ranked next action, blockers, briefing  
8. **Projects** — registry, roadmap stages, planned tasks, project truth  
9. **Risk policy** — GREEN/YELLOW/RED/BLACK guide  

### Chief planner

```bash
python -m buildforme.cli load-sample-project
python -m buildforme.cli plan buildforme
python -m buildforme.cli briefing
```

Scoring rules: `docs/PLANNER_SCORING.md`.

Recommendations are **not** merge/production authority.

### Agent packet generator

Buildforme can turn a manual objective, saved task, PR, or issue into a **complete handoff packet** you paste into any coding agent.

It does **not** run the agent. It does **not** call Claude/Codex/Grok/GLM APIs. It does **not** authorize production writes, secrets, deployments, payments, merges, or GitHub mutations.

**Browser**

1. Open **Agent packets**.  
2. Choose source: Manual / Saved task / PR / Issue.  
3. Click **Generate packet**.  
4. **Copy**, **Save locally**, or **Download .md**.  

**CLI**

```bash
python -m buildforme.cli generate-packet data/sample_task.json
# alias:
python -m buildforme.cli packet data/sample_task.json
# full JSON:
python -m buildforme.cli generate-packet data/sample_task.json --json
```

Saved packets live in `runtime/packets.json` (gitignored).

### Work queue smoke test

1. Start the server and open the dashboard.  
2. Open **Work queue**.  
3. Confirm watched repo defaults to `shanchaudary/Buildforme` (or add it).  
4. Click **Refresh queue**.  
5. Confirm PR #1 (or current open PRs) appears with risk, files, and CI status.  
6. Use a local action such as **Mark locally reviewed** and confirm it appears under **Approvals**.  
7. Confirm the UI never shows a token value.

Local state files (gitignored):

```text
runtime/buildforme_state.json   # tasks (legacy + primary)
runtime/tasks.json              # task mirror
runtime/repos.json              # watched repositories
runtime/approvals.json          # local work-queue approvals
```

## Optional GitHub Read-Only Access

Public repositories can be checked without a token, subject to GitHub public API rate limits.

For private repositories or higher limits, set one local environment variable before starting the server:

```bash
export BUILDFORME_GITHUB_TOKEN=...
# or
export GITHUB_TOKEN=...
```

### What the token is used for

- Authorization header on **read-only** GitHub REST API calls  
- Listing open PRs and issues  
- Reading PR metadata, changed files, and commit check/status data  

### What the token is never used for

- Merging PRs  
- Creating reviews or approvals on GitHub  
- Editing issues, labels, or comments  
- Pushing code  
- Any write API  

The token is not shown in the UI, not saved under `runtime/`, and must not be committed.

## API surface (local server)

```text
GET    /api/health
GET    /api/tasks
POST   /api/classify
POST   /api/tasks
POST   /api/decisions
GET    /api/repos
POST   /api/repos
DELETE /api/repos/{owner%2Fname}
GET    /api/approvals
POST   /api/approvals
GET    /api/packets
POST   /api/packets
GET    /api/packets/{id}
DELETE /api/packets/{id}
POST   /api/packets/generate
POST   /api/packets/from-pr
POST   /api/packets/from-issue
GET    /api/work-queue?repos=owner/name,owner/name
GET    /api/pr/{owner}/{repo}/{number}/status
GET    /api/github/repo?repository=owner/name
GET    /api/github/issues?repository=owner/name&state=open&limit=20
GET    /api/github/pr?repository=owner/name&number=1
```

All GitHub-backed routes are read-only against GitHub. Approvals, decisions, and agent packets are local only.

## Repository Layout

```text
buildforme/                Policy, CLI, server, storage, GitHub client, work queue
public/                    Browser dashboard
runtime/                   Local ignored state files
data/                      Example task packets
docs/                      Architecture, operating model, and roadmap
tests/                     Unit and local server tests
.github/                   CI, issue template, PR template
```

## Intended Next Build Steps

1. ~~GitHub work queue~~ (Stage 2)  
2. ~~Agent packet generator~~ (Stage 3)  
3. ~~Chief planner~~ (Stage 4)  
4. Stage 5 — Execution adapter foundation (no live autonomous runs yet)  
5. Kill switch and repository lock state  
6. Scheduled digests / founder briefings automation  
7. Owner authentication before any hosted deployment  

## Safety Position

The app is intentionally conservative. It should prefer blocking work over approving dangerous work silently.

If the classification is uncertain, the task must be treated as `RED` until a human approves it.

**Local approval ≠ GitHub approval ≠ merge approval.**
