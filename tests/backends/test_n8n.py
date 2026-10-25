"""Tests for the n8n backend adapter (LLMM-012).

Raw bytes are driven through the *real* parsers via the conftest stream harness
(``FakeStreamResponse`` + ``fake_aiohttp_session``), splitting NDJSON chunks at
arbitrary boundaries. Covers the dual streaming/blocking branch (on the actual
response, not the config toggle), multiple begin/end cycles, EOF-as-done, error and
HTML fallbacks, the output-field fallback chain, misconfig visibility, auth, and
credential redaction. LLMM-025 adds replays of real n8n 2.44.6 captures
(``tests/backends/fixtures/n8n/*.json``, body bytes verbatim), the run-free OPTIONS
webhook probe, and AI Agent ``intermediateSteps`` as external tool activity.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.const import CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.llm_middleman.backends import BACKEND_TO_CLS
from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    TurnContext,
)
from custom_components.llm_middleman.backends.n8n import N8nAdapter, N8nWebhookError
from custom_components.llm_middleman.const import (
    BACKEND_N8N,
    CONF_AUTH_TYPE,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_INPUT_FIELD,
    CONF_OUTPUT_FIELD,
    CONF_PASSWORD,
    CONF_SESSION_FIELD,
    CONF_TARGET_TYPE,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    N8N_TARGET_CHAT_TRIGGER,
    N8N_TARGET_WEBHOOK,
)
from tests.conftest import (
    FakeStreamResponse,
    MockChatLog,
    build_entity,
    chunk_bytes,
    collect,
    fake_aiohttp_session,
    make_input,
)

_WEBHOOK = "https://n8n.local/webhook/abc/chat"


def _adapter(
    hass: HomeAssistant,
    session: Any,
    *,
    connection: dict[str, Any] | None = None,
) -> N8nAdapter:
    data: dict[str, Any] = {CONF_WEBHOOK_URL: _WEBHOOK}
    if connection is not None:
        data.update(connection)
    return N8nAdapter(hass, cast("aiohttp.ClientSession", session), data)


async def _collect(
    adapter: N8nAdapter,
    ctx: TurnContext,
    *,
    system_prompt: str = "",
) -> list[conversation.AssistantContentDeltaDict]:
    chat_log = conversation.ChatLog(adapter.hass, "conv-1", [conversation.SystemContent(content=system_prompt)])
    return await collect(adapter.stream_turn(chat_log, make_input("hi there"), ctx))


def _ctx(**options: Any) -> TurnContext:
    return TurnContext(options=options, memory_key="conv-1")


def _response(chunks: list[bytes], content_type: str, *, status: int = 200) -> FakeStreamResponse:
    return FakeStreamResponse(chunks, status=status, headers={"Content-Type": content_type})


# --- streaming (NDJSON) ------------------------------------------------------


async def test_streaming_happy_path_split_mid_object(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{"type":"item","content":"Hel"}\n{"type":"item","content":"lo"}\n{"type":"end"}\n'
    # Fixed width 7 splits every object across chunk boundaries; content-type is plain
    # application/json, so the ndjson branch must be chosen from the first-line shape.
    response = _response(chunk_bytes(blob, 7), "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]


async def test_multiple_begin_end_cycles(hass: HomeAssistant) -> None:
    blob = (
        b'{"type":"begin"}\n{"type":"item","content":"A"}\n{"type":"end"}\n'
        b'{"type":"begin"}\n{"type":"item","content":"B"}\n{"type":"end"}\n'
    )
    response = _response(chunk_bytes(blob, 9), "application/json-lines")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    # The first `end` is not terminal; both items are emitted, single leading role, and the
    # second segment is newline-separated (LLMM-025: not run together as "AB").
    assert deltas == [{"role": "assistant"}, {"content": "A"}, {"content": "\nB"}]


async def test_content_whitespace_not_stripped(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{"type":"item","content":" hi "}\n{"type":"end"}\n'
    response = _response([blob], "application/x-ndjson")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": " hi "}]


async def test_malformed_json_line_skipped(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{not valid json}\n{"type":"item","content":"ok"}\n{"type":"end"}\n'
    response = _response([blob], "application/json-lines")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "ok"}]


async def test_silent_stream_end_raises(hass: HomeAssistant) -> None:
    # begin/end with no item content, then EOF -> nothing usable produced.
    blob = b'{"type":"begin"}\n{"type":"end"}\n'
    response = _response([blob], "application/json-lines")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError):
        await _collect(_adapter(hass, session), _ctx())


async def test_keepalive_first_line_still_streams(hass: HomeAssistant) -> None:
    # n8n >= 2.4x heartbeats `{"type":"keepalive"}` every 30 s; when it is the first line the
    # mode sniff must still pick streaming (plain application/json content-type, as n8n sends).
    blob = b'{"type":"keepalive"}\n{"type":"begin"}\n{"type":"item","content":"Hi"}\n{"type":"end"}\n'
    response = _response(chunk_bytes(blob, 5), "application/json; charset=utf-8")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_keepalive_between_items_ignored(hass: HomeAssistant) -> None:
    blob = (
        b'{"type":"begin"}\n{"type":"item","content":"A"}\n{"type":"keepalive"}\n'
        b'{"type":"item","content":"B"}\n{"type":"end"}\n'
    )
    response = _response([blob], "application/json; charset=utf-8")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "A"}, {"content": "B"}]


async def test_keepalive_only_stream_is_empty(hass: HomeAssistant) -> None:
    # Heartbeats carry no content: a stream of only keepalives + end is the same as an empty one.
    blob = b'{"type":"keepalive"}\n{"type":"keepalive"}\n{"type":"end"}\n'
    response = _response([blob], "application/json; charset=utf-8")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError, match="without producing any content"):
        await _collect(_adapter(hass, session), _ctx())


async def test_error_chunk_raises(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{"type":"error","content":"boom"}\n'
    response = _response([blob], "application/json-lines")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError, match="boom"):
        await _collect(_adapter(hass, session), _ctx())


async def test_html_body_raises(hass: HomeAssistant) -> None:
    blob = b"<html><body><h1>504 Gateway Time-out</h1></body></html>"
    response = _response([blob], "text/html")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError):
        await _collect(_adapter(hass, session), _ctx())


async def test_oversize_ndjson_line_skipped(hass: HomeAssistant) -> None:
    huge = b'{"type":"item","content":"' + b"x" * 70000 + b'"}\n'
    blob = b'{"type":"begin"}\n' + huge + b'{"type":"item","content":"ok"}\n{"type":"end"}\n'
    response = _response(chunk_bytes(blob, 4096), "application/json-lines")
    deltas = await _collect(_adapter(hass, fake_aiohttp_session(response=response)), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "ok"}]


# --- blocking / wrong-mode ---------------------------------------------------


async def test_wrong_mode_mismatch_streaming_toggle_but_blocking_body(hass: HomeAssistant) -> None:
    # `streaming` config is ON, but n8n sent a single blocking JSON body: the adapter
    # must detect the real response and NOT wait for NDJSON deltas.
    response = _response([b'{"output":"done"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx(streaming=True))
    assert deltas == [{"role": "assistant"}, {"content": "done"}]


async def test_blocking_body_over_line_cap_still_parsed(hass: HomeAssistant) -> None:
    """A single-line blocking body longer than the sniff/line cap is still read whole."""
    reply = "y" * 100_000
    blob = json.dumps({"output": reply}).encode()
    response = _response(chunk_bytes(blob, 4096), "application/json")
    deltas = await _collect(_adapter(hass, fake_aiohttp_session(response=response)), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": reply}]


async def test_blocking_body_over_cap_raises(hass: HomeAssistant) -> None:
    """A blocking body past the 1 MiB cap fails the turn instead of buffering unbounded."""
    blob = json.dumps({"output": "z" * (1 << 20)}).encode()
    response = _response(chunk_bytes(blob, 65536), "application/json")
    with pytest.raises(BackendStreamError, match="exceeded"):
        await _collect(_adapter(hass, fake_aiohttp_session(response=response)), _ctx())


async def test_blocking_output_fallback_to_text(hass: HomeAssistant) -> None:
    # Default output_field is `output`; absent -> falls back to `text`.
    response = _response([b'{"text":"hi"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "hi"}]


async def test_blocking_custom_output_field(hass: HomeAssistant) -> None:
    response = _response([b'{"answer":"yo"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx(**{CONF_OUTPUT_FIELD: "answer"}))
    assert deltas == [{"role": "assistant"}, {"content": "yo"}]


async def test_blocking_missing_output_field_surfaces_error(hass: HomeAssistant) -> None:
    # Neither `output` nor `text` present -> surfaced error, never speak the raw object.
    response = _response([b'{"foo":"bar"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError):
        await _collect(_adapter(hass, session), _ctx())


async def test_blocking_pretty_printed_json(hass: HomeAssistant) -> None:
    # A pretty-printed blocking body has newlines but no StructuredChunk first line.
    response = _response([b'{\n  "output": "spread"\n}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "spread"}]


async def test_blocking_continue_conversation(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"more?","continueConversation":true}'], "application/json")
    session = fake_aiohttp_session(response=response)
    ctx = _ctx()
    deltas = await _collect(_adapter(hass, session), ctx)
    assert deltas == [{"role": "assistant"}, {"content": "more?"}]
    assert ctx.continue_conversation is True


# --- request shape -----------------------------------------------------------


def _posted_body(session: Any) -> dict[str, Any]:
    call = session.post.call_args
    return cast("dict[str, Any]", json.loads(call.kwargs["data"]))


async def test_action_included_for_chat_trigger(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(
        _adapter(hass, session, connection={CONF_TARGET_TYPE: N8N_TARGET_CHAT_TRIGGER}),
        _ctx(),
    )
    body = _posted_body(session)
    assert body["action"] == "sendMessage"
    # Session key and turn text are sent under the default field names.
    assert body["sessionId"] == "conv-1"
    assert body["chatInput"] == "hi there"
    # Default bound is the n8n-specific 30 s; no total deadline so a live stream isn't cut off.
    timeout = session.post.call_args.kwargs["timeout"]
    assert timeout.total is None
    assert timeout.sock_connect == 30
    assert timeout.sock_read == 30


async def test_action_omitted_for_plain_webhook(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(
        _adapter(hass, session, connection={CONF_TARGET_TYPE: N8N_TARGET_WEBHOOK}),
        _ctx(),
    )
    assert "action" not in _posted_body(session)


async def test_custom_fields_and_system_prompt(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(
        _adapter(hass, session),
        _ctx(
            **{
                CONF_SESSION_FIELD: "sid",
                CONF_INPUT_FIELD: "prompt",
            }
        ),
        system_prompt="be terse",
    )
    body = _posted_body(session)
    assert body["sid"] == "conv-1"
    assert body["prompt"] == "hi there"
    assert body["systemPrompt"] == "be terse"


async def test_empty_system_prompt_omitted(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(_adapter(hass, session), _ctx())
    assert "systemPrompt" not in _posted_body(session)


async def test_configured_prompt_reaches_body_rendered(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """The agent's CONF_PROMPT template arrives rendered via the real provide-llm-data path."""
    session = fake_aiohttp_session(response=_response([b'{"output":"ok"}'], "application/json"))
    entity = build_entity(hass, _adapter(hass, session), {CONF_PROMPT: "Hi {{ 'x' | upper }}"})

    await entity._async_handle_message(make_input("hi there"), mock_chat_log)

    system_prompt = _posted_body(session)["systemPrompt"]
    assert system_prompt.startswith("Hi X\n")
    assert "{{" not in system_prompt


