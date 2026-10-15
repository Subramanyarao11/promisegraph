"""Coverage of every remaining public/private route, examples parity, and the contract hash."""

from __future__ import annotations

import json

from conftest import REPO_ROOT, WK, new_request
from test_flows import continuation, investigate_to_proposal


def test_contract_hash_and_examples_parity(api):
    assert (REPO_ROOT / "contracts" / "CONTRACT_HASH").read_text().startswith("1.0.0 ")
    ex = json.loads((REPO_ROOT / "fixtures" / "contracts" / "examples.json").read_text())
    for e in ex["endpoint_examples"]:
        path = e["path"].split("/workspaces/ws_demo", 1)[1]
        p, _, q = path.partition("?")
        params = dict(kv.split("=") for kv in q.split("&")) if q else None
        r = api.get(p, params=params)
        assert r.status_code == e["status"]
        body = r.json()
        for k in body:
            if k != "meta":
                assert body[k] == e["body"][k], (path, k)
    # seeded example resources are served verbatim
    for kind, path in (("Proposal", "/proposals/proposal_exports_001"), ("Action", "/actions/action_exports_001"),
                       ("Run", "/runs/run_exports_001"), ("SourceRecord", "/sources/src_export_issue")):
        want = next(x["data"] for x in ex["resource_examples"] if x["schema"] == kind)
        assert api.get(path).json()["data"] == want, kind


def test_lists_pagination_and_filters(api):
    page1 = api.get("/requests", params={"limit": 5}).json()
    assert len(page1["data"]) == 5 and page1["page"]["next_cursor"]
    page2 = api.get("/requests", params={"limit": 5, "cursor": page1["page"]["next_cursor"]}).json()
    assert not {r["id"] for r in page1["data"]} & {r["id"] for r in page2["data"]}
    assert api.get("/requests", params={"limit": 101}).status_code == 422
    assert api.get("/requests", params={"cursor": "garbage!"}).json()["error"]["code"] == "INVALID_INPUT"
    assert all(r["status"] == "resolved" for r in api.get("/requests", params={"status": "resolved", "limit": 100}).json()["data"])
    assert all(r["customer_id"] == "customer_acme"
               for r in api.get("/requests", params={"customer_id": "customer_acme", "limit": 100}).json()["data"])
    assert any(c["id"] == "customer_acme" for c in api.get("/customers", params={"limit": 100}).json()["data"])
    caps = api.get("/capabilities", params={"query": "export"}).json()["data"]
    assert caps and all("export" in (c["name"] + " ".join(c["aliases"]) + c["id"]).lower() for c in caps)
    docs = api.get("/sources", params={"kind": "documentation", "limit": 100}).json()["data"]
    assert docs and all(s["kind"] == "documentation" for s in docs)
    assert api.get("/sources/src_export_issue", params={"revision": 2}).status_code == 404
    assert api.get("/actions", params={"request_id": "request_exports_001"}).json()["data"][0]["id"] == "action_exports_001"
    assert api.get("/work-items", params={"customer_id": "customer_acme", "limit": 100}).json()["data"]
    assert api.get("/requests/nope").json()["error"]["code"] == "NOT_FOUND"
    assert api.get("/no-such-route").json()["error"]["code"] == "NOT_FOUND"


