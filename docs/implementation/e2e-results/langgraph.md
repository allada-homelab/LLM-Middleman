# LLMM-018 live E2E — LANGGRAPH preset

HA version: **2026.7.1** · Integration commit: **cdecb35** · Backend: `langgraph dev` (langgraph 1.2.7 / langgraph-api 0.10.0 / langchain-core 1.4.8) · Date: **2026-07-05**

## Headline: frame-shape verdict — **MATCH** (parser is correct), with one dead-code note

The researcher's least-confident item (the `messages-tuple` frame shape + terminal event
names) is now settled against a **real `langgraph dev` server**. The captured wire frames
**match** `backends/langgraph.py`'s parser field-for-field. The adapter correctly streamed
and reassembled a live reply end-to-end through the HA conversation pipeline.

**One real discrepancy, not a functional bug:** `langgraph dev` / `langgraph-api` 0.10.0
**never emits an `event: end`** — a successful run terminates by **closing the SSE stream
(EOF)**. The adapter's `_EVENT_END = "end"` branch (`langgraph.py:83`, handled at
`:335`) is therefore **dead code that never fires**. It is harmless because the streaming
loop exits naturally when `async_iter_sse` exhausts on stream close, so success still works
(proven live). Recommend LLMM-011 drop the `end` handling or re-document success as
"terminates on EOF". See bug-report text below.

Ground-truth artifact: **`langgraph-raw-capture.txt`** (raw SSE bytes, this session).

## Placement / reachability (recorded per brief)

**Option (a) worked.** `langgraph dev --host 0.0.0.0 --port 2024 --no-browser` was run in
**this devcontainer** (bridge IP `172.17.0.5`). The HA container reaches it:
`docker exec llmm-e2e-ha curl http://172.17.0.5:2024/ok` → `{"ok":true}`. No sibling
container was needed. Parent-entry `base_url = http://172.17.0.5:2024`, `assistant_id = agent`,
no api_key. Graph: a `MessagesState` echo node using `GenericFakeChatModel` (no LLM key),
which streams the reply token-by-token so `messages-tuple` emits real token frames.

## Field-by-field comparison — observed frames vs `backends/langgraph.py`

| Adapter expectation (code) | Observed on the wire | Verdict |
|---|---|---|
| SSE event name `messages` (`_EVENT_MESSAGES`, `:85`/`:339`) — also accepts `messages/*` | `event: messages` (9 token frames) | **MATCH** |
| `data` = 2-element JSON array `[chunk, metadata]` (`_parse_messages_frame`, `:123-145`) | `data: [ {AIMessageChunk...}, {metadata...} ]` — exactly 2 elements | **MATCH** |
| `chunk` is a dict; text at `chunk["content"]` (`_extract_text`, `:100-120`) | `chunk` dict, `"content":"LangGraph"`,`" "`,`"echo:"`… (string tokens) | **MATCH** |
| node from `metadata["langgraph_node"]` (`:144`) → node filter | `metadata.langgraph_node = "respond"` | **MATCH** |
| Terminal success `event: end` → `return` (`_EVENT_END`, `:83`/`:335`) | **no `end` event** — stream EOFs after last frame (`chunk_position:"last"`) | **MISMATCH — dead code; EOF terminates loop, harmless** |
| Terminal error `event: error` → raise (`_EVENT_ERROR`, `:84`/`:337`) | Confirmed emitted on stream failure: `langgraph_api/stream.py:788` `("error",…,"error",…)` + `sse.py:92` `json_to_sse(b"error", exc)` | **MATCH** (name correct) |
| Initial `event: metadata` frame | present (`data:{"run_id",…,"attempt":1}`) — adapter `continue`s past it | **OK** (ignored, correct) |
| Thread create: `POST /threads` → `{"thread_id":…}` (`:279-290`) | `{"thread_id":"019f3000-…", …}` | **MATCH** |
| Run: `POST /threads/{id}/runs/stream`, body `{"assistant_id","input":{"messages":[…]},"stream_mode":"messages-tuple"}` (`:294-306`,`:322-324`) | accepted (HTTP 200, `Content-Type: text/event-stream`) | **MATCH** |
| Line terminators | CRLF (`\r\n`); `_sse.py` handles CR/LF/CRLF | **MATCH** |

## Per-check matrix (evidence = exact utterance + observation)

