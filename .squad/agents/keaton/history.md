# Keaton — History

## Project Context
- **Project:** Dunkin Voice Chat Assistant — voice-driven ordering using Azure OpenAI GPT-4o Realtime + Azure AI Search RAG
- **Stack:** Python 3.11+ (aiohttp) backend, React 18 + TypeScript + Vite frontend
- **User:** Brian Swiger
- **Key files:** app/backend/app.py, app/backend/rtmt.py, app/backend/tools.py, app/frontend/src/App.tsx

## Learnings
- **Architecture:** `rtmt.py` is the core WebSocket proxy (middle-tier pattern) — intercepts tool calls server-side, forwards audio to/from Azure OpenAI Realtime. Do not restructure.
- **Dead code:** `azurespeech.py` and `azure_speech_gpt4o_mini.py` are unused prototypes; `grounding-file*.tsx`, `history-panel.tsx`, `ImageDialog.tsx` have no active imports.
- **Frontend structure:** `App.tsx` is oversized (~585 lines) with inline sub-components (`BrandHero`, SVG art, `SessionTokenBanner`). Needs decomposition.
- **Bug risk:** `useAudioRecorder.tsx` line 13 uses a module-scoped `let buffer` instead of `useRef` — won't reset on remount.
- **Testing:** Only 4 test files total (2 backend: order_state, extras_rules; 2 frontend: order-summary, status-message). No WebSocket or hook tests.
- **Dependency hygiene:** `react-draggable` is a dead dep. `requirements.txt` has BOM encoding issues and includes deps only needed by setup scripts.
- **API contract:** `finalTotal` camelCase in `models.py` is shared with frontend `types.ts` — must change in both if renamed.
- **User preference (Brian):** Wants modern, clean codebase; safety-first approach — "do not break anything."
- **Build commands:** Backend: `ruff check app/backend`, `python -m unittest discover -s tests`. Frontend: `npm run test`, `npm run build`.
- **Key config files:** `pyproject.toml` (ruff only), `app/Dockerfile` (multi-stage Node→Python), `vite.config.ts` (proxy, manual chunks).

## Team Feedback (2026-02-25 Cleanup Sprint)
- **Fenster (Backend):** Successfully modernized all backend files to Python 3.11+. Ruff errors reduced 29→0. All 56 tests pass.
- **McManus (Frontend):** Completed code quality pass on 8 files. Eliminated all `any` types. Fixed critical ref bug in grounding-files.tsx. All 13 tests pass.
- **Hockney (Tester):** Expanded test coverage dramatically: backend 9→56 tests, frontend 4→13 tests. 6 new test files created. All passing.

## Stage 1: Dependencies + Tooling Modernization (2026-08-06)
- **squad upgrade:** v0.11.0 — squad doctor 0 failures
- **Python deps:** Fixed 44 CVEs → 0. aiohttp 3.10.11→3.14.3, azure-search-documents 11.6→12.0, python-dotenv 1.0.1→1.2.2, cryptography pinned >=50.0.0,<51, azure-identity 1.19→1.25.3, cffi 1.17.1→2.1.1, openai 1.54.3→1.109.1, rich 13.9.4→15.0.0
- **azure-search-documents 12.0 migration:** AzureOpenAIParameters→AzureOpenAIVectorizerParameters, resource_uri→resource_url, deployment_id→deployment_name in setup_intvect.py
- **Node runtime:** 18/20→22 (Dockerfile, CI, devcontainer)
- **Missing roles gap:** No DevOps agent (infra/CI/CD coverage), no AI/Realtime specialist (rtmt.py, prompt tuning, WebSocket patterns). Sonic has Squanchy + Unity; McDonald's has Mayor McCheese + Mac Tonight. Recommend adding parity roles.
- **Did NOT touch:** rtmt.py API surface, infra/ bicep, azure.yaml (Stage 2 scope)

## Stage 2: Feature Parity — Voice Picker, Happy Hour, Quantity Limits (2026-08-06)

