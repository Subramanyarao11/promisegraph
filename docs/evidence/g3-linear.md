# G3 — Linear connector verification (T27)

Date: 2026-10-18 · real Linear workspace, dedicated test team **Promise Graph (key PRO)** · AI disabled for this run.

Command: `AI_ENABLED=false scripts/run_scenarios.py --manage --port 8092 --only t06` (LINEAR_ENABLED=true, real key).

Flow observed on the real stack (API + worker + FalkorDB):
1. Fresh request "stream audit events to our SIEM" → planner proposed `create_work`, `connector: linear`
   (rejected the similar CSV-download work with evidence).
2. Operator commit → worker `begin-dispatch` → Linear `issueCreate` → readback.
3. Action `succeeded`, receipt `connector: linear`, `target_id: 4923e78d-6d76-4f92-8c01-e7cb9073de84`,
   `target_url: https://linear.app/promise-graph/issue/PRO-5/stream-audit-events-to-our-siem-audit-log`.
4. Request → `awaiting_work` (not resolved); receipt identical after a real API restart.

Independent readback via Linear GraphQL (not through our code): issue **PRO-5**, team PRO, state **Backlog**,
created 2026-10-18T17:02:30Z. It is the only issue our system created (PRO-1..4 are Linear's onboarding issues).

Scenario t06's local-connector assertions (`connector == local`, receipt target = local work item id) do not apply
in Linear mode; the Linear-mode equivalents above hold. Raw runner output: `g3-linear-t06.json`.

Status: **Linear connector verified** for create + readback. Not exercised live: timeout-after-create reconciliation
against Linear (covered by unit tests with a mocked transport only) and batched status refresh.
