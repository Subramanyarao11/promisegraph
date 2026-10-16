"""HTTP client, lease keeper and checkpoint session (B1 protocol mechanics)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from contracts.tools.validate import errors as contract_errors
from promisegraph_worker.config import Limits, load_settings
from promisegraph_worker.http import ApiClient, ApiError, ApiTimeout, Backoff, LeaseLost
from promisegraph_worker.jobs.lease import LeaseKeeper
from promisegraph_worker.jobs.session import BudgetExhausted, JobSession

from .conftest import Router, err, no_sleep, ok

LEASE = {"job_id": "job_1", "kind": "investigate", "run_id": "run_1", "action_id": None, "attempt": 1, "lease_token": "tok",
         "fencing_token": 3, "expires_at": "2099-01-01T00:00:00Z", "checkpoint": None}
RUN = {"id": "run_1", "request_id": "req_1", "event_seq": 4, "usage": {"tool_calls": 2, "replans": 0, "active_ms": 10},
       "budget": {"max_tool_calls": 12, "max_replans": 2, "max_active_seconds": 90, "extensions_used": 0}, "mode": "scripted", "status": "running"}


def api(router: Router, **kw) -> ApiClient:
    return ApiClient("http://api", "wtok", transport=router.transport(), backoff=Backoff(sleep=no_sleep, **kw))


def test_auth_header_envelope_and_retry_after_on_429(router: Router):
    sleeps: list[float] = []
    seen = {"n": 0}

    def h(req: httpx.Request):
        seen["n"] += 1
        assert req.headers["Authorization"] == "Bearer wtok"
        return err(429, "RATE_LIMITED", headers={"Retry-After": "2"}) if seen["n"] == 1 else ok({"x": 1})

    router.on("GET", "/info", h)
    c = ApiClient("http://api", "wtok", transport=router.transport(), backoff=Backoff(sleep=sleeps.append))
    data, env = c.call("GET", ApiClient.public("ws", "/info"))
    assert data == {"x": 1} and env["meta"]["knowledge_revision"] == 7 and sleeps == [2.0]


def test_retryable_503_backs_off_then_raises_api_error(router: Router):
    router.on("GET", "/info", err(503, "DEPENDENCY_UNAVAILABLE", retryable=True))
    with pytest.raises(ApiError) as e:
        api(router).call("GET", ApiClient.public("ws", "/info"))
    assert e.value.code == "DEPENDENCY_UNAVAILABLE" and len(router.calls) == 4


def test_read_timeout_on_ambiguous_mutation_is_not_retried(router: Router):
    def boom(req):
        raise httpx.ReadTimeout("t", request=req)

    router.on("POST", "/execute-local", boom)
    with pytest.raises(ApiTimeout):
        api(router).call("POST", "/internal/v1/workspaces/ws/actions/a/execute-local", json_body={}, idempotency_key="k", retry_on_timeout=False)
    assert len(router.calls) == 1


def test_heartbeat_renews_and_409_marks_lease_lost(router: Router):
    n = {"hb": 0}

    def hb(req):
        n["hb"] += 1
        body = json.loads(req.content)
        assert body == {"lease_token": "tok", "fencing_token": 3}
        assert not contract_errors("LeaseBinding", body)
        return ok({**LEASE, "expires_at": "2099-01-02T00:00:00Z"}) if n["hb"] == 1 else err(409, "LEASE_LOST")

    router.on("POST", "/jobs/job_1/heartbeat", hb)
    k = LeaseKeeper(api(router), "ws", dict(LEASE))
    assert k.heartbeat() and not k.lost
    assert not k.heartbeat() and k.lost
    with pytest.raises(LeaseLost):
        k.ensure()


def test_local_expiry_without_renewal_stops_mutations(router: Router):
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    k = LeaseKeeper(api(router), "ws", {**LEASE, "expires_at": past})
    with pytest.raises(LeaseLost):
        k.ensure()


def test_background_heartbeat_thread_runs(router: Router):
    router.on("POST", "/heartbeat", ok(dict(LEASE)))
    k = LeaseKeeper(api(router), "ws", dict(LEASE), interval=0.01)
    with k:
        import time

        time.sleep(0.08)
    assert k.heartbeats >= 2


def test_checkpoint_carries_seq_stable_step_and_required_keys(router: Router):
    seq = {"v": 4}

    def cp(req):
        body = json.loads(req.content)
        assert not contract_errors("JobCheckpoint", body), contract_errors("JobCheckpoint", body)
        assert body["expected_event_seq"] == seq["v"]
        for key in ("completed_call_ids", "pending_action_id", "stage", "usage"):
            assert key in body["checkpoint"]
        assert req.headers["Idempotency-Key"]
        seq["v"] += 1
        return ok({"event": {"id": f"e{seq['v']}", "run_id": "run_1", "seq": seq["v"], "type": body["event"]["type"], "occurred_at": "2026-10-16T00:00:00Z",
                             "step_id": body["step_id"], "tool_name": None, "payload": body["event"]["payload"]}, "next_event_seq": seq["v"]})

    router.on("POST", "/jobs/job_1/checkpoint", cp)
    s = JobSession(api(router), LeaseKeeper(api(router), "ws", dict(LEASE)), limits=Limits(), run=dict(RUN))
    s.emit("run_started", "run_started", {"mode": "scripted", "stage": "investigate"})
    s.emit("a", "tool_started", {"call_id": "c1", "arguments": {}})
    assert s.event_seq == 6
    s.emit("a", "tool_started", {"call_id": "c1", "arguments": {}})  # same step: not re-sent
    assert len(router.bodies("/checkpoint")) == 2


def test_checkpoint_resyncs_after_revision_conflict(router: Router):
    state = {"first": True}

    def cp(req):
        body = json.loads(req.content)
        if state["first"]:
            state["first"] = False
            return err(409, "REVISION_CONFLICT", details={"current_event_seq": 9})
        assert body["expected_event_seq"] == 9
        return ok({"event": {"id": "e", "run_id": "run_1", "seq": 10, "type": "error", "occurred_at": "2026-10-16T00:00:00Z", "step_id": body["step_id"],
                             "tool_name": None, "payload": {}}, "next_event_seq": 10})

    router.on("POST", "/checkpoint", cp).on("GET", "/runs/run_1", ok({**RUN, "event_seq": 9}))
    s = JobSession(api(router), LeaseKeeper(api(router), "ws", dict(LEASE)), limits=Limits(), run=dict(RUN))
    s.emit("x", "error", {"code": "E", "message": "m"})
    assert s.event_seq == 10


def test_checkpoint_lease_lost_raises(router: Router):
    router.on("POST", "/checkpoint", err(409, "LEASE_LOST"))
    s = JobSession(api(router), LeaseKeeper(api(router), "ws", dict(LEASE)), limits=Limits(), run=dict(RUN))
    with pytest.raises(LeaseLost):
        s.emit("x", "error", {"code": "E", "message": "m"})
    assert s.keeper.lost


def test_budget_per_job_and_complete_body_is_contract_valid(router: Router):
    router.on("POST", "/jobs/job_1/complete", lambda r: ok({"job_id": "job_1", "completion_id": json.loads(r.content)["completion_id"], "outcome": "failed",
                                                           "recorded_at": "2026-10-16T00:00:00Z", "run": None, "next_job_id": None}))
    s = JobSession(api(router), LeaseKeeper(api(router), "ws", dict(LEASE)), limits=Limits(max_tool_calls=2), run=dict(RUN))
    s.check_budget()
    s.job_tool_calls = 2
    with pytest.raises(BudgetExhausted):
        s.check_budget()
    s.complete("failed", {"proposal_ids": [], "error": {"code": "BUDGET_EXHAUSTED", "message": "m"}})
    body = router.bodies("/complete")[0]
    assert not contract_errors("JobComplete", body)


def test_settings_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_BASE_URL=http://x:1/\nWORKER_TOKEN=w\nWORKSPACE_ACL=operator:a;worker:ws_a,ws_b\nAI_ENABLED=false\nLINEAR_ENABLED=true\nLINEAR_API_KEY=\n")
    s = load_settings(env, environ={})
    assert s.api_base_url == "http://x:1" and s.workspaces == ("ws_a", "ws_b") and not s.ai_enabled and not s.linear_configured
    assert s.ai_model == ""  # Rev 6: no default model
