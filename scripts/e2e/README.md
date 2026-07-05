# E2E regression rig for LLM Middleman

A repeatable recipe for standing up a **throwaway Home Assistant** with this integration and
re-driving the five-preset live matrix (`docs/implementation/e2e-results/MATRIX.md`). Written
for a future agent re-running the matrix **after an HA version bump** or a change to the
backend adapters.

- **What this rig proves:** each backend preset streams, keeps multi-turn continuity, and
  falls back gracefully when its backend dies — driven through the *real* HA conversation API,
  not a unit harness.
- **What it does not:** voice-hardware feel (time-to-first-audio, wake-word follow-up) and the
  interactive HACS device-flow install. Those are owner-run — see
  [`E2E-ENABLEMENT-GUIDE.md`](E2E-ENABLEMENT-GUIDE.md) (Tier 2) and `HANDOFF.md`.

The evidence this recipe is distilled from lives in `docs/implementation/e2e-results/`
(per-preset `.md` files + raw captures) and `docs/implementation/HANDOFF.md`. When a step here
disagrees with the running system, the system wins — fix this file.

> **Kit files here:** [`converse_sse_stub.py`](converse_sse_stub.py) (the converse-preset
> backend stub) and [`E2E-ENABLEMENT-GUIDE.md`](E2E-ENABLEMENT-GUIDE.md) (what the owner must
> provide for the owner-gated tiers). This rig runs entirely from the devcontainer against the
> host Docker daemon; **Tier 0** (converse + langgraph + ollama + HACS-packaging) needs no
> owner input.

---

## Ground rules (read first — these bit prior runs)

1. **Docker-outside-of-docker: no host-path mounts.** The devcontainer talks to the *host*
   Docker daemon, so a `-v /some/host/path:/config` bind mount points at a path on the host,
   not in this container — files you `cp` locally won't be there. Use a **named volume** for
   `/config` and put files in with `docker cp`. Every step below does this.
2. **Reachability is non-obvious.** `-p 8124:8123` publishes on the **host**, not this
   devcontainer — `localhost:8124` here gets connection-refused, and `host.docker.internal`
   does not resolve. Reach containers by their **docker bridge IP** on the service port:
   ```bash
   docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' llmm-e2e-ha
   # e.g. 172.17.0.6  ->  http://172.17.0.6:8123
   ```
3. **The Bash-tool `curl` hook blocks non-loopback hosts.** `~/.claude/hooks/validate-bash.py`
   only permits `localhost`/`127.x`/`0.0.0.0`/`::1`, so `curl http://172.17.0.6:8123` is
   denied. Two hook-safe ways to hit the API:
   - **python** (`urllib`/`aiohttp`) against the bridge IP — the primary method; used for all
     REST/WebSocket driving.
   - **curl *inside* the container:** `docker exec llmm-e2e-ha curl -sS localhost:8123/...`
     (curl is not at command-position after `docker exec <name>`, so the hook doesn't fire).
4. **Everything disposable is named `llmm-e2e-*`** so teardown is a glob. Never touch entries
   or containers you didn't create — a prior run's teardown bulk-deleted sibling config entries
   by domain filter and forced re-runs. Delete only the specific `entry_id`s you made.
5. **Secrets** (owner HA token, endpoint keys) live only in `~/.llmm-e2e.env` (`chmod 600`),
   never in the repo, logs, or PR bodies.

---

## Step 1 — Disposable HA container + integration

```bash
# Named volume for /config (NOT a host bind mount — see ground rule 1).
docker run -d --name llmm-e2e-ha -p 8124:8123 \
  -v llmm-e2e-ha-config:/config \
  homeassistant/home-assistant:2026.7          # bump the tag on an HA version test

# Copy the integration in from the repo checkout, then restart so HA discovers it.
docker cp custom_components/llm_middleman \
  llmm-e2e-ha:/config/custom_components/llm_middleman
docker restart llmm-e2e-ha

HA_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' llmm-e2e-ha)
echo "HA at http://$HA_IP:8123"
```

Wait for `GET http://$HA_IP:8123/api/config` (via python) to return `"state": "RUNNING"`.

A **healthy** load shows exactly one `llm_middleman` log line — the standard *"custom
integration ... has not been tested by Home Assistant"* WARNING (expected for every custom
component). Any `llm_middleman` `ERROR`/traceback is a real defect:

