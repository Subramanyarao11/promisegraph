"""Domain state transitions (PROTOCOL.md "Job and run choreography"). Mock-grade, single-writer, in-memory."""

from __future__ import annotations

import json
import os
import secrets
from datetime import timedelta

from .common import (
    ApiError, bounded_id, canonical, env_flag, iso, new_id, not_found, now, now_dt, parse_ts, plus, pub, sha256,
)
from .state import Workspace, touch

DEFAULT_BUDGET = {"max_tool_calls": 12, "max_replans": 2, "max_active_seconds": 90, "extensions_used": 0}
LIFETIME_TOOL_CALLS, LIFETIME_ACTIVE_SECONDS = 24, 180
SUFFICIENT_KINDS = {"operator_confirmation", "customer_confirmation", "acceptance_check"}
ACTIVE_RUN = {"queued", "running", "waiting_for_input", "awaiting_review"}
KIND_PRIORITY = {"reconcile": 0, "dispatch": 1, "investigate": 2, "refresh": 3}
CHECKPOINT_MAX_BYTES = 64 * 1024
API_OWNED_EVENTS = {"action_updated", "waiting_for_input", "observation_recorded", "run_finished"}


def linear_enabled() -> bool:
    return env_flag("LINEAR_ENABLED", False)


def ai_enabled() -> bool:
    return env_flag("AI_ENABLED", False)


def err(code: str, message: str, retryable: bool = False, details: dict | None = None) -> dict:
    return {"code": code, "message": message[:2000], "retryable": retryable, "details": details or {}}


# =========================================================================== events

def append_event(ws: Workspace, run: dict, etype: str, payload: dict, *, step_id: str | None = None,
                 tool_name: str | None = None) -> dict:
    seq = run["event_seq"] + 1
    ev = {"id": new_id("event"), "run_id": run["id"], "seq": seq, "type": etype, "occurred_at": now(),
          "step_id": step_id, "tool_name": tool_name, "payload": payload}
    ws.events[run["id"]].append(ev)
    run["event_seq"] = seq
    run["updated_at"] = now()
    return ev


def last_event_type(ws: Workspace, run_id: str) -> str | None:
    evs = ws.events.get(run_id) or []
    return evs[-1]["type"] if evs else None


# =========================================================================== runs & jobs

def set_run(run: dict, status: str | None = None, stage: str | None = None) -> None:
    changed = False
    if status and run["status"] != status:
        run["status"] = status
        changed = True
    if stage and run["stage"] != stage:
        run["stage"] = stage
        changed = True
    if changed:
        touch(run)


def set_request(req: dict, status: str) -> None:
    if req["status"] != status:
        req["status"] = status
        touch(req)


def create_run(ws: Workspace, req: dict, mode: str, instruction: str = "", authorization_id: str | None = None) -> dict:
    t = now()
    run = {
        "id": new_id("run"), "workspace_id": ws.id, "request_id": req["id"], "mode": mode, "status": "queued",
        "stage": "investigate", "based_on_revision": ws.revision, "event_seq": 0, "budget": dict(DEFAULT_BUDGET),
        "usage": {"tool_calls": 0, "replans": 0, "active_ms": 0}, "error": None, "created_at": t, "updated_at": t,
        "version": 1, "_checkpoint": None, "_instruction": instruction, "_authorization_id": authorization_id,
    }
    ws.runs[run["id"]] = run
    if mode == "model" and not ai_enabled():
        run["status"] = "failed"
        run["error"] = err("CAPABILITY_DISABLED", "planner_mode model requested while AI_ENABLED=false; not substituted with scripted mode")
        append_event(ws, run, "error", {"code": "CAPABILITY_DISABLED", "message": run["error"]["message"]})
        append_event(ws, run, "run_finished", {"run_status": "failed", "request_status": req["status"], "action_id": None})
        return run
    enqueue(ws, "investigate", run=run, checkpoint=None)
    return run


def enqueue(ws: Workspace, kind: str, *, run: dict | None = None, action_id: str | None = None,
            checkpoint: dict | None = None, request_id: str | None = None) -> dict:
    ws.job_seq += 1
    job = {
        "id": new_id("job"), "kind": kind, "run_id": run["id"] if run else None, "action_id": action_id,
        "request_id": request_id or (run["request_id"] if run else None), "status": "queued", "attempt": 0,
        "fencing_token": 0, "lease_token": None, "expires_dt": None, "lease_seconds": 60, "worker_id": None,
        "checkpoint": checkpoint, "due_dt": now_dt(), "seq": ws.job_seq, "completions": {},
    }
    ws.jobs[job["id"]] = job
    return job


def lease_view(job: dict) -> dict:
    return {"job_id": job["id"], "kind": job["kind"], "run_id": job["run_id"], "action_id": job["action_id"],
            "attempt": job["attempt"], "lease_token": job["lease_token"], "fencing_token": job["fencing_token"],
            "expires_at": iso(job["expires_dt"]), "checkpoint": job["checkpoint"]}


def _lease_live(job: dict) -> bool:
    return job["status"] == "leased" and job["expires_dt"] is not None and job["expires_dt"] > now_dt()


