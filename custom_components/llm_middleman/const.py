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

# The streaming converse endpoint appended to the configured base URL.
CONVERSE_PATH = "/v1/converse"

# Per-turn deadline for the whole SSE response. Voice UX is degraded past this,
# so we fall back rather than hang the pipeline.
DEFAULT_TIMEOUT = 60

# Shown to the user (and spoken via TTS) when the external agent is unreachable
# or errors mid-turn, so the Assist pipeline never hangs.
ERROR_MESSAGE = "Sorry, I could not reach the assistant right now. Please try again."
