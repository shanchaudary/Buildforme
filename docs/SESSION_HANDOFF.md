# Session handoff — Buildforme Founder Control Plane

**Written:** 2026-07-10  
**Repo:** https://github.com/shanchaudary/Buildforme  
**Local path:** `C:\Users\shahn\OneDrive\Desktop\Buildforme`  
**Purpose:** Resume work in a new agent session without re-deriving product state.

---

## 1. What this product is

**Buildforme** is a **founder control plane** for supervising AI coding agents (Grok, Claude, Codex, GLM, etc.).

It is **not** a generic AI wrapper and **not** an autonomous coding agent itself.

It:

1. Classifies task risk (GREEN / YELLOW / RED / BLACK)
2. Shows GitHub work (read-only)
3. Generates tool-neutral agent handoff packets
4. Plans next work deterministically (no LLM planner)
5. Gates future execution with kill switch, locks, preflight, dry-run only

**Default posture:** block uncertain work; never auto-merge; never expose secrets; no production authority without Shan.

---

## 2. Current truth on `main` (as of handoff)

| Item | Value |
| --- | --- |
| Branch | `main` |
| HEAD | `5cc60da` — *Apply Stage 5.5 governance hardening to main* |
| Working tree | clean |
| Tracking | `main` = `origin/main` |

### Merged stages on main

