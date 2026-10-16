"""Tool client, Linear connector, dispatch/reconcile effects and the disabled model adapter."""

from __future__ import annotations

import json

import httpx
import pytest

from contracts.tools.validate import errors as contract_errors
from promisegraph_worker.config import Limits
from promisegraph_worker.connectors.linear import LinearConnector, LinearError
from promisegraph_worker.connectors.local import LocalTaskConnector
from promisegraph_worker.http import ApiClient, Backoff, LeaseHeaders
from promisegraph_worker.jobs.effects import EffectRunner
from promisegraph_worker.jobs.lease import LeaseKeeper
from promisegraph_worker.markers import action_marker
from promisegraph_worker.planner.base import CapabilityDisabled
from promisegraph_worker.tools import TOOL_NAMES, TOOL_SCHEMAS, ToolClient, bound_result

from .conftest import Router, err, no_sleep, ok

REQ = {"id": "req_9", "customer_id": "cust_q", "version": 3, "source_id": "src_req_9"}


def tools(router: Router, auto=("answer", "clarify"), max_bytes=24 * 1024) -> ToolClient:
    api = ApiClient("http://api", "w", transport=router.transport(), backoff=Backoff(sleep=no_sleep))
    return ToolClient(api, workspace_id="ws", request=dict(REQ), run_id="run_9", lease_headers=lambda: LeaseHeaders("job_9", "lt", 5),
                      ensure_lease=lambda: None, auto_commit_kinds=list(auto), max_result_bytes=max_bytes)


def test_catalog_has_the_eight_tools():
    assert set(TOOL_SCHEMAS) == set(TOOL_NAMES) and len(TOOL_NAMES) == 8


def test_result_shape_and_contract_valid_search(router: Router):
    def search(req):
        body = json.loads(req.content)
        assert not contract_errors("ContextSearch", body)
        return ok({"candidates": [], "truncated": False})

    router.on("POST", "/context/search", search)
    r = tools(router).execute({"call_id": "c1", "name": "search_context", "arguments": {"query": "q", "entity_types": ["source"], "customer_id": None, "limit": 5}})
    assert set(r) == {"call_id", "ok", "data", "error", "meta"} and r["ok"] and r["error"] is None and r["meta"]["knowledge_revision"] == 7


def test_invalid_arguments_rejected_before_network(router: Router):
    r = tools(router).execute({"call_id": "c1", "name": "expand_context", "arguments": {"entity_ids": [], "relation_types": ["NOPE"], "depth": 9, "limit": 1}})
    assert not r["ok"] and r["data"] is None and r["error"]["code"] == "INVALID_INPUT" and not router.calls


def test_unknown_tool_is_an_error_result(router: Router):
    assert tools(router).execute({"call_id": "c", "name": "run_cypher", "arguments": {}})["error"]["code"] == "INVALID_INPUT"


def test_prepare_action_binds_request_run_lease_and_idempotency(router: Router):
    def prop(req):
        body = json.loads(req.content)
        assert not contract_errors("ProposalCreate", body), contract_errors("ProposalCreate", body)
        assert body["request_id"] == "req_9" and body["run_id"] == "run_9"
        assert req.headers["X-Job-Id"] == "job_9" and req.headers["X-Lease-Token"] == "lt" and req.headers["X-Fencing-Token"] == "5"
        assert req.headers["Idempotency-Key"]
        return ok({"id": "prop_1", "kind": body["kind"], "validity": "valid", "based_on_revision": 7}, 201)

    router.on("POST", "/proposals", prop)
    r = tools(router).execute({"call_id": "c1", "name": "prepare_action", "arguments": {
        "kind": "escalate", "payload": {"reason": "r", "owner_id": None, "team_id": None}, "evidence_ids": [], "explanation": "e",
        "rejected_alternatives": [], "based_on_revision": 7, "request_id": "OTHER"}})
    assert r["ok"], r


def test_commit_policy_gate_never_fabricates_authorization(router: Router):
    router.on("GET", "/proposals/prop_x", ok({"id": "prop_x", "kind": "create_work"}))
    r = tools(router).execute({"call_id": "c", "name": "commit_action", "arguments": {"proposal_id": "prop_x", "expected_revision": 7}})
    assert r["error"]["code"] == "POLICY_VIOLATION" and not router.bodies("/commit")


