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
