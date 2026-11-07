#!/usr/bin/env bash
# Acceptance item 1: clean clone -> .env -> docker compose up -> healthz + home.
#
# Needs the same env the app needs (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET,
# OPENAI_API_KEY, and optionally OPENAI_BASE_URL / LLM_MODEL). It writes them
# into the throwaway clone's .env, then deletes the whole directory.
#
#   PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... OPENAI_API_KEY=... \
#     ./scripts/clone_check.sh
#
# Oracle: prints PASS/FAIL and exits non-zero on failure.
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
NAME=${CLONE_NAME:-hubby-clone-check}
PORT=${CLONE_PORT:-8099}
# Must live where the docker daemon can see it: a private /tmp in a sandbox is
# invisible to the daemon, which silently creates a root-owned empty data dir.
WORK_ROOT=${CLONE_WORK_ROOT:-$REPO_ROOT/.dev}
mkdir -p "$WORK_ROOT"
WORK=$(mktemp -d "$WORK_ROOT/hubby-clone.XXXXXX")
COMPOSE=(docker compose -p "$NAME" -f docker-compose.yml)
if [ "${CLONE_WITH_GATEWAY:-0}" = "1" ]; then
  COMPOSE+=(-f docker-compose.with-llm-gateway.yml)
fi

cleanup() {
  (cd "$WORK/hubby" 2>/dev/null && "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1) || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "== cloning into $WORK/hubby"
git clone --quiet "$REPO_ROOT" "$WORK/hubby"
cd "$WORK/hubby"

cat > .env <<EOF
PAYPAL_ENV=sandbox
PAYPAL_CLIENT_ID=${PAYPAL_CLIENT_ID}
PAYPAL_CLIENT_SECRET=${PAYPAL_CLIENT_SECRET}
PAYPAL_RETURN_BASE=http://localhost:${PORT}
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-}
LLM_MODEL=${LLM_MODEL:-gpt-5.4-mini}
MONTHLY_CAP_USD=120
HUBBY_PORT=127.0.0.1:${PORT}
LLM_GATEWAY_NETWORK=${LLM_GATEWAY_NETWORK:-litellm_default}
EOF
chmod 600 .env

mkdir -p data && chmod 777 data   # container runs as uid 10001

echo "== compose up (build from the clone)"
HUBBY_PORT="127.0.0.1:${PORT}" "${COMPOSE[@]}" up -d --build >/dev/null

code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" || true)
  [ "$code" = "200" ] && break
  sleep 2
done

fail=0
if [ "$code" = "200" ]; then
  echo "  [PASS] healthz 200"
else
  echo "  [FAIL] healthz returned $code"
  echo "  --- container said ---"
  "${COMPOSE[@]}" logs --tail=40 2>&1 | sed 's/^/  /'
  fail=1
fi

home=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)
if [ "$home" = "200" ]; then
  echo "  [PASS] home page 200"
else
  echo "  [FAIL] home page returned $home"
  fail=1
fi

curl -s "http://127.0.0.1:${PORT}/healthz" || true
echo
exit "$fail"
