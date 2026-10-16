"""PROTOCOL.md "Rev 5" (items 30-35) conformance."""

from __future__ import annotations

import time

from conftest import WK, new_request
from test_flows import investigate_to_proposal


def test_expand_edges_and_truncation_item_30(api):
    rels = ["ABOUT", "IMPLEMENTED_BY", "DEPENDS_ON", "OWNED_BY"]
    out = api.post("/context/expand", {"entity_ids": ["request_exports_001"], "relation_types": rels, "depth": 4, "limit": 100},
                   key=None).json()["data"]
    visited = {n["id"] for n in out["nodes"]}
    assert out["edges"] and len(out["edges"]) <= 500
    for e in out["edges"]:
        assert e["type"] in rels and e["from_id"] in visited and e["to_id"] in visited
    assert any(e["from_id"] == "request_exports_001" and e["to_id"] == "cap_scheduled_exports" and e["type"] == "ABOUT"
               for e in out["edges"])  # real direction
    assert out["truncated"] == (out["truncation"] is not None)
    d = api.post("/context/expand", {"entity_ids": ["request_exports_001"], "relation_types": rels, "depth": 1, "limit": 100},
                 key=None).json()["data"]
    assert d["truncated"] is True and d["truncation"] == "depth"
    lim = api.post("/context/expand", {"entity_ids": ["request_exports_001"], "relation_types": rels, "depth": 4, "limit": 1},
                   key=None).json()["data"]
    assert lim["truncated"] is True and lim["truncation"] == "limit" and len(lim["paths"]) == 1
    none = api.post("/context/expand", {"entity_ids": ["request_exports_001"], "relation_types": ["VERIFIES"], "depth": 2,
                                        "limit": 10}, key=None).json()["data"]
    assert none["truncated"] is False and none["truncation"] is None and none["edges"] == []


def test_attempts_scope_filter_item_31(api):
    all_scoped = api.get("/attempts", params={"scope_entity_type": "capability", "scope_entity_id": "cap_scheduled_exports",
                                              "limit": 100}).json()["data"]
    assert all_scoped and all(a["scope"] == {"entity_type": "capability", "entity_id": "cap_scheduled_exports"} for a in all_scoped)
    assert api.get("/attempts", params={"scope_entity_type": "capability", "scope_entity_id": "cap_ghost"}).json()["data"] == []
    by_type = api.get("/attempts", params={"scope_entity_type": "request", "limit": 100}).json()["data"]
    assert all(a["scope"]["entity_type"] == "request" for a in by_type)


def test_search_customer_filters_requests_only_item_32(api):
    other = next(c["id"] for c in api.get("/customers", params={"limit": 100}).json()["data"] if c["id"] != "customer_acme")
    body = {"query": "scheduled exports", "entity_types": ["request", "capability", "work_item", "source"], "customer_id": other,
            "limit": 20}
    cands = api.post("/context/search", body, key=None).json()["data"]["candidates"]
    assert any(c["entity_type"] == "capability" and c["id"] == "cap_scheduled_exports" for c in cands)
    req_ids = [c["id"] for c in cands if c["entity_type"] == "request"]
    owners = {r["id"]: r["customer_id"] for r in api.get("/requests", params={"limit": 100}).json()["data"]}
    assert all(owners[r] == other for r in req_ids)


def test_people_read_item_33(api):
    p = api.get("/people/person_maya").json()["data"]
    assert p["id"] == "person_maya" and isinstance(p["active"], bool) and p["version"] >= 1
    assert api.get("/people/ghost").json()["error"]["code"] == "NOT_FOUND"
    assert api.get("/people/person_maya", ws="ws_other").status_code == 404
    assert api.get("/people/person_maya", token=WK).status_code == 200


def test_new_run_supersedes_awaiting_review_item_34(api, worker):
    req = new_request(api)
    rid, old_run = req["request"]["id"], req["run"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "link_work", {"work_item_id": "work_export_issue", "comment_text": None},
                                       evidence=["ev_export_issue_1"])
    worker.complete(lease, "waiting", {"proposal_ids": [p["id"]], "awaiting": "review"})
    assert api.get(f"/runs/{old_run}").json()["data"]["status"] == "awaiting_review"
    r = api.post(f"/requests/{rid}/runs", {"mode": "scripted", "instruction": "replan", "authorization_id": None})
    assert r.status_code == 202
    new_run = r.json()["data"]["id"]
    assert api.get(f"/runs/{old_run}").json()["data"]["status"] == "cancelled"
    fin = api.get(f"/runs/{old_run}/events").json()["data"][-1]
    assert fin["type"] == "run_finished" and fin["payload"]["superseded_by"] == new_run and fin["step_id"] is None
    hist = api.get("/proposals", params={"run_id": old_run}).json()["data"]
    assert [x["id"] for x in hist] == [p["id"]] and hist[0]["validity"] == "stale"
    c = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": api.rev(), "authorization_id": None})
    assert c.status_code == 409 and c.json()["error"]["code"] == "REVISION_CONFLICT"
    again = api.post(f"/requests/{rid}/runs", {"mode": "scripted", "instruction": "", "authorization_id": None})
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_TRANSITION"  # new run is queued, not reviewing
    assert worker.claim(["investigate"])["run_id"] == new_run


def test_retryable_failure_next_attempt_completes_item_35(api, worker):
    req = new_request(api)
    run_id = req["run"]["id"]
    lease = worker.claim(["investigate"])
    rc = worker.complete(lease, "retryable_failure", {"proposal_ids": [], "summary": "dependency outage"}, completion_id="cmp-a1")
    assert rc["next_job_id"] == lease["job_id"] and rc["run"]["status"] == "queued"
    replay = worker.complete(lease, "retryable_failure", {"proposal_ids": [], "summary": "dependency outage"},
                             completion_id="cmp-a1")
    assert replay == rc  # dedupe within the attempt
    time.sleep(0.6)  # retry backoff for attempt 1
    lease2 = worker.claim(["investigate"])
    assert lease2["job_id"] == lease["job_id"] and lease2["attempt"] == 2 and lease2["fencing_token"] > lease["fencing_token"]
    done = worker.complete(lease2, "succeeded", {"proposal_ids": []}, completion_id="cmp-a2")
    assert done["outcome"] == "succeeded" and done["run"]["status"] == "succeeded" and done["run"]["id"] == run_id
    late = worker.complete(lease2, "succeeded", {"proposal_ids": []}, completion_id="cmp-a3", expect=409)
    assert late["error"]["code"] == "LEASE_LOST"
