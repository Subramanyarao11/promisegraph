"""Authoritative domain commands. Each command receives a UnitOfWork and returns (http_status, data | ApiError).

Returning an ApiError (instead of raising) means "persist the state changes in `uow` AND answer with this
error" (e.g. a commit that discovers a stale proposal marks it stale, then answers 409 REVISION_CONFLICT).
Raising an ApiError means nothing is written.
"""

from __future__ import annotations

import json
from typing import Any

from . import context as ctxmod
from . import importer
from .core import (ACTIVE_JOB_STATUSES, TERMINAL_RUN, Ctx, active_jobs, bump, bump_revision, enqueue_job,
                   err_obj, evidence_refs, missing_evidence, new_run_doc, put, put_action, put_request, put_run,
                   put_work_item, run_checkpoint, source_revision, append_event)
from .errors import ApiError, conflict, evidence_invalid, invalid, not_found, policy
from .store import UnitOfWork
from .util import canonical_json, edge_id, new_id, payload_hash

SUFFICIENT_VERIFICATION = {"operator_confirmation", "customer_confirmation", "acceptance_check"}
OPEN_REQUEST_STATES = {"open", "investigating", "awaiting_input", "awaiting_review", "awaiting_work",
                       "awaiting_verification", "escalated"}


# ---- loaders ----------------------------------------------------------------------------------------
def load(ctx: Ctx, label: str, node_id: str, what: str) -> dict[str, Any]:
    p = ctx.store.get(ctx.ws, node_id, label)
    if not p:
        raise not_found(what, node_id)
    return p


def auto_commit_kinds(ctx: Ctx) -> list[str]:
    return ["answer", "clarify"]


def worker_job_for(ctx: Ctx, *, run_id: str | None = None, request_id: str | None = None) -> dict[str, Any] | None:
    """For worker mutations on public routes: the validated lease must cover the target run/request."""
    if ctx.role != "worker":
        return None
    job = ctx.job
    assert job is not None
    if run_id is not None and job.get("run_id") != run_id:
        raise conflict("LEASE_LOST", "the presented job lease does not cover this run", {"job_id": job["id"]})
    if request_id is not None and job.get("request_id") != request_id:
        raise conflict("LEASE_LOST", "the presented job lease does not cover this request", {"job_id": job["id"]})
    return job


def guard_worker_lease(ctx: Ctx, uow: UnitOfWork) -> None:
    if ctx.role == "worker" and ctx.lease:
        uow.guard_lease(ctx.lease["job_id"], ctx.lease["lease_token"], ctx.lease["fencing_token"], ctx.now_epoch)


# ---- runs -------------------------------------------------------------------------------------------
def start_run(ctx: Ctx, uow: UnitOfWork, req: dict[str, Any], mode: str, instruction: str = "",
              authorization_id: str | None = None, checkpoint: dict | None = None,
              revision: int | None = None) -> dict[str, Any]:
    revision = ctx.revision() if revision is None else revision
    run = new_run_doc(ctx, req["id"], mode, revision)
    x: dict[str, Any] = {"instruction": instruction, "authorization_id": authorization_id or ""}
    if mode == "model" and not (ctx.settings.ai_enabled and ctx.settings.ai_configured):
        run["status"] = "failed"
        run["error"] = err_obj("CAPABILITY_DISABLED", "Model planner is disabled or unconfigured; the run was not "
                               "silently switched to scripted mode.")
        put_run(uow, run, ts=ctx.now_epoch, **x)
    else:
        cp = dict(checkpoint or {})
        if instruction:
            cp.setdefault("instruction", instruction)
        put_run(uow, run, ts=ctx.now_epoch, **x)
        enqueue_job(ctx, uow, "investigate", run_id=run["id"], request_id=req["id"], checkpoint=cp or None)
    uow.edge("INVESTIGATES", edge_id("INVESTIGATES", run["id"], req["id"]), run["id"], req["id"])
    return run


def has_active_run(ctx: Ctx, request_id: str) -> dict[str, Any] | None:
    runs = ctx.store.find(ctx.ws, "Run", {"request_id": request_id,
                                          "status": ["queued", "running", "waiting_for_input", "awaiting_review"]})
    return runs[0] if runs else None


def intake(ctx: Ctx, uow: UnitOfWork, cmd: dict[str, Any]) -> tuple[int, Any]:
    cust = ctx.store.get(ctx.ws, cmd["customer_id"], "Customer")
    if not cust:
        raise invalid("unknown customer", {"customer_id": f"customer {cmd['customer_id']} not found in workspace"})
    caps = ctx.store.get_many(ctx.ws, cmd["capability_ids"])
    bad = {f"capability_ids/{i}": "unknown capability" for i, c in enumerate(cmd["capability_ids"])
           if c not in caps or caps[c].get("label") != "Capability"}
    if bad:
        raise invalid("unknown capability ids", bad)
    rid = new_id("req")
    src_id = f"src_{rid}"
    src = source_revision(ctx, uow, src_id, title=cmd["subject"], kind="request", origin="owned_permitted",
                          body=cmd["body"], observed_at=ctx.now, head=None, head_known=True)
    req = {"id": rid, "workspace_id": ctx.ws, "customer_id": cmd["customer_id"], "subject": cmd["subject"],
           "body": cmd["body"], "channel": cmd["channel"], "status": "open", "owner_id": None, "source_id": src_id,
           "capability_ids": list(dict.fromkeys(cmd["capability_ids"])), "linked_work_ids": [],
           "created_at": ctx.now, "updated_at": ctx.now, "version": 1}
    put_request(uow, req, external_ref=cmd.get("external_ref") or "", ts=ctx.now_epoch)
    ev = src["evidence_ids"]
    uow.edge("SUBMITTED", edge_id("SUBMITTED", cmd["customer_id"], rid), cmd["customer_id"], rid,
             evidence_ids=ev, confirmed=True, via="intake")
    uow.edge("SUPPORTED_BY", edge_id("SUPPORTED_BY", rid, src_id), rid, src_id, evidence_ids=ev, confirmed=True,
             via="intake")
    for c in req["capability_ids"]:
        # operator-provided and existence-verified -> confirmed; planner suggestions never land here
        uow.edge("ABOUT", edge_id("ABOUT", rid, c), rid, c, evidence_ids=ev, confirmed=True, via="intake")
    run = start_run(ctx, uow, req, cmd["planner_mode"]) if cmd["auto_start"] else None
    return 201, {"request": req, "run": run}


