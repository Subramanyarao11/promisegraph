"""Lead acceptance checks (T07, T10, T12-T14, T19-T21, T26, T28) and joint A2 behaviour on real FalkorDB."""

from __future__ import annotations

import json
import time
import uuid

from fastapi.testclient import TestClient

from conftest import OP, SEED, Api, check, cp, lease_body


def intake(api: Api, customer="cust_acme", caps=("cap_exports",), subject="Exports stopped", auto=True,
           mode="scripted") -> tuple[dict, dict | None]:
    d = api.post("/requests", {"customer_id": customer, "subject": subject, "body": f"{subject}. Please help.",
                               "channel": "manual", "capability_ids": list(caps), "external_ref": None,
                               "auto_start": auto, "planner_mode": mode}, "RequestCreateResult", 201)["data"]
    return d["request"], d["run"]


def propose(api: Api, lease: dict, req: dict, run_id: str, kind: str, payload: dict, ev: list[str] | None = None,
            rev: int | None = None) -> dict:
    ww = api.worker_with(lease)
    if ev is None:
        ev = [s["id"] for s in api.get(f"/requests/{req['id']}/context", "ContextBundle")["data"]["sources"]
              if not s["stale"]][:3]
    return ww.post("/proposals", {"request_id": req["id"], "run_id": run_id, "kind": kind, "payload": payload,
                                  "evidence_ids": ev, "explanation": "test", "rejected_alternatives": [],
                                  "based_on_revision": api.revision() if rev is None else rev}, "Proposal", 201)["data"]


def to_review(api: Api, kind: str, payload: dict, **kw) -> tuple[dict, dict, dict]:
    """Intake + investigate job that prepares a proposal needing operator review (job completes `waiting`)."""
    req, run = intake(api, **kw)
    lease = api.claim(["investigate"])
    prop = propose(api, lease, req, run["id"], kind, payload)
    api.as_worker().post(f"/jobs/{lease['job_id']}/complete",
                         lease_body(lease, completion_id="w", outcome="waiting",
                                    result={"proposal_ids": [prop["id"]], "awaiting": "review"}),
                         "CompletionReceipt", private=True)
    return req, run, prop


def dispatch_local(api: Api) -> tuple[dict, dict]:
    dl = api.claim(["dispatch"])
    r = api.c.post(api.as_worker().url(f"/actions/{dl['action_id']}/execute-local", True),
                   headers={"Authorization": "Bearer test-worker-token", "Idempotency-Key": uuid.uuid4().hex},
                   json={"job_id": dl["job_id"], **lease_body(dl), "expected_action_version": 1})
    return dl, r


LINK = {"work_item_id": "work_export_issue", "comment_text": None}


def test_four_hop_path_T07(seeded: Api):
    req, _ = intake(seeded, auto=False)
    b = seeded.get(f"/requests/{req['id']}/context", "ContextBundle")["data"]
    four = [p for p in b["paths"] if len(p["edges"]) == 4]
    assert four, [len(p["edges"]) for p in b["paths"]]
    p = next(p for p in four if [n["id"] for n in p["nodes"]][-2:] == ["work_migration", "person_li"])
    assert [e["type"] for e in p["edges"]] == ["ABOUT", "IMPLEMENTED_BY", "DEPENDS_ON", "OWNED_BY"]
    for a, e, bnode in zip(p["nodes"], p["edges"], p["nodes"][1:]):  # contiguous, real directions
        assert {e["from_id"], e["to_id"]} == {a["id"], bnode["id"]} and e["evidence_ids"]
    assert p["claim_kind"] == "source_assertion"
    for ev in p["evidence_ids"]:
        seeded.get(f"/evidence/{ev}")
    # depth bound: depth 3 cannot reach the owner of the dependency; depth 7 rejected
    b3 = seeded.get(f"/requests/{req['id']}/context", "ContextBundle", params={"depth": 3})["data"]
    assert not any(n["id"] == "person_li" for p in b3["paths"] for n in p["nodes"])
    seeded.get(f"/requests/{req['id']}/context", params={"depth": 7}, expect=422)
    seeded.post("/context/expand", {"entity_ids": [req["id"]], "relation_types": ["ABOUT"], "depth": 7, "limit": 5},
                expect=422, key=None)
    ex = seeded.post("/context/expand", {"entity_ids": [req["id"]], "relation_types": ["ABOUT", "IMPLEMENTED_BY",
                                                                                         "DEPENDS_ON", "OWNED_BY"],
                                         "depth": 4, "limit": 1}, "ContextExpandResult", key=None)["data"]
    assert ex["truncated"] is True and len(ex["paths"]) == 1
    seeded.post("/context/expand", {"entity_ids": ["nope"], "relation_types": ["ABOUT"], "depth": 1, "limit": 5},
                expect=404, key=None)