def test_context_expand_paths_and_truncation(api):
    body = {"entity_ids": ["request_exports_001"], "relation_types": ["ABOUT", "IMPLEMENTED_BY", "DEPENDS_ON", "OWNED_BY"],
            "depth": 4, "limit": 100}
    out = api.post("/context/expand", body, key=None).json()["data"]
    to_maya = [p for p in out["paths"] if p["nodes"][-1]["id"] == "person_maya"]
    assert to_maya
    for p in out["paths"]:
        ids = [n["id"] for n in p["nodes"]]
        assert len(p["edges"]) == len(ids) - 1
        for i, e in enumerate(p["edges"]):  # contiguous path
            assert {e["from_id"], e["to_id"]} == {ids[i], ids[i + 1]}
    assert api.post("/context/expand", {**body, "depth": 1}, key=None).json()["data"]["truncated"] is True
    assert api.post("/context/expand", {**body, "limit": 1}, key=None).json()["data"]["truncated"] is True
    assert api.post("/context/expand", {**body, "entity_ids": ["ghost"]}, key=None).status_code == 404
    bad = api.post("/context/expand", {**body, "relation_types": ["MATCH (n) RETURN n"]}, key=None)
    assert bad.status_code == 422 and "relation_types/0" in bad.json()["error"]["details"]["fields"]
    ctx = api.get("/requests/request_exports_001/context", params={"depth": 6}).json()["data"]
    assert any(w["id"] == "work_export_issue" for w in ctx["related_work"])
    assert any(d["id"] == "decision_exports_001" for d in ctx["decisions"])
    assert api.get("/requests/request_exports_001/context", params={"depth": 7}).status_code == 422


def test_runs_create_cancel_resume(api, worker):
    r = api.post("/requests/request_sso_002/runs", {"mode": "scripted", "instruction": "", "authorization_id": None})
    assert r.status_code == 202 and r.json()["data"]["status"] == "queued"
    run = r.json()["data"]
    again = api.post("/requests/request_sso_002/runs", {"mode": "scripted", "instruction": "", "authorization_id": None})
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_TRANSITION"
    c = api.post(f"/runs/{run['id']}/cancel", {"reason": "Operator stop."})
    assert c.status_code == 200 and c.json()["data"]["status"] == "cancelled"
    worker.claim(["investigate"], expect=204)
    assert api.post(f"/runs/{run['id']}/cancel", {"reason": "again"}).status_code == 409
    bad_auth = api.post("/requests/request_sso_002/runs", {"mode": "scripted", "instruction": "", "authorization_id": "authorization_ghost"})
    assert bad_auth.status_code == 422 and bad_auth.json()["error"]["code"] == "POLICY_VIOLATION"
    # budget exhaustion -> one explicit resume
    run = api.post("/requests/request_sso_002/runs", {"mode": "scripted", "instruction": "", "authorization_id": None}).json()["data"]
    lease = worker.claim(["investigate"])
    worker.checkpoint(lease, "run_started", {"mode": "scripted", "stage": "investigate"})
    rc = worker.complete(lease, "failed", {"proposal_ids": [], "error": {"code": "BUDGET_EXHAUSTED", "message": "12 tool calls"}})
    assert rc["run"]["status"] == "failed" and rc["run"]["error"]["code"] == "BUDGET_EXHAUSTED"
    v = rc["run"]["version"]
    stale = api.post(f"/runs/{run['id']}/resume", {"expected_run_version": v + 5, "reason": "x", "additional_tool_calls": 12,
                                                   "additional_active_seconds": 90})
    assert stale.status_code == 409
    res = api.post(f"/runs/{run['id']}/resume", {"expected_run_version": v, "reason": "Extend once.", "additional_tool_calls": 12,
                                                 "additional_active_seconds": 90})
    assert res.status_code == 202
    rr = res.json()["data"]
    assert rr["status"] == "queued" and rr["budget"]["max_tool_calls"] == 24 and rr["budget"]["max_active_seconds"] == 180
    assert rr["budget"]["extensions_used"] == 1 and rr["usage"]["tool_calls"] >= 0
    lease = worker.claim(["investigate"])
    assert lease["run_id"] == run["id"] and lease["checkpoint"]["resume_reason"] == "Extend once."
    rc = worker.complete(lease, "failed", {"proposal_ids": [], "error": {"code": "BUDGET_EXHAUSTED", "message": "again"}})
    twice = api.post(f"/runs/{run['id']}/resume", {"expected_run_version": rc["run"]["version"], "reason": "again",
                                                   "additional_tool_calls": 1, "additional_active_seconds": 1})
    assert twice.status_code == 422 and twice.json()["error"]["code"] == "POLICY_VIOLATION"
    # model planner run without AI -> failed CAPABILITY_DISABLED, not resumable
    m = api.post("/requests/request_sso_002/runs", {"mode": "model", "instruction": "", "authorization_id": None}).json()["data"]
    assert m["status"] == "failed" and m["error"]["code"] == "CAPABILITY_DISABLED"


