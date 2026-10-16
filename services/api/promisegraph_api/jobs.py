"""Private worker protocol (contracts §7, PROTOCOL "Job and run choreography").

The API owns enqueueing, retry schedule, lease expiration, continuations and all authoritative transitions.
Leases are fenced: every lease-bound write carries an in-query guard on (status=leased, lease_token,
fencing_token, not expired), so a worker whose lease expired or was re-claimed cannot change state even if
it raced past the Python-level check.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from . import contract
from .core import (JOB_KIND_PRIORITY, MAX_JOB_ATTEMPTS, TERMINAL_RUN, Ctx, active_jobs, append_event, bump,
                   bump_revision, enqueue_job, err_obj, iso_epoch, job_checkpoint, lease_dto, put, put_action,
                   put_request, put_run, put_work_item, source_revision)
from .domain import has_active_run, load, validate_proposal
from .errors import ApiError, conflict, invalid, not_found, policy
from .store import UnitOfWork, _decode
from .util import add_seconds, canonical_json, edge_id, new_id, payload_hash, sha256_hex

CHECKPOINT_MAX_BYTES = 64 * 1024
PERMIT_SECONDS = 15
API_EVENT_TYPES = {"action_updated", "waiting_for_input", "observation_recorded", "run_finished"}
RUN_STAGES = {"investigate", "decide", "prepare", "act", "observe", "verify", "remember"}


# ---- leases -----------------------------------------------------------------------------------------
def check_lease(ctx: Ctx, job_id: str, lease_token: str, fencing_token: int) -> dict[str, Any]:
    job = ctx.store.get(ctx.ws, job_id, "Job")
    if not job:
        raise not_found("job", job_id)
    if (job.get("status") != "leased" or job.get("lease_token") != lease_token
            or int(job.get("fencing_token") or 0) != int(fencing_token)
            or float(job.get("expires_epoch") or 0) < ctx.now_epoch):
        raise conflict("LEASE_LOST", "job lease is not held by this caller (expired, re-claimed or completed)",
                       {"job_id": job_id, "job_status": job.get("status")})
    return job


def claim(ctx: Ctx, cmd: dict[str, Any]) -> dict[str, Any] | None:
    rows = ctx.store.read(ctx.ws, "MATCH (j:Job) WHERE j.kind IN $kinds AND ((j.status = 'queued' AND j.due_epoch <= $now) "
                                  "OR (j.status = 'leased' AND j.expires_epoch < $now)) "
                                  "RETURN properties(j) ORDER BY j.due_epoch, j.seq LIMIT 100",
                          {"kinds": cmd["kinds"], "now": ctx.now_epoch})
    jobs = [_decode(r[0]) for r in rows]
    jobs.sort(key=lambda j: (JOB_KIND_PRIORITY.get(j["kind"], 9), float(j["due_epoch"]), float(j["seq"])))
    for job in jobs:
        uow = UnitOfWork()
        if int(job.get("attempt") or 0) >= MAX_JOB_ATTEMPTS:
            _fail_exhausted_job(ctx, uow, job)
            ctx.store.commit(ctx.ws, uow)
            continue
        token = secrets.token_urlsafe(24)
        fencing = int(job.get("fencing_token") or 0) + 1
        expires = ctx.now_epoch + cmd["lease_seconds"]
        uow.guard_version(job["id"], int(job.get("version") or 0))  # compare-and-set on the fencing token
        uow.node("Job", job["id"], status="leased", lease_token=token, fencing_token=fencing, version=fencing,
                 attempt=int(job.get("attempt") or 0) + 1, expires_epoch=expires, worker_id=cmd["worker_id"],
                 lease_seconds=cmd["lease_seconds"], claimed_at=ctx.now)
        _on_claim(ctx, uow, job)
        ctx.store.commit(ctx.ws, uow)
        claimed = ctx.store.get(ctx.ws, job["id"], "Job")
        return lease_dto(claimed)
    return None


def _on_claim(ctx: Ctx, uow: UnitOfWork, job: dict[str, Any]) -> None:
    if not job.get("run_id"):
        return
    runp = ctx.store.get(ctx.ws, job["run_id"], "Run")
    if not runp:
        return
    run = runp["doc"]
    if run["status"] in TERMINAL_RUN and job["kind"] == "investigate":
        return
    cp = job_checkpoint(job) or {}
    stage = run["stage"]
    if job["kind"] == "investigate":
        stage = cp.get("resume_stage") if cp.get("resume_stage") in RUN_STAGES else "investigate"
    elif job["kind"] in ("dispatch", "reconcile"):
        stage = "act"
    changes: dict[str, Any] = {"stage": stage}
    if run["status"] not in TERMINAL_RUN:
        changes["status"] = "running"
    if job["kind"] == "investigate":
        changes["based_on_revision"] = ctx.revision()
    put_run(uow, bump(run, ctx.now, **changes))
    uow.guard_version(run["id"], run["version"])
    if job["kind"] == "investigate" and stage == "investigate":
        reqp = ctx.store.get(ctx.ws, run["request_id"], "Request")
        if reqp and reqp["doc"]["status"] not in ("resolved", "closed", "investigating"):
            put_request(uow, bump(reqp["doc"], ctx.now, status="investigating"),
                        external_ref=reqp.get("external_ref") or "")


def _fail_exhausted_job(ctx: Ctx, uow: UnitOfWork, job: dict[str, Any]) -> None:
    uow.node("Job", job["id"], status="failed", lease_token="", completed_at=ctx.now)
    if job.get("run_id"):
        runp = ctx.store.get(ctx.ws, job["run_id"], "Run")
        if runp and runp["doc"]["status"] not in TERMINAL_RUN:
            put_run(uow, bump(runp["doc"], ctx.now, status="failed",
                              error=err_obj("LEASE_LOST", f"job {job['id']} exhausted {MAX_JOB_ATTEMPTS} attempts",
                                            retryable=True)))
    if job["kind"] == "dispatch" and job.get("action_id"):
        ap = ctx.store.get(ctx.ws, job["action_id"], "Action")
        if ap and ap["doc"]["status"] == "dispatching":
            # an effect may have happened: never silently redispatch
            put_action(uow, bump(ap["doc"], ctx.now, status="unknown"))


def heartbeat(ctx: Ctx, job_id: str, cmd: dict[str, Any]) -> dict[str, Any]:
    job = check_lease(ctx, job_id, cmd["lease_token"], cmd["fencing_token"])
    uow = UnitOfWork()
    uow.guard_lease(job_id, cmd["lease_token"], cmd["fencing_token"], ctx.now_epoch)
    uow.node("Job", job_id, expires_epoch=ctx.now_epoch + int(job.get("lease_seconds") or 60))
    ctx.store.commit(ctx.ws, uow)
    return lease_dto(ctx.store.get(ctx.ws, job_id, "Job"))


# ---- checkpoint -------------------------------------------------------------------------------------
def checkpoint(ctx: Ctx, uow: UnitOfWork, job_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    job = check_lease(ctx, job_id, cmd["lease_token"], cmd["fencing_token"])
    if not job.get("run_id"):
        raise invalid("this job has no run; checkpoints are run-scoped", {"job_id": "job has no run"})
    cp = cmd["checkpoint"]
    raw = canonical_json(cp)
    if len(raw.encode()) > CHECKPOINT_MAX_BYTES:
        raise invalid("checkpoint exceeds 64 KB", {"checkpoint": f"{len(raw.encode())} bytes"})
    errs = {}
    if not isinstance(cp.get("completed_call_ids"), list) or not all(isinstance(x, str) for x in cp["completed_call_ids"]):
        errs["checkpoint/completed_call_ids"] = "required string array"
    if "pending_action_id" not in cp or not (cp["pending_action_id"] is None or isinstance(cp["pending_action_id"], str)):
        errs["checkpoint/pending_action_id"] = "required string|null"
    if cp.get("stage") not in RUN_STAGES:
        errs["checkpoint/stage"] = "required RunStage"
    if contract.errors("Usage", cp.get("usage")):
        errs["checkpoint/usage"] = "required Usage {tool_calls, replans, active_ms}"
    if errs:
        raise invalid("checkpoint is missing required fields", errs)
    if cmd["event"]["type"] in API_EVENT_TYPES:
        raise invalid(f"event type {cmd['event']['type']} is emitted only by the API",
                      {"event/type": "API-owned event type"})
    runp = load(ctx, "Run", job["run_id"], "run")
    run = runp["doc"]
    # stable step ids: a retried append after a lost response returns the original event
    dup = ctx.store.find(ctx.ws, "RunEvent", {"run_id": run["id"], "step_id": cmd["step_id"]}, limit=1)
    if dup:
        return 200, {"event": dup[0]["doc"], "next_event_seq": int(run["event_seq"])}
    if cmd["expected_event_seq"] != run["event_seq"]:
        raise conflict("REVISION_CONFLICT", "expected_event_seq does not match the run's event sequence",
                       {"current_event_seq": run["event_seq"], "expected_event_seq": cmd["expected_event_seq"]})
    e = cmd["event"]
    payload = dict(e["payload"])
    if e["type"] == "run_started":
        payload.setdefault("mode", run["mode"])
        payload.setdefault("planner_label", "Scripted mode" if run["mode"] == "scripted" else "Model mode")
    ev, new_run = append_event(ctx, uow, run, e["type"], payload, step_id=cmd["step_id"], tool_name=e.get("tool_name"))
    changes: dict[str, Any] = {"stage": cp["stage"], "usage": cp["usage"]}
    if new_run["status"] == "queued":
        changes["status"] = "running"
    new_run = bump(new_run, ctx.now, **changes)
    uow.guard_version(run["id"], run["version"])
    put_run(uow, new_run, checkpoint=cp)
    uow.node("Job", job_id, checkpoint=raw)
    uow.guard_lease(job_id, cmd["lease_token"], cmd["fencing_token"], ctx.now_epoch)
    return 200, {"event": ev, "next_event_seq": ev["seq"]}


# ---- effects ----------------------------------------------------------------------------------------
def effect_request_status(kind: str, work_status: str | None) -> str:
    if kind == "answer":
        return "awaiting_verification"
    if kind == "clarify":
        return "awaiting_input"
    if kind == "link_work":
        return "awaiting_verification" if work_status == "done" else "awaiting_work"
    if kind == "create_work":
        return "awaiting_work"
    return "escalated"


def _apply_local_effect(ctx: Ctx, uow: UnitOfWork, action: dict[str, Any], prop: dict[str, Any],
                        reqp: dict[str, Any], revision: int) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Write the real local effect. Returns (receipt, readback_doc, new_revision)."""
    kind, payload = prop["kind"], prop["payload"]
    req = reqp["doc"]
    new_req_fields: dict[str, Any] = {}
    work_status = None
    if kind in ("answer", "clarify"):
        target_id = f"draft_{action['id']}"
        doc = {"id": target_id, "kind": kind, "request_id": req["id"], "action_id": action["id"],
               "status": "draft", "sent": False, "created_at": ctx.now,
               **({"text": payload["draft_text"], "resolution_criteria": payload["resolution_criteria"]}
                  if kind == "answer" else {"question": payload["question"], "missing_fields": payload["missing_fields"]})}
        uow.node("Draft", target_id, doc=doc, request_id=req["id"], title=f"{kind} draft")
        uow.edge("HAS_DRAFT", edge_id("HAS_DRAFT", req["id"], target_id), req["id"], target_id)
        readback = doc
    elif kind == "escalate":
        target_id = f"case_{action['id']}"
        doc = {"id": target_id, "request_id": req["id"], "action_id": action["id"], "reason": payload["reason"],
               "owner_id": payload.get("owner_id"), "team_id": payload.get("team_id"), "status": "open",
               "created_at": ctx.now}
        uow.node("EscalationCase", target_id, doc=doc, request_id=req["id"], title="Internal review case")
        uow.edge("HAS_CASE", edge_id("HAS_CASE", req["id"], target_id), req["id"], target_id)
        if payload.get("owner_id"):
            new_req_fields["owner_id"] = payload["owner_id"]
        readback = doc
    elif kind == "link_work":
        wid = payload["work_item_id"]
        wp = ctx.store.get(ctx.ws, wid, "WorkItem")
        work_status = wp["doc"]["status"]
        target_id = edge_id("TRACKED_BY", req["id"], wid)  # normalized: repeated links MERGE onto one edge
        uow.edge("TRACKED_BY", target_id, req["id"], wid, evidence_ids=prop["evidence_ids"][:100], confirmed=True,
                 via="action", action_id=action["id"])
        new_req_fields["linked_work_ids"] = list(dict.fromkeys((req.get("linked_work_ids") or []) + [wid]))
        readback = {"edge_id": target_id, "type": "TRACKED_BY", "from_id": req["id"], "to_id": wid}
        revision = bump_revision(uow, revision)
    else:  # create_work (local connector)
        wid = f"work_{action['id']}"
        body = canonical_json({"title": payload["title"], "description": payload["description"],
                               "acceptance_criteria": payload["acceptance_criteria"], "owner_id": payload.get("owner_id"),
                               "capability_id": payload.get("capability_id"), "dependency_ids": payload["dependency_ids"],
                               "request_id": req["id"], "action_id": action["id"]})
        src = source_revision(ctx, uow, f"src_work_item_{wid}", title=f"Work item {wid}: {payload['title']}"[:300],
                              kind="work", origin="owned_permitted", body=body, observed_at=ctx.now, head=None,
                              head_known=True, excerpts=[(body[:4000], "$")])
        ev = src["evidence_ids"]
        doc = {"id": wid, "workspace_id": ctx.ws, "title": payload["title"], "kind": "ticket", "status": "open",
               "owner_id": payload.get("owner_id"), "connector": "local", "external_id": None, "external_url": None,
               "forecast_at": None, "observed_at": ctx.now, "evidence_ids": (ev + prop["evidence_ids"])[:100],
               "version": 1}
        put_work_item(uow, doc, description=payload["description"], source_id=src["source_id"],
                      acceptance_criteria=payload["acceptance_criteria"], request_id=req["id"], action_id=action["id"])
        uow.edge("SUPPORTED_BY", edge_id("SUPPORTED_BY", wid, src["source_id"]), wid, src["source_id"],
                 evidence_ids=ev, confirmed=True, via="action")
        uow.edge("TRACKED_BY", edge_id("TRACKED_BY", req["id"], wid), req["id"], wid, evidence_ids=ev, confirmed=True,
                 via="action", action_id=action["id"])
        if payload.get("capability_id"):
            uow.edge("IMPLEMENTED_BY", edge_id("IMPLEMENTED_BY", payload["capability_id"], wid), payload["capability_id"],
                     wid, evidence_ids=ev, confirmed=True, via="action")
        for dep in payload["dependency_ids"]:
            uow.edge("DEPENDS_ON", edge_id("DEPENDS_ON", wid, dep), wid, dep, evidence_ids=ev, confirmed=True, via="action")
        if payload.get("owner_id"):
            uow.edge("OWNED_BY", edge_id("OWNED_BY", wid, payload["owner_id"]), wid, payload["owner_id"],
                     evidence_ids=ev, confirmed=True, via="action")
        new_req_fields["linked_work_ids"] = list(dict.fromkeys((req.get("linked_work_ids") or []) + [wid]))
        target_id = wid
        readback = doc
        revision = bump_revision(uow, revision)
    receipt = {"id": new_id("rcpt"), "connector": "local", "target_id": target_id[:128], "target_url": None,
               "observed_at": ctx.now, "result_hash": sha256_hex(canonical_json(readback))}
    _finish_effect(ctx, uow, action, reqp, receipt, effect_request_status(kind, work_status), new_req_fields)
    return receipt, readback, revision


