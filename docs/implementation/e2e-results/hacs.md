# LLMM-018 E2E — HACS / packaging consumer-path rehearsal

**Row:** HACS delivery + fresh-consumer install of the *published* artifact
**Date:** 2026-07-05
**Published source:** `github.com/allada-homelab/LLM-Middleman`, branch `main`
**Local checkout HEAD (cross-ref):** `cdecb35` (`git rev-parse HEAD` in `/workspaces/LLM-Middleman`) — matches the published `main` this rehearsal targets.

## Result: PASS (every in-scope check)

The true HACS-frontend install (add custom repository → Download) is **out of scope** because
the real HACS config flow requires **GitHub Device Flow auth — interactive, ~60 s of David's
time**. This rehearsal proves everything short of that click: the repo, *as published on GitHub
right now*, has the exact layout HACS expects and installs + loads cleanly when delivered the way
HACS delivers it (download the ref zip → drop `custom_components/<domain>/` into `/config`).
**See "Owner-run remainder" below — batch the device-flow minute with the voice checks.**

## Per-check results

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Download published `main.zip` (urllib, curl-hook-safe) | PASS | 484 213 bytes, sha256 `170201de59abee02a34f1f7596e6a17ba6ca38e4946fc0183b031377c4fc133f` |
| 2 | `hacs.json` at repo root, valid JSON | PASS | keys `["homeassistant","name","render_readme"]` (see below) |
| 3 | `custom_components/llm_middleman/` present w/ `manifest.json` | PASS | dir name == manifest `domain` |
| 4 | `manifest.json` valid JSON + all HA-required keys | PASS | 0 missing of the 10 required keys |
| 5 | manifest `version` == `0.1.0` (pre-release) | PASS | recorded for LLMM-019 bump |
| 6 | Fresh consumer HA loads the **zip-extracted** integration | PASS | container `llmm-e2e-ha-hacs`, HA 2026.7.1 RUNNING |
| 7 | Config flow returns `backend_type` form (step `user`, 5 opts) | PASS | flow JSON below |
| 8 | No `llm_middleman` errors in logs (only "not tested" WARN) | PASS | log excerpt below |

## 1–5. Published-artifact layout & packaging (from the extracted zip)

HACS's delivery path was simulated exactly: `urllib.request.urlretrieve` of
`https://github.com/allada-homelab/LLM-Middleman/archive/refs/heads/main.zip`, then
`zipfile.extractall`. Top-level extract dir: `LLM-Middleman-main/`.

**`hacs.json`** (parsed OK — root of repo):
```json
{
    "name": "LLM Middleman",
    "render_readme": true,
    "homeassistant": "2025.8.0"
}
```
Keys present: `homeassistant`, `name`, `render_readme`. No `content_in_root` / `domains` /
`country` / `zip_release` keys — **correct**: none are required for a standard-layout,
single-integration repo (HACS auto-discovers the sole `custom_components/<domain>/`).

