# Buildforme Architecture

Buildforme is a supervision layer for AI engineering agents. It is not an autonomous agent by itself in this MVP. It classifies work, produces guardrails, and creates a control surface for humans and reviewer agents.

## Design Principles

1. GitHub is the source of work truth: issues, branches, PRs, CI, reviews.
2. Agents work through task packets, not vague prompts.
3. Risk is deterministic and explainable.
4. Human approval is required for high-risk actions.
5. No model gets direct authority to merge, deploy, charge, expose secrets, or mutate production data.
6. Provider-specific agent logic belongs behind adapters.

## Current Components

```text
Browser dashboard
    ↓
Task packet JSON
    ↓
Policy engine
    ↓
Risk classification
    ↓
Human / reviewer / agent action
```

### Stage 2 — Work queue data flow

```text
GitHub REST API (read-only)
    ↓
github_client.py
    ↓
work_queue.py  (+ policy.py classify_github_item)
    ↓
server.py  /api/work-queue
    ↓
Dashboard Work queue page
    ↓
Local approvals → storage.py → runtime/approvals.json
```

Notes:

- GitHub token (optional) is read from environment only and never persisted or rendered.
- Local approval records are **not** GitHub reviews and grant **no** merge rights.
- CI unknown must never be labeled as passing.

### Stage 3 — Agent packet generator data flow

```text
Work queue item / saved task / PR / issue / manual form
    ↓
packet_generator.py
    ↓
policy.py  (risk + mode-aware allowed actions)
    ↓
server.py  /api/packets/*
    ↓
Dashboard Agent packets page
    ↓
runtime/packets.json  (local only; scrubbed of secret-like fields)
```

Generated packets are **instructions**, not approvals and not executions.

### Stage 4 — Chief planner data flow

```text
projects / stages / planned_tasks / project_truth (local runtime)
        + optional read-only GitHub queue signals
    ↓
planner.py  (deterministic scores + hard rules)
    ↓
recommendation + blockers + confidence
    ↓
briefing.py  (founder briefing)
    ↓
dashboard Chief planner / Projects
    ↓
optional → packet_generator.py (Stage 3 handoff)
```

## Future Components

```text
GitHub Issues / PRs / CI
    ↓
Work queue (Stage 2 — implemented)
    ↓
Agent packet generator (Stage 3 — implemented)
    ↓
Chief planner / roadmap brain (Stage 4 — implemented)
    ↓
Execution adapter foundation (Stage 5)
    ├── Claude adapter contract
    ├── Codex adapter contract
    ├── Grok adapter contract
    └── GLM adapter contract
    ↓
Supervised agent runs (later)
    ↓
Approval queue / kill switch
    ↓
Human merge decision (never auto-merge by default)
```

## Authority Boundaries

Buildforme may eventually automate:

- creating issues
- drafting task packets
- launching green/yellow agent tasks
- reading PR diffs
- reading CI status
- requesting reviews
- generating daily summaries

Buildforme must not automatically perform:

- live production writes
- live payment capture
- database migration merge
- production deployment
- secret rotation
- final legal/regulatory approval
- auto-merge to main

## Data Model, Future

Planned persistent entities:

- Project
- Repository
- TaskPacket
- RiskClassification
- AgentRun
- Review
- ApprovalDecision
- AuditEvent
- PolicyVersion
- Notification

## First Integration Target

The first real integration should be GitHub read-only ingestion:

- list issues
- list PRs
- list changed files
- read CI statuses
- classify PR risk
- generate approval digest

Write actions should come later and must remain gated.