def test_decisions_supersede_revoke_expiry_T10(seeded: Api):
    api = seeded
    # forbidden workaround (customer-scoped) invalidates an answer that uses it for Acme, not for Globex (T09 scope)
    req, run = intake(api)
    lease = api.claim(["investigate"])
    bad = propose(api, lease, req, run["id"], "answer",
                  {"draft_text": "Use a manual_public_export_link for now.", "resolution_criteria": []})
    assert bad["validity"] == "invalid" and any("forbids" in r for r in bad["validation_reasons"])
    b = api.get(f"/requests/{req['id']}/context", "ContextBundle")["data"]
    assert [d["id"] for d in b["decisions"]] == ["dec_no_public_links"]
    assert not any(a["id"] == "att_retry_globex" for a in b["attempts"])  # other customer's failed attempt
    g_req, _ = intake(api, customer="cust_globex", auto=False)
    gb = api.get(f"/requests/{g_req['id']}/context", "ContextBundle")["data"]
    assert gb["decisions"] == [] and any(a["id"] == "att_retry_globex" for a in gb["attempts"])
    # record allowed_action_types, then supersede it -> next plan changes, history kept
    rev = api.revision()
    d1 = api.post("/decisions", {"operation": "record", "decision_id": None, "rule_type": "allowed_action_types",
                                 "value": ["escalate"], "scope": {"entity_type": "customer", "entity_id": "cust_acme"},
                                 "rationale": "Acme requests go to review", "effective_at": "2026-10-01T00:00:00Z",
                                 "expires_at": None, "evidence_ids": [], "expected_revision": rev},
                  "Decision", 201)["data"]
    p1 = propose(api, lease, req, run["id"], "answer", {"draft_text": "ok", "resolution_criteria": []})
    assert p1["validity"] == "invalid" and any(d1["id"] in r for r in p1["validation_reasons"])
    d2 = api.post("/decisions", {"operation": "supersede", "decision_id": d1["id"], "rule_type": "allowed_action_types",
                                 "value": ["answer", "escalate"], "scope": {"entity_type": "customer",
                                                                            "entity_id": "cust_acme"},
                                 "rationale": "Answers allowed again", "effective_at": "2026-10-01T00:00:00Z",
                                 "expires_at": None, "evidence_ids": [], "expected_revision": api.revision()},
                  "Decision", 201)["data"]
    assert d2["supersedes_id"] == d1["id"]
    p2 = propose(api, lease, req, run["id"], "answer", {"draft_text": "ok", "resolution_criteria": []})
    assert p2["validity"] == "valid", p2["validation_reasons"]
    hist = api.get("/decisions", list_of="Decision", params={"scope_entity_type": "customer",
                                                             "scope_entity_id": "cust_acme"})["data"]
    st = {d["id"]: d["status"] for d in hist}
    assert st[d1["id"]] == "superseded" and st[d2["id"]] == "active"
    act = api.get("/decisions", list_of="Decision", params={"active": "true"})["data"]
    assert d1["id"] not in [d["id"] for d in act]
    # revoke
    rv = api.post("/decisions", {"operation": "revoke", "decision_id": d2["id"], "rule_type": "allowed_action_types",
                                 "value": [], "scope": {"entity_type": "customer", "entity_id": "cust_acme"},
                                 "rationale": "lifted", "effective_at": "2026-10-01T00:00:00Z", "expires_at": None,
                                 "evidence_ids": [], "expected_revision": api.revision()}, "Decision", 200)["data"]
    assert rv["status"] == "revoked"
    api.post("/decisions", {"operation": "revoke", "decision_id": d2["id"], "rule_type": "allowed_action_types",
                            "value": [], "scope": {"entity_type": "customer", "entity_id": "cust_acme"},
                            "rationale": "again", "effective_at": "2026-10-01T00:00:00Z", "expires_at": None,
                            "evidence_ids": [], "expected_revision": api.revision()}, expect=409)
    # expiry relative to the scenario clock (2026-10-16T04:30Z)
    d3 = api.post("/decisions", {"operation": "record", "decision_id": None, "rule_type": "allowed_action_types",
                                 "value": ["escalate"], "scope": {"entity_type": "request", "entity_id": req["id"]},
                                 "rationale": "temporary freeze", "effective_at": "2026-10-01T00:00:00Z",
                                 "expires_at": "2026-10-10T00:00:00Z", "evidence_ids": [],
                                 "expected_revision": api.revision()}, "Decision", 201)["data"]
    got = api.get("/decisions", list_of="Decision", params={"scope_entity_type": "request",
                                                            "scope_entity_id": req["id"]})["data"]
    assert got[0]["id"] == d3["id"] and got[0]["status"] == "expired"
    p3 = propose(api, lease, req, run["id"], "answer", {"draft_text": "ok", "resolution_criteria": []})
    assert p3["validity"] == "valid"
    # wrong value type and stale expected_revision
    api.post("/decisions", {"operation": "record", "decision_id": None, "rule_type": "date_extension_allowed",
                            "value": "yes", "scope": {"entity_type": "customer", "entity_id": "cust_acme"},
                            "rationale": "x", "effective_at": "2026-10-01T00:00:00Z", "expires_at": None,
                            "evidence_ids": [], "expected_revision": api.revision()}, expect=422)
    api.post("/decisions", {"operation": "record", "decision_id": None, "rule_type": "date_extension_allowed",
                            "value": True, "scope": {"entity_type": "customer", "entity_id": "cust_acme"},
                            "rationale": "x", "effective_at": "2026-10-01T00:00:00Z", "expires_at": None,
                            "evidence_ids": [], "expected_revision": 0}, expect=409)


def test_conflicting_decisions_force_clarify_T11(seeded: Api):
    api = seeded
    for v in ("person_maya", "person_li"):
        api.post("/decisions", {"operation": "record", "decision_id": None, "rule_type": "routing_owner", "value": v,
                                "scope": {"entity_type": "capability", "entity_id": "cap_exports"}, "rationale": "r",
                                "effective_at": "2026-10-01T00:00:00Z", "expires_at": None, "evidence_ids": [],
                                "expected_revision": api.revision()}, "Decision", 201)
    req, run = intake(api)
    lease = api.claim(["investigate"])
    p = propose(api, lease, req, run["id"], "create_work",
                {"title": "t", "description": "d", "acceptance_criteria": ["a"], "owner_id": "person_maya",
                 "capability_id": "cap_exports", "dependency_ids": [], "connector": "local"})
    assert p["validity"] == "invalid" and any("conflicting" in r for r in p["validation_reasons"])
    c = propose(api, lease, req, run["id"], "clarify", {"question": "Who owns this?", "missing_fields": ["owner"]})
    assert c["validity"] == "valid"
    b = api.get(f"/requests/{req['id']}/context", "ContextBundle")["data"]
    assert any("Conflicting active decisions" in m for m in b["missing_information"])