def test_refresh_coalesces_and_completes(api, worker):
    a = api.post("/requests/request_exports_001/refresh", {"reason": "Check work status"}).json()["data"]
    b = api.post("/requests/request_exports_001/refresh", {"reason": "Again"}).json()["data"]
    assert a["coalesced"] is False and b["coalesced"] is True and a["job_id"] == b["job_id"]
    lease = worker.claim(["refresh"])
    assert lease["kind"] == "refresh" and lease["run_id"] is None and lease["job_id"] == a["job_id"]
    cp = worker.checkpoint(lease, "run_started", {}, expected_event_seq=0, expect=409)
    assert cp.json()["error"]["code"] == "INVALID_TRANSITION"
    missing = worker.complete(lease, "succeeded", {"work_item_ids": []}, expect=422)
    assert "result/observation_ids" in missing["error"]["details"]["fields"]
    rc = worker.complete(lease, "succeeded", {"work_item_ids": ["work_export_issue"], "observation_ids": [],
                                              "affected_request_ids": ["request_exports_001"]})
    assert rc["run"] is None and rc["next_job_id"] is None
    assert api.post("/requests/request_exports_001/refresh", {"reason": "x"}, token=WK).status_code == 403


def test_decisions_record_supersede_revoke(api):
    base = {"rule_type": "allowed_action_types", "value": ["answer", "clarify"],
            "scope": {"entity_type": "customer", "entity_id": "customer_acme"}, "rationale": "Only drafts for Acme.",
            "effective_at": "2026-10-01T00:00:00Z", "expires_at": None, "evidence_ids": ["ev_export_policy_1"]}
    d1 = api.post("/decisions", {**base, "operation": "record", "decision_id": None, "expected_revision": api.rev()})
    assert d1.status_code == 201
    d1 = d1.json()["data"]
    assert d1["status"] == "active" and d1["author_id"] == "person_operator"
    wrong = api.post("/decisions", {**base, "value": True, "operation": "record", "decision_id": None, "expected_revision": api.rev()})
    assert wrong.status_code == 422
    need_target = api.post("/decisions", {**base, "operation": "supersede", "decision_id": None, "expected_revision": api.rev()})
    assert need_target.status_code == 422
    d2 = api.post("/decisions", {**base, "value": ["answer", "clarify", "link_work"], "operation": "supersede",
                                 "decision_id": d1["id"], "expected_revision": api.rev()}).json()["data"]
    assert d2["supersedes_id"] == d1["id"]
    rv = api.post("/decisions", {**base, "operation": "revoke", "decision_id": d2["id"], "expected_revision": api.rev()})
    assert rv.status_code == 200 and rv.json()["data"]["status"] == "revoked"
    hist = {d["id"]: d["status"] for d in api.get("/decisions", params={"scope_entity_type": "customer",
                                                                        "scope_entity_id": "customer_acme", "limit": 100}).json()["data"]}
    assert hist[d1["id"]] == "superseded" and hist[d2["id"]] == "revoked"
    active = api.get("/decisions", params={"active": "true", "limit": 100}).json()["data"]
    assert d1["id"] not in {d["id"] for d in active} and all(d["status"] == "active" for d in active)
    again = api.post("/decisions", {**base, "operation": "revoke", "decision_id": d2["id"], "expected_revision": api.rev()})
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_TRANSITION"


