# Kobayashi — History

## Context

- **Project:** Dunkin Voice Chat Assistant — AI-powered drive-thru ordering experience

## Sprint 4 — Hybrid Local Inference (2026-08-07)

**Branch:** `sprint/local-inference` (from `dev`)

### What was done

Implemented the hybrid local inference path for Azure Local edge deployments, fully gated behind `USE_LOCAL_PIPELINE` (env var, default **false**).

**Feature flag design:**
- `_get_bool_env("USE_LOCAL_PIPELINE", False)` in `app.py` — cloud path is the unchanged default
- When **off**: `RTMiddleTier` + Azure AI Search — byte-for-byte identical to dev
- When **on**: `RTLocalPipeline` (Whisper STT → Phi-4 Mini → Piper TTS) + ChromaDB vector store

**Lazy imports:** `chromadb`, `onnxruntime`, `rtmt_local` are imported **inside** the `if use_local:` branch only. A cloud deployment with those packages absent entirely imports and runs cleanly.

**Search implementations coexist in `tools.py`:**
- `search()` — existing Azure AI Search with semantic ranker, field-name fallbacks, HttpResponseError handling (untouched)
- `search_chromadb()` — new ChromaDB vector search for edge
- `attach_tools_rtmt()` selects which to bind based on `use_local_pipeline` kwarg

**Dependencies:**
- `requirements-edge.txt` (chromadb, onnxruntime, tokenizers) — separate from main `requirements.txt`
- Main `requirements.txt` unchanged — Container Apps image does not grow

**Dockerfile:**
- `app/Dockerfile` — untouched (cloud image)
- `app/Dockerfile.edge` — new, installs both requirement files, sets `USE_LOCAL_PIPELINE=true`
- Rationale: separate Dockerfile avoids build-arg complexity and keeps cloud image pristine

**Ported from pr-2:**
- `rtmt_local.py` — local pipeline implementation
- `scripts/ingest_menu_local.py` — ONNX-based ChromaDB index builder (commit 36b64bb approach)

### Validation results

| Check | Result |
|-------|--------|
| `pytest app/backend -q` | 132 passed, 7 pre-existing failures (async infra) |
| Cloud-only venv (no chromadb/onnxruntime) | 132 passed — identical |
| `ruff check .` | All checks passed |
| `npm run build` (frontend) | ✓ |
| `npm test` (frontend) | 13 passed |
| Employee dashboard build | ✓ |
| Mutation check (default→True) | 3 `create_app` tests FAIL + source inspection test FAIL |
| No banned files in diff | Confirmed (no chroma_data, .onnx, .bin, .gguf) |

### What only an edge cluster can prove

- End-to-end audio pipeline (Whisper → Phi-4 Mini → Piper) with real microphone input
- ChromaDB query latency with the full 80+ item menu index on AKS Edge
- Piper TTS voice quality and streaming chunk timing at 24 kHz
- K8s service mesh connectivity between sidecar containers

## Sonic parity port — `feat/sonic-parity` (2026-09-23)
- **Item 1:**
  - Server bootstrap `session.update` is the first upstream frame.
  - Greeting only after the browser update and `session.updated` (5 s timeout).
  - Voice lock after assistant audio (defer `set_voice`; omit `audio.output.voice`).
  - Request-id read from the request headers.
- **Item 2:**
  - gpt-realtime-2.1, `reasoning.effort=low`, only for reasoning deployments; `configure_realtime_model()`.
  - Transcription is server-owned (whisper-1).
  - Edge configmaps set to 2.1; `USE_LOCAL_PIPELINE` gating unchanged.
- **Item 4:**
  - `_SessionUpdateGuard` correlates `error` events to tracked session.updates (by event_id, or the oldest unacked update).
  - Exactly one minimal fallback; `_reasoning_rejected` is process-wide.
- **Item 5:**
  - `scripts/smoke_realtime.py` + postdeploy hook (never fails a deploy).
  - Live finding: Sonic's synthesiser (user turn + "say this") made 2.1 *answer* the phrase instead of reading it (0/3 verbatim). Phrase-in-`response.instructions` gave 6/6 verbatim.
  - Tokens now come from the azd env's subscription/tenant. Another sign-in on this machine had moved the global `az` default, which caused HTTP 400 tenant mismatches.
- **Live, 2026-09-23:**
  - Smoke check PASS on 2.1 (reasoning low) and on the 1.5 rollback (reasoning off); verbatim transcript on both.
- **Brand spot-check** (Dunkin prompt + real tools/search; 6 scenarios × 2 reps; text turns; guest confirms, because the prompt adds only "after the guest has agreed"):
  - 2.1 effort low: 12/12 correct, first-audio median 0.88 s.
  - 2.1 effort none: 10/12, median 0.87 s.
  - 1.5: 12/12, median 1.63 s.
  - 0 off-brand mentions.
  - The 2 effort-none misses: the model sent "Caramel Craze Latte with Extra Espresso Shot" as ONE item, the extras guard rejected it, and the model still said "All set". Keep `low`. Worth a prompt/tool follow-up.