def test_changed_owner_invalidates_and_blocks_stale_execution_T12(seeded: Api):
    api = seeded
    req, run, prop = to_review(api, "link_work", LINK)
    assert prop["validity"] == "valid"
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "awaiting_review"
    w = api.get("/work-items/work_export_issue", "WorkItem")["data"]
    upd = api.patch("/work-items/work_export_issue", {"expected_version": w["version"], "status": "blocked",
                                                      "owner_id": "person_li", "forecast_at": None,
                                                      "reason": "ownership moved"}, "WorkItemUpdateResult")["data"]
    assert upd["work_item"]["owner_id"] == "person_li" and upd["work_item"]["version"] == w["version"] + 1
    p = api.get(f"/proposals/{prop['id']}", "Proposal")["data"]
    assert p["validity"] == "stale"
    err = api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
                   expect=409)
    assert err["error"]["code"] == "REVISION_CONFLICT"
    # second variant: committed (accepted) action, then knowledge changes before dispatch -> blocked at execute-local
    req2, run2, prop2 = to_review(api, "link_work", {"work_item_id": "work_migration", "comment_text": None})
    res = api.post(f"/proposals/{prop2['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
                   "ProposalCommitResult", 202)["data"]
    assert api.get(f"/requests/{req2['id']}", "Request")["data"]["status"] == "awaiting_review"  # rev2 #14
    run_q = api.get(f"/runs/{run2['id']}", "Run")["data"]
    assert run_q["status"] == "queued" and run_q["stage"] == "act"
    w2 = api.get("/work-items/work_migration", "WorkItem")["data"]
    api.patch("/work-items/work_migration", {"expected_version": w2["version"], "status": "in_progress",
                                             "owner_id": "person_maya", "forecast_at": None, "reason": "handover"},
              "WorkItemUpdateResult")
    dl, r = dispatch_local(api)
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    check("ErrorEnvelope", r.json())
    a = api.get(f"/actions/{res['action']['id']}", "Action")["data"]
    assert a["status"] == "failed" and a["error"]["code"] == "REVISION_CONFLICT" and a["receipt"] is None
    assert api.get(f"/proposals/{prop2['id']}", "Proposal")["data"]["validity"] == "stale"
    assert "work_migration" not in api.get(f"/requests/{req2['id']}", "Request")["data"]["linked_work_ids"]
    comp = api.as_worker().post(f"/jobs/{dl['job_id']}/complete",
                                lease_body(dl, completion_id="f", outcome="failed",
                                           result={"action_id": a["id"], "execution_state": "failed", "receipt": None,
                                                   "error": a["error"]}), "CompletionReceipt", private=True)["data"]
    assert comp["run"]["status"] == "queued" and comp["run"]["usage"]["replans"] == 1 and comp["next_job_id"]
    replan = api.claim(["investigate"])
    assert replan["checkpoint"]["resume_stage"] == "investigate" and a["id"] in replan["checkpoint"]["resume_reason"]


def test_idempotent_replay_before_revision_check_T13(seeded: Api):
    api = seeded
    req, run, prop = to_review(api, "link_work", LINK)
    body = {"expected_revision": api.revision(), "authorization_id": None}
    first = api.post(f"/proposals/{prop['id']}/commit", body, "ProposalCommitResult", 202, key="commit-1")
    # knowledge changes after the commit; a retried (lost-response) commit still replays the original result
    api.import_doc("json", json.dumps({"workspace_id": "ws_demo", "observed_at": "2026-10-15T09:00:00Z",
                                       "teams": [{"id": "team_new", "name": "New"}]}), "t")
    again = api.post(f"/proposals/{prop['id']}/commit", body, "ProposalCommitResult", 202, key="commit-1")
    assert again == first
    # same key/body on intake replays the same resource; no duplicate request
    rb = {"customer_id": "cust_acme", "subject": "Dup?", "body": "same", "channel": "manual", "capability_ids": [],
          "external_ref": None, "auto_start": False, "planner_mode": "scripted"}
    a = api.post("/requests", rb, "RequestCreateResult", 201, key="intake-1")["data"]
    b = api.post("/requests", rb, "RequestCreateResult", 201, key="intake-1")["data"]
    assert a["request"]["id"] == b["request"]["id"]
    # missing key -> 422
    api.post("/requests", rb, expect=422, key=None)


def test_conflicting_key_and_no_duplicate_link_T14(seeded: Api):
    api = seeded
    rb = {"customer_id": "cust_acme", "subject": "One", "body": "b", "channel": "manual", "capability_ids": [],
          "external_ref": None, "auto_start": False, "planner_mode": "scripted"}
    api.post("/requests", rb, "RequestCreateResult", 201, key="k-dup")
    e = api.post("/requests", {**rb, "subject": "Two"}, expect=409, key="k-dup")
    assert e["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # two link proposals prepared at the same revision; the first executes, the second is rejected, one edge only
    req, run, prop = to_review(api, "link_work", LINK)
    api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
             "ProposalCommitResult", 202)
    dl, r = dispatch_local(api)
    assert r.status_code == 200, r.text
    act = r.json()["data"]
    check("Action", act)
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "awaiting_work"  # linked work is blocked
    st = api.c.app.state.store
    assert st.edge_count("ws_demo", req["id"], "TRACKED_BY", "work_export_issue") == 1
    # re-executing (lost response, new key) returns the same action/receipt and does not add an edge
    r2 = api.c.post(api.as_worker().url(f"/actions/{act['id']}/execute-local", True),
                    headers={"Authorization": "Bearer test-worker-token", "Idempotency-Key": "other"},
                    json={"job_id": dl["job_id"], **lease_body(dl), "expected_action_version": 1})
    assert r2.status_code == 200 and r2.json()["data"]["receipt"] == act["receipt"]
    assert st.edge_count("ws_demo", req["id"], "TRACKED_BY", "work_export_issue") == 1
    api.as_worker().post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="d", outcome="succeeded",
                         result={"action_id": act["id"], "execution_state": "succeeded", "receipt": act["receipt"],
                                 "error": None}), "CompletionReceipt", private=True)
    cont = api.claim(["investigate"])
    p2 = propose(api, cont, req, run["id"], "link_work", LINK)
    assert p2["validity"] == "invalid" and any("already linked" in r for r in p2["validation_reasons"])
    assert st.edge_count("ws_demo", req["id"], "TRACKED_BY", "work_export_issue") == 1