**Layout** (matches HACS's integration expectation exactly):
```
LLM-Middleman-main/
├── hacs.json                         # root ✓
└── custom_components/
    └── llm_middleman/                # dir name == manifest domain ✓
        ├── manifest.json             # ✓
        ├── __init__.py  const.py  config_flow.py  conversation.py
        ├── diagnostics.py  strings.json
        ├── backends/  brand/  translations/
```

**`manifest.json`** (parsed OK):
```json
{
    "domain": "llm_middleman",
    "name": "LLM Middleman",
    "codeowners": ["@allada-homelab"],
    "config_flow": true,
    "dependencies": ["conversation"],
    "documentation": "https://github.com/allada-homelab/LLM-Middleman",
    "integration_type": "service",
    "iot_class": "local_polling",
    "issue_tracker": "https://github.com/allada-homelab/LLM-Middleman/issues",
    "version": "0.1.0"
}
```
All 10 HA-required keys present (`domain, name, codeowners, config_flow, dependencies,
documentation, integration_type, iot_class, issue_tracker, version`) — **0 missing**.

### Version sanity (for LLMM-019)
`manifest.json` `"version": "0.1.0"` — the expected **pre-release** placeholder.
**LLMM-019 must bump this** before cutting the GitHub release/tag; HACS reads this field as the
installed version. (`hacs.json` `homeassistant: "2025.8.0"` is the minimum-HA floor, unrelated
to the integration version.)

## 6–8. Fresh-consumer install of the published artifact

Deliberately installed the **zip-extracted** `custom_components/llm_middleman`, **NOT** the local
`/workspaces/LLM-Middleman` checkout — the whole point is the published artifact.

- **Container:** `docker run -d --name llmm-e2e-ha-hacs -v llmm-e2e-ha-hacs-config:/config homeassistant/home-assistant:2026.7`
  (image digest `sha256:f73512ba4fe06bb4d57636fe3578d0820cdec46f81e8f837ab59e451662ff3cb`), bridge IP `172.17.0.7`.
- **Install:** `docker cp <extracted>/custom_components/llm_middleman llmm-e2e-ha-hacs:/config/custom_components/llm_middleman`, then `docker restart`. Manifest verified *inside* the container to confirm it is the zip copy (version `0.1.0`).
- **Onboarding:** headless via REST (same recipe as STATE.md for the main container) — `POST /api/onboarding/users` → `auth_code` → `POST /auth/token` (`grant_type=authorization_code`) → `POST /api/onboarding/core_config` → `POST /api/onboarding/analytics`. Final `GET /api/onboarding`: `user/core_config/analytics` all `done:true` (`integration` step intentionally left undone — not required, per STATE.md). HA version reported `2026.7.1`, state `RUNNING`.

**Config-flow load proof** — `POST /api/config/config_entries/flow` body `{"handler":"llm_middleman","show_advanced_options":false}` → **HTTP 200**:
```json
{
  "type": "form",
  "flow_id": "01KWR0066EBDEPQDSK8VW8WNKM",
  "handler": "llm_middleman",
  "step_id": "user",
  "data_schema": [
    {
      "name": "backend_type",
      "required": true,
      "selector": {"select": {
        "options": ["converse", "langgraph", "n8n", "ollama", "openai_compat"],
        "translation_key": "backend_type", "mode": "dropdown",
        "custom_value": false, "multiple": false, "sort": false
      }}
    }
  ],
  "errors": null, "last_step": null
}
```
Assertions: `type == "form"` ✓ · `step_id == "user"` ✓ · `backend_type` options ==
`["converse","langgraph","n8n","ollama","openai_compat"]` (5) ✓. This proves the manifest parsed,
the integration + all imports (`config_flow.py`, `const.py`, `backends/`) loaded, and the parent
config flow is reachable from a clean consumer install of the published zip. Test flow aborted
afterward (`DELETE .../flow/<flow_id>` → `{"message":"Flow aborted"}`) — no lingering entry.

> A converse **parent entry pointing at a dummy URL is intentionally NOT attempted** — entry
> creation runs `async_validate_connection`, which probes the backend and fails against a dead
> URL. Proving the *form loads* is the correct load-check for this row; full entry-creation +
> turn rows are covered by the per-preset agents against real backends.

**Log check** — `docker logs llmm-e2e-ha-hacs 2>&1 | grep -iE 'llm_middleman|error|traceback|exception'` returned exactly one line:
```
2026-07-05 01:58:23.208 WARNING (SyncWorker_0) [homeassistant.loader] We found a custom
integration llm_middleman which has not been tested by Home Assistant. This component might
cause stability problems, be sure to disable it if you experience issues with Home Assistant
```
This "custom integration ... has not been tested" WARNING is **expected for every custom
component** — not a defect. **No `llm_middleman` ERROR, no traceback, no exception.**

## Owner-run remainder (batch with voice checks)

The genuine end-to-end HACS-frontend install still needs **David's ~60 s of GitHub Device Flow**:
in the live/throwaway HA → HACS → three-dot menu → *Custom repositories* → add
`https://github.com/allada-homelab/LLM-Middleman` (category: Integration) → *Download* → restart.
Everything downstream of that click is already proven above (identical layout, identical artifact,
clean load). Recommend batching this minute with the Tier-2 voice-hardware rows so David does one
interactive session, not two.

## Teardown (performed by this agent — these resources are ours to remove)

```
docker rm -f llmm-e2e-ha-hacs          # done
docker volume rm llmm-e2e-ha-hacs-config   # done
```
The main `llmm-e2e-ha` container + `llmm-e2e-ha-config` volume were **NOT** touched (not ours).
Scratch zip/extract under the session scratchpad only; nothing written to the repo.
