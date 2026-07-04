#!/usr/bin/env bash
# Fail if any copier.yml question is never consumed by template/ or a copier.yml
# conditional (_exclude / when / _tasks / Jinja block). Guards against the dead-
# variable rot that accumulates in long-lived templates.
set -eu
cd "$(dirname "$0")/../.."

dead=0
count=0
vars=$(grep -E '^[a-z][a-zA-Z0-9_]*:' copier.yml | sed 's/:.*//')
for v in $vars; do
    count=$((count + 1))
    if ! grep -rIq "$v" template/ 2>/dev/null \
        && ! grep -E '_exclude|when:|_tasks|\{%' copier.yml | grep -q "$v"; then
        echo "DEAD VAR (declared but never consumed): $v"
        dead=$((dead + 1))
    fi
done

if [ "$dead" -ne 0 ]; then
    echo "FAILED: $dead dead variable(s)."
    exit 1
fi
echo "OK: all $count questions are consumed."