def test_clarify_wait_reply_resume_T02_T03(seeded: Api):
    api = seeded
    req, run = intake(api, caps=())
    lease = api.claim(["investigate"])
    p = propose(api, lease, req, run["id"], "clarify", {"question": "Which export?", "missing_fields": ["export_id"]})
    ww = api.worker_with(lease)
    ww.post(f"/proposals/{p['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
            "ProposalCommitResult", 202)
    api.as_worker().post(f"/jobs/{lease['job_id']}/complete", lease_body(lease, completion_id="1", outcome="succeeded",
                         result={"proposal_ids": [p["id"]]}), "CompletionReceipt", private=True)
    dl, r = dispatch_local(api)
    assert r.status_code == 200
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "awaiting_input"
    api.as_worker().post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="2", outcome="succeeded",
                         result={"action_id": r.json()["data"]["id"], "execution_state": "succeeded",
                                 "receipt": r.json()["data"]["receipt"], "error": None}), "CompletionReceipt",
                         private=True)
    cont = api.claim(["investigate"])
    api.as_worker().post(f"/jobs/{cont['job_id']}/complete", lease_body(cont, completion_id="3", outcome="succeeded",
                         result={"proposal_ids": []}), "CompletionReceipt", private=True)
    run_w = api.get(f"/runs/{run['id']}", "Run")["data"]
    assert run_w["status"] == "waiting_for_input"
    evs = api.get(f"/runs/{run['id']}/events", "EventsPage")["data"]
    assert evs[-1]["type"] == "waiting_for_input" and evs[-1]["payload"]["question"] == "Which export?"
    assert api.claim() is None  # nothing busy-loops while waiting
    rq = api.get(f"/requests/{req['id']}", "Request")["data"]
    rep = api.post(f"/requests/{req['id']}/replies", {"body": "The nightly finance export.", "external_ref": None,
                                                     "expected_request_version": rq["version"]}, "ReplyResult",
                   201)["data"]
    assert rep["run"]["id"] == run["id"] and rep["run"]["status"] == "queued"
    nxt = api.claim(["investigate"])
    assert nxt["run_id"] == run["id"] and nxt["checkpoint"]["reply_source_id"] == rep["source_id"]
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "investigating"
    assert len(api.get(f"/runs/{run['id']}/events", "EventsPage")["data"]) == len(evs)  # history retained
    src = api.get(f"/sources/{rep['source_id']}", "SourceRecord")["data"]
    assert src["body"] == "The nightly finance export."


