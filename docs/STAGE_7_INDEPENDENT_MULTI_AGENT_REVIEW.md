# Stage 7 — Independent Multi-Agent Review Loop

Status: in progress on `stage-7-independent-multi-agent-review-loop`.

## Objective

Stage 7 adds independent multi-agent review after a Stage 6 supervised run reaches
`needs_review`. Reviewers do not receive implementation authority and cannot review
work produced by the same provider identity.

## Packet 7A — accepted review authority foundation

- Review cycles are immutable and bind to the exact run, latest execution evidence,
  scope fingerprint, Constitution hash, Constitution lease, and implementer provider.
- At least two blind reviewers with distinct provider identities are required.
- The implementer provider cannot hold a reviewer assignment.
- Reports and findings are append-only and fingerprinted.
- Critical/high findings are always blocking.
- Quorum and aggregate verdicts are recomputed by SQLite authority.
- Founder acceptance is blocked unless the exact bound cycle is clear.
- Review reports remain hidden from other reviewers until cycle finalization.
- The same execution evidence cannot be reviewed repeatedly; repair and fresh execution
  evidence are required before a new cycle.

## Packet 7B — accepted automated blind reviewer execution

- Each assignment receives one immutable, fingerprinted blind-review packet.
- Before execution, Buildforme re-collects the worktree manifest and patch identity and
  requires exact equality with the bound Stage 6 execution evidence.
- Reviewer commands are code-owned. Packet 7B enables the verified Codex `exec`
  read-only sandbox contract; providers without an approved authentication and
  read-only command contract remain unavailable.
- The process runs through the Stage 6 supervisor with environment allowlisting,
  timeout, cancellation, process-tree cleanup proof, and kill-switch observation.
- The worktree is re-collected after review. Any change blocks the report and records
  immutable failed reviewer-execution evidence.
- Provider output must contain exactly one strict JSON review object. Markdown,
  ambiguous output, unknown fields, and authority claims are rejected.
- Successful reviewer process evidence and the report/findings commit atomically.
- Direct/manual report submission is disabled; the API exposes assignment execution.

### Packet 7B red-team hardening

- Reviewer assignments are claimed atomically before process launch; concurrent launches
  for one assignment are rejected.
- The exact registered repository, remote identity, Git common directory, worktree branch,
  and canonical Constitution lease are revalidated before review.
- Snapshot equality includes full changed-file metadata, including symlink target/escape facts.
- Post-review proof failure never substitutes the pre-review snapshot or claims unchanged state.
- Retry-safe failures require confirmed cleanup and a proven unchanged post-review snapshot.
  Integrity failures block the assignment and cycle atomically.
- Successful process evidence must match the code-owned provider command contract, exact argv,
  live-ready health, and verified authentication probe.
- Provider lookup and health-probe exceptions produce immutable failure evidence after claim.
- Final-tree contracts reject temporary gate scripts and validation artifacts.

### Packet 7B verification

- Focused Packet 7B reviewer process and storage-authority tests passed.
- Packet 7A and Stage 6 authority regressions passed.
- Complete repository suite passed.
- Policy smoke and Constitution validation passed.
- Ordinary PR CI run `29166026829` passed on accepted head
  `aff650d939345da4f5cf979d5f56241976257020`.

## Packet 7C — in progress: distinct-provider reviewer capability

Packet 7A requires at least two distinct reviewer providers. Packet 7B currently has one
approved reviewer command contract (Codex), so Stage 7 cannot yet claim a real review
quorum. Packet 7C must add at least one independently verified provider contract with:

- executable and version compatibility proof;
- machine-verifiable authentication status;
- code-owned noninteractive command shape;
- explicit read-only/no-tool-write enforcement;
- strict structured output transport;
- timeout/cancellation/process cleanup evidence;
- unchanged worktree proof;
- fail-closed unsupported-version behavior.

No provider contract may be invented from assumed CLI flags. Unsupported or unverifiable
providers remain unavailable.

## Remaining Stage 7 scope

- Packet 7C distinct-provider reviewer capability and real two-provider smoke.
- Governed repair-run generation from blocking findings.
- Fresh execution evidence and deterministic re-verification after repair.
- New independent review cycle after repair.
- CLI and browser operator surfaces.
- Final Stage 7 adversarial acceptance.

## Boundaries

No merge, deployment, production mutation, reviewer self-acceptance, review shopping,
or synthetic provider quorum. PR #10 remains draft until all Stage 7 capabilities are
complete and independently accepted.


