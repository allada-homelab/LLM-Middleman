# Backend response fixtures

Each `*.json` file holds one HTTP response body, byte-exact, in a JSON wrapper so editors
and pre-commit fixers can't change the bytes (CRLF, trailing blank lines):

| Field | Meaning |
|-------|---------|
| `description` | What the response shows. |
| `source` | `captured` (recorded from a real server) or `spec-derived` (built from documentation). |
| `server` / `spec` | The server version for a capture, or the documentation the shape comes from. |
| `request` | The request that produced a capture (captures only). |
| `status` | HTTP status. |
| `body` | The exact response body. |

Tests replay them through the real adapters (`tests.conftest.capture_response`).

## `langgraph/`

Captured from `langgraph-api` 0.16.6 (LLMM-022).

## `openai_compat/` and `ollama/` (LLMM-024)

Captured on 2026-10-25 from local servers with no API keys, so nothing is redacted:

- **Ollama 0.40.6** (`alpine/ollama:0.40.6`) with `qwen3:0.6b`: the native `/api/chat` files
  in `ollama/` and the `ollama_v1_*` files (its OpenAI-compatible `/v1` shim).
- **llama.cpp server b11854** (`ghcr.io/ggml-org/llama.cpp:server`, `--jinja`), serving the
  same qwen3 0.6b GGUF: the `llamacpp_*` files.

**Spec-derived**: a local server can't produce these, so they were built from the documented
shapes. Their `spec` field names the source:

| File | Shape |
|------|-------|
| `openai_compat/vllm_midstream_error.json` | vLLM `data: {"error": {…}}` frame, then `[DONE]` |
| `openai_compat/openrouter_midstream_error.json` | OpenRouter top-level `error` plus `finish_reason: "error"`, HTTP 200 |
| `openai_compat/openai_refusal.json` | OpenAI `delta.refusal` stream with null content |
| `openai_compat/openai_max_tokens_unsupported_400.json` | OpenAI reasoning model rejecting `max_tokens` |
| `openai_compat/openai_insufficient_quota_429.json` | OpenAI out-of-quota 429 |
| `ollama/midstream_error.json` | Ollama final `{"error": "…"}` NDJSON line, HTTP 200 |

## `n8n/` and `converse/` (LLMM-025)

These use the older `langgraph/`-style wrapper (provenance in a `server` field, no
`source` field):

- **n8n 2.44.6** (`n8nio/n8n:2.44.6`, Docker): 19 files captured from real workflows that
  stream or reply without an LLM node (Respond to Webhook, Chat Trigger, Wait for the
  keepalive). `agent_intermediate_steps.json` is written from n8n's source, since an AI
  Agent's steps need a chat model to capture.
- **converse**: captured from the repo's own stub, `scripts/e2e/converse_sse_stub.py`.