def test_lease_expiry_and_fencing_T16(seeded: Api):
    api = seeded
    req, run = intake(api)
    old = api.claim(["investigate"], worker_id="w1")
    st = api.c.app.state.store
    st.write("ws_demo", "MATCH (j:Job {id: $id}) SET j.expires_epoch = 0.0", {"id": old["job_id"]})  # simulate expiry
    new = api.claim(["investigate"], worker_id="w2")
    assert new["job_id"] == old["job_id"] and new["fencing_token"] == old["fencing_token"] + 1
    assert new["attempt"] == old["attempt"] + 1
    w = api.as_worker()
    e = w.post(f"/jobs/{old['job_id']}/checkpoint", lease_body(old, step_id="x", expected_event_seq=0,
               event={"type": "run_started", "tool_name": None, "payload": {}}, checkpoint=cp()), expect=409,
               private=True)
    assert e["error"]["code"] == "LEASE_LOST"
    e = w.post(f"/jobs/{old['job_id']}/heartbeat", lease_body(old), expect=409, private=True, key=None)
    assert e["error"]["code"] == "LEASE_LOST"
    e = api.worker_with(old).post("/proposals", {"request_id": req["id"], "run_id": run["id"], "kind": "clarify",
                                                 "payload": {"question": "q", "missing_fields": []}, "evidence_ids": [],
                                                 "explanation": "x", "rejected_alternatives": [],
                                                 "based_on_revision": api.revision()}, expect=409)
    assert e["error"]["code"] == "LEASE_LOST"
    hb = w.post(f"/jobs/{new['job_id']}/heartbeat", lease_body(new), "JobLease", private=True, key=None)["data"]
    assert hb["fencing_token"] == new["fencing_token"]
    ck = w.post(f"/jobs/{new['job_id']}/checkpoint", lease_body(new, step_id="s1", expected_event_seq=0,
                event={"type": "run_started", "tool_name": None, "payload": {}}, checkpoint=cp()), "CheckpointResult",
                private=True)["data"]
    # duplicate step id after a lost response returns the original event, not a second append
    ck2 = w.post(f"/jobs/{new['job_id']}/checkpoint", lease_body(new, step_id="s1", expected_event_seq=1,
                 event={"type": "run_started", "tool_name": None, "payload": {}}, checkpoint=cp()),
                 "CheckpointResult", private=True)["data"]
    assert ck2["event"]["id"] == ck["event"]["id"]
    e = w.post(f"/jobs/{new['job_id']}/checkpoint", lease_body(new, step_id="s2", expected_event_seq=0,
               event={"type": "tool_started", "tool_name": "search_context", "payload": {}}, checkpoint=cp()),
               expect=409, private=True)
    assert e["error"]["code"] == "REVISION_CONFLICT"
    e = w.post(f"/jobs/{new['job_id']}/checkpoint", lease_body(new, step_id="s3", expected_event_seq=1,
               event={"type": "run_finished", "tool_name": None, "payload": {}}, checkpoint=cp()),
               expect=422, private=True)  # rev2 #17: API-owned event type
    # operator token cannot use private routes; wrong token 401
    api.post("/jobs/claim", {"worker_id": "x", "kinds": ["investigate"], "lease_seconds": 60}, expect=403,
             private=True, key=None)
    r = api.c.get(api.url("/requests"), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_verification_failure_reopens_and_connector_status_never_resolves_T17_T18(seeded: Api):
    api = seeded
    req, run, prop = to_review(api, "escalate", {"reason": "needs eng", "owner_id": "person_maya", "team_id": None})
    res = api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
                   "ProposalCommitResult", 202)["data"]
    dl, r = dispatch_local(api)
    assert r.status_code == 200
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "escalated"
    api.as_worker().post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="1", outcome="succeeded",
                         result={"action_id": res["action"]["id"], "execution_state": "succeeded",
                                 "receipt": r.json()["data"]["receipt"], "error": None}), "CompletionReceipt",
                         private=True)
    cont = api.claim(["investigate"])
    wq = api.get(f"/requests/{req['id']}", "Request")["data"]
    # worker may only record connector_status; it never resolves
    e = api.worker_with(cont).post(f"/requests/{req['id']}/observations",
                                   {"action_id": None, "kind": "acceptance_check", "verification": "passed",
                                    "body": "looks done", "evidence_ids": [], "expected_request_version": wq["version"]},
                                   expect=422)
    assert e["error"]["code"] == "POLICY_VIOLATION"
    o = api.worker_with(cont).post(f"/requests/{req['id']}/observations",
                                   {"action_id": res["action"]["id"], "kind": "connector_status", "verification": "passed",
                                    "body": "case closed", "evidence_ids": [], "expected_request_version": wq["version"]},
                                   "ObservationResult", 201)["data"]
    assert o["request"]["status"] == "escalated" and o["observation"]["actor_id"] == "worker:worker_test"
    api.as_worker().post(f"/jobs/{cont['job_id']}/complete", lease_body(cont, completion_id="2", outcome="succeeded",
                         result={"proposal_ids": []}), "CompletionReceipt", private=True)
    rq = api.get(f"/requests/{req['id']}", "Request")["data"]
    rev = api.revision()
    f = api.post(f"/requests/{req['id']}/observations",
                 {"action_id": res["action"]["id"], "kind": "customer_confirmation", "verification": "failed",
                  "body": "Customer says still broken", "evidence_ids": [], "expected_request_version": rq["version"]},
                 "ObservationResult", 201)["data"]
    assert f["request"]["status"] == "investigating" and api.revision() == rev + 1
    att = api.get("/attempts", list_of="Attempt", params={"request_id": req["id"]})["data"]
    assert att[0]["outcome"] == "failed" and att[0]["verification"] == "failed"
    runs = api.get(f"/requests/{req['id']}/runs", list_of="Run")["data"]
    assert len(runs) == 2 and runs[0]["status"] == "queued" and runs[1]["id"] == run["id"]
    # stale expected version
    api.post(f"/requests/{req['id']}/observations", {"action_id": None, "kind": "operator_confirmation",
                                                     "verification": "passed", "body": "x", "evidence_ids": [],
                                                     "expected_request_version": rq["version"]}, expect=409)


def test_unknown_outcome_reconcile_and_linear_permit(app_factory):
    api = Api(TestClient(app_factory(linear_enabled=True, linear_configured=True)))
    api.import_doc("json", json.dumps(SEED), "seed")
    payload = {"title": "Export retries", "description": "d", "acceptance_criteria": ["exports run"],
               "owner_id": None, "capability_id": "cap_exports", "dependency_ids": [], "connector": "linear"}
    req, run, prop = to_review(api, "create_work", payload)
    assert prop["validity"] == "valid", prop
    res = api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
                   "ProposalCommitResult", 202)["data"]
    assert res["action"]["connector"] == "linear"
    dl = api.claim(["dispatch"])
    w = api.as_worker()
    w.post(f"/actions/{dl['action_id']}/execute-local", {"job_id": dl["job_id"], **lease_body(dl),
                                                         "expected_action_version": 1}, expect=422, private=True)
    permit = w.post(f"/actions/{dl['action_id']}/begin-dispatch",
                    {"job_id": dl["job_id"], **lease_body(dl), "expected_action_version": 1,
                     "expected_revision": api.revision(), "remote_preconditions": []}, "DispatchPermit",
                    private=True)["data"]
    assert permit["action_version"] == 2
    assert api.get(f"/actions/{dl['action_id']}", "Action")["data"]["status"] == "dispatching"
    c = w.post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="u", outcome="unknown",
               result={"action_id": dl["action_id"], "execution_state": "unknown", "receipt": None, "error": None}),
               "CompletionReceipt", private=True)["data"]
    assert api.get(f"/actions/{dl['action_id']}", "Action")["data"]["status"] == "unknown"
    rc = api.claim()
    assert rc["kind"] == "reconcile" and rc["job_id"] == c["next_job_id"]
    receipt = {"id": "rcpt_lin_1", "connector": "linear", "target_id": "LIN-42",
               "target_url": "https://linear.app/x/issue/LIN-42", "observed_at": "2026-10-16T05:00:00Z",
               "result_hash": "a" * 64}
    c2 = w.post(f"/jobs/{rc['job_id']}/complete", lease_body(rc, completion_id="r", outcome="succeeded",
                result={"action_id": dl["action_id"], "lookup_evidence": "found by marker", "established": True,
                        "receipt": receipt}), "CompletionReceipt", private=True)["data"]
    a = api.get(f"/actions/{dl['action_id']}", "Action")["data"]
    assert a["status"] == "succeeded" and a["receipt"]["target_id"] == "LIN-42"
    rq = api.get(f"/requests/{req['id']}", "Request")["data"]
    assert rq["status"] == "awaiting_work" and len(rq["linked_work_ids"]) == 1
    wi = api.get(f"/work-items/{rq['linked_work_ids'][0]}", "WorkItem")["data"]
    assert wi["connector"] == "linear" and wi["external_id"] == "LIN-42"
    api.patch(f"/work-items/{wi['id']}", {"expected_version": 1, "status": "done", "owner_id": None,
                                          "forecast_at": None, "reason": "x"}, expect=422)  # provider-owned
    assert api.claim()["job_id"] == c2["next_job_id"]