# --- auth + redaction --------------------------------------------------------


async def test_auth_basic_sets_basicauth_and_redacts(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    with caplog.at_level(logging.DEBUG, logger="custom_components.llm_middleman.backends.n8n"):
        await _collect(
            _adapter(
                hass,
                session,
                connection={
                    CONF_AUTH_TYPE: N8N_AUTH_BASIC,
                    CONF_USERNAME: "user",
                    CONF_PASSWORD: "s3cret",
                },
            ),
            _ctx(),
        )
    headers = session.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == aiohttp.encode_basic_auth("user", "s3cret")
    assert "s3cret" not in caplog.text


async def test_auth_header_sets_header_and_redacts(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    with caplog.at_level(logging.DEBUG, logger="custom_components.llm_middleman.backends.n8n"):
        await _collect(
            _adapter(
                hass,
                session,
                connection={
                    CONF_AUTH_TYPE: N8N_AUTH_HEADER,
                    CONF_HEADER_NAME: "X-Api-Key",
                    CONF_HEADER_VALUE: "tok-42",
                },
            ),
            _ctx(),
        )
    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-Api-Key"] == "tok-42"
    assert "Authorization" not in headers
    assert "tok-42" not in caplog.text


# --- registration + validation ----------------------------------------------


def test_registered_in_factory() -> None:
    assert BACKEND_TO_CLS[BACKEND_N8N] is N8nAdapter
    assert N8nAdapter.backend_type == BACKEND_N8N
    assert N8nAdapter.supports_ha_tools is False
    assert N8nAdapter.supports_memory_scope is True


@pytest.mark.parametrize("url", ["", "not-a-url", "ftp://n8n.local/webhook"])
async def test_validate_connection_rejects_bad_url(hass: HomeAssistant, url: str) -> None:
    with pytest.raises(BackendConnectionError):
        await N8nAdapter.async_validate_connection(hass, {CONF_WEBHOOK_URL: url})


# --- LLMM-025: real n8n 2.44.6 captures ---------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "n8n"
_CHAT_URL = "https://n8n.local/webhook/31546836-94ae-49a2-898f-b6dea0b3feca/chat"
_PLAIN_URL = "https://n8n.local/webhook/stream-text"


def _fixture(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((_FIXTURES / f"{name}.json").read_text()))


def _replay(name: str, *, width: int = 7) -> FakeStreamResponse:
    """The captured body, split at ``width`` bytes, with the captured status and content-type."""
    fx = _fixture(name)
    headers = {"Content-Type": fx["content_type"]} if fx["content_type"] else {}
    return FakeStreamResponse(chunk_bytes(fx["body"].encode(), width), status=fx["status"], headers=headers)


def _spoken(deltas: list[conversation.AssistantContentDeltaDict]) -> str:
    return "".join(str(delta.get("content") or "") for delta in deltas)


@pytest.mark.parametrize(
    ("fixture", "spoken"),
    [
        ("stream_begin_item_end", "Hello from n8n streaming"),
        ("chat_trigger_stream", "chat streamed reply"),
        ("stream_keepalive_first", "after wait"),
        ("json_node_reply", "json node reply"),
        ("first_entry_object", "object reply"),
        # Respond to Webhook "Text" is served as text/html (item 1).
        ("text_reply_html", "plain text reply"),
        # "All Entries" / "All Incoming Items" reply with a JSON array (item 2).
        ("all_entries_single", "array reply"),
        ("all_entries_array", "First item.\nSecond item."),
        ("all_incoming_items", "First item.\nSecond item."),
        # Respond JSON / First Incoming Item in streaming mode send the JSON as a string (item 3).
        ("stream_json_string", "json reply"),
        ("stream_first_item_string", "first item reply"),
        # Two Respond-to-Webhook segments (item 4).
        ("stream_multi_segment", "First part.\nSecond part."),
    ],
)
async def test_captured_reply_is_spoken(hass: HomeAssistant, fixture: str, spoken: str) -> None:
    session = fake_aiohttp_session(response=_replay(fixture))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas[0] == {"role": "assistant"}
    assert _spoken(deltas) == spoken


async def test_captured_reply_opens_exactly_one_assistant_block(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_replay("stream_multi_segment"))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert [delta for delta in deltas if "role" in delta] == [{"role": "assistant"}]


async def test_captured_error_chunk_names_the_failing_node(hass: HomeAssistant) -> None:
    # n8n 2.44.6 sends `"content": null` on a node error (item 6).
    session = fake_aiohttp_session(response=_replay("stream_error"))
    with pytest.raises(BackendStreamError) as err:
        await _collect(_adapter(hass, session), _ctx())
    assert "'C'" in str(err.value)
    assert "None" not in str(err.value)


async def test_error_chunk_with_content_keeps_it(hass: HomeAssistant) -> None:
    blob = b'{"type":"error","content":"rate limited","metadata":{"nodeName":"Agent"}}\n'
    session = fake_aiohttp_session(response=_response([blob], "application/json"))
    with pytest.raises(BackendStreamError, match="rate limited"):
        await _collect(_adapter(hass, session), _ctx())


async def test_html_document_with_doctype_still_rejected(hass: HomeAssistant) -> None:
    # A 200 login/portal page in front of n8n is a full HTML document, not a reply.
    blob = b"<!DOCTYPE html>\n<html><head><title>Sign in</title></head></html>\n"
    session = fake_aiohttp_session(response=_response([blob], "text/html; charset=utf-8"))
    with pytest.raises(BackendStreamError, match="HTML"):
        await _collect(_adapter(hass, session), _ctx())


async def test_plain_text_reply_is_spoken(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_response([b"Lights are on.\nAnything else?"], "text/plain"))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Lights are on.\nAnything else?"}]


async def test_blank_text_reply_raises(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_response([b"  \n"], "text/html; charset=utf-8"))
    with pytest.raises(BackendStreamError, match="empty"):
        await _collect(_adapter(hass, session), _ctx())


async def test_array_without_reply_field_raises(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_response([b'[{"foo":"bar"}]'], "application/json"))
    with pytest.raises(BackendStreamError, match="missing"):
        await _collect(_adapter(hass, session), _ctx())


async def test_array_uses_custom_output_field(hass: HomeAssistant) -> None:
    body = b'[{"answer":"one"},{"other":"skip"},{"answer":"two"}]'
    session = fake_aiohttp_session(response=_response([body], "application/json"))
    deltas = await _collect(_adapter(hass, session), _ctx(**{CONF_OUTPUT_FIELD: "answer"}))
    assert _spoken(deltas) == "one\ntwo"


@pytest.mark.parametrize("content", ['{"foo":1}', "{not json", "[1, 2]"])
async def test_streamed_json_like_item_without_reply_field_spoken_verbatim(hass: HomeAssistant, content: str) -> None:
    line = json.dumps({"type": "item", "content": content}).encode() + b"\n"
    session = fake_aiohttp_session(response=_response([b'{"type":"begin"}\n', line], "application/json"))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert _spoken(deltas) == content


async def test_streamed_json_item_honors_continue_conversation(hass: HomeAssistant) -> None:
    line = json.dumps({"type": "item", "content": '{"output":"More?","continueConversation":true}'}).encode()
    session = fake_aiohttp_session(response=_response([line + b"\n"], "application/json"))
    ctx = _ctx()
    deltas = await _collect(_adapter(hass, session), ctx)
    assert _spoken(deltas) == "More?"
    assert ctx.continue_conversation is True


async def test_empty_segment_adds_no_extra_separator(hass: HomeAssistant) -> None:
    blob = (
        b'{"type":"begin"}\n{"type":"item","content":"A"}\n{"type":"end"}\n'
        b'{"type":"begin"}\n{"type":"end"}\n'
        b'{"type":"begin"}\n{"type":"item","content":"B"}\n{"type":"end"}\n'
    )
    session = fake_aiohttp_session(response=_response([blob], "application/json"))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert _spoken(deltas) == "A\nB"


# --- LLMM-025: AI Agent intermediateSteps as external tool activity -----------


def _tool_events(deltas: list[Any]) -> list[tuple[Any, ...]]:
    """Flatten external tool calls/results out of a delta stream."""
    out: list[tuple[Any, ...]] = []
    for delta in deltas:
        raw = cast("dict[str, Any]", delta)
        calls = cast("list[llm.ToolInput]", raw.get("tool_calls") or [])
        out.extend(("call", c.id, c.tool_name, c.tool_args, c.external) for c in calls)
        if raw.get("role") == "tool_result":
            data = raw["result"].data if "result" in raw else raw["tool_result"]
            out.append(("result", raw["tool_call_id"], raw["tool_name"], data))
    return out


async def test_intermediate_steps_surface_as_external_tool_calls(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_replay("agent_intermediate_steps", width=64))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert _tool_events(deltas) == [
        ("call", "call_abc", "get_state", {"entity_id": "light.kitchen"}, True),
        ("result", "call_abc", "get_state", {"result": [{"state": "on"}]}),
        ("call", "call_def", "history", {"input": "kitchen"}, True),
        ("result", "call_def", "history", {"error": "Recorder unavailable", "errorType": "NodeOperationError"}),
    ]


async def test_intermediate_steps_end_with_the_spoken_reply(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_replay("agent_intermediate_steps", width=64))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas[-2:] == [{"role": "assistant"}, {"content": "The kitchen light is on."}]


@pytest.mark.skipif(
    "result" not in conversation.ToolResultContentDeltaDict.__annotations__,
    reason="the tool-result error flag needs HA >= 2026.10",
)
async def test_failed_intermediate_step_is_an_error_result(hass: HomeAssistant) -> None:
    session = fake_aiohttp_session(response=_replay("agent_intermediate_steps", width=64))
    deltas = await _collect(_adapter(hass, session), _ctx())
    raw = [cast("dict[str, Any]", delta) for delta in deltas]
    results = [delta["result"] for delta in raw if delta.get("role") == "tool_result"]
    assert [result.error for result in results] == [False, True]


async def test_malformed_intermediate_steps_ignored(hass: HomeAssistant) -> None:
    body = json.dumps({"output": "ok", "intermediateSteps": [{"action": "x"}, 5, {"observation": "1"}]}).encode()
    session = fake_aiohttp_session(response=_response([body], "application/json"))
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "ok"}]


