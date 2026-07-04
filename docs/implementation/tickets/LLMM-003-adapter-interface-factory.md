---
id: LLMM-003
title: Adapter interface & factory (`backends/base.py`, `BACKEND_TO_CLS`)
status: done
phase: 1
depends_on: []
---

# LLMM-003 — Adapter interface & factory (`backends/base.py`, `BACKEND_TO_CLS`)

## Context
Implements `plan.md §Adapter interface (backends/base.py)` and the `BACKEND_TO_CLS`
factory from `plan.md §Package layout`. This is the seam the entire re-architecture hangs
on: one backend-agnostic `ConversationEntity` (LLMM-005) drives any adapter through a fixed
contract, and the config flow (LLMM-006) builds its backend-type dropdown from the factory.
Every concrete adapter (LLMM-008/009/010/011/012) subclasses this ABC. The multi-backend
shape is modelled on `acon96/home-llm` (a `BACKEND_TYPE` `SelectSelector` + an abstract
client base + a `BACKEND_TO_CLS: dict[str, type]` factory used in `async_setup_entry` and
for connection validation — research-4).

## Scope
**In:**
- `custom_components/llm_middleman/backends/base.py`:
  - `BackendAdapter(ABC)` with the members from `plan.md §Adapter interface`:
    - `backend_type: ClassVar[str]` — the factory key and config-flow dropdown value.
    - `supports_ha_tools: ClassVar[bool] = False` — gates `CONF_LLM_HASS_API` in the subentry
      flow (text-only adapters inherit the default).
    - `supports_memory_scope: ClassVar[bool] = False` — gates `CONF_MEMORY_SCOPE` in the
      subentry flow (stateful backends set it `True`; stateless openai/ollama inherit `False`,
      per `plan.md §Conversation continuity`).
    - `def __init__(self, hass, session, connection_data)` — stores `hass`, the
      `aiohttp.ClientSession`, and the parent config-entry `data` (connection + credentials)
      on `self`; adapters read connection state (`base_url`/`api_key`/token) from `self`. The
      entity/setup layer (LLMM-005) constructs the adapter with this signature and stores it in
      `entry.runtime_data`.
    - `@classmethod @abstractmethod async def async_validate_connection(cls, hass, data)
      -> None` — config-flow probe against the backend's real endpoint; raises on failure.
    - `@classmethod async def async_list_models(cls, hass, data) -> list[str] | None` — the
      model catalog for the subentry model dropdown (`None` when the backend has no catalog).
      Non-abstract; the default returns `None`. openai/ollama override it.
    - `@abstractmethod def stream_turn(self, chat_log, user_input, ctx: TurnContext)
      -> AsyncGenerator[conversation.AssistantContentDeltaDict]` — one round-trip → canonical
      HA delta dicts.
  - `TurnContext` dataclass — the per-turn channel the entity passes to `stream_turn`:
    ```python
    @dataclass
    class TurnContext:
        options: Mapping[str, Any]           # subentry options for this agent
        memory_key: str                      # session key derived by the entity (memory_scope)
        continue_conversation: bool = False  # adapter may set; entity ORs into the result
    ```
    Created **per turn by the entity** (never stored on the adapter). Rationale: the adapter
    instance lives in `entry.runtime_data`, shared across subentries and concurrent turns, so
    per-turn mutable state on the adapter would race; a fresh `TurnContext` per turn is
    race-free. Stateful adapters read `ctx.memory_key`; adapters signal follow-up listening by
    setting `ctx.continue_conversation = True`, which the entity ORs into the `ConversationResult`.
  - Shared type alias `DeltaStream = AsyncGenerator[conversation.AssistantContentDeltaDict]`.
  - Exception hierarchy: `BackendConnectionError(HomeAssistantError)` and
    `BackendAuthError(BackendConnectionError)` (raised by `async_validate_connection`; the
    config flow maps them to `cannot_connect` / `invalid_auth` form errors). Re-export
    `BackendStreamError` from `._sse` (LLMM-002) so adapters import all backend exceptions
    from `base`.
- `custom_components/llm_middleman/backends/__init__.py`:
  - `BACKEND_TO_CLS: dict[str, type[BackendAdapter]]` — the factory registry. **Empty**
    until the first adapter (LLMM-008) lands; assembled by importing each concrete adapter
    module and mapping `AdapterCls.backend_type → AdapterCls` (home-llm pattern).
  - `get_backend_cls(backend_type: str) -> type[BackendAdapter]` — lookup helper; raises a
    clear error for an unknown type.