def claim(ws: Workspace, body: dict) -> dict | None:
    t = now_dt()
    busy_runs = {j["run_id"] for j in ws.jobs.values() if j["run_id"] and _lease_live(j)}
    cands = []
    for j in ws.jobs.values():
        if j["kind"] not in body["kinds"] or j["due_dt"] > t:
            continue
        if j["status"] == "queued" or (j["status"] == "leased" and not _lease_live(j)):
            if j["run_id"] and j["run_id"] in busy_runs:
                continue
            cands.append(j)
    if not cands:
        return None
    job = min(cands, key=lambda j: (j["due_dt"], KIND_PRIORITY[j["kind"]], j["seq"]))
    ws.fencing += 1
    job.update(status="leased", attempt=job["attempt"] + 1, fencing_token=ws.fencing,
               lease_token="lease_" + secrets.token_urlsafe(24), lease_seconds=body["lease_seconds"],
               expires_dt=plus(body["lease_seconds"]), worker_id=body["worker_id"])
    if job["run_id"]:
        run = ws.runs[job["run_id"]]
        if run["status"] == "queued":
            set_run(run, "running")
        if job["kind"] == "investigate":
            run["based_on_revision"] = ws.revision
            req = ws.requests[run["request_id"]]
            if run["stage"] in ("investigate", "decide", "prepare") and req["status"] not in ("resolved", "closed"):
                set_request(req, "investigating")
    return lease_view(job)


def run_leased(ws: Workspace, run_id: str) -> bool:
    return any(j["run_id"] == run_id and _lease_live(j) for j in ws.jobs.values())


def cancel_run(ws: Workspace, run: dict, reason: str, extra: dict | None = None) -> None:
    """Cooperative cancellation: queued investigate/undispatched dispatch jobs are cancelled; leases may still record."""
    for j in ws.jobs.values():
        if j["run_id"] == run["id"] and j["status"] == "queued" and j["kind"] in ("investigate", "dispatch"):
            if j["kind"] == "dispatch":
                a = ws.actions[j["action_id"]]
                if a["status"] != "accepted":
                    continue
                a["status"] = "cancelled"
                touch(a)
            j["status"] = "cancelled"
    set_run(run, "cancelled")
    run["_cancel_reason"] = reason
    if not run_leased(ws, run["id"]):  # rev2 #6; otherwise appended when the leased job completes
        req = ws.requests[run["request_id"]]
        append_event(ws, run, "run_finished", {"run_status": "cancelled", "request_status": req["status"], "action_id": None,
                                               **(extra or {})})


def supersede_run(ws: Workspace, old: dict, new_run_id: str) -> None:
    """PROTOCOL.md #34: a new run replaces an awaiting_review run; its uncommitted proposals remain as (stale) history."""
    cancel_run(ws, old, f"superseded by {new_run_id}", {"superseded_by": new_run_id})
    for p in ws.proposals.values():
        if p["run_id"] == old["id"] and p["validity"] == "valid" and not any(a["proposal_id"] == p["id"] for a in ws.actions.values()):
            p["validity"] = "stale"
            p["validation_reasons"] = (p["validation_reasons"] + [f"run superseded by {new_run_id}"])[-30:]
            touch(p)


def operator_reconcile(ws: Workspace, action: dict, body: dict) -> dict:
    """rev3 #20: operator establishes the outcome of an `unknown` action, then the normal continuation/replan runs."""
    if action["status"] != "unknown":
        raise ApiError(409, "INVALID_TRANSITION", f"only unknown actions can be reconciled; action is {action['status']}")
    if any(j["action_id"] == action["id"] and j["kind"] == "reconcile" and _lease_live(j) for j in ws.jobs.values()):
        raise ApiError(409, "INVALID_TRANSITION", "a worker reconcile job currently holds a lease on this action")
    if body["expected_action_version"] != action["version"]:
        raise ApiError(409, "REVISION_CONFLICT", "expected_action_version does not match", {"version": action["version"]})
    run = ws.runs[action["run_id"]]
    for j in ws.jobs.values():
        if j["action_id"] == action["id"] and j["kind"] == "reconcile" and j["status"] == "queued":
            j["status"] = "cancelled"
    prev = max((j for j in ws.jobs.values() if j["action_id"] == action["id"]), key=lambda j: j["seq"])
    action["_reconciled_by"] = {"actor_id": "person_operator", "evidence_ids": body["evidence_ids"], "at": now()}
    if body["outcome"] == "succeeded":
        tgt = body["target"]  # PROTOCOL.md #28: operator names the target; the API issues the Receipt
        if action["connector"] == "local":
            eff = ws.local_effects.get(action["id"])
            if not eff:
                raise ApiError(422, "POLICY_VIOLATION", "receipt target does not exist: no local effect was recorded for this "
                               "action (PROTOCOL.md #26)", {"action_id": action["id"], "target_id": tgt["target_id"]})
            if tgt["target_id"] != eff["target_id"]:
                raise ApiError(422, "POLICY_VIOLATION", "receipt target does not exist for this action (PROTOCOL.md #26)",
                               {"action_id": action["id"], "target_id": tgt["target_id"]})
            finalize_local(ws, action, eff)  # receipt id/result_hash issued from the local readback
        else:
            # Mock cannot read Linear back; it attributes the operator-named target and issues the receipt itself.
            readback = {"action_id": action["id"], "target_id": tgt["target_id"], "target_url": tgt["target_url"],
                        "reconciled_by": "person_operator"}
            apply_effect(ws, action, external_receipt={
                "id": bounded_id("receipt", action["id"]), "connector": action["connector"], "target_id": tgt["target_id"],
                "target_url": tgt["target_url"], "observed_at": now(), "result_hash": sha256(canonical(readback))})
        nxt = _continuation(ws, run, action, prev) if run["status"] not in ("running",) else None
    else:
        action["status"] = "failed"
        action["error"] = err("DISPATCH_FAILED", "operator reconciliation established the effect did not happen")
        touch(action)
        nxt = _replan(ws, run) if run["status"] not in ("running",) else None
    if not run_leased(ws, run["id"]):
        _action_event(ws, action)
    return action


def get_job(ws: Workspace, job_id: str) -> dict:
    job = ws.jobs.get(job_id)
    if job is None:
        raise not_found("job", job_id)
    return job


