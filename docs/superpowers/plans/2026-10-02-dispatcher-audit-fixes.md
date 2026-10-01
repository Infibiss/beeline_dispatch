# Dispatcher audit fixes implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent presentation task and review it before implementing the operational task. Preserve the user's requested two-commit order.

**Goal:** Fix the five audited defects and remove repetitive technical prose, then publish and verify the resulting site.

**Architecture:** Presentation changes retain immutable, comparable data in saved plan versions. The final operational change uses constrained gap insertion for ordinary intake batches instead of unconstrained replanning. Risk estimates remain advisory and labelled as forecasts; no empirical calibration is claimed.

**Tech Stack:** Python/Pydantic, existing solvers and validators, vanilla JavaScript, pytest, native browser tools.

## Approved scope and design

The user authorized fixing the five findings from the preceding audit, removing dev text, and handling the first finding last in a separate commit. Do not add calendar/day creation or change risk distribution parameters. Work only in this repository; only GitHub origin is authorized. Use a temporary codex branch in the existing clean checkout to stay within the user's repository boundary. Preserve operational API compatibility and historical snapshots.

### Task 1 — Presentation and comparable saved results (first commit)

Files: `src/field_router/replanning.py`, a focused comparison module, `dispatcher.py`, `report.py`, `scenario_server.py`, `crew.py` where necessary for comparable morning inputs; `static/scenarios.js`, `static/engineer.js`, `engineer.py`, relevant CSS; tests of saved comparison/notification semantics and client presentation helpers; assumptions register.

- [x] Write and run failing behavioral tests for comparison using the same scenario and resource constraints, frozen-before/after assignment counts, immutable application time, and readable new request identifiers. No source-string tests for cosmetic edits.
- [x] Add a compact saved comparison containing baseline/current metrics and per-engineer distance/assignment counts. Compute baseline with the existing first-fit solver and validate it. Preserve actual morning inputs before issue changes unlimited stock into issued stock; do the same for a crew preview. Operational comparisons use the same remaining requests, shifts, starts and inventory as the selected plan. Do not store another all-pairs geography database. Expose a visible comparison from Plan day and from its report; historical versions show their own saved comparison.
- [x] Persist application scenario time and same-scope assignment comparison in approval notifications. Compare the same residual request IDs before and after, excluding frozen work and newly created requests from the before count. Keep actual new/lost/cancelled counts separate. Keep legacy counters for API compatibility; omit ambiguous before/after presentation for older notifications without comparable counters. Resolve older application time from its history event, never current state time.
- [x] Include created canonical request IDs in notifications and use canonical IDs/labels for new request presentation. Label manual new requests as new, not recovered. Add display labels in engineer tasks while retaining canonical IDs for commands.
- [x] Remove recurring dev provenance/calibration text from cards and notifications; keep factual trade-offs and availability errors. Put a short explanation of aggregate forecasts in one help disclosure. Clarify that aggregate risk concerns at least one breach. Replace "a few seconds" with stage-based text that allows a few minutes, without inventing an ETA.
- [x] Run focused tests, browser checks, then `PATH="$PWD/.venv/bin:$PATH" sh scripts/check.sh`. Review all changes before the first commit. Do not change ordinary JSON planning yet.

Example notification invariant test:

```python
before_ids = {v.request_id for r in session.plan.routes for v in r.visits}
batch = session.preview(payload, time_limit=.03)
frozen = {v['request_id'] for r in batch['frozen'] for v in r['visits']}
remaining = {r.id for r in session.batches[batch['id']]['scenario'].requests}
session.approve({'version': session.version, 'batch_id': batch['id'],
                 'candidate_id': batch['recommended_candidate_id']})
notification = session.dispatch_notifications[-1]
assert notification['assignment_comparison']['before_assigned'] == len(before_ids & remaining - frozen)
assert session.state()['last_applied']['time'] == payload['event']['time']
```

Use the actual batch's stored scenario when the response does not expose it; do not add production internals just for tests.

Verification: full check 1109 passed, 1 skipped; JS syntax and wheel resources passed. Final frozen-release/absence changes: 97 focused tests passed. Native browser verified normal new labels, residual counters, immutable 11:50 approval after 12:44 completion, and saved historical comparison. Spec review passed; quality review found no important production issues.

### Task 2 — Ordinary JSON intake preservation (last, separate commit)

Files: `ordinary_insertion.py`, `replanning.py`, intake tests and insertion tests; operational contract documentation.

- [x] Write failing tests for manual and automatic ordinary batches, several arrivals, impossible arrivals, and batches mixed with a new emergency. Check every candidate, not only the recommendation. Include changed facts/absence and restore/atomic approval boundaries.
- [x] Classify pending ordinary arrivals in the existing preview flow. With only ordinary arrivals and no operational changes requiring an independent replan, enumerate feasible insertions in priority then deterministic input order; preserve all previously assigned engineers, relative order and start minutes, and retain unassigned reasons. Carry the newly assigned arrivals into the reference for subsequent insertion. Bound retained candidates rather than creating an unbounded Cartesian product; document heuristic coverage, never claim global optimality.
- [x] If actual facts make the old promises infeasible, require an explicit ordinary replan instead of silently moving promises just to accept the batch. New emergencies and explicit cancellation/unavailability remain eligible for full remaining-day replanning; started work remains protected. Do not weaken current constraints or approval version checks.
- [x] Run focused tests and the real East reproduction from the audit at useful budgets, validate all results, then rerun the full check. Review and commit this fix separately after Task 1.

Essential preservation assertion:

```python
old = {v.request_id: (r.engineer_id, v.start)
       for r in session.plan.routes for v in r.visits}
session.receive_requests({'requests': ordinary_rows})
batch = session.preview({'version': session.version,
    'event': {'id': 'ordinary-batch', 'type': 'replan', 'time': session.time}}, time_limit=.03)
for candidate in batch['candidates']:
    after = {v['request_id']: (r['engineer_id'], v['start'])
             for r in candidate['plan']['routes'] for v in r['visits']}
    assert {key: after[key] for key in old} == old
```

Verification: full check 1127 passed, 1 skipped; JS and wheel resources passed. Native browser: manual JSON preview followed by persisted autopilot approval changed 66 to 67 assigned with zero old owner/start/order changes. Repeat-preview road aliases are reused only for the matching queued arrival; registered point collisions remain rejected. Spec and quality reviews passed, including the GPS and emergency-response regressions. Final prose cleanup: 30 focused LLM tests passed.

### Task 3 — Integration and deployment verification

- [x] Spec review, then quality review; fix findings and rerun the affected checks.
- [ ] Confirm exactly ordered commits, clean status, no secrets/state in commits, and latest origin/main before integration.
- [ ] Fast-forward main after verification and push only the approved GitHub origin. Verify Actions, deployment and visible public behavior; a published image alone is not proof of a working site.
- [ ] Report both commits, what passed locally/publicly, what the risk percentages mean, and remaining material limitations (uncalibrated forecasts, new-day workflow, shared ephemeral demo and deployment credentials expiry).
