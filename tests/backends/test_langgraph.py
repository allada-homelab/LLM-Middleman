"""Tests for the LangGraph adapter (LLMM-011).

Raw ``messages-tuple`` SSE bytes are driven through the **real** ``_sse`` parser + the
adapter via the conftest harness (arbitrary chunk boundaries, CRLF), and thread mapping /
persistence is exercised against a real ``helpers.storage.Store`` backed by the
``hass_storage`` fixture.

The frame shape (``[message_chunk, metadata]`` with ``metadata.langgraph_node``) and
EOF-based termination match a live ``langgraph-api`` 0.10.0 capture (LLMM-018 E2E): a
successful run streams ``messages`` frames and closes on EOF with no terminal ``event:
end``. Mock streams here likewise terminate by byte exhaustion, never a terminal event.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
)
from custom_components.llm_middleman.backends.langgraph import (
    LangGraphAdapter,
    _extract_text,
    _parse_messages_frame,
)
from tests.conftest import _FakeStreamContent, chunk_bytes, sse_bytes

_BASE_URL = "http://lg.local:2024"
_CONN: dict[str, Any] = {"base_url": _BASE_URL, "api_key": None, "assistant_id": "agent", "entry_id": "e1"}


# --- fake aiohttp responses -------------------------------------------------


class _StreamResp:
    """Streaming response CM: ``status`` + ``content.iter_any()`` over ``chunks``."""

    def __init__(self, chunks: list[bytes], *, status: int = 200) -> None:
        self.status = status
        self.content = _FakeStreamContent(chunks)

    async def __aenter__(self) -> _StreamResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _JsonResp:
    """JSON response CM for thread creation: ``status`` + ``json()``."""

    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> object:
        return self._payload

    async def __aenter__(self) -> _JsonResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _StatusResp:
    """Bare status-only response CM (validation probes)."""

    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> _StatusResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _input(text: str = "hi") -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


def _chat_log() -> conversation.ChatLog:
    # The adapter deletes chat_log immediately (server-side history); shape is irrelevant.
    return cast("conversation.ChatLog", MagicMock())


def _msg_frame(text: str, *, node: str = "agent") -> tuple[str, str]:
    """A ``messages-tuple`` token frame: ``[message_chunk, metadata]``."""
    return ("messages", json.dumps([{"content": text, "type": "AIMessageChunk"}, {"langgraph_node": node}]))


async def _collect(gen: DeltaStream) -> list[conversation.AssistantContentDeltaDict]:
    return [delta async for delta in gen]


def _text_of(deltas: list[conversation.AssistantContentDeltaDict]) -> str:
    return "".join(delta.get("content") or "" for delta in deltas)


def _patch_client_session(mp: pytest.MonkeyPatch, session: MagicMock) -> None:
    """Point the adapter's ``async_get_clientsession`` at ``session`` (typed helper)."""

    def _get(_hass: HomeAssistant) -> MagicMock:
        return session

    mp.setattr("custom_components.llm_middleman.backends.langgraph.async_get_clientsession", _get)


def _adapter(hass: HomeAssistant, session: MagicMock, conn: dict[str, Any] | None = None) -> LangGraphAdapter:
    return LangGraphAdapter(hass, cast("aiohttp.ClientSession", session), conn if conn is not None else _CONN)


def _stateless_session(blob: bytes, *, chunk: int | list[int] = 1) -> MagicMock:
    """Session whose single ``/runs/stream`` POST streams ``blob`` in ``chunk`` pieces."""
    session = MagicMock()
    session.post = MagicMock(return_value=_StreamResp(chunk_bytes(blob, chunk)))
    return session


async def _run_stateless(
    hass: HomeAssistant, blob: bytes, *, chunk: int | list[int] = 1, options: dict[str, Any] | None = None
) -> list[conversation.AssistantContentDeltaDict]:
    session = _stateless_session(blob, chunk=chunk)
    adapter = _adapter(hass, session)
    opts: dict[str, Any] = {"stateless_runs": True, **(options or {})}
    ctx = TurnContext(options=opts, memory_key="k")
    return await _collect(adapter.stream_turn(_chat_log(), _input(), ctx))


