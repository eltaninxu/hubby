#!/usr/bin/env bash
# Start or stop the hosted demo.
#
#   scripts/demo.sh on       start the container (Caddy starts serving it)
#   scripts/demo.sh off      stop it (the domain answers 502 until 'on')
#   scripts/demo.sh status   container state + /healthz
#   scripts/demo.sh logs     tail the app log
#
# Keeping it off when nobody is testing means a stranger cannot spend the
# household's model quota.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.with-llm-gateway.yml)
HEALTH_URL=${HUBBY_HEALTH_URL:-https://hubby.royola.dev/healthz}

case "${1:-status}" in
  on)
    "${COMPOSE[@]}" up -d
    echo "hubby: on"
    ;;
  off)
    "${COMPOSE[@]}" stop
    echo "hubby: off (the domain now answers 502 until 'on')"
    ;;
  status)
    docker ps --filter name=hubby-hubby-1 --format 'container: {{.Status}}' || true
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" || true)
    echo "healthz: ${code}"
    ;;
  logs)
    "${COMPOSE[@]}" logs --tail=50 -f
    ;;
  *)
    echo "usage: $0 on|off|status|logs" >&2
    exit 2
    ;;
esac