### Voice Picker (Task 1)
- Added `sendVoiceChoice()` to `useRealtime.tsx` → sends `{ type: "extension.set_voice", voice }`.
- `App.tsx` owns `voiceChoice` state, persists to `localStorage("voiceChoice")`, syncs on startup.
- `settings.tsx` renders a `<select>` with all 10 GA voices (alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar) + descriptors.
- `rtmt.py` intercepts `extension.set_voice` in the forwarding loop. Pre-session: stores voice for next `session.update`. Mid-session: sends a GA-shaped `session.update` with `audio.output.voice` via `_to_ga_session()`.
- **GA routing:** `_to_ga_session()` already maps legacy `voice` → `session.audio.output.voice`. The `extension.set_voice` handler leverages this same path.
- Default: `coral` (from env `AZURE_OPENAI_REALTIME_VOICE_CHOICE`, fallback in app.py).
- Piper/local-mode voice selector intentionally omitted per Brian's instruction.

### Happy Hour (Task 2)
- **Window:** 2 PM – 5 PM store-local time (Eastern default). Dunkin's afternoon is the natural iced-drink/espresso lull period.
- **Eligible categories:** "cold beverages" and "signature lattes" — i.e., cold brews, refreshers, iced drinks, and espresso-based lattes. NOT donuts, NOT breakfast sandwiches.
- **Discount:** 25% off (multiplier 0.75). Sonic uses 50% on slushes/drinks (aggressive fast-food play); Dunkin's margins are tighter on specialty coffee, so 25% is more realistic.
- **Reasoning:** Dunkin's afternoon traffic competes with Starbucks cold-drink sales; discounting iced/espresso items in the 2-5 PM window mirrors real Dunkin promotions ("Afternoon Pick-Me-Up").
- Config in `config.yaml` → `business_rules.happy_hour_*`. 8% flat tax preserved and applied AFTER discount.
- All pricing tests patch `is_happy_hour` where it's used (`order_state.is_happy_hour`) — no wall-clock dependency.

### Quantity Limits (Task 3)
- `MAX_QUANTITY_PER_ITEM = 10`, `MAX_TOTAL_ITEMS = 25` — configured in `config.yaml`.
- Per-item: rejects if `existing_qty + requested > 10`. Partial-add offer if room remains.
- Total order: rejects if `sum(all_items) + requested > 25`.
- Rejection returns `ToolResultDirection.TO_SERVER` with a conversational message ending in `?` so the AI relays naturally.
- Remove actions bypass all limits (always allowed).

### Files Changed
- **New:** `config.yaml`, `config_loader.py`, `tests/test_happy_hour.py`, `tests/test_quantity_limits.py`, `tests/test_voice_change.py`
- **Modified:** `order_state.py`, `rtmt.py`, `tools.py`, `requirements.txt`, `tests/test_order_state.py`, `App.tsx`, `settings.tsx`, `useRealtime.tsx`

### Validation
- `python -m pytest app/backend -q`: **103 passed** (was 81)
- `ruff check .`: All checks passed
- `npm run build`: ✓ built in 2s
- `npm test`: 13 passed (5 files)
- Mutation checks: 4 mutations across happy-hour and quantity-limit code — all detected by tests

## 2026-08-07 — Sprint 3: CRM Dashboard Port

**Branch:** `sprint/crm-dashboard` (from `dev`)

### Files Ported from pr-2
- `app/backend/crm/__init__.py`, `models.py`, `repository.py`
- `app/backend/drive_thru/__init__.py`, `models.py`, `store.py`, `simulator.py`, `demo.py`
- `app/backend/dashboard.py`
- `app/backend/data/crm_seed.json`
- `app/backend/tests/test_crm.py` (expanded with 17 tests)
- `app/employee-dashboard/` (full React app — 7 source files)
- `scripts/seed_crm.py`

### Deliberately Excluded
- `app/backend/chroma_data/chroma.sqlite3` — gitignored, Sprint 4 scope
- All `.js` / `.d.ts` build artifacts under `src/` — gitignored
- `tailwind.config.js`, `tsconfig.node.json` — replaced by Tailwind 4 CSS-first config

