---
id: LLMM-023
title: AI Task generate_data subentry for openai_compat and ollama
status: in-review
phase: 5
depends_on: [LLMM-007, LLMM-008, LLMM-010]
---

# LLMM-023 — AI Task `generate_data` subentry (openai_compat, ollama)

## Context
Home Assistant's `ai_task` integration lets automations call `ai_task.generate_data`
("summarize these sensor readings as JSON") against any AI Task entity. Core LLM
integrations (anthropic, openai, ollama, google) each offer an `ai_task_data` subentry type
next to `conversation`, so one connection serves both. LLM Middleman exposes only
`conversation` subentries, so users of its OpenAI-compatible or Ollama connections cannot
use AI Task without a second core integration pointing at the same server. This was a
fast-follow item from the v1 roadmap (`docs/implementation/README.md`).

Architecture is unchanged (`plan.md` §config model): a connection is the parent entry and
each agent a subentry. This ticket adds a second subentry type, limited to the two
stateless-replay presets that own the model call (the other presets forward a turn to a
backend that owns its own prompt and output shape, so structured output can't be enforced
there).

## Scope
**In**
- New subentry type `ai_task_data` (`SUBENTRY_TYPE_AI_TASK`) offered by the parent flow only
  when the connection's preset is `openai_compat` or `ollama`; a capability ClassVar on the
  adapter (e.g. `supports_ai_task`), not a backend-type check in the flow.
- `ai_task.py` platform with an `AITaskEntity` per `ai_task_data` subentry, feature
  `GENERATE_DATA` (+ `SUPPORT_ATTACHMENTS` once attachments work), `PARALLEL_UPDATES = 0`.
- `_async_generate_data(task, chat_log)`: run one adapter round-trip (plus the HA tool loop
  if `task.llm_api` is set) on the chat log HA prepared, then return the last assistant
  text, or its JSON parse when `task.structure` is set.
