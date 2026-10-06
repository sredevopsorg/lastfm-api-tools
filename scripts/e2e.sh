#!/usr/bin/env bash
#
# Run the end-to-end suite against the composed stack.
#
#   scripts/e2e.sh            # run the suite
#   scripts/e2e.sh --keep     # leave the stack up for inspection
#
# Deliberately not `docker compose up --abort-on-container-exit`: the stack has one-shot
# services (migrate, seed) that are *supposed* to exit, and that flag tears everything
# down the moment the first container finishes. So the stack is brought up detached,
# its health is awaited, and the test runner is then invoked as a one-off. That also
# means `--keep` is trivial, which matters when a spec fails and you want to look at the
# app rather than at a log.
set -euo pipefail

cd "$(dirname "$0")/.."

export DOCKER_CONFIG="${DOCKER_CONFIG:-$PWD/.docker-config}"
export BUILDX_CONFIG="${BUILDX_CONFIG:-$PWD/.buildx-config}"

COMPOSE=(docker compose -f docker-compose.e2e.yml)
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

cleanup() {
  if [[ "$KEEP" == "0" ]]; then
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  else
    echo "  stack left running: ${COMPOSE[*]} logs app"
  fi
}
trap cleanup EXIT

echo "==> tearing down any previous run"
"${COMPOSE[@]}" down -v >/dev/null 2>&1 || true

# Only the long-lived services; migrate and seed run as part of the dependency graph.
echo "==> building and starting the stack"
"${COMPOSE[@]}" up -d --build postgres stub migrate seed app

echo "==> waiting for the app to report healthy"
status=""
for _ in $(seq 1 90); do
  status="$("${COMPOSE[@]}" ps --format json app 2>/dev/null | head -1 || true)"
  if [[ "$status" == *'"Health":"healthy"'* ]]; then
    echo "  app is healthy"
    break
  fi
  if "${COMPOSE[@]}" ps app 2>/dev/null | grep -q "Exit"; then
    echo "  app exited before becoming healthy:" >&2
    "${COMPOSE[@]}" logs app >&2
    exit 1
  fi
  sleep 2
done
if [[ "$status" != *'"Health":"healthy"'* ]]; then
  echo "  app never became healthy:" >&2
  "${COMPOSE[@]}" logs app >&2
  exit 1
fi

echo "==> running the Playwright suite"
set +e
"${COMPOSE[@]}" run --rm --no-deps playwright
code=$?
set -e

if [[ "$code" != "0" ]]; then
  echo "==> suite failed; app logs follow" >&2
  "${COMPOSE[@]}" logs app >&2 || true
fi

exit "$code"
