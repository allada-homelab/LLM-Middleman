"""AI Task platform for LLM Middleman (LLMM-023): ``ai_task.generate_data``.

One ``AITaskEntity`` per ``ai_task_data`` subentry, offered only for adapters with
``supports_ai_task`` (the stateless-replay presets that own the model call). HA prepares
the chat log (system prompt, the task instructions plus any attachments, and the task's
LLM API); this entity runs the adapter on it — inside the shared bounded tool loop when
``task.llm_api`` is set — and returns the last assistant text, or its JSON parse when the
task has a ``structure``. The structure travels to the adapter as a rendered JSON Schema
on ``TurnContext.output_schema``, so conversation and AI Task requests share one
request-building path. Unlike a conversation turn there is no spoken fallback: any
failure raises a translated ``HomeAssistantError`` to the calling automation.
"""

from __future__ import annotations

import logging
from json import JSONDecodeError
from typing import Any

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify
from homeassistant.util.json import json_loads

from . import LLMMiddlemanConfigEntry
from .backends._compat import schema_to_openapi
from .backends.base import BackendAdapter, OutputSchema, TurnContext
from .const import DOMAIN, MAX_TOOL_ITERATIONS, SUBENTRY_TYPE_AI_TASK
from .helpers import async_record_backend_failure, async_record_backend_reached, async_run_tool_loop

_LOGGER = logging.getLogger(__name__)

# Tasks are independent backend round-trips; never serialize them (core convention).
PARALLEL_UPDATES = 0

# Log at most this much of an unparsable structured reply.
_LOGGED_RESPONSE_CHARS = 1000


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LLMMiddlemanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one AI task entity per ``ai_task_data`` subentry."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_AI_TASK:
            continue
        async_add_entities(
            [LLMMiddlemanTaskEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class LLMMiddlemanTaskEntity(ai_task.AITaskEntity):
    """Runs ``generate_data`` tasks through the connection's backend adapter."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    def __init__(self, entry: LLMMiddlemanConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity for one AI task subentry."""
        self.entry = entry
        self.subentry = subentry
        self.adapter: BackendAdapter = entry.runtime_data
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="LLM Middleman",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Run the task on the prepared chat log and return its text or parsed JSON."""
        ctx = TurnContext(
            options=self.subentry.data,
            memory_key=chat_log.conversation_id,
            output_schema=_output_schema(task, chat_log),
            subentry_id=self.subentry.subentry_id,
        )
        # Stateless-replay adapters build the request from chat_log and never read the
        # turn input; stream_turn's contract still takes one.
        user_input = conversation.ConversationInput(
            text=task.instructions,
            context=Context(),
            conversation_id=chat_log.conversation_id,
            device_id=None,
            satellite_id=None,
            language=self.hass.config.language,
            agent_id=self.entity_id,
        )

        async def _run_round() -> None:
            async for _content in chat_log.async_add_delta_content_stream(
                self.entity_id, self.adapter.stream_turn(chat_log, user_input, ctx)
            ):
                pass

        try:
            completed = await async_run_tool_loop(chat_log, _run_round)
        except Exception as err:
            if isinstance(err, HomeAssistantError) and err.translation_domain == DOMAIN:
                raise  # already user-facing (e.g. a non-image attachment)
            async_record_backend_failure(self.hass, self.entry, self.adapter, err)
            raise _backend_error(err) from err
        async_record_backend_reached(self.hass, self.entry, self.adapter)

        if not completed:
            _LOGGER.warning(
                "AI task tool loop hit MAX_TOOL_ITERATIONS (%d) without a final answer", MAX_TOOL_ITERATIONS
            )
        last = chat_log.content[-1]
        if not completed or not isinstance(last, conversation.AssistantContent):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="ai_task_no_response")

        text = last.content or ""
        if task.structure is None:
            return ai_task.GenDataTaskResult(conversation_id=chat_log.conversation_id, data=text)
        try:
            data = json_loads(text)
        except JSONDecodeError as err:
            _LOGGER.error(
                "Failed to parse the AI task's structured response: %s. Response: %s",
                err,
                text[:_LOGGED_RESPONSE_CHARS],
            )
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="ai_task_json_parse_error") from err
        return ai_task.GenDataTaskResult(conversation_id=chat_log.conversation_id, data=data)


def _output_schema(task: ai_task.GenDataTask, chat_log: conversation.ChatLog) -> OutputSchema | None:
    """Render ``task.structure`` (voluptuous on HA < 2026.9, probatio after) to JSON Schema.

    Structure fields are HA selectors, so the selector serializer renders them (core
    ollama/openai use the chat log's API serializer when one is set, the same default).
    """
    if task.structure is None:
        return None
    serializer = chat_log.llm_api.custom_serializer if chat_log.llm_api else llm.selector_serializer
    schema: dict[str, Any] = schema_to_openapi(task.structure, serializer)
    # OpenAI requires ^[a-zA-Z0-9_-]{1,64}$ for json_schema.name.
    return OutputSchema(name=slugify(task.name)[:64] or "response", schema=schema)


def _backend_error(err: Exception) -> HomeAssistantError:
    """Wrap a failed backend round-trip as the translated AI Task error."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="ai_task_backend_error",
        translation_placeholders={"error": str(err) or type(err).__name__},
    )