def test_decision_policy_blocks_disallowed_kind(api, worker):
    api.post("/decisions", {"operation": "record", "decision_id": None, "rule_type": "allowed_action_types",
                            "value": ["clarify"], "scope": {"entity_type": "capability", "entity_id": "cap_scheduled_exports"},
                            "rationale": "Clarify only.", "effective_at": "2026-10-01T00:00:00Z", "expires_at": None,
                            "evidence_ids": [], "expected_revision": api.rev()})
    req = new_request(api)
    _, p = investigate_to_proposal(api, worker, req["request"]["id"], "answer", {"draft_text": "x", "resolution_criteria": []})
    assert p["validity"] == "invalid" and any("POLICY_VIOLATION" in r for r in p["validation_reasons"])


def test_imports_json_markdown_csv(api):
    doc = {"workspace_id": "ws_demo", "observed_at": "2026-10-10T00:00:00Z",
           "customers": [{"id": "customer_newco", "name": "NewCo (synthetic)", "plan": None, "constraints": []}],
           "work_items": [
               {"id": "work_newco_1", "title": "NewCo onboarding", "description": "", "kind": "ticket", "status": "open",
                "owner_id": None, "connector": "local", "external_id": None, "external_url": None, "forecast_at": None,
                "observed_at": "2026-10-10T00:00:00Z", "capability_ids": ["cap_scheduled_exports"], "depends_on": [],
                "acceptance_criteria": []},
               {"id": "work_bad", "title": "", "kind": "nope"}]}
    pv = api.post("/imports/preview", {"format": "json", "origin": "synthetic", "content": json.dumps(doc), "source_key": "t"})
    assert pv.status_code == 201
    pv = pv.json()["data"]
    assert pv["valid_count"] == 2 and pv["rejected_count"] == 1 and pv["rejected"][0]["location"] == "$.work_items[1]"
    assert {c["change"] for c in pv["changes"]} == {"create"}
    stale = api.post(f"/imports/{pv['preview_id']}/commit", {"expected_revision": pv["based_on_revision"] - 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    cm = api.post(f"/imports/{pv['preview_id']}/commit", {"expected_revision": pv["based_on_revision"]}).json()["data"]
    assert cm["imported_count"] == 2 and cm["rejected_count"] == 1 and cm["knowledge_revision"] == pv["based_on_revision"] + 1
    src = cm["source_revisions"][0]
    rec = api.get(f"/sources/{src['source_id']}", params={"revision": src["revision"]}).json()["data"]
    assert rec["sha256"] and rec["origin"] == "synthetic"
    dup = api.post(f"/imports/{pv['preview_id']}/commit", {"expected_revision": api.rev()})
    assert dup.status_code == 409
    # unchanged content -> noop
    pv2 = api.post("/imports/preview", {"format": "json", "origin": "synthetic", "content": json.dumps(doc), "source_key": "t"}).json()["data"]
    assert {c["change"] for c in pv2["changes"]} == {"noop"}
    cm2 = api.post(f"/imports/{pv2['preview_id']}/commit", {"expected_revision": pv2["based_on_revision"]}).json()["data"]
    assert cm2["imported_count"] == 0 and cm2["noop_count"] == 2 and cm2["knowledge_revision"] == pv2["based_on_revision"]
    # workspace mismatch rejects the document
    other = api.post("/imports/preview", {"format": "json", "origin": "synthetic",
                                          "content": json.dumps({**doc, "workspace_id": "ws_other"}), "source_key": "t"}).json()["data"]
    assert other["valid_count"] == 0 and other["rejected_count"] >= 1
    md = "---\ntitle: Export FAQ\ncapabilities: cap_scheduled_exports\nobserved_at: 2026-10-01T00:00:00Z\n---\n# FAQ\nExports run hourly.\n"
    pm = api.post("/imports/preview", {"format": "markdown", "origin": "synthetic", "content": md, "source_key": "doc_export_faq"}).json()["data"]
    assert pm["valid_count"] == 1
    api.post(f"/imports/{pm['preview_id']}/commit", {"expected_revision": pm["based_on_revision"]})
    assert api.get("/sources/doc_export_faq").json()["data"]["kind"] == "documentation"
    exp = api.post("/context/expand", {"entity_ids": ["cap_scheduled_exports"], "relation_types": ["SUPPORTED_BY"], "depth": 1,
                                       "limit": 100}, key=None).json()["data"]
    assert any(p["nodes"][-1]["id"] == "doc_export_faq" for p in exp["paths"])
    csv_ok = ("id,title,kind,status,owner_id,capability_ids,depends_on,forecast_at,observed_at\n"
              "work_csv_x,CSV item,ticket,open,,cap_scheduled_exports,,,2026-10-01T00:00:00Z\n")
    pc = api.post("/imports/preview", {"format": "csv", "origin": "synthetic", "content": csv_ok, "source_key": "c"}).json()["data"]
    assert pc["valid_count"] == 1 and pc["changes"][0]["entity_id"] == "work_csv_x"
    bad = api.post("/imports/preview", {"format": "csv", "origin": "synthetic", "content": "a,b\n1,2\n", "source_key": "c"}).json()["data"]
    assert bad["valid_count"] == 0 and bad["rejected_count"] == 1


def test_linear_dispatch_permit(api, worker, monkeypatch):
    monkeypatch.setenv("LINEAR_ENABLED", "true")
    assert api.get("/info").json()["data"]["connectors"]["linear"] == "unverified"
    req = new_request(api, caps=("cap_sso_login",), subject="SSO bug", body="SSO broken")
    rid = req["request"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "create_work", {
        "title": "Fix SSO", "description": "SSO broken", "acceptance_criteria": [], "owner_id": None,
        "capability_id": "cap_sso_login", "dependency_ids": [], "connector": "linear"})
    assert p["validity"] == "valid"
    worker.complete(lease, "waiting", {"proposal_ids": [p["id"]], "awaiting": "review"})
    action = api.post(f"/proposals/{p['id']}/commit", {"expected_revision": p["based_on_revision"], "authorization_id": None}
                      ).json()["data"]["action"]
    assert action["connector"] == "linear"
    lease = worker.claim(["dispatch"])
    local = worker.execute_local(lease, action, expect=422)
    assert local["error"]["code"] == "POLICY_VIOLATION"
    bd = {"job_id": lease["job_id"], **worker.bind(lease), "expected_action_version": action["version"],
          "expected_revision": api.rev(), "remote_preconditions": []}
    permit = api.post(f"/actions/{action['id']}/begin-dispatch", bd, token=WK, private=True)
    assert permit.status_code == 200
    assert api.get(f"/actions/{action['id']}").json()["data"]["status"] == "dispatching"
    receipt = {"id": "receipt_linear_1", "connector": "linear", "target_id": "LIN-123", "target_url": "https://linear.example/LIN-123",
               "observed_at": "2026-10-16T05:00:00Z", "result_hash": "a" * 64}
    rc = worker.complete(lease, "succeeded", {"action_id": action["id"], "execution_state": "succeeded", "receipt": receipt, "error": None})
    assert rc["next_job_id"]
    a = api.get(f"/actions/{action['id']}").json()["data"]
    assert a["status"] == "succeeded" and a["receipt"]["target_id"] == "LIN-123"
    assert any(w["external_id"] == "LIN-123" and w["connector"] == "linear"
               for w in api.get("/work-items", params={"limit": 100}).json()["data"])
    assert continuation(api, worker, rid, action["id"])["run"]["status"] == "succeeded"
    # external work cannot be patched by the operator
    wid = next(w["id"] for w in api.get("/work-items", params={"limit": 100}).json()["data"] if w["external_id"] == "LIN-123")
    pt = api.patch(f"/work-items/{wid}", {"expected_version": 1, "status": "done", "owner_id": None, "forecast_at": None, "reason": "x"})
    assert pt.status_code == 422 and pt.json()["error"]["code"] == "POLICY_VIOLATION"
