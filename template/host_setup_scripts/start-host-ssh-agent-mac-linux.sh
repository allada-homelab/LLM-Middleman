# shellcheck shell=bash
# Source this: . start-host-ssh-agent-mac-linux.sh
# Do NOT run as ./start-host-ssh-agent-mac-linux.sh — env vars won't propagate.

setup_ssh_agent() {
  local agent_env="$HOME/.ssh/agent-env"
  local key="${1:-}"
  local lifetime="${SSH_AGENT_LIFETIME:-3600}"

  echo "[setup-ssh-agent] Checking for existing agent..."

  # Clean up stale socket references
  if [ -n "${SSH_AUTH_SOCK:-}" ] && [ ! -S "$SSH_AUTH_SOCK" ]; then
    echo "[setup-ssh-agent] Stale SSH_AUTH_SOCK detected, unsetting..."
    unset SSH_AUTH_SOCK SSH_AGENT_PID
  fi

  # Try reattaching to a persisted agent
  if [ -z "${SSH_AUTH_SOCK:-}" ] && [ -f "$agent_env" ]; then
    echo "[setup-ssh-agent] Found persisted agent env, reattaching..."
    # shellcheck source=/dev/null
    source "$agent_env" >/dev/null
    # Validate the reattached socket is still alive
    if [ -n "${SSH_AUTH_SOCK:-}" ] && [ ! -S "$SSH_AUTH_SOCK" ]; then
      echo "[setup-ssh-agent] Persisted agent is dead, cleaning up..."
      unset SSH_AUTH_SOCK SSH_AGENT_PID
      rm -f "$agent_env"
    fi
  fi

  # Check agent status
  ssh-add -l >/dev/null 2>&1
  local status=$?

  # 0 = agent running with keys
  if [ "$status" -eq 0 ]; then
    echo "[setup-ssh-agent] Agent already running with keys."
    return 0
  fi

  # 2 = no agent contactable — start one
  if [ "$status" -eq 2 ]; then
    echo "[setup-ssh-agent] No agent found, starting one..."
    eval "$(ssh-agent -s)"

    # Persist for other terminals
    mkdir -p "$HOME/.ssh"
    cat > "$agent_env" <<EOF
export SSH_AUTH_SOCK=${SSH_AUTH_SOCK}
export SSH_AGENT_PID=${SSH_AGENT_PID}
EOF
    chmod 600 "$agent_env"
  fi

  # 1 = agent running, no keys loaded (or we just started a fresh agent)
  echo "[setup-ssh-agent] Adding key(s) (lifetime=${lifetime}s)..."
  if [ -n "$key" ]; then
    ssh-add -t "$lifetime" "$key" || { echo "[setup-ssh-agent] Failed to add key: $key"; return 1; }
  else
    ssh-add -t "$lifetime" || { echo "[setup-ssh-agent] Failed to add key(s)."; return 1; }
  fi

  echo "[setup-ssh-agent] Done. SSH_AUTH_SOCK=${SSH_AUTH_SOCK}"
}

setup_ssh_agent "$@"