# --- parser units -----------------------------------------------------------


def test_extract_text_string_and_blocks() -> None:
    assert _extract_text("hello") == "hello"
    assert _extract_text([{"type": "text", "text": "a"}, {"type": "tool_use"}, "b"]) == "ab"
    assert _extract_text({"unexpected": "shape"}) == ""


def test_parse_messages_frame_shapes() -> None:
    text, node = _parse_messages_frame(json.dumps([{"content": "hi"}, {"langgraph_node": "agent"}]))
    assert (text, node) == ("hi", "agent")
    # Malformed JSON and unexpected shapes are skipped, not raised.
    assert _parse_messages_frame("{not json") == (None, None)
    assert _parse_messages_frame(json.dumps({"not": "a tuple"})) == (None, None)
    # Empty token text normalizes to None (no spurious delta).
    assert _parse_messages_frame(json.dumps([{"content": ""}, {"langgraph_node": "agent"}])) == (None, "agent")


# --- streaming (stateless runs isolate the parser) --------------------------


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_happy_path_concatenates_tokens(hass: HomeAssistant, newline: bytes) -> None:
    # A successful run terminates by SSE EOF (byte exhaustion), no terminal event.
    blob = sse_bytes(_msg_frame("Hello "), _msg_frame("world"), newline=newline)
    deltas = await _run_stateless(hass, blob)
    assert deltas[0] == {"role": "assistant"}
    assert _text_of(deltas) == "Hello world"


async def test_node_filter_emits_only_matching_node(hass: HomeAssistant) -> None:
    blob = sse_bytes(
        _msg_frame("tool-chatter ", node="tools"),
        _msg_frame("Answer", node="agent"),
    )
    deltas = await _run_stateless(hass, blob, options={"response_node_filter": "agent"})
    assert deltas == [{"role": "assistant"}, {"content": "Answer"}]


async def test_error_event_raises_backend_stream_error(hass: HomeAssistant) -> None:
    blob = sse_bytes(_msg_frame("partial"), ("error", '{"error": "boom"}'))
    with pytest.raises(BackendStreamError):
        await _run_stateless(hass, blob)


async def test_non_token_only_stream_emits_nothing(hass: HomeAssistant) -> None:
    # A stream carrying no token frames (here just metadata, then EOF) yields no deltas.
    assert await _run_stateless(hass, sse_bytes(("metadata", '{"run_id": "r1"}'))) == []


async def test_eof_terminates_stream_cleanly(hass: HomeAssistant) -> None:
    # The success path: EOF (byte exhaustion) ends the run; the generator simply returns.
    deltas = await _run_stateless(hass, sse_bytes(_msg_frame("Hi")))
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_malformed_json_frame_is_skipped(hass: HomeAssistant) -> None:
    blob = sse_bytes(("messages", "{not json"), _msg_frame("ok"))
    assert _text_of(await _run_stateless(hass, blob)) == "ok"


async def test_oversized_line_drained_then_stream_continues(hass: HomeAssistant) -> None:
    # A data line beyond the reader's cap is drained and skipped (never hangs); a
    # following token frame still streams instead of the whole turn aborting.
    huge = json.dumps([{"content": "x" * 70000}, {"langgraph_node": "agent"}])
    blob = sse_bytes(("messages", huge), _msg_frame("ok"))
    assert _text_of(await _run_stateless(hass, blob)) == "ok"


async def test_non_token_events_ignored(hass: HomeAssistant) -> None:
    # metadata/values frames (non "messages") contribute no deltas.
    blob = sse_bytes(("metadata", '{"run_id": "r1"}'), _msg_frame("Hi"))
    assert _text_of(await _run_stateless(hass, blob)) == "Hi"


# --- request shape ----------------------------------------------------------