def _finish_effect(ctx: Ctx, uow: UnitOfWork, action: dict[str, Any], reqp: dict[str, Any], receipt: dict[str, Any],
                   new_status: str, req_fields: dict[str, Any]) -> dict[str, Any]:
    req = reqp["doc"]
    uow.node("Receipt", receipt["id"], doc=receipt, action_id=action["id"], title=f"Receipt {receipt['target_id']}")
    uow.edge("HAS_RECEIPT", edge_id("HAS_RECEIPT", action["id"], receipt["id"]), action["id"], receipt["id"])
    new_action = bump(action, ctx.now, status="succeeded", receipt=receipt, error=None)
    put_action(uow, new_action)
    status = new_status if req["status"] not in ("resolved", "closed") else req["status"]
    put_request(uow, bump(req, ctx.now, status=status, **req_fields), external_ref=reqp.get("external_ref") or "")
    uow.guard_version(req["id"], req["version"])
    return new_action


def _revalidate(ctx: Ctx, action: dict[str, Any], ap: dict[str, Any], prop: dict[str, Any], req: dict[str, Any],
                revision: int, expected_revision: int | None = None) -> tuple[str, str, list[str]] | None:
    """Immediately-before-effect revalidation. Returns (code, validity, reasons) on failure."""
    basis = int(ap.get("basis_revision") or 0)
    if revision != basis or (expected_revision is not None and expected_revision != revision):
        return ("REVISION_CONFLICT", "stale",
                [f"REVISION_CONFLICT: knowledge revision is {revision}, action was accepted at {basis}"])
    if payload_hash(prop["payload"]) != ap.get("payload_hash"):
        return ("POLICY_VIOLATION", "invalid", ["POLICY_VIOLATION: payload differs from authorized payload"])
    check = dict(prop)
    validity, reasons, _ = validate_proposal(ctx, check, req, revision)
    if validity == "stale":
        return ("REVISION_CONFLICT", "stale", reasons)
    if validity == "invalid":
        return ("POLICY_VIOLATION", "invalid", reasons)
    return None


