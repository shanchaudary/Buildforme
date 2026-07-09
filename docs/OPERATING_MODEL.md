# Operating Model

Buildforme is designed to let agents work while Shan is not watching the screen, without giving agents ownership authority.

## Roles

| Role | Responsibility |
| --- | --- |
| Founder | Final approval for high-risk work, roadmap direction, merges, production deployment |
| Builder Agent | Executes one scoped task on a branch |
| Reviewer Agent | Red-teams the builder output |
| Policy Engine | Classifies task and PR risk |
| CI | Runs objective verification commands |
| Buildforme | Coordinates packets, risk, status, and approvals |

## Approval layers (do not conflate)

| Layer | Meaning | Who | GitHub effect |
| --- | --- | --- | --- |
| **Local Buildforme approval** | Shan recorded a note/decision in `runtime/approvals.json` | Founder via dashboard | **None** — local only |
| **GitHub PR review/approval** | Formal review on the PR | Human on GitHub | Appears on the PR |
| **Merge approval** | Permission to merge to the target branch | Founder / branch protection | Required for merge; Buildforme never auto-merges |

Stage 2 only implements **local Buildforme approval**. UI copy must keep that explicit.

## Agent packets (Stage 3)

| Concept | Meaning |
| --- | --- |
| **Generated packet** | Structured instructions for an external agent (Markdown/JSON) |
| **Not approval** | Saving or copying a packet does not approve RED work or merges |
| **Not execution** | Buildforme does not call Grok/Codex/Claude/GLM in this stage |
| **Scope** | Agent must not exceed allowed files/actions in the packet |
| **Human gate** | RED/BLACK, production, secrets, payments, deploy, merge still need Shan |

Local packet save writes only to `runtime/packets.json`. It does **not** mutate GitHub.

## Chief planner (Stage 4)

| Concept | Meaning |
| --- | --- |
| **Project truth** | Evidence-backed claims about reality (not hopes) |
| **Roadmap intent** | Stages and planned tasks (local only) |
| **Recommendation** | Deterministic next action with score + explanation |
| **Not authorization** | Planner never grants merge/production authority |
| **Needs Shan** | RED/BLACK/security/founder decisions |

Unverified truth is never treated as completed work.

## Risk Matrix

### GREEN

Examples:

- read-only audit
- documentation-only changes
- test-only additions with no production behavior change
- PR review
- CI rerun

Automation:

- may run unattended
- must report final status
- must not auto-merge by default

### YELLOW

Examples:

- scoped bug fix
- parser fix
- UI/API response-shape alignment
- defensive rendering
- non-sensitive test coverage

Automation:

- may create branch/PR
- requires second-pass review
- no merge without approval

### RED

Examples:

- auth/session changes
- tenant isolation/RLS
- database migrations
- production writes
- Stripe/payment
- ERP credential storage
- S3/report delivery
- email to real users
- production deployment
- legal/regulatory output behavior

Automation:

- plan only unless Shan approves execution
- PR must remain blocked until explicit approval

### BLACK

Examples:

- print secrets
- commit `.env`
- bypass auth
- fake success
- hide failing tests
- production mutation without approval
- merge without review

Automation:

- reject automatically

## Approval Rule

Agents may prepare work. They may not become Shan.

Buildforme can reduce the amount of time Shan spends supervising, but it must preserve human authority over high-risk decisions.

## Daily Workflow Target

When fully built, the system should support:

```text
Morning digest:
- What ran overnight
- What passed
- What failed
- What needs Shan
- Recommended next action

Evening digest:
- New PRs
- Review findings
- Blockers
- Next green/yellow tasks ready to run
```

## Kill Switch

Future versions must include a kill switch that can:

- pause all agent launches
- mark all pending approvals blocked
- stop auto-run for green/yellow tasks
- prevent merge recommendations
- preserve audit logs
