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

# --- LangGraph backend (LLMM-011) -------------------------------------------
# backend_type key registered in backends.BACKEND_TO_CLS.
BACKEND_LANGGRAPH = "langgraph"
# Parent-entry option: graph name or UUID to run (LangGraph "assistant").
CONF_ASSISTANT_ID = "assistant_id"
DEFAULT_ASSISTANT_ID = "agent"
# Agent option: the graph's input key the new message list is nested under.
CONF_INPUT_MESSAGES_KEY = "input_messages_key"
DEFAULT_INPUT_MESSAGES_KEY = "messages"
# Agent option: only emit tokens whose metadata.langgraph_node matches this name
# (so intermediate/tool-node chatter is not spoken). Empty/unset = emit all nodes.
CONF_RESPONSE_NODE_FILTER = "response_node_filter"
# Agent option: run without a server-side thread (POST /runs/stream).
CONF_STATELESS_RUNS = "stateless_runs"
# helpers.storage.Store schema version for the session_key -> thread_id map.
STORAGE_VERSION = 1
