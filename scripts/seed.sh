#!/usr/bin/env bash
# Seed PromiseGraph workspaces through the public import path (preview + commit), exactly as users import.
#
# Usage: scripts/seed.sh [workspace ...]        (default: every directory under fixtures/seed/)
# Env:   API_BASE_URL (default http://localhost:8000), OPERATOR_TOKEN (required). Loaded from .env when present;
#        variables already set in the environment win over .env.
#
# Each file in fixtures/seed/<ws>/ is posted in lexical order:
#   POST $API_BASE_URL/api/v1/workspaces/<ws>/imports/preview   {format, origin: synthetic, source_key: <file stem>, content}
#   POST $API_BASE_URL/api/v1/workspaces/<ws>/imports/<preview_id>/commit   {expected_revision: <preview.based_on_revision>}
# Idempotency keys are deterministic (workspace + file + content sha256), so re-running is safe: an unchanged file
# replays its original preview/commit (or previews as noop). Any non-2xx response or rejected record aborts the run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    name="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    if [[ -z "${!name+x}" ]]; then export "$name=$value"; fi
  done < "$ROOT/.env"
fi

API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
API_BASE_URL="${API_BASE_URL%/}"
: "${OPERATOR_TOKEN:?OPERATOR_TOKEN is not set (export it or add it to .env)}"
command -v curl >/dev/null || { echo "seed: curl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "seed: python3 is required" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

die() { echo "seed: ERROR: $*" >&2; exit 1; }

# post <url> <idempotency-key> <body-file> -> writes response to $TMP/resp.json, dies on non-2xx
post() {
  local url="$1" key="$2" body="$3" code
  code="$(curl -sS -o "$TMP/resp.json" -w '%{http_code}' -X POST "$url" \
    -H "Authorization: Bearer $OPERATOR_TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -H "Idempotency-Key: $key" \
    -H "X-Correlation-Id: seed-$key" \
    --data-binary "@$body")" || die "request to $url failed (network)"
  if [[ "$code" != 2?? ]]; then
    echo "seed: HTTP $code from POST $url" >&2
    cat "$TMP/resp.json" >&2 || true
    echo >&2
    exit 1
  fi
}

# json_get <file> <python expression over `d`>
json_get() {
  python3 - "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d = d.get("data", d) if isinstance(d, dict) else d
v = eval(sys.argv[2], {"__builtins__": {}}, {"d": d})
print(json.dumps(v) if isinstance(v, (dict, list)) else v)
PY
}

seed_file() {
  local ws="$1" file="$2" name stem ext format sha key preview_id revision rejected
  name="$(basename "$file")"
  stem="${name%.*}"
  ext="${name##*.}"
  case "$ext" in
    json) format=json ;;
    md|markdown) format=markdown ;;
    csv) format=csv ;;
    *) echo "seed: skipping $ws/$name (unsupported extension)"; return 0 ;;
  esac
  sha="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$file")"
  key="seed-${ws}-${stem}-${sha:0:16}"

  python3 - "$file" "$format" "$stem" > "$TMP/preview.json" <<'PY'
import json, sys
path, fmt, key = sys.argv[1:4]
print(json.dumps({"format": fmt, "origin": "synthetic", "source_key": key,
                  "content": open(path, encoding="utf-8").read()}))
PY
  post "$API_BASE_URL/api/v1/workspaces/$ws/imports/preview" "$key-preview" "$TMP/preview.json"
  preview_id="$(json_get "$TMP/resp.json" 'd["preview_id"]')"
  revision="$(json_get "$TMP/resp.json" 'd["based_on_revision"]')"
  rejected="$(json_get "$TMP/resp.json" 'd["rejected_count"]')"
  if [[ "$rejected" != "0" ]]; then
    echo "seed: preview of $ws/$name rejected $rejected record(s):" >&2
    json_get "$TMP/resp.json" 'd["rejected"]' >&2
    exit 1
  fi

  printf '{"expected_revision": %s}' "$revision" > "$TMP/commit.json"
  post "$API_BASE_URL/api/v1/workspaces/$ws/imports/$preview_id/commit" "$key-commit-r$revision" "$TMP/commit.json"
  python3 - "$TMP/resp.json" "$ws/$name" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d = d.get("data", d)
if d.get("rejected_count", 0):
    sys.exit(f"seed: commit of {sys.argv[2]} rejected {d['rejected_count']} record(s)")
print(f"seed: {sys.argv[2]:<45} imported={d['imported_count']} noop={d['noop_count']} "
      f"invalidated={d['invalidated_proposal_count']} knowledge_revision={d['knowledge_revision']}")
PY
}

if [[ $# -gt 0 ]]; then
  workspaces=("$@")
else
  workspaces=()
  for d in "$ROOT"/fixtures/seed/*/; do workspaces+=("$(basename "$d")"); done
fi
[[ ${#workspaces[@]} -gt 0 ]] || die "no workspaces under fixtures/seed/"

for ws in "${workspaces[@]}"; do
  dir="$ROOT/fixtures/seed/$ws"
  [[ -d "$dir" ]] || die "no seed directory $dir"
  echo "seed: workspace $ws -> $API_BASE_URL"
  while IFS= read -r file; do
    seed_file "$ws" "$file"
  done < <(find "$dir" -maxdepth 1 -type f ! -name '.*' | LC_ALL=C sort)
done
echo "seed: done"