def check_lease(ws: Workspace, job_id: str, lease_token: str, fencing_token, *, kinds: tuple = (), run_id: str | None = None,
                action_id: str | None = None) -> dict:
    job = ws.jobs.get(job_id)
    if job is None:
        raise ApiError(409, "LEASE_LOST", f"job {job_id} has no active lease", {"job_id": job_id})
    try:
        ft = int(fencing_token)
    except (TypeError, ValueError):
        raise ApiError(409, "LEASE_LOST", "fencing token is not current", {"job_id": job_id})
    if job["status"] != "leased" or job["lease_token"] != lease_token or job["fencing_token"] != ft:
        raise ApiError(409, "LEASE_LOST", "lease token/fencing token is not current for this job", {"job_id": job_id})
    if not _lease_live(job):
        raise ApiError(409, "LEASE_LOST", "lease has expired", {"job_id": job_id, "expired_at": iso(job["expires_dt"])})
    if kinds and job["kind"] not in kinds:
        raise ApiError(409, "LEASE_LOST", f"job {job_id} ({job['kind']}) does not cover this operation", {"job_id": job_id})
    if run_id is not None and job["run_id"] != run_id:
        raise ApiError(409, "LEASE_LOST", f"job {job_id} is not bound to run {run_id}", {"job_id": job_id})
    if action_id is not None and job["action_id"] != action_id:
        raise ApiError(409, "LEASE_LOST", f"job {job_id} is not bound to action {action_id}", {"job_id": job_id})
    return job


def heartbeat(ws: Workspace, job: dict) -> dict:
    job["expires_dt"] = plus(job["lease_seconds"])
    return lease_view(job)


def checkpoint(ws: Workspace, job: dict, body: dict) -> dict:
    if not job["run_id"]:
        raise ApiError(409, "INVALID_TRANSITION", f"{job['kind']} job has no run to checkpoint")
    run = ws.runs[job["run_id"]]
    if body["event"]["type"] in API_OWNED_EVENTS:  # rev2 #17
        raise ApiError(422, "INVALID_INPUT", f"event type {body['event']['type']} is API-owned and cannot be emitted by a worker",
                       {"fields": {"event/type": f"must not be one of {sorted(API_OWNED_EVENTS)}"}})
    for prior in ws.events[run["id"]]:
        if prior["step_id"] == body["step_id"]:
            return {"event": prior, "next_event_seq": run["event_seq"]}
    cp = body["checkpoint"]
    if len(canonical(cp).encode()) > CHECKPOINT_MAX_BYTES:
        raise ApiError(422, "INVALID_INPUT", "checkpoint exceeds 64 KB", {"fields": {"checkpoint": "max 65536 bytes"}})
    missing = [k for k in ("completed_call_ids", "pending_action_id", "stage", "usage") if k not in cp]
    if missing:
        raise ApiError(422, "INVALID_INPUT", "checkpoint is missing required keys",
                       {"fields": {f"checkpoint/{k}": "required" for k in missing}})
    from .common import contract_errors
    bad = contract_errors("Usage", cp["usage"]) + contract_errors("RunStage", cp["stage"])
    if not isinstance(cp["completed_call_ids"], list):
        bad.append("completed_call_ids must be an array")
    if bad:
        raise ApiError(422, "INVALID_INPUT", "checkpoint fields are invalid", {"fields": {"checkpoint": "; ".join(bad)[:500]}})
    if body["expected_event_seq"] != run["event_seq"]:
        raise ApiError(409, "REVISION_CONFLICT", "expected_event_seq does not match the run's current event_seq",
                       {"expected_event_seq": body["expected_event_seq"], "current_event_seq": run["event_seq"]})
    e = body["event"]
    ev = append_event(ws, run, e["type"], e["payload"], step_id=body["step_id"], tool_name=e["tool_name"])
    job["checkpoint"] = cp
    run["_checkpoint"] = cp
    run["usage"] = {k: max(run["usage"][k], cp["usage"][k]) for k in run["usage"]}
    if run["status"] not in ("cancelled",) and cp["stage"] != run["stage"]:
        run["stage"] = cp["stage"]
    return {"event": ev, "next_event_seq": run["event_seq"]}


# =========================================================================== completion

RESULT_FIELDS = {
    "dispatch": ("action_id", "execution_state", "receipt", "error"),
    "reconcile": ("action_id", "lookup_evidence", "established", "receipt"),
    "refresh": ("work_item_ids", "observation_ids", "affected_request_ids"),
}


def complete(ws: Workspace, job: dict, body: dict) -> dict:
    outcome, result = body["outcome"], body["result"]
    req_fields = RESULT_FIELDS.get(job["kind"], ())
    if job["kind"] == "investigate":
        req_fields = ("proposal_ids",)  # rev2 #12
    missing = [f for f in req_fields if f not in result]
    if missing:
        raise ApiError(422, "INVALID_INPUT", f"{job['kind']} completion result is missing fields",
                       {"fields": {f"result/{f}": "required" for f in missing}})
    if job["kind"] in ("dispatch", "reconcile") and result["action_id"] != job["action_id"]:
        raise ApiError(422, "INVALID_INPUT", "result.action_id does not match the job's action",
                       {"fields": {"result/action_id": f"expected {job['action_id']}"}})
    next_job = None
    handler = {"investigate": _complete_investigate, "dispatch": _complete_dispatch,
               "reconcile": _complete_reconcile, "refresh": _complete_refresh}[job["kind"]]
    next_job = handler(ws, job, outcome, result)
    if job["status"] == "leased":
        job["status"] = "completed"
    run = ws.runs.get(job["run_id"]) if job["run_id"] else None
    if run and run["status"] == "cancelled" and last_event_type(ws, run["id"]) != "run_finished":
        append_event(ws, run, "run_finished", {"run_status": "cancelled",
                                               "request_status": ws.requests[run["request_id"]]["status"], "action_id": None})
    receipt = {"job_id": job["id"], "completion_id": body["completion_id"], "outcome": outcome, "recorded_at": now(),
               "run": pub(run) if run else None, "next_job_id": next_job["id"] if next_job else None}
    # PROTOCOL.md #35: completion ids are per attempt; a retryable_failure requeues the job with no final completion
    job["completions"][body["completion_id"]] = receipt
    return receipt


