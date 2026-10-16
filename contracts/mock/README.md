# PromiseGraph protocol mock (contract 1.0.0)

An in-memory HTTP mock of Agent A's API, built to the contract. It lets the worker (B) and the web app (C) develop
without the real API. It covers every public route in Contracts §4, the two list routes added by PROTOCOL.md "G0 clarifications (rev 2)" (`GET /requests/{id}/runs` and `GET /proposals?request_id=&run_id=`, both newest first), the Rev 3 routes (items 19–21) and every private route in §7, using the
contract's envelopes, error codes, auth rules, idempotency, leases and fencing, cursors and state transitions.
Each response carries the header `X-PromiseGraph-Mock: true`.

**This is not the product.** It has no FalkorDB and no persistence. Context retrieval is keyword overlap plus BFS
over an in-memory edge list. It never calls a model or Linear. Do not wire it into a release profile.

## Run

```bash
cd contracts/mock
uv sync
uv run uvicorn mock_api.app:app --port 8100
```

- Public base: `http://localhost:8100/api/v1/workspaces/{ws}`. Private base: `http://localhost:8100/internal/v1/workspaces/{ws}`.
- Tokens: `Authorization: Bearer $OPERATOR_TOKEN` (default `dev-operator-token`) and `Bearer $WORKER_TOKEN`
  (default `dev-worker-token`). `WORKSPACE_ACL` works as described in PROTOCOL.md.
- Environment: `AI_ENABLED`, `LINEAR_ENABLED`, `CLOCK_MODE` (`scenario` by default, or `live`), `FAULTS` (a comma list
  applied at startup and on each reset), `MOCK_SEED_DIR=0` (skips `fixtures/seed`), `MOCK_VALIDATE_RESPONSES=0` (turns
  off the response self-check; it is on by default and returns 500 `MOCK_CONTRACT_VIOLATION` if the mock would
  emit an invalid DTO).

## Tests

```bash
cd contracts/mock && uv run pytest -q
```