def test_commit_allowed_kind_sends_null_authorization(router: Router):
    router.on("GET", "/proposals/prop_a", ok({"id": "prop_a", "kind": "answer"}))
    router.on("POST", "/proposals/prop_a/commit", lambda r: ok({"action": {"id": "act_1"}, "job_id": "job_d"}, 202))
    r = tools(router).execute({"call_id": "c", "name": "commit_action", "arguments": {"proposal_id": "prop_a", "expected_revision": 7}})
    assert r["ok"] and router.bodies("/commit")[0] == {"expected_revision": 7, "authorization_id": None}


def test_worker_cannot_record_verification_kinds(router: Router):
    r = tools(router).execute({"call_id": "c", "name": "record_observation", "arguments": {"action_id": None, "kind": "customer_confirmation", "verification": "passed", "body": "b", "evidence_ids": []}})
    assert r["error"]["code"] == "POLICY_VIOLATION" and not router.calls


def test_result_bounded_to_24kb_and_flagged():
    data = {"candidates": [{"id": f"c{i}", "excerpt": "x" * 1000} for i in range(60)], "truncated": False}
    out, truncated, original = bound_result(data, 24 * 1024)
    assert truncated and original > 24 * 1024 and len(json.dumps(out)) <= 24 * 1024 and out["truncated"] is True
    assert out["candidates"][0]["id"] == "c0"  # keeps the highest-ranked items


def test_lease_lost_on_tool_propagates(router: Router):
    from promisegraph_worker.http import LeaseLost

    router.on("POST", "/proposals", err(409, "LEASE_LOST"))
    with pytest.raises(LeaseLost):
        tools(router).execute({"call_id": "c1", "name": "prepare_action", "arguments": {"kind": "clarify", "payload": {"question": "q", "missing_fields": []},
                                                                                         "evidence_ids": [], "explanation": "e", "rejected_alternatives": [], "based_on_revision": 1}})


# ----------------------------------------------------------------------------------------------- Linear
ISSUE = {"id": "lin-uuid-1", "identifier": "PG-1", "url": "https://linear.app/x/issue/PG-1", "title": "T", "description": "d " + action_marker("act_L"),
         "updatedAt": "2026-10-16T00:00:00Z", "state": {"name": "Todo", "type": "unstarted"}, "assignee": None}


def linear(handler, **kw) -> LinearConnector:
    return LinearConnector(enabled=True, api_key="lin_test", team_id="team_t", transport=httpx.MockTransport(handler), backoff=Backoff(sleep=no_sleep), **kw)


def test_linear_disabled_makes_no_network_calls():
    called = []
    c = LinearConnector(enabled=False, api_key="", team_id="", transport=httpx.MockTransport(lambda r: called.append(r)))
    with pytest.raises(CapabilityDisabled):
        c.find_by_marker("a")
    assert not called and c.status == "disabled"


def test_linear_graphql_errors_on_http_200_are_errors():
    c = linear(lambda r: httpx.Response(200, json={"data": None, "errors": [{"message": "Argument Validation Error"}]}))
    with pytest.raises(LinearError) as e:
        c.read_issue("x")
    assert e.value.code == "LINEAR_GRAPHQL_ERROR"


def test_linear_rate_limit_backs_off_then_succeeds():
    n = {"i": 0}

    def h(r):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(400, json={"errors": [{"message": "rate", "extensions": {"code": "RATELIMITED"}}]})
        if n["i"] == 2:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"data": {"issue": ISSUE}})

    assert linear(h).read_issue("lin-uuid-1").identifier == "PG-1" and n["i"] == 3


def test_linear_create_timeout_is_ambiguous_and_carries_marker():
    sent = []

    def h(r):
        sent.append(json.loads(r.content))
        raise httpx.ReadTimeout("t", request=r)

    with pytest.raises(LinearError) as e:
        linear(h).create_issue(action_id="act_L", title="T", description="d")
    assert e.value.kind == "ambiguous" and len(sent) == 1  # never blindly re-sent
    assert action_marker("act_L") in sent[0]["variables"]["input"]["description"]


