# G2 scenario evidence: real API + real worker

- **Date:** 2026-10-16. Run 16:32:19–16:33:44 UTC, graph prefix stem `pgeval_20261016163219`.
- **Code under test:** git `08b9c00` ("Mock: rev 5 and retryable-completion fix"), which includes A's Rev 5 (`676a4e2`).
  The run used a clean `git archive 08b9c00` export. Agent B had uncommitted edits in `services/worker/` during the
  session, and those edits change the results (see "Working-tree run" below), so the committed tree is the
  baseline.
- **Stack:** FalkorDB v4.20.7 (`promisegraph-falkordb-1`, :6380), plus `services/api` (uvicorn) and
  `services/worker` (`python -m promisegraph_worker`, scripted planner), both started by `scripts/stack.sh`. Each
  stack got its own graph prefix and was seeded through `scripts/seed.sh` (the public import path). The protocol
  mock was not used. The shared `pg_` instance on :8000 was not touched.
- **Stacks:**
  - Shared fresh stack on :8020 (prefix `…s_`) for scenarios without preconditions.
  - One dedicated fresh stack per `fresh_seed` scenario on :8021: t10, t12, and t15. t15 ran with
    `FAULTS=local_create_timeout_after_effect`.
  - A fresh stack on :8021 for the holdouts.
  - All stacks were torn down and their graphs deleted afterwards. Afterwards, `GRAPH.LIST` showed no `pgeval*`
    graphs and nothing was listening on :802x.
- **Contract validation:** every response was checked with `contracts/tools/validate.py`. There were **0 contract
  violations** in every run.

## Command

```bash
export STACK_STATE_DIR=<scratch>/stack          # pids + api/worker/seed logs per port
uv run --no-project --with httpx==0.28.1 --with jsonschema==4.25.1 \
  python scripts/run_scenarios.py --manage --port 8020 --holdouts --json docs/evidence/g2-scenarios.json
# single stack instead: scripts/stack.sh up --port 8020 --prefix pgeval_x_ ; run_scenarios.py --base-url http://127.0.0.1:8020 [--only t04,t12]
```

Artifacts:
- `docs/evidence/g2-scenarios.json` holds the full results (step captures summary, every expectation with its
  actual values, and ids).
- `docs/evidence/g2-scenarios.log` is the console output.
- `docs/evidence/g2-scenarios-worktree.json` is the same run against the working tree, including B's uncommitted
  worker edits.

## Per-scenario results (committed `08b9c00`)

**11 of 18 PASS.** The "Required" column counts required expectations that held.

| Scenario | Acceptance | Result | Required | Notes |
| --- | --- | --- | --- | --- |
| t01-answer-supported-export | T01 | **FAIL** | 10/11 | `context_includes attempts attempt_corvid_weekly_xlsx` missing (D1) |
| t02-clarify-provisioning | T02 | PASS | 9/9 | |
| t03-reply-resumes-run | T02, T03 | PASS | 18/18 | same run id resumed; events contiguous |
| t04-link-existing-export-work | T04 | **FAIL** | 15/16 | `context_includes attempts attempt_exports_001` missing (D1) |
| t05-similar-incompatible-task | T05 | PASS | 9/9 | fails on the working tree (D2) |
| t06-create-work-with-receipt | T06 | PASS | 13/13 | `stable_after_restart` checked by a real API process restart: status, receipt target and hash unchanged |
| t07-four-hop-owner-path | T07 | PASS | 10/10 | optional `path_contains_ordered@s3` missed: the request context bundle has 0 paths (D5) |
| t08-stale-truncated-uncertainty | T08 | **FAIL** | 6/7 | `evidence_marked_stale@s3`: the bundle has no evidence for `work_retention_legal_hold` (D5) |
| t09-scoped-failed-workaround | T09 | PASS | 9/9 | |
| t10-superseded-expired-decision | T10 | **FAIL** | 8/9 | second `decision_state@s7`: the new decision is missing from the `active=false` list (S1) |
| t11-conflicting-active-decisions | T11 | PASS | 6/6 | |
| t12-owner-change-invalidates-proposal | T12 | **FAIL** | 6/7 | `context_includes attempts attempt_helix_webhook_retry_backoff` missing (D3); the replan run also fails (D4) |
| t15-unknown-create-reconciles | T15 | PASS | 6/6 | optional `action_updated{status: unknown}` not seen: the action went accepted → succeeded, which the scenario note allows |
| t17-completed-work-needs-verification | T17 | PASS | 6/6 | |
| t18-failed-verification-reopens | T18 | PASS | 6/6 | optional `text_matches@s7` missed: the follow-up explanation does not mention the earlier failure (D7) |
| t20-workspace-isolation | T20 | PASS | 8/8 | |
| t26-reimport-noop-and-revision | T26 | **FAIL** | 3/4 | `source_body_unchanged@s3`: stored body 617 chars vs file 723 chars (S2) |
| t28-similarity-not-duplicate | T28 | **FAIL** | 4/5 | `proposal_kind@s2` = `create_work`, which is not in the allowed kinds (D6) |