- Structured output:
  - `openai_compat`: `response_format = {"type": "json_schema", "json_schema": {"name":
    <task name slug>, "schema": <schema>, "strict": false}}`.
  - `ollama`: `format = <schema>` (Ollama's structured outputs).
  - Schema rendering reuses `backends/_compat.py:schema_to_openapi` — `task.structure` is a
    voluptuous schema on HA 2026.7–2026.8 and a `probatio.Schema` on ≥ 2026.9, which that
    shim already handles for tool parameters.
- Attachments (`UserContent.attachments`): images only, read off the event loop and sent
  base64 — `openai_compat` as a `content` parts array with `{"type": "image_url",
  "image_url": {"url": "data:<mime>;base64,..."}}`; `ollama` as the message's `images`
  list. A non-image attachment raises a translated `HomeAssistantError`.
- Subentry options: model (catalog dropdown, as the conversation subentry), the preset's
  sampling/runtime options, timeout. No prompt or memory-scope field.
- Strings + `translations/en.json`, diagnostics (redacted like conversation subentries),
  README section.

**Out**
- `generate_image` (no supported preset has an image endpoint behind this adapter layer).
- AI Task for converse / n8n / LangGraph / Dify.

## Implementation notes
- Reference implementations (HA 2026.10): `homeassistant/components/anthropic/ai_task.py`
  (entity, `_async_generate_data`, JSON-parse error path) and
  `homeassistant/components/ollama/ai_task.py` + `ollama/entity.py:_async_handle_chat_log`
  (`structure` → `probatio.to_openapi` → `format`; attachments → `images`).
  `homeassistant/components/ai_task/entity.py:_async_get_ai_task_chat_log` shows what HA puts
  in the chat log before `_async_generate_data` runs (system prompt, user content with
  attachments).
- The adapters build their request body in `_build_body` / `_build_request`; pass the
  output schema through `TurnContext` (a new optional field) instead of a second code path,
  so the conversation and AI Task requests stay one implementation.
- `custom_components/llm_middleman/__init__.py:PLATFORMS` gains `Platform.AI_TASK`; the
  conversation platform must skip `ai_task_data` subentries and vice versa.
- `config_flow.py:async_get_supported_subentry_types` returns the AI Task flow only for
  adapters with the capability; core's anthropic flow shows the per-type step/strings.
- Message conversion in both adapters (`_convert_content`) needs the `UserContent`
  attachments branch; keep `backends/_tools.py:replayable_content` in the path.

## Acceptance criteria
Tests are in `tests/test_ai_task.py` (driven through HA's real `ai_task.generate_data`
service, network mocked at `aioclient_mock`) and `tests/test_config_flow_ai_task.py`.

- [x] An `openai_compat` or `ollama` connection offers "Add AI task"; other presets do not.
      (`test_ai_task_offered_for_capable_presets`, `test_ai_task_not_offered_for_forwarding_presets`,
      `test_create_openai_ai_task_subentry`, `test_ollama_ai_task_form_fields`)
- [x] `ai_task.generate_data` without `structure` returns the model's text.
      (`test_generate_data_returns_text`, `test_text_task_sends_no_output_format`)
- [x] With `structure`, the request carries `response_format` (openai_compat) / `format`
      (ollama) rendered from the schema on both HA 2026.7 (voluptuous) and ≥ 2026.9
      (probatio), and the result is the parsed JSON object.
      (`test_structure_sets_openai_response_format`, `test_structure_sets_ollama_format`,
      `test_structure_returns_parsed_json`, `test_structure_renders_voluptuous_and_probatio`;
      the service-driven tests exercise voluptuous on the floor leg and probatio on the locked leg)
- [x] Unparsable JSON raises a translated `HomeAssistantError`, logged with the response.
      (`test_unparsable_json_raises_translated_error`)
- [x] Image attachments reach the model in each preset's wire shape; a non-image raises a
      translated error. (`test_image_attachment_openai_wire`, `test_image_attachment_ollama_wire`,
      `test_non_image_attachment_raises_translated_error`)
- [x] `task.llm_api` set → the HA tool loop runs, bounded like conversation turns.
      (`test_llm_api_runs_tool_loop`, `test_tool_loop_is_bounded`)
- [x] Removing the subentry removes its entity; diagnostics include it, redacted.
      (`test_removing_subentry_removes_entity`, `test_diagnostics_include_ai_task_subentry_redacted`,
      `test_platforms_only_build_their_own_subentry_type`)
- [x] Unit tests for each criterion; both CI legs (locked + floor) green; coverage ≥ the
      gate.

Live rows (Verification below) are **owner-run**: the `ollama/ollama` image is ~3.4 GB
compressed, over the devcontainer's pull budget. See `e2e-results/ai-task.md`.

## Decisions (open questions resolved)
- **Servers that reject `response_format.json_schema`:** no `json_object` fallback and no
  toggle. The server's 4xx reaches the user as the translated `ai_task_backend_error`, and
  `_post_checked` logs its body. Documented in the README.
- **`strict`:** `false`, as planned. HA structure schemas don't mark every property
  required or set `additionalProperties: false`, which strict mode requires.
- **Attachment size:** no cap, documented in the README (core openai/ollama don't cap
  either). Files are read in the executor.
- **Failures:** unlike a conversation turn there is no spoken fallback. Backend errors,
  an empty stream, and a hit tool-loop cap raise translated errors to the automation.
- **Test dependency:** `pyturbojpeg==1.8.3` is back in the dev group. HA 2026.7 and
  2026.10 pin the same version, so the CI floor step needs no change.

## Verification
- `uv run pytest -q`, `uv run basedpyright`, `uv run ruff check .`, and the floor leg from
  `.github/workflows/lint.yml`.
- Live: on a throwaway HA (`scripts/e2e/README.md`), call `ai_task.generate_data` with and
  without a `structure` against a real OpenAI-compatible server and a real Ollama, and with
  an image attachment against a vision model; record the rows in
  `docs/implementation/e2e-results/`.

## Risks / open questions
- Importing `homeassistant.components.ai_task` pulls in `camera`, which needs `turbojpeg`
  (PyTurboJPEG) — not in the current dev environment, so the import fails in tests today.
  The dev dependency group must add it (check it installs on both CI legs).
- Many OpenAI-compatible servers ignore or reject `response_format.json_schema` (older vLLM,
  LM Studio versions, some gateways). Decide between a `json_object` fallback with the
  schema in the prompt, or surfacing the server's 400 as a clear error; don't add a config
  toggle users have to discover.
- `strict: true` requires every property to be required and `additionalProperties: false`;
  HA schemas don't guarantee that, hence `strict: false` above — confirm against OpenAI.
- Reading large attachments into memory for base64: cap the size or document the limit.
