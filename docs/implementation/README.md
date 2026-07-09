# Implementation tracking

This directory is the project-management home for the **v1 multi-backend re-architecture**
(see [`plan.md`](plan.md), the approved architecture of record). It is owned and maintained
by the implementing agent; humans review via PRs.

## Layout

- [`plan.md`](plan.md) — the approved architecture plan (provenance header at top).
- [`tickets/`](tickets/) — one file per ticket, `LLMM-###-short-slug.md`.
- This README — conventions, the ticket index, and the dependency graph.

## Ticket conventions

Each ticket is a self-contained brief: an implementer should be able to execute it with
`plan.md` + the ticket alone. Frontmatter:

```yaml
---
id: LLMM-004
title: Backend-agnostic conversation entity
status: todo          # todo | in-progress | in-review | done | blocked
phase: 1              # 1 Foundation · 2 Adapters · 3 Tool loop · 4 Polish/release
depends_on: [LLMM-002, LLMM-003]
---
```

Body sections (all required): **Context** (why; link to plan section), **Scope** (in/out),
**Implementation notes** (files, patterns to copy with `file:line`/repo references),
**Acceptance criteria** (checkboxes; objective), **Verification** (commands/observations
proving it works), **Risks / open questions**.

## Workflow rules

1. One ticket → one branch (`llmm-###-slug`) → one PR referencing the ticket ID.
2. Update the ticket's `status` in the same PR that changes it; `done` requires all
   acceptance criteria checked and the ticket's Verification section executed with
   evidence linked/pasted into the PR.
3. Repo hard rules apply (no direct commits to `main`, no force-push, no red merges,
   no gate bypasses — see `CLAUDE.md`).
4. Scope changes discovered mid-ticket: edit the ticket in the same PR (small) or file a
   new ticket (large) — never silently expand.
5. Tickets follow dependency order below; parallel work is fine when dependencies allow.

## Ticket index & dependency graph

Phase milestones match `plan.md` §Implementation phases. `⇐` = depends on.

### Phase 1 — Foundation + OpenAI-compatible
| ID | Title | ⇐ |
|---|---|---|
| LLMM-001 | Tooling & manifest housekeeping (py3.14, strict pyright, typecheck in CI, manifest/hacs floors) | — |
| LLMM-002 | Spec-compliant SSE reader (`backends/_sse.py`) + raw-byte test harness | — |
| LLMM-003 | Adapter interface & factory (`backends/base.py`, `BACKEND_TO_CLS`) | — |
| LLMM-004 | Test harness port (MockChatLog conftest + fake-stream helpers) | — |
| LLMM-005 | Backend-agnostic conversation entity (never-hangs guard, timeouts, continue_conversation, memory_scope) | LLMM-003, LLMM-004 |
| LLMM-006 | Parent config flow (backend-type menu, per-backend connection steps, probes) | LLMM-003 |
| LLMM-007 | Conversation subentry flow (agents: prompt/model/options) | LLMM-006 |
| LLMM-008 | OpenAI-compatible adapter (text-only) | LLMM-002, LLMM-003, LLMM-004 |

### Phase 2 — Remaining adapters
| ID | Title | ⇐ |
|---|---|---|
| LLMM-009 | Custom `/v1/converse` adapter (v0 contract through new parser/guard) | LLMM-002, LLMM-003, LLMM-004 |
| LLMM-010 | Ollama native adapter (NDJSON, trim-history) | LLMM-003, LLMM-004 |
| LLMM-011 | LangGraph adapter (threads, messages-tuple, Store-persisted mapping) | LLMM-002, LLMM-003, LLMM-004 |
| LLMM-012 | n8n adapter (Chat Trigger/plain webhook, dual streaming/blocking) | LLMM-003, LLMM-004 |
| LLMM-013 | v0→v1 config-entry migration (v0 entry → converse parent + subentry) | LLMM-007, LLMM-009 |

### Phase 3 — HA tool loop
| ID | Title | ⇐ |
|---|---|---|
| LLMM-014 | Tool loop core + OpenAI-compatible tools (LLM API multi-select, CONTROL flag, iteration bound) | LLMM-005, LLMM-007, LLMM-008 |
| LLMM-015 | Ollama tool support (native tool_calls + malformed-args repair) | LLMM-010, LLMM-014 |

### Phase 4 — Polish & release
| ID | Title | ⇐ |
|---|---|---|
| LLMM-016 | Diagnostics (redacted config-entry diagnostics) | LLMM-006, LLMM-007 |
| LLMM-017 | Docs rewrite (knowledge base, README, stale-claim fixes, CHANGELOG) | Phase 2 complete |
| LLMM-018 | Live E2E verification matrix (per-preset against real backends) | Phase 2 complete |
| LLMM-019 | Release engineering (hassfest, brands, HACS release) | LLMM-016, LLMM-017, LLMM-018 |

### Phase 5 — Fast-follow adapters
| ID | Title | ⇐ |
|---|---|---|
| LLMM-020 | Dify adapter (chat/agent/chatflow apps, SSE streaming, server-side memory) | LLMM-003, LLMM-004 |

### Fast-follow (roadmap, not v1 — file tickets when picked up)
AI Task `generate_data` subentry type · token stats via `chat_log.async_trace` · external
tool-activity surfacing (`ToolInput(external=True)`) · AG-UI / Anthropic /
Responses-toggle presets.