def create_run(ctx: Ctx, uow: UnitOfWork, request_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    reqp = load(ctx, "Request", request_id, "request")
    if cmd.get("authorization_id"):
        auth = ctx.store.get(ctx.ws, cmd["authorization_id"], "Authorization")
        if not auth or auth.get("request_id") != request_id:
            raise policy("authorization_id does not reference a stored authorization for this request",
                         {"authorization_id": cmd["authorization_id"]})
    active = has_active_run(ctx, request_id)
    if active:
        raise conflict("INVALID_TRANSITION", f"request already has an active run {active['id']}",
                       {"run_id": active["id"], "run_status": active["status"]})
    run = start_run(ctx, uow, reqp["doc"], cmd["mode"], cmd["instruction"], cmd.get("authorization_id"))
    return 202, run


def cancel_run(ctx: Ctx, uow: UnitOfWork, run_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    runp = load(ctx, "Run", run_id, "run")
    run = runp["doc"]
    if run["status"] in TERMINAL_RUN:
        raise conflict("INVALID_TRANSITION", f"run is {run['status']}", {"run_status": run["status"]})
    for job in active_jobs(ctx, run_id):
        # queued/blocked work stops; an in-flight effect (leased dispatch/reconcile) may still record its receipt
        if job["status"] in ("queued", "blocked") or job["kind"] == "investigate":
            uow.node("Job", job["id"], status="cancelled")
        if job["kind"] == "dispatch" and job["status"] in ("queued", "blocked") and job.get("action_id"):
            ap = ctx.store.get(ctx.ws, job["action_id"], "Action")
            if ap and ap["doc"]["status"] == "accepted":
                put_action(uow, bump(ap["doc"], ctx.now, status="cancelled"))
    run = bump(run, ctx.now, status="cancelled",
               error=err_obj("CANCELLED", f"Cancelled by operator: {cmd['reason']}"))
    _ev, run = append_event(ctx, uow, run, "run_finished",
                            {"run_status": "cancelled", "request_status": _req_status(ctx, run["request_id"]),
                             "action_id": None})
    uow.guard_version(run_id, runp["doc"]["version"])
    put_run(uow, run)
    return 200, run


def _req_status(ctx: Ctx, request_id: str) -> str:
    p = ctx.store.get(ctx.ws, request_id, "Request")
    return p["doc"]["status"] if p else "open"


def resume_run(ctx: Ctx, uow: UnitOfWork, run_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    if ctx.role != "operator":
        raise ApiError(403, "UNAUTHORIZED", "only an authorized operator can resume a run")
    runp = load(ctx, "Run", run_id, "run")
    run = runp["doc"]
    if cmd["expected_run_version"] != run["version"]:
        raise conflict("REVISION_CONFLICT", "run version changed", {"current_version": run["version"]})
    if run["status"] != "failed" or (run.get("error") or {}).get("code") == "CAPABILITY_DISABLED":
        raise conflict("INVALID_TRANSITION", f"run is {run['status']} and not a recoverable failed run",
                       {"run_status": run["status"]})
    budget = dict(run["budget"])
    if budget["extensions_used"] >= 1:
        raise policy("default policy permits one budget extension per run",
                     {"extensions_used": budget["extensions_used"]})
    from .core import LIFETIME_MAX_ACTIVE_SECONDS, LIFETIME_MAX_TOOL_CALLS
    budget["max_tool_calls"] = min(LIFETIME_MAX_TOOL_CALLS, budget["max_tool_calls"] + cmd["additional_tool_calls"])
    budget["max_active_seconds"] = min(LIFETIME_MAX_ACTIVE_SECONDS,
                                       budget["max_active_seconds"] + cmd["additional_active_seconds"])
    budget["extensions_used"] += 1
    cp = run_checkpoint(runp) or {}
    cp = {**cp, "resume_stage": cp.get("stage") if cp.get("stage") in ("investigate", "decide", "prepare", "act",
          "observe", "verify", "remember") else "investigate", "resume_reason": cmd["reason"][:500]}
    run = bump(run, ctx.now, status="queued", budget=budget, error=None)
    uow.guard_version(run_id, runp["doc"]["version"])
    put_run(uow, run, checkpoint=cp)
    enqueue_job(ctx, uow, "investigate", run_id=run_id, request_id=run["request_id"], checkpoint=cp)
    return 202, run


# ---- replies ----------------------------------------------------------------------------------------
def reply(ctx: Ctx, uow: UnitOfWork, request_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    reqp = load(ctx, "Request", request_id, "request")
    req = reqp["doc"]
    if cmd["expected_request_version"] != req["version"]:
        raise conflict("REVISION_CONFLICT", "request version changed", {"current_version": req["version"]})
    if req["status"] == "closed":
        raise conflict("INVALID_TRANSITION", "request is closed; reopen explicitly", {"request_status": "closed"})
    src_id = new_id("src_reply")
    src = source_revision(ctx, uow, src_id, title=f"Reply to {req['subject']}"[:300], kind="request",
                          origin="owned_permitted", body=cmd["body"], observed_at=ctx.now, head=None, head_known=True)
    uow.edge("HAS_REPLY", edge_id("HAS_REPLY", request_id, src_id), request_id, src_id)
    uow.edge("SUPPORTED_BY", edge_id("SUPPORTED_BY", request_id, src_id), request_id, src_id,
             evidence_ids=src["evidence_ids"], confirmed=True, via="reply")
    new_req = bump(req, ctx.now)
    uow.guard_version(request_id, req["version"])
    put_request(uow, new_req, external_ref=reqp.get("external_ref") or "")
    resume_cp = {"resume_stage": "investigate", "reply_source_id": src_id}
    waiting = ctx.store.find(ctx.ws, "Run", {"request_id": request_id, "status": "waiting_for_input"},
                             order="ts", desc=True, limit=1)
    if waiting:
        runp = waiting[0]
        run = runp["doc"]
        cp = {**(run_checkpoint(runp) or {}), **resume_cp}  # rev2 #18
        run = bump(run, ctx.now, status="queued", stage="investigate")
        uow.guard_version(run["id"], runp["doc"]["version"])
        put_run(uow, run, checkpoint=cp)
        enqueue_job(ctx, uow, "investigate", run_id=run["id"], request_id=request_id, checkpoint=cp)
    else:
        active = has_active_run(ctx, request_id)
        if active:
            run = active["doc"]  # the active run sees the reply through context; no second execution job
        else:
            last = ctx.store.find(ctx.ws, "Run", {"request_id": request_id}, order="ts", desc=True, limit=1)
            mode = last[0]["doc"]["mode"] if last else "scripted"
            run = start_run(ctx, uow, new_req, mode, checkpoint=resume_cp)
    return 201, {"source_id": src_id, "source_revision": src["revision"], "run": run}


# ---- proposals --------------------------------------------------------------------------------------
def _payload_text(kind: str, payload: dict[str, Any]) -> str:
    return canonical_json(payload).lower()


def validate_proposal(ctx: Ctx, p: dict[str, Any], req: dict[str, Any], revision: int
                      ) -> tuple[str, list[str], dict[str, int]]:
    """Deterministic authoritative validation. Returns (validity, reasons, consulted {node_id: version})."""
    reasons_invalid: list[str] = []
    reasons_stale: list[str] = []
    consulted: dict[str, int] = {}
    kind, payload = p["kind"], p["payload"]
    if req["status"] in ("resolved", "closed"):
        reasons_invalid.append(f"INVALID_TRANSITION: request is {req['status']}")
    alt_ev = [e for a in p.get("rejected_alternatives") or [] for e in a["evidence_ids"]]
    missing = missing_evidence(ctx, list(p["evidence_ids"]) + alt_ev)
    if missing:
        reasons_invalid.append("EVIDENCE_INVALID: unknown evidence ids " + ", ".join(sorted(set(missing))[:10]))
    if kind in ("answer", "link_work", "create_work") and not p["evidence_ids"]:
        reasons_invalid.append(f"EVIDENCE_INVALID: a {kind} proposal must cite at least one evidence id")
    for ev in evidence_refs(ctx, p["evidence_ids"]):
        src = ctx.store.get(ctx.ws, ev["source_id"], "Source")
        if src:
            consulted[ev["source_id"]] = int(src["latest_revision"])
            if ev["source_revision"] < int(src["latest_revision"]):
                reasons_stale.append(f"REVISION_CONFLICT: evidence {ev['id']} cites superseded revision "
                                     f"{ev['source_revision']} of {ev['source_id']}")
    work_scope: list[str] = []
    ref_ids: list[str] = []
    if kind == "link_work":
        ref_ids.append(payload["work_item_id"])
    elif kind == "create_work":
        ref_ids += [x for x in [payload.get("owner_id"), payload.get("capability_id")] if x]
        ref_ids += payload.get("dependency_ids") or []
    elif kind == "escalate":
        ref_ids += [x for x in [payload.get("owner_id"), payload.get("team_id")] if x]
    refs = ctx.store.get_many(ctx.ws, ref_ids)
    if kind == "link_work":
        w = refs.get(payload["work_item_id"])
        if not w or w.get("label") != "WorkItem":
            reasons_invalid.append(f"INVALID_INPUT: unknown work item {payload['work_item_id']}")
        else:
            consulted[w["id"]] = int(w["version"])
            work_scope.append(w["id"])
            if w["id"] in (req.get("linked_work_ids") or []):
                reasons_invalid.append(f"INVALID_TRANSITION: work item {w['id']} is already linked to this request")
            if w["doc"]["status"] == "cancelled":
                reasons_invalid.append(f"POLICY_VIOLATION: work item {w['id']} is cancelled; reinvestigate or "
                                       "escalate instead of waiting on it")
            if w["doc"].get("owner_id"):
                o = ctx.store.get(ctx.ws, w["doc"]["owner_id"])
                if o:
                    consulted[o["id"]] = int(o.get("version") or 1)
    if kind == "create_work":
        if payload.get("owner_id"):
            o = refs.get(payload["owner_id"])
            if not o or o.get("label") not in ("Person", "Team"):
                reasons_invalid.append(f"INVALID_INPUT: unknown owner {payload['owner_id']}")
            else:
                consulted[o["id"]] = int(o.get("version") or 1)
                if o.get("label") == "Person" and o.get("active") is False:
                    reasons_invalid.append(f"POLICY_VIOLATION: owner {o['id']} is not an active member")
        if payload.get("capability_id"):
            c = refs.get(payload["capability_id"])
            if not c or c.get("label") != "Capability":
                reasons_invalid.append(f"INVALID_INPUT: unknown capability {payload['capability_id']}")
            else:
                consulted[c["id"]] = int(c.get("version") or 1)
        for d in payload.get("dependency_ids") or []:
            w = refs.get(d)
            if not w or w.get("label") != "WorkItem":
                reasons_invalid.append(f"INVALID_INPUT: unknown dependency work item {d}")
            else:
                consulted[d] = int(w["version"])
                work_scope.append(d)
        if not payload["acceptance_criteria"]:
            reasons_invalid.append("INVALID_INPUT: create_work requires acceptance criteria")
        if payload["connector"] == "linear" and not (ctx.settings.linear_enabled and ctx.settings.linear_configured):
            reasons_invalid.append("CAPABILITY_DISABLED: linear connector is not configured")
    if kind == "escalate":
        for f, labels in (("owner_id", ("Person",)), ("team_id", ("Team",))):
            if payload.get(f):
                o = refs.get(payload[f])
                if not o or o.get("label") not in labels:
                    reasons_invalid.append(f"INVALID_INPUT: unknown {f} {payload[f]}")
                else:
                    consulted[o["id"]] = int(o.get("version") or 1)
    # scoped decisions
    scopes = ctxmod.request_scopes(req, work_scope)
    if kind == "create_work" and payload.get("capability_id"):
        scopes.append(("capability", payload["capability_id"]))
    decisions = ctxmod.decisions_for_scopes(ctx, list(dict.fromkeys(scopes)))
    for d in decisions:
        consulted[d["id"]] = int(d["version"])
    groups = ctxmod.conflicting(decisions)
    if groups and kind not in ("clarify", "escalate"):
        for g in groups:
            reasons_invalid.append("POLICY_VIOLATION: conflicting active decisions " + ", ".join(x["id"] for x in g)
                                   + f" ({g[0]['rule_type']}); clarify or escalate")
    text = _payload_text(kind, payload)
    for d in decisions:
        rt, v = d["rule_type"], d["value"]
        if rt == "allowed_action_types" and isinstance(v, list) and kind not in v:
            reasons_invalid.append(f"POLICY_VIOLATION: decision {d['id']} allows only {', '.join(v)}")
        elif rt == "forbidden_workaround" and isinstance(v, str) and v.strip():
            needles = {v.lower(), v.lower().replace("_", " ")}
            if any(n in text for n in needles):
                reasons_invalid.append(f"POLICY_VIOLATION: decision {d['id']} forbids workaround '{v}'")
        elif rt == "routing_owner" and isinstance(v, str) and kind in ("create_work", "escalate"):
            chosen = {payload.get("owner_id"), payload.get("team_id")} - {None}
            if v not in chosen:
                reasons_invalid.append(f"POLICY_VIOLATION: decision {d['id']} routes this scope to {v}")
    if p["based_on_revision"] != revision:
        reasons_stale.append(f"REVISION_CONFLICT: proposal based on knowledge revision {p['based_on_revision']}, "
                             f"current is {revision}")
    if reasons_invalid:
        return "invalid", [r[:1000] for r in reasons_invalid + reasons_stale][:30], consulted
    if reasons_stale:
        return "stale", [r[:1000] for r in reasons_stale][:30], consulted
    return "valid", [], consulted


def create_proposal(ctx: Ctx, uow: UnitOfWork, cmd: dict[str, Any]) -> tuple[int, Any]:
    reqp = load(ctx, "Request", cmd["request_id"], "request")
    runp = load(ctx, "Run", cmd["run_id"], "run")
    run = runp["doc"]
    if run["request_id"] != cmd["request_id"]:
        raise invalid("run does not belong to request", {"run_id": "run/request mismatch"})
    worker_job_for(ctx, run_id=run["id"])
    if run["status"] in TERMINAL_RUN:
        raise conflict("INVALID_TRANSITION", f"run is {run['status']}", {"run_status": run["status"]})
    revision = ctx.revision()
    validity, reasons, consulted = validate_proposal(ctx, cmd, reqp["doc"], revision)
    prop = {"id": new_id("prop"), "workspace_id": ctx.ws, "request_id": cmd["request_id"], "run_id": cmd["run_id"],
            "kind": cmd["kind"], "payload": cmd["payload"], "evidence_ids": cmd["evidence_ids"],
            "explanation": cmd["explanation"], "rejected_alternatives": cmd["rejected_alternatives"],
            "based_on_revision": cmd["based_on_revision"], "validity": validity, "validation_reasons": reasons,
            "authorization_id": None, "created_at": ctx.now, "version": 1}
    put(uow, "Proposal", prop, validity=validity, request_id=prop["request_id"], run_id=prop["run_id"],
        created_at=prop["created_at"], ts=ctx.now_epoch,
        consulted=json.dumps(consulted), committed_action_id="", title=f"{cmd['kind']} proposal")
    for nid in consulted:
        uow.edge("CONSULTED", edge_id("CONSULTED", prop["id"], nid), prop["id"], nid)
    guard_worker_lease(ctx, uow)
    return 201, prop


def _mark_proposal(uow: UnitOfWork, prop: dict[str, Any], validity: str, reasons: list[str]) -> dict[str, Any]:
    doc = dict(prop)
    doc["validity"] = validity
    doc["validation_reasons"] = list(dict.fromkeys((prop.get("validation_reasons") or []) + reasons))[:30]
    doc["version"] = int(prop["version"]) + 1
    put(uow, "Proposal", doc, validity=validity, request_id=doc["request_id"], run_id=doc["run_id"])
    return doc


def commit_proposal(ctx: Ctx, uow: UnitOfWork, proposal_id: str, cmd: dict[str, Any], idem_key: str
                    ) -> tuple[int, Any]:
    pp = load(ctx, "Proposal", proposal_id, "proposal")
    prop = pp["doc"]
    if pp.get("committed_action_id"):
        raise conflict("INVALID_TRANSITION", f"proposal already committed as {pp['committed_action_id']}",
                       {"action_id": pp["committed_action_id"]})
    runp = load(ctx, "Run", prop["run_id"], "run")
    reqp = load(ctx, "Request", prop["request_id"], "request")
    run = runp["doc"]
    worker_job_for(ctx, run_id=run["id"])
    if run["status"] in TERMINAL_RUN:
        raise conflict("INVALID_TRANSITION", f"run is {run['status']}", {"run_status": run["status"]})
    if prop["validity"] == "invalid":
        if any(r.startswith("EVIDENCE_INVALID") for r in prop["validation_reasons"]):
            raise evidence_invalid("proposal cites invalid evidence", {"validation_reasons": prop["validation_reasons"][:10]})
        raise policy("proposal is invalid", {"validation_reasons": prop["validation_reasons"][:10]})
    revision = ctx.revision()
    if prop["validity"] == "stale":
        return 409, conflict("REVISION_CONFLICT", "proposal is stale; re-investigate",
                             {"validation_reasons": prop["validation_reasons"][:10], "knowledge_revision": revision})
    if cmd["expected_revision"] != revision or prop["based_on_revision"] != revision:
        _mark_proposal(uow, prop, "stale", [f"REVISION_CONFLICT: knowledge revision is {revision} at commit "
                                            f"(proposal basis {prop['based_on_revision']}, expected "
                                            f"{cmd['expected_revision']})"])
        return 409, conflict("REVISION_CONFLICT", "knowledge revision changed; re-investigate before committing",
                             {"knowledge_revision": revision, "based_on_revision": prop["based_on_revision"],
                              "expected_revision": cmd["expected_revision"]})
    validity, reasons, _consulted = validate_proposal(ctx, prop, reqp["doc"], revision)
    if validity != "valid":
        _mark_proposal(uow, prop, validity, reasons)
        if validity == "stale":
            return 409, conflict("REVISION_CONFLICT", "proposal is stale", {"validation_reasons": reasons[:10]})
        return 422, policy("proposal no longer passes validation", {"validation_reasons": reasons[:10]})
    auth_id = cmd.get("authorization_id")
    auth = None
    if auth_id:
        auth = ctx.store.get(ctx.ws, auth_id, "Authorization")
        if not auth or auth.get("request_id") != prop["request_id"]:
            raise policy("authorization_id does not reference a stored authorization for this request",
                         {"authorization_id": auth_id})
    policy_basis = "operator"
    if ctx.role == "worker":
        run_auth = runp.get("authorization_id") or ""
        if prop["kind"] in auto_commit_kinds(ctx):
            policy_basis = "workspace_auto_commit_policy"
        elif auth and (auth_id == run_auth or auth.get("proposal_id") == proposal_id):
            policy_basis = "stored_authorization"
        else:
            raise policy(f"{prop['kind']} requires an operator authorization; complete the job as waiting for review",
                         {"kind": prop["kind"], "auto_commit_kinds": auto_commit_kinds(ctx)})
    elif not auth_id:
        auth_id = new_id("auth")
        uow.node("Authorization", auth_id, proposal_id=proposal_id, request_id=prop["request_id"],
                 actor_id=ctx.actor_id, payload_hash=payload_hash(prop["payload"]), created_at=ctx.now,
                 title="Operator authorization")
    jobs = active_jobs(ctx, run["id"])
    if any(j["kind"] in ("dispatch", "reconcile") for j in jobs):
        raise conflict("INVALID_TRANSITION", "run already has an active execution job",
                       {"job_ids": [j["id"] for j in jobs]})
    connector = prop["payload"]["connector"] if prop["kind"] == "create_work" else "local"
    action = {"id": new_id("act"), "workspace_id": ctx.ws, "request_id": prop["request_id"], "run_id": run["id"],
              "proposal_id": proposal_id, "kind": prop["kind"], "status": "accepted", "verification": "pending",
              "idempotency_key": idem_key, "connector": connector, "receipt": None, "error": None,
              "created_at": ctx.now, "updated_at": ctx.now, "version": 1}
    put_action(uow, action, ts=ctx.now_epoch, basis_revision=revision, payload_hash=payload_hash(prop["payload"]),
               authorization_id=auth_id or "", policy_basis=policy_basis)
    newp = dict(prop)
    newp["authorization_id"] = auth_id
    newp["version"] = int(prop["version"]) + 1
    put(uow, "Proposal", newp, committed_action_id=action["id"])
    uow.guard_version(proposal_id, prop["version"])
    uow.edge("EXECUTES", edge_id("EXECUTES", action["id"], proposal_id), action["id"], proposal_id)
    busy = [j for j in jobs if j["kind"] == "investigate"]
    new_run = dict(run)
    if busy:
        # at most one active execution job per run: dispatch waits for the investigate job to complete
        job_id = enqueue_job(ctx, uow, "dispatch", run_id=run["id"], request_id=prop["request_id"],
                             action_id=action["id"], status="blocked", blocked_by=busy[0]["id"])
    else:
        job_id = enqueue_job(ctx, uow, "dispatch", run_id=run["id"], request_id=prop["request_id"],
                             action_id=action["id"])
        new_run = bump(run, ctx.now, status="queued", stage="act")
        _e, new_run = append_event(ctx, uow, new_run, "action_updated",
                                   {"action_id": action["id"], "status": "accepted", "connector": connector})
    if new_run != run:
        uow.guard_version(run["id"], run["version"])
        put_run(uow, new_run)
    guard_worker_lease(ctx, uow)
    return 202, {"action": action, "job_id": job_id}


# ---- observations / verification ---------------------------------------------------------------------
def record_observation(ctx: Ctx, uow: UnitOfWork, request_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    reqp = load(ctx, "Request", request_id, "request")
    req = reqp["doc"]
    worker_job_for(ctx, request_id=request_id)
    if cmd["expected_request_version"] != req["version"]:
        raise conflict("REVISION_CONFLICT", "request version changed", {"current_version": req["version"]})
    if ctx.role == "worker" and cmd["kind"] != "connector_status":
        raise policy("a worker may only record connector_status observations; verification must be attributable "
                     "to an operator, the customer or a declared acceptance check", {"kind": cmd["kind"]})
    missing = missing_evidence(ctx, cmd["evidence_ids"])
    if missing:
        raise evidence_invalid("unknown evidence ids", {"missing": missing[:20]})
    action = None
    if cmd.get("action_id"):
        ap = load(ctx, "Action", cmd["action_id"], "action")
        action = ap["doc"]
        if action["request_id"] != request_id:
            raise invalid("action does not belong to request", {"action_id": "request mismatch"})
    obs_id = new_id("obs")
    src = source_revision(ctx, uow, f"src_{obs_id}", title=f"Observation {cmd['kind']} on {request_id}"[:300],
                          kind="observation", origin="owned_permitted", body=cmd["body"], observed_at=ctx.now,
                          head=None, head_known=True)
    obs = {"id": obs_id, "workspace_id": ctx.ws, "request_id": request_id, "action_id": cmd.get("action_id"),
           "kind": cmd["kind"], "verification": cmd["verification"], "body": cmd["body"],
           "evidence_ids": cmd["evidence_ids"], "actor_id": ctx.actor_id, "observed_at": ctx.now}
    uow.node("Observation", obs_id, doc=obs, request_id=request_id, title=f"{cmd['kind']} ({cmd['verification']})",
             source_id=src["source_id"])
    uow.edge("SUPPORTED_BY", edge_id("SUPPORTED_BY", obs_id, src["source_id"]), obs_id, src["source_id"],
             evidence_ids=src["evidence_ids"], confirmed=True, via="observation")
    if action:
        uow.edge("OBSERVED_AS", edge_id("OBSERVED_AS", action["id"], obs_id), action["id"], obs_id,
                 evidence_ids=src["evidence_ids"], confirmed=True)
    new_req = dict(req)
    sufficient = cmd["kind"] in SUFFICIENT_VERIFICATION
    revision = None
    if sufficient and cmd["verification"] in ("passed", "failed"):
        uow.edge("VERIFIES", edge_id("VERIFIES", obs_id, request_id), obs_id, request_id,
                 evidence_ids=src["evidence_ids"], confirmed=True)
        passed = cmd["verification"] == "passed"
        attempt_id = new_id("att")
        scope = {"entity_type": "customer", "entity_id": req["customer_id"]}
        att = {"id": attempt_id, "workspace_id": ctx.ws, "request_id": request_id,
               "procedure_key": (action["kind"] if action else "resolution")[:200],
               "context_signature": (",".join(req.get("capability_ids") or []) + f"|{req['customer_id']}")[:500]
               or req["customer_id"],
               "scope": scope, "outcome": "succeeded" if passed else "failed",
               "verification": cmd["verification"], "summary": cmd["body"][:4000],
               "evidence_ids": list(dict.fromkeys(src["evidence_ids"] + cmd["evidence_ids"]))[:100],
               "observed_at": ctx.now}
        uow.node("Attempt", attempt_id, doc=att, request_id=request_id, scope_type=scope["entity_type"],
                 scope_id=scope["entity_id"], title=f"Attempt {att['procedure_key']} ({att['outcome']})", version=1)
        uow.edge("ATTEMPTED_FOR", edge_id("ATTEMPTED_FOR", attempt_id, request_id), attempt_id, request_id,
                 evidence_ids=src["evidence_ids"], confirmed=True)
        uow.edge("OBSERVED_AS", edge_id("OBSERVED_AS", attempt_id, obs_id), attempt_id, obs_id,
                 evidence_ids=src["evidence_ids"], confirmed=True)
        revision = bump_revision(uow, ctx.revision())  # verified/failed outcomes are memory that changes plans
        if action:
            put_action(uow, bump(action, ctx.now, verification=cmd["verification"]))
        if passed:
            if req["status"] != "closed":
                new_req = bump(req, ctx.now, status="resolved")
        else:
            new_req = bump(req, ctx.now, status="investigating" if req["status"] != "closed" else "closed")
            if req["status"] != "closed" and not has_active_run(ctx, request_id):
                last = ctx.store.find(ctx.ws, "Run", {"request_id": request_id}, order="ts", desc=True,
                                      limit=1)
                mode = last[0]["doc"]["mode"] if last else "scripted"
                start_run(ctx, uow, new_req, mode, checkpoint={
                    "resume_stage": "investigate", "action_id": cmd.get("action_id"), "previous_job_id": None,
                    "failed_verification_observation_id": obs_id, "failed_attempt_id": attempt_id},
                    revision=revision)
    elif action and cmd["kind"] == "connector_status":
        pass  # external/connector status is never customer verification (T17)
    if new_req is not req:
        uow.guard_version(request_id, req["version"])
        put_request(uow, new_req, external_ref=reqp.get("external_ref") or "")
    # API-appended trace event when no worker currently holds the run
    run = _latest_run(ctx, request_id)
    if run and ctx.role == "operator" and not any(j["status"] == "leased" for j in active_jobs(ctx, run["doc"]["id"])):
        _e, nr = append_event(ctx, uow, run["doc"], "observation_recorded",
                              {"observation_id": obs_id, "kind": cmd["kind"], "verification": cmd["verification"]})
        uow.guard_version(nr["id"], run["doc"]["version"])
        put_run(uow, bump(nr, ctx.now))
    guard_worker_lease(ctx, uow)
    return 201, {"observation": obs, "request": new_req}


def _latest_run(ctx: Ctx, request_id: str) -> dict[str, Any] | None:
    rows = ctx.store.find(ctx.ws, "Run", {"request_id": request_id}, order="ts", desc=True, limit=1)
    return rows[0] if rows else None


# ---- refresh ----------------------------------------------------------------------------------------
def refresh(ctx: Ctx, uow: UnitOfWork, request_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    reqp = load(ctx, "Request", request_id, "request")
    existing = ctx.store.find(ctx.ws, "Job", {"kind": "refresh", "target": request_id,
                                              "status": ["queued", "leased"]}, limit=1)
    if existing:
        return 202, {"job_id": existing[0]["id"], "coalesced": True}
    cp = {"request_id": request_id, "reason": cmd["reason"][:500],
          "work_item_ids": reqp["doc"].get("linked_work_ids") or [], "completed_call_ids": [],
          "pending_action_id": None, "stage": "observe", "usage": {"tool_calls": 0, "replans": 0, "active_ms": 0}}
    job_id = enqueue_job(ctx, uow, "refresh", run_id=None, request_id=request_id, checkpoint=cp, target=request_id)
    return 202, {"job_id": job_id, "coalesced": False}


# ---- work items -------------------------------------------------------------------------------------
def patch_work_item(ctx: Ctx, uow: UnitOfWork, work_item_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    wp = load(ctx, "WorkItem", work_item_id, "work item")
    w = wp["doc"]
    if w["connector"] != "local":
        raise policy("external work is provider-owned; refresh/readback supplies attributed observations",
                     {"connector": w["connector"]})
    if cmd["expected_version"] != w["version"]:
        raise conflict("REVISION_CONFLICT", "work item version changed", {"current_version": w["version"]})
    if cmd.get("owner_id"):
        o = ctx.store.get(ctx.ws, cmd["owner_id"])
        if not o or o.get("label") not in ("Person", "Team"):
            raise invalid("unknown owner", {"owner_id": f"{cmd['owner_id']} is not a person or team"})
        if o.get("label") == "Person" and o.get("active") is False:
            raise invalid("owner is inactive", {"owner_id": f"{cmd['owner_id']} is not active"})
    src_id = wp.get("source_id") or f"src_work_item_{work_item_id}"
    record = {"id": work_item_id, "title": w["title"], "kind": w["kind"], "status": cmd["status"],
              "owner_id": cmd.get("owner_id"), "forecast_at": cmd.get("forecast_at"), "observed_at": ctx.now,
              "connector": w["connector"], "update_reason": cmd["reason"], "updated_by": ctx.actor_id}
    body = canonical_json(record)
    src = source_revision(ctx, uow, src_id, title=f"Work item {work_item_id} update", kind="work",
                          origin="owned_permitted", body=body, observed_at=ctx.now,
                          excerpts=[(body[:4000], "$")])
    new_w = bump(w, ctx.now, status=cmd["status"], owner_id=cmd.get("owner_id"), forecast_at=cmd.get("forecast_at"),
                 observed_at=ctx.now, evidence_ids=(list(w["evidence_ids"]) + src["evidence_ids"])[-100:])
    uow.guard_version(work_item_id, w["version"])
    put_work_item(uow, new_w, description=wp.get("description") or "", source_id=src_id)
    if w.get("owner_id") != cmd.get("owner_id"):
        uow.delete_edges(work_item_id, "OWNED_BY", "out")
        if cmd.get("owner_id"):
            uow.edge("OWNED_BY", edge_id("OWNED_BY", work_item_id, cmd["owner_id"]), work_item_id, cmd["owner_id"],
                     evidence_ids=src["evidence_ids"], confirmed=True, via="work_item_update")
    uow.edge("SUPPORTED_BY", edge_id("SUPPORTED_BY", work_item_id, src_id), work_item_id, src_id,
             evidence_ids=src["evidence_ids"], confirmed=True, via="work_item_update")
    revision = bump_revision(uow, ctx.revision())
    importer.invalidate_consulting(ctx, uow, [work_item_id, src_id, *( [w["owner_id"]] if w.get("owner_id") else [])],
                                   f"work item {work_item_id} changed")
    if w["status"] != cmd["status"]:
        _work_status_followups(ctx, uow, work_item_id, cmd["status"], revision)
    return 200, {"work_item": new_w, "knowledge_revision": revision}


def _work_status_followups(ctx: Ctx, uow: UnitOfWork, work_item_id: str, status: str, revision: int) -> None:
    """Completed tracked work moves requests toward verification (never resolves); cancelled work reopens."""
    for rp in ctx.store.in_neighbors(ctx.ws, work_item_id, "TRACKED_BY"):
        if rp.get("label") != "Request":
            continue
        req = rp["doc"]
        if status == "done" and req["status"] == "awaiting_work":
            put_request(uow, bump(req, ctx.now, status="awaiting_verification"), external_ref=rp.get("external_ref") or "")
        elif status == "cancelled" and req["status"] in ("awaiting_work", "awaiting_verification"):
            nr = bump(req, ctx.now, status="investigating")
            put_request(uow, nr, external_ref=rp.get("external_ref") or "")
            if not has_active_run(ctx, req["id"]):
                last = _latest_run(ctx, req["id"])
                start_run(ctx, uow, nr, last["doc"]["mode"] if last else "scripted", checkpoint={
                    "resume_stage": "investigate", "action_id": None, "previous_job_id": None,
                    "cancelled_work_item_id": work_item_id}, revision=revision)


# ---- decisions --------------------------------------------------------------------------------------
def decision_command(ctx: Ctx, uow: UnitOfWork, cmd: dict[str, Any]) -> tuple[int, Any]:
    if ctx.role != "operator":
        raise ApiError(403, "UNAUTHORIZED", "decisions are recorded by an authorized operator")
    revision = ctx.revision()
    if cmd["expected_revision"] != revision:
        raise conflict("REVISION_CONFLICT", "knowledge revision changed", {"knowledge_revision": revision})
    verr = importer._decision_value_error(cmd)
    if verr:
        raise invalid(verr, {"value": verr})
    sc = cmd["scope"]
    target = ctx.store.get(ctx.ws, sc["entity_id"], importer.SCOPE_LABEL[sc["entity_type"]])
    if not target:
        raise invalid("unknown scope entity", {"scope/entity_id": f"{sc['entity_type']} {sc['entity_id']} not found"})
    if cmd["rule_type"] == "routing_owner":
        o = ctx.store.get(ctx.ws, cmd["value"])
        if not o or o.get("label") not in ("Person", "Team"):
            raise invalid("routing_owner value must be a person or team id", {"value": "unknown owner"})
    missing = missing_evidence(ctx, cmd["evidence_ids"])
    if missing:
        raise evidence_invalid("unknown evidence ids", {"missing": missing[:20]})
    op = cmd["operation"]
    prior = None
    if op in ("supersede", "revoke"):
        if not cmd.get("decision_id"):
            raise invalid(f"{op} requires decision_id", {"decision_id": "required"})
        prior = load(ctx, "Decision", cmd["decision_id"], "decision")
        pd = ctxmod.effective_decision(prior["doc"], ctx.ref_now)
        if pd["status"] != "active":
            raise conflict("INVALID_TRANSITION", f"decision is {pd['status']}", {"decision_status": pd["status"]})
    changed: list[str] = []
    if op == "revoke":
        pd = prior["doc"]
        if pd["rule_type"] != cmd["rule_type"] or pd["scope"] != sc:
            raise invalid("revoke must name the target decision's rule_type and scope",
                          {"rule_type": "mismatch with target decision"})
        result = bump(pd, ctx.now, status="revoked")
        uow.guard_version(pd["id"], pd["version"])
        put(uow, "Decision", result, status="revoked")
        changed.append(pd["id"])
        status_code = 200
    else:
        did = new_id("dec")
        result = {"id": did, "workspace_id": ctx.ws, "rule_type": cmd["rule_type"], "value": cmd["value"],
                  "scope": sc, "status": "active", "rationale": cmd["rationale"], "author_id": ctx.actor_id,
                  "effective_at": cmd["effective_at"], "expires_at": cmd.get("expires_at"),
                  "supersedes_id": prior["id"] if prior else None, "evidence_ids": cmd["evidence_ids"], "version": 1}
        put(uow, "Decision", result, title=f"{cmd['rule_type']} decision", status="active",
            rule_type=cmd["rule_type"], scope_type=sc["entity_type"], scope_id=sc["entity_id"], created_at=ctx.now)
        uow.edge("APPLIES_TO", edge_id("APPLIES_TO", did, sc["entity_id"]), did, sc["entity_id"],
                 evidence_ids=cmd["evidence_ids"], confirmed=True, via="decision")
        if prior:
            pd = prior["doc"]
            uow.guard_version(pd["id"], pd["version"])
            put(uow, "Decision", bump(pd, ctx.now, status="superseded"), status="superseded")
            uow.edge("SUPERSEDES", edge_id("SUPERSEDES", did, pd["id"]), did, pd["id"],
                     evidence_ids=cmd["evidence_ids"], confirmed=True, via="decision")
            changed.append(pd["id"])
        status_code = 201
    bump_revision(uow, revision)
    # invalidate consulted proposals; for new constraints also those in the affected scope
    changed.append(sc["entity_id"])
    importer.invalidate_consulting(ctx, uow, changed, f"decision {op} on {sc['entity_type']} {sc['entity_id']}")
    return status_code, result


# ---- imports ----------------------------------------------------------------------------------------
def import_preview(ctx: Ctx, uow: UnitOfWork, cmd: dict[str, Any]) -> tuple[int, Any]:
    if ctx.role != "operator":
        raise ApiError(403, "UNAUTHORIZED", "imports are operator commands")
    recs, rejected = importer.parse(ctx.ws, cmd["format"], cmd["content"], cmd["source_key"])
    default_obs = recs[0].observed_at if recs and recs[0].observed_at else ctx.now
    planned, rejected = importer.plan(ctx, recs, rejected, default_obs)
    pid = new_id("imp")
    revision = ctx.revision()
    uow.node("ImportPreview", pid, status="previewed", based_on_revision=revision, format=cmd["format"],
             origin=cmd["origin"], source_key=cmd["source_key"], content=cmd["content"], default_observed=default_obs,
             created_at=ctx.now, title=f"Import {cmd['source_key']}"[:300])
    changes = [{"entity_type": r.etype if r.etype != "document" else "source", "entity_id": r.id, "change": c}
               for r, c in planned]
    return 201, {"preview_id": pid, "valid_count": len(planned), "rejected_count": len(rejected),
                 "changes": changes[:1000], "rejected": rejected[:1000], "based_on_revision": revision}


def import_commit(ctx: Ctx, uow: UnitOfWork, preview_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    if ctx.role != "operator":
        raise ApiError(403, "UNAUTHORIZED", "imports are operator commands")
    pv = load(ctx, "ImportPreview", preview_id, "import preview")
    if pv.get("status") != "previewed":
        raise conflict("INVALID_TRANSITION", f"import preview is {pv.get('status')}", {"status": pv.get("status")})
    revision = ctx.revision()
    if cmd["expected_revision"] != revision or int(pv["based_on_revision"]) != revision:
        raise conflict("REVISION_CONFLICT", "knowledge revision changed since preview; preview again",
                       {"knowledge_revision": revision, "preview_revision": int(pv["based_on_revision"])})
    recs, rejected = importer.parse(ctx.ws, pv["format"], pv["content"], pv["source_key"])
    planned, rejected = importer.plan(ctx, recs, rejected, pv.get("default_observed") or ctx.now)
    res = importer.apply(ctx, uow, planned, pv["origin"], revision)
    uow.node("ImportPreview", preview_id, status="committed", committed_at=ctx.now)
    return 200, {"imported_count": res["imported_count"], "rejected_count": len(rejected),
                 "noop_count": res["noop_count"], "source_revisions": res["source_revisions"][:1000],
                 "invalidated_proposal_count": res["invalidated_proposal_count"],
                 "knowledge_revision": res["knowledge_revision"]}


# ---- closure (PROTOCOL rev 3 #21) ---------------------------------------------------------------------
def close_request(ctx: Ctx, uow: UnitOfWork, request_id: str, cmd: dict[str, Any]) -> tuple[int, Any]:
    reqp = load(ctx, "Request", request_id, "request")
    req = reqp["doc"]
    if cmd["expected_request_version"] != req["version"]:
        raise conflict("REVISION_CONFLICT", "request version changed", {"current_version": req["version"]})
    if req["status"] == "closed":
        raise conflict("INVALID_TRANSITION", "request is already closed", {"request_status": "closed"})
    new_req = bump(req, ctx.now, status="closed")
    uow.guard_version(request_id, req["version"])
    put_request(uow, new_req, external_ref=reqp.get("external_ref") or "", close_reason=cmd["reason"][:2000],
                closed_by=ctx.actor_id)
    return 200, new_req