def test_linear_success_false_is_not_treated_as_absence():
    with pytest.raises(LinearError) as e:
        linear(lambda r: httpx.Response(200, json={"data": {"issueCreate": {"success": False, "issue": None}}})).create_issue(action_id="a", title="t", description="d")
    assert e.value.kind == "ambiguous"


def test_linear_find_by_marker_filters_on_exact_marker():
    other = {**ISSUE, "id": "lin-2", "description": "unrelated"}
    c = linear(lambda r: httpx.Response(200, json={"data": {"issues": {"nodes": [ISSUE, other]}}}))
    found = c.find_by_marker("act_L")
    assert [i.id for i in found] == ["lin-uuid-1"] and found[0].work_status == "open"
    rc = found[0].receipt("receipt_act_L", "2026-10-16T00:00:00Z")
    assert not contract_errors("Receipt", rc)


# ----------------------------------------------------------------------------------------------- effects
LEASE_D = {"job_id": "job_d", "kind": "dispatch", "run_id": "run_9", "action_id": "act_L", "attempt": 1, "lease_token": "lt", "fencing_token": 8,
           "expires_at": "2099-01-01T00:00:00Z", "checkpoint": None}
ACTION = {"id": "act_L", "status": "accepted", "connector": "linear", "kind": "create_work", "proposal_id": "prop_L", "version": 1, "receipt": None, "request_id": "req_9", "error": None}
PROPOSAL = {"id": "prop_L", "kind": "create_work", "based_on_revision": 7,
            "payload": {"title": "T", "description": "D", "acceptance_criteria": ["a"], "owner_id": None, "capability_id": None, "dependency_ids": [], "connector": "linear"}}


def effect_runner(router: Router, linear_handler=None, linear_enabled=True) -> tuple[EffectRunner, LeaseKeeper]:
    api = ApiClient("http://api", "w", transport=router.transport(), backoff=Backoff(sleep=no_sleep))
    lin = (linear(linear_handler) if linear_enabled else LinearConnector(enabled=False, api_key="", team_id=""))
    router.on("GET", "/runs/run_9", ok({"id": "run_9", "request_id": "req_9", "event_seq": 3, "status": "queued", "mode": "scripted",
                                         "usage": {"tool_calls": 0, "replans": 0, "active_ms": 0}, "budget": {"max_tool_calls": 12, "max_replans": 2, "max_active_seconds": 90}}))
    router.on("POST", "/complete", lambda r: ok({"job_id": "job_d", "completion_id": json.loads(r.content)["completion_id"], "outcome": json.loads(r.content)["outcome"],
                                                 "recorded_at": "2026-10-16T00:00:00Z", "run": None, "next_job_id": None}))
    return EffectRunner(api, Limits(), LocalTaskConnector(api), lin), LeaseKeeper(api, "ws", dict(LEASE_D))


def _permit(router: Router):
    from promisegraph_worker.http import canonical_json, sha256_hex

    def bd(req):
        body = json.loads(req.content)
        assert not contract_errors("BeginDispatch", body), contract_errors("BeginDispatch", body)
        return ok({"permit_id": "perm", "expires_at": "2099-01-01T00:00:00Z", "payload_hash": sha256_hex(canonical_json(PROPOSAL["payload"])), "action_version": 2})

    router.on("POST", "/begin-dispatch", bd)


def test_linear_timeout_after_create_reports_unknown_then_reconcile_finds_marker(router: Router):
    router.on("GET", "/actions/act_L", ok(dict(ACTION))).on("GET", "/proposals/prop_L", ok(PROPOSAL))
    _permit(router)
    created = {"n": 0}

    def lin(r):
        q = json.loads(r.content)["query"]
        if "issueCreate" in q:
            created["n"] += 1
            raise httpx.ReadTimeout("t", request=r)
        if "issues(" in q:
            return httpx.Response(200, json={"data": {"issues": {"nodes": [ISSUE]}}})
        return httpx.Response(200, json={"data": {"issue": ISSUE}})

    runner, keeper = effect_runner(router, lin)
    out = runner.dispatch(keeper)
    assert out["outcome"] == "unknown" and out["result"]["execution_state"] == "unknown" and created["n"] == 1
    assert not contract_errors("JobComplete", router.bodies("/complete")[0])
    router.on("GET", "/actions/act_L", ok({**ACTION, "status": "unknown"}))
    out = runner.reconcile(LeaseKeeper(runner.api, "ws", {**LEASE_D, "job_id": "job_r", "kind": "reconcile"}))
    assert out["outcome"] == "succeeded" and out["result"]["established"] is True and out["result"]["receipt"]["target_id"] == "lin-uuid-1"
    assert created["n"] == 1  # no blind recreate