def _retry(ws: Workspace, job: dict) -> dict:
    """retryable_failure: release the lease and requeue the same job with backoff (attempt preserved)."""
    if job["attempt"] >= 3:
        return None
    job.update(status="queued", lease_token=None, expires_dt=None, due_dt=plus(min(2 ** (job["attempt"] - 1), 8) * 0.5))
    if job["run_id"]:
        run = ws.runs[job["run_id"]]
        if run["status"] == "running":
            set_run(run, "queued")
    return job


def _fail_run(ws: Workspace, run: dict, code: str, message: str, retryable: bool = False) -> None:
    run["error"] = err(code, message, retryable)
    set_run(run, "failed")
    req = ws.requests[run["request_id"]]
    if last_event_type(ws, run["id"]) != "run_finished":
        append_event(ws, run, "run_finished", {"run_status": "failed", "request_status": req["status"], "action_id": None})


def _finish_run(ws: Workspace, run: dict, stage_hint: str | None) -> None:
    req = ws.requests[run["request_id"]]
    acts = sorted((a for a in ws.actions.values() if a["run_id"] == run["id"] and a["status"] == "succeeded"),
                  key=lambda a: a["_seq"])
    last = acts[-1] if acts else None
    if last and last["kind"] == "clarify" and req["status"] == "awaiting_input":
        set_run(run, "waiting_for_input", "observe")
        if last_event_type(ws, run["id"]) != "waiting_for_input":
            q = ws.proposals[last["proposal_id"]]["payload"]["question"]
            append_event(ws, run, "waiting_for_input", {"action_id": last["id"], "question": q})
        return
    set_run(run, "succeeded", stage_hint if stage_hint in ("observe", "verify", "remember") else "remember")
    if last_event_type(ws, run["id"]) != "run_finished":
        append_event(ws, run, "run_finished", {"run_status": "succeeded", "request_status": req["status"],
                                               "action_id": last["id"] if last else None})


def _complete_investigate(ws, job, outcome, result):
    run = ws.runs[job["run_id"]]
    if outcome == "unknown":
        raise ApiError(422, "INVALID_INPUT", "outcome unknown is only valid for dispatch/reconcile jobs",
                       {"fields": {"outcome": "not allowed for investigate"}})
    if outcome == "retryable_failure":
        return _retry(ws, job) or _fail_run(ws, run, "JOB_FAILED", "investigate job exhausted retries", True)
    if run["status"] == "cancelled":
        return None
    if outcome == "failed":
        e = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = e.get("code") or result.get("code") or "JOB_FAILED"
        _fail_run(ws, run, str(code)[:100], str(e.get("message") or result.get("summary") or "investigation failed"),
                  retryable=code == "BUDGET_EXHAUSTED")
        return None
    if outcome == "waiting":
        if result.get("awaiting") != "review":
            raise ApiError(422, "INVALID_INPUT", "investigate waiting completion requires awaiting='review'",
                           {"fields": {"result/awaiting": "must be 'review'"}})
        set_run(run, "awaiting_review", "decide")
        set_request(ws.requests[run["request_id"]], "awaiting_review")
        return None
    # succeeded
    pending = [j for j in ws.jobs.values() if j["run_id"] == run["id"] and j["kind"] == "dispatch" and j["status"] == "queued"]
    if pending:
        set_run(run, "queued", "act")
        return pending[0]
    _finish_run(ws, run, result.get("stage"))
    return None


def _continuation(ws: Workspace, run: dict, action: dict, previous_job: dict) -> dict | None:
    if run["status"] == "cancelled":
        return None
    cp = {"resume_stage": "observe", "action_id": action["id"], "previous_job_id": previous_job["id"]}  # rev2 #13
    set_run(run, "queued", "observe")
    return enqueue(ws, "investigate", run=run, checkpoint=cp)


def _replan(ws: Workspace, run: dict) -> dict | None:
    if run["status"] == "cancelled":
        return None
    run["usage"]["replans"] += 1
    if run["usage"]["replans"] > run["budget"]["max_replans"]:
        _fail_run(ws, run, "BUDGET_EXHAUSTED", "replan budget exhausted", True)
        return None
    set_run(run, "queued", "investigate")
    cp = dict(run.get("_checkpoint") or {})  # rev2 #18: last worker checkpoint + resume_stage + resume_reason
    cp.update(resume_stage="investigate", resume_reason="replan after failed dispatch revalidation")
    return enqueue(ws, "investigate", run=run, checkpoint=cp)


def _action_event(ws, action):
    run = ws.runs[action["run_id"]]
    append_event(ws, run, "action_updated", {"action_id": action["id"], "status": action["status"], "connector": action["connector"]})


