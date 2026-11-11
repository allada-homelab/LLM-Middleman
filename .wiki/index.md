---
okf_version: "0.2"
---

# Gotcha

* [Converse streams ending without done were passed off as full replies](./converse-dropped-stream-looks-complete.md) - A converse stream ending without done now raises (was passed off as a full reply), and an empty done no longer speaks the untranslated fallback — both go through the entity guard like Dify.
* [Dify SSE events are dispatched on the JSON event field](./dify-sse-event-name-in-json.md) - Dify sends bare data frames with the event name inside the JSON, so dispatch on payload event; an Agent app's closing message repeats the answer and must not be spoken again.
* [HA 2026.9+ tool schemas are probatio, not voluptuous](./probatio-tool-schemas-ha-2026-9.md) - From HA 2026.9, llm tool schemas are probatio Schemas that voluptuous_openapi cannot convert, so tool schemas must go through backends/_compat.py schema_to_openapi.
* [hassfest rejects HA-core requirements in custom manifests](./hassfest-rejects-ha-core-requirements.md) - hassfest fails a custom manifest that lists any package from HA core's requirements.txt (e.g. aiohttp, probatio), so those stay undeclared and are exempted in the manifest guard test.
* [hassfest rejects URLs and angle brackets in strings.json descriptions](./hassfest-strings-no-urls-or-angle-brackets.md) - Config-flow description text must hold no dotted-domain URL and no angle-bracket token, or hassfest fails CI; edit strings.json and translations/en.json together.
* [LangGraph assistant probe rejects graph ids](./langgraph-assistant-probe-graph-id.md) - LangGraph GET /assistants/{id} accepts only UUIDs and answers 422 for a graph id, so the config probe must POST /assistants/search with graph_id when the assistant is a graph name.
* [LangGraph resume needs a pending interrupt, not thread status](./langgraph-resume-check-thread-interrupts.md) - Before resuming a LangGraph interrupt, check GET /threads/{id} has a non-empty interrupts field; status alone misfires because a cancelled run also reads interrupted.
* [n8n keepalive frames and Chat Trigger probe hazard](./n8n-keepalive-and-chat-trigger-probe.md) - n8n 2.4x sends a keepalive line every 30 s, possibly first, so the NDJSON sniffer must accept it; a setup probe must never GET a Chat Trigger URL, which starts a run.
* [Reconfigure/reauth abort must match whether the entry has an update listener](./config-entry-update-listener-aware-abort.md) - Reconfigure and reauth save via update_and_abort when the entry has an update listener, else update_reload_and_abort; reload+listener breaks in HA 2026.12.
* [The ChatLog timestamp prefix now applies only to presets that forward bare text](./chat-log-snapshots-user-text-before-handler.md) - The ChatLog timestamp prefix now applies only to presets that forward bare turn text; openai_compat/ollama replay ChatLog, n8n/langgraph forward the rendered prompt, so all four opt out.

# Decision

* [Ollama backend must not force num_ctx/keep_alive when the user leaves them unset](./ollama-num-ctx-keep-alive-unset.md) - Ollama num_ctx/keep_alive are sent only when configured — HA's 8192/-1 defaults overrode OLLAMA_KEEP_ALIVE and VRAM-sized contexts; the 30s-idle reason died with the Timeout fix.
* [The agent Timeout is the maximum backend silence, not a total deadline](./agent-timeout-is-max-silence.md) - The per-agent Timeout bounds how long a backend may stay silent (aiohttp sock_read), never a total deadline or a fixed idle cap; both broke slow backends.

# Runbook

* [Capture real backend streams inside the devcontainer](./live-backend-capture.md) - Real backend byte streams can be captured in the devcontainer with small images, reached by bridge IP because published ports are unreachable.
* [Updating from python-template in a HACS-integration repo](./copier-update-for-hacs-integration.md) - This repo renders from python-template as project_type library, so a same-version copier update changes only _commit; keep the HACS pyproject and hand-port template CI changes.
