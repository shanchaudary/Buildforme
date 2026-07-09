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