def _complete_dispatch(ws, job, outcome, result):
    action = ws.actions[job["action_id"]]
    run = ws.runs[job["run_id"]]
    if outcome == "waiting":
        raise ApiError(422, "INVALID_INPUT", "outcome waiting is not valid for dispatch jobs", {"fields": {"outcome": "invalid"}})
    if outcome == "retryable_failure":
        if action["status"] != "accepted":
            raise ApiError(409, "INVALID_TRANSITION", "action already left 'accepted'; report unknown, not retryable_failure")
        return _retry(ws, job)
    if outcome == "succeeded":
        if action["connector"] == "linear" and action["status"] == "dispatching":
            rc = result.get("receipt")
            from .common import contract_errors
            if not isinstance(rc, dict) or contract_errors("Receipt", rc) or rc["connector"] != "linear":
                raise ApiError(422, "INVALID_INPUT", "linear dispatch success requires a valid linear Receipt readback", {"fields": {"result/receipt": "valid linear Receipt required"}})
            apply_effect(ws, action, external_receipt=rc)
        if action["status"] != "succeeded" or not action["receipt"]:
            raise ApiError(409, "INVALID_TRANSITION", f"action is {action['status']} without a persisted receipt; cannot complete succeeded")
        _action_event(ws, action)
        return _continuation(ws, run, action, job)
    if outcome == "unknown":
        if action["status"] in ("accepted", "dispatching"):
            action["status"] = "unknown"
            touch(action)
        _action_event(ws, action)
        set_run(run, "queued", "act") if run["status"] != "cancelled" else None
        return enqueue(ws, "reconcile", run=run, action_id=action["id"])
    # failed
    if action["status"] in ("accepted", "dispatching"):
        e = result.get("error") if isinstance(result.get("error"), dict) else None
        action["status"] = "failed"
        action["error"] = err((e or {}).get("code", "DISPATCH_FAILED"), (e or {}).get("message", "dispatch failed"))
        touch(action)
    _action_event(ws, action)
    p = ws.proposals[action["proposal_id"]]
    if p["validity"] == "valid" and action["error"] and action["error"]["code"] in ("REVISION_CONFLICT", "POLICY_VIOLATION"):
        p["validity"] = "stale"
        touch(p)
    return _replan(ws, run)


def _complete_reconcile(ws, job, outcome, result):
    action = ws.actions[job["action_id"]]
    run = ws.runs[job["run_id"]]
    if outcome == "retryable_failure":
        return _retry(ws, job)
    if result["established"] is True and outcome == "succeeded":
        if action["connector"] == "local":
            eff = ws.local_effects.get(action["id"])
            if not eff:
                raise ApiError(409, "INVALID_TRANSITION", "no local effect exists for this action; cannot establish success")
            rc = result.get("receipt")
            if isinstance(rc, dict) and rc.get("target_id") not in (None, eff["target_id"]):
                raise ApiError(422, "INVALID_INPUT", "receipt target does not match the recorded local effect", {"fields": {"result/receipt/target_id": "does not match local effect"}})
            finalize_local(ws, action, eff)
        else:
            rc = result.get("receipt")
            from .common import contract_errors
            if not isinstance(rc, dict) or contract_errors("Receipt", rc):
                raise ApiError(422, "INVALID_INPUT", "established linear result requires a valid Receipt", {"fields": {"result/receipt": "valid Receipt required"}})
            apply_effect(ws, action, external_receipt=rc)
        _action_event(ws, action)
        return _continuation(ws, run, action, job)
    if result["established"] is True and outcome == "failed":
        action["status"] = "failed"
        action["error"] = err("DISPATCH_FAILED", "reconciliation confirmed the effect did not happen")
        touch(action)
        _action_event(ws, action)
        return _replan(ws, run)
    # still unknown -> operator reconciliation
    if run["status"] != "cancelled":
        set_run(run, "awaiting_review", "act")
        set_request(ws.requests[run["request_id"]], "awaiting_review")
    return None


def _complete_refresh(ws, job, outcome, result):
    if outcome == "retryable_failure":
        return _retry(ws, job)
    return None


# =========================================================================== proposals

def _active_decisions(ws, scope_ids):
    from .context import _decision_active
    return [d for d in ws.decisions.values() if _decision_active(d) and d["scope"]["entity_id"] in scope_ids]


def evaluate_proposal(ws: Workspace, body: dict, req: dict) -> tuple[str, list[str], set[str], str]:
    kind, pl = body["kind"], body["payload"]
    reasons_invalid, reasons_stale = [], []
    consulted = {req["id"]}
    connector = "local"
    for ev in body["evidence_ids"]:
        if ev not in ws.evidence:
            reasons_invalid.append(f"EVIDENCE_INVALID: evidence {ev} does not exist in this workspace")
        else:
            consulted |= ws.ev_entities.get(ev, set())
    if not body["evidence_ids"] and kind not in ("clarify", "escalate"):
        reasons_invalid.append("EVIDENCE_INVALID: at least one evidence id is required for this action kind")
    if kind == "link_work":
        w = ws.work_items.get(pl["work_item_id"])
        consulted.add(pl["work_item_id"])
        if not w:
            reasons_invalid.append(f"EVIDENCE_INVALID: work item {pl['work_item_id']} does not exist")
        elif w["status"] == "cancelled":
            reasons_invalid.append(f"POLICY_VIOLATION: work item {w['id']} is cancelled; reinvestigate or escalate")
        if pl["work_item_id"] in req["linked_work_ids"]:
            reasons_invalid.append(f"POLICY_VIOLATION: request is already linked to {pl['work_item_id']}")
    elif kind == "create_work":
        connector = pl["connector"]
        if connector == "linear" and not linear_enabled():
            reasons_invalid.append("CAPABILITY_DISABLED: linear connector is unconfigured")
        for d in pl["dependency_ids"]:
            consulted.add(d)
            if d not in ws.work_items:
                reasons_invalid.append(f"EVIDENCE_INVALID: dependency {d} does not exist")
        if pl["capability_id"]:
            consulted.add(pl["capability_id"])
            if pl["capability_id"] not in ws.capabilities:
                reasons_invalid.append(f"EVIDENCE_INVALID: capability {pl['capability_id']} does not exist")
    if kind in ("create_work", "escalate") and pl.get("owner_id"):
        o = ws.people.get(pl["owner_id"])
        consulted.add(pl["owner_id"])
        if not o:
            reasons_invalid.append(f"POLICY_VIOLATION: owner {pl['owner_id']} does not exist")
        elif not o.get("active", True):
            reasons_invalid.append(f"POLICY_VIOLATION: owner {pl['owner_id']} is inactive")
    if kind == "escalate" and pl.get("team_id") and pl["team_id"] not in ws.teams:
        reasons_invalid.append(f"POLICY_VIOLATION: team {pl['team_id']} does not exist")
    scope_ids = {req["id"], req["customer_id"], *req["capability_ids"], *consulted}
    for d in _active_decisions(ws, scope_ids):
        consulted.add(d["id"])
        if d["rule_type"] == "allowed_action_types" and isinstance(d["value"], list) and kind not in d["value"]:
            reasons_invalid.append(f"POLICY_VIOLATION: decision {d['id']} does not allow action kind {kind}")
        if d["rule_type"] == "forbidden_workaround" and isinstance(d["value"], str) and d["value"]:
            text = json.dumps(pl).lower()
            if d["value"].lower() in text or d["value"].replace("_", " ").lower() in text:
                reasons_invalid.append(f"POLICY_VIOLATION: payload uses workaround forbidden by decision {d['id']}")
    if body["based_on_revision"] != ws.revision:
        reasons_stale.append(f"based_on_revision {body['based_on_revision']} != current knowledge_revision {ws.revision}")
    validity = "invalid" if reasons_invalid else ("stale" if reasons_stale else "valid")
    return validity, [r[:1000] for r in (reasons_invalid + reasons_stale)][:30], consulted, connector


