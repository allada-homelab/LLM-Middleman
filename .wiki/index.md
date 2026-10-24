---
okf_version: "0.2"
---

# Gotcha

* [ChatLog snapshots the user text before _async_handle_message runs](./chat-log-snapshots-user-text-before-handler.md) - HA adds the user turn to ChatLog before calling the entity, so editing user_input.text there reaches only presets that read user_input; openai_compat and ollama replay ChatLog.
* [Dify SSE events are dispatched on the JSON event field](./dify-sse-event-name-in-json.md) - Dify sends bare data frames with the event name inside the JSON, so dispatch on payload event; an Agent app's closing message repeats the answer and must not be spoken again.
* [HA 2026.9+ tool schemas are probatio, not voluptuous](./probatio-tool-schemas-ha-2026-9.md) - From HA 2026.9, llm tool schemas are probatio Schemas that voluptuous_openapi cannot convert, so tool schemas must go through backends/_compat.py schema_to_openapi.
* [hassfest rejects HA-core requirements in custom manifests](./hassfest-rejects-ha-core-requirements.md) - hassfest fails a custom manifest that lists any package from HA core's requirements.txt (e.g. aiohttp, probatio), so those stay undeclared and are exempted in the manifest guard test.
* [hassfest rejects URLs and angle brackets in strings.json descriptions](./hassfest-strings-no-urls-or-angle-brackets.md) - Config-flow description text must hold no dotted-domain URL and no angle-bracket token, or hassfest fails CI; edit strings.json and translations/en.json together.
* [LangGraph assistant probe rejects graph ids](./langgraph-assistant-probe-graph-id.md) - LangGraph GET /assistants/{id} accepts only UUIDs and answers 422 for a graph id, so the config probe must POST /assistants/search with graph_id when the assistant is a graph name.
* [LangGraph resume needs a pending interrupt, not thread status](./langgraph-resume-check-thread-interrupts.md) - Before resuming a LangGraph interrupt, check GET /threads/{id} has a non-empty interrupts field; status alone misfires because a cancelled run also reads interrupted.
* [Reconfigure/reauth abort must match whether the entry has an update listener](./config-entry-update-listener-aware-abort.md) - Reconfigure and reauth save via update_and_abort when the entry has an update listener, else update_reload_and_abort; reload+listener breaks in HA 2026.12.

# Runbook

* [Updating from python-template in a HACS-integration repo](./copier-update-for-hacs-integration.md) - This repo renders from python-template as project_type library, so a same-version copier update changes only _commit; keep the HACS pyproject and hand-port template CI changes.
