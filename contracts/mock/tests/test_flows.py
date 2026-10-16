"""End-to-end protocol choreography against the mock (PROTOCOL.md "Job and run choreography")."""

from __future__ import annotations

import re

from conftest import OP, WK, new_request
from mock_api.app import store


def investigate_to_proposal(api, worker, req_id, kind, payload, *, evidence=None):
    """Claim the investigate job, emit run_started + a search tool call, read context, create a proposal."""
    lease = worker.claim(["investigate"])
    assert lease["kind"] == "investigate" and lease["action_id"] is None
    assert api.get(f"/requests/{req_id}").json()["data"]["status"] == "investigating"
    worker.checkpoint(lease, "run_started", {"mode": "scripted", "stage": "investigate"})
    worker.checkpoint(lease, "tool_started", {"call_id": "c1", "arguments": {"query": "scheduled exports"}},
                      tool_name="search_context", calls=())
    s = api.post("/context/search", {"query": "scheduled exports stopped", "entity_types": ["capability", "work_item", "source"],
                                     "customer_id": None, "limit": 5}, token=WK, key=None)
    assert s.status_code == 200 and s.json()["data"]["candidates"]
    worker.checkpoint(lease, "tool_completed", {"call_id": "c1", "ok": True, "result_count": len(s.json()["data"]["candidates"]),
                                                "ids": [c["id"] for c in s.json()["data"]["candidates"]], "duration_ms": 3,
                                                "error_code": None}, tool_name="search_context", calls=("c1",))
    ctx = api.get(f"/requests/{req_id}/context", token=WK).json()["data"]
    ev = evidence if evidence is not None else [ctx["sources"][0]["id"]]
    p = worker.propose(lease, req_id, kind, payload, ev, ctx["based_on_revision"])
    worker.checkpoint(lease, "proposal_ready", {"proposal_id": p["id"], "kind": kind, "validity": p["validity"]},
                      stage="decide", calls=("c1",))
    return lease, p


def dispatch_local(api, worker, action_id):
    lease = worker.claim(["dispatch"])
    assert lease["kind"] == "dispatch" and lease["action_id"] == action_id
    action = api.get(f"/actions/{action_id}", token=WK).json()["data"]
    assert action["status"] == "accepted"
    done = worker.execute_local(lease, action)
    assert done["status"] == "succeeded" and done["receipt"]["connector"] == "local"
    assert re.fullmatch(r"[a-f0-9]{64}", done["receipt"]["result_hash"])
    rc = worker.complete(lease, "succeeded", {"action_id": action_id, "execution_state": "succeeded",
                                              "receipt": done["receipt"], "error": None})
    assert rc["next_job_id"] and rc["run"]["status"] == "queued" and rc["run"]["stage"] == "observe"
    return done, rc


def continuation(api, worker, req_id, action_id, *, record_status=True):
    lease = worker.claim(["investigate"])
    cp = lease["checkpoint"]
    assert set(cp) == {"resume_stage", "action_id", "previous_job_id"}  # rev2 #13
    assert cp["resume_stage"] == "observe" and cp["action_id"] == action_id
    assert store.workspaces[api.ws].jobs[cp["previous_job_id"]]["kind"] in ("dispatch", "reconcile")
    a = api.get(f"/actions/{action_id}", token=WK).json()["data"]
    if record_status:
        r = api.get(f"/requests/{req_id}", token=WK).json()["data"]
        o = api.post(f"/requests/{req_id}/observations",
                     {"action_id": action_id, "kind": "connector_status", "verification": "pending",
                      "body": f"Readback: {a['receipt']['target_id']} exists.", "evidence_ids": [],
                      "expected_request_version": r["version"]}, token=WK, headers=worker.hdrs(lease))
        assert o.status_code == 201, o.text
        assert o.json()["data"]["request"]["status"] == r["status"]  # connector_status never resolves
        assert o.json()["data"]["observation"]["actor_id"] == "worker:worker_test"
    worker.checkpoint(lease, "tool_completed", {"call_id": "inspect", "ok": True, "result_count": 1, "ids": [action_id],
                                                "duration_ms": 1, "error_code": None}, tool_name="inspect_action", stage="observe")
    return worker.complete(lease, "succeeded", {"run_status_hint": "succeeded", "stage": "remember", "proposal_ids": [],
                                                "action_ids": [action_id], "summary": "Observed receipt.", "evidence_ids": []})


