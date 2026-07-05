---
id: LLMM-019
title: Release engineering (hassfest, brands, HACS release)
status: in-review
phase: 4
depends_on: [LLMM-016, LLMM-017, LLMM-018]
---

# LLMM-019 — Release engineering (hassfest, brands, HACS release)

## Context

The final Phase 4 ticket: ship v1 as an installable HACS release. It makes hassfest
green, submits the integration to the `home-assistant/brands` repo, finalizes the
manifest release version + `hacs.json` floors, cuts a GitHub **release**, and confirms a
clean HACS custom-repo install. Implements `plan.md` §Verification (HACS custom-repo
install, hassfest) and §Housekeeping (manifest/hacs bumps, codeowners is a real handle).

Depends on LLMM-016 (diagnostics), LLMM-017 (docs), and LLMM-018 (live E2E evidence) —
release is the last gate, so all quality work must be done and the E2E matrix passed.

## Scope

**In:**
- Confirm **hassfest** passes (the `validate.yml` job, `home-assistant/actions/hassfest`)
  against the final v1 code and fix any manifest/structure violations it flags.
- Submit the integration icon/logo to the **`home-assistant/brands`** repo (separate PR
  to that upstream repo).
- Set the manifest **release `version`** and confirm `hacs.json` / manifest HA-version
  floors are correct and mutually consistent at release.
- Cut a **GitHub release** (annotated tag + release notes), not just a tag.
- Verify a clean **HACS custom-repo install** of the released version on the live HA
  instance.

**Out:**
- The initial min-HA-version floor bump for subentry safety (≥2025.8) and dropping
  `requirements: ["aiohttp"]` → owned by **LLMM-001** (this ticket confirms they are
  correct at release, and only adjusts if hassfest/HACS disagree).
- Docs/CHANGELOG content → **LLMM-017** (this ticket only sets the version string the
  CHANGELOG heading references and cuts the release from it).
- The per-preset functional E2E → **LLMM-018** (this ticket links its results as the
  release evidence).

## Implementation notes

**Current state (verify against files at build time — these drift):**
- `custom_components/llm_middleman/manifest.json`: `version` `0.1.0`; `codeowners`
  `["@allada-homelab"]`; `iot_class` `local_push`; `documentation` +
  `issue_tracker` set to the GitHub repo; `requirements ["aiohttp"]` (LLMM-001 removes).
- `hacs.json`: `{"name": "LLM Middleman", "render_readme": true,
  "homeassistant": "2025.1.0"}` — LLMM-001 bumps `homeassistant` to the subentry-safe
  floor (≥2025.8). Confirm it matches the manifest's advertised minimum at release.
- `brand/` images already exist locally (`icon.png`, `icon@2x.png`, `logo.png`,
  `logo@2x.png`) — these feed the brands submission but are **not** how HA ships brand
  art; the `home-assistant/brands` PR is required.

**hassfest.** It runs in `.github/workflows/validate.yml` on push/PR. Confirm green on
the release commit. Common failures to preempt (research-1 §manifest / HACS):
- `version` present and AwesomeVersion-valid (SemVer) — required for custom integrations.
- Required manifest keys present: `domain, name, codeowners, dependencies, documentation,
  integration_type, iot_class, requirements` (+ `issue_tracker` for HACS).
- `codeowners` is a **real GitHub handle** (plan §Housekeeping flags verifying
  `@allada-homelab` resolves).
- `iot_class` revisit (`local_polling` vs current `local_push` — plan §Housekeeping);
  hassfest accepts either but pick the honest one for the forwarding shim.

**Brands submission (`home-assistant/brands`).** Open a PR adding
`custom_integrations/llm_middleman/icon.png` (+ `icon@2x.png`, optional `logo.png`) per
the brands repo's spec (square icon, sizes/format enforced by its CI). This is an
upstream PR with its own review latency — start it early. **This is an outward action to
a third-party repo:** state the rollback (close the PR) and get owner confirmation before
opening it.

**Release version + `hacs.json` floors.**
- Choose the v1 release version (e.g. `1.0.0` for the ground-up rewrite — owner's call;
  coordinate with the CHANGELOG heading LLMM-017 wrote). Set `manifest.json` `version`.
- Confirm `hacs.json` `homeassistant` floor ≥ the actual minimum the code requires
  (subentries ≥2025.3, safely ≥2025.8 per plan) and ≤ the version E2E ran on
  (2026.7.1). Optionally set `hacs` min-version if a feature requires it.

