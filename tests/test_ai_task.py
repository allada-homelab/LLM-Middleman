"""AI Task ``generate_data`` platform (LLMM-023), driven through HA's real ``ai_task`` service.

Each test sets up a real config entry (openai_compat or ollama) with an ``ai_task_data``
subentry, mocks only the HTTP boundary (``aioclient_mock``), and calls
``ai_task.generate_data`` (or ``ai_task.async_generate_data`` where the service has no
equivalent parameter, e.g. ``llm_api``). The floor CI leg runs the same tests against HA
2026.7, where the service builds ``structure`` as a voluptuous schema instead of probatio.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import ai_task, conversation, media_source
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.llm_middleman.const import (
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_MODEL,
    CONF_NAME,
    DOMAIN,
    MAX_TOOL_ITERATIONS,
    SUBENTRY_TYPE_AI_TASK,
    SUBENTRY_TYPE_CONVERSATION,
)
from custom_components.llm_middleman.diagnostics import async_get_config_entry_diagnostics

from .conftest import probatio_tool_schema, tool_call_result, vol_tool_schema

BASE_URL = "http://backend.local:8000"
TURN_PATH = {BACKEND_OPENAI_COMPAT: "/v1/chat/completions", BACKEND_OLLAMA: "/api/chat"}
PROBE: dict[str, tuple[str, dict[str, Any]]] = {
    BACKEND_OPENAI_COMPAT: ("/v1/models", {"data": []}),
    BACKEND_OLLAMA: ("/api/tags", {"models": []}),
}
TOOL_PRESETS = [BACKEND_OPENAI_COMPAT, BACKEND_OLLAMA]

# A service-call ``structure`` (selector fields) and the JSON Schema it must render to.
STRUCTURE = {"answer": {"selector": {"text": None}, "required": True, "description": "The answer"}}


# --- wire helpers -------------------------------------------------------------------


def _sse(*payloads: object) -> bytes:
    return b"".join(b"data: " + json.dumps(p).encode() + b"\n\n" for p in payloads) + b"data: [DONE]\n\n"


def text_reply(preset: str, text: str) -> bytes:
    """A complete streamed reply carrying ``text`` in ``preset``'s wire format."""
    if preset == BACKEND_OPENAI_COMPAT:
        return _sse({"choices": [{"delta": {"role": "assistant", "content": text}}]})
    return (
        json.dumps({"message": {"role": "assistant", "content": text}, "done": False}).encode()
        + b"\n"
        + json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}).encode()
        + b"\n"
    )


def tool_reply(preset: str) -> bytes:
    """A streamed reply that only calls the ``get_time`` tool."""
    if preset == BACKEND_OPENAI_COMPAT:
        call = {"index": 0, "id": "call_1", "function": {"name": "get_time", "arguments": "{}"}}
        return _sse({"choices": [{"delta": {"role": "assistant", "tool_calls": [call]}}]})
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}],
    }
    return json.dumps({"message": message, "done": True}).encode() + b"\n"


def queue_replies(aioclient_mock: AiohttpClientMocker, preset: str, replies: list[bytes]) -> None:
    """Serve ``replies`` in order from the preset's turn endpoint (the last one repeats)."""
    remaining = list(replies)

    async def _next(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        body = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return AiohttpClientMockResponse(method, url, response=body)

    aioclient_mock.post(f"{BASE_URL}{TURN_PATH[preset]}", side_effect=_next)  # pyright: ignore[reportUnknownMemberType]


def turn_bodies(aioclient_mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    """The JSON bodies of every turn POST sent so far."""
    calls: list[tuple[str, URL, Any, Any]] = aioclient_mock.mock_calls  # pyright: ignore[reportUnknownMemberType]
    return [data for method, _url, data, _headers in calls if method.lower() == "post"]


def sent_schema(preset: str, body: dict[str, Any]) -> dict[str, Any]:
    """The output JSON Schema a turn request carried, wherever ``preset`` puts it."""
    if preset == BACKEND_OPENAI_COMPAT:
        return body["response_format"]["json_schema"]["schema"]
    return body["format"]


# --- setup ----------------------------------------------------------------------------


async def setup_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    preset: str,
    *,
    with_conversation: bool = False,
) -> MockConfigEntry:
    """Set up a ``preset`` connection with one AI task subentry (and optionally an agent)."""
    path, payload = PROBE[preset]
    aioclient_mock.get(f"{BASE_URL}{path}", json=payload)  # pyright: ignore[reportUnknownMemberType]
    subentries = [
        ConfigSubentryData(data={CONF_MODEL: "m1"}, subentry_type=SUBENTRY_TYPE_AI_TASK, title="Task", unique_id=None)
    ]
    if with_conversation:
        subentries.append(
            ConfigSubentryData(
                data={CONF_NAME: "Agent"}, subentry_type=SUBENTRY_TYPE_CONVERSATION, title="Agent", unique_id=None
            )
        )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"backend_type": preset, "base_url": BASE_URL},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def task_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The AI task entity id of ``entry``'s single ``ai_task_data`` subentry."""
    subentry_id = next(s.subentry_id for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_AI_TASK)
    entity_id = er.async_get(hass).async_get_entity_id("ai_task", DOMAIN, subentry_id)
    assert entity_id is not None
    return entity_id


