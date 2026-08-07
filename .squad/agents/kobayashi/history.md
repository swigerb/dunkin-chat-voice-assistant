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