def answer_flow(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    assert req["request"]["status"] == "open" and req["run"]["status"] == "queued"
    lease, p = investigate_to_proposal(api, worker, rid, "answer",
                                       {"draft_text": "Exports are blocked by tracked work; we will update you.",
                                        "resolution_criteria": ["Customer confirms exports run"]})
    assert p["validity"] == "valid", p
    res = worker.commit(lease, p)
    action = res["action"]
    assert action["status"] == "accepted" and action["connector"] == "local" and res["job_id"]
    rc = worker.complete(lease, "succeeded", {"run_status_hint": "queued", "stage": "act", "proposal_ids": [p["id"]],
                                              "action_ids": [action["id"]], "summary": "Answer drafted.", "evidence_ids": p["evidence_ids"]})
    assert rc["run"]["status"] == "queued" and rc["run"]["stage"] == "act" and rc["next_job_id"] == res["job_id"]
    done, _ = dispatch_local(api, worker, action["id"])
    assert done["receipt"]["target_id"].startswith("draft_")
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_verification"
    rc = continuation(api, worker, rid, action["id"])
    assert rc["run"]["status"] == "succeeded"
    return rid, run_id, action["id"]


def test_answer_flow_end_to_end(api, worker):
    rid, run_id, aid = answer_flow(api, worker)
    run = api.get(f"/runs/{run_id}").json()["data"]
    assert run["status"] == "succeeded" and run["usage"]["tool_calls"] >= 1
    evs = api.get(f"/runs/{run_id}/events").json()
    types = [e["type"] for e in evs["data"]]
    assert types[0] == "run_started" and types[-1] == "run_finished" and "proposal_ready" in types
    assert [e["seq"] for e in evs["data"]] == list(range(1, len(types) + 1))
    # event cursor pagination
    page = api.get(f"/runs/{run_id}/events", params={"after_seq": 1, "limit": 2}).json()
    assert [e["seq"] for e in page["data"]] == [2, 3] and page["next_after_seq"] == 3 and page["has_more"] is True
    tail = api.get(f"/runs/{run_id}/events", params={"after_seq": len(types)}).json()
    assert tail["data"] == [] and tail["next_after_seq"] == len(types) and tail["has_more"] is False
    # no jobs left
    assert api.post("/jobs/claim", {"worker_id": "w", "kinds": ["investigate", "dispatch"], "lease_seconds": 60},
                    token=WK, private=True, key=None).status_code == 204
    # sufficient verification resolves
    r = api.get(f"/requests/{rid}").json()["data"]
    o = api.post(f"/requests/{rid}/observations", {"action_id": aid, "kind": "customer_confirmation", "verification": "passed",
                                                   "body": "Customer confirmed exports run.", "evidence_ids": [],
                                                   "expected_request_version": r["version"]})
    assert o.status_code == 201 and o.json()["data"]["request"]["status"] == "resolved"
    assert api.get(f"/actions/{aid}").json()["data"]["verification"] == "passed"


def test_link_work_requires_operator_commit(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    before = len(api.get("/work-items", params={"limit": 100}).json()["data"])
    lease, p = investigate_to_proposal(api, worker, rid, "link_work", {"work_item_id": "work_export_issue", "comment_text": None},
                                       evidence=["ev_export_issue_1"])
    assert p["validity"] == "valid"
    denied = worker.commit(lease, p, expect=422)
    assert denied["error"]["code"] == "POLICY_VIOLATION"
    worker.complete(lease, "waiting", {"proposal_ids": [p["id"]], "awaiting": "review"})
    assert api.get(f"/runs/{run_id}").json()["data"]["status"] == "awaiting_review"
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_review"
    body = {"expected_revision": p["based_on_revision"], "authorization_id": None}
    c1 = api.post(f"/proposals/{p['id']}/commit", body, key="op-commit-1")
    assert c1.status_code == 202
    action = c1.json()["data"]["action"]
    assert api.get(f"/proposals/{p['id']}").json()["data"]["authorization_id"]
    run = api.get(f"/runs/{run_id}").json()["data"]
    assert run["status"] == "queued" and run["stage"] == "act"
    dispatch_local(api, worker, action["id"])
    req_now = api.get(f"/requests/{rid}").json()["data"]
    assert req_now["status"] == "awaiting_work" and "work_export_issue" in req_now["linked_work_ids"]
    rc = continuation(api, worker, rid, action["id"])
    assert rc["run"]["status"] == "succeeded"
    a = api.get(f"/actions/{action['id']}").json()["data"]
    assert a["status"] == "succeeded" and a["verification"] == "pending"
    # same idempotency key returns the same action; no new work item was created
    c2 = api.post(f"/proposals/{p['id']}/commit", body, key="op-commit-1")
    assert c2.status_code == 202 and c2.json()["data"]["action"]["id"] == action["id"]
    assert c2.headers.get("Idempotent-Replayed") == "true"
    assert len(api.get("/work-items", params={"limit": 100}).json()["data"]) == before
    # a fresh commit of an already-committed proposal is an invalid transition
    c3 = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": api.rev(), "authorization_id": None})
    assert c3.status_code == 409 and c3.json()["error"]["code"] == "INVALID_TRANSITION"


def test_create_work_local_creates_task_with_receipt(api, worker):
    req = new_request(api, subject="SSO JIT provisioning missing", body="We need SSO just-in-time provisioning.",
                      caps=("cap_sso_login",))
    rid = req["request"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "create_work", {
        "title": "Add SSO JIT provisioning", "description": "Customers need JIT provisioning.", "acceptance_criteria": ["JIT works"],
        "owner_id": "person_maya", "capability_id": "cap_sso_login", "dependency_ids": [], "connector": "local"})
    assert p["validity"] == "valid"
    worker.complete(lease, "waiting", {"proposal_ids": [p["id"]], "awaiting": "review"})
    action = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": p["based_on_revision"], "authorization_id": None}
                      ).json()["data"]["action"]
    done, _ = dispatch_local(api, worker, action["id"])
    wi = done["receipt"]["target_id"]
    work = {w["id"]: w for w in api.get("/work-items", params={"limit": 100}).json()["data"]}
    assert wi in work and work[wi]["connector"] == "local" and work[wi]["status"] == "open"
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_work"
    assert continuation(api, worker, rid, action["id"])["run"]["status"] == "succeeded"
    # linear is unconfigured -> invalid proposal with CAPABILITY_DISABLED reason
    req2 = new_request(api, subject="Another SSO ask", body="SSO again", caps=("cap_sso_login",))
    lease, p2 = investigate_to_proposal(api, worker, req2["request"]["id"], "create_work", {
        "title": "x", "description": "y", "acceptance_criteria": [], "owner_id": None, "capability_id": None,
        "dependency_ids": [], "connector": "linear"})
    assert p2["validity"] == "invalid" and any("CAPABILITY_DISABLED" in r for r in p2["validation_reasons"])


def test_clarify_reply_resumes_same_run(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "clarify",
                                       {"question": "Which export schedule stopped?", "missing_fields": ["schedule_id"]})
    action = worker.commit(lease, p)["action"]
    worker.complete(lease, "succeeded", {"proposal_ids": [p["id"]], "action_ids": [action["id"]], "summary": "Asked."})
    dispatch_local(api, worker, action["id"])
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_input"
    rc = continuation(api, worker, rid, action["id"])
    assert rc["run"]["status"] == "waiting_for_input"
    evs = api.get(f"/runs/{run_id}/events").json()["data"]
    assert evs[-1]["type"] == "waiting_for_input" and evs[-1]["payload"]["question"] == "Which export schedule stopped?"
    seq_before = evs[-1]["seq"]
    # nothing to claim while waiting for the customer
    worker.claim(["investigate"], expect=204)
    r = api.get(f"/requests/{rid}").json()["data"]
    rep = api.post(f"/requests/{rid}/replies", {"body": "The daily finance schedule.", "external_ref": None,
                                                "expected_request_version": r["version"]})
    assert rep.status_code == 201
    out = rep.json()["data"]
    assert out["run"]["id"] == run_id and out["run"]["status"] == "queued"
    lease = worker.claim(["investigate"])
    assert lease["run_id"] == run_id and lease["checkpoint"]["reply_source_id"] == out["source_id"]
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "investigating"
    src = api.get(f"/sources/{out['source_id']}", params={"revision": out["source_revision"]}).json()["data"]
    assert src["body"] == "The daily finance schedule."
    worker.checkpoint(lease, "run_started", {"mode": "scripted", "stage": "investigate"})
    evs = api.get(f"/runs/{run_id}/events", params={"after_seq": 0}).json()["data"]
    assert evs[-1]["seq"] == seq_before + 1  # history retained, sequence continues
    # stale reply version conflicts
    bad = api.post(f"/requests/{rid}/replies", {"body": "x", "external_ref": None, "expected_request_version": 1})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "REVISION_CONFLICT"


def test_idempotency_replay_and_conflict(api):
    body = {"customer_id": "customer_acme", "subject": "Idem", "body": "Idempotent intake", "channel": "manual",
            "capability_ids": [], "external_ref": None, "auto_start": False, "planner_mode": "scripted"}
    r1 = api.post("/requests", body, key="idem-1")
    r2 = api.post("/requests", body, key="idem-1")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["data"]["request"]["id"] == r2.json()["data"]["request"]["id"]
    assert r2.headers["Idempotent-Replayed"] == "true"
    r3 = api.post("/requests", {**body, "subject": "Different"}, key="idem-1")
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    r4 = api.post("/requests", body, key=None)
    assert r4.status_code == 422 and r4.json()["error"]["code"] == "INVALID_INPUT"
    assert "header/Idempotency-Key" in r4.json()["error"]["details"]["fields"]
    # replay happens before revision checks
    dec = {"operation": "record", "decision_id": None, "rule_type": "date_extension_allowed", "value": False,
           "scope": {"entity_type": "customer", "entity_id": "customer_acme"}, "rationale": "No extensions.",
           "effective_at": "2026-10-01T00:00:00Z", "expires_at": None, "evidence_ids": [], "expected_revision": api.rev()}
    d1 = api.post("/decisions", dec, key="idem-dec")
    assert d1.status_code == 201
    d2 = api.post("/decisions", dec, key="idem-dec")  # revision moved since, but the recorded key replays
    assert d2.status_code == 201 and d2.json()["data"]["id"] == d1.json()["data"]["id"]
    d3 = api.post("/decisions", dec, key="idem-dec-new")
    assert d3.status_code == 409 and d3.json()["error"]["code"] == "REVISION_CONFLICT"
    # keys are scoped per caller: the same key from the worker is a different scope (and here, forbidden)
    w = api.post("/requests", body, key="idem-1", token=WK)
    assert w.status_code == 403


def test_lease_lost_and_fencing(api, worker):
    req = new_request(api)
    rid = req["request"]["id"]
    lease = worker.claim(["investigate"])
    hb = api.post(f"/jobs/{lease['job_id']}/heartbeat", worker.bind(lease), token=WK, private=True, key=None)
    assert hb.status_code == 200 and hb.json()["data"]["fencing_token"] == lease["fencing_token"]
    api.fault({"lease_expire_now": True})
    r = worker.checkpoint(lease, "run_started", {"mode": "scripted", "stage": "investigate"}, expect=409)
    assert r.json()["error"]["code"] == "LEASE_LOST"
    n_props = len(store.workspaces["ws_demo"].proposals)
    p = worker.propose(lease, rid, "answer", {"draft_text": "x", "resolution_criteria": []}, ["ev_export_issue_1"], api.rev(),
                       expect=409)
    assert p["error"]["code"] == "LEASE_LOST" and len(store.workspaces["ws_demo"].proposals) == n_props
    # recovery claim: same job, higher fencing token, attempt 2
    lease2 = worker.claim(["investigate"])
    assert lease2["job_id"] == lease["job_id"] and lease2["fencing_token"] > lease["fencing_token"] and lease2["attempt"] == 2
    old = api.post(f"/jobs/{lease['job_id']}/heartbeat", worker.bind(lease), token=WK, private=True, key=None)
    assert old.status_code == 409 and old.json()["error"]["code"] == "LEASE_LOST"
    comp = worker.complete(lease, "failed", {"proposal_ids": []}, expect=409)
    assert comp["error"]["code"] == "LEASE_LOST"
    # missing lease headers on a worker public mutation
    r = api.post("/proposals", {"request_id": rid, "run_id": lease2["run_id"], "kind": "answer",
                                "payload": {"draft_text": "x", "resolution_criteria": []}, "evidence_ids": [],
                                "explanation": "x", "rejected_alternatives": [], "based_on_revision": 0}, token=WK)
    assert r.status_code == 422
    # checkpoint ordering + step dedupe with the live lease
    worker.checkpoint(lease2, "run_started", {"mode": "scripted", "stage": "investigate"}, step_id="s-1")
    dup = worker.checkpoint(lease2, "run_started", {"mode": "scripted", "stage": "investigate"}, step_id="s-1",
                            expected_event_seq=0)
    assert dup.json()["data"]["event"]["seq"] == 1
    bad = worker.checkpoint(lease2, "tool_started", {"call_id": "c"}, expected_event_seq=0, expect=409)
    assert bad.json()["error"]["code"] == "REVISION_CONFLICT"
    # completion is idempotent by completion_id
    c1 = worker.complete(lease2, "failed", {"proposal_ids": [], "error": {"code": "BUDGET_EXHAUSTED", "message": "budget"}},
                         completion_id="cmp-fixed")
    c2 = worker.complete(lease2, "failed", {"proposal_ids": [], "error": {"code": "BUDGET_EXHAUSTED", "message": "budget"}},
                         completion_id="cmp-fixed")
    assert c1["recorded_at"] == c2["recorded_at"] and c1["run"]["status"] == "failed"


def test_stale_proposal_blocked(api, worker):
    req = new_request(api)
    rid = req["request"]["id"]
    api.fault({"stale_next_proposal": True})
    lease, p = investigate_to_proposal(api, worker, rid, "answer", {"draft_text": "x", "resolution_criteria": []})
    assert p["validity"] == "stale" and p["validation_reasons"]
    r = worker.commit(lease, p, expect=409, revision=api.rev())
    assert r["error"]["code"] == "REVISION_CONFLICT"
    assert api.get("/actions", params={"request_id": rid}).json()["data"] == []
    # replanned proposal against the current revision is valid
    p2 = worker.propose(lease, rid, "link_work", {"work_item_id": "work_export_issue", "comment_text": None},
                        ["ev_export_issue_1"], api.rev())
    assert p2["validity"] == "valid"
    worker.complete(lease, "waiting", {"proposal_ids": [p2["id"]], "awaiting": "review"})
    # work-item PATCH bumps revision and marks consulted proposals stale
    w = [x for x in api.get("/work-items", params={"limit": 100}).json()["data"] if x["id"] == "work_export_issue"][0]
    rev0 = api.rev()
    up = api.patch("/work-items/work_export_issue", {"expected_version": w["version"], "status": "in_progress",
                                                     "owner_id": "person_maya", "forecast_at": None, "reason": "Unblocked."})
    assert up.status_code == 200 and up.json()["data"]["knowledge_revision"] == rev0 + 1
    assert up.json()["data"]["work_item"]["version"] == w["version"] + 1
    assert api.get(f"/proposals/{p2['id']}").json()["data"]["validity"] == "stale"
    c = api.post(f"/proposals/{p2['id']}/commit", {"expected_revision": api.rev(), "authorization_id": None})
    assert c.status_code == 409 and c.json()["error"]["code"] == "REVISION_CONFLICT"
    stale_v = api.patch("/work-items/work_export_issue", {"expected_version": w["version"], "status": "done",
                                                          "owner_id": None, "forecast_at": None, "reason": "x"})
    assert stale_v.status_code == 409 and stale_v.json()["error"]["code"] == "REVISION_CONFLICT"


def test_dispatch_revalidation_never_sends_stale_intent(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "link_work", {"work_item_id": "work_export_migration", "comment_text": None},
                                       evidence=["ev_migration_owner_1"])
    worker.complete(lease, "waiting", {"proposal_ids": [p["id"]], "awaiting": "review"})
    action = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": p["based_on_revision"], "authorization_id": None}
                      ).json()["data"]["action"]
    w = [x for x in api.get("/work-items", params={"limit": 100}).json()["data"] if x["id"] == "work_export_migration"][0]
    api.patch("/work-items/work_export_migration", {"expected_version": w["version"], "status": "blocked", "owner_id": None,
                                                    "forecast_at": None, "reason": "Owner left."})
    lease = worker.claim(["dispatch"])
    r = worker.execute_local(lease, action, expect=409)
    assert r["error"]["code"] == "REVISION_CONFLICT"
    a = api.get(f"/actions/{action['id']}").json()["data"]
    assert a["status"] == "failed" and a["receipt"] is None and a["error"]["code"] == "REVISION_CONFLICT"
    rc = worker.complete(lease, "failed", {"action_id": action["id"], "execution_state": "failed", "receipt": None,
                                           "error": a["error"]})
    replan = store.workspaces["ws_demo"].jobs[rc["next_job_id"]]["checkpoint"]
    assert replan["resume_stage"] == "investigate" and replan["resume_reason"]  # rev2 #18
    assert rc["run"]["status"] == "queued" and rc["run"]["stage"] == "investigate" and rc["run"]["usage"]["replans"] == 1
    assert worker.claim(["investigate"])["run_id"] == run_id
    assert "work_export_migration" not in api.get(f"/requests/{rid}").json()["data"]["linked_work_ids"]


