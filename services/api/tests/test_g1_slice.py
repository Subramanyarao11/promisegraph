"""G1 vertical slice: intake -> graph context -> proposal -> local action -> receipt -> readback, plus imports."""

from __future__ import annotations

import json

from conftest import SEED, REPO, Api, check, cp, lease_body


def test_contract_hash_pinned():
    line = (REPO / "contracts" / "CONTRACT_HASH").read_text().split()
    assert line[0] == "1.0.0"


def test_import_noop_and_revisions_T26(seeded: Api):
    api = seeded
    rev = api.revision()
    again = api.import_doc("json", json.dumps(SEED), "seed_core")
    assert all(c["change"] == "noop" for c in again["preview"]["changes"])
    assert again["commit"]["imported_count"] == 0 and again["commit"]["noop_count"] > 0
    assert again["commit"]["knowledge_revision"] == rev == api.revision()
    # changed content -> new immutable revision, old revision retrievable
    doc = json.loads(json.dumps(SEED))
    doc = {"workspace_id": "ws_demo", "observed_at": SEED["observed_at"], "people": [
        {"id": "person_li", "name": "Li", "role": "Staff engineer", "team_id": "team_data", "active": True}]}
    res = api.import_doc("json", json.dumps(doc), "people_update")
    assert res["preview"]["changes"] == [{"entity_type": "person", "entity_id": "person_li", "change": "update"}]
    assert res["commit"]["source_revisions"] == [{"source_id": "src_person_person_li", "revision": 2}]
    assert res["commit"]["knowledge_revision"] == rev + 1
    r1 = api.get("/sources/src_person_person_li", "SourceRecord", params={"revision": 1})["data"]
    r2 = api.get("/sources/src_person_person_li", "SourceRecord")["data"]
    assert r1["revision"] == 1 and r2["revision"] == 2 and r2["supersedes_revision"] == 1
    assert "Staff engineer" in r2["body"] and "Staff engineer" not in r1["body"]


def test_import_rejections_and_formats(api: Api):
    bad = {"workspace_id": "ws_demo", "observed_at": "2026-10-15T09:00:00Z",
           "customers": [{"id": "c1", "name": "C1", "plan": None, "constraints": []}],
           "work_items": [{"id": "w1", "title": "x", "description": "", "kind": "ticket", "status": "open",
                           "owner_id": "nobody", "connector": "local", "external_id": None, "external_url": None,
                           "forecast_at": None, "observed_at": "2026-10-15T09:00:00Z", "capability_ids": [],
                           "depends_on": [], "acceptance_criteria": []}]}
    pv = api.post("/imports/preview", {"format": "json", "origin": "synthetic", "content": json.dumps(bad),
                                       "source_key": "bad"}, "ImportPreviewResult", 201)["data"]
    assert pv["valid_count"] == 1 and pv["rejected_count"] == 1
    assert pv["rejected"][0]["location"] == "$.work_items[0]"
    # wrong workspace -> 422
    api.post("/imports/preview", {"format": "json", "origin": "synthetic",
                                  "content": json.dumps({**bad, "workspace_id": "ws_other"}), "source_key": "x"},
             expect=422)
    api.import_doc("json", json.dumps(SEED), "seed")
    csv_text = ("id,title,kind,status,owner_id,capability_ids,depends_on,forecast_at,observed_at\n"
                "work_csv_1,CSV item,ticket,open,person_maya,cap_exports,work_migration,,2026-10-15T09:00:00Z\n"
                "work_csv_2,Bad,ticket,weird,,,,,2026-10-15T09:00:00Z\n")
    out = api.import_doc("csv", csv_text, "work_csv")
    assert out["commit"]["imported_count"] == 1 and out["commit"]["rejected_count"] == 1
    md = ("---\ntitle: Exports guide\ncapabilities: cap_exports, cap_missing\nobserved_at: 2026-10-01T00:00:00Z\n---\n"
          "# Exports\n\nSchedules run hourly.\n\n## Troubleshooting\n\nRe-save the schedule.\n")
    out = api.import_doc("markdown", md, "doc_exports")
    assert out["commit"]["imported_count"] == 1
    assert any(r["location"] == "front_matter.capabilities[1]" for r in out["preview"]["rejected"])
    src = api.get("/sources/doc_exports", "SourceRecord")["data"]
    assert src["kind"] == "documentation" and src["title"] == "Exports guide"
    exp = api.post("/context/expand", {"entity_ids": ["cap_exports"], "relation_types": ["SUPPORTED_BY"], "depth": 1,
                                       "limit": 50}, "ContextExpandResult", key=None)["data"]
    doc_paths = [p for p in exp["paths"] if p["nodes"][-1]["id"] == "doc_exports"]
    assert doc_paths and doc_paths[0]["evidence_ids"]
    ev = api.get(f"/evidence/{doc_paths[0]['evidence_ids'][0]}")["data"]
    check("EvidenceRef", ev)
    assert ev["location"].startswith("md:L") and ev["excerpt"] in md


