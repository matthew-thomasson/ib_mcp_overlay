#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="/Users/matthewthomasson/ib_mcp_overlay"
LOG_DIR="$HOME/Library/Logs/ib_mcp_overlay"
mkdir -p "$LOG_DIR"

cd "$REPO_DIR"

if ! /opt/homebrew/bin/docker info >/dev/null 2>&1; then
  if ! /opt/homebrew/bin/colima start --cpu 2 --memory 4 >>"$LOG_DIR/colima.log" 2>&1; then
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') colima start failed; attempting stop/start recovery" >>"$LOG_DIR/startup.log"
    /opt/homebrew/bin/colima stop >>"$LOG_DIR/colima.log" 2>&1 || true
    /opt/homebrew/bin/colima start --cpu 2 --memory 4 >>"$LOG_DIR/colima.log" 2>&1
  fi
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

if /opt/homebrew/bin/docker ps -a --format '{{.Names}}' | grep -qx 'option_chain_mcp'; then
  container_project="$(
    /opt/homebrew/bin/docker inspect \
      --format '{{ index .Config.Labels "com.docker.compose.project" }}' \
      option_chain_mcp 2>/dev/null || true
  )"
  if [[ "$container_project" != "ib_mcp_overlay" ]]; then
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') removing stale option_chain_mcp container from project: ${container_project:-unknown}" >>"$LOG_DIR/startup.log"
    /opt/homebrew/bin/docker rm -f option_chain_mcp >>"$LOG_DIR/docker-compose.log" 2>&1
  fi
fi

compose_cmd=(/opt/homebrew/bin/docker compose -f docker-compose.override.yml)

if [[ -f ".env.shared" ]]; then
  compose_cmd+=(--env-file .env.shared)
  set -a
  source .env.shared
  set +a
fi

if [[ -f ".env" ]]; then
  compose_cmd+=(--env-file .env)
  set -a
  source .env
  set +a
fi

if [[ -z "${IB_FLEX_TOKEN:-}" && -n "${IB_FLEX_TOKEN_SECRET_ID:-}" ]]; then
  if command -v aws >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') loading IB_FLEX_TOKEN from AWS Secrets Manager secret ${IB_FLEX_TOKEN_SECRET_ID}" >>"$LOG_DIR/startup.log"
    secret_value="$(
      aws secretsmanager get-secret-value \
        --secret-id "$IB_FLEX_TOKEN_SECRET_ID" \
        --query SecretString \
        --output text 2>>"$LOG_DIR/startup.log" || true
    )"
    if [[ -n "$secret_value" && "$secret_value" != "None" ]]; then
      if [[ "$secret_value" == \{* ]]; then
        IB_FLEX_TOKEN="$(
          SECRET_VALUE="$secret_value" python3 -c 'import json, os; print(json.loads(os.environ["SECRET_VALUE"]).get("token", ""))' 2>>"$LOG_DIR/startup.log" || true
        )"
      else
        IB_FLEX_TOKEN="$secret_value"
      fi
      export IB_FLEX_TOKEN
    fi
  else
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') aws CLI not found; relying on local IB_FLEX_TOKEN if set" >>"$LOG_DIR/startup.log"
  fi
fi

IB_PORT="${IB_PORT:-7496}" "${compose_cmd[@]}" up -d --build --remove-orphans >>"$LOG_DIR/docker-compose.log" 2>&1