def test_unknown_outcome_reconciles(api, worker):
    req = new_request(api)
    rid = req["request"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "answer", {"draft_text": "Draft", "resolution_criteria": []})
    action = worker.commit(lease, p)["action"]
    worker.complete(lease, "succeeded", {"proposal_ids": [p["id"]], "action_ids": [action["id"]], "summary": "s"})
    api.fault({"local_create_timeout_after_effect": True})
    lease = worker.claim(["dispatch"])
    r = worker.execute_local(lease, action, expect=503)
    assert r["error"]["code"] == "DEPENDENCY_UNAVAILABLE" and r["error"]["retryable"] is True
    rc = worker.complete(lease, "unknown", {"action_id": action["id"], "execution_state": "unknown", "receipt": None,
                                            "error": {"code": "TIMEOUT", "message": "timed out"}})
    assert api.get(f"/actions/{action['id']}").json()["data"]["status"] == "unknown"
    rl = worker.claim(["investigate", "dispatch", "reconcile"])
    assert rl["kind"] == "reconcile" and rl["job_id"] == rc["next_job_id"]
    rc2 = worker.complete(rl, "succeeded", {"action_id": action["id"], "lookup_evidence": {"looked_up": "local draft store"},
                                            "established": True, "receipt": None})
    a = api.get(f"/actions/{action['id']}").json()["data"]
    assert a["status"] == "succeeded" and a["receipt"] is not None
    assert rc2["run"]["stage"] == "observe" and rc2["next_job_id"]
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_verification"
    # exactly one local effect (no blind recreation)
    assert len([d for d in store.workspaces["ws_demo"].drafts.values() if d["action_id"] == action["id"]]) == 1


