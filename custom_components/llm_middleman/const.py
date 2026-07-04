"""Constants for the LLM Middleman integration."""

DOMAIN = "llm_middleman"

# Config entry data keys
CONF_URL = "url"
CONF_TOKEN = "token"  # noqa: S105 — config-key name, not a secret value
CONF_NAME = "name"
CONF_SYSTEM_PROMPT = "system_prompt"

# Backend-type keys (parent-entry `backend_type`; factory keys in BACKEND_TO_CLS).
BACKEND_OPENAI_COMPAT = "openai_compat"

# v1 parent-entry connection keys (shared across backend presets).
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"

# v1 per-agent subentry option keys.
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

# Shown to the user (and spoken via TTS) when the external agent is unreachable
# or errors mid-turn, so the Assist pipeline never hangs.
ERROR_MESSAGE = "Sorry, I could not reach the assistant right now. Please try again."