def create_proposal(ws: Workspace, body: dict) -> dict:
    req = ws.get("requests", body["request_id"], "request")
    run = ws.get("runs", body["run_id"], "run")
    if run["request_id"] != req["id"]:
        raise ApiError(422, "INVALID_INPUT", "run does not belong to request", {"fields": {"run_id": "not a run of request_id"}})
    validity, reasons, consulted, connector = evaluate_proposal(ws, body, req)
    p = {
        "id": new_id("proposal"), "workspace_id": ws.id, "request_id": req["id"], "run_id": run["id"],
        "kind": body["kind"], "payload": body["payload"], "evidence_ids": body["evidence_ids"],
        "explanation": body["explanation"], "rejected_alternatives": body["rejected_alternatives"],
        "based_on_revision": body["based_on_revision"], "validity": validity, "validation_reasons": reasons,
        "authorization_id": None, "created_at": now(), "version": 1, "_consulted": consulted, "_connector": connector,
    }
    ws.proposals[p["id"]] = p
    return p


def commit_proposal(ws: Workspace, p: dict, body: dict, *, caller: str, idem_key: str) -> tuple[dict, dict]:
    if any(a["proposal_id"] == p["id"] for a in ws.actions.values()):
        raise ApiError(409, "INVALID_TRANSITION", "proposal is already committed")
    if ws.runs[p["run_id"]]["status"] == "cancelled" and p["validity"] == "valid":
        p["validity"] = "stale"  # PROTOCOL.md #34: stale proposals conflict, never INVALID_TRANSITION
        p["validation_reasons"] = (p["validation_reasons"] + ["proposal's run was cancelled"])[-30:]
        touch(p)
    if body["expected_revision"] != ws.revision:
        raise ApiError(409, "REVISION_CONFLICT", "expected_revision does not match current knowledge_revision",
                       {"expected_revision": body["expected_revision"], "knowledge_revision": ws.revision})
    if p["validity"] == "valid" and p["based_on_revision"] != ws.revision:
        p["validity"] = "stale"
        p["validation_reasons"] = (p["validation_reasons"] + [f"knowledge_revision moved to {ws.revision}"])[-30:]
        touch(p)
    if p["validity"] == "stale":
        raise ApiError(409, "REVISION_CONFLICT", "proposal is stale; replan against current knowledge",
                       {"proposal_id": p["id"], "validation_reasons": p["validation_reasons"][:10]})
    if p["validity"] == "invalid":
        code = "EVIDENCE_INVALID" if any(r.startswith("EVIDENCE_INVALID") for r in p["validation_reasons"]) else "POLICY_VIOLATION"
        raise ApiError(422, code, "proposal is invalid and cannot be committed",
                       {"proposal_id": p["id"], "validation_reasons": p["validation_reasons"][:10]})
    auth_id = body["authorization_id"]
    if auth_id is not None:
        a = ws.authorizations.get(auth_id)
        if a is None or a["request_id"] not in (None, p["request_id"]):
            raise ApiError(422, "POLICY_VIOLATION", f"authorization {auth_id} does not exist or does not apply")
    if caller == "worker":
        if auth_id is not None:
            raise ApiError(422, "POLICY_VIOLATION", "workers must pass authorization_id: null; only operator commits authorize")
        if p["kind"] not in ws.auto_commit_kinds:
            raise ApiError(422, "POLICY_VIOLATION", f"worker may not commit {p['kind']} without an operator authorization",
                           {"auto_commit_kinds": ws.auto_commit_kinds})
    elif auth_id is None:
        auth_id = new_id("authorization")
        ws.authorizations[auth_id] = {"id": auth_id, "request_id": p["request_id"], "proposal_id": p["id"],
                                      "actor_id": "person_operator", "created_at": now()}
    p["authorization_id"] = auth_id
    touch(p)
    t = now()
    action = {
        "id": new_id("action"), "workspace_id": ws.id, "request_id": p["request_id"], "run_id": p["run_id"],
        "proposal_id": p["id"], "kind": p["kind"], "status": "accepted", "verification": "pending",
        "idempotency_key": idem_key, "connector": p["_connector"], "receipt": None, "error": None,
        "created_at": t, "updated_at": t, "version": 1, "_seq": len(ws.actions) + 1,
    }
    ws.actions[action["id"]] = action
    run = ws.runs[p["run_id"]]
    job = enqueue(ws, "dispatch", run=run, action_id=action["id"])
    if caller == "operator":
        if run["status"] in ("awaiting_review", "succeeded", "failed", "waiting_for_input"):
            run["error"] = None if run["status"] == "failed" else run["error"]
            set_run(run, "queued", "act")
        if run["status"] != "running":  # never append under a live worker lease (would break expected_event_seq)
            append_event(ws, run, "action_updated", {"action_id": action["id"], "status": "accepted", "connector": action["connector"]})
    return action, job


