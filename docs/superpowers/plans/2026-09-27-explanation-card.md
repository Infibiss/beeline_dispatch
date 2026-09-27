# Dispatcher explanation card implementation plan

**Goal:** Deliver the approved short explanation for recommendations and applied plans, including autopilot, without a second details disclosure.

**Architecture:** A Python presenter builds a structured card from verified candidate facts. The existing LLM selects emphasis; it cannot invent text, numbers or change the recommendation. A shared DOM renderer displays headings, lists and an attention block using textContent. Legacy explanations remain readable. User decision: no recommendation or badge before the explicit AI request; default sort is neutral strategy order. Backend ranking and autopilot selection remain unchanged.

- [x] Add failing behavior tests for equal plans, loss-first recommendations, mandatory warnings, and LLM fallback cards.
- [x] Create `explanation_card.py`: summary, up to two reasons, tradeoffs, mandatory attention. Compare named alternatives; do not claim causal explanations or promise notifications.
- [x] Attach cards to preview candidates and LLM responses; persist the chosen card in application notifications before asynchronous enrichment.
- [x] Render the same card in recommendation and last-application panels; title depends on recommended/applied state. No raw HTML, markdown parser, additional disclosure or dependency.
- [x] Run Python presenter/LLM/intake tests and existing Node DOM tests. Inspect desktop and narrow layouts in a local browser. Document actual verification before commit.

**Accepted UX:** readable in about 20–30 seconds; all material unassigned/lost work and emergency delays remain visible. Existing “Посмотреть изменения” provides per-request details. Model risk remains explicitly uncalibrated. No optimizer changes are implied by this presentation change.

**Review and verification:**
- Independent code review found missing preservation/overtime comparisons; both reproduced with failing tests and fixed.
- Added opt-in, neutral sorting, failed-request, safe text rendering, applied-card, equal-metric, preservation, overtime and model-emphasis checks.
- HTTP integration verifies the card is available before slow LLM enrichment and survives provider failure. Replaced a pre-existing timing race with an explicit second-provider-start event.
- Local browser: preview without badges → explicit AI request → structured card → inspect changes → apply a different plan → correct applied-card title and facts. Tested 1280px and 390px viewport widths; no extra disclosure. Browser provider was a deterministic local double; no live YandexGPT call was needed.
- Final `scripts/check.sh`: 819 tests passed (3 existing SWIG deprecation warnings), JavaScript syntax checks passed, wheel built and package assets verified.
