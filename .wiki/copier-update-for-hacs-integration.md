---
type: runbook
title: Updating from python-template in a HACS-integration repo
description: This repo renders from python-template as project_type library, so a same-version copier update changes only _commit; keep the HACS pyproject and hand-port template CI changes.
tags: [copier, python-template, ci, maintenance]
generated: {by: okf-wiki/haiku, at: 2026-10-24T21:55:56Z}
verified:
  - {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z, commit: 0c763fbee1c0}
  - {by: okf-wiki/haiku, at: 2026-10-24T21:55:56Z, commit: 30cf56853287}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/57, title: "chore: sync python-template update"}
  - {id: s2, resource: commit:ad6aff3, title: "chore(template): update to python-template v0.25.0"}
  - {id: s3, resource: commit:623c83d, title: Update to python-template v0.27.0}
  - {id: s4, resource: commit:b5442b2, title: Update to python-template v0.28.0}
  - {id: s5, resource: commit:5aeed74, title: Scan the whole git history for secrets in CI}
  - {id: s6, resource: commit:c8ce2be, title: "chore: update python-template to v0.30.0"}
  - {id: s7, resource: https://github.com/allada-homelab/LLM-Middleman/pull/69, title: "fix(justfile): drop the duplicate test recipe so just runs at all"}
  - {id: s8, resource: https://github.com/allada-homelab/LLM-Middleman/pull/92, title: "chore: copier answers hygiene"}
---

# Updating from python-template in a HACS-integration repo

## When

Running `copier update` against `allada-homelab/python-template` (see `UPGRADING.md` for the
generic mechanics). The answers say `project_type: library` with the service-only flags
(docs site, integration tests, renovate, PR labeler, PyPI publish) off. This repo is a HACS
custom integration, not a pip package. Re-rendering at the same template version changes
only `_commit`; PR #92 rendered with `--defaults`, reintroduced nothing, and removed dead
justfile recipes (`test-int`, `build`, `docs`, `docs-serve`, `compose-config`, `up`, `down`)
and the docker Dependabot entry[^s8].

## Steps

1. Keep `pyproject.toml` and `pyrightconfig.json` as they are (custom_components paths, HA
   deps, Python 3.14); decline the template's service versions[^s1][^s3].
2. Re-delete anything a render brings back that this repo dropped: the `src/` scaffold,
   Dockerfile/compose files, template CI workflows, and the justfile recipes listed above.
   Current renders no longer bring these back, so a clean update shows no diff for them[^s2][^s4][^s6][^s8].
3. Keep `python_default` answered `3.14`, matching `.python-version`; a 3.13 answer makes
   renders revert it[^s3].
4. CI here is the repo-owned `lint.yml`, `validate.yml`, and `release.yml`, so template CI
   changes do not apply by themselves. Read the template release notes and port what matters
   by hand. Past examples: the full-history gitleaks scan[^s5], and zizmor-clean `permissions`
   plus `persist-credentials: false` on both workflows[^s4].
5. Before hand-patching a template-owned file (devcontainer scripts, justfile), prefer fixing
   python-template first: a local patch to those files was dropped for the upstream version
   on the next update[^s3].
6. Open upstream python-template follow-ups, not local patches: the build recipe is emitted
   without a build backend, CONTRIBUTING has blank-line issues, and UPGRADING lacks a `.env`
   note for library projects[^s8].

## Check it worked

- `just --list` runs and shows none of the removed recipes. A template merge once left two
  `test` recipes, and `just` refused to run at all[^s7].
- After a same-version update, `git diff --stat` shows only `.copier-answers.yml`; no
  `src/`, Dockerfile, or extra workflows came back, and `pyproject.toml` still points at
  `custom_components`.
- The gate passes: `just lint`, `just typecheck`, `just test`, and pre-commit on all files.

## Verify

- `.copier-answers.yml` :: `project_type: library`
- `.copier-answers.yml` :: `python_default: '3.14'`
- `.github/workflows/lint.yml` :: `gitleaks-history`
- `pyrightconfig.json` :: `custom_components/llm_middleman`
