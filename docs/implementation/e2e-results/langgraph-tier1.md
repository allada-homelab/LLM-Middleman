# LLMM-022 live E2E — LangGraph tier-1

Backend: `langgraph dev` (langgraph-api **0.16.6**, langgraph 1.2.17, langchain-core via
`uvx --python 3.12 --from "langgraph-cli[inmem]" --with langchain-core`) · Date: **2026-10-24** ·
Driver: the real `LangGraphAdapter` (and the real conversation entity for the tool-loop row)
inside a `pytest-homeassistant-custom-component` HA instance (HA 2026.10.3), over real sockets.
No HA container and no LLM: the graphs in `scripts/e2e/langgraph/graphs.py` use a scripted
streaming chat model.

## Wire findings (raw probes, before the rework)

Each finding below was observed on the wire; representative raw bodies are stored verbatim
(CRLF included) as test fixtures in `tests/backends/fixtures/langgraph/*.json`.

| Probe | Observed | Consequence |
|---|---|---|
| ReAct graph, `stream_mode=["messages-tuple","updates"]` | `messages` frames: `AIMessageChunk`s with `tool_call_chunks` and `content:""`, then a **`type:"tool"` frame whose `content` is the tool output** (`"sunny and 21C"`), then answer tokens. `updates` frames: `{"agent":{"messages":[{type:"ai", tool_calls:[{name,args,id}], usage_metadata}]}}`, `{"tools":{"messages":[{type:"tool", tool_call_id, name, content, status}]}}` | Bug 1 confirmed: the old parser would speak the tool output. Speak only AI types; take finished tool calls/results + usage from `updates`. |
| Node returning an `AIMessage` without streaming | one `messages` frame with **`type:"ai"`** (whole message), plus its `updates` frame | `ai` must be spoken as well as `AIMessageChunk`. |
| Graph calling `interrupt({"question": …})` | no `messages`; `event: updates` `{"__interrupt__":[{"value":{"question":…},"id":…}]}`, then EOF. `GET /threads/{id}` → `status:"interrupted"`, `interrupts:{<task>:[{value,id}]}` | Detect via `updates`; speak `value`/`question`/`message`. |
| `{"command":{"resume":"kitchen"}}` on that thread | `"Turning on the lights in the kitchen."`, thread back to `idle` | Resume works. |
| New `input` on an interrupted thread | the graph restarts from START and interrupts again | Resume must use `command`, not input. |
| `command.resume` on an idle thread | HTTP 200, `metadata` then EOF — **nothing happens** (utterance lost); on a brand-new thread an `event: error` (`TypeError`) | Gate resume on `GET /threads/{id}` showing a pending interrupt. |
| `if_not_exists:"create"` on a random uuid thread | HTTP 200, thread created | Deterministic uuid5 thread ids need no POST /threads. |
| Disconnect mid-run with `on_disconnect:"cancel"` | run `interrupted`, thread `status:"interrupted"` **with `interrupts:{}`** | Field confirmed (`RunCreateStreamingStateful.on_disconnect: cancel|continue`, default `continue` — server OpenAPI and live docs). Resume detection must read `interrupts`, not `status`. |
| Disconnect without it | run still `running` / thread `busy` | Abandoned runs kept running before the rework. |
| `POST /threads/{id}/runs/{run_id}/cancel` | `202` (query `wait=false`, `action=interrupt|rollback`); `404` once the run is no longer active | Best-effort cancel; 404 is normal after `on_disconnect`. |
| Double-text (slow graph, 2nd turn 1.5 s into the 1st), per `multitask_strategy` | `rollback`: 1st stream EOFs silently, thread holds only the 2nd human message · `interrupt`: both human messages kept, 1st unanswered · `reject`: 2nd gets **HTTP 409** · `enqueue`: 2nd waits ~40 s for the 1st | Default `rollback` for voice. |
| 40 s silent node | `: heartbeat` SSE comment every **5 s** | The 30 s `sock_read` idle timeout never fires; no per-preset timeout needed. |
| `context` with extra keys | graph with no `context_schema`: accepted; dataclass schema: unknown keys dropped | Adding HA metadata keys is backward compatible. |
| `GET /assistants/agent` | **422** "must be a UUID"; `POST /assistants/search {"graph_id":"nope"}` → **404**; known graph → list | Validate UUIDs with GET, graph ids with search. |

## Live E2E (`scripts/e2e/langgraph_live_e2e.py`)

Command (server from a scratch copy of `scripts/e2e/langgraph/` on port 2025):

```
LANGGRAPH_E2E_URL=http://127.0.0.1:2025 uv run python -m pytest -p no:cacheprovider scripts/e2e/langgraph_live_e2e.py -v -s
→ 9 passed in 92.26s
```

| Row | Result | Observed (`E2E …` line) |
|---|---|---|
| Tokens stream, tool output not spoken | **PASS** | spoken `"It is sunny and 21C in Paris."` in 5 content deltas; tool call `get_weather {"city":"Paris"}` external=true; 1 tool result; trace `{"stats":{"input_tokens":34,"output_tokens":12}}` |
| Prompt once; HA context | **PASS** | 3 turns, each reply `ctx lang=en dev=dev-1 sat=sat-1 conv=e2e-conv user=user-1 prompt='Be brief.' sys_in_state=0`; `GET /threads/{id}/state` roles `[human, ai] × 3` (**0** system messages) |
| Interrupt → resume | **PASS** | asked `"Which room should I turn the lights on in?"`, continue_conversation=true, thread `interrupted`; reply "kitchen" → `"Turning on the lights in the kitchen."`, thread `idle` |
| Double-text (default rollback) | **PASS** | 2nd turn `"Done after a slow step."` (40.0 s); superseded turn yielded no deltas (entity voices the fallback); 1st run deleted (`GET …/runs/{id}` has no status); thread human messages `["second"]` |
| Cancel cancels server run | **PASS** | task cancelled after `metadata`; run `01a17013-…` status `interrupted` |
| Thread recreate after DELETE | **PASS** | `echo:one` → `DELETE /threads/{id}` 204 → `echo:two`; thread re-created holding only `["two"]` |
| Slow silent step (40 s > 30 s idle) | **PASS** | `"Done after a slow step."` after 41.0 s |
| Assistant check | **PASS** | graph id `react` ok; its UUID `166c0d22-…` ok via GET; `nope` → `AssistantNotFoundError` |
| One POST per tool turn (entity) | **PASS** | 1 run POST; speech = answer; chat log roles `[system, assistant, tool_result, assistant]` |

## Owner-run rows (not run here)

- A **real tool-calling model** (OpenAI/Anthropic) in a ReAct graph: confirm real providers'
  `tool_call_chunks`/`usage_metadata` shapes match the scripted model's (same LangChain types).
- **LangSmith cloud deployment**: `x-api-key` auth, assistant check, heartbeat cadence, and
  `on_disconnect` behind the cloud proxy.
- **Self-hosted server with custom auth** reading `Authorization: Bearer`.
- **Voice hardware**: the interrupt follow-up keeps the satellite listening; the Assist chat
  renders external tool calls.
- Full HA UI pass of the new config-flow fields (connection *API key header*, agent
  *Double-texting* / *Input messages key* / *Spoken node* / *Stateless runs*).