def test_reconcile_still_unknown_goes_to_operator(seeded: Api):
    api = seeded
    req, run, prop = to_review(api, "link_work", LINK)
    api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
             "ProposalCommitResult", 202)
    dl = api.claim(["dispatch"])
    api.as_worker().post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="u", outcome="unknown",
                         result={"action_id": dl["action_id"], "execution_state": "unknown", "receipt": None,
                                 "error": None}), "CompletionReceipt", private=True)
    rc = api.claim(["reconcile"])
    c = api.as_worker().post(f"/jobs/{rc['job_id']}/complete", lease_body(rc, completion_id="r", outcome="succeeded",
                             result={"action_id": dl["action_id"], "lookup_evidence": "inconclusive",
                                     "established": False, "receipt": None}), "CompletionReceipt",
                             private=True)["data"]
    assert c["run"]["status"] == "awaiting_review" and c["next_job_id"] is None
    assert api.get(f"/actions/{dl['action_id']}", "Action")["data"]["status"] == "unknown"
    assert api.claim() is None  # no blind redispatch


def test_create_work_receipt_and_fault_replay_T06(app_factory):
    api = Api(TestClient(app_factory(faults={"local_create_timeout_after_effect"})))
    api.import_doc("json", json.dumps(SEED), "seed")
    req, run, prop = to_review(api, "create_work", {"title": "SSO audit export", "description": "new",
                                                    "acceptance_criteria": ["Audit rows exported"],
                                                    "owner_id": "person_maya", "capability_id": "cap_sso",
                                                    "dependency_ids": [], "connector": "local"}, caps=("cap_sso",))
    api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
             "ProposalCommitResult", 202)
    dl = api.claim(["dispatch"])
    h = {"Authorization": "Bearer test-worker-token", "Idempotency-Key": "exec-1"}
    url = api.as_worker().url(f"/actions/{dl['action_id']}/execute-local", True)
    body = {"job_id": dl["job_id"], **lease_body(dl), "expected_action_version": 1}
    r1 = api.c.post(url, headers=h, json=body)
    assert r1.status_code == 503 and r1.json()["error"]["retryable"] is True
    r2 = api.c.post(url, headers=h, json=body)  # retry replays the recorded success; no second task
    assert r2.status_code == 200 and r2.headers.get("Idempotent-Replay") == "true"
    a = r2.json()["data"]
    check("Action", a)
    wi = api.get(f"/work-items/{a['receipt']['target_id']}", "WorkItem")["data"]
    assert wi["status"] == "open" and wi["owner_id"] == "person_maya"
    assert len(api.get(f"/requests/{req['id']}", "Request")["data"]["linked_work_ids"]) == 1
    src = api.get(f"/sources/src_work_item_{wi['id']}", "SourceRecord")["data"]
    assert "Audit rows exported" in src["body"]


def test_restart_durability_T19(app_factory, prefix):
    p = f"{prefix}durable_"
    app1 = app_factory(prefix=p)
    api = Api(TestClient(app1))
    api.import_doc("json", json.dumps(SEED), "seed")
    req, run, prop = to_review(api, "escalate", {"reason": "r", "owner_id": None, "team_id": "team_data"})
    res = api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
                   "ProposalCommitResult", 202)["data"]
    dl, r = dispatch_local(api)
    rev = api.revision()
    # a brand-new API process (fresh Store, no caches) sees identical durable state
    api2 = Api(TestClient(app_factory(prefix=p)))
    assert api2.revision() == rev
    assert api2.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "escalated"
    a = api2.get(f"/actions/{res['action']['id']}", "Action")["data"]
    assert a["status"] == "succeeded" and a["receipt"] == r.json()["data"]["receipt"]
    assert api2.get(f"/runs/{run['id']}", "Run")["data"]["id"] == run["id"]
    assert api2.get("/decisions", list_of="Decision")["data"]
    assert api2.get("/attempts", list_of="Attempt")["data"]
    # the in-flight lease is still honoured after the restart
    api2.as_worker().post(f"/jobs/{dl['job_id']}/heartbeat", lease_body(dl), "JobLease", private=True, key=None)


