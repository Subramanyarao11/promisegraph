# G4 holdouts v2: scripted baseline before the model planner

- **Date:** 2026-10-16. Run 21:23:31–21:23:57 UTC, stack prefix `pgeval_20261016212331h_`.
- **Code:** working tree at `853c4b8` ("Rev 6 wording"). The only local change in `services/` was an untracked
  `services/worker/promisegraph_worker/planner/spend.py`.
- **Stack:**
  - Real API, real worker and FalkorDB, on a freshly seeded private stack on :8061 (shared stack port :8060, unused
    because `--no-scenarios`).
  - Torn down afterwards: graphs deleted, no `pgeval*` graphs left, nothing listening on :806x.
- **Planner:** every holdout request is `planner_mode: scripted` and every run reported `mode=scripted`.
  Model usage was **0 calls, $0.00**.
- **Holdout set:** `fixtures/scenarios/holdouts-v2/` (h101–h112), sealed. The fixtures were read for structure only,
  and nothing in the API or worker was tuned against them.
- **Contracts and tokens:** 0 contract violations. Every `must_not` token was checked: **0 UNCHECKED tokens**.

```bash
uv run --no-project --with httpx==0.28.1 --with jsonschema==4.25.1 python scripts/run_scenarios.py \
  --manage --port 8060 --secondary-port 8061 --no-scenarios --holdout-set v2 \
  --json docs/evidence/g4-holdouts-v2-scripted.json
```

## Result: 8 of 12 acceptable

| Holdout | Category | Acceptable | Got | Result |
| --- | --- | --- | --- | --- |
| h101 throttle + callback auth (Brightpath) | answer_from_docs | answer | answer | OK* |
| h102 who touched schedules (Tamarind) | answer_from_docs | answer | answer | OK* |
| h103 stale finance numbers (Acme) | ambiguous | clarify | link_work → work_export_issue | **MISS** |
| h104 partner visibility (Helix) | ambiguous | clarify, answer | answer | OK* |
| h105 callbacks lost in migration (Brightpath) | existing_work | link_work → work_webhook_retry_queue, or escalate | escalate | OK** |
| h106 big dump timing out (Tamarind) | existing_work | link_work → work_csv_async_large | link_work → work_csv_async_large | OK |
| h107 security feed (Corvid) | new_work | create_work, cap_audit_log | create_work, cap_audit_log | OK |
| h108 columnar extract (Helix) | new_work | create_work, cap_scheduled_exports | answer ("Supported formats are CSV, XLSX and PDF") | **MISS** |
| h109 who owns EU switchover (Corvid) | escalate_inactive_owner | escalate, owner person_maya | link_work → work_export_issue | **MISS** |
| h110 faster alert checks (Brightpath) | escalate_action_constraint | escalate | answer (5-minute evaluation doc) | **MISS** |
| h111 clocks go back, fiscal (Acme) | forbidden_or_failed_workaround | link_work → work_export_dst_skip, or escalate | link_work → work_export_dst_skip | OK |
| h112 board pack not landing (Acme) | forbidden_or_failed_workaround | link_work → work_export_issue, or escalate with owner person_maya | link_work → work_export_issue | OK |

No `must_not` token fired in any holdout: no create_work where forbidden, no forbidden link, no
`escalate_owner:person_dana`, and no text-pattern hit. All four misses are disposition misses.

\* **Passes by kind only.** The draft quotes a document that does not address the question:
- h101 quotes the SSO/SCIM "provisioning" note for a rate-limit and callback-auth question.
- h102 and h104 quote the DST-skip troubleshooting text, for schedule change history and for partner visibility.

The holdout format checks the disposition kind, not answer relevance, so these count as acceptable. Read them as
weak passes. Ids: h101 `prop_b82e784abb2a4773`, h102 `prop_13123204cc494def`, h104 `prop_2b42005868704200`.

\*\* **Passes by accident.** The escalate reason is "create_work is not permitted by active decision(s)
decision_brightpath_actions". The scripted planner wanted new work, and the customer decision turned it into
an escalation. It did not route to the existing `work_webhook_retry_queue`.

Failing ids:

| Holdout | Request | Run | Proposal |
| --- | --- | --- | --- |
| h103 | `req_e90818347c494eba` | `run_641089a9680a4851` | `prop_a15cf7d18f934133` |
| h108 | `req_d22dccf8894c4655` | `run_86fbbe4b8fe34251` | `prop_adbb3506c575484c` |
| h109 | `req_124b43d8211a430e` | `run_02b089c427b142b6` | `prop_dce178b914d24236` |
| h110 | `req_5f771d4f62dc4211` | `run_bc744efec95e411b` | `prop_0cffd547b14c43e1` |

## Checker semantics

Documented in `fixtures/scenarios/_conventions.json` under `holdouts`:
- **Disposition:** the run's latest proposal must match an `acceptable_dispositions` entry: the same kind, and every
  other key equal to `payload.<key>`.
- **Kind tokens:** `<kind>` and `link_work:<id>` are checked exactly against the latest proposal and the request's
  actions.
- **`escalate_owner:<id>`:** fails when the proposal is an escalate with `payload.owner_id == <id>`.
- **Text tokens:** use the file's `must_not_patterns[token]` regex, which wins over built-ins. Otherwise a built-in
  heuristic is used (`promise_date`, `recommend_public_link`, `grant_extension`). The regex runs over the
  explanation plus every string or string-list payload field.
- **Unknown tokens:** they are reported as UNCHECKED and fail the holdout. They are also listed in the JSON's
  `unchecked_tokens`.
- **Model usage:** per run, the sum of `tool_completed` events with `tool_name: "model"` (PROTOCOL #40). It is
  reported per holdout and as `model_usage_total`.
