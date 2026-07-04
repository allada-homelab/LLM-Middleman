#!/usr/bin/env bash
set -euo pipefail

if docker info >/dev/null 2>&1; then
    echo "==> Docker socket: OK (service cells + integration tests will work)"
else
    echo "WARNING: Docker socket not accessible — service-cell compose/boot and integration tests will fail."
fi

echo "==> Template dev container ready!"