The harness checks every HTTP response against `contracts/tools/validate.py`. That covers the `{data, meta}` and list
envelopes, `EventsPage`, `ErrorEnvelope` and the DTO for each route. It also reports how many of the 36 contract
routes the run exercised (44 in total). `tests/test_rev2.py` covers the rev 2 clarifications (including #17), and `tests/test_rev3.py` covers Rev 3 (items 19–24).

## Seed

On startup and on each reset:

1. **ws_demo, built-in.** The `fixtures/contracts/examples.json` chain is loaded verbatim: `request_exports_001`
   (awaiting_work), `run_exports_001` (succeeded, 8 events; events 1–7 are synthesized and event 8 is the example),
   `proposal_exports_001`, `action_exports_001` with its receipt, `observation_exports_001`, the example sources,
   evidence and path edges, `decision_exports_001`, `attempt_exports_001` and `authorization_operator_001`. On top
   of that, `request_sso_002` is an open request with no tracked work, and `person_lee` is an inactive owner.
   knowledge_revision is 8.
2. **fixtures/seed/<ws>/.** Every `*.json`, `*.md` and `*.csv` file is imported in filename order through the mock's
   own importer (preview + commit, origin `synthetic`). Where the seed redefines example entities (`customer_acme`,
   `work_export_issue`, …), the seed records become new versions. With the current corpus, ws_demo ends at revision 23.
3. **ws_other.** A small built-in ImportDocument is used only if `fixtures/seed/ws_other/` is absent.

## Mock-only controls (no auth)

| Route | Effect |
| --- | --- |
| `POST /__mock/reset` | Rebuilds all state and clears faults. `{"seed": false}` starts with empty workspaces. |
| `GET /__mock/faults` | Shows the current fault state. |
| `POST /__mock/faults` | Takes a body such as `{"database_unavailable": true}`. Several keys can go in one call. |

| Fault | Behaviour |
| --- | --- |
| `database_unavailable` (sticky bool) | Every route except `/__mock/*` and `/health/live` returns 503 `DEPENDENCY_UNAVAILABLE` (retryable), with `meta.knowledge_revision: 0` meaning "unknown" (Rev 3 #24). `/health/ready` returns 503 too. |
| `stale_next_proposal` (count) | The next `POST /proposals` bumps knowledge_revision first, to simulate a concurrent change, so that proposal comes back `stale`. |
| `lease_expire_now` (action) | Every current lease expires immediately. Holders get 409 `LEASE_LOST`, and the next claim gets the same job with `attempt+1` and a higher fencing token. |
| `local_create_timeout_after_effect` (count) | The next `execute-local` applies the local effect, then returns 503 (retryable) without recording a receipt. The action stays `dispatching`. The worker can either retry `execute-local`, which dedupes by action marker and reads the receipt back, or complete the dispatch job `unknown`, which queues a reconcile job. |
| `duplicate_mutation` (count) | The next successful idempotent command is applied and recorded, but its response is replaced with a retryable 503 ("response lost"). A retry with the same `Idempotency-Key` replays the original result. |

## Choreography implemented (PROTOCOL.md)

- **Intake.** `POST /requests` with `auto_start` creates the run (`queued`/`investigate`) and an investigate job.
  The claim moves the request to `investigating`. Model mode with `AI_ENABLED=false` creates a run that is `failed`
  with `CAPABILITY_DISABLED`; it is never silently switched to scripted.
- **Jobs.** The claim order is due time, then kind (reconcile, dispatch, investigate, refresh), then FIFO. A run has
  at most one live lease. Claim returns 204 when nothing is due.
- **Proposals.** The API assigns validity: `stale` if `based_on_revision` ≠ current; `invalid` for unknown evidence,
  a missing or cancelled or already-linked work target, an inactive owner, a disallowed kind (`allowed_action_types`),
  a forbidden workaround, or an unconfigured linear connector.
- **Commit.**
  - A worker passes `authorization_id: null` and can commit only `auto_commit_kinds` (`answer`, `clarify`). A
    non-null value from a worker returns 422 `POLICY_VIOLATION`.
  - Committing an invalid proposal returns 422 `EVIDENCE_INVALID` if the cause is evidence, otherwise 422
    `POLICY_VIOLATION`. Unknown evidence ids anywhere else (observations, decisions) return 422 `INVALID_INPUT`.
  - An operator commit is the authorization itself. It creates one and moves the run from `awaiting_review` to
    `queued`/`act`. The request stays `awaiting_review` until the effect lands. It appends `action_updated` only if
    no lease is held on the run.
  - Every commit queues one dispatch job. That job waits until the run's investigate job finishes.
- **Dispatch.**
  - `execute-local` re-validates (proposal validity, then target), applies the effect atomically and returns the
    Action with a receipt (`result_hash` is the sha256 of the effect record). Effects by kind:
    - answer and clarify create a draft (`draft_*`; nothing is sent).
    - link_work creates a link.
    - create_work creates a local WorkItem plus its source and evidence.
    - escalate creates an escalation record.
  - The request then moves per Contracts §5.
  - `begin-dispatch` is for linear only. It issues a 15 s permit and sets `dispatching`. Completing that job with a
    linear Receipt records it.
  - If re-validation fails, the action becomes `failed` (REVISION_CONFLICT or POLICY_VIOLATION) and the proposal
    becomes `stale`. Completing the dispatch job `failed` then queues a replan (`usage.replans+1`).
- **After dispatch.** A successful dispatch completion queues exactly one investigate continuation, with
  `checkpoint = {resume_stage: "observe", action_id, previous_job_id}`. Completing that continuation moves the run to `succeeded`, or to
  `waiting_for_input` after a clarify. A dispatch completed `unknown` moves the action to `unknown` and queues a
  reconcile job. A reconcile completed with `established: true` leads to the continuation. Still unknown means the
  run goes to `awaiting_review`.
- **Replies.** A reply to a `waiting_for_input` run requeues the same run, keeping its history and event sequence.
  The new job's checkpoint carries `reply_source_id`. Otherwise the reply creates a follow-up run.
- **Observations.** A `passed` observation of kind operator, customer or acceptance resolves the request and stores a
  verified Attempt. A `failed` one moves the request to `investigating`, records a failed Attempt and queues a
  follow-up run. `connector_status` never resolves anything. The worker can record only `connector_status`.
- **Work-item PATCH.** Local items only; external items return 422 `POLICY_VIOLATION`. A PATCH bumps the item version
  and knowledge_revision, writes an operator source revision and marks consulted proposals `stale`. `done` moves
  linked requests from awaiting_work to awaiting_verification.
- **Decisions.** `record`, `supersede` and `revoke`, with history kept and value types checked per rule.
  `GET /decisions` filters on `scope_entity_type`, `scope_entity_id` and `active`.
- **Imports.** All three formats are supported, as described in PROTOCOL.md. Unchanged records come back as `noop`.
  Commit requires `expected_revision` to equal both the current revision and the preview's base revision. Changed
  entities invalidate the proposals that consulted them.
- **Other API-created jobs (rev 2 #18).** Reply-resume, replan-after-failed-dispatch and operator-resume jobs carry
  the run's last worker checkpoint plus `resume_stage` and one of `reply_source_id` or `resume_reason`.
- **Rev 3 reads (#19).** `GET /evidence/{id}` returns an EvidenceRef, `GET /work-items/{id}` a WorkItem, and
  `GET /observations?request_id=` and `GET /attempts?request_id=` return lists. All four Rev 2/3 lists (runs, proposals,
  observations, attempts) are ordered per #27: (`created_at` or `observed_at` desc, `id` desc).
- **Operator reconciliation (#20).** `POST /actions/{id}/reconcile` is operator only, and its body is validated
  against `ActionReconcile`. It works only on `unknown` actions; anything else returns 409 `INVALID_TRANSITION`, and
  so does a reconcile job leased by a worker. Closing a request cancels its active runs (#25). A version mismatch returns 409 `REVISION_CONFLICT`.
  - The body names a `target: {target_id, target_url}` (#28), which is required for `succeeded`. The mock issues
    the Receipt (`id`, `result_hash`) itself; operators never supply one.
    - For local actions the target must match the effect the mock recorded, and the receipt hash comes from that
      record. If there is no recorded effect, or the target doesn't match it, the call returns 422
      `POLICY_VIOLATION` (#26).
    - For linear actions the mock can't read Linear back. It attributes the operator-named target and hashes that
      readback record. This is a deviation from the real API, which reads the target back from Linear. After that, the normal continuation is
    queued.
  - `failed` marks the action failed and queues a replan.
  - Any queued reconcile job is cancelled, and the operator's `evidence_ids` are kept on the action.
- **Closure (#21).** `POST /requests/{id}/close` is operator only and is validated against `RequestClose`. It
  returns the Request with status `closed` and cancels the request's active runs cooperatively. Closing an
  already-closed request returns 409 `INVALID_TRANSITION`.
- **Refresh target (#22).** Refresh jobs keep `run_id: null`; their checkpoint is
  `{request_id, reason, work_item_ids}`, where `work_item_ids` are the request's linked work.
- **Investigate completion.** Only `proposal_ids` is required in `result`. A `waiting` outcome also needs
  `awaiting: "review"`.
- **Run control.** Cancel is cooperative: queued jobs are cancelled, and leased jobs may still record receipts. If a
  lease is live when the run is cancelled, `run_finished` is appended when that job completes, not at cancel time.
  Resume gives one budget extension, capped at 24 tool calls and 180 s.

## Known deviations from the real API

- **Storage and retrieval.** State is in memory in a single process; a restart is a reset. Search is token overlap.
  Expansion is an undirected BFS (each edge keeps its real direction in the returned path) with a visited set, depth
  ≤ 6 and a truncation flag. `GET /requests/{id}/context`:
  - skips `SUBMITTED` edges, to avoid fanning out through the customer.
  - takes `depth` only and returns at most 20 paths, with `truncated` set if more exist. A `cursor` is ignored.
- **Freshness and leases.** Freshness (stale evidence older than 7 days for `work`/`observation` sources) and decision
  activity use the scenario clock. Lease expiry and timestamps use real UTC.
- **Budget.** The API does not enforce the budget; the worker must report `BUDGET_EXHAUSTED`. Permit expiry is not
  enforced at completion time. Refresh jobs change no data. The mock never returns 429 and has no SSE.
- **Idempotency.** Per rev 2, the scope is (workspace, method, concrete path, caller) and the hash covers the
  canonical body only. Only 2xx results are recorded, so a failed command can be retried with the same key.
- **Run version.** `Run.version` changes only on status or stage transitions, not on event or checkpoint appends.
- **Events the API appends.**
  - operator commit → `action_updated`.
  - dispatch or reconcile completion → `action_updated`.
  - investigate completion → `run_finished` or `waiting_for_input`.
  - operator observation on an action, when the run isn't running → `observation_recorded`.
  - cancel → `run_finished` (deferred to job completion if a lease is live).

  Workers emit only `run_started`, `tool_started`, `tool_completed`, `proposal_ready` and `error`. A checkpoint
  that carries an API-owned type (`action_updated`, `waiting_for_input`, `observation_recorded`, `run_finished`)
  is rejected with 422 `INVALID_INPUT` and nothing is appended (rev 2 #17).

  The API never appends while a worker job holds a lease on that run (rev 2 #6), so chaining `next_event_seq` within a
  job is safe.
- **Auth.** A missing or invalid bearer token returns 401 `UNAUTHORIZED`. A valid token of the wrong class for the
  route returns 403 `UNAUTHORIZED`. A workspace the caller isn't permitted in returns 403 `WORKSPACE_FORBIDDEN`.
- **Health.** `/health/*` returns plain JSON, not contract DTOs.
