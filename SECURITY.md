# Security Policy

## Supported versions

Only the latest released version of `llm-middleman` receives security fixes. There are
no long-term support branches.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it through GitHub's private vulnerability reporting:

1. Go to the [Security tab](https://github.com/allada-homelab/LLM-Middleman/security/advisories/new).
2. Click **Report a vulnerability**.

This creates a private advisory visible only to the maintainers. No email address or PGP key
is needed — GitHub handles the confidential channel.

> Private vulnerability reporting must be enabled once per repository:
> Settings → Code security → **Private vulnerability reporting**.

## What to expect

- An acknowledgement that the report was received.
- An assessment of whether it is exploitable and in scope.
- A fix released in a new version, with the advisory published once users can upgrade.

Please give the maintainers a reasonable chance to ship a fix before disclosing publicly.

## Scope

In scope: anything in this repository's own code and its published artifacts.

Out of scope: vulnerabilities in third-party dependencies (report those upstream — though
telling us is welcome so the dependency can be bumped), and findings that require an attacker
to already control the host the service runs on.
