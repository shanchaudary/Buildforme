# Stage 6 — Multi-Provider Supervised Execution

## Objective

Enable Buildforme to execute bounded software-engineering tasks through multiple external AI coding providers under constitutional supervision.

## Providers (architecture required for all)

| Provider | Transport |
| --- | --- |
| Codex CLI | CLI adapter |
| Claude Code CLI | CLI adapter |
| Grok CLI | CLI adapter |
| GLM CLI | CLI adapter |

Optional API transports may exist. Unavailable providers are reported honestly—not faked.

## Capabilities

1. Provider discovery and health  
2. Provider recommendation and selection  
3. Provider-neutral adapter contract  
4. Isolated execution (worktree, locks, budgets, cleanup)  
5. Supervised process execution  
6. Constitutional binding  
7. Evidence collection  
8. Deterministic verification  
9. Review-required outcome (provider cannot self-accept)  
10. UI / CLI / API integration  
11. Failure and adversarial coverage  

## Boundaries

**May:** create/modify governed local feature branch + worktree.  

**Must not:** merge, deploy, production mutation, payments, legal conclusions, expose secrets, bypass founder approval, claim Stage 7 multi-agent review.

## Verification profiles

Repository-specific commands come from project `verification_profile` / project truth—never Cascada conditionals in core.

## Hardening requirements (merge-ready)

| Control | Rule |
| --- | --- |
| Changed files | Canonical manifest includes tracked/staged/unstaged/untracked/**ignored** (e.g. `.env`) |
| Process tree | Own process group/session; never signal parent CI/test process |
| Environment | Explicit allowlist only; no full parent env inheritance |
| Redaction | Central authority before any persistence/display |
| Repository | Registered binding only — no request `repo_root` authority |
| Baseline | Exact SHA pinned **before** approval; in scope fingerprint |
| Branch collision | Fail closed unless validated resume |
| Founder accept | Hard blocks cannot be overridden via `accept_for_pr_prep` |
| Evidence | Append-only; immutable evidence_id |
| Auth | `unknown` is **not** live-ready |
| Local mutate | Founder token + same-origin/loopback checks |

## Acceptance

Complete only when multi-provider architecture is real, isolation + evidence + independent verification + review gate work, cancellation/timeouts proved, **GitHub CI green**, no merge/deploy authority added, and the hardening table above is implemented and tested.
