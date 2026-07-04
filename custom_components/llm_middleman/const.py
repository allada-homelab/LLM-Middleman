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