### Rework Applied
1. **Deadlock fix (e3db8a8)** already in the code; also fixed `_spawn_placeholder_cars` which had the same re-entrant lock bug in `reset()`.
2. **Tailwind 3→4**: `@tailwindcss/postcss`, `@theme` block in CSS, removed `autoprefixer`, `tailwind.config.js` deleted, `flex-grow`→`grow`.
3. **Stack upgrade**: React 19.2, Vite 6.4, TS 5.8, Tailwind 4.3, lucide-react 1.28.
4. **Routes wired additively** in `app.py` after `rtmt.attach_to_app` — no existing code disturbed.
5. **Idempotent seeding**: table-exists guard + INSERT OR REPLACE. Mutation-checked.

### Dashboard Dependency Upgrade Table
| Package | pr-2 (old) | Ported (new) |
|---------|-----------|-------------|
| react | 18.3 | 19.2 |
| react-dom | 18.3 | 19.2 |
| vite | 5.4 | 6.4 |
| tailwindcss | 3.4 | 4.3 |
| typescript | 5.5 | 5.8 |
| lucide-react | 0.445 | 1.28 |
| @vitejs/plugin-react | 4.3 | 5.2 |
| autoprefixer | 10.4 | removed |
| @tailwindcss/postcss | — | 4.3 (new) |

### Test Results
- **Before**: 110 passed
- **After**: 127 passed (+17 new: CRM repo lookup, favorites, suggestions, idempotent seeding, dashboard spawn/complete/reset/demo routes)
- `ruff check .` → All checks passed
- Main frontend: `npm run build` ✓, `npm test` → 13 passed
- Employee dashboard: `npm run build` ✓
- Lockfiles: 0 internal Microsoft host references

### Mutation Check (Seeding Idempotency)
- **Mutant**: Disabled table-exists guard + changed INSERT OR REPLACE → INSERT
- **Result**: `sqlite3.IntegrityError: UNIQUE constraint failed: customers.id` → test FAILED as expected
- **Restored**: test PASSED

### Not Verifiable Until Deployed
- WebSocket `/dashboard` real-time event flow (requires running server)
- CRM Bluetooth MAC lookup with real hardware
- Demo fleet auto-spawning under load

## 2026-08-07 — Sprint 5: Documentation Port & Rework

**Branch:** `sprint/docs` (from `dev`)

### Documents Produced
1. **`docs/Demo/8-minute-demo-script.md`** — Full narrated 8-minute live demo script for technical audiences.
2. **`docs/demo-guide.md`** — Consolidated presenter guide (merged demo-guide + demo-cheat-sheet from contributor branches).
3. **`docs/foundry-local-architecture.md`** — Opt-in Foundry Local pipeline topology, clearly scoped to `USE_LOCAL_PIPELINE=true`.

### Consolidation Decision
- The contributor's `demo-guide.md` and `demo-cheat-sheet.md` overlapped ~80% (same talking points, same Q&A, same cluster commands). Consolidated into one `demo-guide.md` with a "Quick Reference" section providing 30-sec / 2-min / 5-min versions. Three overlapping docs would confuse presenters.

### Stale Statements Corrected
| Statement | Was (April contributor branch) | Now (August dev) |
|---|---|---|
| Realtime model | `gpt-4o-realtime-preview` | `gpt-realtime-1.5` (GA, version 2026-02-23) |
| API surface | `/openai/realtime?api-version=2024-10-01-preview` | `/openai/v1/realtime?model=` (GA surface) |
| Default menu search | "ChromaDB handles menu retrieval" | Azure AI Search is default; ChromaDB only when `USE_LOCAL_PIPELINE=true` |
| Edge image | Not mentioned | `Dockerfile.edge` with `requirements-edge.txt` |
| Domain | `dunkin.adaptivecloudlab.com` | `<your-domain>` (placeholder convention from azure-local-deployment.md) |
| ACR | `cadunkinacr` | `<your-acr>` |
| Fork URL | `mgodfre3/dunkin-chat-voice-assistant` | `<your-org>/<your-repo>` |
| Site name | `california` | `<your-site>` / `example-site` |

### New Content Added (Sprint 3 & 4 features)
- Voice picker (10 GA voices, live swap, settings dialog, `session.update`)
- Crew dashboard — what it shows, how to launch (`app/employee-dashboard/`), Demo Controls
- Happy hour (2–5 PM, 25% off cold beverages + signature lattes)
- Quantity limits (10 per item, 25 per order, conversational refusal)
- Extras validation (whipped cream / swirls / shots only on eligible categories)
- Authentication position (public by default, Entra ID opt-in)

