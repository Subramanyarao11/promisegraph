#!/usr/bin/env bash
# T19 evidence: restart FalkorDB container and confirm workspace state + knowledge revision survive.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
H=(-H "Authorization: Bearer ${OPERATOR_TOKEN}")
snap() { curl -sf "${H[@]}" "${API_BASE_URL}/api/v1/workspaces/ws_demo/requests?limit=100" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['meta']['knowledge_revision'], len(d['data']), sorted(r['id']+':'+r['status'] for r in d['data'])[:3])"; }
before=$(snap); echo "before: $before"
docker compose restart falkordb >/dev/null
for i in $(seq 1 30); do curl -sf "${API_BASE_URL}/health/ready" >/dev/null 2>&1 && break; sleep 1; done
after=$(snap); echo "after:  $after"
[ "$before" = "$after" ] && echo "PERSISTENCE OK" || { echo "PERSISTENCE MISMATCH"; exit 1; }