| Stage | PR | Summary |
| --- | --- | --- |
| 0–1 MVP | #1 | Policy engine, CLI, local server, dashboard, GitHub inspect |
| 2 Work queue | (in #1 branch lineage / later main) | Watched repos, PR/issue queue, CI, local approvals |
| 3 Packet generator | #2 | Tool-neutral packets; copy/save/download; no live agents |
| 4 Chief planner | #3 | Projects, roadmap, truth, ranking, briefing |
| 5 Execution safety | #4 | Kill switch, locks, run state machine, preflight, dry-run providers |
| 5.5 Governance hardening | #5 → Stage 5 branch; **#7 → main** | Adversarial fixes, fingerprints, fail-closed controls |
| 5.6 AI Constitution | *(this branch → PR)* | Constitutional law engine, leases, inheritance, UI |

### Closed / do not reopen

| PR | Note |
| --- | --- |
| **#6** | Closed (bad recovery attempt). **Leave closed.** |
| Old Stage 5 feature branch for new main PRs | Do not reuse for fresh main work |

### Historical branches (remote may still exist)

- `founder-control-plane-mvp` — early Stage 1–2
- `stage-3-agent-packet-generator`
- `stage-4-chief-planner`
- `stage-5-execution-safety-foundation` (has #5 merge)
- `stage-5-5-execution-governance-validation` (merged into Stage 5 branch via #5)
- `stage-5-5-main-hardening` (merged to main via #7)

---

## 3. What each stage delivered (implementation map)

### Stage 1 — Local supervisor
- `buildforme/policy.py` — deterministic risk
- `buildforme/cli.py` — classify, serve
- `buildforme/server.py` — dependency-free HTTP on `127.0.0.1:8787`
- `buildforme/storage.py` — local JSON under `runtime/` (gitignored)
- `public/` — polished dark control-plane UI
- Fixed CSS 404: static assets served from `/` and `/public/`

### Stage 2 — GitHub work queue
- `buildforme/github_client.py` — read-only REST
- `buildforme/work_queue.py` — assemble queue + recommended next
- Local approvals ≠ GitHub approvals
- CI status: passing / failing / pending / **unknown** (never invent pass)

### Stage 3 — Agent packet generator
- `buildforme/packet_generator.py`
- Sources: manual, task, PR, issue (+ planner handoff later)
- CLI: `python -m buildforme.cli generate-packet data/sample_task.json`
- APIs: `/api/packets/*`
- **No provider execution**

### Stage 4 — Chief planner
- `buildforme/planner.py` — deterministic scoring
- `buildforme/briefing.py` — founder briefing
- `docs/PLANNER_SCORING.md`
- Sample: `data/sample_project.json`
- CLI: `load-sample-project`, `plan`, `briefing`
- UI: Chief planner, Projects (roadmap + truth)

### Stage 5 — Execution safety foundation
- `buildforme/run_state.py` — explicit state machine
- `buildforme/execution_preflight.py` — deny-by-default checks
- `buildforme/execution_service.py` — create/preflight/approve/dry-run/cancel/retry
- `buildforme/providers.py` — Codex/Claude/Grok/GLM profiles
- `buildforme/adapters/dry_run.py` — **no network/shell/GitHub**
- UI: **Execution control** — **no Run live button**
- CLI: `execution-status`, `kill-switch`, `providers`, `run-*`

### Stage 5.5 — Governance validation (on main via #7)
- `buildforme/governance.py` — strict bool, IDs, branch, capabilities, scope fingerprint (SHA-256)
- Fail-closed project execution control when record missing
- Approval bound to immutable scope fingerprint
- Kill switch revalidated at dry-run
- BLACK / forbidden capabilities blocked at create
- Provider live/credential escalation rejected
- Adversarial tests: `tests/test_governance_adversarial.py`
- Doc: `docs/STAGE_5_5_GOVERNANCE_VALIDATION.md`
- CLI: `python -m buildforme.cli governance-validate`

### Stage 5.6 — AI Constitution & Governance Engine
- `governance/AI_CONSTITUTION.json` + `.md` — 20 immutable engineering laws
- `governance/constitution_*.py` — hash, lease, inheritance, validator, audit, engine
- Packets/runs/approvals/providers inherit constitution version + hash
- Provider must acknowledge before run create; leases immutable per run
- Compact reminders (not full re-inject every prompt)
- UI: Constitution page; CLI: `constitution-status|validate|refresh|export`
- Docs: `docs/AI_CONSTITUTION.md`, `CONSTITUTION_ENGINE.md`, `CONSTITUTION_LEASES.md`, `ENGINEERING_LAWS.md`
- **Still no live providers / secrets / GitHub writes / Stage 6**

---

## 4. Safety laws (never weaken)

From `AGENTS.md` + product intent:

- No secrets in git, UI, or runtime JSON
- No auto-merge
- No production writes / payments / deploys without human gates
- Prefer blocking uncertain work over approving danger
- GitHub: **read-only** from the app through Stage 5.5
- Local approval ≠ GitHub review ≠ merge authority
- Packet ≠ approval ≠ execution
- Dry-run ≠ live agent call
- Missing governance truth → **fail closed**

---

## 5. How to run (quick)

```bash
cd C:\Users\shahn\OneDrive\Desktop\Buildforme
git checkout main
git pull --ff-only origin main

python -m unittest discover -s tests -p "test_*.py"
python -m buildforme.cli classify data/sample_task.json
python -m buildforme.cli generate-packet data/sample_task.json
python -m buildforme.cli load-sample-project
python -m buildforme.cli plan buildforme --local-only
python -m buildforme.cli governance-validate
python -m buildforme.cli constitution-validate
python -m buildforme.cli constitution-refresh
python -m buildforme.cli constitution-status
python -m buildforme.cli serve
# open http://127.0.0.1:8787
```

Optional GitHub token (read-only only):

```bash
# PowerShell
$env:BUILDFORME_GITHUB_TOKEN = "..."
```

Never commit tokens. Never show them in UI.

---

## 6. Runtime storage (local only, gitignored)

Under `runtime/` (typical):

- `buildforme_state.json` / `tasks.json` — tasks
- `repos.json`, `approvals.json`, `packets.json`
- `projects.json`, `stages.json`, `planned_tasks.json`, `project_truth.json`
- `events.json`, `briefings.json`
- `execution_control.json`, `project_execution_controls.json`
- `repository_locks.json`, `providers.json`
- `runs.json`, `run_events.json`, `run_approvals.json`

---

## 7. Dashboard navigation (expected)

1. Execution control  
2. Chief planner  
3. Projects  
4. Classify task  
5. Saved tasks  
6. GitHub inspect  
7. Work queue  
8. Approvals  
9. Agent packets  
10. Risk policy  

---

## 8. Explicitly NOT built (do not invent)

- Live Claude / Codex / Grok / GLM API calls  
- Credential storage / secret entry UI  
- GitHub writes (issues, labels, PR create, merge)  
- Auto-merge, deploy, production mutation  
- Hosted multi-user auth  
- Background workers / Temporal  
- Stage 6 supervised live pilot  

---

## 9. Recommended next stage (when Shan approves)

### Stage 6 — Supervised Live Agent Adapter Pilot (NOT started)

Only after:

1. Stage 5 + 5.5 are on main and smoke-tested (done for merge; re-smoke if needed)
2. Shan **explicitly** approves Stage 6
3. Scope remains: **one provider**, **one repo**, **GREEN/YELLOW only**, feature branch, **no merge/deploy**, kill switch + run logs enforced

Do **not** start Stage 6 in a new session unless Shan asks for it by name.

---

## 10. Operating modes for agents in this repo

Declare one of:

- `READ_ONLY_AUDIT`
- `PLAN_ONLY`
- `DOCUMENTATION_ONLY`
- `IMPLEMENTATION`
- `REVIEW`
- `RELEASE` (requires explicit user approval)
- `SECURITY_VALIDATION_AND_SCOPED_REMEDIATION` (Stage 5.5 style)

Follow `AGENTS.md` final report template when doing implementation work.

---

## 11. Key bugs already fixed (don’t reintroduce)

| Issue | Fix |
| --- | --- |
| CSS/JS 404 at `/` | Serve public assets at root + `/public/` |
| Forbidden `.env` listed as high risk | Only allowed/changed paths escalate sensitivity |
| `auth` matching `authority` | Word-boundary pattern hits |
| Docs about deployment = deploy | Mode-aware RED filtering for docs/audit |
| Missing approvals → preflight_failed forever | Missing = warning → `awaiting_approval` |
| `bool("false")` kill switch | `parse_bool_strict` |
| Missing project exec control → enabled | Fail closed unless explicit record |
| Approvals reusable after packet change | SHA-256 scope fingerprint |

---

## 12. Session continuity checklist for next agent

```text
[ ] git fetch origin && git checkout main && git pull --ff-only
[ ] Confirm HEAD is 5cc60da or later with Stage 5.5 on main
[ ] Read AGENTS.md + this handoff
[ ] Ask Shan what to build next (do not invent Stage 6)
[ ] Create a new feature branch from main
[ ] Keep dependency-light architecture (stdlib server + static public/)
[ ] Never claim live agents or GitHub writes exist
```

---

## 13. Owner / authority

- **Founder:** Shan (final approval for RED/BLACK, merge, deploy, payments, secrets)
- **Buildforme:** coordinates classification, planning, packets, dry-run supervision only

---

*End of handoff. Prefer this file + git history over re-deriving conversation context.*
