# Architecture Decision Records

This directory holds **Architecture Decision Records (ADRs)** — short, dated, append-only records of material decisions made about the project, its workflow, or its architecture.

## Why ADRs

When a project compounds over months, decisions get lost. People forget *why* something was structured a certain way, then accidentally undo a deliberate choice. ADRs are a cheap, durable record of:

- **What** was decided.
- **Why** it was decided (context, alternatives considered, tradeoffs).
- **When** it was decided.
- **Whether it's still in effect** (Accepted / Superseded / Deprecated).

When a future PR challenges a past decision, the ADR is what we read first. If the decision is wrong now, we **supersede** it with a new ADR — we don't rewrite the old one.

## When to write an ADR

Write one when the decision:

- Constrains future work in a non-obvious way.
- Closes a question that someone might reasonably re-open later.
- Picks one of several plausible alternatives.
- Affects multiple modules or the workflow itself.

You do **NOT** need an ADR for: typical implementation choices that follow established patterns, single-file refactors, dependency bumps, or anything obviously revisitable inside a single PR.

## Naming + numbering

```
docs/adr/NNNN-short-kebab-slug.md
```

- `NNNN` — zero-padded sequence number, allocated in PR order. The next number is one greater than the highest existing.
- `short-kebab-slug` — 3–6 words.

## Status lifecycle

- **Proposed** — drafted in a PR, not yet merged. The PR is the discussion.
- **Accepted** — merged. The decision is in effect.
- **Superseded by ADR-NNNN** — replaced by a later ADR; the new one explains what changed and why. The old one stays in the repo unchanged.
- **Deprecated** — no longer in effect, and not directly superseded (e.g., the constraint that motivated the decision no longer exists).

ADRs are **append-only**: once accepted, the text doesn't change — only the status header may change when a later ADR supersedes it.

To supersede an ADR: write a new one. In the new ADR's metadata header, set `Supersedes: ADR-NNNN`. In a follow-up commit on the same PR, update the OLD ADR's status header to `Superseded by ADR-MMMM` — but make no other changes to the old text.

## Template

Copy [`template.md`](template.md) and fill in the sections. Keep ADRs **short** — 1 page is the target, 2 pages the cap. If the explanation is longer, the decision is probably too big for one ADR.

## Index

| # | Title | Status |
|---|---|---|
| — | _No ADRs yet. Copy `template.md` to `0001-your-decision.md` to add the first._ | — |
