# Stage 7 — Independent Multi-Agent Review Loop

## Status

Packet 7A implements the non-bypassable review authority foundation.
Stage 7 is not complete until automated reviewer execution, repair orchestration,
and re-verification are implemented and independently accepted.

## Packet 7A delivered authority

- Review cycles bind to one execution run, exact immutable execution evidence,
  scope fingerprint, Constitution hash, lease, and implementer provider.
- Minimum two blind reviewers with distinct provider identities.
- The implementation provider cannot review its own work.
- Reviewer assignments are immutable and one provider may submit only once.
- Reports and findings are append-only and fingerprinted.
- Critical and high findings are always blocking.
- Reviewers cannot claim founder, merge, deploy, or acceptance authority.
- Aggregation is deterministic and storage independently recomputes it.
- Quorum failure cannot produce a verdict.
- A clear verdict is bound atomically to the run.
- Once a run enters Stage 7 review, founder acceptance is blocked until the
  exact bound cycle is clear, quorum is met, evidence is current, and no
  blocking findings remain.

## Remaining Stage 7 packets

1. Automated blind reviewer execution using distinct live-ready providers.
2. Structured review packet construction from exact patch/evidence material.
3. Governed repair run generation from blocking findings.
4. Fresh re-verification and a new independent review cycle after repair.
5. CLI and browser control-plane surfaces.
6. End-to-end multi-provider smoke and adversarial red-team acceptance.

## Boundaries

- No reviewer may merge, deploy, mutate production, approve its own work, or
  change run authority.
- No same-provider quorum by default.
- No consensus sharing before each reviewer submits.
- No finding is closed without fresh repair evidence and re-verification.


## Packet 7A red-team hardening

- Storage independently revalidates the canonical run scope, Constitution, lease,
  implementer provider, latest execution evidence kind, evidence fingerprint, and
  evidence Constitution before creating a review cycle.
- Persisted assignments must exactly equal the cycle's declared reviewer set.
- Governance policy flags for blind review, self-review prohibition, blocking
  critical/high findings, and no founder override cannot be weakened by input.
- Finding rows must exactly match the report and each finding fingerprint is
  independently recomputed before insertion.
- Reports are withheld from the read API until the cycle is finalized, preserving
  blind independence during collection.

- Review shopping is prohibited: an execution-evidence record can be bound to only
  one independent review cycle. A repair verdict requires fresh repair execution
  evidence and re-verification before another cycle can begin.
