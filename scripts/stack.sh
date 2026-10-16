#!/usr/bin/env bash
# Private PromiseGraph evaluation stack: real API (FalkorDB-backed) + real worker, isolated by graph prefix.
# Coordinator-owned. Never touches the shared demo instance (prefix pg_, port 8000).
#
# Usage:
#   scripts/stack.sh up     --port N --prefix P [--faults F] [--no-seed]
#   scripts/stack.sh down   --port N [--prefix P] [--keep-graphs]
#   scripts/stack.sh status --port N
#   scripts/stack.sh restart-api --port N   (restart only the API process; same prefix/faults)
#
#   up      starts `uvicorn promisegraph_api.app:app --port N` with DATABASE_GRAPH_PREFIX=P and FAULTS=F
#           (everything else from the repo .env), waits for /health/ready, refuses to start if any graph
#           with prefix P already exists, seeds through scripts/seed.sh (API_BASE_URL override), then
#           starts `python -m promisegraph_worker` against it.
#   down    stops worker + API and deletes every FalkorDB graph whose name starts with P
#           (docker exec <falkordb container> redis-cli GRAPH.DELETE). P is read from the state dir if omitted.
#   status  prints pids, health and the prefix's graphs.
#
# State (pids, logs, prefix) lives in $STACK_STATE_DIR/<port>/ (default ${TMPDIR:-/tmp}/promisegraph-stack).
# Env: FALKORDB_CONTAINER (default promisegraph-falkordb-1), READY_TIMEOUT (default 60 s).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_ROOT="${STACK_STATE_DIR:-${TMPDIR:-/tmp}/promisegraph-stack}"
CONTAINER="${FALKORDB_CONTAINER:-promisegraph-falkordb-1}"
READY_TIMEOUT="${READY_TIMEOUT:-60}"

die() { echo "stack: ERROR: $*" >&2; exit 1; }
log() { echo "stack: $*"; }

cmd="${1:-}"; [[ -n "$cmd" ]] || die "usage: stack.sh up|down|status|restart-api --port N [--prefix P] [--faults F]"
shift
PORT="" PREFIX="" FAULTS_ARG="" SEED=1 KEEP_GRAPHS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --faults) FAULTS_ARG="$2"; shift 2 ;;
    --no-seed) SEED=0; shift ;;
    --keep-graphs) KEEP_GRAPHS=1; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port N is required"
[[ "$PORT" != "8000" ]] || die "port 8000 is the shared demo instance; pick another port"
STATE="$STATE_ROOT/$PORT"
BASE="http://127.0.0.1:$PORT"

