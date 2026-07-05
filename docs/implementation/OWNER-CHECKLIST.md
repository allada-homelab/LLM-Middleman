# Owner checklist — the last steps to close out v1

Everything agent-runnable is done and merged: v1 implemented (PRs #2–#20), live
dress-rehearsal E2E passed with evidence ([`e2e-results/`](e2e-results/), PR #24), the
three bugs it found fixed (#21–#23), **v1.0.0 released**, brands PR
[home-assistant/brands#10693](https://github.com/home-assistant/brands/pull/10693) open
with green CI. What remains needs your infrastructure or your hands. Each item below
says exactly what to do, what evidence to capture, and which ticket box it closes.

When all three checks pass, tell the agent — it flips LLMM-018 and LLMM-019 to `done`
in a final PR (19/19) and records your evidence.

---

## 1. Install released v1.0.0 via HACS on your live HA (~5 min)

Closes: LLMM-019's last acceptance box + LLMM-018's install row.

1. On your live HA: **HACS → ⋮ (top right) → Custom repositories** → add
   `https://github.com/allada-homelab/LLM-Middleman`, category **Integration**.
   (If HACS itself isn't set up yet, its config flow asks for a GitHub device-code —
   the one interactive step nothing headless can do.)
2. Find **LLM Middleman** in HACS → **Download** — confirm it offers **v1.0.0** (the
   release, not `main`). Install, then **restart HA**.
3. **Settings → Devices & Services → Add integration → LLM Middleman.** You should get
   the backend-type dropdown (5 presets). Create any entry — your llama.cpp proxy as
   `OpenAI-compatible` is the natural first one: base URL = server root (a trailing
   `/v1` is stripped automatically), any non-empty API key if your proxy ignores auth.
4. Add one conversation subentry (agent name + prompt; pick a model from the dropdown).
   The conversation entity must appear **immediately** — no reload (that was BUG-1;
   the fix is in v1.0.0).
5. In **Assist chat**, pick the new agent and send one message; watch the reply stream.

**Evidence to capture:** the HACS version string shown (v1.0.0), and one Assist
exchange (screenshot or copied text).

Rollback if anything misbehaves: remove the integration entry + uninstall in HACS;
nothing else on your HA is touched.

## 2. Tool-call verification with a capable model (~10 min)

Closes: LLMM-018's two tool rows (openai_compat + ollama). The rehearsal proved the
adapters send tools correctly; the tiny local test models just never called them — this
retest uses a model that will.

**Option A — hand me the keys (preferred, I drive):** create `/home/vscode/.llmm-e2e.env`
in the devcontainer:

```bash
OPENAI_COMPAT_BASE_URL=http://<your-llamacpp-or-proxy-host>:<port>
OPENAI_COMPAT_API_KEY=<key, or any dummy if unauthenticated>
OLLAMA_BASE_URL=http://<your-ollama-host>:11434   # optional, if you run one
```

then say **"run Tier 1"** — I re-run the tool rows in a throwaway HA against your
endpoints (recipe: [`../../scripts/e2e/README.md`](../../scripts/e2e/README.md)) and
record the evidence. Your live HA is not touched.

**Option B — do it yourself on the live HA:** on the entry from step 1's subentry,
enable **Control Home Assistant** (the `llm_hass_api` multi-select → Assist). Create a
test helper (**Settings → Devices & Services → Helpers → Toggle**, name it e.g.
`E2E Test`). In Assist, tell the agent **"turn on the E2E test"** — the toggle must
flip to on. Repeat once via an Ollama-type entry if you run ollama with a
tool-capable model (qwen3:4b+, llama3.1:8b, etc.).

**Evidence:** the helper's state changing (screenshot/logbook line) per preset tested.

## 3. Voice-hardware checks (~2 min at a satellite)

Closes: LLMM-018's owner-run voice rows. Set your voice pipeline's conversation agent
to a v1 agent first (Settings → Voice assistants).

1. **Streaming TTS / time-to-first-audio:** ask something long — *"tell me a short
   story about this house."* PASS = speech starts almost immediately (~0.5 s feel)
   while the model is clearly still generating, not after a long silence.
2. **Follow-up listening (mic stays open):** say *"ask me a trivia question."* The
   reply ends in a question mark, so the satellite should **re-open the mic without
   the wake word** (listening chime/LED). Answer it; the agent should respond in the
   same conversation context.

**Evidence:** just your observation per check ("audio started ~instantly", "mic
reopened: yes/no").

---

## After the three checks

Tell the agent the results (pass/fail + the evidence). It will:
- append your rows to [`e2e-results/MATRIX.md`](e2e-results/MATRIX.md),
- flip LLMM-018 and LLMM-019 to `done` in a final PR — the v1 program closes at 19/19.

## What's left beyond your checklist (no action needed)

- **Brands PR** [#10693](https://github.com/home-assistant/brands/pull/10693): waiting
  on Home Assistant maintainer review (their queue; CI already green). Until it merges,
  the integration simply shows a placeholder icon.
- **Dependabot's first run:** glance at the repo's Insights → Dependency graph →
  Dependabot tab once to confirm the new config was accepted.
- **Fast-follow roadmap** (build when you want them, one ticket each — see
  [`README.md`](README.md)): AI Task `generate_data`, token stats via
  `chat_log.async_trace`, external tool-activity surfacing, AG-UI / Dify / Anthropic
  presets.
- **Weekly CI** now runs Mondays ~03:00 UTC — a red badge with no recent commits means
  an HA update broke something; the E2E rig in `scripts/e2e/` is the reproduction tool.
