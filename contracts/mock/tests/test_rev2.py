"""PROTOCOL.md "G0 clarifications (rev 2)" conformance."""

from __future__ import annotations

from conftest import WK, assert_newest_first, new_request
from test_flows import investigate_to_proposal


def test_request_runs_list_newest_first(api, worker):
    req = new_request(api)
    rid, r1 = req["request"]["id"], req["run"]["id"]
    api.post(f"/runs/{r1}/cancel", {"reason": "restart"})
    r2 = api.post(f"/requests/{rid}/runs", {"mode": "scripted", "instruction": "", "authorization_id": None}).json()["data"]["id"]
    runs = api.get(f"/requests/{rid}/runs").json()
    order = [r["id"] for r in runs["data"]]
    assert set(order) == {r1, r2} and runs["page"]["next_cursor"] is None
    assert_newest_first(runs["data"], "created_at")
    p1 = api.get(f"/requests/{rid}/runs", params={"limit": 1}).json()
    p2 = api.get(f"/requests/{rid}/runs", params={"limit": 1, "cursor": p1["page"]["next_cursor"]}).json()
    assert [r["id"] for r in p1["data"] + p2["data"]] == order and p2["page"]["next_cursor"] is None
    assert api.get("/requests/request_exports_001/runs").json()["data"][0]["id"] == "run_exports_001"
    assert api.get("/requests/nope/runs").json()["error"]["code"] == "NOT_FOUND"
    assert api.get(f"/requests/{rid}/runs", ws="ws_other").status_code == 404  # isolation
    assert api.get(f"/requests/{rid}/runs", token=WK).status_code == 200


