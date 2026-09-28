# Product UI Implementation Plan

> Execute in this session using subagent-driven-development for isolated dashboard work and review. User approved design and ongoing main workflow; preserve unrelated untracked documents.

**Goal:** Replace the crowded dispatcher prototype with the approved workflow-oriented interface.
**Architecture:** Keep the existing API and fact/planning contracts. Shared app-context builds the authenticated header; dispatcher navigation manages existing panels plus explicit settings, demo and technical sections. Risk presentation focuses on actionable visits, with model diagnostics outside the working view.
**Tech Stack:** Existing HTML/CSS, vanilla JavaScript, Python HTTP server, pytest and Node DOM regression harness.

- [x] Navigation tests first: settings accessible from header, GPS nested under crews, menu open/close and keyboard Escape, status-to-replan action changes with mode. Run `pytest tests/test_product_ui.py -q` and observe missing new controls, then implement shared header and panel navigation in app-context.js/css and scenarios.html/js/css.
- [x] Move settings, execution controls, demo stream, JSON import and model metadata into explicit pages. Preserve original element IDs/form ownership and request-version protections. Browser verify admin profile/logout and region selection; ensure engineer cannot gain admin links.
- [x] Simplify plan, event queues, status forms, notices and comparison cards. Keep assignment losses, emergency response breaches, stale plans and support communication visible. Keep AI opt-in. Test with existing strategy/intake/GPS regressions, adapting assertions only where display requirements intentionally changed.
- [x] Isolated dashboard task: risk-dashboard.js/css and risk UI tests. Show real visit entries and percentages as estimates; remove uncomputed vehicle filler and fractional-client/cost displays from primary view. Preserve full diagnostics in technical section. Test stale run protection and actions.
- [x] Align crew, account/login and engineer pages with common tokens/header without changing credential, GPS or completion semantics. Review responsive layouts.
- [x] Inspect localhost with a temporary state and deterministic optional provider. Exercise manual calculation/apply, recorded fact, autopilot, regions, risk, GPS empty state, account menu, 390px and 1440px layouts. Save screenshots.
- [x] Independent spec and code review, resolve regressions. Run `PATH="$PWD/.venv/bin:$PATH" sh scripts/check.sh`; inspect full exit status and package assets. Commit only task files and push main after passing checks.


## Validation notes
- Independent review found and verified fixes for panel initialization after relocation, mobile keyboard focus, stale correction/request context from risk actions, and retained numerical diagnostics.
- Browser: manual preview, deterministic AI explanation, inspection/apply; fact save and immediate replan on the actual South-east dataset; mode settings; region switching; admin accounts and logout; engineer role; demo selectors; 390 px and desktop layouts.
- A reused browser tab failed to load the common shell. A fresh tab and repeated region changes loaded the correct dataset. Added an explicit scoped-page guard: a missing shell cannot silently fetch the primary region. Covered by a regression test.
- Browser sessions used disposable state and accounts. No production facts changed; no GPS device permission or external LLM call was needed for presentation testing.
- Full check: 836 passed, 3 upstream SWIG warnings; wheel built and package assets verified; final focused UI checks repeated after review fixes. Browser screenshots saved under output/local/product-ui/ (not committed).

## Follow-up polish
- Applied the user's ten UI corrections: removed duplicate/technical text and lower plan disclosures; always-visible event form; uniform 44 px fields, wider plan history, centered profile chevron; grouped demo panels; immediate persisted header mode switch.
- Header mode save uses the scoped/versioned existing API, retains thresholds and unsaved threshold edits, blocks concurrent changes, and rolls back the displayed mode on failure.
- Forecast cards count visits/crews whose individual probability is at least 20%; incomplete estimates are not displayed as zero. This UI review threshold is documented as an assumption.
- Full check: 839 passed; package built and assets verified. Repeated focused checks after final copy/layout changes. Browser exercised both header modes, six arrivals with automatic application and history, reset, demo risk navigation, crew field heights, and mobile form alignment. All browser data was disposable.

## Workspace integration follow-up
- Matched header controls, circular account menu, operational request card, inset schedule selection, wide event form and grouped history filters.
- Crew editor is embedded in the common shell; direct /crew links also open the shell. Same-origin/source-checked messages carry draft/busy state and apply completion. Dirty edits block region changes and remain intact while visiting another tab. Inventory is restored to current state when opening crews.
- UI regression tests cover navigation, dirty guards, rejecting foreign messages, embedded apply notifications and explicit discard. Browser verification found and fixed a discard/unload conflict and old CSS width limits.
- Browser verified a real preview/apply cycle on disposable data, draft retention across tabs, blocked region switch, explicit discard, regional editor, 44 px plan controls, inset schedule selection, wide event fields, profile alignment and 390 px layout without horizontal overflow. Screenshots: output/local/product-ui/crew-workspace-final.png and event-form-final.png.
- Workspace-only full verification: 843 passed, wheel and runtime assets verified. Additional day-control changes below are included in the next full run.

## Day-control follow-up before push
- Add aggregate-only verified briefing builder and reuse the configured LLM client/budget.
- Add a versioned regional endpoint referring to the dashboard snapshot shown in UI.
- Render an opt-in compact card and cancel/invalidate stale responses; fix floated refresh/error layout.
- Verify malformed model output, shared call limit, privacy, regional isolation, stale results and actual timeout/retry/summary in the browser; rerun CI before push.

- Browser: actual YandexGPT briefing succeeded; deliberately delayed dashboard reproduced the timeout, confirmed non-overlapping refresh/error rectangles, and recovered on retry. Verified 390 px briefing and invalidation on refresh. Saved risk-summary-final.png and risk-timeout-final.png under output/local/product-ui/.

- Full day-control verification: 849 passed; JavaScript syntax, wheel build and package assets verified. User then requested morning 120 s / day 30 s: CLI and Render share constants, PowerShell and README match; targeted deployment/crew checks: 38 passed. Runtime budgets are per optimizer invocation, not an end-to-end response deadline.