def test_failed_verification_reopens(api, worker):
    rid, run_id, aid = answer_flow(api, worker)
    r = api.get(f"/requests/{rid}").json()["data"]
    o = api.post(f"/requests/{rid}/observations", {"action_id": aid, "kind": "acceptance_check", "verification": "failed",
                                                   "body": "Exports still fail.", "evidence_ids": [],
                                                   "expected_request_version": r["version"]})
    assert o.status_code == 201 and o.json()["data"]["request"]["status"] == "investigating"
    assert api.get(f"/actions/{aid}").json()["data"]["verification"] == "failed"
    assert api.get(f"/runs/{run_id}").json()["data"]["status"] == "succeeded"  # prior history preserved
    lease = worker.claim(["investigate"])
    assert lease["run_id"] != run_id
    ctx = api.get(f"/requests/{rid}/context").json()["data"]
    assert any(a["request_id"] == rid and a["outcome"] == "failed" for a in ctx["attempts"])
    # the worker cannot fabricate verification
    r = api.get(f"/requests/{rid}").json()["data"]
    w = api.post(f"/requests/{rid}/observations", {"action_id": None, "kind": "customer_confirmation", "verification": "passed",
                                                   "body": "fake", "evidence_ids": [], "expected_request_version": r["version"]},
                 token=WK, headers=worker.hdrs(lease))
    assert w.status_code == 422 and w.json()["error"]["code"] == "POLICY_VIOLATION"