async def generate(hass: HomeAssistant, entity_id: str, **fields: Any) -> dict[str, Any]:
    """Call ``ai_task.generate_data`` through HA's service registry and return its response."""
    response = await hass.services.async_call(
        "ai_task",
        "generate_data",
        {"task_name": "Test Task", "entity_id": entity_id, "instructions": "Do it", **fields},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return dict(response)


async def run_task(hass: HomeAssistant, entity_id: str, **fields: Any) -> ai_task.GenDataTaskResult:
    """Call ``ai_task.async_generate_data`` for inputs the service can't carry (raw schemas, ``llm_api``)."""
    # Upstream types ``attachments`` as a bare ``list[dict]``, which strict mode reports.
    return await ai_task.async_generate_data(  # pyright: ignore[reportUnknownMemberType]
        hass, task_name="Test Task", entity_id=entity_id, instructions="Do it", **fields
    )


@pytest.fixture(params=TOOL_PRESETS)
def tool_preset(request: pytest.FixtureRequest) -> str:
    """Parametrize over the two presets that offer AI Task."""
    return str(request.param)


# --- text / structured output ------------------------------------------------------------


async def test_generate_data_returns_text(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str
) -> None:
    """Without ``structure`` the service returns the model's text verbatim."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [text_reply(tool_preset, "Hello there")])

    result = await generate(hass, task_entity_id(hass, entry))

    assert result["data"] == "Hello there"


async def test_text_task_sends_no_output_format(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str
) -> None:
    """A plain task leaves the request free-form: no ``response_format``/``format`` field."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [text_reply(tool_preset, "ok")])

    await generate(hass, task_entity_id(hass, entry))

    body = turn_bodies(aioclient_mock)[0]
    assert "response_format" not in body
    assert "format" not in body
    assert body["model"] == "m1"


async def test_structure_returns_parsed_json(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str
) -> None:
    """With ``structure`` the reply is parsed and returned as a JSON object."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [text_reply(tool_preset, '{"answer": "42"}')])

    result = await generate(hass, task_entity_id(hass, entry), structure=STRUCTURE)

    assert result["data"] == {"answer": "42"}


async def test_structure_sets_openai_response_format(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """openai_compat carries the rendered schema as a non-strict ``json_schema`` response_format."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT)
    queue_replies(aioclient_mock, BACKEND_OPENAI_COMPAT, [text_reply(BACKEND_OPENAI_COMPAT, '{"answer": "x"}')])

    await generate(hass, task_entity_id(hass, entry), structure=STRUCTURE)

    response_format = turn_bodies(aioclient_mock)[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "test_task"
    assert response_format["json_schema"]["strict"] is False
    schema = response_format["json_schema"]["schema"]
    assert schema["properties"]["answer"]["type"] == "string"
    assert schema["required"] == ["answer"]


async def test_structure_sets_ollama_format(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """ollama carries the rendered schema as the request's ``format`` (structured outputs)."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OLLAMA)
    queue_replies(aioclient_mock, BACKEND_OLLAMA, [text_reply(BACKEND_OLLAMA, '{"answer": "x"}')])

    await generate(hass, task_entity_id(hass, entry), structure=STRUCTURE)

    schema = turn_bodies(aioclient_mock)[0]["format"]
    assert schema["properties"]["answer"]["type"] == "string"
    assert schema["required"] == ["answer"]


@pytest.mark.parametrize("make_schema", [vol_tool_schema, probatio_tool_schema], ids=["voluptuous", "probatio"])
async def test_structure_renders_voluptuous_and_probatio(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    tool_preset: str,
    make_schema: Callable[[], Any],
) -> None:
    """Both schema flavours HA passes as ``task.structure`` render to the same JSON Schema.

    HA 2026.7-2026.8 builds voluptuous schemas, HA >= 2026.9 probatio ones; the probatio
    case skips on the floor leg (no probatio installed).
    """
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [text_reply(tool_preset, '{"name": "a"}')])

    await run_task(hass, task_entity_id(hass, entry), structure=make_schema())

    schema = sent_schema(tool_preset, turn_bodies(aioclient_mock)[0])
    assert schema["properties"]["brightness"] == {"type": "integer", "minimum": 0, "maximum": 100}
    assert schema["required"] == ["name"]


async def test_unparsable_json_raises_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A structured task whose reply isn't JSON raises a translated error and logs the reply."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [text_reply(tool_preset, "not json at all")])

    with caplog.at_level(logging.ERROR), pytest.raises(HomeAssistantError) as raised:
        await generate(hass, task_entity_id(hass, entry), structure=STRUCTURE)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "ai_task_json_parse_error"
    assert "not json at all" in caplog.text


async def test_backend_error_raises_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str
) -> None:
    """A backend HTTP error surfaces as a translated error, never as fallback text in ``data``."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    aioclient_mock.post(f"{BASE_URL}{TURN_PATH[tool_preset]}", status=500, text="boom")  # pyright: ignore[reportUnknownMemberType]

    with pytest.raises(HomeAssistantError) as raised:
        await generate(hass, task_entity_id(hass, entry))

    assert raised.value.translation_key == "ai_task_backend_error"


async def test_empty_reply_raises_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str
) -> None:
    """A stream that ends without any assistant content raises instead of returning nothing."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    empty = b"data: [DONE]\n\n" if tool_preset == BACKEND_OPENAI_COMPAT else b'{"done": true}\n'
    queue_replies(aioclient_mock, tool_preset, [empty])

    with pytest.raises(HomeAssistantError) as raised:
        await generate(hass, task_entity_id(hass, entry))

    assert raised.value.translation_key == "ai_task_no_response"


async def test_token_usage_traced(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Token usage from the round-trip is traced like a conversation turn's."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT)
    reply = _sse(
        {"choices": [{"delta": {"role": "assistant", "content": "hi"}}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
    )
    queue_replies(aioclient_mock, BACKEND_OPENAI_COMPAT, [reply])

    with patch.object(conversation.ChatLog, "async_trace") as trace:
        await generate(hass, task_entity_id(hass, entry))

    trace.assert_any_call({"stats": {"input_tokens": 9, "cached_input_tokens": 0, "output_tokens": 2}})


# --- attachments --------------------------------------------------------------------------


def _attachment(path: Path, mime_type: str) -> media_source.PlayMedia:
    return media_source.PlayMedia(url="http://example.local/media", mime_type=mime_type, path=path)


async def _generate_with_attachment(hass: HomeAssistant, entity_id: str, media: media_source.PlayMedia) -> None:
    with patch("homeassistant.components.media_source.async_resolve_media", return_value=media):
        await generate(
            hass,
            entity_id,
            attachments=[{"media_content_id": "media-source://media_source/local/file", "media_content_type": ""}],
        )


async def test_image_attachment_openai_wire(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    """openai_compat sends an image as a base64 data-URL ``image_url`` content part."""
    image = tmp_path / "cat.png"
    image.write_bytes(b"\x89PNG fake")
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT)
    queue_replies(aioclient_mock, BACKEND_OPENAI_COMPAT, [text_reply(BACKEND_OPENAI_COMPAT, "a cat")])

    await _generate_with_attachment(hass, task_entity_id(hass, entry), _attachment(image, "image/png"))

    user = turn_bodies(aioclient_mock)[0]["messages"][-1]
    encoded = base64.b64encode(b"\x89PNG fake").decode()
    assert user["content"] == [
        {"type": "text", "text": "Do it"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
    ]


async def test_image_attachment_ollama_wire(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    """ollama sends an image as a base64 entry in the user message's ``images`` list."""
    image = tmp_path / "cat.jpg"
    image.write_bytes(b"\xff\xd8 fake")
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OLLAMA)
    queue_replies(aioclient_mock, BACKEND_OLLAMA, [text_reply(BACKEND_OLLAMA, "a cat")])

    await _generate_with_attachment(hass, task_entity_id(hass, entry), _attachment(image, "image/jpeg"))

    user = turn_bodies(aioclient_mock)[0]["messages"][-1]
    assert user == {"role": "user", "content": "Do it", "images": [base64.b64encode(b"\xff\xd8 fake").decode()]}


async def test_non_image_attachment_raises_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str, tmp_path: Path
) -> None:
    """A non-image attachment raises a translated error before anything is sent."""
    document = tmp_path / "notes.pdf"
    document.write_bytes(b"%PDF")
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [text_reply(tool_preset, "unused")])

    with pytest.raises(HomeAssistantError) as raised:
        await _generate_with_attachment(hass, task_entity_id(hass, entry), _attachment(document, "application/pdf"))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "unsupported_attachment_type"
    assert turn_bodies(aioclient_mock) == []


