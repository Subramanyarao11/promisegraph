#!/usr/bin/env bash
# One-command local demo: FalkorDB (docker) + API :8000 + worker + web :5173, seeded from fixtures/seed.
# Usage: scripts/demo.sh up | down | reset
#   up     start everything; seeds only if the demo graphs (prefix pg_) are empty
#   down   stop API, worker and web (FalkorDB and its data are kept)
#   reset  down, delete the demo graphs (explicit operator action), then up with a fresh seed
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${DEMO_STATE_DIR:-${TMPDIR:-/tmp}/promisegraph-demo}"
mkdir -p "$STATE"
cd "$ROOT"
[[ -f .env ]] || cp .env.example .env
set -a; . ./.env; set +a
API_PORT="${API_PORT:-8000}"
CONTAINER="${FALKORDB_CONTAINER:-promisegraph-falkordb-1}"

stop() {
  for p in web worker api; do
    if [[ -f "$STATE/$p.pid" ]]; then
      pid="$(cat "$STATE/$p.pid")"; pkill -TERM -P "$pid" 2>/dev/null || true; kill "$pid" 2>/dev/null || true
      rm -f "$STATE/$p.pid"
      echo "demo: stopped $p"
    fi
  done
}

# start <name> <dir> <cmd...>: detached, stdin closed, output to $STATE/<name>.log
start() {
  local name="$1" dir="$2"; shift 2
  pushd "$ROOT/$dir" >/dev/null
  nohup "$@" </dev/null >"$STATE/$name.log" 2>&1 &
  echo $! >"$STATE/$name.pid"
  disown
  popd >/dev/null
}

demo_graphs() { docker exec "$CONTAINER" redis-cli GRAPH.LIST | grep -E "^${DATABASE_GRAPH_PREFIX:-pg_}ws_" || true; }

up() {
  docker compose up -d falkordb >/dev/null
  for port in "$API_PORT" 5173; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then echo "demo: port $port is already in use; run 'scripts/demo.sh down' or free it"; exit 1; fi
  done
  start api services/api uv run uvicorn promisegraph_api.app:app --port "$API_PORT"
  for _ in $(seq 1 60); do curl -sf -m 2 "http://localhost:$API_PORT/health/live" >/dev/null && break; sleep 1; done
  if [[ -z "$(demo_graphs)" ]]; then
    echo "demo: seeding through the import API"
    API_BASE_URL="http://localhost:$API_PORT" scripts/seed.sh
  else
    echo "demo: existing demo graphs kept (use 'reset' for a fresh seed)"
  fi
  curl -sf -m 5 "http://localhost:$API_PORT/health/ready" >/dev/null || { echo "demo: API not ready; see $STATE/api.log"; exit 1; }
  API_BASE_URL="http://localhost:$API_PORT" start worker services/worker uv run python -m promisegraph_worker
  (cd apps/web && pnpm install --frozen-lockfile --silent </dev/null >"$STATE/pnpm.log" 2>&1)
  VITE_API_TARGET="http://localhost:$API_PORT" start web apps/web pnpm dev --port 5173 --strictPort
  for _ in $(seq 1 30); do curl -sf -m 2 http://localhost:5173 >/dev/null && break; sleep 1; done
  echo "demo: ready — open http://localhost:5173  (live model enabled: ${AI_ENABLED:-false}; logs in $STATE)"
}

case "${1:-up}" in
  up) up ;;
  down) stop ;;
  reset)
    stop
    for g in $(demo_graphs); do docker exec "$CONTAINER" redis-cli GRAPH.DELETE "$g" >/dev/null; echo "demo: deleted $g"; done
    up ;;
  *) echo "usage: scripts/demo.sh up|down|reset"; exit 2 ;;
esac