**GitHub release.** HACS prefers real releases; without one it uses the first 7 chars of
the latest commit hash as the version (research-1 §HACS). Cut the release via
`gh release create <vX.Y.Z> --title ... --notes ...` from the merged release commit on
`main` (after the release PR merges — never tag from a branch that bypassed the gate).
**Irreversible outward action:** state the rollback (`gh release delete` + `git push
--delete origin <tag>`) and get owner confirmation before creating the tag/release.

**HACS install check.** After the release exists, add the repo as a HACS custom
repository on the live HA, install the **released** version (not a branch), restart, and
confirm the integration loads and a config entry can be created. This is the final
consumer-path proof (distinct from LLMM-018's development install).

## Acceptance criteria

- [x] hassfest (`validate.yml`) is green on the release commit; any violations fixed at
      the source (not suppressed).
- [x] A `home-assistant/brands` PR adding the `llm_middleman` icon/logo is open (or
      merged), passing that repo's CI.
- [x] `manifest.json` `version` is the chosen v1 SemVer; `codeowners` resolves to a real
      GitHub account; `iot_class` decision recorded.
- [x] `hacs.json` HA-version floor is consistent with the manifest and with the versions
      E2E validated; `requirements` no longer lists `aiohttp` (confirming LLMM-001).
- [x] A GitHub **release** (tag + notes) is cut from the merged release commit on `main`.
- [ ] A clean HACS custom-repo install of the released version loads on the live HA
      instance and a config entry can be created.
- [x] Gates green: `just check` + `just typecheck` (+ hassfest).

## Verification

- CI: the `Validate` workflow's `Hassfest` job is green on the release commit
  (link the run).
- `gh release view <vX.Y.Z>` shows the release with notes; `gh api
  repos/allada-homelab/LLM-Middleman/releases/latest` returns the tag.
- Brands: `gh pr view <brands-PR-url>` shows the PR against `home-assistant/brands`
  passing checks.
- Manifest/hacs: read `manifest.json` + `hacs.json` and confirm the version + floor
  values; `curl`/`gh api` the `codeowners` handle resolves to a real GitHub user.
- Live install: on HA, HACS → custom repo → install released version → restart → the
  integration appears and `Add entry` reaches the backend-type step. Record the observed
  version string matches the release.

## Risks / open questions

- **Outward/irreversible actions gated on owner confirmation:** the `home-assistant/brands`
  PR and the GitHub release tag are both outward and should not proceed without explicit
  owner sign-off; rollbacks are "close the PR" and "delete the release + tag"
  respectively.
- Brands-repo review latency is outside our control — start that PR as early as the icon
  is final so it isn't the release bottleneck; the integration installs from HACS without
  it, only the icon is missing until it merges.
- `iot_class` (`local_push` vs `local_polling`) is a judgment call the plan flags but
  does not decide — the forwarding shim is request/response (arguably `local_polling`);
  pick the honest value and note the reasoning. Non-blocking for hassfest.
- The v1 version string (e.g. `1.0.0` vs `0.x`) is the owner's release call; confirm
  before tagging so the CHANGELOG heading (LLMM-017) and the tag agree.

## Release evidence (2026-07-05, owner-approved recommendations)

- **Release**: v1.0.0 cut from `main` (`ccdad83`, PR #25) — tag + notes from the
  CHANGELOG's 1.0.0 section; `gh api .../releases/latest` returns `v1.0.0`.
  Rollback (if ever needed): delete release + tag.
- **hassfest**: green on the release commit (PR #25 checks).
- **Brands**: home-assistant/brands PR #10693 open, all its CI green (icons 256/512,
  logos 512x128/1024x256 accepted); awaiting upstream review — icon-only cosmetic
  until merged.
- **Manifest/HACS**: version 1.0.0; codeowners `@allada-homelab` resolves (GitHub org);
  `iot_class: local_polling` (honest for a request/response forwarding shim);
  hacs.json floor 2025.8.0; no `requirements` key (LLMM-001 confirmed).
- **E2E evidence**: linked dress-rehearsal matrix `docs/implementation/e2e-results/`
  (PR #24).
- **Remaining before `done`**: owner-run clean HACS install of the released v1.0.0 on
  the live HA (last acceptance box) — batched with the LLMM-018 owner rows.