# =========================================================================== effects

def revalidate(ws: Workspace, action: dict) -> None:
    """Raise after marking the action failed if its intent is no longer valid (never dispatch stale intent)."""
    p = ws.proposals[action["proposal_id"]]
    problem = None
    if p["validity"] != "valid":
        problem = ("REVISION_CONFLICT", f"proposal {p['id']} is {p['validity']}")
    elif p["kind"] == "link_work":
        w = ws.work_items.get(p["payload"]["work_item_id"])
        if not w or w["status"] == "cancelled":
            problem = ("POLICY_VIOLATION", "linked work no longer exists or is cancelled")
    if problem:
        code, msg = problem
        action["status"] = "failed"
        action["error"] = err(code, msg)
        touch(action)
        if p["validity"] == "valid":
            p["validity"] = "stale"
            p["validation_reasons"] = (p["validation_reasons"] + [msg])[-30:]
            touch(p)
        raise ApiError(409 if code == "REVISION_CONFLICT" else 422, code, f"dispatch revalidation failed: {msg}",
                       {"action_id": action["id"]})


def perform_local_effect(ws: Workspace, action: dict) -> dict:
    """Create the local effect (task/draft/link/escalation). Deduplicated by action marker."""
    if action["id"] in ws.local_effects:
        return ws.local_effects[action["id"]]
    p = ws.proposals[action["proposal_id"]]
    pl, req = p["payload"], ws.requests[action["request_id"]]
    t = now()
    if p["kind"] in ("answer", "clarify"):
        target = bounded_id("draft", action["id"])
        rec = {"id": target, "kind": p["kind"], "request_id": req["id"], "action_id": action["id"], "payload": pl,
               "sent": False, "created_at": t}
        ws.drafts[target] = rec
    elif p["kind"] == "link_work":
        target = bounded_id("link", f"{req['id']}__{pl['work_item_id']}")
        rec = {"id": target, "request_id": req["id"], "work_item_id": pl["work_item_id"], "comment_draft": pl["comment_text"],
               "action_id": action["id"], "created_at": t}
    elif p["kind"] == "create_work":
        target = bounded_id("work", action["id"].removeprefix("action_"))
        rec = {"id": target, "title": pl["title"], "action_id": action["id"], "created_at": t}
        src, _ = ws.add_source(bounded_id("src", target), title=f"Local task {target}: {pl['title']}", kind="work",
                               origin="owned_permitted", body=canonical({"title": pl["title"], "description": pl["description"],
                                                                         "acceptance_criteria": pl["acceptance_criteria"]}),
                               observed_at=t)
        ev = ws.add_evidence(src, excerpt=pl["description"], location="body", entities=[target])
        ws.work_items[target] = {
            "id": target, "workspace_id": ws.id, "title": pl["title"], "kind": "ticket", "status": "open",
            "owner_id": pl["owner_id"], "connector": "local", "external_id": None, "external_url": None,
            "forecast_at": None, "observed_at": t, "evidence_ids": [ev["id"]], "version": 1,
            "_description": pl["description"], "_capability_ids": [pl["capability_id"]] if pl["capability_id"] else [],
            "_depends_on": pl["dependency_ids"], "_acceptance_criteria": pl["acceptance_criteria"],
        }
        ws.add_node(target, "WorkItem", pl["title"])
        if pl["capability_id"]:
            ws.add_edge("IMPLEMENTED_BY", pl["capability_id"], target, [ev["id"]], asserted_by=target)
        for d in pl["dependency_ids"]:
            ws.add_edge("DEPENDS_ON", target, d, [ev["id"]], asserted_by=target)
        if pl["owner_id"]:
            ws.add_edge("OWNED_BY", target, pl["owner_id"], [ev["id"]], asserted_by=target)
    else:  # escalate
        target = bounded_id("escalation", action["id"])
        rec = {"id": target, "request_id": req["id"], "reason": pl["reason"], "owner_id": pl["owner_id"],
               "team_id": pl["team_id"], "action_id": action["id"], "created_at": t}
        ws.escalations[target] = rec
    eff = {"target_id": target, "record": rec, "applied": False}
    ws.local_effects[action["id"]] = eff
    return eff


def apply_request_transition(ws: Workspace, action: dict, work_item_id: str | None) -> None:
    req = ws.requests[action["request_id"]]
    kind = action["kind"]
    if kind == "answer":
        set_request(req, "awaiting_verification")
    elif kind == "clarify":
        set_request(req, "awaiting_input")
    elif kind in ("link_work", "create_work"):
        if work_item_id and work_item_id not in req["linked_work_ids"]:
            req["linked_work_ids"].append(work_item_id)
            touch(req)
            ws.add_edge("TRACKED_BY", req["id"], work_item_id, [], asserted_by=action["id"])
        w = ws.work_items.get(work_item_id)
        set_request(req, "awaiting_verification" if w and w["status"] == "done" else "awaiting_work")
        ws.bump()
    elif kind == "escalate":
        pl = ws.proposals[action["proposal_id"]]["payload"]
        if pl["owner_id"]:
            req["owner_id"] = pl["owner_id"]
        set_request(req, "escalated")