def _fail_action(ctx: Ctx, uow: UnitOfWork, action: dict[str, Any], prop: dict[str, Any], code: str, validity: str,
                 reasons: list[str]) -> ApiError:
    err = err_obj(code, reasons[0] if reasons else code, details={"validation_reasons": reasons[:10]})
    put_action(uow, bump(action, ctx.now, status="failed", error=err))
    pdoc = dict(prop)
    pdoc["validity"] = validity
    pdoc["validation_reasons"] = list(dict.fromkeys((prop.get("validation_reasons") or []) + reasons))[:30]
    pdoc["version"] = int(prop["version"]) + 1
    put(uow, "Proposal", pdoc, validity=validity)
    status = 409 if code == "REVISION_CONFLICT" else 422
    return ApiError(status, code, "dispatch blocked: " + (reasons[0] if reasons else code),
                    {"validation_reasons": reasons[:10], "action_id": action["id"]})


def _dispatch_job(ctx: Ctx, action_id: str, cmd: dict[str, Any]) -> dict[str, Any]:
    job = check_lease(ctx, cmd["job_id"], cmd["lease_token"], cmd["fencing_token"])
    if job["kind"] != "dispatch" or job.get("action_id") != action_id:
        raise conflict("LEASE_LOST", "lease is not a dispatch lease for this action", {"job_id": job["id"]})
    return job


