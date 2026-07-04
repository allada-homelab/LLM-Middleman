# Controlling Home Assistant — MCP client → `mcp_server`

How your external agent reaches back into Home Assistant to actually control the home. Researched
against current HA docs + `home-assistant/core` source (July 2026). Items to confirm against **your**
HA instance/version are marked **VERIFY**.

---

## 1. The picture

HA ships a built-in **`mcp_server`** integration that exposes HA's **Assist LLM API** (the same tools
the built-in voice agent uses — intents like `HassTurnOn`/`HassLightSet`, plus the entities the user
has *exposed to Assist*) as an **MCP server**. Your agent is an **MCP client**: connect → list tools →
call tools when the LLM asks.

**Safety boundary:** the tool/entity surface is exactly what the user has *exposed to Assist* in HA.
Nothing else is reachable. Document this for the owner.

---

## 2. Endpoint & transport

From `home-assistant/core` `components/mcp_server/http.py`, `mcp_server` registers these views (all
subclass `HomeAssistantView` → require HA auth):

| Path | Transport | Notes |
|---|---|---|
| **`/api/mcp`** | **Streamable HTTP** (current, preferred) | Serves the configured LLM API; the **Assist API does not require admin**. |
| `/api/mcp/{api_id}` | Streamable HTTP | A specific LLM API by id; admin required **except** the Assist API. |
| `/mcp_server/sse` | SSE (legacy) | Long-running session stream. |
| `/mcp_server/messages/{session_id}` | SSE (legacy) | Client POSTs MCP messages here. |

**Use `https://<ha-host>/api/mcp` with the Streamable HTTP transport.** The legacy `/mcp_server/sse`
pair still works if you prefer the SSE client. **VERIFY** the path on your version (older builds
exposed only the SSE endpoints).

---

## 3. Authentication

`HomeAssistantView` requires a standard HA token. Two options:

- **Long-lived access token (recommended for a service)** — create one in HA (User Profile →
  Security → Long-lived access tokens) and send it as `Authorization: Bearer <token>` on the MCP
  connection. Simple, no OAuth dance. This is your `HA_TOKEN` (a powerful secret — see brief §8).
- **OAuth / application credentials** — HA fully supports MCP OAuth (what Claude.ai uses). More setup;
  unnecessary for a self-hosted service. Skip unless you need per-user tokens.

**VERIFY:** that your MCP client actually forwards the `Authorization` header on the Streamable-HTTP
connection (see §5) — some client/transport versions need the header passed explicitly.

---

## 4. User-side HA setup (one-time, document for the owner)

1. **Add the integration:** Settings → Devices & Services → Add Integration → **Model Context Protocol
   Server** (`mcp_server`).
2. **Expose entities to Assist:** Settings → Voice assistants → Expose — only exposed entities are
   controllable. This is the control surface.
3. **Create a long-lived token** (User Profile → Security) → set it as the agent's `HA_TOKEN`.
4. Give the agent `HA_BASE_URL` (e.g. `https://homeassistant.local:8123`); derive `HA_MCP_URL =
   HA_BASE_URL + "/api/mcp"`.

---

## 5. Python MCP client usage

Deps: `mcp` (the official `modelcontextprotocol/python-sdk`). Streamable-HTTP client shape (confirmed
against the SDK):

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HA_MCP_URL = f"{settings.ha_base_url}/api/mcp"
HEADERS = {"Authorization": f"Bearer {settings.ha_token}"}

async def open_ha_session():
    # streamablehttp_client yields (read, write, get_session_id)
    async with streamablehttp_client(HA_MCP_URL, headers=HEADERS) as (read, write, _get_sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()          # tools.tools: list[Tool]
            # ... run your agent loop, calling session.call_tool(...) as the LLM requests
            result = await session.call_tool(
                "HassTurnOff", arguments={"name": "kitchen lights"}
            )
            # result.content: list of content blocks (text/JSON); result.isError: bool
```

- **VERIFY** the `headers=` kwarg name on your installed `mcp` version (the SDK accepts custom headers
  for streamable-http; the exact signature has shifted across versions). If it's not supported, the
  legacy SSE client `from mcp.client.sse import sse_client` also takes a URL + headers.
- **Session lifetime:** keep one `ClientSession` open per turn (or pooled) — `initialize()` +
  `list_tools()` is cheap but not free; don't reconnect per tool call. Tool lists can be cached and
  refreshed periodically (**VERIFY** whether HA emits `tools/list_changed`).
- `list_tools()` returns MCP `Tool` objects: `name: str`, `description: str | None`,
  `inputSchema: dict` (a JSON Schema for the arguments).

---

## 6. Mapping MCP tools → your LLM's tool schema (near 1:1)

An MCP `Tool` maps directly onto both providers' tool formats. Build these once per turn from
`list_tools()`:

**OpenAI-compatible (Chat Completions `tools`):**
```python
openai_tool = {
    "type": "function",
    "function": {
        "name": t.name,
        "description": t.description or "",
        "parameters": t.inputSchema,        # JSON Schema, as-is
    },
}
```

**Anthropic (Messages `tools`):**
```python
anthropic_tool = {
    "name": t.name,
    "description": t.description or "",
    "input_schema": t.inputSchema,          # JSON Schema, as-is
}
```

When the LLM returns a tool call `{name, arguments}`, forward it verbatim to
`session.call_tool(name, arguments=args)`, then feed `result.content` back into the model as the
tool result. Notes:
- Some small/local models emit `arguments` as a *stringified* JSON blob — parse/repair before calling
  (see `llm-providers.md` §5).
- MCP `inputSchema` is standard JSON Schema; OpenAI "strict" mode may want `additionalProperties:
  false` + all-required — apply per backend if you enable strict structured tools.

---

## 7. Fallback control channel (if MCP is awkward)

If the MCP path is blocked on your version, HA's plain HTTP API works with the same `HA_TOKEN`
(`Authorization: Bearer`):
- **Call a service:** `POST /api/services/<domain>/<service>` with a JSON body (e.g.
  `POST /api/services/light/turn_off` `{"entity_id": "light.kitchen"}`).
- **Read state:** `GET /api/states` / `GET /api/states/<entity_id>`.
- **WebSocket API** (`/api/websocket`) for streaming state + service calls.

Trade-off: you lose the Assist tool schemas/prompts (you'd hand-build tool definitions and enforce
the exposure boundary yourself), so prefer MCP unless it's genuinely unavailable.

---

## 8. VERIFY checklist (do this pass against your HA 2026.7 before trusting the above)

1. The `/api/mcp` Streamable-HTTP endpoint is present and serves the **Assist** API without admin on
   your version (vs. only legacy `/mcp_server/sse`).
2. A long-lived token as `Authorization: Bearer` authenticates the MCP connection (vs. OAuth
   required).
3. Your `mcp` SDK version's `streamablehttp_client` accepts the auth `headers` kwarg as shown.
4. `list_tools()` returns the expected Assist tools for your exposed entities, with usable
   `inputSchema`s.
5. A round-trip `call_tool()` actually actuates a device (e.g. toggles a test light).

---

## Sources
- HA `mcp_server` (user docs) — https://www.home-assistant.io/integrations/mcp_server/
- HA `mcp` client (user docs) — https://www.home-assistant.io/integrations/mcp/
- `home-assistant/core` `components/mcp_server/http.py` (endpoint paths + auth) —
  https://github.com/home-assistant/core/blob/dev/homeassistant/components/mcp_server/http.py
- MCP Python SDK — https://github.com/modelcontextprotocol/python-sdk
- MCP client guide — https://modelcontextprotocol.io/docs/develop/build-client
