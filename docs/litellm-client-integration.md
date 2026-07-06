# Configuring Agents, Skills, MCP servers & MCP gateways from a client

> A field guide for wiring OpenAI-compatible clients, Home Assistant, and the LLM Middleman integration to a **LiteLLM v1.89.3** gateway that fronts A2A agents and an MCP gateway. Base URL used in examples: `https://ai-gateway.rancher.devguy.dev` (public OpenAI-compat). LAN/ungated alias: `https://litellm.internal.devguy.dev`. Admin/UI: `https://litellm.rancher.devguy.dev/ui` (Authentik-gated).

---

> **⚠️ Live-test correction (this session, with your key) — read before §3.** The A2A "easy path" below is documented behaviour, but on **your** gateway it does **not** work yet:
> - `POST /v1/chat/completions` with `model:"a2a/74fce41d-…"` → **400** `"could not find suitable inference handler for a2a/…"`. The A2A→OpenAI-compat bridge is **not wired** on this deployment (or requires config not present).
> - Native `POST /a2a/{id}` `message/send` → **500** `"Failed to parse JSON for agent card from https://ai-gateway.devguy.dev/.well-known/agent.json"`. The one registered agent, **"Test Agent", is a dead placeholder** — its `agent_card_params.url` points at the gateway root, which serves no agent card.
> - What *does* work today: **discovery** — `GET /v1/agents` (200) and the agent card at `GET /a2a/{id}/.well-known/agent-card.json` (200, and its `skills:[{id:"chat",…}]` is real). Invocation is blocked until a **real** A2A agent is registered (one whose `url` serves a valid agent card) and, for the OpenAI-compat path, until the a2a inference handler is enabled.
>
> Everything in §3 is correct *as LiteLLM behaviour*; it's just not yet functional on this gateway. Treat §3 as "how it will work once an agent is properly registered."

## 1. TL;DR + quick reference

- **Agents (A2A)**: discovery works today (`GET /v1/agents`, agent cards, `skills[]`), but **invocation is broken on your gateway** — see the live-test correction above. The *documented* easy path is to set the model string to `a2a/<agent-name>` on `/v1/chat/completions` (standard OpenAI SSE streaming, so **Home Assistant + LLM Middleman `openai_compat` would work with just a model-string swap** — once a real agent is registered and the a2a handler is enabled).
- **MCP servers**: your MCP gateway is enabled but has **zero upstream servers registered**. Nothing is consumable until an admin registers one (config.yaml `mcp_servers:` or the UI). Your virtual key can *read* MCP state but almost certainly cannot *register* servers.
- **Consuming MCP from OpenAI clients**: Chat Completions has **no native MCP**. You get MCP tools either via the **Responses API** `type:"mcp"` tool block, via **LiteLLM's server-side injection** (a `type:"mcp"` tool entry on either endpoint), or via a **DIY function-tool bridge**.
- **Home Assistant + LLM Middleman**: the `openai_compat` preset **cannot** reach LiteLLM's server-side MCP injection (it only emits `{type:"function"}` tools). To give the HA agent MCP tools, bridge them **HA-side** with HA's own MCP *client* integration, then multi-select it under "Control Home Assistant". **Caveat (corrected below):** HA's MCP client is **SSE-only** while LiteLLM's client-facing gateway is **streamable-HTTP-only**, so this path needs a streamable→SSE proxy or a newer HA build — see §6.
- **"Skills"** means three unrelated things. Only **A2A agent-card skills** (read-only discovery) apply to your stack. **Anthropic Agent Skills** (SKILL.md) are Claude-runtime only and don't apply. **MCP** has no "skills" primitive.

### Quick-reference table

| What you want to do | Which client / API | How |
|---|---|---|
| Call an A2A agent, simplest path | Any OpenAI client → `/v1/chat/completions` | `model: "a2a/<agent-name>"`, `stream:true` — **⚠️ 400 on your gateway today (no handler); see §1 correction** |
| Call an A2A agent from Home Assistant | LLM Middleman `openai_compat` | Set model to `a2a/<agent-name>` — **⚠️ blocked until §1 correction resolved** |
| Call an A2A agent natively | A2A JSON-RPC client → `POST /a2a/{id_or_name}` | `method:"message/send"` — **⚠️ 500 on your gateway today (placeholder agent); see §1** |
| Discover agents | HTTP | `GET /v1/agents` |
| Read an agent's advertised skills | HTTP | `GET /a2a/{id}/.well-known/agent-card.json` |
| Register an A2A agent | Admin | config.yaml `agents:` block, agent-create API, or UI |
| Register an MCP server | Admin | config.yaml `mcp_servers:`, `POST /v1/mcp/server`, or UI |
| Consume MCP from a generic MCP client | Cursor / Claude Desktop | Point at `<base>/mcp/`, `Accept: text/event-stream` |
| Consume MCP from an OpenAI client | `POST /v1/responses` | `tools:[{type:"mcp", server_url:"litellm_proxy", …}]` |
| Consume MCP from a Chat Completions client | `POST /v1/chat/completions` | `tools:[{type:"mcp", server_url:"litellm_proxy/<server>/mcp", …}]` |
| Give the HA agent MCP tools | Home Assistant | HA `mcp` client integration (needs SSE bridge) → multi-select in LLM Middleman |
| Let external clients control HA | Home Assistant | HA `mcp_server` integration → register HA upstream in LiteLLM |