### Working-tree run (B's uncommitted worker edits, ~16:30 UTC)

12 of 18 PASS. Compared with the committed tree:
- t01 and t04 now **pass**, because the worker reads capability-scoped memory (fixes D1).
- t05 now **fails** (D2).

The edits were still in progress during the run: `planner/scripted.py` changed at 16:31:45 UTC. An earlier
working-tree run at 16:25 UTC passed 13 of 18, including t05.

## Holdouts (fresh stack, scripted planner)

**1 of 6 acceptable.** Each holdout is checked for an acceptable disposition (kind plus payload fields) and for
its `must_not` items. `create_work` and `link_work:<id>` are checked exactly. `promise_date`,
`grant_extension` and `recommend_public_link` are text heuristics, and their hits need manual review.

| Holdout | Result | Disposition | Finding |
| --- | --- | --- | --- |
| h01-weekly-pdf-acme | MISS | create_work (cap_dashboard_sharing) | Expected an answer. A supported schedule configuration became new work (`must_not create_work` violated). |
| h02-provisioning-brightpath | MISS | escalate | The planner wanted `create_work`, which the Brightpath decision blocked, so it escalated. Expected clarify, or a link to `work_scim_nested_groups`. |
| h03-finance-export-corvid | OK | link_work → work_export_issue | No date promised. |
| h04-chat-alerts-helix | MISS | link_work → work_alert_sensitivity | `must_not link_work:work_alert_sensitivity` violated. It linked to done, unrelated work. |
| h05-purge-extension-helix | MISS | create_work (cap_audit_log) | Expected escalate, answer or clarify under `decision_retention_no_extension`. The capability is also wrong. |
| h06-public-link-acme | MISS (heuristic) | answer | The draft quotes the docs on "Public (no-login) share links" for Acme despite `decision_exports_001`. It also cites an unrelated SSO JIT attempt as a "verified resolution". |

Ids are in `g2-scenarios.json` under `holdouts[]`: request, run and proposal.

## Triaged defects

Owners: **A** = API (`services/api`), **B** = worker (`services/worker`), **S** = scenario/coordinator.