### Also Fixed
- Stale `gpt-4o-realtime-preview` in `docs/azure-local-deployment.md`, `docs/existing_services.md`, `docs/manual_setup.md` → `gpt-realtime-1.5`
- README TOC updated with "Demo & Presentation Guides" section

### Validation Results
- `python -m pytest app/backend -q` → **139 passed** ✓
- `ruff check .` → All checks passed ✓
- `npm run build` (frontend) → Built in 5.46s ✓
- `npm test` (frontend) → 13 passed ✓
- Internal links: all 18 referenced paths verified present ✓
- Grep `mgodfre3` / `cadunkinacr` / `adaptivecloudlab` → only in azure-local-deployment.md "Example" column ✓
- Grep `gpt-4o-realtime` / `2024-10-01` / `openai/realtime?` → zero matches ✓
- `git status --short` → only intended doc files ✓

### Items Flagging for Human Review
- `flux/apps/dunkin-voice/configmap.yaml` still has `AZURE_OPENAI_REALTIME_DEPLOYMENT: "gpt-4o-realtime-preview"` — this is live infra config, not docs, so I did not modify it per the "documentation only" rule. It should be updated when the deployment model is rotated.
- The `.env-sample` shows `gpt-realtime-mini` which may need updating to `gpt-realtime-1.5` — again, application config, not docs scope.

## Sonic parity port — `feat/sonic-parity` (2026-09-23)
- Reviewed and sequenced the 7-item Sonic port. One commit per item: 5ea449c, 0513622, 98be241, 5a73acf, 7b82260, 6466071, 14a68ca. Follow-up c4249de (smoke check fixes the live probe found).
- **Item 1 reconciliation with the Sprint-1 greeting fix (Godfrey / `ba8c94d` lineage):**
  - Kept per-connection `tools_pending`.
  - Replaced the `session_configured` bool, which only gated mid-session voice updates. The greeting now waits for the relayed browser update plus `session.updated`, and every upstream socket is bootstrapped first.
  - `ba8c94d` is **not** an ancestor of `dev`. Its request-id fix read `ws.headers`, which are the response headers; now fixed to read the request headers.
- **Shared Azure OpenAI (`cog-axgpampkq3yfa`, rg-sonic-demo):**
  - `AZURE_OPENAI_REUSE_EXISTING=true`, so `module openAi` is skipped. Provision only adds deterministic-guid role assignments there, which are idempotent. No Sonic deployment is redeclared.
  - gpt-realtime-2.1 cap 10 is shared with Sonic; contention is the remaining risk.
- Scope notes:
  - Sonic's idle-4000, token-wait and `response.cancel` parts of item 6 are N/A (Dunkin has none of those features).
  - `/dashboard` socket untouched (server-push only).
  - es/fr/ja `status.notRecordingMessage` still has stale Contoso copy (pre-existing, out of scope).

## Round 3 review (2026-09-23)
- Commits on feat/round3: 9a2d398 (dz), d41f5bf (R3), ecb4ebf (D2), e4c4b1a (D1), be235fc (R1 backend), 6f7f0c0 (R1 frontend + clips). Not pushed, merged or deployed.
- Only a deploy can confirm:
  - real production rate-limit behaviour and clip playback / mic mute in a browser;
  - an operator's edge cluster applying the placeholder config with their own AOAI account.

## Order resume review (2026-09-24)
- **Commits on feat/order-resume:** daf6683 (.env.template), 8376080 (idle close), f4283af (step 0 infra), 8554085 (step 1 detach), 49043b1 (step 2 handshake), 4b6b02a (step 3 rehydrate/nudge), c94771d (dashboard), 9a70e5c (frontend), f96cc60 (e2e), 3a5d843 (nudge test deflake). Not pushed, merged or deployed.
- **Only a deploy can confirm:**
  - sticky affinity plus the single worker in ACA;
  - the secret surviving `azd provision`;
  - the real model obeying the rehydration item (no re-greet) and the nudge wording;
  - real-network drop timing against the 120 s hold.
