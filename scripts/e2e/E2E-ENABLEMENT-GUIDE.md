# Enabling agent-run E2E for LLM-Middleman (LLMM-018)

What you need to provide, tiered by effort. Everything not listed here I can do myself
from the devcontainer (verified: Docker works against your host daemon; the repo is
public, so HACS custom-repo installs need no token).

## Tier 0 — costs you one "yes"

**Say "go ahead with the disposable-HA dress rehearsal."** With that authorization I can,
with zero further input:

- Spin up a throwaway **Home Assistant 2026.7 container** with the integration mounted,
  drive config flows and Assist turns through its REST/WebSocket API, and run the full
  matrix rows that need no external infra:
  - **converse** — against my pre-validated SSE stub (streaming, continue_conversation,
    error→fallback, backend-down).
  - **langgraph** — against a local `langgraph dev` sample graph (MessagesState echo
    graph; needs no LLM key). This is the row most likely to catch a real bug — the
    `messages-tuple` frame shape is the plan's least-confident item.
  - **ollama** — I run `ollama` in Docker and pull a ~1 GB small model (qwen3:0.6b or
    similar) for real streaming + native tool calls. Costs a few GB disk + CPU inference
    on your host; tell me if that's not okay and I'll use your existing ollama instead
    (Tier 1).
  - **HACS dress rehearsal** — install HACS in the throwaway HA and install the
    integration as a custom repo, proving the consumer path end-to-end (the final check
    on your *live* HA stays separate).
  - Everything torn down afterward (containers + volumes removed; stub deleted).

## Tier 1 — five minutes of your time, unlocks the rest

Put the values below in **`/home/vscode/.llmm-e2e.env`** (outside the repo — never
committed; I'll `chmod 600` it and redact values from all output):

```bash
# Your live HA (unlocks live-instance rows + the real HACS install check)
HA_BASE_URL=http://<ha-host>:8123
HA_TOKEN=<long-lived access token>   # HA: your Profile → Security → Long-lived access tokens → create

# Your OpenAI-compatible endpoint (llama.cpp proxy / LiteLLM / etc.)
OPENAI_COMPAT_BASE_URL=http://<host>:<port>
OPENAI_COMPAT_API_KEY=<key or any dummy string if unauthenticated>

# Optional: existing ollama (skips the Tier-0 docker one)
OLLAMA_BASE_URL=http://<host>:11434

# Optional: existing n8n (else I self-host one in docker)
N8N_BASE_URL=http://<host>:5678
N8N_USER=<email>          # owner login, only if you have one running
N8N_PASSWORD=<password>
```

Plus two one-time facts (just tell me in chat):
1. **Network**: can this devcontainer reach your homelab LAN? (I'll verify with a curl to
   each URL and report; if the container is isolated, run the containers' host-network
   variant or give me reachable URLs.)
2. **n8n AI Agent credential**: the streaming row needs a Chat Trigger → AI Agent
   workflow; the AI Agent node needs a model credential. Easiest: I point it at your
   OpenAI-compatible endpoint above. Confirm that's acceptable.

With Tier 1 I can run **every matrix row except voice hardware**, including:
- openai_compat + ollama tool calls executing a real HA intent (I'll create a
  `input_boolean` test entity in the throwaway HA to flip — no touching your live
  devices unless you explicitly want the live-HA tool test).
- n8n both modes (stream-enabled and deliberately-not, proving the blocking-body
  detection) with `sessionId` continuity.
- The v0→v1 migration check (I seed a version-1 entry in the throwaway HA).

## Tier 2 — stays yours no matter what

1. **Voice hardware rows** — wake word, mic-open follow-up (`continue_conversation`) and
   the ~0.5 s time-to-first-audio feel on a real satellite. I'll tell you exactly which
   two utterances to speak and what to watch for; you report back, I record it as
   "owner-run" evidence.
2. **Live-HA sanity** — if you'd rather I *not* touch your live instance even with a
   token, you run the HACS install + one Assist turn there yourself (5 min with the
   results template).
3. **LLMM-019 sign-offs** (unchanged): release version string, the
   `home-assistant/brands` PR, and the GitHub release/tag.

## Safety rails I'll follow (so you know the blast radius)

- Live-HA access is **read + integration-scoped**: I create/configure only
  `llm_middleman` config entries and test helpers, never touch existing
  entries/automations/devices; every entity I create gets deleted; the token is yours to
  revoke the minute we're done.
- Secrets live only in `~/.llmm-e2e.env`, never in the repo, logs, or PR bodies
  (diagnostics redaction gets tested with a fake key, not your real one).
- All throwaway containers are named `llmm-e2e-*` so you can `docker rm -f` them anytime;
  I remove them + their volumes at the end and paste the teardown evidence.
- Results land in the matrix under `docs/implementation/e2e-results/` (see `MATRIX.md`);
  the v1.0.0 dress-rehearsal rows are already there, and a re-run appends/updates them.

## TL;DR of what to actually do

1. Reply "go" for the Tier-0 dress rehearsal (optionally: "no local model pull").
2. Create `/home/vscode/.llmm-e2e.env` with `HA_BASE_URL`, `HA_TOKEN`,
   `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY` (+ optional ollama/n8n lines).
3. Tell me: LAN reachable? n8n may use your OpenAI-compatible endpoint?
4. Keep the voice satellite handy for two spoken checks at the end.
