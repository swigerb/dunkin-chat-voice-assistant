# Hockney — History

## Project Context
- **Project:** Dunkin Voice Chat Assistant — testing for Python backend + React frontend
- **Stack:** Python unittest (backend), Vitest + React Testing Library (frontend)
- **User:** Brian Swiger
- **Existing tests:** app/backend/tests/test_order_state.py, app/backend/tests/test_extras_rules.py
- **Frontend coverage scoped to:** order-summary.tsx, status-message.tsx (in vite.config.ts)

## Issue #9 — session.updated leak regression test (2026-09-24)

Added `SessionUpdatedLeakTests` to `app/backend/tests/test_session_bootstrap.py`
(paired with Kobayashi's rtmt.py fix on `fix/session-updated-leak`). Reused
the existing `_RealtimeHarness`/`FakeGARealtime` fake-upstream harness rather
than a new one — it already echoes `instructions`/`tools` on `session.updated`
exactly like the real GA service does, so no harness changes were needed.

Gotcha: `_response_done` drains every frame until `response.done`, which
silently swallows the intervening `session.updated` — had to use
`_browser_events(browser, duration=2.0)` instead to actually observe it.

Confirmed the test fails pre-fix (real system prompt in the diff) and after
a mutation-check revert of just the `session.updated` case (fails
identically); passes with the fix in place. 395 passed total (394 baseline
+ 1), ruff clean, frontend 132/132 unchanged.

## Learnings
- Backend tests require `pip install -r requirements.txt` for azure SDK deps (azure.identity, azure.search.documents)
- `_get_bool_env` in app.py is importable independently but triggers full module import chain; keep azure deps installed
- `_is_extra_item` and `_infer_category` in tools.py are pure functions, easy to unit-test without mocks
- The `search()` tool can be tested with AsyncMock clients; `HttpResponseError` fallback path needs two-call mock
- OrderState singleton must have `.sessions = {}` in setUp to isolate tests
- Frontend vitest coverage is scoped to order-summary.tsx and status-message.tsx in vite.config.ts
- `vi` is globally available in vitest (globals: true); no explicit import needed in test files
- `userEvent` from @testing-library/user-event works for click simulation on button components

## Test Files Created/Expanded (Session 2025-07)
### Backend (app/backend/tests/)
- **test_models.py** — 12 tests: OrderItem + OrderSummary Pydantic model creation, serialization, JSON round-trips, edge cases (zero qty/price, large qty, float precision)
- **test_app.py** — 8 tests: `_get_bool_env` truthy/falsy values, whitespace stripping, default parameter behavior; `create_app` default voice is "coral", system prompt contains "Please pull around to the next window", system prompt contains `get_order` tool instruction
- **test_tools_search.py** — 8 tests: `_is_extra_item`, `_infer_category` category inference, search result formatting, empty results fallback, HTTP error handling, field-mismatch retry
- **test_order_state.py** (expanded) — 11 new tests: delete session, delete nonexistent session, concurrent sessions independence, round-trip token format, multiple advances, remove/decrement/noop, duplicate add, display formatting for all size variants
- **test_extras_rules.py** (expanded) — 7 new tests: cold brew extras, mixed donut+latte order, multiple donuts blocked, multiple beverages allowed, blocked base message content, non-extra always allowed, remove bypasses check

### Frontend (app/frontend/src/components/ui/__tests__/)
- **calculate-order-summary.test.tsx** — 4 tests: empty list, single item, multi-item with quantities, zero-price
- **loading-spinner.test.tsx** — 3 tests: default message, custom message, spinner element present
- **grounding-file.test.tsx** — 2 tests: renders file name, onClick fires

### Totals
- Backend: 59 tests (was 9) — all passing
- Frontend: 13 tests (was 4) — all passing

## Team Feedback (2026-02-25 Cleanup Sprint)
- **Keaton (Lead):** Comprehensive audit identified testing gaps (WebSocket, hooks). Hockney's expansion addresses core gaps well.
- **Fenster (Backend):** All 56 tests pass after Python modernization. Clean, maintainable baseline.
- **McManus (Frontend):** All 13 tests pass after code quality cleanup. Ready for integration with new test files.

## Voice & Prompt Update (Brian's request)
- Added 3 tests to test_app.py covering: default voice "coral", closing phrase "Please pull around to the next window", and get_order tool instruction in system prompt
- `create_app` can be tested by mocking `RTMiddleTier` class and `attach_tools_rtmt`, with `RUNNING_IN_PRODUCTION=1` to skip .env loading
- All 59 backend tests pass

## Stage 1: Test Hygiene (2026-08-06)
- **Backend baseline:** 59 tests, 3 pre-existing errors (test_app.py static dir). Fixed by creating static/index.html in setUp. Now 59 pass, 0 fail.
- **Frontend baseline:** 13 tests, 5 files — all pass. Unchanged after upgrade (React 19 + Vitest 2 + jsdom 29).
- **Flaky test search:** No datetime.now/date.today/discount/happy-hour/promo logic found in either backend or frontend tests. No time-dependent pricing in this demo.
- **No regressions from dependency upgrades.**

## Sonic parity port — `feat/sonic-parity` (2026-09-23)
- Baseline: backend 139, frontend 13. Final: backend 252 (+113), frontend 31 (+18). Ruff clean, build OK.
- Mutation (170 distinct, all against new tests):

| Item | Mutants | Result |
|---|---|---|
| 1 | 11 | 11 killed |
| 2 | 27 | 27 killed |
| 3 | 17 | 17 killed |
| 4 | 29 | 28 killed, 1 equivalent (code removed) |
| 5 | 27 | 27 killed |
| 5 follow-up (synth + tenant) | 24 | 24 killed |
| 6 | 27 | 27 killed |
| 7 | 8 | 8 killed |

- Survivors in the first rounds (items 1, 2, 3, 4, 5, 6) were fixed by tightening tests, never by weakening them. Every re-run was killed.
- A parse-error "kill" does not count: re-ran that mutant as a valid `pass` (killed).

## Round 3 (2026-09-23)
- Mutation checks, all killed: dz 2/2, R3 6/6, D2 10/10, D1 16/16, R1 backend 34/34, R1 frontend 26/26 (94 total).
- Survivors that became tests or cleanups:
  - D2: redundant regex lookbehind removed.
  - D1: redundant article group and `_is_extra_item` check removed.
  - R1 backend: final-notice reset; incomplete ≠ failed; second-delay config; end-to-end test through `_forward_messages`.
  - R1 frontend: blocked autoplay; overlapping clips; stop-conversation cleanup; locale not copied from en.
- Counts: backend 252 → 301, frontend 31 → 61.

## Order resume port (2026-09-24)
- **Mutation results:** 233 killed, 2 equivalent (both removed as redundant code), 0 surviving.
  - .env.template 4; idle backend 14; idle frontend 6; infra 12; detach 13; handshake 35+1 equivalent; rehydrate/nudge 37; dashboard 20; frontend resume 85+1 equivalent; e2e 2; nudge deflake 5.
- **Frontend survivors that became tests:**
  - stale open flag after a close;
  - early tap + resume sending nothing twice;
  - resume_rejected after an early tap not wiping the fresh order;
  - identifiers kept on a continuing tap;
  - retries exhausted after a resume → fresh start waits for the greeting.
- **Flake found and fixed:** the rate-limit nudge-skip test raced a 0.1 s real sleep under full-suite load. It is now event-gated via `_nudge_sleep`, and passed 3/3 full runs.
- **e2e** (`scripts/e2e_order_resume.py`, msedge): 50/50, run twice. Dashboard no-dedupe mutant: 3 checks fail. No-release mutant: 3 scenarios fail.
- **Counts:** backend 301 → 394, frontend 65 → 132.
