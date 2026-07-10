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

Status: implemented.

Delivered:

- Dependency-free local HTTP server.
- Server-backed task classification.
- Local JSON task persistence.
- Local approval decision recording API.
- Browser dashboard that can save and list tasks.
- Optional read-only GitHub repository, issue, PR, and changed-file inspection.
- Tests for policy, storage, GitHub client helpers, and local server endpoints.
- Polished dark control-plane UI (sidebar navigation).

Exit criteria:

- `python -m unittest discover -s tests -p 'test_*.py'` passes.
- `python -m buildforme.cli classify data/sample_task.json` passes.
- `python -m buildforme.cli serve` starts the app.
- Browser can classify and save a task locally.
- Browser can inspect a public GitHub PR without secrets.

## Stage 2 — GitHub Work Queue

Status: implemented in the current MVP branch.

Objective:

- Turn GitHub Issues and PRs into a usable work queue.
- Show blocked, ready, failed, and needs-review states.
- Recommend the next action for Shan without screen-watching.

Delivered:

- Watched repository storage (`runtime/repos.json`).
- Work queue API assembling open PRs, issues, CI, risk, and recommended next task.
- PR changed-file summary and commit check/status normalization (`passing` / `failing` / `pending` / `unknown`).
- Local work-queue approvals (`runtime/approvals.json`) that never write to GitHub.
- Dashboard pages: Work queue + Approvals (existing Classify / Saved / Inspect preserved).
- Policy helper `classify_github_item` and recommended-action strings.
- Tests for policy, GitHub client, storage, server, and work-queue ranking.

Do not build yet:

- autonomous provider execution
- auto-merge
- production deployment
- secret storage
- GitHub label writes or PR reviews via API

## Stage 3 — Agent Packet Generator

Status: implemented in the current branch.

Objective:

- Given an issue/PR/work-queue item/saved task/manual objective, generate a complete safe task packet for Codex, Claude, GLM, or Grok.
- Still no live agent execution.
- Still no auto-merge.
- Still no production authority.

Delivered:

- `buildforme/packet_generator.py` with tool-neutral Markdown packets.
- Dashboard **Agent packets** page (generate, copy, save, download).
- APIs: `POST /api/packets/generate`, `GET/POST/DELETE /api/packets`, `from-pr`, `from-issue`.
- Local storage `runtime/packets.json` with secret-field scrubbing.
- CLI: `python -m buildforme.cli generate-packet data/sample_task.json`.
- Policy nuance: docs/audit about high-risk topics vs execution; BLACK unchanged.
- Tests for generator, storage, server, policy, CLI.

## Stage 4 — Chief Planner and Roadmap Brain

Status: implemented in branch `stage-4-chief-planner`.

Objective:

- Deterministic project truth, roadmap stages, planned tasks, dependencies.
- Ranked next-action recommendations with explainable scores.
- Founder briefing without fabricated history.
- Handoff into Stage 3 packet generator.

Delivered:

- `buildforme/planner.py`, `briefing.py`
- Storage: projects, stages, planned_tasks, project_truth, events, briefings
- APIs for projects/roadmap/truth/plan/briefing/packet handoff
- UI: Chief planner + Projects
- Sample project `data/sample_project.json`
- CLI: `load-sample-project`, `plan`, `briefing`
- `docs/PLANNER_SCORING.md`

Still forbidden: live agents, GitHub writes, auto-merge, production authority.

## Stage 5 — Execution Safety Foundation

Status: implemented in branch `stage-5-execution-safety-foundation`.

Objective:

- Kill switch, project pause/lock, repository locks
- Supervised run state machine and preflight
- Dry-run provider registry (Codex/Claude/Grok/GLM)
- Append-only run events, approvals, budgets/timeouts
- No live provider calls

## Stage 5.6 — AI Constitution & Governance Engine

Status: implemented on main.

## Stage 6 — Multi-Provider Supervised Execution

Status: in progress on `stage-6-multi-provider-supervised-execution`.

**Complete integrated stage** (not pilot-only / not one-provider-only):

- Provider discovery, health, recommendation for Codex / Claude / Grok / GLM CLI
- Provider-neutral adapter contract + real adapter modules for all four
- Isolated worktrees, locks, process supervision, cancellation, timeouts
- Constitutional binding (append-only leases)
- Evidence + deterministic verification + review-required outcomes
- CLI / API / UI integration
- Adversarial and failure-path tests

Boundaries: no merge, deploy, production mutation, secret exposure, or Stage 7 multi-agent review claims.

See `docs/STAGE_6_MULTI_PROVIDER_EXECUTION.md` and `docs/COMPLETE_BUILD_DIRECTIVE.md`.

## Stage 7 — Independent Multi-Agent Review Loop

Next after Stage 6 acceptance. Reviewer independence, structured findings, repair loop, re-verification.

## Stage 8 — Founder Briefings and Decision Control

Cross-project evidence-backed briefings and required founder decisions.

## Stage 9 — Multi-Company Control Plane

Project/company isolation with singular global Constitution.

## Stage 10 — Controlled Autonomous Company

Governed continuous operation with kill switches, budgets, and founder gates. Not unrestricted autonomy.

Forbidden without explicit founder policy:

- automatic merge
- production writes
- live payment actions
- regulatory/legal conclusions