**Out:**
- **Any concrete adapter** (`openai_compat.py`, `converse.py`, etc.) — LLMM-008+ . This
  ticket ships the ABC, the (empty) registry, and shared types only.
- The shared history-replay/trim helper (`backends/_history.py` `trim_history`) — introduced
  by the first stateless adapter (LLMM-008) and reused by LLMM-010.
- `entry.runtime_data` wiring / `async_create_clientsession` in `__init__.py` setup — LLMM-005
  owns building the adapter instance and storing it. This ticket only defines the constructor
  **contract** (`__init__(hass, session, connection_data)`) the setup code calls.

## Implementation notes
- **ABC shape** (copy the `plan.md §Adapter interface` code block verbatim). Declare the
  abstract `stream_turn` as `def stream_turn(self, chat_log, user_input, ctx: TurnContext)
  -> AsyncGenerator[...]: ...` (body `raise NotImplementedError` or `...`); concrete adapters
  implement it as an `async def` generator with `yield`. The docstring must record the
  two-axis contract from `plan.md §Adapter interface`: **stateless** adapters rebuild provider
  messages from `chat_log.content` (with ollama-style trim via `CONF_MAX_HISTORY`) and pass HA
  tool schemas when `chat_log.llm_api` is set; **stateful** adapters send only the new turn
  keyed on `ctx.memory_key`. `ctx.options` is the per-agent subentry options dict; the entity
  builds one `TurnContext` per turn (see the race rationale in Scope) — adapters must **not**
  stash per-turn state on `self`.
- **Constructor contract:** `__init__(self, hass, session, connection_data)` stores the three
  args on `self`; adapters read `base_url`/`api_key`/token from `self.connection_data` (or
  cache them in `__init__`). LLMM-005's `async_setup_entry` calls
  `adapter_cls(hass, async_create_clientsession(hass), entry.data)`.
- **`async_validate_connection` / `async_list_models` probes** (record in the docstring so
  adapter tickets match `plan.md §Adapter interface`): `async_validate_connection` hits
  openai `GET /v1/models` · ollama `GET /api/tags` · langgraph `GET /ok` (fallback
  `POST /assistants/search`) · converse transport-level check; it returns `None` and
  **raises** on failure — do not return an error string. `async_list_models(cls, hass, data)`
  returns the model-id list for backends with a catalog (openai from `/v1/models`, ollama from
  `/api/tags`) or `None` when there is no catalog (converse/langgraph/n8n); the base default
  returns `None`.
- **Factory registration convention** (home-llm): `backends/__init__.py` imports each
  concrete adapter and builds `BACKEND_TO_CLS = {C.backend_type: C for C in (...)}`. Import
  direction: `base` ← concrete adapters ← `__init__` (no cycle — `base.py` imports only
  `_sse` and HA). Document the exact one-line addition an adapter ticket makes (import + add
  to the tuple) so LLMM-008's "registered in `BACKEND_TO_CLS`" is unambiguous. If LLMM-002
  already created an empty `backends/__init__.py`, replace its body with the registry.
- **Type surface for consumers**: LLMM-005 types `config_entry.runtime_data` as
  `BackendAdapter`; LLMM-006 iterates `BACKEND_TO_CLS.keys()` for the `SelectSelector`
  options and calls `get_backend_cls(...).async_validate_connection(...)`; LLMM-007 calls
  `get_backend_cls(...).async_list_models(...)` for the model dropdown and gates
  `CONF_MEMORY_SCOPE` on `.supports_memory_scope`. Keep the public names stable:
  `BackendAdapter`, `TurnContext`, `BACKEND_TO_CLS`, `get_backend_cls`, `DeltaStream`,
  `BackendConnectionError`, `BackendAuthError`, `BackendStreamError`.
- Backend-type string constants (`BACKEND_OPENAI_COMPAT = "openai_compat"`, etc.) are owned
  by each adapter ticket in `const.py` (LLMM-008 already plans to add `BACKEND_OPENAI_COMPAT`);
  this ticket does not need to pre-declare them — the registry keys off each class's
  `backend_type` classvar.

## Acceptance criteria
- [x] `BackendAdapter(ABC)` exists with `backend_type: ClassVar[str]`, `supports_ha_tools:
      ClassVar[bool] = False`, `supports_memory_scope: ClassVar[bool] = False`,
      `__init__(self, hass, session, connection_data)`, abstract classmethod
      `async_validate_connection(cls, hass, data) -> None`, non-abstract classmethod
      `async_list_models(cls, hass, data) -> list[str] | None` (default returns `None`), and
      abstract `stream_turn(self, chat_log, user_input, ctx: TurnContext) ->
      AsyncGenerator[conversation.AssistantContentDeltaDict]`.
