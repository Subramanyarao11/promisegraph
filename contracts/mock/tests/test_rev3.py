"""PROTOCOL.md "Rev 3" (items 19-24) conformance."""

from __future__ import annotations

from conftest import WK, assert_newest_first, new_request
from mock_api.app import store
from mock_api.common import canonical, sha256
from test_flows import investigate_to_proposal


def _unknown_action_awaiting_review(api, worker):
    """Drive an answer to `unknown` (timeout after effect), then a reconcile that cannot establish -> awaiting_review."""
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "answer", {"draft_text": "Draft", "resolution_criteria": []})
    action = worker.commit(lease, p)["action"]
    worker.complete(lease, "succeeded", {"proposal_ids": [p["id"]]})
    api.fault({"local_create_timeout_after_effect": True})
    lease = worker.claim(["dispatch"])
    worker.execute_local(lease, action, expect=503)
    worker.complete(lease, "unknown", {"action_id": action["id"], "execution_state": "unknown", "receipt": None, "error": None})
    rl = worker.claim(["reconcile"])
    worker.complete(rl, "succeeded", {"action_id": action["id"], "lookup_evidence": {}, "established": False, "receipt": None})
    assert api.get(f"/runs/{run_id}").json()["data"]["status"] == "awaiting_review"
    a = api.get(f"/actions/{action['id']}").json()["data"]
    assert a["status"] == "unknown"
    return rid, run_id, a


def test_additive_reads_item_19(api, worker):
    ev = api.get("/evidence/ev_export_issue_1").json()["data"]
    assert ev["source_id"] == "src_export_issue" and ev["source_revision"] == 1
    assert api.get("/evidence/ev_ghost").json()["error"]["code"] == "NOT_FOUND"
    assert api.get("/evidence/ev_export_issue_1", ws="ws_other").status_code == 404
    w = api.get("/work-items/work_export_issue").json()["data"]
    assert w["id"] == "work_export_issue" and w["workspace_id"] == "ws_demo"
    assert api.get("/work-items/ghost").status_code == 404
    obs = api.get("/observations", params={"request_id": "request_exports_001"}).json()["data"]
    assert [o["id"] for o in obs] == ["observation_exports_001"]
    assert api.get("/observations", params={"request_id": "request_exports_001"}, ws="ws_other").json()["data"] == []
    req = new_request(api, auto_start=False)
    rid = req["request"]["id"]
    assert api.get("/attempts", params={"request_id": rid}).json()["data"] == []
    for verdict in ("failed", "passed"):
        v = api.get(f"/requests/{rid}").json()["data"]["version"]
        api.post(f"/requests/{rid}/observations", {"action_id": None, "kind": "operator_confirmation", "verification": verdict,
                                                   "body": f"Check {verdict}.", "evidence_ids": [], "expected_request_version": v})
    atts = api.get("/attempts", params={"request_id": rid}).json()["data"]
    obs = api.get("/observations", params={"request_id": rid}).json()["data"]
    assert sorted(a["verification"] for a in atts) == sorted(o["verification"] for o in obs) == ["failed", "passed"]
    assert_newest_first(atts, "observed_at")
    assert_newest_first(obs, "observed_at")
    assert_newest_first(api.get("/attempts", params={"limit": 100}).json()["data"], "observed_at")
    assert_newest_first(api.get("/observations", params={"limit": 100}).json()["data"], "observed_at")
    assert len(api.get("/attempts", params={"limit": 1}).json()["data"]) == 1