check_prefix() {
  local p="$1"
  [[ "$p" =~ ^[A-Za-z0-9_]+$ ]] || die "prefix must match [A-Za-z0-9_]+ (got '$p')"
  [[ "$p" == *_ ]] || die "prefix must end with '_' (got '$p')"
  [[ ${#p} -ge 6 ]] || die "prefix must be at least 6 characters (got '$p')"
  case "pg_ws_demo pg_ws_other" in *"$p"*) die "prefix '$p' would match the shared pg_ graphs" ;; esac
  [[ "$p" != "pg_" ]] || die "refusing the shared pg_ prefix"
}

graphs_with_prefix() {
  docker exec "$CONTAINER" redis-cli GRAPH.LIST | tr -d '\r' | while IFS= read -r g; do
    [[ -n "$g" && "$g" == "$1"* ]] && echo "$g"
  done
  return 0
}

pid_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }

stop_tree() {  # stop a uv-launched process and its children
  local pid="$1" kids
  pid_alive "$pid" || return 0
  kids="$(pgrep -P "$pid" || true)"
  kill -TERM "$pid" $kids 2>/dev/null || true
  for _ in $(seq 1 50); do
    pid_alive "$pid" || break
    sleep 0.1
  done
  pid_alive "$pid" && kill -KILL "$pid" $kids 2>/dev/null || true
  for k in $kids; do pid_alive "$k" && kill -KILL "$k" 2>/dev/null || true; done
  return 0
}

ready() { curl -fsS -m 2 "$BASE/health/ready" >/dev/null 2>&1; }

start_api() {  # uses PREFIX, FAULTS_ARG; appends to api.log so restarts keep history
  (
    cd "$ROOT/services/api"
    DATABASE_GRAPH_PREFIX="$PREFIX" FAULTS="$FAULTS_ARG" API_BASE_URL="$BASE" \
      nohup uv run uvicorn promisegraph_api.app:app --host 127.0.0.1 --port "$PORT" \
      >>"$STATE/api.log" 2>&1 &
    echo $! > "$STATE/api.pid"
  )
  local deadline=$((SECONDS + READY_TIMEOUT))
  until ready; do
    pid_alive "$(cat "$STATE/api.pid")" || { tail -40 "$STATE/api.log" >&2; die "api exited during startup"; }
    [[ $SECONDS -lt $deadline ]] || { tail -40 "$STATE/api.log" >&2; die "api not ready after ${READY_TIMEOUT}s"; }
    sleep 0.5
  done
  log "api ready: $(curl -fsS "$BASE/health/ready")"
}

case "$cmd" in
  up)
    [[ -n "$PREFIX" ]] || die "--prefix P is required for up"
    check_prefix "$PREFIX"
    command -v docker >/dev/null || die "docker is required"
    if [[ -f "$STATE/api.pid" ]] && pid_alive "$(cat "$STATE/api.pid")"; then
      die "a stack is already running on port $PORT (state $STATE); run down first"
    fi
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then die "port $PORT is already in use"; fi
    existing="$(graphs_with_prefix "$PREFIX")"
    [[ -z "$existing" ]] || die "graphs with prefix $PREFIX already exist: $(echo $existing)"
    mkdir -p "$STATE"
    : > "$STATE/api.log"
    echo "$PREFIX" > "$STATE/prefix"
    echo "$FAULTS_ARG" > "$STATE/faults"
    log "api :$PORT prefix=$PREFIX faults=${FAULTS_ARG:-none} state=$STATE"
    start_api
    if [[ $SEED -eq 1 ]]; then
      start=$SECONDS
      API_BASE_URL="$BASE" "$ROOT/scripts/seed.sh" >"$STATE/seed.log" 2>&1 \
        || { tail -40 "$STATE/seed.log" >&2; die "seed failed (see $STATE/seed.log)"; }
      log "seeded in $((SECONDS - start))s ($(grep -c 'imported=' "$STATE/seed.log") files; log $STATE/seed.log)"
    fi
    (
      cd "$ROOT/services/worker"
      API_BASE_URL="$BASE" WORKER_ID="worker_eval_$PORT" \
        nohup uv run python -m promisegraph_worker >"$STATE/worker.log" 2>&1 &
      echo $! > "$STATE/worker.pid"
    )
    sleep 2
    pid_alive "$(cat "$STATE/worker.pid")" || { tail -40 "$STATE/worker.log" >&2; die "worker exited during startup"; }
    log "worker pid $(cat "$STATE/worker.pid") (log $STATE/worker.log)"
    log "up: $BASE"
    ;;

  down)
    [[ -n "$PREFIX" ]] || PREFIX="$(cat "$STATE/prefix" 2>/dev/null || true)"
    for svc in worker api; do
      if [[ -f "$STATE/$svc.pid" ]]; then
        stop_tree "$(cat "$STATE/$svc.pid")"
        rm -f "$STATE/$svc.pid"
        log "stopped $svc"
      fi
    done
    if [[ $KEEP_GRAPHS -eq 0 && -n "$PREFIX" ]]; then
      check_prefix "$PREFIX"
      n=0
      for g in $(graphs_with_prefix "$PREFIX"); do
        docker exec "$CONTAINER" redis-cli GRAPH.DELETE "$g" >/dev/null
        n=$((n + 1))
      done
      log "deleted $n graph(s) with prefix $PREFIX"
    fi
    ;;

  restart-api)  # T19-style check: stop and restart only the API process with the same prefix/faults
    PREFIX="$(cat "$STATE/prefix" 2>/dev/null || true)"; FAULTS_ARG="$(cat "$STATE/faults" 2>/dev/null || true)"
    [[ -n "$PREFIX" && -f "$STATE/api.pid" ]] || die "no running stack on port $PORT"
    stop_tree "$(cat "$STATE/api.pid")"
    for _ in $(seq 1 50); do lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.1; done
    echo "--- restart $(date -u +%FT%TZ) ---" >> "$STATE/api.log"
    log "restarting api :$PORT prefix=$PREFIX"
    start_api
    ;;

  status)
    PREFIX="${PREFIX:-$(cat "$STATE/prefix" 2>/dev/null || true)}"
    for svc in api worker; do
      p="$(cat "$STATE/$svc.pid" 2>/dev/null || true)"
      if pid_alive "$p"; then log "$svc pid $p running"; else log "$svc not running"; fi
    done
    if ready; then log "health: $(curl -fsS "$BASE/health/ready")"; else log "health: not ready"; fi
    [[ -n "$PREFIX" ]] && log "graphs ($PREFIX): $(graphs_with_prefix "$PREFIX" | tr '\n' ' ')"
    log "state: $STATE"
    ;;

  *) die "unknown command $cmd (up|down|status|restart-api)" ;;
esac
