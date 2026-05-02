#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="/Users/matthewthomasson/ib_mcp_overlay"
LOG_DIR="$HOME/Library/Logs/ib_mcp_overlay"
mkdir -p "$LOG_DIR"

cd "$REPO_DIR"

if ! /opt/homebrew/bin/colima status >/dev/null 2>&1; then
  /opt/homebrew/bin/colima start --cpu 2 --memory 4 >>"$LOG_DIR/colima.log" 2>&1
fi

for _ in {1..60}; do
  if /opt/homebrew/bin/docker info >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! /opt/homebrew/bin/docker info >/dev/null 2>&1; then
  echo "docker daemon did not become ready" >>"$LOG_DIR/startup.log"
  exit 1
fi

IB_PORT=7496 /opt/homebrew/bin/docker compose -f docker-compose.override.yml up -d --build >>"$LOG_DIR/docker-compose.log" 2>&1