def execute_local(ctx: Ctx, uow: UnitOfWork, action_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    _dispatch_job(ctx, action_id, cmd)
    uow.guard_lease(cmd["job_id"], cmd["lease_token"], cmd["fencing_token"], ctx.now_epoch)
    ap = load(ctx, "Action", action_id, "action")
    action = ap["doc"]
    if action["status"] == "succeeded" and action.get("receipt"):
        return 200, action  # effect already applied and receipted (lost-response recovery)
    if action["status"] != "accepted":
        raise conflict("INVALID_TRANSITION", f"action is {action['status']}", {"action_status": action["status"]})
    if cmd["expected_action_version"] != action["version"]:
        raise conflict("REVISION_CONFLICT", "action version changed", {"current_version": action["version"]})
    if action["connector"] != "local":
        raise policy("execute-local only applies to local-connector actions", {"connector": action["connector"]})
    prop = load(ctx, "Proposal", action["proposal_id"], "proposal")["doc"]
    reqp = load(ctx, "Request", action["request_id"], "request")
    revision = ctx.revision()
    failure = _revalidate(ctx, action, ap, prop, reqp["doc"], revision)
    if failure:
        code, validity, reasons = failure
        err = _fail_action(ctx, uow, action, prop, code, validity, reasons)
        return err.status, err
    uow.guard_version(action_id, action["version"])
    receipt, readback, _rev = _apply_local_effect(ctx, uow, action, prop, reqp, revision)
    new_action = bump(action, ctx.now, status="succeeded", receipt=receipt, error=None)
    ctx.post_commit = lambda: _verify_readback(ctx, receipt, readback)
    if "local_create_timeout_after_effect" in ctx.settings.faults and prop["kind"] == "create_work":
        ctx.fault_after_commit = True
    return 200, new_action


def _verify_readback(ctx: Ctx, receipt: dict[str, Any], readback: dict[str, Any]) -> None:
    """Read the target back from the graph after commit and check it matches the receipt hash."""
    tid = receipt["target_id"]
    if tid.startswith("e_"):
        rows = ctx.store.read(ctx.ws, "MATCH (a:E)-[r:TRACKED_BY {id: $id}]->(b:E) RETURN r.id, a.id, b.id", {"id": tid})
        ok = bool(rows) and sha256_hex(canonical_json({"edge_id": rows[0][0], "type": "TRACKED_BY", "from_id": rows[0][1],
                                                       "to_id": rows[0][2]})) == receipt["result_hash"]
    else:
        p = ctx.store.get(ctx.ws, tid)
        ok = bool(p) and sha256_hex(canonical_json(p["doc"])) == receipt["result_hash"]
    if not ok:
        raise ApiError(500, "READBACK_MISMATCH", f"local target {tid} did not read back as receipted")


def begin_dispatch(ctx: Ctx, uow: UnitOfWork, action_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    _dispatch_job(ctx, action_id, cmd)
    uow.guard_lease(cmd["job_id"], cmd["lease_token"], cmd["fencing_token"], ctx.now_epoch)
    ap = load(ctx, "Action", action_id, "action")
    action = ap["doc"]
    if action["status"] != "accepted":
        raise conflict("INVALID_TRANSITION", f"action is {action['status']}", {"action_status": action["status"]})
    if cmd["expected_action_version"] != action["version"]:
        raise conflict("REVISION_CONFLICT", "action version changed", {"current_version": action["version"]})
    if action["connector"] == "local":
        raise policy("local actions use execute-local", {"connector": "local"})
    prop = load(ctx, "Proposal", action["proposal_id"], "proposal")["doc"]
    reqp = load(ctx, "Request", action["request_id"], "request")
    revision = ctx.revision()
    failure = _revalidate(ctx, action, ap, prop, reqp["doc"], revision, cmd["expected_revision"])
    if failure:
        code, validity, reasons = failure
        err = _fail_action(ctx, uow, action, prop, code, validity, reasons)
        return err.status, err
    # remote preconditions: changed provider versions must first be persisted as observations
    for pc in cmd["remote_preconditions"]:
        wp = ctx.store.get(ctx.ws, pc["target_id"], "WorkItem")
        if wp and wp.get("provider_version") and pc.get("provider_version") \
                and wp["provider_version"] != pc["provider_version"]:
            err = _fail_action(ctx, uow, action, prop, "REVISION_CONFLICT", "stale",
                               [f"REVISION_CONFLICT: remote target {pc['target_id']} changed; record an observation "
                                "and revalidate"])
            return err.status, err
    permit_id = new_id("permit")
    expires = add_seconds(ctx.now_dt, PERMIT_SECONDS)
    phash = payload_hash(prop["payload"])
    new_action = bump(action, ctx.now, status="dispatching")
    uow.guard_version(action_id, action["version"])
    put_action(uow, new_action, permit_id=permit_id)
    uow.node("Permit", permit_id, action_id=action_id, job_id=cmd["job_id"], expires_at=expires, payload_hash=phash,
             remote_preconditions=json.dumps(cmd["remote_preconditions"]), created_at=ctx.now, title="Dispatch permit")
    return 200, {"permit_id": permit_id, "expires_at": expires, "payload_hash": phash,
                 "action_version": new_action["version"]}


# ---- completion -------------------------------------------------------------------------------------
def complete(ctx: Ctx, uow: UnitOfWork, job_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    job = check_lease(ctx, job_id, cmd["lease_token"], cmd["fencing_token"])
    uow.guard_lease(job_id, cmd["lease_token"], cmd["fencing_token"], ctx.now_epoch)
    outcome, result = cmd["outcome"], cmd["result"]
    run_doc = None
    next_job: str | None = None
    final_status = "completed"
    runp = ctx.store.get(ctx.ws, job["run_id"], "Run") if job.get("run_id") else None
    if job["kind"] == "investigate":
        if not isinstance(result.get("proposal_ids"), list):
            raise invalid("investigate completion result requires proposal_ids", {"result/proposal_ids": "required"})
        run_doc, next_job, final_status = _complete_investigate(ctx, uow, job, runp, outcome, result)
        if run_doc is not None and runp and run_doc["status"] != runp["doc"]["status"]:
            run_doc = _completion_event(ctx, uow, job, run_doc)
    elif job["kind"] == "dispatch":
        run_doc, next_job, final_status = _complete_dispatch(ctx, uow, job, runp, outcome, result)
    elif job["kind"] == "reconcile":
        run_doc, next_job, final_status = _complete_reconcile(ctx, uow, job, runp, outcome, result)
    else:  # refresh
        if outcome == "retryable_failure" and int(job.get("attempt") or 1) < MAX_JOB_ATTEMPTS:
            final_status = "queued"
    job_fields: dict[str, Any] = {"status": final_status, "completion_id": cmd["completion_id"], "outcome": outcome,
                                  "result": json.dumps(result)[:60000], "completed_at": ctx.now}
    if final_status == "queued":
        backoff = 2 ** int(job.get("attempt") or 1)
        job_fields.update(lease_token="", expires_epoch=0.0, due_epoch=ctx.now_epoch + backoff)
        next_job = job_id
    else:
        job_fields.update(lease_token="")
    uow.node("Job", job_id, **job_fields)
    receipt = {"job_id": job_id, "completion_id": cmd["completion_id"], "outcome": outcome, "recorded_at": ctx.now,
               "run": run_doc if run_doc is not None else (runp["doc"] if runp else None), "next_job_id": next_job}
    return 200, receipt


def _completion_event(ctx: Ctx, uow: UnitOfWork, job: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """API-authored events for transitions it performs during job completion (PROTOCOL rev 2 #6)."""
    cp = job_checkpoint(job) or {}
    action_id = cp.get("action_id")
    if run["status"] in ("succeeded", "failed"):
        reqp = ctx.store.get(ctx.ws, run["request_id"], "Request")
        _e, run = append_event(ctx, uow, run, "run_finished", {"run_status": run["status"],
                               "request_status": reqp["doc"]["status"] if reqp else None, "action_id": action_id})
    elif run["status"] == "waiting_for_input" and action_id:
        prop_q = None
        ap = ctx.store.get(ctx.ws, action_id, "Action")
        if ap:
            pp = ctx.store.get(ctx.ws, ap["doc"]["proposal_id"], "Proposal")
            prop_q = pp["doc"]["payload"].get("question") if pp else None
        _e, run = append_event(ctx, uow, run, "waiting_for_input", {"action_id": action_id, "question": prop_q})
    else:
        return run
    put_run(uow, run)
    return run


def _save_run(uow: UnitOfWork, runp: dict[str, Any], new: dict[str, Any], **x: Any) -> dict[str, Any]:
    uow.guard_version(runp["doc"]["id"], runp["doc"]["version"])
    put_run(uow, new, **x)
    return new


def _result_error(result: dict[str, Any], default_code: str) -> dict[str, Any]:
    e = result.get("error")
    if isinstance(e, dict) and e.get("code"):
        return err_obj(str(e["code"]), str(e.get("message") or e["code"]), bool(e.get("retryable", False)))
    code = result.get("error_code") or default_code
    return err_obj(str(code), str(result.get("summary") or result.get("message") or code))


def _complete_investigate(ctx: Ctx, uow: UnitOfWork, job: dict[str, Any], runp: dict[str, Any] | None, outcome: str,
                          result: dict[str, Any]) -> tuple[dict | None, str | None, str]:
    if not runp:
        return None, None, "completed"
    run = runp["doc"]
    cp = job_checkpoint(job) or {}
    blocked = ctx.store.find(ctx.ws, "Job", {"run_id": run["id"], "status": "blocked", "blocked_by": job["id"]})
    if run["status"] == "cancelled":
        for b in blocked:
            uow.node("Job", b["id"], status="cancelled")
        return run, None, "completed"
    if outcome in ("retryable_failure", "unknown") and int(job.get("attempt") or 1) < MAX_JOB_ATTEMPTS:
        new = _save_run(uow, runp, bump(run, ctx.now, status="queued"))
        return new, job["id"], "queued"
    if outcome in ("failed", "retryable_failure", "unknown"):
        for b in blocked:
            uow.node("Job", b["id"], status="cancelled")
            ap = ctx.store.get(ctx.ws, b.get("action_id") or "", "Action")
            if ap and ap["doc"]["status"] == "accepted":
                put_action(uow, bump(ap["doc"], ctx.now, status="cancelled"))
        new = _save_run(uow, runp, bump(run, ctx.now, status="failed", error=_result_error(result, "JOB_FAILED")))
        return new, None, "completed"
    if blocked:
        b = blocked[0]
        uow.node("Job", b["id"], status="queued", blocked_by="", due_epoch=ctx.now_epoch)
        new = _save_run(uow, runp, bump(run, ctx.now, status="queued", stage="act"))
        return new, b["id"], "completed"
    if outcome == "waiting":
        awaiting = result.get("awaiting")
        if awaiting == "input":
            new = _save_run(uow, runp, bump(run, ctx.now, status="waiting_for_input"))
        else:
            new = _save_run(uow, runp, bump(run, ctx.now, status="awaiting_review", stage="decide"))
            reqp = ctx.store.get(ctx.ws, run["request_id"], "Request")
            if reqp and reqp["doc"]["status"] not in ("resolved", "closed"):
                put_request(uow, bump(reqp["doc"], ctx.now, status="awaiting_review"),
                            external_ref=reqp.get("external_ref") or "")
        return new, None, "completed"
    # succeeded
    action_id = cp.get("action_id") if cp.get("resume_stage") == "observe" else None
    if action_id:
        ap = ctx.store.get(ctx.ws, action_id, "Action")
        if ap and ap["doc"]["kind"] == "clarify" and ap["doc"]["status"] == "succeeded":
            new = _save_run(uow, runp, bump(run, ctx.now, status="waiting_for_input", stage="observe"))
            return new, None, "completed"
    stage = result.get("stage") if result.get("stage") in RUN_STAGES else "remember"
    new = _save_run(uow, runp, bump(run, ctx.now, status="succeeded", stage=stage, error=None))
    return new, None, "completed"


def _continuation(ctx: Ctx, uow: UnitOfWork, run: dict[str, Any], action: dict[str, Any],
                  prev_job_id: str | None = None) -> tuple[dict, str | None]:
    if run["status"] == "cancelled":
        return run, None
    cp = {"resume_stage": "observe", "action_id": action["id"], "previous_job_id": prev_job_id}
    jid = enqueue_job(ctx, uow, "investigate", run_id=run["id"], request_id=run["request_id"], action_id=None,
                      checkpoint=cp)
    return bump(run, ctx.now, status="queued", stage="observe"), jid


def _replan(ctx: Ctx, uow: UnitOfWork, run: dict[str, Any], action: dict[str, Any], err: dict | None,
            prev_job_id: str | None = None) -> tuple[dict, str | None]:
    if run["status"] == "cancelled":
        return run, None
    usage = dict(run["usage"])
    if usage["replans"] >= run["budget"]["max_replans"]:
        return bump(run, ctx.now, status="failed", error=err_obj("BUDGET_EXHAUSTED", "replanning budget exhausted after "
                                                                  "a blocked dispatch", retryable=True)), None
    usage["replans"] += 1
    runp = ctx.store.get(ctx.ws, run["id"], "Run") or {}
    last = json.loads(runp["checkpoint"]) if runp.get("checkpoint") else {}
    cp = {**last, "resume_stage": "investigate",  # rev2 #18: last worker checkpoint + resume_stage + reason
          "resume_reason": f"replan after {(err or {}).get('code', 'FAILED')} on action {action['id']}"}
    jid = enqueue_job(ctx, uow, "investigate", run_id=run["id"], request_id=run["request_id"], checkpoint=cp)
    return bump(run, ctx.now, status="queued", stage="investigate", usage=usage), jid


def _external_success(ctx: Ctx, uow: UnitOfWork, action: dict[str, Any], receipt: dict[str, Any]) -> None:
    """Persist a provider receipt for an external (linear) create_work action and its local mirror."""
    prop = load(ctx, "Proposal", action["proposal_id"], "proposal")["doc"]
    reqp = load(ctx, "Request", action["request_id"], "request")
    req = reqp["doc"]
    fields: dict[str, Any] = {}
    status = effect_request_status(action["kind"], None)
    if action["kind"] == "create_work":
        payload = prop["payload"]
        wid = "work_ext_" + sha256_hex(f"{action['connector']}|{receipt['target_id']}")[:20]
        body = canonical_json({"receipt": receipt, "title": payload["title"], "request_id": req["id"]})
        src = source_revision(ctx, uow, f"src_work_item_{wid}", title=f"External work {receipt['target_id']}",
                              kind="work", origin="owned_permitted", body=body, observed_at=receipt["observed_at"])
        doc = {"id": wid, "workspace_id": ctx.ws, "title": payload["title"], "kind": "ticket", "status": "open",
               "owner_id": payload.get("owner_id"), "connector": action["connector"],
               "external_id": receipt["target_id"], "external_url": receipt.get("target_url"), "forecast_at": None,
               "observed_at": receipt["observed_at"], "evidence_ids": src["evidence_ids"], "version": 1}
        put_work_item(uow, doc, description=payload["description"], source_id=src["source_id"])
        uow.edge("TRACKED_BY", edge_id("TRACKED_BY", req["id"], wid), req["id"], wid, evidence_ids=src["evidence_ids"],
                 confirmed=True, via="action", action_id=action["id"])
        uow.edge("SUPPORTED_BY", edge_id("SUPPORTED_BY", wid, src["source_id"]), wid, src["source_id"],
                 evidence_ids=src["evidence_ids"], confirmed=True, via="action")
        fields["linked_work_ids"] = list(dict.fromkeys((req.get("linked_work_ids") or []) + [wid]))
        bump_revision(uow, ctx.revision())
    _finish_effect(ctx, uow, action, reqp, receipt, status, fields)


def _receipt_from(result: dict[str, Any], action: dict[str, Any]) -> dict[str, Any] | None:
    r = result.get("receipt")
    if not isinstance(r, dict):
        return None
    errs = contract.resource_errors("Receipt", r)
    if errs:
        raise invalid("result.receipt is not a valid Receipt", {f"result/receipt/{k}": v for k, v in errs.items()})
    if r["connector"] != action["connector"]:
        raise policy("receipt connector does not match the action's connector",
                     {"receipt_connector": r["connector"], "action_connector": action["connector"]})
    return r


def _complete_dispatch(ctx: Ctx, uow: UnitOfWork, job: dict[str, Any], runp: dict[str, Any] | None, outcome: str,
                       result: dict[str, Any]) -> tuple[dict | None, str | None, str]:
    ap = load(ctx, "Action", job["action_id"], "action")
    action = ap["doc"]
    run = runp["doc"] if runp else None
    next_job = None
    new_action = action
    if action["status"] == "succeeded":
        new_run, next_job = _continuation(ctx, uow, run, action, job["id"])
    elif outcome == "succeeded":
        if action["connector"] == "local":
            raise policy("local action has no persisted effect; call execute-local first", {"action_id": action["id"]})
        if action["status"] not in ("dispatching", "unknown"):
            raise conflict("INVALID_TRANSITION", f"action is {action['status']}", {"action_status": action["status"]})
        receipt = _receipt_from(result, action)
        if receipt is None:
            raise invalid("succeeded external dispatch requires result.receipt", {"result/receipt": "required"})
        _external_success(ctx, uow, action, receipt)
        new_action = bump(action, ctx.now, status="succeeded", receipt=receipt)
        new_run, next_job = _continuation(ctx, uow, run, new_action, job["id"])
    elif outcome == "unknown":
        if action["status"] in ("dispatching", "accepted"):
            new_action = bump(action, ctx.now, status="unknown",
                              error=err_obj("UNKNOWN_OUTCOME", "dispatch outcome unknown; reconciling before any redispatch"))
            put_action(uow, new_action)
        next_job = enqueue_job(ctx, uow, "reconcile", run_id=run["id"], request_id=run["request_id"],
                               action_id=action["id"], checkpoint={"resume_stage": "act", "action_id": action["id"],
                                                                   "previous_job_id": job["id"]})
        new_run = bump(run, ctx.now, status="queued", stage="act")
    elif outcome == "retryable_failure" and action["status"] == "accepted" \
            and int(job.get("attempt") or 1) < MAX_JOB_ATTEMPTS:
        # nothing was dispatched (no permit taken): safe to retry the same dispatch job
        new_run = _api_event(ctx, uow, bump(run, ctx.now, status="queued"), action, "accepted")
        _save_run(uow, runp, new_run)
        return new_run, job["id"], "queued"
    else:  # failed (or retryable after a permit: treated as failed, never blind redispatch)
        if action["status"] in ("accepted", "dispatching"):
            new_action = bump(action, ctx.now, status="failed", error=_result_error(result, "DISPATCH_FAILED"))
            put_action(uow, new_action)
        err = new_action.get("error") or {}
        if err.get("code") in ("REVISION_CONFLICT", "POLICY_VIOLATION"):
            new_run, next_job = _replan(ctx, uow, run, new_action, err, job["id"])
        else:
            new_run = bump(run, ctx.now, status="failed", error=err or err_obj("DISPATCH_FAILED", "dispatch failed"))
    new_run = _api_event(ctx, uow, new_run, new_action, new_action["status"])
    _save_run(uow, runp, new_run)
    return new_run, next_job, "completed"


def _api_event(ctx: Ctx, uow: UnitOfWork, run: dict[str, Any], action: dict[str, Any], status: str) -> dict[str, Any]:
    _e, run = append_event(ctx, uow, run, "action_updated",
                           {"action_id": action["id"], "status": status, "connector": action["connector"]})
    return run


def _complete_reconcile(ctx: Ctx, uow: UnitOfWork, job: dict[str, Any], runp: dict[str, Any] | None, outcome: str,
                        result: dict[str, Any]) -> tuple[dict | None, str | None, str]:
    ap = load(ctx, "Action", job["action_id"], "action")
    action = ap["doc"]
    run = runp["doc"]
    next_job = None
    new_action = action
    established = bool(result.get("established")) and outcome == "succeeded"
    if action["status"] == "succeeded":
        new_run, next_job = _continuation(ctx, uow, run, action, job["id"])
    elif established and isinstance(result.get("receipt"), dict):
        receipt = _receipt_from(result, action)
        if action["connector"] == "local":
            raise policy("local actions reconcile from their persisted receipt only", {"action_id": action["id"]})
        _external_success(ctx, uow, action, receipt)
        new_action = bump(action, ctx.now, status="succeeded", receipt=receipt, error=None)
        new_run, next_job = _continuation(ctx, uow, run, new_action, job["id"])
    elif established:
        # proven absence: confirmed failure, safe to replan
        new_action = bump(action, ctx.now, status="failed",
                          error=err_obj("DISPATCH_FAILED", "reconciliation established that no effect was created"))
        put_action(uow, new_action)
        new_run, next_job = _replan(ctx, uow, run, new_action, {"code": "DISPATCH_FAILED"}, job["id"])
    else:
        # still unknown: stop automatic redispatch; operator reconciliation required
        new_run = bump(run, ctx.now, status="awaiting_review", stage="act",
                       error=err_obj("UNKNOWN_OUTCOME", "external outcome unresolved; operator reconciliation required"))
        reqp = ctx.store.get(ctx.ws, run["request_id"], "Request")
        if reqp and reqp["doc"]["status"] not in ("resolved", "closed"):
            put_request(uow, bump(reqp["doc"], ctx.now, status="awaiting_review"),
                        external_ref=reqp.get("external_ref") or "")
    new_run = _api_event(ctx, uow, new_run, new_action, new_action["status"])
    _save_run(uow, runp, new_run)
    return new_run, next_job, "completed"


# ---- operator reconciliation (PROTOCOL rev 3 #20) ---------------------------------------------------
def operator_reconcile(ctx: Ctx, uow: UnitOfWork, action_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    from .core import missing_evidence
    ap = load(ctx, "Action", action_id, "action")
    action = ap["doc"]
    if cmd["expected_action_version"] != action["version"]:
        raise conflict("REVISION_CONFLICT", "action version changed", {"current_version": action["version"]})
    if action["status"] != "unknown":
        raise conflict("INVALID_TRANSITION", f"action is {action['status']}; only unknown actions are reconciled",
                       {"action_status": action["status"]})
    missing = missing_evidence(ctx, cmd["evidence_ids"])
    if missing:
        from .errors import evidence_invalid
        raise evidence_invalid("unknown evidence ids", {"missing": missing[:20]})
    runp = load(ctx, "Run", action["run_id"], "run")
    run = runp["doc"]
    if any(j["kind"] in ("dispatch", "reconcile") and j["status"] in ("queued", "leased")
           for j in active_jobs(ctx, run["id"])):
        raise conflict("INVALID_TRANSITION", "a reconcile/dispatch job is still active for this run", {})
    if cmd["outcome"] == "succeeded":
        target = cmd.get("target")
        if not target:
            raise invalid("succeeded reconciliation requires a target", {"target": "required for succeeded"})
        receipt = _readback_receipt(ctx, action, target)
        if action["connector"] == "local":
            reqp = load(ctx, "Request", action["request_id"], "request")
            _finish_effect(ctx, uow, action, reqp, receipt, effect_request_status(action["kind"], None), {})
        else:
            _external_success(ctx, uow, action, receipt)
        new_action = bump(action, ctx.now, status="succeeded", receipt=receipt, error=None)
        new_run, _jid = _continuation(ctx, uow, run, new_action)
    else:
        new_action = bump(action, ctx.now, status="failed",
                          error=err_obj("DISPATCH_FAILED", "operator reconciliation confirmed no effect was created"))
        put_action(uow, new_action)
        new_run, _jid = _replan(ctx, uow, run, new_action, {"code": "DISPATCH_FAILED"})
    if not any(j["status"] == "leased" for j in active_jobs(ctx, run["id"])):
        new_run = _api_event(ctx, uow, new_run if new_run is not run else bump(run, ctx.now), new_action,
                             new_action["status"])
    if new_run is not run:
        _save_run(uow, runp, new_run)
    uow.guard_version(action_id, action["version"])
    return 200, new_action


def _readback_receipt(ctx: Ctx, action: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Rev 4 #28: the API reads the target back and issues the Receipt id/result_hash itself."""
    tid = target["target_id"]
    if action["connector"] == "local":
        rows = ctx.store.read(ctx.ws, "MATCH (a:E)-[r:TRACKED_BY {id: $id}]->(b:E) RETURN r.id, a.id, b.id", {"id": tid})
        if rows:
            readback: Any = {"edge_id": rows[0][0], "type": "TRACKED_BY", "from_id": rows[0][1], "to_id": rows[0][2]}
        else:
            p = ctx.store.get(ctx.ws, tid)
            if not p or p.get("label") not in ("Draft", "EscalationCase", "WorkItem") or not isinstance(p.get("doc"), dict):
                raise policy("local reconcile target does not exist in the graph", {"target_id": tid})
            readback = p["doc"]
    else:
        # attributed connector observation: a worker-recorded connector_status on this action naming the target
        obs = ctx.store.find(ctx.ws, "Observation", {"action_id": action["id"], "kind": "connector_status"},
                             order="observed_at", desc=True)
        match = [o for o in obs if str(o.get("actor_id", "")).startswith("worker:") and tid in o["doc"]["body"]]
        if not match:
            raise policy("no attributed connector_status observation on this action names the target; refresh first",
                         {"target_id": tid})
        readback = {"target_id": tid, "target_url": target.get("target_url"), "observation": match[0]["doc"]}
    return {"id": new_id("rcpt"), "connector": action["connector"], "target_id": tid,
            "target_url": target.get("target_url") if action["connector"] != "local" else None,
            "observed_at": ctx.now, "result_hash": sha256_hex(canonical_json(readback))}