async def test_run_body_and_headers(hass: HomeAssistant) -> None:
    captured: dict[str, Any] = {}

    def post_handler(url: str, **kwargs: Any) -> _StreamResp:
        captured["url"] = url
        captured["data"] = kwargs["data"]
        captured["headers"] = kwargs["headers"]
        return _StreamResp(chunk_bytes(sse_bytes(_msg_frame("ok")), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=post_handler)
    conn = {**_CONN, "api_key": "sk-1", "assistant_id": "my-graph"}
    adapter = _adapter(hass, session, conn)
    opts: dict[str, Any] = {"stateless_runs": True, "system_prompt": "Be nice", "input_messages_key": "msgs"}

    await _collect(adapter.stream_turn(_chat_log(), _input("hi"), TurnContext(options=opts, memory_key="k")))

    assert cast("str", captured["url"]).endswith("/runs/stream")
    assert captured["headers"]["x-api-key"] == "sk-1"
    body = json.loads(cast("str", captured["data"]))
    assert body["assistant_id"] == "my-graph"
    assert body["stream_mode"] == "messages-tuple"
    assert body["input"]["msgs"] == [
        {"role": "system", "content": "Be nice"},
        {"role": "user", "content": "hi"},
    ]


async def test_no_api_key_omits_header(hass: HomeAssistant) -> None:
    captured: dict[str, Any] = {}

    def post_handler(url: str, **kwargs: Any) -> _StreamResp:
        captured["headers"] = kwargs["headers"]
        return _StreamResp(chunk_bytes(sse_bytes(_msg_frame("ok")), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=post_handler)
    adapter = _adapter(hass, session)
    await _collect(
        adapter.stream_turn(_chat_log(), _input(), TurnContext(options={"stateless_runs": True}, memory_key="k"))
    )
    assert "x-api-key" not in captured["headers"]


# --- thread mapping ---------------------------------------------------------


def _threaded_session(run_blob: bytes) -> tuple[MagicMock, dict[str, Any]]:
    """Session that creates sequential thread ids and streams ``run_blob`` per run."""
    state: dict[str, Any] = {"threads": 0, "run_urls": []}

    def post_handler(url: str, **kwargs: Any) -> _JsonResp | _StreamResp:
        if url.endswith("/threads"):
            state["threads"] += 1
            return _JsonResp({"thread_id": f"t-{state['threads']}"})
        state["run_urls"].append(url)
        return _StreamResp(chunk_bytes(run_blob, 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=post_handler)
    return session, state


async def test_device_scope_reuses_and_creates_threads(hass: HomeAssistant) -> None:
    run_blob = sse_bytes(_msg_frame("ok"))
    session, state = _threaded_session(run_blob)
    adapter = _adapter(hass, session)
    opts: dict[str, Any] = {"memory_scope": "device"}

    for key in ("dev-A", "dev-A", "dev-B"):
        await _collect(adapter.stream_turn(_chat_log(), _input(), TurnContext(options=opts, memory_key=key)))

    assert state["threads"] == 2  # dev-A -> t-1 (reused once), dev-B -> t-2
    assert state["run_urls"] == [
        f"{_BASE_URL}/threads/t-1/runs/stream",
        f"{_BASE_URL}/threads/t-1/runs/stream",
        f"{_BASE_URL}/threads/t-2/runs/stream",
    ]


async def test_stateless_toggle_never_creates_thread(hass: HomeAssistant) -> None:
    session = _stateless_session(sse_bytes(_msg_frame("ok")), chunk=8)
    adapter = _adapter(hass, session)
    await _collect(
        adapter.stream_turn(_chat_log(), _input(), TurnContext(options={"stateless_runs": True}, memory_key="k"))
    )
    posted = [call.args[0] for call in session.post.call_args_list]
    assert posted == [f"{_BASE_URL}/runs/stream"]
    assert all(not url.endswith("/threads") for url in posted)


async def test_rejected_thread_is_recreated(hass: HomeAssistant) -> None:
    run_blob = sse_bytes(_msg_frame("recovered"))
    state: dict[str, Any] = {"threads": 0}

    def post_handler(url: str, **kwargs: Any) -> _JsonResp | _StreamResp:
        if url.endswith("/threads"):
            state["threads"] += 1
            return _JsonResp({"thread_id": f"t-{state['threads']}"})
        if "/threads/t-1/" in url:  # first thread rejected server-side
            return _StreamResp([], status=404)
        return _StreamResp(chunk_bytes(run_blob, 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=post_handler)
    adapter = _adapter(hass, session)
    deltas = await _collect(
        adapter.stream_turn(_chat_log(), _input(), TurnContext(options={"memory_scope": "device"}, memory_key="dev-A"))
    )

    assert state["threads"] == 2  # t-1 rejected, t-2 created and used
    assert _text_of(deltas) == "recovered"


async def test_run_http_error_raises_backend_stream_error(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.post = MagicMock(return_value=_StreamResp([], status=500))
    adapter = _adapter(hass, session)
    with pytest.raises(BackendStreamError):
        await _collect(
            adapter.stream_turn(_chat_log(), _input(), TurnContext(options={"stateless_runs": True}, memory_key="k"))
        )


# --- persistence across restarts --------------------------------------------


async def test_device_map_persists_across_restart(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    run_blob = sse_bytes(_msg_frame("ok"))

    session1, state1 = _threaded_session(run_blob)
    adapter1 = _adapter(hass, session1)
    await _collect(
        adapter1.stream_turn(_chat_log(), _input(), TurnContext(options={"memory_scope": "device"}, memory_key="dev-A"))
    )
    assert state1["threads"] == 1

    # The map is written to persistent storage.
    stored = hass_storage["llm_middleman.langgraph.e1"]["data"]
    assert stored == {"dev-A": "t-1"}

    # "Restart": a fresh adapter instance (same entry) reloads the map and reuses the thread.
    session2, state2 = _threaded_session(run_blob)
    adapter2 = _adapter(hass, session2)
    await _collect(
        adapter2.stream_turn(_chat_log(), _input(), TurnContext(options={"memory_scope": "device"}, memory_key="dev-A"))
    )
    assert state2["threads"] == 0  # no new thread created
    assert state2["run_urls"] == [f"{_BASE_URL}/threads/t-1/runs/stream"]


async def test_conversation_scope_is_not_persisted(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    run_blob = sse_bytes(_msg_frame("ok"))
    session, _state = _threaded_session(run_blob)
    adapter = _adapter(hass, session)
    # Default scope is conversation.
    await _collect(adapter.stream_turn(_chat_log(), _input(), TurnContext(options={}, memory_key="conv-1")))
    assert "llm_middleman.langgraph.e1" not in hass_storage


# --- connection validation --------------------------------------------------


async def test_validate_ok_endpoint_succeeds(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_StatusResp(200))
    with pytest.MonkeyPatch.context() as mp:
        _patch_client_session(mp, session)
        await LangGraphAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "sk-1"})
    assert session.get.call_args.kwargs["headers"]["x-api-key"] == "sk-1"


async def test_validate_falls_back_to_assistants_search(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_StatusResp(404))
    session.post = MagicMock(return_value=_StatusResp(200))
    with pytest.MonkeyPatch.context() as mp:
        _patch_client_session(mp, session)
        await LangGraphAdapter.async_validate_connection(hass, {"base_url": _BASE_URL})
    assert session.post.call_args.args[0] == f"{_BASE_URL}/assistants/search"


async def test_validate_auth_error(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_StatusResp(401))
    with pytest.MonkeyPatch.context() as mp:
        _patch_client_session(mp, session)
        with pytest.raises(BackendAuthError):
            await LangGraphAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "bad"})


async def test_validate_fallback_failure_is_connection_error(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_StatusResp(404))
    session.post = MagicMock(return_value=_StatusResp(500))
    with pytest.MonkeyPatch.context() as mp:
        _patch_client_session(mp, session)
        with pytest.raises(BackendConnectionError):
            await LangGraphAdapter.async_validate_connection(hass, {"base_url": _BASE_URL})


async def test_validate_transport_error_is_connection_error(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("no route"))
    with pytest.MonkeyPatch.context() as mp:
        _patch_client_session(mp, session)
        with pytest.raises(BackendConnectionError):
            await LangGraphAdapter.async_validate_connection(hass, {"base_url": _BASE_URL})
