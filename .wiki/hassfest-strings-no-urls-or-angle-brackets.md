---
type: gotcha
title: hassfest rejects URLs and angle brackets in strings.json descriptions
description: Config-flow description text must hold no dotted-domain URL and no angle-bracket token, or hassfest fails CI; edit strings.json and translations/en.json together.
tags: [hassfest, config-flow, strings, ci]
generated: {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z}
verified:
  - {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z, commit: 0c763fbee1c0}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/8, title: "LLMM-006 fix: hassfest-invalid angle brackets in n8n webhook_url description"}
  - {id: s2, resource: https://github.com/allada-homelab/LLM-Middleman/pull/40, title: "fix(strings): drop URL from dify base_url description"}
  - {id: s3, resource: commit:8acfa31, title: Drop literal URLs from the base_url hint (hassfest forbids them)}
  - {id: s4, resource: .github/workflows/validate.yml, title: hassfest CI job}
---

# hassfest rejects URLs and angle brackets in strings.json descriptions

## Symptom

The `Validate` / `Hassfest` check goes red on a PR that only touched config-flow text, with
`Invalid strings.json / translations/en.json: the string should not contain URLs, please use
description placeholders instead` pointing at a `data_description` key. Local `just test`,
ruff and basedpyright all stay green, so nothing warns you before CI.

## What fails

- A literal example URL with a dotted domain in a description (for example an
  `api.<vendor>.<tld>/v1` hint). This bit the Dify `base_url` hint[^s2] and the generic
  `base_url` hint before that[^s3].
- An angle-bracket placeholder such as `<id>` in a description: hassfest parses it as HTML.
  This turned `main` red after the n8n `webhook_url` hint landed[^s1].

## What works

- Describe the shape in words ("Dify API root including /v1") or use a bare token like `ID`.
- A host-and-port example with no dotted TLD (`http://host:11434`) is not flagged, which is
  why the Ollama hint keeps one[^s2].
- Change `strings.json` and `translations/en.json` in the same edit. The integration ships
  both by hand; `en.json` is the resolved copy (`[%key:...%]` references expanded), so a
  fix in one file only still fails.

## Why

hassfest's TRANSLATIONS check forbids URLs in strings so they go through description
placeholders, and treats `<...>` as markup. hassfest is not runnable in this repo (no HA
core checkout); the only gate is the `validate.yml` CI job[^s4], which also runs weekly
against the untagged hassfest image. A scheduled red run with no code change is HA-side
rule drift, not a regression you caused.

## Verify

- `custom_components/llm_middleman/strings.json` :: /https?://[A-Za-z0-9-]+\.[A-Za-z]/ => 0
- `custom_components/llm_middleman/translations/en.json` :: /https?://[A-Za-z0-9-]+\.[A-Za-z]/ => 0
- `custom_components/llm_middleman/strings.json` :: `a trailing slash is stripped`
- `.github/workflows/validate.yml` :: `home-assistant/actions/hassfest`