- [x] `TurnContext` dataclass exists with fields `options: Mapping[str, Any]`,
      `memory_key: str`, `continue_conversation: bool = False`.
- [x] Instantiating `BackendAdapter` (or a subclass missing an abstract method) raises
      `TypeError`; a subclass implementing both abstracts instantiates.
- [x] `BACKEND_TO_CLS: dict[str, type[BackendAdapter]]` exists (empty in this ticket) and
      `get_backend_cls("openai_compat")` raises a clear error while the registry is empty.
- [x] `TurnContext`, `DeltaStream`, `BackendConnectionError`, `BackendAuthError` are exported
      from `base`, and `BackendStreamError` is importable from `base` (re-exported from `_sse`).
- [x] No concrete adapter is added by this ticket.
- [x] Gates green: `just check` + `just typecheck` (strict — verify the `def -> AsyncGenerator`
      abstract / `async def` override typechecks; see Risks).

## Verification
Write `tests/test_backends_base.py` (top-level path avoids depending on LLMM-004's
`tests/backends/` package):
- **abstract enforcement** — define an in-test `_DummyAdapter(BackendAdapter)` with both
  abstracts implemented (`stream_turn(self, chat_log, user_input, ctx)` an `async def`
  yielding one `{"role":"assistant"}`/`{"content":"x"}`); assert it instantiates. Define a
  second subclass missing `stream_turn`; assert `TypeError` on instantiation.
- **classvars** — `_DummyAdapter.backend_type == "dummy"`,
  `_DummyAdapter.supports_ha_tools is False` and `_DummyAdapter.supports_memory_scope is False`
  (inherited defaults).
- **async_list_models default** — `await _DummyAdapter.async_list_models(hass, {})` returns
  `None` (base default; no catalog).
- **TurnContext** — `TurnContext(options={}, memory_key="k")` has `continue_conversation is
  False`; a subclass/adapter setting `ctx.continue_conversation = True` mutates only the
  passed instance.
- **factory** — register `_DummyAdapter` into a local copy of the mapping (or, if the ticket
  exposes a registration path, use it) and assert `get_backend_cls("dummy")` returns it;
  assert `get_backend_cls("nope")` raises.
- **exception surface** — `from ...backends.base import BackendStreamError,
  BackendConnectionError, BackendAuthError` all import; `issubclass(BackendAuthError,
  BackendConnectionError)`.
- **delta type** — driving `_DummyAdapter(...).stream_turn(chat_log, user_input,
  TurnContext(options={}, memory_key="k"))` yields dicts assignable to
  `conversation.AssistantContentDeltaDict` (a `[d async for d in ...]` smoke check).
Run `just check` + `just typecheck`; record baseline, report delta.

## Risks / open questions
- **Full contract lives here (not deferred to LLMM-005).** The follow-up-listening and
  continuity seams are decided in this ticket: `TurnContext.continue_conversation` (adapter
  sets, entity ORs in) replaces the earlier per-turn adapter attribute, and
  `supports_memory_scope` (ClassVar) plus `TurnContext.memory_key` cover
  `plan.md §Follow-up listening` / `§Conversation continuity`. The `TurnContext` channel is
  race-free by construction (created per turn by the entity, never stored on the shared
  adapter instance), which is *why* it replaces per-turn mutable adapter state. LLMM-005
  consumes these; it does not extend the ABC.
- **`def` abstract vs `async def` override under strict pyright.** basedpyright treats an
  async-generator function as "callable returning `AsyncGenerator`", so an `async def
  stream_turn` overriding the `def -> AsyncGenerator` abstract should typecheck. Confirm at
  build; if pyright flags variance, declare the abstract as `async def stream_turn(...) ->
  AsyncGenerator[...]` with an empty async-generator body instead (`if False: yield`).
- **Empty registry until LLMM-008.** The config-flow dropdown (LLMM-006) has no options
  until the first adapter registers — expected during Phase 1 (LLMM-006 and LLMM-008 run in
  parallel), not a defect. Note it so LLMM-006's tests don't assume a populated dropdown.
- **`HomeAssistantError` base for connection errors** lets the config flow surface a clean
  message; confirm LLMM-006 maps `BackendAuthError`/`BackendConnectionError` to the right
  `errors["base"]` keys rather than leaking the exception text.
