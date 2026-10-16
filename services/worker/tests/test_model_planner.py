"""ModelPlanner + spend guard (Rev 6 #38-#40). httpx.MockTransport only: no real provider calls."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from promisegraph_worker.config import Settings, load_settings
from promisegraph_worker.http import Backoff
from promisegraph_worker.planner.base import CapabilityDisabled, PlanContext
from promisegraph_worker.planner.model import ModelPlanner, compact, tool_result_text
from promisegraph_worker.planner.spend import BudgetExhausted, Prices, SpendConfigError, SpendGuard
from promisegraph_worker.worker import Worker

from .conftest import no_sleep

PRICES = Prices(input_per_mtok=2.0, output_per_mtok=8.0)


def guard(tmp_path: Path, budget: float = 8.0, max_out: int = 1500) -> SpendGuard:
    return SpendGuard(budget_usd=budget, prices=PRICES, max_output_tokens=max_out, ledger_path=tmp_path / "ledger.json")


def oa_reply(tool_calls: list[dict[str, Any]] | None = None, text: str | None = None, pin: int = 1000, pout: int = 100) -> httpx.Response:
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"tc_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(tool_calls)]
    return httpx.Response(200, json={"id": "chatcmpl-x", "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                                     "usage": {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout}})


class Script:
    """Sequence of canned provider responses; records requests."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        assert request.url.host in ("api.openai.com", "api.anthropic.com")
        return self.responses.pop(0)


def planner(tmp_path: Path, script: Script, *, budget: float = 8.0, provider: str = "openai", max_calls: int = 12, g: SpendGuard | None = None) -> ModelPlanner:
    return ModelPlanner(enabled=True, provider=provider, api_key="sk-test", model="test-model", guard=g or guard(tmp_path, budget),
                        max_calls_per_run=max_calls, max_output_tokens=1500, transport=httpx.MockTransport(script), backoff=Backoff(sleep=no_sleep))


def drive(p: ModelPlanner, tool_responder, run_id: str = "rn_1"):
    n = {"i": 0}
    usage: list[dict[str, Any]] = []

    def new_id() -> str:
        n["i"] += 1
        return f"c{n['i']}"

    ctx = PlanContext(workspace_id="w", request={"id": "rq_1", "customer_id": "cu", "subject": "s", "body": "b", "capability_ids": [], "linked_work_ids": []},
                      run={"id": run_id}, info={"auto_commit_kinds": ["answer", "clarify"]}, new_call_id=new_id, replan=lambda r: None,
                      extra={"emit_model_usage": usage.append})
    calls = []
    gen = p.plan(ctx)
    result = None
    try:
        while True:
            call = gen.send(result) if result is not None else next(gen)
            calls.append(call)
            result = tool_responder(call)
    except StopIteration as stop:
        return stop.value, calls, usage


def ok(call, data):
    return {"call_id": call["call_id"], "ok": True, "data": data, "error": None, "meta": {}}


EV = {"id": "ev_doc_1", "source_id": "src_doc", "source_revision": 1, "origin": "synthetic", "excerpt": "Docs say X.", "location": "md:L1-L2",
      "observed_at": "2026-10-16T00:00:00Z", "ingested_at": "2026-10-16T00:00:00Z", "stale": False}


def responder(call):
    if call["name"] == "search_context":
        return ok(call, {"candidates": [{"entity_type": "source", "id": "src_doc", "title": "Doc", "score": 0.5, "evidence": [EV]}], "truncated": False})
    if call["name"] == "prepare_action":
        return ok(call, {"id": "pr_1", "kind": call["arguments"]["kind"], "validity": "valid", "based_on_revision": 3})
    if call["name"] == "commit_action":
        return ok(call, {"action": {"id": "ac_1"}, "job_id": "jb"})
    return ok(call, {})


ANSWER = {"kind": "answer", "payload": {"draft_text": "X", "resolution_criteria": ["confirmed"]}, "evidence_ids": ["ev_doc_1"], "explanation": "doc", "rejected_alternatives": [], "based_on_revision": 3}


# --------------------------------------------------------------------------------------------- loop
def test_tool_call_loop_uses_same_tools_and_records_usage(tmp_path):
    script = Script([
        oa_reply([("search_context", {"query": "x", "entity_types": ["source"], "customer_id": None, "limit": 5})]),
        oa_reply([("prepare_action", ANSWER)]),
        oa_reply([("commit_action", {"proposal_id": "pr_1", "expected_revision": 3})]),
        oa_reply(text="Answered from docs."),
    ])
    out, calls, usage = drive(planner(tmp_path, script), responder)
    assert [c["name"] for c in calls] == ["search_context", "prepare_action", "commit_action"]
    assert out.completion == "succeeded" and out.action_ids == ["ac_1"]
    body = script.requests[0]
    assert body["model"] == "test-model" and body["max_completion_tokens"] == 1500 and body["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in body["tools"]} == {"search_context", "expand_context", "read_evidence", "read_memory", "prepare_action",
                                                               "commit_action", "inspect_action", "record_observation"}
    assert script.requests[1]["messages"][-1]["role"] == "tool"  # tool results go back by tool_call_id
    assert len(usage) == 4 and all(set(u) >= {"call_id", "ok", "input_tokens", "output_tokens", "cost_usd", "budget_remaining_usd"} for u in usage)
    assert usage[0]["cost_usd"] == pytest.approx(1000 * 2 / 1e6 + 100 * 8 / 1e6)
    assert not any("messages" in json.dumps(u) or "content" in u for u in usage)  # no prompts stored


