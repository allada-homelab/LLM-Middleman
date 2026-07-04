---
id: LLMM-001
title: Tooling & manifest housekeeping (py3.14, strict pyright, typecheck in CI, manifest/hacs floors)
status: todo
phase: 1
depends_on: []
---

# LLMM-001 — Tooling & manifest housekeeping (py3.14, strict pyright, typecheck in CI, manifest/hacs floors)

## Context
Implements `plan.md §Housekeeping in scope` (the tooling/manifest bullets) plus the v0
gate defects called out in `plan.md §Verified constraints` ("pyright config 'standard'
while docs claim strict; CI never runs typecheck"). The v1 rewrite targets HA 2026.x /
Python ≥3.14 and the subentry config model (core 2025.8+); the repo's build metadata and
CI gate must match that floor before any rewrite code lands. This is the first Phase-1
ticket (no dependencies) so every later ticket inherits a correct, strict gate.

This is a **HACS custom integration, not a pip package** — there is intentionally no
`[build-system]`. Runtime deps live in `manifest.json` `requirements`; dev tooling in
`pyproject.toml` (`CLAUDE.md §What this is`).

## Scope
**In (config / manifest / CI only):**
- `pyproject.toml`: ruff `target-version` → `py314`; `[project].requires-python` → `>=3.14`
  (and the trailing comment on the `target-version` line, which currently says `>=3.13`).
- `pyrightconfig.json`: `typeCheckingMode` `"standard"` → `"strict"`;
  `pythonVersion` `"3.13"` → `"3.14"` (aligns the config to the strict gate the docs
  already advertise).
- `.github/workflows/lint.yml`: add a basedpyright step (`uv run basedpyright`) so CI runs
  the typecheck gate it currently omits.
- `custom_components/llm_middleman/manifest.json`: drop the `"requirements": ["aiohttp"]`
  entry (aiohttp ships in HA core's requirements — a custom integration must not
  re-declare it); set `iot_class` `local_push` → `local_polling`; confirm `codeowners` is
  a real GitHub handle.
- `hacs.json`: bump `homeassistant` `"2025.1.0"` → `"2025.8.0"` (the subentry-safe floor;
  `plan.md §Verified constraints`).
- Regenerate `uv.lock` **only if** the `requires-python` bump makes `uv lock --check` fail
  (run `uv lock`, commit the result — never hand-edit the lockfile).

**Out:**
- Any runtime / feature code. Making the strict gate green requires resolving typing
  errors in the still-present **v0** files (`conversation.py`, `tests/conftest.py`,
  `tests/test_config_flow.py`, `tests/test_conversation.py`). See **Risks** — those files
  are replaced by LLMM-004/005/006/007; do the *minimum* to turn the gate green, do not
  build or refactor behavior here.
- Docs rewrite (`docs/knowledge/03`, handoff bundle, CHANGELOG) → **LLMM-017**.
- Release engineering (hassfest as a release gate, brands, HACS release) → **LLMM-019**.
- `manifest.json` `version` bump → LLMM-019 (release time).

## Implementation notes
- **pyproject.toml** (current values at `pyproject.toml:6` `requires-python = ">=3.13"`,
  `:35` `target-version = "py313"` with a `>=3.13` comment): set both to the 3.14 floor.
  The dev venv is already Python 3.14.3 (`.venv/lib/python3.14/...`), so this matches
  reality. Do **not** touch `[project].dependencies` or `[dependency-groups]` — out of
  scope.
- **pyrightconfig.json** (current `typeCheckingMode: "standard"`, `pythonVersion: "3.13"`):
  flip to `"strict"` / `"3.14"`. Keep the existing `executionEnvironments` test overrides
  (`reportPrivateUsage`/`reportUnusedFunction` "none") — they are legitimate test-scope
  relaxations, not gate weakening. **Do not** add global rule suppressions to hide v0
  errors (hard rule: fix the gate, not the symptom — `CLAUDE.md §Hard rules`).
- **CI (`.github/workflows/lint.yml`)**: the single `uv` job already runs
  `uv sync --locked --dev` then ruff + pytest. Add `- run: uv run basedpyright` as a step
  in that job (reuses the one sync). Update the job `name:` string to reflect it now runs
  pyright. The raw `uv run ...` form matches the existing steps (CI does not invoke `just`).
- **manifest.json** (current at `manifest.json:9` `iot_class: "local_push"`, `:11`
  `requirements: ["aiohttp"]`, `:3` `codeowners: ["@allada-homelab"]`): remove the
  `requirements` key entirely or set it to `[]`. `iot_class` → `local_polling` (the shim
  issues one request/response per turn, HA-initiated — polling-shaped, not a device push;
  `plan.md §Housekeeping` parenthesizes `local_polling`). Verify `@allada-homelab`
  resolves to the real repo owner/org on GitHub (the `documentation`/`issue_tracker` URLs
  point at `github.com/allada-homelab/LLM-Middleman`); if the owner's actual handle
  differs, correct it — hassfest validates codeowner format and the docstring URL.
- **hacs.json** (current `homeassistant: "2025.1.0"`): → `"2025.8.0"`.

## Acceptance criteria
- [ ] `pyproject.toml`: ruff `target-version = "py314"` and `requires-python = ">=3.14"`
      (comment updated).
- [ ] `pyrightconfig.json`: `typeCheckingMode = "strict"`, `pythonVersion = "3.14"`; no new
      global rule suppressions were added to mask errors.
- [ ] `.github/workflows/lint.yml` runs `uv run basedpyright` in the uv job.
- [ ] `manifest.json`: no `aiohttp` requirement; `iot_class = "local_polling"`;
      `codeowners` confirmed a real GitHub handle.
- [ ] `hacs.json`: `homeassistant = "2025.8.0"`.
- [ ] `uv lock --check` passes (lock regenerated with `uv lock` if the `requires-python`
      bump required it; the regenerated `uv.lock` is committed, not hand-edited).
- [ ] Gates green: `just check` + `just typecheck`.

## Verification
- `uv run basedpyright` exits 0 locally (strict). **Baseline first:** the current tree has
  **34 strict errors** (this session: `basedpyright --typeCheckingMode strict` → 12
  `reportTypedDictNotRequiredAccess`, 6 `reportUnknownVariableType`, 6
  `reportUnknownMemberType`, 3 `reportUnknownParameterType`, 2 `reportMissingTypeArgument`,
  1 each `reportReturnType`/`reportPrivateImportUsage`/`reportMissingParameterType`/
  `reportInvalidTypeForm`/`reportIncompatibleVariableOverride`), all in the four v0 files.
  Record this baseline, then drive it to 0.
- `just check` green (`uv lock --check` + ruff check + ruff format --check + pytest) — the
  ruff `target-version` bump may surface new `UP`/pyupgrade findings; fix them (they are
  in-scope for "make the gate green").
- Grep-confirm the edits: `target-version = "py314"`, `requires-python = ">=3.14"`,
  `"typeCheckingMode": "strict"`, `"pythonVersion": "3.14"`, `iot_class` = `local_polling`,
  no `aiohttp` in `manifest.json`, `homeassistant` = `2025.8.0` in `hacs.json`,
  `uv run basedpyright` present in `lint.yml`.
- Manifest validity: `python -c "import json,pathlib;
  json.loads(pathlib.Path('custom_components/llm_middleman/manifest.json').read_text())"`
  parses; required keys (domain/name/codeowners/config_flow/dependencies/documentation/
  integration_type/iot_class/issue_tracker/version) remain present. **hassfest itself runs
  only in `validate.yml` CI (NOT run locally here)** — LLMM-019 owns hassfest as a release
  gate; this ticket only keeps the manifest well-formed.

## Risks / open questions
- **Strict flip vs. still-present v0 code (the load-bearing risk).** Turning the gate
  green means resolving the 34 strict errors in `conversation.py` (`reportPrivateImportUsage`
  on `AbstractConversationAgent`), `tests/conftest.py` (the `llm_api`
  `reportIncompatibleVariableOverride` + async-generator return types), and the two v0
  test files (`reportTypedDictNotRequiredAccess` on `device_info[...]`/TypedDict access).
  These files are replaced by LLMM-004 (conftest), LLMM-005 (`conversation.py`), and
  LLMM-006/007 (config-flow tests). Two ways to reconcile with "Out: any runtime code":
  - **(Recommended)** Land the config/CI/manifest changes here and make the gate green with
    the *minimum* targeted fixes to the v0 files — precise `# pyright: ignore[<rule>]` with
    a `# LLMM-00N replaces this` comment, or a correct annotation where trivial. Keeps
    Phase 1 building on a green strict gate from commit 1; the churn is deleted when those
    files are replaced.
  - **(Rejected alternative, logged so it isn't re-litigated)** Split: land
    pyproject/manifest/hacs now and sequence the strict-flip + CI step after
    LLMM-004/005/006/007 replace the v0 files. Rejected because the new v1 foundation code
    (LLMM-002/003) would then land WITHOUT a strict typecheck gate — the exact v0 defect
    this ticket exists to fix.
    **DECIDED (orchestrator, 2026-07-04): option (a).** Ignores must be per-line,
    rule-scoped (`# pyright: ignore[<rule>]`), and tagged `# LLMM-00N replaces this`.
    Whichever, **never** weaken the global pyright config to hide the errors.
- **`requires-python` bump may invalidate `uv.lock`.** Bumping the floor can change
  resolution markers → `uv lock --check` fails. Regenerate with `uv lock` and commit;
  confirm `just sync` (`uv sync --locked --dev`) still resolves.
- **`iot_class` is a judgment call.** `local_polling` is the closest fit for a
  request/response-per-turn shim; `calculated` is the only other plausible value. hassfest
  accepts any valid enum value, so this is low-stakes — recorded here so it is not
  re-litigated (`plan.md §Housekeeping` explicitly names `local_polling`).
- **`codeowners` handle** cannot be verified offline; confirm against the real GitHub
  owner before merge (the repo's git author is "David Allada").
