#!/usr/bin/env bash
set -euo pipefail

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