def test_invalid_citation_rejected_locally_before_api(tmp_path):
    bad = {**ANSWER, "evidence_ids": ["ev_made_up"]}
    script = Script([oa_reply([("prepare_action", bad)]), oa_reply(text="giving up")])
    out, calls, _usage = drive(planner(tmp_path, script), responder)
    assert calls == []  # nothing reached the tools/API
    tool_msg = script.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "EVIDENCE_INVALID" in tool_msg["content"] and out.completion == "failed"


def test_malformed_arguments_are_tool_errors_not_crashes(tmp_path):
    script = Script([httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "search_context", "arguments": "{not json"}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}), oa_reply(text="done")])
    out, calls, _ = drive(planner(tmp_path, script), responder)
    assert calls == [] and "INVALID_INPUT" in script.requests[1]["messages"][-1]["content"]


# --------------------------------------------------------------------------------------------- spend guard
def test_budget_refusal_at_the_boundary_makes_no_call(tmp_path):
    g = guard(tmp_path, budget=0.02)
    reservation = PRICES.cost(1000, 1500)  # 0.002 + 0.012 = 0.014
    rid, _ = g.reserve(1000, run_id="r0")
    g.settle(rid, 1000, 1500)  # spent 0.014; remaining 0.006 < next worst case
    script = Script([])
    out, calls, usage = drive(planner(tmp_path, script, g=g), responder)
    assert script.requests == [] and calls == []  # refused before any HTTP
    assert out.completion == "failed" and out.error["code"] == "CAPABILITY_DISABLED" and out.error["details"]["reason"] == "AI_BUDGET_EXHAUSTED"
    assert usage and usage[0]["ok"] is False and usage[0]["error_code"] == "AI_BUDGET_EXHAUSTED"
    assert reservation == pytest.approx(0.014)


def test_reservation_exactly_at_budget_is_allowed_and_one_cent_over_is_refused(tmp_path):
    g = guard(tmp_path, budget=0.014)
    rid, res = g.reserve(1000, run_id="r")  # exactly equals the budget: allowed (spent + reservation > budget refuses)
    assert res == pytest.approx(0.014)
    with pytest.raises(BudgetExhausted):
        g.reserve(1, run_id="r")  # open reservation still counted
    g.settle(rid, 10, 10)  # actual usage was tiny: the freed budget allows the next worst-case reservation
    _rid2, res2 = g.reserve(1, run_id="r")
    assert res2 == pytest.approx(PRICES.cost(1, 1500))


def test_ledger_survives_restart_and_orphaned_reservations_count(tmp_path):
    g1 = guard(tmp_path)
    rid, _ = g1.reserve(2000, run_id="r1")
    g1.settle(rid, 2000, 500)
    orphan, orphan_usd = g1.reserve(1000, run_id="r1")  # process "crashes" before settle
    spent = 2000 * 2 / 1e6 + 500 * 8 / 1e6
    g2 = guard(tmp_path)  # restart: new process, same ledger file
    assert g2.remaining() == pytest.approx(8.0 - spent - orphan_usd)
    assert g2.run_calls("r1") == 2
    state = json.loads((tmp_path / "ledger.json").read_text())
    assert state["spent_usd"] == pytest.approx(spent) and orphan in state["open_reservations"]


def test_actual_usage_replaces_reservation(tmp_path):
    g = guard(tmp_path)
    rid, reserved = g.reserve(5000, run_id="r")
    cost, remaining = g.settle(rid, 1200, 80)
    assert cost < reserved and remaining == pytest.approx(8.0 - cost)


def test_provider_4xx_releases_and_timeout_keeps_reservation(tmp_path):
    g = guard(tmp_path)
    script = Script([httpx.Response(400, json={"error": {"message": "bad request", "type": "invalid_request_error", "code": None}})])
    out, _, usage = drive(planner(tmp_path, script, g=g), responder)
    assert out.error["code"] == "PROVIDER_ERROR" and g.remaining() == pytest.approx(8.0) and usage[0]["ok"] is False

    def timeout(req):
        raise httpx.ReadTimeout("t", request=req)

    p2 = ModelPlanner(enabled=True, provider="openai", api_key="k", model="m", guard=g, transport=httpx.MockTransport(timeout), backoff=Backoff(sleep=no_sleep))
    out2, _, _ = drive(p2, responder, run_id="rn_2")
    assert out2.completion == "retryable_failure" and g.remaining() < 8.0  # billed-unknown: kept as spent


