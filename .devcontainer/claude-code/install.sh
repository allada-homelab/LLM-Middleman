#!/usr/bin/env bash
# Installs a pinned Claude Code native binary into the image (runs as root at build).
# The official feature (ghcr.io/anthropics/devcontainer-features/claude-code) has no
# version option, and the native installer (claude.ai/install.sh) installs into $HOME
# and auto-updates, so neither pins. This downloads the release binary directly and
# refuses it unless its sha256 matches the one recorded here.
#
# Bumping: set VERSION, then take the linux checksums from
# https://downloads.claude.ai/claude-code-releases/$VERSION/manifest.json after
# verifying manifest.json.sig (see https://code.claude.com/docs/en/setup, "Binary
# integrity and code signing"). The 2.1.280 values below were copied from a manifest
# whose signature verified against key 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE.
set -euo pipefail

# >= 2.1.277: earlier releases do not read AGENTS.md, this project's only agent guide.
VERSION=2.1.280
SHA256_X64=1e08503dbdf3c2cb0d706d32f3408277388d1c76ef108673e8fe42c1b322925b
SHA256_ARM64=92f2b4fd05d0bdcf7b9a0d4e0ecef4a1e4b368b290cd8fd07cff9a50013f45a2

case "$(uname -m)" in
    x86_64) platform=linux-x64 sha256=$SHA256_X64 ;;
    aarch64 | arm64) platform=linux-arm64 sha256=$SHA256_ARM64 ;;
    *)
        echo "claude-code feature: unsupported architecture $(uname -m)" >&2
        exit 1
        ;;
esac

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL -o "$tmp" "https://downloads.claude.ai/claude-code-releases/$VERSION/$platform/claude"
echo "$sha256  $tmp" | sha256sum -c -
install -m 0755 "$tmp" /usr/local/bin/claude
/usr/local/bin/claude --version
