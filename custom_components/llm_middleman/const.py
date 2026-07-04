"""Constants for the LLM Middleman integration."""

DOMAIN = "llm_middleman"

# Config entry data keys
CONF_URL = "url"
CONF_TOKEN = "token"  # noqa: S105 — config-key name, not a secret value
CONF_NAME = "name"
CONF_SYSTEM_PROMPT = "system_prompt"

# v1 parent-entry connection keys (LLMM-006 config flow). The backend type selected
# in step 1 keys BACKEND_TO_CLS and routes to that backend's connection step.
CONF_BACKEND_TYPE = "backend_type"
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_ASSISTANT_ID = "assistant_id"
CONF_WEBHOOK_URL = "webhook_url"
CONF_TARGET_TYPE = "target_type"
CONF_AUTH_TYPE = "auth_type"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105 — config-key name, not a secret value
CONF_HEADER_NAME = "header_name"
CONF_HEADER_VALUE = "header_value"

# Backend-type values: each equals the SelectSelector option and the config-flow
# connection step suffix (async_step_<value>), and each adapter's `backend_type`.
BACKEND_OPENAI_COMPAT = "openai_compat"
BACKEND_LANGGRAPH = "langgraph"
BACKEND_OLLAMA = "ollama"
BACKEND_CONVERSE = "converse"
BACKEND_N8N = "n8n"

# n8n connection choices (CONF_TARGET_TYPE / CONF_AUTH_TYPE option values).
N8N_TARGET_CHAT_TRIGGER = "chat_trigger"
N8N_TARGET_WEBHOOK = "webhook"
N8N_AUTH_NONE = "none"
N8N_AUTH_BASIC = "basic"
N8N_AUTH_HEADER = "custom_header"

# v1 per-agent subentry option keys (LLMM-008 OpenAI-compatible agent options).
CONF_MODEL = "model"
CONF_MAX_HISTORY = "max_history"
CONF_TEMPERATURE = "temperature"
CONF_TOP_P = "top_p"
CONF_MAX_TOKENS = "max_tokens"

# The streaming converse endpoint appended to the configured base URL.
CONVERSE_PATH = "/v1/converse"

# Per-turn deadline for the whole SSE response. Voice UX is degraded past this,
# so we fall back rather than hang the pipeline.
DEFAULT_TIMEOUT = 60

# --- v1 re-architecture (LLMM-005) ---

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

# --- v1 backend config keys (parent entry + agent subentry) ---
# Backend-type discriminators for the BACKEND_TO_CLS factory.
BACKEND_OLLAMA = "ollama"

# Parent-entry connection keys (shared across presets; string values mirror the
# plan's per-connector matrix and the test harness literals).
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"

# Agent-subentry option keys.
CONF_MODEL = "model"
CONF_MAX_HISTORY = "max_history"

# Ollama-native option keys (core-ollama option set).
CONF_NUM_CTX = "num_ctx"
CONF_KEEP_ALIVE = "keep_alive"
CONF_THINK = "think"

# keep_alive sentinel: -1 means "keep the model loaded forever" and must be sent as
# the integer -1, not a duration string ("-1s" is an invalid negative duration).
KEEP_ALIVE_FOREVER = -1