def test_rate_limit_and_5xx_back_off_then_succeed(tmp_path):
    sleeps: list[float] = []
    script = Script([httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "slow down", "type": "requests"}}),
                     httpx.Response(503, json={"error": {"message": "overloaded"}}),
                     oa_reply(text="done after backoff")])
    p = ModelPlanner(enabled=True, provider="openai", api_key="k", model="m", guard=guard(tmp_path), transport=httpx.MockTransport(script), backoff=Backoff(sleep=sleeps.append))
    out, _, usage = drive(p, responder)
    assert len(script.requests) == 3 and sleeps[0] == 3.0 and usage[-1]["ok"] is True and out.error["code"] == "NO_PROPOSAL"


def test_error_object_in_http_200_is_an_error(tmp_path):
    script = Script([httpx.Response(200, json={"error": {"message": "model not found", "type": "invalid_request_error"}})])
    out, _, _ = drive(planner(tmp_path, script), responder)
    assert out.error["code"] == "PROVIDER_ERROR" and "model not found" in out.error["message"]


def test_max_calls_per_run_enforced(tmp_path):
    script = Script([oa_reply([("search_context", {"query": "x", "entity_types": ["source"], "customer_id": None, "limit": 5})]) for _ in range(3)])
    out, calls, _ = drive(planner(tmp_path, script, max_calls=2), responder)
    assert len(script.requests) == 2 and out.error["code"] == "BUDGET_EXHAUSTED"


def test_anthropic_adapter_behind_same_interface(tmp_path):
    script = Script([httpx.Response(200, json={"content": [{"type": "tool_use", "id": "tu1", "name": "prepare_action", "input": {**ANSWER, "evidence_ids": []}}],
                                               "stop_reason": "tool_use", "usage": {"input_tokens": 50, "output_tokens": 20}}),
                     httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 60, "output_tokens": 5}})])
    out, calls, usage = drive(planner(tmp_path, script, provider="anthropic"), responder)
    assert calls[0]["name"] == "prepare_action" and script.requests[0]["max_tokens"] == 1500 and usage[0]["input_tokens"] == 50
    assert script.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_compact_context_trims_excerpts_but_keeps_ids():
    big = {"candidates": [{"id": f"c{i}", "evidence": [{**EV, "excerpt": "x" * 2000}]} for i in range(30)]}
    text = tool_result_text({"ok": True, "data": big, "error": None})
    assert len(text) <= 6100 and "c0" in text
    assert len(compact("y" * 1000)) < 410


# --------------------------------------------------------------------------------------------- configuration
def test_disabled_ai_constructs_nothing_and_makes_no_calls(tmp_path):
    called: list[Any] = []
    with pytest.raises(CapabilityDisabled):
        ModelPlanner(enabled=False, provider="openai", api_key="k", model="m", guard=guard(tmp_path), transport=httpx.MockTransport(lambda r: called.append(r)))
    w = Worker(Settings(api_base_url="http://api", worker_token="w", ai_enabled=False), model_transport=httpx.MockTransport(lambda r: called.append(r)))
    assert "model" not in w.investigate.planners and not called


@pytest.mark.parametrize("env,needle", [
    ({"AI_PRICE_INPUT_PER_MTOK": ""}, "PRICE"),
    ({"AI_BUDGET_USD": "10.5"}, "AI_BUDGET_USD"),
    ({"AI_MODEL": ""}, "AI_MODEL"),
])
def test_model_mode_refuses_to_start_without_guard_config(tmp_path, env, needle):
    base = {"AI_ENABLED": "true", "AI_PROVIDER": "openai", "AI_MODEL": "m", "AI_API_KEY": "k", "AI_BUDGET_USD": "8",
            "AI_PRICE_INPUT_PER_MTOK": "2", "AI_PRICE_OUTPUT_PER_MTOK": "8", "AI_LEDGER_PATH": str(tmp_path / "l.json")}
    s = load_settings(tmp_path / "none.env", environ={**base, **env})
    with pytest.raises(SpendConfigError) as e:
        Worker(s)
    assert needle in str(e.value)


def test_relative_ledger_path_resolves_against_repo_root(tmp_path):
    s = load_settings(tmp_path / "none.env", environ={"AI_LEDGER_PATH": ".ai-ledger.json"})
    assert s.ledger_path == Path(__file__).resolve().parents[3] / ".ai-ledger.json"
    assert replace(s, ai_ledger_path="/abs/x.json").ledger_path == Path("/abs/x.json")


def test_guard_rejects_budget_over_operator_ceiling(tmp_path):
    with pytest.raises(SpendConfigError):
        guard(tmp_path, budget=10.01)
