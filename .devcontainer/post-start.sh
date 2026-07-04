#!/usr/bin/env bash
set -euo pipefail

if docker info > /dev/null 2>&1; then
    echo "==> Docker socket: OK"
else
    echo "WARNING: Docker socket not accessible. Integration-test containers will not work."
fi

echo "==> Dev container ready!"
