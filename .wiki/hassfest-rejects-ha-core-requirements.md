---
type: gotcha
title: hassfest rejects HA-core requirements in custom manifests
description: hassfest fails a custom manifest that lists any package from HA core's requirements.txt (e.g. aiohttp, probatio), so those stay undeclared and are exempted in the manifest guard test.
tags: [hassfest, ci, home-assistant, maintenance]
generated: {by: okf-wiki/haiku, at: 2026-10-24T21:55:50Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-24T21:55:50Z, commit: 30cf56853287}
sources:
  - {id: s1, resource: "CI failure log, 2026-10-24: hassfest [REQUIREMENTS] error on main and every PR (HA core script/hassfest/requirements.py loads core_requirements)", title: hassfest rejects aiohttp in custom manifest}
  - {id: s2, resource: https://github.com/allada-homelab/LLM-Middleman/pull/85, title: PR 85 fixes the aiohttp hassfest failure}
  - {id: s3, resource: https://github.com/allada-homelab/LLM-Middleman/pull/88, title: PR 88 adds probatio to the hassfest exemption}
  - {id: s4, resource: tests/test_manifest_requirements.py, title: HASSFEST_FORBIDDEN_CORE_REQUIREMENTS guard test}
---

# hassfest rejects HA-core requirements in custom manifests

## Symptom

On 2026-10-24 CI went red on `main` and on every PR with `[REQUIREMENTS] Requirement aiohttp is a dependency of Home Assistant itself and must not be listed in the manifest of a custom integration`[^s1].

## What fails

Listing a package from HA core's `requirements.txt` in `manifest.json` `requirements` fails hassfest. `aiohttp` and `probatio` are both core dependencies, so declaring either one breaks the validate workflow[^s1].

## What works

Leave core packages undeclared. `voluptuous` is not in core `requirements.txt`, so it stays declared. `voluptuous-openapi` must also stay declared: HA dropped it in 2026.9, which caused the 2026-09 outage. The guard test treats the names in `HASSFEST_FORBIDDEN_CORE_REQUIREMENTS` as declared, so it still passes for undeclared core packages[^s4].

## Why

hassfest loads `core_requirements` from HA core's `requirements.txt` and rejects any custom-manifest entry that matches them[^s1]. `aiohttp` was fixed in PR 85[^s2]. `probatio` became a core dependency in HA 2026.9 and was added to the exemption in PR 88[^s3][^s4]. The guard checks "declared" independently of the installed `homeassistant`, so the exemption is keyed by name, not by what the interpreter provides[^s4]. When hassfest flags a new core package, add it to that set.

## Verify

- `tests/test_manifest_requirements.py` :: `HASSFEST_FORBIDDEN_CORE_REQUIREMENTS`