def test_operator_reconcile_succeeded_item_20(api, worker):
    rid, run_id, a = _unknown_action_awaiting_review(api, worker)
    path = f"/actions/{a['id']}/reconcile"
    target = store.workspaces["ws_demo"].local_effects[a["id"]]["target_id"]
    base = {"outcome": "succeeded", "target": {"target_id": target, "target_url": None}, "evidence_ids": [],
            "expected_action_version": a["version"]}
    assert api.post(path, base, token=WK).status_code == 403
    r = api.post(path, {**base, "target": None})
    assert r.status_code == 422 and "target" in r.json()["error"]["details"]["fields"]
    legacy = api.post(path, {k: v for k, v in base.items() if k != "target"} | {"receipt": None})  # #28: receipt removed
    assert legacy.status_code == 422
    assert api.post(path, {**base, "extra": 1}).status_code == 422
    r = api.post(path, {**base, "expected_action_version": a["version"] + 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    r = api.post(path, {**base, "target": {"target_id": "draft_wrong", "target_url": None}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "POLICY_VIOLATION"
    r = api.post(path, base, key="op-reconcile-1")
    assert r.status_code == 200
    done = r.json()["data"]
    assert done["status"] == "succeeded" and done["receipt"]["target_id"] == target
    eff = store.workspaces["ws_demo"].local_effects[a["id"]]["record"]
    assert done["receipt"]["result_hash"] == sha256(canonical(eff))  # API-issued from the readback, not operator-supplied
    again = api.post(path, base, key="op-reconcile-1")
    assert again.headers["Idempotent-Replayed"] == "true" and again.json()["data"] == done
    run = api.get(f"/runs/{run_id}").json()["data"]
    assert (run["status"], run["stage"]) == ("queued", "observe")
    assert api.get(f"/requests/{rid}").json()["data"]["status"] == "awaiting_verification"
    lease = worker.claim(["investigate"])
    assert lease["checkpoint"]["resume_stage"] == "observe" and lease["checkpoint"]["action_id"] == a["id"]
    assert api.get(f"/runs/{run_id}/events").json()["data"][-1]["type"] == "action_updated"
    r = api.post(path, {**base, "expected_action_version": done["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_TRANSITION"
    assert api.post("/actions/action_exports_001/reconcile",
                    {**base, "expected_action_version": 3}).json()["error"]["code"] == "INVALID_TRANSITION"


def test_operator_reconcile_failed_replans(api, worker):
    rid, run_id, a = _unknown_action_awaiting_review(api, worker)
    r = api.post(f"/actions/{a['id']}/reconcile", {"outcome": "failed", "target": None, "evidence_ids": ["ev_export_issue_1"],
                                                    "expected_action_version": a["version"]})
    assert r.status_code == 200 and r.json()["data"]["status"] == "failed"
    run = api.get(f"/runs/{run_id}").json()["data"]
    assert (run["status"], run["stage"]) == ("queued", "investigate") and run["usage"]["replans"] == 1
    lease = worker.claim(["investigate"])
    assert lease["run_id"] == run_id and lease["checkpoint"]["resume_reason"]


def test_close_request_item_21(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    v = req["request"]["version"]
    assert api.post(f"/requests/{rid}/close", {"reason": "dup", "expected_request_version": v}, token=WK).status_code == 403
    assert api.post(f"/requests/{rid}/close", {"expected_request_version": v}).status_code == 422
    r = api.post(f"/requests/{rid}/close", {"reason": "dup", "expected_request_version": v + 9})
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    r = api.post(f"/requests/{rid}/close", {"reason": "Duplicate of another request.", "expected_request_version": v})
    assert r.status_code == 200
    closed = r.json()["data"]
    assert closed["status"] == "closed" and closed["version"] > v
    assert api.get(f"/runs/{run_id}").json()["data"]["status"] == "cancelled"
    worker.claim(["investigate"], expect=204)
    r = api.post(f"/requests/{rid}/close", {"reason": "again", "expected_request_version": closed["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_TRANSITION"


def test_items_22_to_24(api, worker):
    # 22: refresh keeps run_id null; target in checkpoint
    api.post("/requests/request_exports_001/refresh", {"reason": "poll work"})
    lease = worker.claim(["refresh"])
    assert lease["run_id"] is None
    assert lease["checkpoint"] == {"request_id": "request_exports_001", "reason": "poll work", "work_item_ids": ["work_export_issue"]}
    # 23: link_work actions are local
    assert api.get("/actions/action_exports_001").json()["data"]["connector"] == "local"
    # 24: database unreachable -> meta.knowledge_revision 0 ("unknown")
    assert api.rev() > 0
    api.fault({"database_unavailable": True})
    r = api.get("/info")
    assert r.status_code == 503 and r.json()["meta"]["knowledge_revision"] == 0


def test_reconcile_without_local_effect_item_26(api, worker):
    req = new_request(api)
    rid, run_id = req["request"]["id"], req["run"]["id"]
    lease, p = investigate_to_proposal(api, worker, rid, "answer", {"draft_text": "Draft", "resolution_criteria": []})
    action = worker.commit(lease, p)["action"]
    worker.complete(lease, "succeeded", {"proposal_ids": [p["id"]]})
    lease = worker.claim(["dispatch"])  # report unknown without ever executing -> no local effect exists
    worker.complete(lease, "unknown", {"action_id": action["id"], "execution_state": "unknown", "receipt": None, "error": None})
    rl = worker.claim(["reconcile"])
    worker.complete(rl, "succeeded", {"action_id": action["id"], "lookup_evidence": {}, "established": False, "receipt": None})
    a = api.get(f"/actions/{action['id']}").json()["data"]
    r = api.post(f"/actions/{a['id']}/reconcile", {"outcome": "succeeded", "target": {"target_id": "draft_ghost", "target_url": None},
                                                    "evidence_ids": [],
                                                    "expected_action_version": a["version"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "POLICY_VIOLATION"
    assert api.get(f"/actions/{a['id']}").json()["data"]["status"] == "unknown"
    assert api.get(f"/runs/{run_id}").json()["data"]["status"] == "awaiting_review"
