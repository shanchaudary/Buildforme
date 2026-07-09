# Buildforme Roadmap

## Stage 0 — MVP Foundation

Status: started in `founder-control-plane-mvp`.

Exit criteria:

- Policy engine classifies task packets.
- CLI can classify JSON packets.
- Static dashboard drafts and classifies packets.
- GitHub issue/PR templates exist.
- CI runs tests.

## Stage 1 — GitHub Read-Only Supervisor

Objective:

- Pull issues, PRs, changed files, reviews, labels, and CI statuses.
- Produce a daily approval digest.
- Classify PRs against the risk matrix.

Forbidden in this stage:

- merging PRs
- editing files through GitHub API
- triggering production deployments
- handling secrets beyond presence checks

## Stage 2 — Task Packet Orchestrator

Objective:

- Convert founder intent into task packets.
- Queue green/yellow tasks.
- Block red/black tasks.
- Maintain audit log.

Exit criteria:

- Every launched task has task ID, risk, scope, acceptance criteria, and reviewer requirement.

## Stage 3 — Agent Adapter Interface

Objective:

- Define provider-neutral adapter contract.
- Add manual adapter first.
- Add Claude/Codex/GLM adapters only after secrets and approval gates exist.

Exit criteria:

- Adapters cannot override policy decisions.
- Red and black tasks cannot run without approval.

## Stage 4 — Approval Queue

Objective:

- Web UI for pending approvals.
- Approve/reject/rework decisions.
- Immutable audit events.

Exit criteria:

- Shan sees only meaningful decisions, not raw terminal noise.

## Stage 5 — Notifications

Objective:

- Email or chat digests.
- Urgent approval alerts.
- Failure reports.

Exit criteria:

- Daily summary can be delivered without exposing secrets.

## Stage 6 — Safe Write Actions

Objective:

- Create labels/issues/comments/PR review requests through GitHub.
- Never merge automatically.

Exit criteria:

- All writes are audited.
- Dangerous writes remain blocked.

## Stage 7 — Deployment

Objective:

- Authenticated hosted app.
- Secret manager.
- Backups.
- Observability.
- Kill switch.

Exit criteria:

- Production-readiness checklist passed or accepted risks documented.