def test_proposals_list_filters_newest_first(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    lease, p1 = investigate_to_proposal(api, worker, rid, "answer", {"draft_text": "a", "resolution_criteria": []})
    p2 = worker.propose(lease, rid, "clarify", {"question": "Which?", "missing_fields": []}, [], api.rev())
    by_req = api.get("/proposals", params={"request_id": rid}).json()["data"]
    assert {p["id"] for p in by_req} == {p1["id"], p2["id"]}
    assert_newest_first(by_req, "created_at")
    assert [p["id"] for p in api.get("/proposals", params={"run_id": run_id}).json()["data"]] == [p["id"] for p in by_req]
    assert_newest_first(api.get("/proposals", params={"limit": 100}).json()["data"], "created_at")
    assert api.get("/proposals", params={"request_id": rid, "run_id": "run_other"}).json()["data"] == []
    assert any(p["id"] == "proposal_exports_001" for p in api.get("/proposals", params={"limit": 100}).json()["data"])
    assert api.get("/proposals", params={"request_id": rid}, ws="ws_other").json()["data"] == []


def test_status_codes_item_8(api, worker):
    req = api.post("/requests", {"customer_id": "customer_acme", "subject": "s", "body": "b", "channel": "manual",
                                 "capability_ids": [], "external_ref": None, "auto_start": False, "planner_mode": "scripted"})
    assert req.status_code == 201
    rid = req.json()["data"]["request"]["id"]
    run = api.post(f"/requests/{rid}/runs", {"mode": "scripted", "instruction": "", "authorization_id": None})
    assert run.status_code == 202
    lease, p = investigate_to_proposal(api, worker, rid, "link_work", {"work_item_id": "work_export_issue", "comment_text": None},
                                       evidence=["ev_export_issue_1"])
    worker.complete(lease, "waiting", {"proposal_ids": [p["id"]], "awaiting": "review"})
    c = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": p["based_on_revision"], "authorization_id": None})
    assert c.status_code == 202
    # rev2 #14: request stays awaiting_review, run queued at act
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_review"
    r = api.get(f"/runs/{run.json()['data']['id']}").json()["data"]
    assert (r["status"], r["stage"]) == ("queued", "act")
    v = api.get(f"/requests/{rid}").json()["data"]["version"]
    assert api.post(f"/requests/{rid}/replies", {"body": "more", "external_ref": None, "expected_request_version": v}).status_code == 201
    v = api.get(f"/requests/{rid}").json()["data"]["version"]
    assert api.post(f"/requests/{rid}/observations", {"action_id": None, "kind": "operator_confirmation", "verification": "pending",
                                                      "body": "noted", "evidence_ids": [], "expected_request_version": v}).status_code == 201
    assert api.post(f"/requests/{rid}/refresh", {"reason": "r"}).status_code == 202
    dec = {"rule_type": "date_extension_allowed", "value": False, "scope": {"entity_type": "customer", "entity_id": "customer_acme"},
           "rationale": "r", "effective_at": "2026-10-01T00:00:00Z", "expires_at": None, "evidence_ids": []}
    d = api.post("/decisions", {**dec, "operation": "record", "decision_id": None, "expected_revision": api.rev()})
    assert d.status_code == 201
    s = api.post("/decisions", {**dec, "operation": "supersede", "decision_id": d.json()["data"]["id"], "expected_revision": api.rev()})
    assert s.status_code == 201
    rv = api.post("/decisions", {**dec, "operation": "revoke", "decision_id": s.json()["data"]["id"], "expected_revision": api.rev()})
    assert rv.status_code == 200
    pv = api.post("/imports/preview", {"format": "csv", "origin": "synthetic", "source_key": "k",
                                       "content": "id,title,kind,status,owner_id,capability_ids,depends_on,forecast_at,observed_at\n"
                                                  "work_s8,T,ticket,open,,,,,2026-10-01T00:00:00Z\n"})
    assert pv.status_code == 201
    cm = api.post(f"/imports/{pv.json()['data']['preview_id']}/commit", {"expected_revision": api.rev()})
    assert cm.status_code == 200


def test_auth_split_item_15(api, monkeypatch):
    for tok in (None, "bogus"):
        r = api.get("/info", token=tok)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    wrong_class_private = api.post("/jobs/claim", {"worker_id": "w", "kinds": ["investigate"], "lease_seconds": 60},
                                   private=True, key=None)
    assert wrong_class_private.status_code == 403 and wrong_class_private.json()["error"]["code"] == "UNAUTHORIZED"
    wrong_class_public = api.post("/requests/request_sso_002/refresh", {"reason": "x"}, token=WK)
    assert wrong_class_public.status_code == 403 and wrong_class_public.json()["error"]["code"] == "UNAUTHORIZED"
    monkeypatch.setenv("WORKSPACE_ACL", "operator:ws_demo;worker:ws_demo")
    for kw in ({}, {"token": WK}):
        f = api.get("/info", ws="ws_other", **kw)
        assert f.status_code == 403 and f.json()["error"]["code"] == "WORKSPACE_FORBIDDEN"


def test_idempotency_scope_is_concrete_path(api):
    body = {"reason": "same body"}
    a = api.post("/requests/request_exports_001/refresh", body, key="shared-key")
    b = api.post("/requests/request_sso_002/refresh", body, key="shared-key")
    assert a.status_code == b.status_code == 202
    assert a.json()["data"]["job_id"] != b.json()["data"]["job_id"] and "Idempotent-Replayed" not in b.headers
    again = api.post("/requests/request_sso_002/refresh", body, key="shared-key")
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == b.json()
    clash = api.post("/requests/request_sso_002/refresh", {"reason": "other"}, key="shared-key")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_worker_authorization_completion_and_evidence_rules(api, worker):
    req = new_request(api)
    rid = req["request"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "answer", {"draft_text": "a", "resolution_criteria": []},
                                       evidence=["ev_does_not_exist"])
    # rev2 #7: evidence failure is a stored invalid proposal (201); commit -> 422 EVIDENCE_INVALID
    assert p["validity"] == "invalid" and p["validation_reasons"][0].startswith("EVIDENCE_INVALID")
    r = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": api.rev(), "authorization_id": None})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EVIDENCE_INVALID"
    good = worker.propose(lease, rid, "answer", {"draft_text": "b", "resolution_criteria": []}, ["ev_export_issue_1"], api.rev())
    # rev2 #11: workers pass authorization_id null
    w = api.post(f"/proposals/{good['id']}/commit", {"expected_revision": api.rev(), "authorization_id": "authorization_operator_001"},
                 token=WK, headers=worker.hdrs(lease))
    assert w.status_code == 422 and w.json()["error"]["code"] == "POLICY_VIOLATION"
    # observations with unknown evidence are input errors, not EVIDENCE_INVALID
    v = api.get(f"/requests/{rid}").json()["data"]["version"]
    o = api.post(f"/requests/{rid}/observations", {"action_id": None, "kind": "operator_confirmation", "verification": "pending",
                                                   "body": "x", "evidence_ids": ["ev_ghost"], "expected_request_version": v})
    assert o.status_code == 422 and o.json()["error"]["code"] == "INVALID_INPUT"
    # rev2 #12: only proposal_ids is required on investigate completion
    rc = worker.complete(lease, "succeeded", {"proposal_ids": [p["id"], good["id"]]})
    assert rc["run"]["status"] == "succeeded"


def test_cancel_under_lease_defers_run_finished(api, worker):
    req = new_request(api)
    run_id = req["run"]["id"]
    lease = worker.claim(["investigate"])
    worker.checkpoint(lease, "run_started", {"mode": "scripted", "stage": "investigate"})
    seq = api.get(f"/runs/{run_id}").json()["data"]["event_seq"]
    assert api.post(f"/runs/{run_id}/cancel", {"reason": "stop"}).json()["data"]["status"] == "cancelled"
    assert api.get(f"/runs/{run_id}").json()["data"]["event_seq"] == seq  # rev2 #6: nothing appended under the lease
    worker.checkpoint(lease, "tool_started", {"call_id": "c1", "arguments": {}}, tool_name="search_context", expected_event_seq=seq)
    rc = worker.complete(lease, "succeeded", {"proposal_ids": []})
    assert rc["run"]["status"] == "cancelled"
    evs = api.get(f"/runs/{run_id}/events").json()["data"]
    assert evs[-1]["type"] == "run_finished" and evs[-1]["payload"]["run_status"] == "cancelled"
    # rev2 #10: context takes depth only; a stray cursor is ignored
    assert api.get(f"/requests/{req['request']['id']}/context", params={"depth": 2, "cursor": "x"}).status_code == 200


def test_worker_cannot_emit_api_owned_events_item_17(api, worker):
    req = new_request(api)
    run_id = req["run"]["id"]
    lease = worker.claim(["investigate"])
    for etype in ("action_updated", "waiting_for_input", "observation_recorded", "run_finished"):
        r = worker.checkpoint(lease, etype, {}, expect=422)
        assert r.json()["error"]["code"] == "INVALID_INPUT" and "event/type" in r.json()["error"]["details"]["fields"]
    assert api.get(f"/runs/{run_id}").json()["data"]["event_seq"] == 0  # nothing appended
    for etype in ("run_started", "tool_started", "tool_completed", "proposal_ready", "error"):
        worker.checkpoint(lease, etype, {})
    rc = worker.complete(lease, "succeeded", {"proposal_ids": []})
    evs = api.get(f"/runs/{run_id}/events").json()["data"]
    assert [e["type"] for e in evs] == ["run_started", "tool_started", "tool_completed", "proposal_ready", "error", "run_finished"]
    assert evs[-1]["step_id"] is None and rc["run"]["status"] == "succeeded"  # API-authored
