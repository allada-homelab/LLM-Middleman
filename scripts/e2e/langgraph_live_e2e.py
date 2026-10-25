# A pytest module outside tests/: asserts are pytest's mechanism (the tests/ S101 ignore), and
# fixtures imported from tests.conftest are "redefined" by the test parameters that use them.
# ruff: noqa: S101, F811
"""Live E2E for the LangGraph preset (LLMM-022): the real adapter against a running server.

Not part of the unit suite (``testpaths = ["tests"]``) and skipped unless
``LANGGRAPH_E2E_URL`` is set. Serve the rig's graphs and run::

    cp -r scripts/e2e/langgraph /tmp/lg-e2e && cd /tmp/lg-e2e   # langgraph dev writes state into cwd
    uvx --python 3.12 --from "langgraph-cli[inmem]" --with langchain-core \\
        langgraph dev --port 2025 --no-browser --no-reload
    # in the repo:
    LANGGRAPH_E2E_URL=http://127.0.0.1:2025 \\
        uv run python -m pytest scripts/e2e/langgraph_live_e2e.py -v -s

Each test prints one ``E2E <row>: {...}`` line with the observed facts; results are
recorded in ``docs/implementation/e2e-results/langgraph-tier1.md``. The slow-step rows take
~40 s each (the ``slow`` graph is silent for 40 s; the slow-step row sets a 30 s agent
timeout, so only the server's heartbeats keep it alive).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends.base import TurnContext
from custom_components.llm_middleman.backends.langgraph import AssistantNotFoundError, LangGraphAdapter
from custom_components.llm_middleman.const import CONF_TIMEOUT

# The suite's fixtures, registered for this module (python -m pytest puts the repo on sys.path).
from tests.conftest import (  # noqa: F401
    MockChatLog,
    auto_enable_custom_integrations,
    build_entity,
    mock_chat_log,
    setup_ha_components,
)

URL = os.environ.get("LANGGRAPH_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not URL, reason="set LANGGRAPH_E2E_URL to a langgraph dev serving scripts/e2e/langgraph"
)

ANSWER = "It is sunny and 21C in Paris."
SLOW_ANSWER = "Done after a slow step."


def report(row: str, **facts: Any) -> None:
    print(f"\nE2E {row}: {json.dumps(facts, default=str)}")


@pytest.fixture
async def http(socket_enabled: None) -> AsyncGenerator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def make_adapter(hass: HomeAssistant, http: aiohttp.ClientSession, graph: str) -> LangGraphAdapter:
    # A fresh entry id per adapter -> fresh deterministic threads per test run.
    return LangGraphAdapter(hass, http, {"base_url": URL, "assistant_id": graph, "entry_id": f"e2e-{uuid.uuid4()}"})


def chat_log(prompt: str = "") -> MagicMock:
    log = MagicMock()
    log.content = [conversation.SystemContent(content=prompt)]
    log.conversation_id = "e2e-conv"
    return log


async def turn(
    adapter: LangGraphAdapter,
    text: str,
    *,
    key: str = "session-1",
    options: dict[str, Any] | None = None,
    log: MagicMock | None = None,
    **input_kwargs: Any,
) -> tuple[list[conversation.AssistantContentDeltaDict], TurnContext]:
    ctx = TurnContext(options=options or {}, memory_key=key)
    user_input = conversation.ConversationInput(
        text=text,
        context=input_kwargs.pop("context", Context()),
        conversation_id="e2e-conv",
        device_id=input_kwargs.pop("device_id", None),
        satellite_id=input_kwargs.pop("satellite_id", None),
        language="en",
        agent_id="conversation.e2e",
    )
    stream = adapter.stream_turn(cast("conversation.ChatLog", log or chat_log()), user_input, ctx)
    return [delta async for delta in stream], ctx


def spoken(deltas: list[conversation.AssistantContentDeltaDict]) -> str:
    return "".join(delta.get("content") or "" for delta in deltas)


async def api(http: aiohttp.ClientSession, method: str, path: str, body: object = None) -> Any:
    async with http.request(method, f"{URL}{path}", json=body) as resp:
        return {"status": resp.status} if resp.status == 204 else await resp.json()


async def thread_messages(http: aiohttp.ClientSession, thread_id: str) -> list[dict[str, Any]]:
    state = await api(http, "GET", f"/threads/{thread_id}/state")
    return cast("list[dict[str, Any]]", (state.get("values") or {}).get("messages", []))


async def wait_for_run_id(adapter: LangGraphAdapter, previous: str | None = None) -> str:
    for _ in range(200):
        if adapter.last_run_id not in (None, previous):
            return cast("str", adapter.last_run_id)
        await asyncio.sleep(0.05)
    raise AssertionError("run never started")


async def test_tokens_stream_and_tool_output_is_not_spoken(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    log = chat_log()
    deltas, _ = await turn(make_adapter(hass, http, "react"), "weather?", log=log)
    calls = [call for delta in deltas for call in delta.get("tool_calls") or []]
    content_deltas = [d for d in deltas if d.get("content")]
    report(
        "tokens+tools",
        spoken=spoken(deltas),
        content_deltas=len(content_deltas),
        tool_calls=[(c.tool_name, c.tool_args, c.external) for c in calls],
        tool_results=sum(1 for d in deltas if d.get("role") == "tool_result"),
        trace=log.async_trace.call_args.args[0],
    )
    assert spoken(deltas) == ANSWER
    assert len(content_deltas) == 5  # streamed token by token
    assert [(c.tool_name, c.external) for c in calls] == [("get_weather", True)]
    log.async_trace.assert_called_once_with({"stats": {"input_tokens": 34, "output_tokens": 12}})


async def test_prompt_and_context_arrive_once_per_turn(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    adapter = make_adapter(hass, http, "ctx")
    replies = []
    for index in range(3):
        deltas, _ = await turn(
            adapter,
            f"turn {index}",
            log=chat_log("Be brief."),
            device_id="dev-1",
            satellite_id="sat-1",
            context=Context(user_id="user-1"),
        )
        replies.append(spoken(deltas))
    messages = await thread_messages(http, await adapter._thread_id("session-1"))
    roles = [m["type"] for m in messages]
    report("prompt-once", replies=replies, roles_in_thread=roles)
    assert roles.count("system") <= 1
    assert roles.count("human") == 3
    assert all("prompt='Be brief.'" in reply and "sys_in_state=0" in reply for reply in replies)
    assert all("lang=en dev=dev-1 sat=sat-1 conv=e2e-conv user=user-1" in reply for reply in replies)


async def test_interrupt_then_resume(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    adapter = make_adapter(hass, http, "interrupt")
    asked, ctx1 = await turn(adapter, "lights on")
    thread_id = await adapter._thread_id("session-1")
    status_between = (await api(http, "GET", f"/threads/{thread_id}"))["status"]
    answered, ctx2 = await turn(adapter, "kitchen")
    status_after = (await api(http, "GET", f"/threads/{thread_id}"))["status"]
    report(
        "interrupt->resume",
        asked=spoken(asked),
        continue_conversation=ctx1.continue_conversation,
        status_between=status_between,
        answered=spoken(answered),
        status_after=status_after,
    )
    assert spoken(asked) == "Which room should I turn the lights on in?"
    assert ctx1.continue_conversation is True
    assert spoken(answered) == "Turning on the lights in the kitchen."
    assert (status_between, status_after, ctx2.continue_conversation) == ("interrupted", "idle", False)


async def test_double_text_rolls_back_the_superseded_run(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    adapter = make_adapter(hass, http, "slow")
    first = asyncio.create_task(turn(adapter, "first"))
    first_run = await wait_for_run_id(adapter)
    started = time.monotonic()
    second, _ = await turn(adapter, "second")
    elapsed = time.monotonic() - started
    superseded, _ = await first
    thread_id = await adapter._thread_id("session-1")
    humans = [m["content"] for m in await thread_messages(http, thread_id) if m["type"] == "human"]
    first_status = (await api(http, "GET", f"/threads/{thread_id}/runs/{first_run}")).get("status")
    report(
        "double-text",
        second=spoken(second),
        second_seconds=round(elapsed, 1),
        superseded_deltas=superseded,
        first_run_status=first_status,
        human_messages=humans,
    )
    assert spoken(second) == SLOW_ANSWER
    assert superseded == []  # the entity guard voices the fallback for the superseded turn
    assert humans == ["second"]


async def test_cancelled_turn_cancels_the_server_run(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    adapter = make_adapter(hass, http, "slow")
    task = asyncio.create_task(turn(adapter, "go"))
    run_id = await wait_for_run_id(adapter)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()
    thread_id = await adapter._thread_id("session-1")
    status = "pending"
    for _ in range(40):
        status = (await api(http, "GET", f"/threads/{thread_id}/runs/{run_id}"))["status"]
        if status != "running":
            break
        await asyncio.sleep(0.1)
    report("cancel", run_id=run_id, run_status=status)
    assert status == "interrupted"


async def test_thread_is_recreated_after_delete(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    adapter = make_adapter(hass, http, "agent")
    first, _ = await turn(adapter, "one")
    thread_id = await adapter._thread_id("session-1")
    deleted = await api(http, "DELETE", f"/threads/{thread_id}")
    second, _ = await turn(adapter, "two")
    humans = [m["content"] for m in await thread_messages(http, thread_id) if m["type"] == "human"]
    report("recreate-after-delete", first=spoken(first), delete=deleted, second=spoken(second), humans=humans)
    assert (spoken(first), spoken(second)) == ("echo:one", "echo:two")
    assert humans == ["two"]


async def test_slow_silent_step_outlives_idle_timeout(hass: HomeAssistant, http: aiohttp.ClientSession) -> None:
    started = time.monotonic()
    # A 30 s agent timeout bounds each silence; the 40 s step outlives it only via heartbeats.
    deltas, _ = await turn(make_adapter(hass, http, "slow"), "go", options={CONF_TIMEOUT: 30})
    elapsed = time.monotonic() - started
    report("slow-step", spoken=spoken(deltas), seconds=round(elapsed, 1))
    assert spoken(deltas) == SLOW_ANSWER
    assert elapsed > 30  # longer than the agent timeout (sock_read): heartbeats kept it alive


async def test_assistant_validation(hass: HomeAssistant, socket_enabled: None) -> None:
    await LangGraphAdapter.async_validate_connection(hass, {"base_url": URL, "assistant_id": "react"})
    found = await _search(hass, "react")
    await LangGraphAdapter.async_validate_connection(hass, {"base_url": URL, "assistant_id": found})
    with pytest.raises(AssistantNotFoundError):
        await LangGraphAdapter.async_validate_connection(hass, {"base_url": URL, "assistant_id": "nope"})
    report("assistant-check", graph_id="ok", uuid=found, unknown="AssistantNotFoundError")


async def _search(hass: HomeAssistant, graph: str) -> str:
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{URL}/assistants/search", json={"graph_id": graph, "limit": 1}) as resp,
    ):
        return cast("str", (await resp.json())[0]["assistant_id"])


async def test_tool_turn_through_entity_posts_once(
    hass: HomeAssistant, http: aiohttp.ClientSession, mock_chat_log: MockChatLog
) -> None:
    adapter = make_adapter(hass, http, "react")
    posts: list[str] = []
    real_post = http.post

    def spy(url: str, **kwargs: Any) -> Any:
        posts.append(url)
        return real_post(url, **kwargs)

    adapter.session = cast("aiohttp.ClientSession", MagicMock(post=spy))
    entity = build_entity(hass, adapter, {})
    user_input = conversation.ConversationInput(
        text="weather?",
        context=Context(),
        conversation_id=mock_chat_log.conversation_id,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.test",
    )
    result = await entity._async_handle_message(user_input, mock_chat_log)
    roles = [c.role for c in mock_chat_log.content]
    report("entity-one-post", run_posts=len(posts), speech=result.response.speech["plain"]["speech"], roles=roles)
    assert len(posts) == 1
    assert result.response.speech["plain"]["speech"] == ANSWER
    assert roles[-3:] == ["assistant", "tool_result", "assistant"]