---

## 2. What's live on your gateway right now

Probed live this session (authoritative):

- **Gateway**: LiteLLM proxy **v1.89.3**. A2A gateway (added v1.80.8-stable) and MCP gateway are both available.
- **A2A agents**: `GET /v1/agents` → **200**, one registered agent:
  - `agent_name: "Test Agent"`, `agent_id: 74fce41d-420d-4cc7-8201-23b2ef7a634d`, `litellm_params.is_public: true`, `agent_card_params.url: https://ai-gateway.devguy.dev/`, `protocolVersion: "1.0"`, `securitySchemes.LiteLLMKey: {type:http, scheme:bearer}`, `defaultInputModes/OutputModes: ["text"]`, `capabilities: {}` (no streaming flag advertised).
  - Its `skills[]` (live-fetched from the card): `[{id:"chat", name:"Chat", tags:["chat"], description:"Conversational interaction with the agent."}]` — confirms **A2A `skills` is a top-level card array** (sibling to `capabilities`).
  - **Invocation is BROKEN (live-tested this session):** OpenAI-compat `a2a/<id>` → **400 "could not find suitable inference handler"**; native JSON-RPC `message/send` → **500** because the agent's `url` (`https://ai-gateway.devguy.dev/`) serves no valid agent card. **"Test Agent" is a non-functional placeholder.** Discovery works; calling does not.
  - **Agent-card path confirmed:** `GET /a2a/{id}/.well-known/agent-card.json` → **200**. The legacy `/.well-known/agent.json` → 403 for this key; bare `GET /a2a/{id}` → 405 (it's the POST JSON-RPC endpoint, not a GET card).
- **MCP gateway**: `GET /mcp/enabled` → `{"enabled":true}`. But `GET /v1/mcp/server` → **`[]`** — **zero upstream MCP servers registered**. Nothing MCP is consumable until an admin adds one.
- **MCP endpoint**: `/mcp/` is a **streamable-HTTP** endpoint (a single POST endpoint whose responses stream back as SSE) → **406 "Client must accept text/event-stream"** unless `Accept: text/event-stream` is sent. **This 406 is normal streamable-HTTP behavior, not a legacy dual-endpoint SSE transport and not a misconfiguration.** Routing is `/mcp/<server-name-or-access-group>`; a bad segment → `"MCP server, toolset, or access group '…' not found"`.
- **Responses API**: `POST /v1/responses` exists (`GET` → 405, i.e. POST-only).
- **Models**: `large-default` is a live local vLLM that streams and emits `tool_calls`. `openai/*` model names all **500** (the gateway has no upstream OpenAI key).
- **Your virtual key**: scoped to `llm_api_routes`. It **can** `GET /v1/agents`, `GET /v1/mcp/server`, and reach `/mcp/`. It **cannot** call `/model/info`, and write actions (registering agents/servers) are almost certainly admin/master-key gated (unconfirmed — you have no key to test with this session).

---

## 3. Agents (A2A)

LiteLLM's "Agents" feature is its A2A gateway. Registered agents can be invoked with the same key/team access controls and logging as LLM APIs.

### 3.1 OpenAI-compatible invocation (the easy path)

Set the model to `a2a/<agent-name>` and POST to `/v1/chat/completions`. The `a2a/` prefix is **mandatory** — it routes to the agent instead of an LLM. Streaming returns standard OpenAI SSE `choices[].delta.content` chunks.

```bash
curl -X POST https://ai-gateway.rancher.devguy.dev/v1/chat/completions \
  -H 'Authorization: Bearer <virtual-key>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"a2a/Test Agent","messages":[{"role":"user","content":"Hello, what can you do?"}],"stream":true}'
```

> **Inferred (untested live):** whether the space in `a2a/Test Agent` is accepted by OpenAI clients, or whether you must use the UUID form `a2a/74fce41d-…`. If the name misbehaves, fall back to the UUID.

### 3.2 Native A2A JSON-RPC

POST JSON-RPC 2.0 to `POST /a2a/{agent_id_or_name}` (UUID or registered name). Core methods: `message/send`, `message/stream`. Task-management methods documented for the A2A gateway include `tasks/get`, `tasks/list`, `tasks/cancel`, `tasks/resubscribe`, `tasks/pushNotificationConfig/{set,get,list,delete}`, and `agent/getAuthenticatedExtendedCard`. There is also an alias `POST /a2a/{agent_id}/message/send`.

> **Inferred:** the prior draft's split of these methods into "run through the full LiteLLM pipeline" (`message/send`, `message/stream`) vs "passthrough" (`tasks/*`, `agent/getAuthenticatedExtendedCard`) is **not stated in the LiteLLM A2A docs** — treat that categorization as inferred, not doc-confirmed.

```bash
curl -X POST https://ai-gateway.rancher.devguy.dev/a2a/74fce41d-... \
  -H 'Authorization: Bearer <virtual-key>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"req-1","method":"message/send",
       "params":{"message":{"role":"user","parts":[{"kind":"text","text":"Hello"}],"messageId":"msg-1"}}}'
```

> **Inferred:** the exact `params.message` shape above (`parts:[{kind:"text",text:…}]`, `messageId`) is the A2A spec 0.3+ form, not a LiteLLM-doc-cited example. Plausibly correct; confirm against the agent's advertised `protocolVersion`.

**Agent card** is served at `GET /a2a/{agent_id}/.well-known/agent-card.json` (RFC 8615 standard path) or the legacy `GET /a2a/{agent_id}/.well-known/agent.json`.

> **Correction to the prior draft:** it claimed the card is at `GET /a2a/{agent_id}` — that is wrong. Use the `.well-known/agent-card.json` path.

### 3.3 a2a-sdk (Python)

`pip install 'a2a-sdk>=1.1.0,<2.0' httpx`. Point the client at the LiteLLM base URL + virtual key. Discover via `GET /v1/agents` (returns `agent_name`, `agent_id`, `litellm_params`, `agent_card_params`); stream with `ClientConfig(streaming=True)` and `send_message()`.

```
GET https://ai-gateway.rancher.devguy.dev/v1/agents
  -> [{agent_name, agent_id, litellm_params, agent_card_params}]
```

### 3.4 Registration

**config.yaml (GitOps, loaded at startup):**

```yaml
agents:
  - agent_name: my-agent
    agent_card_params:
      name: "My Agent"
      url: "http://localhost:10001"
      protocolVersion: "1.0"      # 0.3 or 1.0
```

**API (DB-persisted, admin/master key):** LiteLLM exposes a DB-backed agent-create path (admin/master-key gated).

> **Inferred (path not doc-confirmed):** the create route is presented here as `POST /v1/agents` by REST convention, but ground truth only confirms `GET /v1/agents` as the **LIST** route. Confirm the exact create verb/path against the gateway's `/docs` (Swagger) with an admin key before scripting it.

```bash
curl -X POST https://litellm.rancher.devguy.dev/v1/agents \
  -H 'Authorization: Bearer <master-key>' -H 'Content-Type: application/json' \
  -d '{"agent_name":"audit-critical-agent",
       "agent_card_params":{"name":"...","url":"https://...","protocolVersion":"1.0"},
       "litellm_params":{"require_trace_id_on_calls_to_agent":true}}'
```

**Admin UI:** Agents tab (same effect as the API; DB-persisted).

Supported agent providers include A2A, Vertex AI Agent Engine, LangGraph, Azure AI Foundry, Bedrock AgentCore, Pydantic AI.

### 3.5 Auth & credential forwarding

- **Auth to LiteLLM**: `Authorization: Bearer <virtual-key>` or `x-litellm-api-key`.
- LiteLLM **auto-injects** `X-LiteLLM-Trace-Id` and `X-LiteLLM-Agent-Id` upstream.
- **Forwarding client credentials to the upstream agent**, three mechanisms:
  1. **Static admin-configured headers** (win on conflict);
  2. **`extra_headers` allow-list** naming client headers to forward (case-insensitive match);
  3. **Convention `x-a2a-{agent_name_or_id}-{header}`** — client prefixes a header, LiteLLM strips the prefix and forwards it.

```
x-a2a-my-agent-authorization: Bearer <upstream-token>
  -> forwarded upstream as: authorization: Bearer <upstream-token>
```

### 3.6 Open questions (agents)

- Can the `llm_api_routes`-scoped key create agents, or is it admin-gated? (Read works; write untested — no key.)
- Exact agent-**create** verb/path on v1.89.3 (confirm on `/docs`).
- Does `a2a/Test Agent` (name with a space) work in OpenAI clients, or must the UUID be used? (Untested live.)
- Whether `a2a/…` models work on `/v1/responses` (untested).

---

## 4. MCP servers & the MCP gateway

Your MCP gateway is enabled but has **zero registered servers**. Everything below is a prerequisite before any client-side MCP consumption works.

### 4.1 Register the FIRST server (do this first)

**Option A — config.yaml (declarative, restart to apply):**

```yaml
mcp_servers:
  deepwiki:
    url: "https://mcp.deepwiki.com/mcp"
    transport: "http"              # http | sse | stdio — DEFAULTS TO sse IF OMITTED
    spec_version: "2025-06-18"
    description: "DeepWiki docs MCP"
    access_groups: ["dev_group"]
    # auth to the UPSTREAM server, if it needs it:
    # auth_type: "bearer_token"    # see enum note below
    # auth_value: "os.environ/DEEPWIKI_TOKEN"
    # static_headers:
    #   X-Custom: "value"
```

Notes:
- **`transport` defaults to `sse` when omitted.** Set it explicitly to `http` for a streamable-HTTP upstream, or you'll silently get the legacy SSE upstream transport. (This is the connection LiteLLM makes to the *upstream* server — it is **not** a client-facing endpoint; see §6.)
- **`auth_type` enum is not just** `none|api_key|bearer_token|basic|authorization|oauth2|aws_sigv4`. The current docs also list at least `token` and `oauth2_token_exchange` (plus AWS SigV4 sub-keys). Treat the list as **non-exhaustive** — check `docs.litellm.ai/docs/mcp` for the full set.
- For a stdio server, use `command`/`args`/`env` instead of `url`. Restart the proxy, then verify with `GET /v1/mcp/server`.

**Option B — management REST API (no restart; admin/master key; needs `store_model_in_db: true`):**

```bash
curl -X POST https://litellm.internal.devguy.dev/v1/mcp/server \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"server_name":"DeepWiki","url":"https://mcp.deepwiki.com/mcp","transport":"http","available_on_public_internet":true}'
```

> Your `GET /v1/mcp/server -> []` is the **LIST** route returning empty — **not** evidence that create is unavailable. The **CREATE** route is `POST /v1/mcp/server`. Your `llm_api_routes` key almost certainly cannot POST here — use an admin/master key.

**Option C — Admin UI:** `/ui` (Authentik-gated on `litellm.rancher.devguy.dev`) → **MCP Servers** tab → *+ Add New MCP Server*. Same `store_model_in_db: true` persistence requirement.

After creation, `GET /v1/mcp/server` lists it with a `server_id`.

### 4.2 How clients consume `/mcp/`

Point a generic MCP client (Cursor, Claude Desktop, any streamable-HTTP MCP client) at `<base>/mcp/`. Auth with `x-litellm-api-key: Bearer <key>` (or standard `Authorization: Bearer <key>`). **Streamable-HTTP requires `Accept: text/event-stream`** — that's the 406 you saw.

```jsonc
// Cursor mcp.json
{
  "mcpServers": {
    "LiteLLM": {
      "url": "https://ai-gateway.rancher.devguy.dev/mcp/",
      "headers": {
        "x-litellm-api-key": "Bearer $LITELLM_API_KEY",
        "x-mcp-servers": "deepwiki"
      }
    }
  }
}
```

Raw handshake (why the bare call 406'd):

```bash
curl -N https://ai-gateway.rancher.devguy.dev/mcp/ \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json"
```

There's also a no-LLM REST shortcut: `POST /mcp-rest/tools/list` and `POST /mcp-rest/tools/call`.

### 4.3 Per-request server/tool selection

Three interchangeable mechanisms:

```bash
# (a) header: comma-separated server aliases (also accepts access-group names)
-H "x-mcp-servers: deepwiki,github_mcp"
# (b) header: access group
-H "x-mcp-access-groups: dev_group"
# (c) path routing (live-probe confirmed)
https://ai-gateway.rancher.devguy.dev/mcp/dev_group
```

With no scoping, `/mcp/` lists tools from every server the key is permitted to see.

### 4.4 Auth: key-gating vs upstream credentials

- **Which tools a caller sees** is gated by the LiteLLM virtual key/team via `object_permission`:

```bash
curl -X POST https://litellm.internal.devguy.dev/key/generate \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"object_permission":{"mcp_servers":["deepwiki"],"mcp_access_groups":["dev_group"],
        "mcp_tool_permissions":{"deepwiki":["read_wiki_structure","ask_question"]}},
       "mcp_rpm_limit":{"deepwiki":100}}'
```

Effective access is the **intersection** across config + key + team + org (most-restrictive wins; org is a ceiling). `allowed_tools`/`disallowed_tools` at the server config level are an independent allow/block list, also intersected.

- **Upstream MCP-server credentials** (if not baked into config via `auth_type`/`auth_value`/`static_headers`) are passed per-request with a namespaced header `x-mcp-<server-alias>-<header>`. LiteLLM strips the prefix and forwards the rest to that specific server. Legacy `x-mcp-auth: <token>` is forwarded to all servers (deprecated).

```bash
-H "x-mcp-github-authorization: Bearer ghp_xxx"
-H "x-mcp-zapier-x-api-key: zap_xxx"
```

### 4.5 Toolsets (curated cross-server collections)

A **Toolset** is a named collection of specific tools drawn from one or more servers. Manage via `POST /v1/mcp/toolset`, `GET /v1/mcp/toolset`, `DELETE /v1/mcp/toolset/<id>`, or the UI **Toolsets** tab. Consume by pathing to it: `server_url: "litellm_proxy/mcp/<toolset-name>"`.

> **Path-form gotcha (real, not a bug):** the position of `/mcp/` in `server_url` **differs by feature** and it's easy to transpose:
> - Chat Completions **server** injection → `litellm_proxy/<server>/mcp` (server name *before* `mcp`) — see §5.2.
> - **Toolset** → `litellm_proxy/mcp/<toolset-name>` (`mcp` *before* the toolset name).
> Both are correct per LiteLLM docs; don't copy one shape into the other slot.

### 4.6 Open questions (MCP registration)

- The full JSON body accepted by `POST /v1/mcp/server` beyond `server_name`/`url`/`transport`/`available_on_public_internet` (e.g. whether `auth_type`, `spec_version`, `access_groups`, `command/args/env` are accepted at create time or only in config.yaml). Confirm against the live Swagger at the gateway's `/docs`.
- Whether `x-mcp-servers` accepts access-group names on v1.89.3, or whether groups require `x-mcp-access-groups`.
- Path form drift: live probe confirmed flat `/mcp/<server-name>`; some docs show a `/mcp/<server_name>/mcp` variant. Confirm with a real key.

---

## 5. Consuming MCP from OpenAI-compatible clients

**Core fact (verified):** the OpenAI **Chat Completions** API has **no native MCP support**. MCP is surfaced only via (1) the **Responses API** `mcp` tool type, or (2) a gateway that translates a `type:"mcp"` tool entry into real tool calls. LiteLLM v1.89.3 does **both** — but it does **not** globally auto-inject MCP tools; **the client must send a `type:"mcp"` tool entry**.

### 5.1 Responses API — the OpenAI-native way

`type:"mcp"` tool objects exist **only** on `/v1/responses`. Fields: `type`, `server_label`, `server_url` (remote MCP URL) **or** `connector_id`, `server_description`, `headers` (map), `allowed_tools` (array filter), `require_approval` (`"never"|"always"|object`), `authorization` (OAuth token), `defer_loading` (bool).

Reach **LiteLLM-aggregated** MCP tools by self-referencing `server_url:"litellm_proxy"` (bare literal on this endpoint = all servers the key is scoped to):

```bash
curl -X POST https://ai-gateway.rancher.devguy.dev/v1/responses \
  -H "Authorization: Bearer $LITELLM_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"large-default","input":"Run available tools","tool_choice":"required",
       "tools":[{"type":"mcp","server_label":"litellm","server_url":"litellm_proxy","require_approval":"never"}]}'
```

Or point at an arbitrary remote MCP server directly:

```bash
curl https://ai-gateway.rancher.devguy.dev/v1/responses -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"large-default","input":"List repo issues",
       "tools":[{"type":"mcp","server_label":"dmcp","server_url":"https://dmcp-server.deno.dev/sse","require_approval":"never"}]}'
```

With `require_approval:"never"`, LiteLLM discovers the tools, runs the tool loop server-side, and feeds results back before returning.

### 5.2 Chat Completions — via LiteLLM server-side injection

You *can* get MCP into a Chat Completions call, but only by adding a `type:"mcp"` entry. **The path form differs by endpoint**: chat/completions uses `litellm_proxy/<server>/mcp`; responses uses the bare `litellm_proxy`. Don't assume one works on the other (and see the toolset transposition warning in §4.5).

```bash
curl https://ai-gateway.rancher.devguy.dev/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"large-default",
       "messages":[{"role":"user","content":"Summarize the latest open PR."}],
       "tools":[{"type":"mcp","server_url":"litellm_proxy/github/mcp","server_label":"github_mcp","require_approval":"never"}]}'
```

The target model must emit `tool_calls` (`large-default` does). Works only once an MCP server is registered (currently none).

### 5.3 DIY function-tool bridge (for clients that only speak `{type:"function"}`)

For a client that can't send a `type:"mcp"` entry: open an MCP session to `/mcp/<server-or-access-group>` (with `Accept: text/event-stream` + `Authorization`), `list_tools()`, convert each MCP tool → `{type:"function", function:{name, description, parameters:<inputSchema>}}`, send in `tools[]`, and on `tool_calls` execute against the MCP session and append a `role:"tool"` message. LiteLLM's SDK ships a helper that does the conversion:

```python
# Import path is inferred from memory — verify against the installed litellm version.
from litellm.experimental_mcp_client import load_mcp_tools, call_openai_tool
tools = await load_mcp_tools(session=mcp_session, format='openai')  # [{type:'function',...}]
resp = client.chat.completions.create(model='large-default', messages=msgs, tools=tools)
tc = resp.choices[0].message.tool_calls[0]
result = await call_openai_tool(session=mcp_session, openai_tool=tc)
msgs += [resp.choices[0].message,
         {'role':'tool','tool_call_id':tc.id,'content':result.content[0].text}]
```

> **Inferred:** the helper *names* (`load_mcp_tools`, `call_openai_tool`) are documented, but the **module import path** `litellm.experimental_mcp_client` is from memory — confirm with `python -c "import litellm.experimental_mcp_client"` against the installed version.

### 5.4 Open questions (OpenAI-client MCP)

- Whether `large-default` completes LiteLLM's server-side MCP auto-execution loop end-to-end (discover → call → feed back). Unverifiable here (no key + zero servers).
- OpenAI has renamed/moved the MCP-tool docs page (301); re-verify field names (`server_url`/`connector_id`/`require_approval`/…) before hardcoding.

---

## 6. Home Assistant

HA plays **two independent, single-direction MCP roles**, each a stock core integration — plus the LLM Middleman path, which needs **neither**.

### 6.1 The two integrations (don't conflate them)

| | `mcp_server` (HA as SERVER) | `mcp` (HA as CLIENT) |
|---|---|---|
| Direction | Exposes HA's Assist tools **outward** | Connects **out** to a remote MCP server |
| Endpoint | `https://<ha>/api/mcp` | Config field **"SSE Server URL"** (must end `/sse`) |
| Transport | **Streamable HTTP** | **SSE only** (as of the current HA `mcp` docs) |
| Auth | Long-lived token as `Authorization: Bearer` (or OAuth/IndieAuth) | OAuth Client ID/Secret (Application Credentials) **or none** — **no bearer/token field** |
| Tools exposed/consumed | HA Assist API, gated by Assist exposure | Wraps remote tools as a synthetic `llm.API` |

### 6.2 Topology (i) — LiteLLM registers HA as an upstream MCP server

So that **other** LiteLLM clients/agents (the A2A "Test Agent", `/v1/responses`, Claude Desktop) can control the home. This direction works cleanly: HA-as-server speaks streamable HTTP, which is exactly what LiteLLM's *upstream* connector wants.

**HA side:** add **"Model Context Protocol Server"** integration → `Settings > Voice assistants > Expose` (only exposed entities are controllable — this is the entire safety surface) → create a long-lived token under `Profile > Security`. Endpoint: `https://homeassistant.local:8123/api/mcp`, Streamable HTTP, bearer token.

**LiteLLM side:**

```yaml
mcp_servers:
  home_assistant:
    url: "https://homeassistant.local:8123/api/mcp"
    transport: "http"            # streamable HTTP — REQUIRED; omitting defaults to sse
    auth_type: "bearer_token"    # -> Authorization: Bearer <auth_value>
    auth_value: "<HA_LONG_LIVED_ACCESS_TOKEN>"
    description: "Home Assistant Assist API"
```

### 6.3 Topology (ii) — HA client points at LiteLLM to gain extra tools

`Settings > Devices & Services > Add Integration > "Model Context Protocol"`. Required field: **SSE Server URL** (HA's client wraps the remote tools as a synthetic `llm.API` that shows up in a conversation agent's "Control Home Assistant" list).

> **Corrected from the prior draft — this is the load-bearing fix.** The prior draft said "you must hit LiteLLM's SSE endpoint (`…/sse`)." **LiteLLM's client-facing MCP gateway is streamable-HTTP-only at `/mcp/`; there is no documented client-facing `/sse` endpoint on v1.89.3.** The `sse|http|stdio` `transport` setting in §4.1/§6.2 governs how LiteLLM connects to an **upstream** server — it is *not* a client endpoint you can point HA at. And the MCP spec has **deprecated the standalone SSE transport** in favor of streamable HTTP.
>
> Consequences for this topology:
> - **HA's `mcp` client is SSE-only** (per the current HA docs), and **LiteLLM offers no SSE client endpoint** → a **direct** HA→LiteLLM connection is **likely infeasible as-is**, regardless of which LiteLLM URL you use.
> - **You need a bridge**: run a **streamable-HTTP → SSE proxy** (e.g. an `mcp-proxy`) in front of LiteLLM and give HA that proxy's `…/sse` URL. This is a hard requirement here, not an optional "shim."
> - **Version-drift caveat (verify):** if a newer HA release adds streamable-HTTP support to the `mcp` client, the bridge becomes unnecessary — check your HA version's `mcp` integration docs before building the proxy.
> - **Auth mismatch compounds it:** HA's client offers OAuth/none but **no static-bearer field**, while LiteLLM expects a bearer key. Use the **ungated LAN endpoint** (`litellm.internal.devguy.dev`) behind the proxy, or wire up OAuth.
> - **Zero servers:** even fully bridged, this surfaces nothing until upstream MCP servers are registered on LiteLLM (currently none).

### 6.4 Where LLM Middleman fits (the everyday path — no MCP needed)

The `openai_compat` preset is a **Chat Completions client**: on each turn it forwards HA's Assist tool **schemas** as `{type:"function",…}` in the request body. The backend **model only decides** which tool to call (emits `tool_calls`); **Home Assistant executes the tool locally** via ChatLog/Assist and appends the result, looping (bounded) until done.

Key consequences (verified against the repo):
- The backend model/LiteLLM **never reach into HA** and **never see HA credentials** — only tool schemas. So you do **not** need `mcp_server` for the LLM Middleman agent to control HA.
- Because the preset **only emits `{type:"function"}` tools and has no code path to add a `type:"mcp"` entry**, LiteLLM's server-side MCP injection is **unreachable** from it. "Point `openai_compat` at the gateway and MCP tools appear" is **false**.
- To give the LLM Middleman agent MCP tools, bridge **HA-side**: complete topology (ii) (including the SSE proxy above), then in the conversation subentry multi-select **both** "Assist" and the MCP-client `llm.API` under "Control Home Assistant" (`CONF_LLM_HASS_API` is a multi-select; the code explicitly anticipates "any HA MCP-client entry"). HA merges the tools; `openai_compat` forwards them all as function tools; HA executes them locally.
- Requires a tool-capable model — `large-default` (emits `tool_calls`). `openai/*` models 500 on this gateway.

### 6.5 Open questions (Home Assistant)

- Whether an `mcp-proxy` (streamable→SSE) reliably fronts LiteLLM's `/mcp/` for HA's SSE-only client, and what exact `…/sse` URL the proxy should present (including whether server/access-group namespacing survives the proxy).
- Whether a current or upcoming HA release adds streamable-HTTP support to the `mcp` client (which would remove the bridge requirement entirely).
- Whether HA's OAuth (IndieAuth via Application Credentials) can complete a handshake against a proxied MCP OAuth flow, or whether only the ungated LAN endpoint is viable.
- Whether HA's legacy `/mcp_server/sse` server endpoints still exist on the owner's version, or only `/api/mcp`.

---

## 7. "Skills" — three unrelated meanings

Only one is reachable from your stack.

### (a) A2A agent-card skills — discoverable, NOT client-selectable ✅ relevant

`skills` is a **top-level array on the Agent Card** (sibling to `capabilities`, `defaultInputModes`, `defaultOutputModes` — **not** nested in `capabilities`, which holds only boolean flags). Each `AgentSkill` has `id`, `name`, `description`, `tags`, `examples`, `inputModes`, `outputModes` (optionally `security`).

Discover:

```bash
# 1. list agents
curl -s https://ai-gateway.rancher.devguy.dev/v1/agents -H "Authorization: Bearer $KEY"
#   -> agent_id "74fce41d-...", agent_name "Test Agent"

# 2. read that agent's skills[]
curl -s https://ai-gateway.rancher.devguy.dev/a2a/74fce41d-.../.well-known/agent-card.json \
  -H "Authorization: Bearer $KEY"
```

**Skill selection is implicit**: `message/send` has **no `skillId` field** — you send natural language and the agent chooses the skill. `id`/`tags`/`examples` are for discovery/matching, not for addressing a call.

> **Live-confirmed this session:** the "Test Agent" card's real payload is `skills:[{id:"chat", name:"Chat", tags:["chat"], description:"Conversational interaction with the agent."}]`, and `skills` is top-level (sibling to `capabilities:{}`) — exactly as the spec says. Fetched via `GET /a2a/74fce41d-…/.well-known/agent-card.json` → 200.

### (b) Anthropic Agent Skills (SKILL.md) — NOT configurable from your stack ❌ not applicable

SKILL.md folders (YAML `name`+`description` always loaded; body on trigger; bundled files as needed), invoked automatically by **Claude** via progressive disclosure in Claude's code-execution VM. They exist **only** on Claude surfaces: claude.ai (zip upload), Claude Code (`~/.claude/skills/` or `.claude/skills/`), and the **Claude API** (`/v1/skills`, referenced by `skill_id` in the `container` param, behind beta headers `skills-2025-10-02` + `code-execution-2025-08-25` + `files-api-2025-04-14`).

There is **no** way to configure/invoke them through a generic OpenAI-compatible client, through LiteLLM's OpenAI-compat surface, or through HA's `openai_compat` preset. Your turns hit a local vLLM (`large-default`) via LiteLLM, **not** the Claude API — so Anthropic Agent Skills **do not apply here at all**.

### (c) MCP has no "skills" primitive ⚠️ terminology

MCP's three server primitives are **tools** (model-controlled; `tools/list`, `tools/call`), **resources** (app-controlled; `resources/list`, `resources/read`), and **prompts** (user-controlled templates; `prompts/list`, `prompts/get`). The nearest analog to a reusable "skill" is an **MCP prompt** (or a tool for actions). On your gateway, `GET /mcp/enabled -> {"enabled":true}` but `GET /v1/mcp/server -> []`, so there are no prompts/tools to list until a server is registered.

```bash
# once a server is registered:
curl -s https://ai-gateway.rancher.devguy.dev/mcp/<server-name> \
  -H "Authorization: Bearer $KEY" -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"prompts/list"}'   # or tools/list
```

**Guidance:** confirm which "skills" the user means before building — A2A skills (relevant, read-only), Anthropic SKILL.md (not applicable), or MCP prompts/tools (relevant once a server exists).

---

## 8. Corrections to the prior draft + open questions

### Corrections to `LiteLLM_agent_inference.md`

1. **Agent-card path was wrong.** Draft said `GET /a2a/{agent_id}`; correct is `GET /a2a/{agent_id}/.well-known/agent-card.json` (or legacy `/.well-known/agent.json`).
2. **The "wildcard-routing gotcha"** (a bare `*` `model_list` making an unprefixed name fall through to llama-swap) is a claim about *your own gateway's config*, **not** documented LiteLLM behaviour — label it **inferred**. The doc-verified fact is only that the `a2a/` prefix routes to the agent.
3. **Missing the entire agent-headers surface**: the `x-litellm-api-key` auth alternative, the auto-injected `X-LiteLLM-Trace-Id`/`X-LiteLLM-Agent-Id`, and the three credential-forwarding mechanisms (static headers, `extra_headers` allow-list, `x-a2a-{agent}-{header}`).
4. **config.yaml `agents:` shape** was under-specified: real shape is `agents: -> agent_name + agent_card_params{name,url,protocolVersion}`, with protocol versions 0.3 vs 1.0, and the `agent/getAuthenticatedExtendedCard` JSON-RPC method. The full task-method set also includes `tasks/resubscribe` and `tasks/pushNotificationConfig/{set,get,list,delete}`; the earlier "pipeline vs passthrough" split of these methods is **inferred, not doc-confirmed**.
5. **What the draft got right (confirmed):** A2A added in v1.80.8-stable; the `a2a/` prefix; `POST /a2a/{id_or_name}` JSON-RPC; `message/send` + `message/stream`; `a2a-sdk>=1.1.0` + `ClientConfig(streaming=True)`.

### Cross-cutting corrections

- `GET /v1/mcp/server -> []` is the **LIST** route returning empty — not proof create is unavailable. Create is `POST /v1/mcp/server` (admin key + `store_model_in_db:true`).
- Registered MCP servers do **not** automatically flow into HA. LLM Middleman injects **HA's own** tools via `/v1/chat/completions`, not the gateway's MCP toolset.
- The 406 at `/mcp/` is **normal streamable-HTTP behavior** (streamable-HTTP requires `Accept: text/event-stream`) — it is *not* evidence of a legacy dual-endpoint SSE transport, and not a misconfiguration.
- **LiteLLM's client-facing MCP gateway is streamable-HTTP-only at `/mcp/`. There is no documented client-facing `/sse` endpoint.** The `sse|http|stdio` `transport` config value governs LiteLLM's connection to **upstream** servers only. The prior draft's "hit LiteLLM's `…/sse` endpoint" guidance for HA was **wrong** and has been replaced with the SSE-proxy requirement in §6.3.
- config.yaml `transport` **defaults to `sse`** when omitted — set `http` explicitly for streamable-HTTP upstreams.
- `auth_type` enum is **non-exhaustive** in this guide — the docs also list `token`, `oauth2_token_exchange`, and AWS SigV4 sub-keys beyond the values shown.
- MCP `server_url` path-form **ordering differs by feature and is easy to transpose**: chat/completions server = `litellm_proxy/<server>/mcp`; toolset = `litellm_proxy/mcp/<toolset>`; responses = bare `litellm_proxy`. All correct — don't cross them.
- OpenAI's remote-MCP tool is **Responses-API-only**; Chat Completions "MCP" works only via LiteLLM's translation layer, which is gateway-specific.
- HA's `mcp` client is SSE + OAuth/none with **no bearer field**; the bearer guidance in `mcp-to-home-assistant.md` applies to HA-as-*server* (`mcp_server`) only.
- One earlier WebFetch wrongly nested A2A `skills` inside `capabilities` — `skills` is top-level; `capabilities` holds only boolean flags.

### Consolidated open questions

- Can the `llm_api_routes` virtual key write (register agents/MCP servers), or is it read-only? (No key to test.)
- Exact agent-**create** verb/path on v1.89.3 (Swagger `/docs`).
- Does `a2a/Test Agent` (space in name) work in OpenAI clients, or must the UUID be used?
- Full `POST /v1/mcp/server` body on this version; whether `x-mcp-servers` accepts group names; exact `/mcp/<name>` vs `/mcp/<name>/mcp` path form.
- For HA topology (ii): does an `mcp-proxy` (streamable→SSE) reliably front LiteLLM, and/or does a newer HA build add streamable-HTTP client support (removing the bridge)?
- Whether `large-default` completes LiteLLM's server-side MCP auto-execution loop end-to-end.
- The "Test Agent" card's actual advertised `skills[]` (not fetched — no key).
- Which upstream MCP server(s) the owner wants to register first (currently zero — the blocker for every MCP consumption path above).
