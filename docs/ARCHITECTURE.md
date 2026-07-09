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

## Future Components

```text
GitHub Issues / PRs / CI
    ↓
Repository watcher
    ↓
Policy engine
    ↓
Agent router
    ├── Claude adapter
    ├── Codex adapter
    ├── GLM adapter
    └── Future agent adapter
    ↓
Reviewer agent
    ↓
Approval queue
    ↓
Merge / reject / rework
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
