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

# --- n8n backend (LLMM-012) ---
# Registered backend-type key (BACKEND_TO_CLS) and config-flow dropdown value.
BACKEND_N8N = "n8n"

# Parent-entry connection keys. The webhook URL is the opaque full production chat
# URL (``/webhook/<id>/chat``) the user pastes verbatim; no path is appended.
CONF_WEBHOOK_URL = "webhook_url"
CONF_TARGET_TYPE = "target_type"
TARGET_CHAT_TRIGGER = "chat_trigger"  # Chat Trigger node — request carries ``action``
TARGET_PLAIN_WEBHOOK = "plain_webhook"  # plain Webhook + Respond-to-Webhook — no ``action``
CONF_N8N_AUTH_TYPE = "n8n_auth_type"
N8N_AUTH_NONE = "none"
N8N_AUTH_BASIC = "basic"
N8N_AUTH_HEADER = "header"
CONF_N8N_USERNAME = "n8n_username"
CONF_N8N_PASSWORD = "n8n_password"  # noqa: S105 — config-key name, not a secret value
CONF_N8N_HEADER_NAME = "n8n_header_name"
CONF_N8N_HEADER_VALUE = "n8n_header_value"

# Per-agent (subentry) option keys. ``CONF_STREAMING`` is a config-flow help-text
# toggle only: the adapter branches on the actual response, never on this toggle.
CONF_STREAMING = "streaming"
CONF_INPUT_FIELD = "input_field"
CONF_OUTPUT_FIELD = "output_field"
CONF_SESSION_FIELD = "session_field"
CONF_TIMEOUT = "timeout"

# n8n field defaults (n8n 1.103.0). The output field falls back to ``text`` when the
# configured field is absent; a fully missing reply surfaces an error, never raw JSON.
N8N_DEFAULT_INPUT_FIELD = "chatInput"
N8N_DEFAULT_OUTPUT_FIELD = "output"
N8N_OUTPUT_FIELD_FALLBACK = "text"
N8N_DEFAULT_SESSION_FIELD = "sessionId"
# n8n workflows can be slow; lower than the global default so voice UX fails soft sooner.
N8N_DEFAULT_TIMEOUT = 30