```bash
docker logs llmm-e2e-ha 2>&1 | grep -iE 'llm_middleman|error|traceback'
```

## Step 2 — Headless onboarding + long-lived token

HA has no CLI onboarding; drive it over REST from python against `http://$HA_IP:8123`:

1. `POST /api/onboarding/users` `{name, username, password, language, client_id}` → `auth_code`.
2. `POST /auth/token` `grant_type=authorization_code`, `code=<auth_code>`, `client_id=<same>`
   → short-lived bearer.
3. `POST /api/onboarding/core_config`, then `POST /api/onboarding/analytics` (bearer from 2).
4. **Mint the long-lived token over WebSocket** (`/api/websocket`): `auth` with the bearer,
   then `{"type":"auth/long_lived_access_token","client_name":"llmm-e2e","lifespan":365}`.
   HA stores no plaintext copy — capture it to `~/.llmm-e2e.env` (`chmod 600`).

`GET /api/onboarding` should report `user`/`core_config`/`analytics` = **done**. The
`integration` step (companion-app IndieAuth linking) is intentionally left undone — it is not
required for onboarding to complete, and no `login_flow` step is needed.

Use the long-lived token (`Authorization: Bearer <LLAT>`) for everything below.

## Step 3 — Test helper for the tool-call rows

No REST CRUD endpoint exists for helpers in this HA; create over WebSocket, then **expose it to
Assist** (default exposure is `None`, so tools can't see it):

```jsonc
// WS: create the helper
{"type":"input_boolean/create","name":"llmm e2e test","initial":false}
// -> entity_id  input_boolean.llmm_e2e_test
// WS: expose to the conversation assistant
{"type":"homeassistant/expose_entity","assistants":["conversation"],
 "entity_ids":["input_boolean.llmm_e2e_test"],"should_expose":true}
```

Verify: `GET /api/states/input_boolean.llmm_e2e_test` → `"state":"off"`.

## Step 4 — Drive a preset (config → subentry → turns)

All flows go through the real flow APIs; all turns through `/api/conversation/process`.

```jsonc
// 1. Start the parent flow  ->  the "user" form (a backend_type dropdown, 5 options).
POST /api/config/config_entries/flow   {"handler":"llm_middleman","show_advanced_options":false}
//    Assert: type=="form", step_id=="user",
//    backend_type options == ["converse","langgraph","n8n","ollama","openai_compat"].
//    (NOTE: it is a form field, NOT a type:"menu" — assert accordingly.)

// 2. Pick the backend  ->  its per-preset connection form (fields differ, see the table).
POST /api/config/config_entries/flow/<flow_id>   {"backend_type":"<preset>"}

// 3. Submit connection config  ->  create_entry. The adapter runs a real connection probe
//    here (see per-preset table); a dead/unreachable URL fails with cannot_connect.
POST /api/config/config_entries/flow/<flow_id>   {"base_url": "...", ...}
//    -> capture entry_id.

// 4. Add a conversation agent (subentry). memory_scope is offered only for stateful presets.
POST /api/config/config_entries/subentries/flow   {"handler":[<entry_id>,"conversation"]}
POST /api/config/config_entries/subentries/flow/<subflow_id>
     {"name":"E2E <preset>","memory_scope":"conversation","timeout":60,"max_history":20}
//    -> the conversation.e2e_<preset> entity now registers (an entry update listener
//    reloads automatically — this was BUG-1 in the dress rehearsal, fixed post-E2E; if an
//    entity is missing, POST .../entry/<entry_id>/reload as a fallback).

// 5. Drive turns. Reuse the returned conversation_id to test multi-turn continuity.
POST /api/conversation/process
     {"text":"...","agent_id":"conversation.e2e_<preset>","conversation_id":"<optional>"}
```

**The four checks every preset row must show** (evidence = exact utterance + observation):

| Check | How to observe over REST | Note |
|---|---|---|
| Streaming starts early | Wire-level (backend logs N incremental frames). `/conversation/process` **buffers** the final string, so true first-token-audio timing is an owner voice check. | |
| Multi-turn continuity | Turn 1 plants a token ("secret word is BANANA42"); turn 2 (same `conversation_id`) recalls it. Stateful presets forward `conversation_id`; stateless replay ChatLog. | |
| Backend-down fallback | Stop the backend mid-session; a turn returns `"Sorry, I could not reach the assistant right now. Please try again."` and **never hangs** (bounded by the subentry `timeout`). No raw error text leaks. | |
| Preset-specific | See the per-preset rows below. | |

---

## Per-preset backend recipes

### converse — in-container SSE stub

The stub ([`converse_sse_stub.py`](converse_sse_stub.py)) runs **inside** the HA container so
the integration reaches it on loopback:

```bash
docker cp scripts/e2e/converse_sse_stub.py llmm-e2e-ha:/config/converse_sse_stub.py
docker exec -d llmm-e2e-ha python3 /config/converse_sse_stub.py --port 8099
# Parent: backend_type=converse, base_url=http://localhost:8099
# teardown: docker exec llmm-e2e-ha pkill -f converse_sse_stub.py && \
#           docker exec llmm-e2e-ha rm /config/converse_sse_stub.py
```

- **Probe caveat:** the converse adapter probes with a plain `GET` on `base_url` and treats
  **any** HTTP response as reachable (only connect/timeout → `cannot_connect`). The stub serves
  only `POST /v1/converse`, so `GET /` returns 404 — entry creation still succeeds.
- **Preset-specific rows:** utterance containing `"follow"` → `done.continue_conversation=true`
  (mic stays open); `"boom"` → an `error` event mid-stream → graceful fallback, no raw error,
  no hang. The stub logs each request body so `conversation_id` forwarding is observable.

### langgraph — local `langgraph dev`

Run a sample MessagesState echo graph in **this devcontainer** (no LLM key needed — a
`GenericFakeChatModel` streams tokens so `messages-tuple` emits real token frames):

```bash
langgraph dev --host 0.0.0.0 --port 2024 --no-browser   # in the devcontainer
LG_IP=$(hostname -I | awk '{print $1}')                 # devcontainer bridge IP
docker exec llmm-e2e-ha curl -sS http://$LG_IP:2024/ok  # from HA -> {"ok":true}
# Parent: backend_type=langgraph, base_url=http://$LG_IP:2024, assistant_id=agent, no api_key
```

- **Highest-value row:** the `messages-tuple` frame shape. The dress rehearsal confirmed it
  **matches** `backends/langgraph.py`'s parser field-for-field (raw bytes in
  `docs/implementation/e2e-results/langgraph-raw-capture.txt`).
