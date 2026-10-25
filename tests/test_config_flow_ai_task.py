"""AI Task subentry flow (LLMM-023): offered per adapter capability, with a model/options-only form."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.backends.ollama import OllamaAdapter
from custom_components.llm_middleman.backends.openai_compat import OpenAICompatAdapter
from custom_components.llm_middleman.config_flow import (
    AITaskSubentryFlowHandler,
    ConversationSubentryFlowHandler,
    LLMMiddlemanConfigFlow,
)
from custom_components.llm_middleman.const import (
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_BACKEND_TYPE,
    CONF_KEEP_ALIVE,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_NUM_CTX,
    CONF_TEMPERATURE,
    CONF_THINK,
    CONF_TIMEOUT,
    CONF_TOP_P,
    DOMAIN,
    SUBENTRY_TYPE_AI_TASK,
    SUBENTRY_TYPE_CONVERSATION,
)

from .live_backend import PRESETS
from .test_config_flow import _parent_entry, _schema_keys

AI_TASK_PRESETS = {BACKEND_OPENAI_COMPAT, BACKEND_OLLAMA}


async def _init_ai_task(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Start the add-AI-task (user) subentry flow, returning a plain dict."""
    result = await hass.config_entries.subentries.async_init(  # pyright: ignore[reportUnknownMemberType]
        (entry.entry_id, SUBENTRY_TYPE_AI_TASK),
        context={"source": config_entries.SOURCE_USER},
    )
    return dict(result)


@pytest.mark.parametrize("backend_type", sorted(AI_TASK_PRESETS))
def test_ai_task_offered_for_capable_presets(backend_type: str) -> None:
    """openai_compat and ollama offer "Add AI task" next to the conversation agent."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_BACKEND_TYPE: backend_type})

    types = LLMMiddlemanConfigFlow.async_get_supported_subentry_types(entry)  # type: ignore[arg-type]

    assert types == {
        SUBENTRY_TYPE_CONVERSATION: ConversationSubentryFlowHandler,
        SUBENTRY_TYPE_AI_TASK: AITaskSubentryFlowHandler,
    }


@pytest.mark.parametrize("backend_type", sorted(set(PRESETS) - AI_TASK_PRESETS))
def test_ai_task_not_offered_for_forwarding_presets(backend_type: str) -> None:
    """Presets whose backend owns the prompt and output shape offer conversation agents only."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_BACKEND_TYPE: backend_type})

    types = LLMMiddlemanConfigFlow.async_get_supported_subentry_types(entry)  # type: ignore[arg-type]

    assert types == {SUBENTRY_TYPE_CONVERSATION: ConversationSubentryFlowHandler}


def test_unmigrated_v0_entry_offers_conversation_only() -> None:
    """A v0 entry (no backend type yet, e.g. a failed migration) still lists without raising."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data={"url": "http://x", "token": "t"})

    types = LLMMiddlemanConfigFlow.async_get_supported_subentry_types(entry)  # type: ignore[arg-type]

    assert types == {SUBENTRY_TYPE_CONVERSATION: ConversationSubentryFlowHandler}


async def test_create_openai_ai_task_subentry(hass: HomeAssistant) -> None:
    """The openai_compat form has name, model, timeout and the sampling options — no prompt."""
    entry = _parent_entry(hass, BACKEND_OPENAI_COMPAT)
    with patch.object(OpenAICompatAdapter, "async_list_models", return_value=["m1"]):
        form = await _init_ai_task(hass, entry)
        assert _schema_keys(form["data_schema"]) == {
            CONF_NAME,
            CONF_MODEL,
            CONF_TIMEOUT,
            CONF_TEMPERATURE,
            CONF_TOP_P,
            CONF_MAX_TOKENS,
        }

        result = dict(
            await hass.config_entries.subentries.async_configure(  # pyright: ignore[reportUnknownMemberType]
                form["flow_id"], {CONF_NAME: "Summaries", CONF_MODEL: "m1", CONF_TEMPERATURE: 0.2}
            )
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Summaries"
    assert result["data"] == {CONF_MODEL: "m1", CONF_TEMPERATURE: 0.2}


async def test_ollama_ai_task_form_fields(hass: HomeAssistant) -> None:
    """The ollama form carries Ollama's runtime options instead of the sampling ones."""
    entry = _parent_entry(hass, BACKEND_OLLAMA)
    with patch.object(OllamaAdapter, "async_list_models", return_value=["llama3"]):
        form = await _init_ai_task(hass, entry)

    assert _schema_keys(form["data_schema"]) == {
        CONF_NAME,
        CONF_MODEL,
        CONF_TIMEOUT,
        CONF_NUM_CTX,
        CONF_KEEP_ALIVE,
        CONF_THINK,
    }


async def test_reconfigure_ai_task_subentry(hass: HomeAssistant) -> None:
    """Reconfigure prefills the stored model, drops the name field, and saves the new options."""
    entry = _parent_entry(
        hass,
        BACKEND_OPENAI_COMPAT,
        subentries_data=[
            {"subentry_type": SUBENTRY_TYPE_AI_TASK, "data": {CONF_MODEL: "m1"}, "title": "Task", "unique_id": None}
        ],
    )
    subentry_id = next(iter(entry.subentries))
    with patch.object(OpenAICompatAdapter, "async_list_models", return_value=["m1", "m2"]):
        form: dict[str, Any] = dict(await entry.start_subentry_reconfigure_flow(hass, subentry_id))
        assert CONF_NAME not in _schema_keys(form["data_schema"])
        result = dict(
            await hass.config_entries.subentries.async_configure(  # pyright: ignore[reportUnknownMemberType]
                form["flow_id"], {CONF_MODEL: "m2"}
            )
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data == {CONF_MODEL: "m2"}
    assert entry.subentries[subentry_id].title == "Task"


async def test_ai_task_subentry_aborts_when_parent_not_loaded(hass: HomeAssistant) -> None:
    """An AI task can't be added under a connection that failed to load."""
    entry = _parent_entry(hass, BACKEND_OLLAMA)
    entry.mock_state(hass, config_entries.ConfigEntryState.SETUP_RETRY)

    result = await _init_ai_task(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