# --- HA tool loop ---------------------------------------------------------------------------


class _TimeTool(llm.Tool):
    """A real HA tool answering ``{"time": "noon"}``."""

    name = "get_time"
    description = "Return the current time"
    integration = DOMAIN  # HA >= 2026.10 attributes every tool to an integration

    async def async_call(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> Any:
        return tool_call_result({"time": "noon"})


class _ToolAPI(llm.API):
    """An LLM API exposing the one ``get_time`` tool."""

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        return llm.APIInstance(api=self, api_prompt="Use tools.", llm_context=llm_context, tools=[_TimeTool()])


@pytest.fixture
def tool_api(hass: HomeAssistant) -> llm.API:
    """The task's ``llm_api``: HA runs its tool for real when the model calls it."""
    return _ToolAPI(hass=hass, id="test_tools", name="Test tools")


async def test_llm_api_runs_tool_loop(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_preset: str, tool_api: llm.API
) -> None:
    """``task.llm_api`` set: tools are offered, HA runs the call, and the result is replayed."""
    entry = await setup_entry(hass, aioclient_mock, tool_preset)
    queue_replies(aioclient_mock, tool_preset, [tool_reply(tool_preset), text_reply(tool_preset, "It is noon")])

    result = await run_task(hass, task_entity_id(hass, entry), llm_api=tool_api)

    first, second = turn_bodies(aioclient_mock)
    assert first["tools"][0]["function"]["name"] == "get_time"
    assert second["messages"][-1]["role"] == "tool"
    assert "noon" in second["messages"][-1]["content"]
    assert result.data == "It is noon"


async def test_tool_loop_is_bounded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tool_api: llm.API
) -> None:
    """A backend that never stops calling tools is cut off at MAX_TOOL_ITERATIONS with an error."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT)
    queue_replies(aioclient_mock, BACKEND_OPENAI_COMPAT, [tool_reply(BACKEND_OPENAI_COMPAT)])

    with pytest.raises(HomeAssistantError) as raised:
        await run_task(hass, task_entity_id(hass, entry), llm_api=tool_api)

    assert len(turn_bodies(aioclient_mock)) == MAX_TOOL_ITERATIONS
    assert raised.value.translation_key == "ai_task_no_response"


# --- platform split, removal, diagnostics ------------------------------------------------------


async def test_platforms_only_build_their_own_subentry_type(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each subentry gets exactly one entity, on the platform matching its type."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT, with_conversation=True)
    registry = er.async_get(hass)

    by_subentry = {
        subentry.subentry_type: [e.domain for e in registry.entities.values() if e.unique_id == subentry.subentry_id]
        for subentry in entry.subentries.values()
    }

    assert by_subentry == {SUBENTRY_TYPE_AI_TASK: ["ai_task"], SUBENTRY_TYPE_CONVERSATION: ["conversation"]}


async def test_entity_advertises_generate_data_and_attachments(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The entity supports GENERATE_DATA and SUPPORT_ATTACHMENTS (no image generation)."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OLLAMA)

    state = hass.states.get(task_entity_id(hass, entry))

    assert state is not None
    features = ai_task.AITaskEntityFeature(state.attributes["supported_features"])
    assert features == ai_task.AITaskEntityFeature.GENERATE_DATA | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS


async def test_removing_subentry_removes_entity(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Deleting the AI task subentry reloads the entry and its entity disappears."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT, with_conversation=True)
    entity_id = task_entity_id(hass, entry)
    subentry_id = next(s.subentry_id for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_AI_TASK)

    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(entity_id) is None
    assert hass.states.get(entity_id) is None


async def test_diagnostics_include_ai_task_subentry_redacted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Diagnostics list the AI task subentry and its entity, with the connection URL redacted."""
    entry = await setup_entry(hass, aioclient_mock, BACKEND_OPENAI_COMPAT)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    subentry = next(s for s in dump["subentries"].values() if s["subentry_type"] == SUBENTRY_TYPE_AI_TASK)
    assert subentry["data"] == {CONF_MODEL: "m1"}
    assert task_entity_id(hass, entry) in dump["entities"]
    assert dump["data"]["base_url"] == "**REDACTED**"
    assert BASE_URL not in json.dumps(dump)
