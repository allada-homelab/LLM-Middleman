---
okf_version: "0.2"
---

# Gotcha

* [ChatLog snapshots the user text before _async_handle_message runs](./chat-log-snapshots-user-text-before-handler.md) - HA adds the user turn to ChatLog before calling the entity, so editing user_input.text there reaches only presets that read user_input; openai_compat and ollama replay ChatLog.
* [hassfest rejects URLs and angle brackets in strings.json descriptions](./hassfest-strings-no-urls-or-angle-brackets.md) - Config-flow description text must hold no dotted-domain URL and no angle-bracket token, or hassfest fails CI; edit strings.json and translations/en.json together.

# Runbook

* [Updating from python-template in a HACS-integration repo](./copier-update-for-hacs-integration.md) - This repo renders from a service template it no longer resembles; on copier update keep the HACS pyproject and the deletions, and hand-port template CI changes into lint.yml.