def test_workspace_isolation(api, worker, monkeypatch):
    other_req = api.get("/requests", ws="ws_other", params={"limit": 100}).json()["data"][0]["id"]
    r = api.get(f"/requests/{other_req}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    demo_ids = {x["id"] for x in api.get("/requests", params={"limit": 100}).json()["data"]}
    s = api.post("/context/search", {"query": "scheduled exports", "entity_types": ["request", "capability", "work_item", "source"],
                                     "customer_id": None, "limit": 20}, ws="ws_other", key=None).json()["data"]
    assert not ({c["id"] for c in s["candidates"]} & (demo_ids | {"cap_scheduled_exports", "work_export_issue"}))
    new_request(api)
    assert api.post("/jobs/claim", {"worker_id": "w", "kinds": ["investigate"], "lease_seconds": 60}, token=WK, private=True,
                    key=None, ws="ws_other").status_code == 204
    assert api.get("/info", ws="ws_nope").json()["error"]["code"] == "NOT_FOUND"
    monkeypatch.setenv("WORKSPACE_ACL", "operator:ws_demo;worker:ws_demo")
    f = api.get("/info", ws="ws_other")
    assert f.status_code == 403 and f.json()["error"]["code"] == "WORKSPACE_FORBIDDEN"
    p = api.post("/jobs/claim", {"worker_id": "w", "kinds": ["investigate"], "lease_seconds": 60}, private=True, key=None)
    assert p.status_code == 403 and p.json()["error"]["code"] == "UNAUTHORIZED"
    u = api.get("/info", token="nope")
    assert u.status_code == 401 and u.json()["error"]["code"] == "UNAUTHORIZED"
    assert api.get("/info", token=None).status_code == 401


def test_database_unavailable_fault(api, client):
    api.fault({"database_unavailable": True})
    for resp in (api.get("/info"), api.get("/requests"), api.post("/jobs/claim", {"worker_id": "w", "kinds": ["investigate"],
                                                                                  "lease_seconds": 60}, token=WK, private=True)):
        assert resp.status_code == 503
        e = resp.json()["error"]
        assert e["code"] == "DEPENDENCY_UNAVAILABLE" and e["retryable"] is True
    assert client.get("/health/ready").status_code == 503
    assert client.get("/health/live").status_code == 200
    api.fault({"database_unavailable": False})
    assert api.get("/info").status_code == 200 and client.get("/health/ready").status_code == 200


def test_duplicate_mutation_fault_replays(api):
    api.fault({"duplicate_mutation": True})
    body = {"customer_id": "customer_acme", "subject": "Dup", "body": "dup", "channel": "manual", "capability_ids": [],
            "external_ref": None, "auto_start": False, "planner_mode": "scripted"}
    n = len(store.workspaces["ws_demo"].requests)
    lost = api.post("/requests", body, key="dup-1")
    assert lost.status_code == 503 and lost.json()["error"]["retryable"] is True
    again = api.post("/requests", body, key="dup-1")
    assert again.status_code == 201 and len(store.workspaces["ws_demo"].requests) == n + 1


def test_model_planner_disabled_is_honest(api):
    out = new_request(api, mode="model")
    assert out["run"]["status"] == "failed" and out["run"]["error"]["code"] == "CAPABILITY_DISABLED"
    info = api.get("/info").json()["data"]
    assert info["planners"]["model"] == "disabled" and info["connectors"]["linear"] == "unconfigured"
    assert info["auto_commit_kinds"] == ["answer", "clarify"]