def test_workspace_isolation_T20(seeded: Api):
    api = seeded
    req, _ = intake(api, auto=False)
    other = api.in_ws("ws_other")
    other.get(f"/requests/{req['id']}", expect=404)
    assert other.get("/customers", list_of="Customer")["data"] == []
    assert other.post("/context/search", {"query": "exports", "entity_types": ["request", "capability"],
                                          "customer_id": None, "limit": 10}, "ContextSearchResult",
                      key=None)["data"]["candidates"] == []
    other.post("/context/expand", {"entity_ids": [req["id"]], "relation_types": ["ABOUT"], "depth": 1, "limit": 5},
               expect=404, key=None)
    other.post("/requests", {"customer_id": "cust_acme", "subject": "x", "body": "x", "channel": "manual",
                             "capability_ids": [], "external_ref": None, "auto_start": False,
                             "planner_mode": "scripted"}, expect=422)
    api.in_ws("ws_unknown").get("/requests", expect=404)
    st = api.c.app.state.store
    assert st.graph_name("ws_demo") != st.graph_name("ws_other")
    # ACL: operator not permitted in ws_other -> 403 WORKSPACE_FORBIDDEN
    restricted = api.c.app  # reuse settings object of a new app
    from promisegraph_api.app import create_app
    from conftest import make_settings
    s = make_settings(st.settings.graph_prefix)
    s.acl = {"operator": {"ws_demo"}, "worker": {"ws_demo"}}
    c2 = TestClient(create_app(s))
    r = c2.get("/api/v1/workspaces/ws_other/requests", headers={"Authorization": f"Bearer {OP}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "WORKSPACE_FORBIDDEN"
    assert restricted is not None


def test_database_down_is_unavailable_T21(app_factory):
    app = app_factory(db_port=6399)  # nothing listens here
    c = TestClient(app)
    r = c.get("/api/v1/workspaces/ws_demo/requests", headers={"Authorization": f"Bearer {OP}"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    check("ErrorEnvelope", r.json())
    assert r.json()["error"]["retryable"] is True
    r = c.post("/api/v1/workspaces/ws_demo/context/search", headers={"Authorization": f"Bearer {OP}"},
               json={"query": "exports", "entity_types": ["capability"], "customer_id": None, "limit": 5})
    assert r.status_code == 503
    assert c.get("/health/ready").status_code == 503
    assert c.get("/health/live").status_code == 200


def test_candidates_never_become_confirmed_T28(seeded: Api):
    api = seeded
    b = api.get("/requests/req_similar/context", "ContextBundle")["data"]
    rel = [p for p in b["paths"] if any(e["type"] == "RELATED_TO" for e in p["edges"])]
    assert rel and all(p["claim_kind"] == "inference" for p in rel)
    assert all(not e["confirmed"] for p in rel for e in p["edges"] if e["type"] == "RELATED_TO")
    before = api.get("/requests/req_similar", "Request")["data"]
    api.post("/context/search", {"query": "export schedule wrong", "entity_types": ["request"], "customer_id": None,
                                 "limit": 5}, "ContextSearchResult", key=None)
    after = api.get("/requests/req_similar", "Request")["data"]
    assert after == before and after["linked_work_ids"] == []  # similarity created no link/identity
    st = api.c.app.state.store
    assert st.edge_count("ws_demo", "req_similar", "TRACKED_BY", "work_export_issue") == 0


def test_refresh_coalesces_and_model_mode_disabled(seeded: Api):
    api = seeded
    req, _ = intake(api, auto=False)
    a = api.post(f"/requests/{req['id']}/refresh", {"reason": "check work"}, "RefreshResult", 202)["data"]
    b = api.post(f"/requests/{req['id']}/refresh", {"reason": "again"}, "RefreshResult", 202)["data"]
    assert b["job_id"] == a["job_id"] and b["coalesced"] is True and a["coalesced"] is False
    lease = api.claim(["refresh"])
    assert lease["run_id"] is None and lease["checkpoint"]["request_id"] == req["id"]
    api.as_worker().post(f"/jobs/{lease['job_id']}/complete", lease_body(lease, completion_id="x", outcome="succeeded",
                         result={"work_item_ids": [], "observation_ids": [], "affected_request_ids": [req["id"]]}),
                         "CompletionReceipt", private=True)
    run = api.post(f"/requests/{req['id']}/runs", {"mode": "model", "instruction": "", "authorization_id": None},
                   "Run", 202)["data"]
    assert run["status"] == "failed" and run["error"]["code"] == "CAPABILITY_DISABLED"
    assert api.claim(["investigate"]) is None
    api.post(f"/requests/{req['id']}/runs", {"mode": "scripted", "instruction": "", "authorization_id": "nope"},
             expect=422)


def test_cancel_and_resume(seeded: Api):
    api = seeded
    req, run = intake(api)
    lease = api.claim(["investigate"])
    api.as_worker().post(f"/jobs/{lease['job_id']}/complete", lease_body(lease, completion_id="f", outcome="failed",
                         result={"proposal_ids": [], "error": {"code": "BUDGET_EXHAUSTED", "message": "12 calls"}}),
                         "CompletionReceipt", private=True)
    r = api.get(f"/runs/{run['id']}", "Run")["data"]
    assert r["status"] == "failed" and r["error"]["code"] == "BUDGET_EXHAUSTED"
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "investigating"
    res = api.post(f"/runs/{run['id']}/resume", {"expected_run_version": r["version"], "reason": "extend",
                                                 "additional_tool_calls": 12, "additional_active_seconds": 90},
                   "Run", 202)["data"]
    assert res["budget"]["max_tool_calls"] == 24 and res["budget"]["extensions_used"] == 1
    lease = api.claim(["investigate"])
    api.post(f"/runs/{run['id']}/cancel", {"reason": "stop"}, "Run", 200)
    e = api.worker_with(lease).post("/proposals", {"request_id": req["id"], "run_id": run["id"], "kind": "clarify",
                                                   "payload": {"question": "q", "missing_fields": []},
                                                   "evidence_ids": [], "explanation": "x", "rejected_alternatives": [],
                                                   "based_on_revision": api.revision()}, expect=409)
    assert e["error"]["code"] in ("INVALID_TRANSITION", "LEASE_LOST")
    time.sleep(0)


def test_operator_reconcile_and_close_rev3(seeded: Api):
    api = seeded
    req, run, prop = to_review(api, "link_work", LINK)
    api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
             "ProposalCommitResult", 202)
    dl = api.claim(["dispatch"])
    w = api.as_worker()
    w.post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="u", outcome="unknown",
           result={"action_id": dl["action_id"], "execution_state": "unknown", "receipt": None, "error": None}),
           "CompletionReceipt", private=True)
    act = api.get(f"/actions/{dl['action_id']}", "Action")["data"]
    body = {"outcome": "succeeded", "receipt": None, "evidence_ids": [], "expected_action_version": act["version"]}
    api.post(f"/actions/{act['id']}/reconcile", body, expect=409)  # reconcile job still active
    rc = api.claim(["reconcile"])
    w.post(f"/jobs/{rc['job_id']}/complete", lease_body(rc, completion_id="r", outcome="succeeded",
           result={"action_id": act["id"], "lookup_evidence": "none", "established": False, "receipt": None}),
           "CompletionReceipt", private=True)
    assert api.get(f"/runs/{run['id']}", "Run")["data"]["status"] == "awaiting_review"
    api.post(f"/actions/{act['id']}/reconcile", body, expect=422)  # succeeded needs a receipt
    api.as_worker().post(f"/actions/{act['id']}/reconcile", body, expect=403)  # operator only
    bad = {**body, "receipt": {"id": "rcpt_x", "connector": "local", "target_id": "nope", "target_url": None,
                               "observed_at": "2026-10-16T05:00:00Z", "result_hash": "b" * 64}}
    api.post(f"/actions/{act['id']}/reconcile", bad, expect=422)  # target does not exist
    fb = {"outcome": "failed", "receipt": None, "evidence_ids": [], "expected_action_version": act["version"]}
    out = api.post(f"/actions/{act['id']}/reconcile", fb, "Action", 200, key="rec-1")["data"]
    assert out["status"] == "failed"
    assert api.post(f"/actions/{act['id']}/reconcile", fb, "Action", 200, key="rec-1")["data"] == out  # replay
    r = api.get(f"/runs/{run['id']}", "Run")["data"]
    assert r["status"] == "queued" and r["usage"]["replans"] == 1
    lease = api.claim(["investigate"])
    assert lease["run_id"] == run["id"] and "resume_reason" in lease["checkpoint"]
    evs = api.get(f"/runs/{run['id']}/events", "EventsPage")["data"]
    assert evs[-1]["type"] == "action_updated" and evs[-1]["payload"]["status"] == "failed"
    # closure
    rq = api.get(f"/requests/{req['id']}", "Request")["data"]
    api.post(f"/requests/{req['id']}/close", {"reason": "dup", "expected_request_version": rq["version"] - 1}, expect=409)
    api.post(f"/requests/{req['id']}/close", {"reason": ""}, expect=422)
    c = api.post(f"/requests/{req['id']}/close", {"reason": "customer withdrew", "expected_request_version":
                                                   rq["version"]}, "Request", 200)["data"]
    assert c["status"] == "closed" and c["version"] == rq["version"] + 1
    api.post(f"/requests/{req['id']}/close", {"reason": "again", "expected_request_version": c["version"]},
             expect=409)


def test_operator_reconcile_succeeded_linear(app_factory):
    api = Api(TestClient(app_factory(linear_enabled=True, linear_configured=True)))
    api.import_doc("json", json.dumps(SEED), "seed")
    payload = {"title": "t", "description": "d", "acceptance_criteria": ["a"], "owner_id": None,
               "capability_id": "cap_exports", "dependency_ids": [], "connector": "linear"}
    req, run, prop = to_review(api, "create_work", payload)
    api.post(f"/proposals/{prop['id']}/commit", {"expected_revision": api.revision(), "authorization_id": None},
             "ProposalCommitResult", 202)
    dl = api.claim(["dispatch"])
    w = api.as_worker()
    w.post(f"/actions/{dl['action_id']}/begin-dispatch", {"job_id": dl["job_id"], **lease_body(dl),
           "expected_action_version": 1, "expected_revision": api.revision(), "remote_preconditions": []},
           "DispatchPermit", private=True)
    w.post(f"/jobs/{dl['job_id']}/complete", lease_body(dl, completion_id="u", outcome="unknown",
           result={"action_id": dl["action_id"], "execution_state": "unknown", "receipt": None, "error": None}),
           "CompletionReceipt", private=True)
    rc = api.claim(["reconcile"])
    w.post(f"/jobs/{rc['job_id']}/complete", lease_body(rc, completion_id="r", outcome="succeeded",
           result={"action_id": dl["action_id"], "lookup_evidence": "none", "established": False, "receipt": None}),
           "CompletionReceipt", private=True)
    act = api.get(f"/actions/{dl['action_id']}", "Action")["data"]
    receipt = {"id": "rcpt_op", "connector": "linear", "target_id": "LIN-7", "target_url": None,
               "observed_at": "2026-10-16T05:00:00Z", "result_hash": "c" * 64}
    out = api.post(f"/actions/{act['id']}/reconcile", {"outcome": "succeeded", "receipt": receipt, "evidence_ids": [],
                                                       "expected_action_version": act["version"]}, "Action")["data"]
    assert out["status"] == "succeeded" and out["receipt"]["target_id"] == "LIN-7"
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "awaiting_work"
    cont = api.claim(["investigate"])
    assert cont["checkpoint"]["resume_stage"] == "observe" and cont["checkpoint"]["action_id"] == act["id"]
