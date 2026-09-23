# Decisions

Team decisions log. Append-only — newest entries at the bottom.

---

## 2026-02-25: Codebase Cleanup Sprint

### Keaton: Full Codebase Audit (Lead)
**Timestamp:** 2026-02-25T14:32:52Z

Completed comprehensive codebase evaluation identifying cleanup opportunities across backend and frontend.

**P0 Findings (Safe, High-Value):**
1. Remove dead frontend components (grounding-file*.tsx, history-panel.tsx, ImageDialog.tsx)
2. Remove commented-out code in App.tsx (lines 15, 402–403)
3. Fix requirements.txt BOM encoding
4. Remove unused react-draggable from package.json
5. Fix type() == str → isinstance() in rtmt.py:31
6. Replace print() with logger in rtmt.py:257,271
7. Fix quad-quote `""""` in tools.py:89

**P1 Findings (Moderate Value):**
1. Extract BrandHero, SessionTokenBanner, SVG art from App.tsx
2. Convert ToolResult, Tool, RTToolCall to @dataclass
3. Modernize from typing import List → list[]
4. Fix useAudioRecorder buffer to use useRef (bug risk)
5. Audit and remove unused react-icons and axios
6. Replace f-strings in logger.*() calls with lazy %s formatting
7. Add .dockerignore to app/

**P2 Findings (Backlog):**
1. Archive azurespeech.py, azure_speech_gpt4o_mini.py
2. Upgrade Vitest to 2.x
3. Split requirements.txt into app vs dev
4. Expand test coverage (rtmt.py, useRealtime, context providers)
5. Evaluate darkreader vs native Tailwind dark mode
6. Add [project] metadata to pyproject.toml
7. Group vendor chunks in vite.config.ts

**DO NOT CHANGE (Preserve):**
- rtmt.py WebSocket proxy architecture
- tools.py search fallback logic
- order_state.py singleton + session model
- app.py credential resolution
- Frontend audio pipeline (useAudioPlayer/useAudioRecorder)
- Greeting-then-mic flow with 5-second safety timeout
- order_state.py round-trip token idempotency design

---

### Fenster: Backend Python Modernization
**Timestamp:** 2026-02-25T14:32:52Z

Modernized all backend Python files to Python 3.11+ idioms. All ruff violations resolved.

**Key Changes:**
- Type annotations: Optional[X] → X | None, List[X] → list[X], Callable from collections.abc
- super() call simplified in OrderState.__new__
- isinstance check: type(x) == str → isinstance(x, str)
- Import hygiene: Fixed sorting (I001), removed unused (F401), removed duplicates (F811)
- Logging: Replaced print() with logger.warning()/logger.info()
- Added __all__ exports to public API modules
- Removed redundant block comments in tools.py
- Normalized get_order signature to (args, session_id)

**Impact:**
- Ruff errors: 29 → 0
- Test suite: All 56 tests pass
- No behavioral changes
- Files: models.py, order_state.py, rtmt.py, tools.py, app.py, azurespeech.py, azure_speech_gpt4o_mini.py

---

### McManus: Frontend Code Quality Cleanup
**Timestamp:** 2026-02-25T14:32:52Z

React/TypeScript code quality pass — internal hygiene, no visual/behavioral changes.

**Key Decisions:**
1. Removed duplicate ThemeProvider in index.tsx
2. Eliminated all any types in useAzureSpeech.tsx with proper types from types.ts
3. Converted static data loading to module-level const in menu-panel.tsx
4. Fixed ref bug in grounding-files.tsx (isAnimating.current)
5. Modernized React patterns (React.FC → direct functions)
6. Replaced unused useState with useMemo for computed values
7. Removed dead code and comments

**Impact:**
- All 13 tests pass
- TypeScript strict clean
- Build succeeds (tsc + vite)
- Files modified: 8 (App.tsx, hooks, components, context, tests)

---

### Hockney: Test Expansion
**Timestamp:** 2026-02-25T14:32:52Z

Expanded test coverage across backend and frontend.

**Backend Tests (app/backend/tests/):**
- test_models.py: 12 tests (Pydantic model serialization, JSON round-trips, edge cases)
- test_app.py: 5 tests (_get_bool_env truthy/falsy/whitespace)
- test_tools_search.py: 8 tests (pure functions, mocked Azure Search, error handling)
- test_order_state.py (expanded): 11 new tests (sessions, tokens, formatting)
- test_extras_rules.py (expanded): 7 new tests (mixed orders, rules, edge cases)

**Frontend Tests (app/frontend/src/components/ui/__tests__/):**
- calculate-order-summary.test.tsx: 4 tests
- loading-spinner.test.tsx: 3 tests
- grounding-file.test.tsx: 2 tests

**Impact:**
- Backend: 9 → 56 tests
- Frontend: 4 → 13 tests
- 6 new test files
- All passing, no production code changes

---

### Keaton: Sonic parity port (feat/sonic-parity)
**Timestamp:** 2026-09-23

- The realtime default is **gpt-realtime-2.1** with `reasoning.effort=low`; gpt-realtime-1.5 is the rollback. Spot-check: low 12/12 vs none 10/12. At none, the model folded an extra into the drink name, the extras guard rejected it, and the model still claimed it was added.
- Default voice is **marin**. Voice changes apply from the next conversation (GA voice lock).
- The server bootstraps every upstream session. A rejected session.update gets exactly one minimal fallback.
- WebSocket compression is off (`connection.ws_compression: false`) until aiohttp fixes the PONG + compressed-frame 1002 bug.
- On connection loss the conversation ends, the guest sees a notice, and there is no silent mic restart.
- `webAppExists` reads `SERVICE_BACKEND_RESOURCE_EXISTS`.
- Smoke check tokens use the azd env subscription/tenant, never the global `az` default.
- Shared OpenAI with Sonic (`cog-axgpampkq3yfa`): reuse only, with idempotent role assignments. 2.1 capacity is shared.