| Check | Result | Evidence |
|---|---|---|
| Raw frame-shape capture | **PASS** | `langgraph-raw-capture.txt`: 1×`metadata` + 9×`messages` `[chunk,metadata]` frames, no `end`. |
| Parent entry + connection probe `GET /ok` | **PASS** | Flow `user`→`langgraph`(fields `base_url,api_key,assistant_id`)→`create_entry` title "LangGraph". Probe passed (entry created, no form error). |
| Conversation subentry `E2E LangGraph` | **PASS** | subentry flow `set_options` fields incl. `memory_scope` (stateful) → `create_entry`. Entity `conversation.e2e_langgraph` registered after an entry reload. |
| Streaming turn (reply matches graph) | **PASS** | "what is the capital of France" → speech `"LangGraph echo: what is the capital of France"` (HTTP 200). Token streaming proven at wire+adapter: 9 discrete token frames reassembled; adapter yields role-first then per-content deltas (`:346-349`). |
| Thread continuity (same conversation_id → same thread_id) | **PASS** | Two same-cid turns (`01KWR09NDVEHE6GEGK9PEZGFJH`). Dev-server `/threads/search` + `/threads/{id}/runs`: thread `019f3004-d5bf-7ab0-bc87-33aadb3510a3` holds **2 runs** (both `success`); no extra thread spawned for turn 2. memory_key→thread_id map proven. |
| Terminal-error handling | **PASS** | 2nd parent entry `assistant_id="does-not-exist-xyz"`; turn → run `POST` returns **HTTP 422** → `BackendStreamError` (`langgraph.py:332`) → guard fallback `"Sorry, I could not reach the assistant right now. Please try again."` (HTTP 200, ~0.0 s, no hang). |
| Backend-down fallback | **PASS** | Server stopped; turn → `_create_thread` (`langgraph.py:281`) raises `aiohttp.ClientConnectorError` (connect refused) → guard fallback, HTTP 200, ~0.0 s, no hang. |

### Cross-cutting (per LLMM-018)
- **Streaming-TTS start:** proven at the wire/adapter layer — server emits 9 separate
  token frames and the adapter yields incremental `{"content": …}` deltas before the run
  finishes. (The `/api/conversation/process` REST endpoint returns only the final assembled
  string, so first-token-audio timing itself is an owner-run voice check.)
- **Continuity:** follow-up in the same session hit the same server-side thread (2 runs).
- **Fallback on backend-down:** graceful message, pipeline never hangs.

## Bug report text for LLMM-011 (paste into the ticket)

> **`langgraph dev` never emits `event: end`; the `_EVENT_END` handling is dead code.**
> Live capture against `langgraph dev` (langgraph-api 0.10.0, `stream_mode=messages-tuple`)
> shows a successful run terminates by **closing the SSE stream (EOF)** immediately after the
> last `event: messages` frame — there is **no `event: end`**. `grep` of `langgraph_api`
> confirms no `end` event is emitted anywhere; only `event: error` (stream.py:788 / sse.py:92)
> and the wire trace back this up. Consequently `LangGraphAdapter`'s `_EVENT_END = "end"`
> constant and its `if event.event == _EVENT_END: return` branch (`backends/langgraph.py:83,335`)
> **never execute**. **Impact: none functional** — the `async for … in async_iter_sse(...)`
> loop exits cleanly on stream EOF, so streaming success works (verified end-to-end: reply
> `"LangGraph echo: …"` streamed and spoke correctly). **Recommended fix (cosmetic):** remove
> the dead `_EVENT_END`/`end` handling, or, if kept for hosted-platform compatibility, add a
> comment that `langgraph dev`/OSS terminates on EOF and `end` is a cloud-only convenience —
> and update the module docstring (`:12-13`) which currently states "The run ends on
> `event: end` (success)". The **frame parser (`_parse_messages_frame`) and `messages`/`error`
> event names need no change** — they match the live wire exactly.
>
> **Minor robustness note (not a defect):** on a transport-level backend-down, `_create_thread`
> lets a raw `aiohttp.ClientConnectorError` propagate (it only wraps `status >= 400` as
> `BackendStreamError`). The conversation entity guard catches it broadly → graceful fallback,
> no hang (verified), so behavior is correct; wrapping it as `BackendConnectionError`/
> `BackendStreamError` for consistency with the rest of the adapter would be tidier but is optional.

## Non-issue ruled out (do not re-litigate)
`backends/langgraph.py:132` `except json.JSONDecodeError, ValueError:` is **valid Python 3**
(parses as a 2-tuple of exception types, catches both) — confirmed via `py_compile`
(`COMPILE_OK`) and `ast.parse` (handler type = `Tuple[...]`, name=None) on Python 3.14. Not a bug.

## Teardown (done)
- Deleted my 3 langgraph config entries (`01KWR09KF0…`, `01KWR0AP9B…`, `01KWR0AT5G…`);
  other agents' entries (Ollama, Converse, n8n) left untouched.
- Stopped the `langgraph dev` process group (I started it); confirmed `/ok` → connection refused.
- **`llmm-e2e-ha` left running** (shared with sibling agents). Scratchpad graph/venv live under
  the session scratchpad (`/tmp/claude-1002/.../scratchpad/lgproj`, `lgvenv`) — outside the repo.

## Note on concurrency observed
This HA instance is shared with sibling E2E agents. Mid-run, all `llm_middleman` entries
(incl. mine) were deleted once by another agent's teardown; I recreated my entry+subentry and
re-drove the continuity/error/down checks back-to-back. Also: adding a conversation subentry
over REST did **not** auto-create the entity until an entry **reload** — expected (the UI reloads
after a subentry flow); flagged only as an operational note, not a defect.
