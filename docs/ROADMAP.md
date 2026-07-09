# Buildforme Roadmap

Buildforme is being built as a staged supervisor for AI engineering agents. The goal is not to give agents unchecked authority. The goal is to route work through explicit safety gates, task packets, PRs, tests, reviews, and human approval only where required.

## Stage 0 — Governance Foundation

Status: implemented in the first MVP branch.

Delivered:

- AGENTS operating law.
- Risk policy model.
- Task packet shape.
- PR safety template.
- Agent task issue template.
- CI gate.
- Static dashboard.

Exit criteria:

- Task classifications are deterministic.
- Unsafe requests are rejected or blocked.
- CI verifies policy behavior.

## Stage 1 — Local Testable Supervisor

Status: implemented in the current MVP branch.

Delivered:

- Dependency-free local HTTP server.
- Server-backed task classification.
- Local JSON task persistence.
- Local approval decision recording API.
- Browser dashboard that can save and list tasks.
- Optional read-only GitHub repository, issue, PR, and changed-file inspection.
- Tests for policy, storage, GitHub client helpers, and local server endpoints.

Exit criteria:

- `python -m unittest discover -s tests -p 'test_*.py'` passes.
- `python -m buildforme.cli classify data/sample_task.json` passes.
- `python -m buildforme.cli serve` starts the app.
- Browser can classify and save a task locally.
- Browser can inspect a public GitHub PR without secrets.

## Stage 2 — GitHub Work Queue

Next.

Objective:

- Turn GitHub Issues and PRs into a usable work queue.
- Show blocked, ready, failed, and needs-review states.
- Map labels such as `risk:green`, `risk:yellow`, `risk:red`, `blocked:shan`, `stage:A` into the dashboard.

Tasks:

- Sync selected GitHub issues into local records.
- Sync selected PR metadata and changed files into local records.
- Pull CI status for PR head commits.
- Add dashboard filters for risk, stage, and approval state.
- Add a review checklist for each PR.

Do not build yet:

- autonomous provider execution
- auto-merge
- production deployment
- secret storage

## Stage 3 — Agent Adapter Contracts

Objective:

- Define provider-neutral adapter contracts for Claude, Codex, GLM, and future coding agents.
- Keep provider credentials outside task packets and outside git.

Tasks:

- Add adapter interface docs and stub-free contracts.
- Add task dispatch plan format.
- Add result intake format.
- Add reviewer-agent packet format.

Do not build yet:

- live provider calls unless approved
- background scheduling
- autonomous merge

## Stage 4 — Approval Queue and Kill Switch

Objective:

- Give the owner a dashboard that clearly separates auto-runnable work from blocked human decisions.

Tasks:

- Add approval queue views.
- Add `PAUSED` repository state.
- Add kill-switch state file.
- Block all non-read-only work when paused.
- Add digest output.

## Stage 5 — Hosted, Authenticated Control Plane

Objective:

- Deploy Buildforme behind owner authentication.

Required gates:

- Authentication.
- Authorization.
- Secret manager.
- HTTPS.
- Audit logging.
- Backup/restore plan.
- No provider secrets shown in UI.

## Stage 6 — Scheduled Supervision

Objective:

- Let Buildforme prepare daily summaries and safe next-task packets while the owner is away.

Tasks:

- Scheduled digest.
- PR review digest.
- CI failure digest.
- Blocked approval digest.

Forbidden until explicitly approved:

- automatic merge
- production writes
- live payment actions
- regulatory/legal conclusions