### Packet 7C Claude reviewer contract

- Claude Code authentication is verified through the official machine-readable
  `claude auth status` JSON command; environment markers alone are not accepted.
- Claude Code must be version 2.1.205 or newer and its installed help must expose every
  required noninteractive, JSON Schema, read-only, tool-restriction, MCP-isolation,
  safe-mode, and no-session flag.
- The code-owned review command uses `--permission-mode plan`, limits built-in tools to
  `Read,Grep,Glob`, enables safe mode and strict MCP isolation, disables session
  persistence, and requires validated JSON Schema output.
- Claude output is accepted only from a successful result wrapper containing one
  `structured_output` object. Plain prose, plain report JSON, error subtypes, and missing
  structured output fail closed.
- Codex and Claude can now execute independent blind assignments and satisfy a genuine
  two-provider storage quorum in the integration path. A founder-controlled live smoke
  remains required before Packet 7C acceptance.


### Packet 7B red-team isolation hardening

Reviewer processes execute only in a disposable copied workspace. The governed execution
worktree is never used as reviewer cwd. The full disposable tree is fingerprinted before and
after review, the authoritative worktree is separately re-proved unchanged, escaping symlinks
are rejected, and the copy is destroyed on every outcome. Review packets also carry the
canonical Constitution reminder bound to the run lease.


## Packet 7D-A — governed repair-packet authority

- Exactly one append-only repair packet may be created from a finalized `repair_required` cycle.
- SQLite independently binds the source run, execution evidence, scope, Constitution, aggregate, every report fingerprint, and every persisted blocking finding.
- Allowed and forbidden files are copied exactly from the source execution packet; callers cannot expand repair scope.
- A provider that participated in the source review cannot author the repair. The selected repair provider becomes the implementer identity and is excluded from the next independent review.
- Packet creation only establishes immutable repair authority. The seed-commit and child-run admission seam remains the next Packet 7D implementation slice.


## Packet 7D-B — exact repair seed and child admission

- Buildforme creates a deterministic local seed commit from only the exact changed paths in the immutable source evidence. It does not move the source branch, modify the reviewed worktree, push a ref, or use the seed as the approved baseline.
- The seed is retained under a local `refs/buildforme/repair-seeds/` ref and independently revalidated against the source manifest before storage admission.
- The child run keeps the original approved baseline for complete diff/evidence verification while `execution_seed_commit` controls only the initial repair worktree state.
- Repair packet, seed proof, child run, fresh Constitution lease, scope fingerprint, task-lock transfer, admission record, source-run binding, and audit events commit through one dedicated SQLite transaction.
- A failed admission deletes a newly created seed ref; duplicate admission replays the one canonical child.


## Packet 7D-C — repair execution and mandatory fresh re-review

- Repair children are marked `stage7_review_required` at admission, so founder acceptance fails closed before a fresh clear cycle exists.
- After approved supervised repair execution reaches `needs_review` with deterministic verification passed, Buildforme opens a new cycle through the existing review-cycle authority.
- SQLite requires fresh child execution evidence, the exact source reviewer-provider set, and exclusion of the repair implementer.
- The repair packet, admission, child, fresh evidence, and new cycle are linked append-only; duplicate orchestration replays the same cycle.


## Packet 7E — operator surfaces and real reviewer smoke

- Founder-authenticated HTTP actions create repair packets, admit exact-seed children, execute approved repairs, and open mandatory fresh review cycles. The API rejects command, path, scope, reviewer, seed, and policy overrides.
- Local CLI commands expose the same governed repair workflow without adding a second authority.
- The dashboard adds a Stage 7 review/repair status panel with in-memory founder token and CSRF inputs; the page does not persist credentials.
- `scripts/stage7_real_two_provider_smoke.py` runs real Codex and Claude blind reviewer processes against a disposable, deterministically verified implementation fixture. The output explicitly discloses that the implementation is controlled rather than claiming a third-provider execution. Acceptance requires two real authenticated process records, a clear two-provider aggregate, unchanged source identity/patch, no direct report submission, and no merge.


### Packet 7E red-team hardening

- Repair HTTP mutations return forbidden on failed founder authentication, derive the audit actor only from the validated session, and reject every non-allowlisted request field.
- Smoke acceptance binds the two successful execution attempts to the two persisted report fingerprints and aggregate report fingerprints, the exact cycle/evidence/run binding, distinct implementer identity, and the actual Git merge-commit count. It no longer accepts caller-provided `no synthetic report` or `no merge` booleans.
