---
type: runbook
title: Updating from python-template in a HACS-integration repo
description: This repo renders from a service template it no longer resembles; on copier update keep the HACS pyproject and the deletions, and hand-port template CI changes into lint.yml.
tags: [copier, python-template, ci, maintenance]
generated: {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z}
verified:
  - {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z, commit: 0c763fbee1c0}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/57, title: "chore: sync python-template update"}
  - {id: s2, resource: commit:ad6aff3, title: "chore(template): update to python-template v0.25.0"}
  - {id: s3, resource: commit:623c83d, title: Update to python-template v0.27.0}
  - {id: s4, resource: commit:b5442b2, title: Update to python-template v0.28.0}
  - {id: s5, resource: commit:5aeed74, title: Scan the whole git history for secrets in CI}
  - {id: s6, resource: commit:c8ce2be, title: "chore: update python-template to v0.30.0"}
  - {id: s7, resource: https://github.com/allada-homelab/LLM-Middleman/pull/69, title: "fix(justfile): drop the duplicate test recipe so just runs at all"}
---

# Updating from python-template in a HACS-integration repo

## When

Running `copier update` against `allada-homelab/python-template` (see `UPGRADING.md` for the
generic mechanics). The answers say `project_type: service`, but this repo was rebuilt as a
HACS custom integration: no `src/`, no Dockerfile or compose files, no mkdocs site, no
docker-publish or template CI workflows. A plain accept-all update fights those deletions.

## Steps

1. Keep `pyproject.toml` and `pyrightconfig.json` as they are (custom_components paths, HA
   deps, Python 3.14); decline the template's service versions[^s1][^s3].
2. Re-delete anything the render brings back that this repo dropped: the web-service `src/`
   scaffold, Dockerfile/compose, template CI workflows, and CI-only extras such as
   `.github/gitleaks.Dockerfile` plus its Dependabot docker entry[^s2][^s4][^s6].
3. Keep `python_default` answered `3.14`, matching `.python-version`; a 3.13 answer makes
   renders revert it[^s3].
4. CI here is the repo-owned `lint.yml` and `validate.yml`, so template CI changes do not
   apply by themselves. Read the template release notes and port what matters by hand.
   Past examples: the full-history gitleaks scan[^s5], and zizmor-clean `permissions` plus
   `persist-credentials: false` on both workflows[^s4].
5. Before hand-patching a template-owned file (devcontainer scripts, justfile), prefer fixing
   python-template first: a local patch to those files was dropped for the upstream version
   on the next update[^s3].

## Check it worked

- `just --list` runs. A template merge once left two `test` recipes, and `just` refused to
  run at all[^s7].
- `git diff --stat` shows no reintroduced `src/`, Dockerfile or extra workflows, and
  `pyproject.toml` still points at `custom_components`.
- The gate passes: `just lint`, `just typecheck`, `just test`, and pre-commit on all files.

## Verify

- `.copier-answers.yml` :: `project_type: service`
- `.copier-answers.yml` :: `python_default: '3.14'`
- `.github/workflows/lint.yml` :: `gitleaks-history`
- `pyrightconfig.json` :: `custom_components/llm_middleman`
