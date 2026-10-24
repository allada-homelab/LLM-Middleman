# Dify live E2E — owner-run (LLMM-021)

**Status: NOT RUN.** No Dify instance is reachable from the devcontainer, so every row
below is owner-run against the owner's Dify and live HA. The recipe, per-row procedure, and
pass criteria are in [`scripts/e2e/README.md`](../../../scripts/e2e/README.md) (dify section);
`scripts/e2e/dify_e2e.py` drives the REST-observable rows.

What *was* verified in the devcontainer (agent-run, against the spec's wire shapes):
`tests/backends/test_dify.py` replays spec-exact Chatbot / Legacy Agent / Agent / Chatflow
streams (success, `workflow_finished(failed)`→`error`, `workflow_paused`, moderation,
usage, reasoning, stop-on-cancel/timeout, inputs mapping, user option) through the real SSE
parser, and the cross-preset contract + streaming E2E suites run the Dify Agent wire over
real sockets. That is wire-level evidence, not a live-Dify result.

Fill in one row per check (date, HA version, Dify version/Cloud, app type, observation):

| # | Row | App type(s) | Result | Evidence |
|---|---|---|---|---|
| 1 | Streamed reply, no duplicate text | Chatbot / Legacy Agent / Agent / Chatflow | NOT RUN | |
| 2 | Second turn recalls | all | NOT RUN | |
| 3 | Conversation deleted in Dify recovers | any | NOT RUN | |
| 4 | Cancel mid-tool → stopped in Dify logs | Agent | NOT RUN | |
| 5 | Chatflow failure → fallback | Chatflow | NOT RUN | |
| 6 | Device scope survives HA restart | any | NOT RUN | |
| 7 | Bad key → `invalid_auth` | any | NOT RUN | |
| 8 | Workflow-app key → `not_chat_app` | Workflow | NOT RUN | |
| 9 | Usage visible in trace | any | NOT RUN | |
| 10 | Required-input app works with mapping | Chatbot or Chatflow | NOT RUN | |
| 11 | HA context + satellite `ask_question` follow-up | any | NOT RUN | |
| 12 | Regression on the owner's "New Jarvis" app | owner's app | NOT RUN | |
