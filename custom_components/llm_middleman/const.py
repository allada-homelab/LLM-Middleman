"""Constants for the LLM Middleman integration."""

DOMAIN = "llm_middleman"

# Config entry data keys
CONF_URL = "url"
CONF_TOKEN = "token"  # noqa: S105 — config-key name, not a secret value
CONF_NAME = "name"
CONF_SYSTEM_PROMPT = "system_prompt"

# The streaming converse endpoint appended to the configured base URL.
CONVERSE_PATH = "/v1/converse"

# Per-turn deadline for the whole SSE response. Voice UX is degraded past this,
# so we fall back rather than hang the pipeline.
DEFAULT_TIMEOUT = 60

# --- v1 re-architecture (LLMM-005) ---

# Parent config-entry data key selecting the backend preset (BACKEND_TO_CLS key).
CONF_BACKEND_TYPE = "backend_type"

# Per-agent (conversation subentry) option keys.
CONF_TIMEOUT = "timeout"
CONF_MEMORY_SCOPE = "memory_scope"

# memory_scope values — control the session key the entity derives per turn and
# hands stateful adapters via ``TurnContext.memory_key``.
MEMORY_SCOPE_CONVERSATION = "conversation"  # default: HA conversation_id (session TTL)
MEMORY_SCOPE_DEVICE = "device"  # per-satellite/room; falls back to conversation
MEMORY_SCOPE_AGENT = "agent"  # one global thread per agent subentry

# Subentry type for a conversation agent (one entity per such subentry).
SUBENTRY_TYPE_CONVERSATION = "conversation"

# sock_read idle timeout: kill only a truly stalled stream, not a slow-but-alive
# one (the v0 single-total-deadline bug). Paired with CONF_TIMEOUT as the total.
IDLE_TIMEOUT = 30

# Shown to the user (and spoken via TTS) when the external agent is unreachable
# or errors mid-turn, so the Assist pipeline never hangs.
ERROR_MESSAGE = "Sorry, I could not reach the assistant right now. Please try again."
