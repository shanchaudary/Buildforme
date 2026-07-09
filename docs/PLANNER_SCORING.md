# Planner scoring model (Stage 4)

Deterministic. Same inputs → same ranking. Hard rules override scores.

## Score components

| Component | Range | Notes |
| --- | --- | --- |
| Blocker impact | 0–30 | Critical unsafe/blocker +30, high +20, mild +10 |
| Stage alignment | −20–20 | Current stage +20, next +10, later −20 |
| Dependency readiness | −40–15 | All complete +15, none 0, incomplete −20/−40 |
| Risk suitability | −100–15 | GREEN +15, YELLOW +10, RED −20, BLACK −100 |
| CI urgency | 0–25 | Failing +25, pending/unknown PR +15, passing PR +10 |
| Effort efficiency | 0–10 | small +10, medium +5, large/unknown 0 |
| Age | 0–5 | Small boost for stale ready work |
| Human attention cost | −15–0 | RED/substantial −15, light approval −5 |

`total_score` is the sum of components.

## Hard overrides

1. BLACK → `reject_task`, never execute, no packet for execution.
2. Incomplete hard dependencies → `resolve_blocker`, not executable.
3. Later stage while earlier mandatory stages incomplete → `no_action` / do not start.
4. Failing CI PR → high CI urgency (prefer before unrelated implementation).
5. Unsafe truth → high blocker impact / resolve path.
6. RED → `request_shan_decision` / Needs Shan (not unattended).
7. No merge authority implied.
8. No production authority implied.
9. Sparse data → low confidence; prefer audit/verify language.
10. Complete/rejected tasks excluded from candidates.
11. Local approvals ≠ GitHub approvals.
12. Explanation required on every recommendation.

## Confidence

- **high** — stages+tasks exist, GitHub available, verified truth present  
- **medium** — roadmap present, some signals missing  
- **low** — sparse roadmap / unavailable GitHub / unverified only  

## References

Implementation: `buildforme/planner.py`  
UI: Chief planner page  
CLI: `python -m buildforme.cli plan <project_id>`