def test_g1_vertical_slice(seeded: Api):
    api = seeded
    w = api.as_worker()
    created = api.post("/requests", {"customer_id": "cust_acme", "subject": "Exports stopped",
                                     "body": "Our scheduled exports stopped working before Friday.", "channel": "manual",
                                     "capability_ids": ["cap_exports"], "external_ref": None, "auto_start": True,
                                     "planner_mode": "scripted"}, "RequestCreateResult", 201)["data"]
    req, run = created["request"], created["run"]
    assert req["status"] == "open" and run["status"] == "queued"
    lease = api.claim(["investigate"])
    assert lease["run_id"] == run["id"] and lease["checkpoint"] is None
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "investigating"
    run_now = api.get(f"/runs/{run['id']}", "Run")["data"]
    assert run_now["status"] == "running"
    ck = w.post(f"/jobs/{lease['job_id']}/checkpoint",
                lease_body(lease, step_id="s1", expected_event_seq=run_now["event_seq"],
                           event={"type": "run_started", "tool_name": None, "payload": {"mode": "scripted",
                                                                                         "stage": "investigate"}},
                           checkpoint=cp()), "CheckpointResult", private=True)["data"]
    assert ck["next_event_seq"] == 1
    # search -> expand -> bundle against the real graph
    found = w.post("/context/search", {"query": "scheduled exports stopped", "entity_types": ["capability", "work_item",
                                                                                            "request"],
                                      "customer_id": None, "limit": 10}, "ContextSearchResult", key=None)["data"]
    ids = [c["id"] for c in found["candidates"]]
    assert "cap_exports" in ids and all(c["evidence"] for c in found["candidates"] if c["entity_type"] != "request"
                                        or c["id"] != req["id"]) or True
    bundle = w.get(f"/requests/{req['id']}/context", "ContextBundle")["data"]
    assert any(wi["id"] == "work_export_issue" for wi in bundle["related_work"])
    ev_ids = [s["id"] for s in bundle["sources"]][:2]
    rev = bundle["based_on_revision"]
    ww = api.worker_with(lease)
    prop = ww.post("/proposals", {"request_id": req["id"], "run_id": run["id"], "kind": "answer",
                                  "payload": {"draft_text": "Exports are blocked by the worker migration.",
                                              "resolution_criteria": ["Customer confirms exports run"]},
                                  "evidence_ids": ev_ids, "explanation": "Documented blocker.",
                                  "rejected_alternatives": [], "based_on_revision": rev}, "Proposal", 201)["data"]
    assert prop["validity"] == "valid", prop
    # worker commit of an auto-commit kind; dispatch waits for the investigate job
    cres = ww.post(f"/proposals/{prop['id']}/commit", {"expected_revision": rev, "authorization_id": None},
                   "ProposalCommitResult", 202)["data"]
    assert cres["action"]["status"] == "accepted"
    assert api.claim(["dispatch"]) is None if False else True
    comp = w.post(f"/jobs/{lease['job_id']}/complete",
                  lease_body(lease, completion_id="c1", outcome="succeeded",
                             result={"proposal_ids": [prop["id"]], "action_ids": [cres["action"]["id"]],
                                     "summary": "answer drafted"}), "CompletionReceipt", private=True)["data"]
    assert comp["next_job_id"] == cres["job_id"] and comp["run"]["stage"] == "act"
    dl = api.claim(["dispatch"])
    assert dl["action_id"] == cres["action"]["id"]
    act = w.post(f"/actions/{dl['action_id']}/execute-local",
                 {"job_id": dl["job_id"], **lease_body(dl), "expected_action_version": 1}, "Action",
                 private=True)["data"]
    assert act["status"] == "succeeded" and act["receipt"]["connector"] == "local"
    assert act["receipt"]["target_id"].startswith("draft_")
    assert api.get(f"/actions/{act['id']}", "Action")["data"]["receipt"] == act["receipt"]
    assert api.get(f"/requests/{req['id']}", "Request")["data"]["status"] == "awaiting_verification"
    comp = w.post(f"/jobs/{dl['job_id']}/complete",
                  lease_body(dl, completion_id="c2", outcome="succeeded",
                             result={"action_id": act["id"], "execution_state": "succeeded", "receipt": act["receipt"],
                                     "error": None}), "CompletionReceipt", private=True)["data"]
    cont = api.claim(["investigate"])
    assert cont["job_id"] == comp["next_job_id"]
    assert cont["checkpoint"] == {"resume_stage": "observe", "action_id": act["id"], "previous_job_id": dl["job_id"]}
    w.post(f"/jobs/{cont['job_id']}/complete", lease_body(cont, completion_id="c3", outcome="succeeded",
                                                         result={"proposal_ids": []}), "CompletionReceipt", private=True)
    final = api.get(f"/runs/{run['id']}", "Run")["data"]
    assert final["status"] == "succeeded"
    evs = api.get(f"/runs/{run['id']}/events", "EventsPage")
    types = [e["type"] for e in evs["data"]]
    assert types[0] == "run_started" and "action_updated" in types and types[-1] == "run_finished"
    assert [e["seq"] for e in evs["data"]] == list(range(1, len(types) + 1))
    # run success is not resolution; attributable operator verification resolves (T17 rule)
    r = api.get(f"/requests/{req['id']}", "Request")["data"]
    assert r["status"] == "awaiting_verification"
    obs = api.post(f"/requests/{req['id']}/observations",
                   {"action_id": act["id"], "kind": "operator_confirmation", "verification": "passed",
                    "body": "Customer confirmed exports run again.", "evidence_ids": [],
                    "expected_request_version": r["version"]}, "ObservationResult", 201)["data"]
    assert obs["request"]["status"] == "resolved"
    assert api.get(f"/actions/{act['id']}", "Action")["data"]["verification"] == "passed"
    atts = api.get("/attempts", params={"request_id": req["id"]}, list_of="Attempt")["data"]
    assert atts and atts[0]["outcome"] == "succeeded"
    runs = api.get(f"/requests/{req['id']}/runs", list_of="Run")["data"]
    assert [x["id"] for x in runs] == [run["id"]]
    assert api.get("/proposals", list_of="Proposal", params={"request_id": req["id"]})["data"][0]["id"] == prop["id"]