| # | Owner | Scenario / assertion | Evidence (committed run) | Diagnosis |
| --- | --- | --- | --- | --- |
| D1 | B | t01 `context_includes attempts attempt_corvid_weekly_xlsx`; t04 `… attempt_exports_001` | t01: request `req_0b936e6e9fe54883`, run `run_e80fdf9a257e4f73`, attempts present: `attempt_corvid_scim_v2`. t04: run `run_ee3452e1ac1d4ca8`, attempts present: acme_* only. | Both attempts have scope `capability:cap_scheduled_exports`. Committed `read_memory` never queries the capability scope (rev 5 #31). B's uncommitted edits fix this (both pass on the working tree). |
| D2 | B | t05 `proposal_kind@s2` (create_work expected), `rejected_alternative link_work work_s3_destination`, `run_status awaiting_review` | Working tree only: request `req_68fc575e12bc4d78`, run `run_2e1580eeb5a04304`, proposal `prop_9faf3cbc2a4a4043` = **answer**, auto-committed (action `act_068d357433854aea`), run succeeded. Its only rejected alternative is `create_work`: "Documented resolution exists". | Regression in B's uncommitted `planner/facts.py` and `scripted.py`. SFTP delivery is answered from `cap_sftp_export_destination` docs instead of proposing new work. Passes at `08b9c00`. |
| D3 | B (A secondary) | t12 `context_includes attempts attempt_helix_webhook_retry_backoff` | Request `req_a4643a8b1cb444c2`, run `run_e8ad978cc4dd4944`. Present: `attempt_helix_alert_sensitivity`, `attempt_helix_scim_accounts`. | The attempt is scoped to `request:request_helix_webhook_retries`, the earlier Helix request on the same work item. The run's search found that request and the attempt's source `src_attempt_attempt_helix_webhook_retry_backoff`, but `read_memory` only scopes customer, the current request and the capability. So it never loads the prior request's attempts. The worker should read memory for related prior requests and linked work, or A should expose attempts by `work_item_id`. |
| D4 | A (B secondary) | t12, not asserted: replan run s9 | Run `run_cf7a47ef157c4c7c`: `failed`, `BUDGET_EXHAUSTED` "replan budget exhausted (3/2)". All 3 proposals are stale with "REVISION_CONFLICT: evidence ev_src_work_item_work_webhook_retry_queue_r1_1 cites superseded revision 1". Reproduced on a separate stack. | After `PATCH /work-items/work_webhook_retry_queue`, `POST /context/expand` still returns the DEPENDS_ON path `work_webhook_retry_queue → work_queue_v2_cutover` carrying `ev_…_r1_1`, the superseded revision. The API's own proposal validator then rejects that id, so the API hands out evidence it will refuse. The worker repeats the same citation on every replan, and once also reported the item's status as "unknown". `revision_gt` and the optional Leo text still pass, so T12's "replan uses new revision" is only partly met. |
| D5 | A (or S) | t08 `evidence_marked_stale@s3`; t07 optional `path_contains_ordered@s3` | t08 request `req_3e7823f060ef41a4`. `GET /requests/{id}/context` has `related_work=[]`, `paths=[]`, and missing_information "No confirmed capability is linked to this request." The proposal's evidence `ev_src_work_item_work_retention_legal_hold_r1_1` **is** `stale: true` (checked via `GET /evidence`). | Staleness itself works. The request context bundle only follows the request's own ABOUT/TRACKED_BY links, and nothing creates an ABOUT link during investigation, so an un-committed request's bundle never contains the work the run found. Either A builds the bundle from capabilities and work the run found (e.g. the latest proposal's evidence), or the scenario should assert staleness on the proposal's evidence instead of `s3`. |
| D6 | B | t28 `proposal_kind@s2` | Request `req_06c831c06c7d437a`, run `run_785470ab7b1a454a`, proposal `prop_1ae0380885214932` = create_work | A firewall-caused webhook outage gets new work. Allowed kinds are clarify, answer, link_work and escalate. The duplicate and identity guards pass: no RELATED_TO confirmed edge, and no "same outage" text. |
| D7 | B (optional) | t18 optional `text_matches@s7`; holdouts h01, h02, h04, h05, h06 | See the holdout table | Disposition quality of the scripted planner: create_work is overused, done work is linked, and an answer offers a workaround the customer's decision forbids. |
| S1 | S | t10 second `decision_state@s7` (`${s3.decision.id}` active, supersedes `decision_brightpath_actions`) | `GET /decisions?scope…&active=false` returns only `decision_brightpath_actions` (superseded). With no filter, both are returned: `dec_… active supersedes_id=decision_brightpath_actions`. | The API applies `active=false` as an exact filter (non-active only, consistent with PROTOCOL rev 2 #9), so the active replacement cannot appear in the s7 list. Scenario fix: drop `active` from s7, or add a second list step. Product behaviour is correct. |
| S2 | S (or protocol) | t26 `source_body_unchanged@s3` | `GET /sources/20-docs-api-rate-limits?revision=1`: body is 617 chars, which is exactly the file minus its YAML front matter. The file is 723 chars. Revision 2 has `supersedes_revision=1`. | Revision 1 is unchanged. The markdown importer stores the body without the front matter (title, capabilities and observed_at are parsed into fields). The assertion compares against the raw file. Scenario or protocol should state that `SourceRecord.body` excludes front matter, or the assertion should compare against the front-matter-stripped content. |

### Runner interpretations

These are documented in the header of `scripts/run_scenarios.py`:
- **Context assertions on an `await_run` step:** they check the context the run actually used. That is
  `GET /requests/{id}/context`, plus the ids returned by the run's `tool_completed` events, classified by prefix
  (`attempt_`, `decision_`, `work_`). Without the event ids, t01, t04 and t09 would fail on every build, because the
  bundle is request-link-scoped (D5).
- **`work_item_count_delta`:** measured against the count at scenario start. Every scenario starts on a freshly
  seeded stack or in a stack where earlier scenarios created no work it observes.
- **`stable_after_restart`:** restarts only the API process (`stack.sh restart-api`). FalkorDB is shared, so it is
  not restarted.
- **`fresh_seed` and `server env K=V` preconditions:** each such scenario gets its own freshly seeded stack, with
  that env applied.
- **Optional expectations:** they are reported, as above, but never fail a scenario.

---

## Official G2 rerun — commit 4b1944a (2026-10-16)

Clean `git archive HEAD` export, real API + real worker + FalkorDB v4.20.7, fresh graphs per stack, all torn down.
Command: `scripts/run_scenarios.py --manage --port 8050 --secondary-port 8051 --holdouts`.
Raw output: `g2-official.json`, `g2-official.log`.

**Result: scenarios 18/18 PASS; holdouts 4/6 acceptable.**

Honest caveats:
- Holdouts h01–h06 influenced planner fixes during G2 (three genuine bugs were found through them), so 4/6 is
  no longer an unbiased generalisation measure. A sealed set (`holdouts-v2`) written blind to the planner is
  reserved for G4.
- "Acceptable" is disposition-level. h06's answer avoids the forbidden public-link workaround (decision respected)
  but quotes an irrelevant troubleshooting sentence — a substantive answer-quality weakness of lexical scripted
  planning, not counted as a pass for quality.
- Remaining misses h02 (escalates where clarify/link expected) and h05 (resolves "purge" to audit log instead of
  data retention) are lexical-matching limits, with no rule or decision violations.
- T19 restart durability is separately evidenced by `scripts/check_persistence.sh` (PERSISTENCE OK) and the
  runner's real API restart in t06. T21 (database down) on the real stack is pending C's fault profile.