async def test_turn_with_intermediate_steps_posts_once(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """External tool results must not re-enter the entity's HA tool loop (one POST per turn)."""
    session = fake_aiohttp_session(response=_replay("agent_intermediate_steps", width=64))
    entity = build_entity(hass, _adapter(hass, session), {})

    result = await entity._async_handle_message(make_input("is the kitchen light on?"), mock_chat_log)

    assert session.post.call_count == 1
    assert result.response.speech["plain"]["speech"] == "The kitchen light is on."


# --- LLMM-025: run-free webhook probe ------------------------------------------


def _probe_session(response: FakeStreamResponse | None = None, *, exc: Exception | None = None) -> MagicMock:
    session = MagicMock()
    session.options = MagicMock(return_value=response, side_effect=exc)
    return session


def _probe_reply(name: str) -> FakeStreamResponse:
    """A captured OPTIONS/404 probe answer (body + allow-methods header)."""
    fx = _fixture(name)
    headers: dict[str, str] = {}
    if fx["content_type"]:
        headers["Content-Type"] = fx["content_type"]
    if fx.get("access_control_allow_methods"):
        headers["Access-Control-Allow-Methods"] = fx["access_control_allow_methods"]
    return FakeStreamResponse([fx["body"].encode()], status=fx["status"], headers=headers, text=fx["body"])


async def _validate(hass: HomeAssistant, session: MagicMock, url: str, **extra: Any) -> None:
    with patch("custom_components.llm_middleman.backends.n8n.async_get_clientsession", return_value=session):
        await N8nAdapter.async_validate_connection(hass, {CONF_WEBHOOK_URL: url, **extra})


@pytest.mark.parametrize(("fixture", "url"), [("probe_registered", _PLAIN_URL), ("probe_chat_trigger", _CHAT_URL)])
async def test_probe_accepts_registered_webhook(hass: HomeAssistant, fixture: str, url: str) -> None:
    await _validate(hass, _probe_session(_probe_reply(fixture)), url)


async def test_probe_uses_options_never_get_or_post(hass: HomeAssistant) -> None:
    # GET or POST would start a run (a Chat Trigger answers GET; POST is a turn).
    session = _probe_session(_probe_reply("probe_chat_trigger"))
    await _validate(hass, session, _CHAT_URL)
    session.get.assert_not_called()
    session.post.assert_not_called()
    session.options.assert_called_once()


async def test_probe_sends_origin_but_no_request_method_header(hass: HomeAssistant) -> None:
    # n8n 2.44.6 answers 500 to an unknown path when Access-Control-Request-Method is sent.
    session = _probe_session(_probe_reply("probe_registered"))
    await _validate(hass, session, _PLAIN_URL)
    headers = session.options.call_args.kwargs["headers"]
    assert headers["Origin"] == "https://n8n.local"
    assert "Access-Control-Request-Method" not in headers


async def test_probe_sends_configured_auth(hass: HomeAssistant) -> None:
    session = _probe_session(_probe_reply("probe_registered"))
    auth = {CONF_AUTH_TYPE: N8N_AUTH_HEADER, CONF_HEADER_NAME: "X-Key", CONF_HEADER_VALUE: "v"}
    await _validate(hass, session, _PLAIN_URL, **auth)
    assert session.options.call_args.kwargs["headers"]["X-Key"] == "v"


@pytest.mark.parametrize("fixture", ["probe_unregistered", "not_registered_post"])
async def test_probe_flags_unregistered_webhook(hass: HomeAssistant, fixture: str) -> None:
    session = _probe_session(_probe_reply(fixture))
    with pytest.raises(N8nWebhookError) as err:
        await _validate(hass, session, _PLAIN_URL)
    assert err.value.error_key == "webhook_not_registered"


async def test_probe_rejects_test_url_without_a_request(hass: HomeAssistant) -> None:
    session = _probe_session(_probe_reply("probe_test_url"))
    with pytest.raises(N8nWebhookError) as err:
        await _validate(hass, session, "https://n8n.local/webhook-test/abc/chat")
    assert err.value.error_key == "webhook_test_url"
    session.options.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
async def test_probe_auth_rejection_is_invalid_auth(hass: HomeAssistant, status: int) -> None:
    with pytest.raises(BackendAuthError):
        await _validate(hass, _probe_session(FakeStreamResponse([b""], status=status)), _PLAIN_URL)


async def test_probe_transport_failure_is_cannot_connect(hass: HomeAssistant) -> None:
    session = _probe_session(exc=aiohttp.ClientConnectionError("refused"))
    with pytest.raises(BackendConnectionError):
        await _validate(hass, session, _PLAIN_URL)


@pytest.mark.parametrize(
    "response",
    [
        FakeStreamResponse([b""], status=405),  # a proxy that does not forward OPTIONS
        FakeStreamResponse([b""], status=204),  # a proxy answering CORS itself (no allow-methods)
        FakeStreamResponse([b"nope"], status=404, text="nope"),  # a 404 that is not n8n's
    ],
)
async def test_probe_unknown_answer_does_not_block_setup(hass: HomeAssistant, response: FakeStreamResponse) -> None:
    await _validate(hass, _probe_session(response), _PLAIN_URL)
