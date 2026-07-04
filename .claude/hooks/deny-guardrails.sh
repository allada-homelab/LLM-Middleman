#!/usr/bin/env bash
# PreToolUse(Bash) hook — deterministic deny for the project's hard rules.
#
# Turns prose ("never force-push, never bypass the gate, never amend a published
# commit, never kill user processes, never admin-merge") into a few fast greps.
# Denies via permissionDecision:"deny". Fails OPEN: if jq is missing or input is
# unreadable, it allows the command rather than blocking work.
#
# A bare `git commit --amend` of unpushed WIP stays allowed — only amend chained
# with push is blocked.

set -euo pipefail

command -v jq >/dev/null 2>&1 || exit 0

input=$(cat 2>/dev/null || echo "")
[ -z "$input" ] && exit 0

cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")
[ -z "$cmd" ] && exit 0

deny() {
    jq -nc --arg r "$1" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
    exit 0
}

# 1. bare force push (--force / -f). --force-with-lease is the sanctioned form
#    (it refuses to clobber unseen remote work) and stays allowed.
if printf '%s' "$cmd" | grep -qEi 'git[[:space:]]+push([[:space:]]+[^|;&]*)?[[:space:]](--force([=[:space:]]|$)|-f([[:space:]]|$))'; then
    deny "Hard rule: never bare --force push. Use --force-with-lease, or rebase onto origin/main and push normally; if the branch is published, add a new commit instead."
fi

# 2. bypass commit/push verification (--no-verify, or commit -n)
if printf '%s' "$cmd" | grep -qE '(commit|push)[^|;&]*--no-verify' \
    || printf '%s' "$cmd" | grep -qE 'commit[^|;&]*[[:space:]]-n([[:space:]]|$)'; then
    deny "Hard rule: never bypass the gate with --no-verify / -n. Fix the failing lint/typecheck/test instead."
fi

# 3. amend chained with push (bare amend of unpushed WIP stays allowed)
if printf '%s' "$cmd" | grep -qE 'commit[^|;&]*--amend' && printf '%s' "$cmd" | grep -qE 'push'; then
    deny "Hard rule: never amend a published commit. This command amends and pushes — add a new commit on top instead."
fi

# 4. pkill / killall of user processes
if printf '%s' "$cmd" | grep -qE '\b(pkill|killall)\b'; then
    deny "Hard rule: never pkill/killall user processes. Use a scoped 'kill %1' on a job you started."
fi

# 5. admin merge bypasses branch protection
if printf '%s' "$cmd" | grep -qE 'gh[[:space:]]+pr[[:space:]]+merge[^|;&]*--admin'; then
    deny "Hard rule: never merge red. 'gh pr merge --admin' bypasses branch protection — fix the failing checks instead."
fi

exit 0