def test_reconcile_absence_is_inconclusive_operator_review(router: Router):
    router.on("GET", "/actions/act_L", ok({**ACTION, "status": "unknown"})).on("GET", "/proposals/prop_L", ok(PROPOSAL))
    runner, keeper = effect_runner(router, lambda r: httpx.Response(200, json={"data": {"issues": {"nodes": []}}}))
    out = runner.reconcile(keeper)
    assert out["outcome"] == "unknown" and out["result"]["established"] is False
    assert any("inconclusive" in x for x in out["result"]["lookup_evidence"])


def test_linear_dispatch_success_reads_back_and_reports_receipt(router: Router):
    router.on("GET", "/actions/act_L", ok(dict(ACTION))).on("GET", "/proposals/prop_L", ok(PROPOSAL))
    _permit(router)

    def lin(r):
        q = json.loads(r.content)["query"]
        if "issueCreate" in q:
            return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": ISSUE}}})
        return httpx.Response(200, json={"data": {"issue": {**ISSUE, "state": {"name": "In Progress", "type": "started"}}}})

    runner, keeper = effect_runner(router, lin)
    out = runner.dispatch(keeper)
    assert out["outcome"] == "succeeded" and not contract_errors("Receipt", out["result"]["receipt"])


def test_linear_dispatch_with_previous_dispatching_holder_looks_before_acting(router: Router):
    router.on("GET", "/actions/act_L", ok({**ACTION, "status": "dispatching"})).on("GET", "/proposals/prop_L", ok(PROPOSAL))
    sent = []

    def lin(r):
        q = json.loads(r.content)["query"]
        sent.append(q)
        if "issues(" in q:
            return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})
        raise AssertionError("must not create")

    runner, keeper = effect_runner(router, lin)
    assert runner.dispatch(keeper)["outcome"] == "unknown" and not any("issueCreate" in q for q in sent)


def test_local_dispatch_ambiguous_failure_reports_unknown_not_retry(router: Router):
    router.on("GET", "/actions/act_L", ok({**ACTION, "connector": "local"}))
    router.on("POST", "/execute-local", err(503, "DEPENDENCY_UNAVAILABLE", retryable=True))
    runner, keeper = effect_runner(router, linear_enabled=False)
    out = runner.dispatch(keeper)
    assert out["outcome"] == "unknown" and len(router.bodies("/execute-local")) == 1
    assert not contract_errors("ExecuteLocal", router.bodies("/execute-local")[0])


def test_local_dispatch_revalidation_failure_is_definitive(router: Router):
    router.on("GET", "/actions/act_L", ok({**ACTION, "connector": "local"}))
    router.on("POST", "/execute-local", err(409, "REVISION_CONFLICT"))
    runner, keeper = effect_runner(router, linear_enabled=False)
    out = runner.dispatch(keeper)
    assert out["outcome"] == "failed" and out["result"]["error"]["code"] == "REVISION_CONFLICT"


def test_cancelled_run_does_not_start_an_accepted_effect(router: Router):
    router.on("GET", "/actions/act_L", ok({**ACTION, "connector": "local"}))
    runner, keeper = effect_runner(router, linear_enabled=False)
    router.on("GET", "/runs/run_9", ok({"id": "run_9", "request_id": "req_9", "event_seq": 3, "status": "cancelled", "mode": "scripted",
                                        "usage": {"tool_calls": 0, "replans": 0, "active_ms": 0}, "budget": {}}))
    out = runner.dispatch(keeper)
    assert out["result"]["error"]["code"] == "CANCELLED" and not router.bodies("/execute-local")


def test_linear_unconfigured_dispatch_fails_honestly_without_network(router: Router):
    router.on("GET", "/actions/act_L", ok(dict(ACTION)))
    runner, keeper = effect_runner(router, linear_enabled=False)
    assert runner.dispatch(keeper)["result"]["error"]["code"] == "CAPABILITY_DISABLED"


