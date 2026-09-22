#!/usr/bin/env bash
set -euo pipefail

echo "==> Syncing Python dependencies to uv.lock..."
# Every start, not just create: the workspace may have been checked out or merged
# on the host while the container was down. Warn rather than fail — a failing
# postStartCommand refuses to start the container on every start.
uv sync --locked --all-groups --all-extras ||
    echo "WARNING: uv sync --locked failed — uv.lock is stale or unreadable; run \`uv lock\` and \`just sync\`" >&2

# Warn only. A bind mount re-resolves its source path on every container START, so
# with a stable host socket path a stop+start heals a dead agent; only a changed
# path needs a recreate.
if [ -S "${SSH_AUTH_SOCK:-}" ]; then
    ssh_rc=0
    ssh-add -l > /dev/null 2>&1 || ssh_rc=$?
    case "$ssh_rc" in
        2) echo "WARNING: SSH agent socket is dead (host agent restarted since this container was created/started) — stop+start the container" >&2 ;;
        1) echo "WARNING: host SSH agent has no keys loaded — run ssh-add on the host" >&2 ;;
    esac
fi

# The docker-outside-of-docker feature binds the host daemon socket to
# /var/run/docker-host.sock and proxies it to /var/run/docker.sock, so the two
# checks below distinguish "the socket was never mounted" from "it is mounted but
# the daemon does not answer" — the same symptom with two different fixes.
docker_ok=1
if [ ! -S /var/run/docker-host.sock ]; then
    echo "WARNING: the host docker socket is not mounted into this container" >&2
    echo "  (/var/run/docker-host.sock missing). Is the docker-outside-of-docker" >&2
    echo "  feature still in devcontainer.json, and is a rootful daemon running on" >&2
    echo "  the host?" >&2
    docker_ok=0
elif ! docker info > /dev/null 2>&1; then
    echo "WARNING: the docker socket is mounted but the daemon is unreachable" >&2
    echo "  ($(docker info 2>&1 | tail -n 1)). The host daemon may be stopped, or" >&2
    echo "  this user may not be permitted to use it." >&2
    docker_ok=0
fi

# Warn, don't fail: nothing in the default dev loop needs Docker. Integration
# tests are opt-in (include_integration_tests) and deselected from `just test`,
# so a Docker-less host still gets a fully working container — and a failing
# postStartCommand would refuse to start it on *every* start, not just the first.
if [ "$docker_ok" -eq 0 ]; then
    echo "==> Dev container started WITHOUT Docker — integration tests and"
    echo "    testcontainers will fail until the warning above is fixed."
    exit 0
fi

echo "==> Docker socket: OK"
echo "==> Dev container ready!"