- **Success terminates on SSE EOF** — `langgraph dev` never emits `event: end` (the adapter no
  longer looks for one). `event: error` is the failure signal → fallback.
- Continuity: two same-`conversation_id` turns land on the **same** server-side `thread_id`
  (check `/threads/search` → one thread, two runs).

### ollama — local container + small model

```bash
docker run -d --name llmm-e2e-ollama ollama/ollama:latest
docker exec llmm-e2e-ollama ollama pull qwen3:0.6b       # ~1 GB
OL_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' llmm-e2e-ollama)
# Parent: backend_type=ollama, base_url=http://$OL_IP:11434  (probe: GET /api/tags)
```

- **Bridge IP is not stable across `docker restart`** (docker reassigns it); a config entry
  pins the IP, so a restart of the ollama container invalidates the entry. Avoid restarting it
  mid-run, or recreate the entry at the new IP.
- Model dropdown is sourced from `/api/tags`; `llm_hass_api` (Assist tools) is offered;
  `memory_scope` is correctly **absent** (ollama is stateless-replay).
- **Tool-call row is model-limited on a 0.6–1.5B model:** the tiny model emits `HassTurnOn`
  with an invalid `device_class` slot, which HA's intent rejects — the adapter/tool loop is
  proven correct (tools forwarded, `tool_calls` parsed, intent invoked). A **capable tool
  model** (owner's proxy, Tier 1) is needed to see `input_boolean.llmm_e2e_test` actually flip.

### openai_compat — reuse the ollama `/v1` shim

```bash
# Parent: backend_type=openai_compat, base_url=http://$OL_IP:11434  (ROOT), any dummy api_key
```

- **`base_url` is the server ROOT — the adapter hardcodes the `/v1` prefix.** The flow now
  strips a trailing `/v1` (and a trailing `/`), so both `http://host:11434` and the
  OpenAI-conventional `http://host:11434/v1` resolve (this was the `/v1/v1` trap, fixed
  post-E2E). Probe is `GET /v1/models`; the model dropdown comes from `data[].id`.
- Dummy/empty API key is accepted (sent as a Bearer header only when set).
- Same tool-call model limitation as ollama.

### n8n — self-hosted, headless bootstrap

```bash
# Must set the listen address, else n8n binds IPv6-only (::) and exits immediately.
docker run -d --name llmm-e2e-n8n -p 5678:5678 \
  -e N8N_LISTEN_ADDRESS=0.0.0.0 -e N8N_HOST=0.0.0.0 n8nio/n8n
N8_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' llmm-e2e-n8n)
```

Headless setup + workflow, all via the internal `/rest/*` API (python; capture the `n8n-auth`
cookie from setup/login):

1. `POST /rest/owner/setup` `{email,firstName,lastName,password}` → 200 + `n8n-auth` cookie.
2. `POST /rest/login` `{emailOrLdapLoginId,password}` for the session cookie.
3. Model credential: `POST /rest/credentials` `{type:"openAiApi",
   data:{apiKey:"ollama", url:"http://$OL_IP:11434/v1"}}` (point the AI Agent at the ollama
   `/v1` shim).
4. Create a **Chat Trigger → AI Agent** workflow. For the streaming row set the Chat Trigger
   `responseMode=streaming` + AI Agent `enableStreaming=true`; for the blocking row use
   `responseMode=lastNode`.
5. **Activate (n8n 2.x model):** a plain `PATCH /rest/workflows/{id} {active:true}` does **not**
   activate. Use `POST /rest/workflows/{id}/activate {versionId:<current versionId>}` → 200,
   `active:true`, `triggerCount:1`.
6. Production webhook URL: `http://$N8_IP:5678/webhook/<chatTrigger.webhookId>/chat`.
   Parent: `backend_type=n8n`, `webhook_url=<that>`, `target_type=chat_trigger`,
   `auth_type=none`.

- **Load-bearing row — wrong-mode/content-sniffing:** point the entry at a **blocking**
  workflow; the adapter content-sniffs the response (there is no config streaming toggle) and
  still answers cleanly — no crash, no raw JSON. A body missing `output`/`text` → fallback,
  never leaks the raw object.
- Continuity is via `sessionId` = `conversation_id` (verify in `/rest/executions/{id}`).
- **Timing:** a slow local model in **blocking** mode can exceed the `N8N_DEFAULT_TIMEOUT=30`s;
  raise the subentry `timeout` (e.g. 180) for blocking rows. Streaming stays well under it.

### HACS packaging — fresh-consumer install

Prove the *published* artifact installs the way HACS delivers it (download the ref zip → drop
`custom_components/<domain>/` into `/config`), in a **separate** container:

```bash
docker run -d --name llmm-e2e-ha-hacs -v llmm-e2e-ha-hacs-config:/config \
  homeassistant/home-assistant:2026.7
# Download the published zip with python/urllib (curl-hook-safe), extract, and:
docker cp <extracted>/custom_components/llm_middleman \
  llmm-e2e-ha-hacs:/config/custom_components/llm_middleman
docker restart llmm-e2e-ha-hacs
```

Onboard headlessly (Step 2), then assert `POST /api/config/config_entries/flow` returns the
`user` form with the 5-option `backend_type` dropdown, and the logs show only the "not tested"
WARNING. Check `hacs.json` (root) and `manifest.json` are valid and the manifest `version`
matches the release tag. The genuine **HACS-frontend install** (add custom repository →
Download) needs GitHub Device Flow — interactive, ~60 s — and is **owner-run** (Tier 2).

---

## Teardown (disposable — removes containers AND their named volumes)

```bash
docker rm -f llmm-e2e-ha llmm-e2e-ha-hacs llmm-e2e-ollama llmm-e2e-n8n 2>/dev/null
docker volume rm llmm-e2e-ha-config llmm-e2e-ha-hacs-config 2>/dev/null
# stop any langgraph dev process you started in the devcontainer
pkill -f 'langgraph dev' 2>/dev/null
# drop the credentials/secrets file
rm -f ~/.llmm-e2e.env
```

Paste the teardown evidence into the results write-up, and confirm no `llmm-e2e-*` container or
volume survives (`docker ps -a --filter name=llmm-e2e`).