def finalize_local(ws: Workspace, action: dict, eff: dict) -> dict:
    if not eff["applied"]:
        p = ws.proposals[action["proposal_id"]]
        wid = p["payload"].get("work_item_id") if p["kind"] == "link_work" else (eff["target_id"] if p["kind"] == "create_work" else None)
        apply_request_transition(ws, action, wid)
        eff["applied"] = True
    action["receipt"] = {"id": bounded_id("receipt", action["id"]), "connector": "local", "target_id": eff["target_id"],
                         "target_url": None, "observed_at": now(), "result_hash": sha256(canonical(eff["record"]))}
    action["status"] = "succeeded"
    action["error"] = None
    touch(action)
    return action


def apply_effect(ws: Workspace, action: dict, *, external_receipt: dict) -> None:
    """Record a linear (fake) result reported by the worker: mirror the external work item and apply transitions."""
    p = ws.proposals[action["proposal_id"]]
    wid = None
    if p["kind"] == "create_work":
        wid = bounded_id("work", external_receipt["target_id"])
        if wid not in ws.work_items:
            t = now()
            ws.work_items[wid] = {
                "id": wid, "workspace_id": ws.id, "title": p["payload"]["title"], "kind": "ticket", "status": "open",
                "owner_id": p["payload"]["owner_id"], "connector": "linear", "external_id": external_receipt["target_id"],
                "external_url": external_receipt["target_url"], "forecast_at": None, "observed_at": t, "evidence_ids": [],
                "version": 1, "_description": p["payload"]["description"], "_capability_ids": [], "_depends_on": [],
                "_acceptance_criteria": [],
            }
            ws.add_node(wid, "WorkItem", p["payload"]["title"])
    elif p["kind"] == "link_work":
        wid = p["payload"]["work_item_id"]
    apply_request_transition(ws, action, wid)
    action["receipt"] = dict(external_receipt)
    action["status"] = "succeeded"
    action["error"] = None
    touch(action)


def payload_hash(ws: Workspace, action: dict) -> str:
    return sha256(canonical(ws.proposals[action["proposal_id"]]["payload"]))


# =========================================================================== observations & verification

def record_observation(ws: Workspace, req: dict, body: dict, actor: str) -> dict:
    action = None
    if body["action_id"]:
        action = ws.actions.get(body["action_id"])
        if not action or action["request_id"] != req["id"]:
            raise not_found("action", body["action_id"])
    for ev in body["evidence_ids"]:
        if ev not in ws.evidence:
            raise ApiError(422, "INVALID_INPUT", f"evidence {ev} does not exist in this workspace",
                           {"fields": {"evidence_ids": f"unknown evidence {ev}"}})
    t = now()
    oid = new_id("observation")
    src, _ = ws.add_source(bounded_id("src", oid), title=f"Observation {body['kind']} on {req['id']}", kind="observation",
                           origin="owned_permitted", body=body["body"], observed_at=t)
    ev = ws.add_evidence(src, excerpt=body["body"], location="body", entities=[req["id"]] + ([action["id"]] if action else []))
    obs = {"id": oid, "workspace_id": ws.id, "request_id": req["id"], "action_id": body["action_id"], "kind": body["kind"],
           "verification": body["verification"], "body": body["body"], "evidence_ids": list(dict.fromkeys(body["evidence_ids"] + [ev["id"]]))[:100],
           "actor_id": actor, "observed_at": t}
    ws.observations[oid] = obs
    ws.add_node(oid, "Observation", f"{body['kind']} ({body['verification']})")
    ws.add_edge("OBSERVED_AS", req["id"], oid, [ev["id"]], asserted_by=oid)
    if action:
        ws.add_node(action["id"], "Action", f"{action['kind']} action")
        ws.add_edge("VERIFIES", oid, action["id"], [ev["id"]], asserted_by=oid)
    ws.bump()
    verdict = body["verification"]
    if body["kind"] in SUFFICIENT_KINDS and verdict in ("passed", "failed"):
        if action and action["verification"] != verdict:
            action["verification"] = verdict
            touch(action)
        att_id = new_id("attempt")
        ws.attempts[att_id] = {
            "id": att_id, "workspace_id": ws.id, "request_id": req["id"],
            "procedure_key": (action["kind"] if action else "operator_verification"),
            "context_signature": (":".join(req["capability_ids"]) or "uncategorized") + (f":{action['kind']}" if action else ""),
            "scope": {"entity_type": "request", "entity_id": req["id"]},
            "outcome": "succeeded" if verdict == "passed" else "failed", "verification": verdict,
            "summary": body["body"], "evidence_ids": obs["evidence_ids"], "observed_at": t,
        }
        ws.add_node(att_id, "Attempt", f"{verdict} verification")
        ws.add_edge("ATTEMPTED_FOR", att_id, req["id"], [ev["id"]], asserted_by=att_id)
        if verdict == "passed":
            set_request(req, "resolved")
        else:
            set_request(req, "investigating")
            if not any(r["request_id"] == req["id"] and r["status"] in ("queued", "running") for r in ws.runs.values()):
                prev = sorted((r for r in ws.runs.values() if r["request_id"] == req["id"]), key=lambda r: r["created_at"])
                create_run(ws, req, prev[-1]["mode"] if prev else "scripted", "follow-up after failed verification")
    if action and actor == "person_operator":
        run = ws.runs[action["run_id"]]
        if run["status"] != "running":
            append_event(ws, run, "observation_recorded", {"observation_id": oid, "kind": body["kind"], "verification": verdict})
    return obs
