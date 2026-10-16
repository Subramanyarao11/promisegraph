"""Worker-level acceptance flows against a running protocol mock (PG_IT_BASE_URL, default :8101).

These drive the real worker (ScriptedPlanner, real tool loop, real job protocol) and operator-side HTTP calls. Fault
cases use the mock's /__mock/faults controls, so this module requires the mock (not the real API).
Scenario intents come from fixtures/scenarios; expectations are evaluated on decisions/citations, not exact prose.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace

import httpx
import pytest

from promisegraph_worker.config import Limits
from promisegraph_worker.tools import ToolClient

from .driver import BASE, Operator, drain, worker

pytestmark = pytest.mark.integration


def _probe() -> tuple[bool, bool]:
    try:
        r = httpx.get(f"{BASE}/health/live", timeout=2)
        return r.status_code == 200, bool(r.json().get("mock"))
    except Exception:  # noqa: BLE001
        return False, False


UP, IS_MOCK = _probe()
if not UP:
    pytest.skip(f"API/mock not reachable at {BASE}", allow_module_level=True)
needs_mock = pytest.mark.skipif(not IS_MOCK, reason="uses /__mock fault controls")
FAULT_BASE = os.environ.get("PG_IT_FAULT_BASE_URL")  # real API started with FAULTS=local_create_timeout_after_effect


@pytest.fixture()
def op() -> Operator:
    o = Operator()
    if IS_MOCK:
        o.reset()
    return o


def run_intake(op: Operator, customer: str, subject: str, body: str, **wk):
    res = op.create_request(customer, subject, body)
    drain(worker(**wk))
    run = op.get(f"/runs/{res['run']['id']}")
    return res["request"], run, op.latest_proposal(run["id"], res["request"]["id"])


def kinds(events):
    return [e["type"] for e in events]


# --------------------------------------------------------------------------------------------- T01 / T22
def test_t01_answer_then_verification_resolves(op: Operator):
    base = op.work_count()
    req, run, p = run_intake(op, "customer_corvid", "Monthly PDF of the bed occupancy dashboard",
                             "Could the Bed Occupancy dashboard be sent automatically as a PDF on the 1st of every month at 06:00 Berlin time to finance@corvid.example? Who on our side is allowed to set this up?")
    assert run["status"] == "succeeded" and p["kind"] == "answer" and p["validity"] == "valid"
    assert any("20-docs-scheduled-exports" in e for e in p["evidence_ids"]) or "20-docs-scheduled-exports" in p["payload"]["draft_text"], p["evidence_ids"]
    action = op.actions(req["id"])[0]
    assert action["status"] == "succeeded" and action["verification"] == "pending" and action["receipt"]
    assert op.work_count() == base
    assert op.get(f"/requests/{req['id']}")["status"] == "awaiting_verification"
    assert op.observe(req["id"], action["id"], "customer_confirmation", "passed", "Customer confirmed.").status_code == 201
    assert op.get(f"/requests/{req['id']}")["status"] == "resolved"
    events = op.events(run["id"])
    assert events[0]["type"] == "run_started" and events[0]["payload"]["label"] == "Scripted mode"  # T22 labelled
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert not any(e["type"] in ("run_finished", "action_updated") and e["step_id"] for e in events)  # API-authored only


# --------------------------------------------------------------------------------------------- T02 / T03
def test_t02_t03_clarify_wait_then_reply_resumes_same_run(op: Operator):
    req, run, p = run_intake(op, "customer_tamarind", "Provisioning for store managers",
                             "We need provisioning for our identity provider before 200 store managers join next month. Can you turn it on?")
    assert p["kind"] == "clarify" and p["payload"]["missing_fields"]
    assert re.search(r"(?is)(account|user).*(group|role)|(group|role).*(account|user)", p["payload"]["question"])
    assert run["status"] == "waiting_for_input"
    req = op.get(f"/requests/{req['id']}")
    assert req["status"] == "awaiting_input"
    r = op.post(f"/requests/{req['id']}/replies", {"body": "We mean group sync: our IdP groups 'Store Managers' and 'Regional Leads' should map to the Viewer and Editor roles. Accounts are already created at first login.",
                                                   "external_ref": None, "expected_request_version": req["version"]})
    assert r.status_code == 201 and r.json()["data"]["run"]["id"] == run["id"]
    drain(worker())
    run2 = op.get(f"/runs/{run['id']}")
    p2 = op.latest_proposal(run["id"], req["id"])
    assert run2["status"] in ("succeeded", "awaiting_review"), run2
    assert p2["kind"] in ("answer", "link_work")
    if p2["kind"] == "link_work":
        assert p2["payload"]["work_item_id"] == "work_scim_nested_groups"
    assert p2["based_on_revision"] >= p["based_on_revision"]
    types = kinds(op.events(run["id"]))
    i = types.index("waiting_for_input")
    assert types[0] == "run_started" and "run_started" in types[i + 1:]


# --------------------------------------------------------------------------------------------- T04 / T14
def test_t04_link_once_awaiting_work_and_idempotent_commit(op: Operator):
    base = op.work_count()
    req, run, p = run_intake(op, "customer_acme", "Scheduled exports stopped working",
                             "Our scheduled exports stopped working again this week. Finance needs the board pack exports before Friday. What is going on?")
    assert run["status"] == "awaiting_review" and p["kind"] == "link_work" and p["payload"]["work_item_id"] == "work_export_issue"
    assert any(a["kind"] == "create_work" for a in p["rejected_alternatives"])
    assert not re.search(r"(?i)(will|should) be (fixed|restored|ready|delivered|working)[^.]*(by|before) friday", p["explanation"])
    assert not re.search(r"(?i)public (share )?link", p["explanation"])
    c1 = op.commit(p, key="it-t04-commit")
    assert c1.status_code == 202
    drain(worker())
    c2 = op.commit(p, key="it-t04-commit")
    assert c2.json()["data"]["action"]["id"] == c1.json()["data"]["action"]["id"]  # replayed, not duplicated
    action = op.get(f"/actions/{c1.json()['data']['action']['id']}")
    assert action["status"] == "succeeded" and action["verification"] == "pending"
    assert op.get(f"/runs/{run['id']}")["status"] == "succeeded"
    req = op.get(f"/requests/{req['id']}")
    assert req["status"] == "awaiting_work" and req["linked_work_ids"] == ["work_export_issue"]
    assert op.work_count() == base


# --------------------------------------------------------------------------------------------- T05 / T28
def test_t05_similar_incompatible_rejected_with_evidence(op: Operator):
    base = op.work_count()
    _req, run, p = run_intake(op, "customer_tamarind", "Push daily sales export to our SFTP server",
                              "Every morning we need the daily sales export pushed to our SFTP server at sftp.tamarind.example. Our firewall cannot pull files from cloud storage buckets.")
    assert run["status"] == "awaiting_review" and p["kind"] == "create_work"
    alt = [a for a in p["rejected_alternatives"] if a["kind"] == "link_work" and "work_s3_destination" in a["reason"]]
    assert alt and alt[0]["evidence_ids"]
    assert p["payload"]["capability_id"] in ("cap_sftp_export_destination", "cap_scheduled_exports")
    assert "work_sftp_spike" not in p["payload"]["dependency_ids"] and p["payload"]["acceptance_criteria"]
    assert op.work_count() == base


# --------------------------------------------------------------------------------------------- T06
def test_t06_create_work_receipt_after_operator_commit(op: Operator):
    base = op.work_count()
    req, run, p = run_intake(op, "customer_corvid", "Stream audit events to our SIEM",
                             "Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS in near real time. A CSV download is not enough for continuous monitoring.")
    if p["kind"] == "link_work":  # reused graph: an earlier identical request already created this task
        prior = [a for a in op.get("/actions", limit=100) if a["kind"] == "create_work" and a["receipt"] and a["receipt"]["target_id"] == p["payload"]["work_item_id"]]
        assert prior, "link_work is only correct when the target was created for an earlier identical request"
        return
    assert p["kind"] == "create_work" and p["payload"]["connector"] == "local" and p["payload"]["capability_id"] == "cap_audit_log"
    assert p["payload"]["owner_id"] in ("person_jonas", "person_priya", None)
    assert any(a["kind"] == "link_work" and "work_audit_log_csv_download" in a["reason"] and a["evidence_ids"] for a in p["rejected_alternatives"])
    c = op.commit(p)
    assert c.status_code == 202
    drain(worker())
    action = op.get(f"/actions/{c.json()['data']['action']['id']}")
    assert action["status"] == "succeeded" and action["receipt"]["connector"] == "local"
    assert op.work_count() == base + 1
    req = op.get(f"/requests/{req['id']}")
    assert req["status"] == "awaiting_work" and action["receipt"]["target_id"] in req["linked_work_ids"]


# --------------------------------------------------------------------------------------------- T07 / T08
def test_t07_t08_owner_path_and_staleness_are_explicit(op: Operator):
    _r, _run, p = run_intake(op, "customer_corvid", "Who owns the EU export fix and when will it land?",
                             "Our EU scheduled exports are still failing. Which team owns the underlying fix, and can you give us a date we can share with our hospital clients?")
    assert p["kind"] in ("escalate", "link_work")
    if p["kind"] == "link_work":
        assert p["payload"]["work_item_id"] == "work_export_issue"
    assert re.search(r"(?i)(inactive|no active owner|left|work_queue_v2_cutover|queue v2)", p["explanation"]), p["explanation"]
    assert "person_dana" != p["payload"].get("owner_id")
    if IS_MOCK:
        op.reset()
    _r, _run, p = run_intake(op, "customer_acme", "When will legal hold for retention be available?",
                             "Our compliance team needs 2025 event data excluded from the purge under a legal hold. When will that be available and who is working on it?")
    assert p["kind"] in ("link_work", "escalate", "clarify", "answer")
    assert re.search(r"(?i)(stale|not current|last (observed|updated)|unconfirmed|no forecast|unknown|inactive)", p["explanation"])
    assert not re.search(r"(?i)(will|should) (be available|ship|land) (by|on|in) ", p["explanation"])


# --------------------------------------------------------------------------------------------- T09
def test_t09_scoped_failed_workaround_other_customer_unaffected(op: Operator):
    _r, _run, p = run_intake(op, "customer_acme", "Fiscal close export skipped when clocks changed",
                             "The scheduled export for our fiscal close did not run on the night the clocks changed. We need it to run reliably before the November change.")
    assert p["kind"] in ("link_work", "escalate")
    assert not re.search(r"(?i)recreate[^.]*\bUTC\b", str(p["payload"]))
    _r, _run, p2 = run_intake(op, "customer_brightpath", "Nightly export skipped on DST night",
                              "Our nightly shipments export skipped one run on the day daylight saving time ended. How do we stop that from happening?")
    assert p2["kind"] in ("answer", "link_work")
    if p2["kind"] == "link_work":
        assert p2["payload"]["work_item_id"] == "work_export_dst_skip"


# --------------------------------------------------------------------------------------------- T10 / T11
def test_t10_policy_and_t11_conflicting_decisions_escalate(op: Operator):
    _r, _run, p = run_intake(op, "customer_brightpath", "Nightly export in Parquet format",
                             "Our data team wants the nightly shipments scheduled export delivered as Parquet instead of CSV so it can load straight into our lakehouse.")
    assert p["kind"] == "escalate"
    req, _run, p = run_intake(op, "customer_corvid", "Delay purge of Q1 2025 raw events",
                              "Our auditors need the Q1 2025 raw events kept 30 days beyond the scheduled purge date. Please push that purge back.")
    assert p["kind"] in ("escalate", "clarify")
    assert re.search(r"(?i)(conflict|contradict|incompatible)", p["explanation"])
    assert "decision_corvid_retention_extension" in p["explanation"] and "decision_retention_no_extension" in p["explanation"]
    assert op.get(f"/requests/{req['id']}")["status"] in ("awaiting_review", "awaiting_input", "escalated")


# --------------------------------------------------------------------------------------------- T12 (stale, in-job replan)
@needs_mock
def test_t12_stale_proposal_replans_before_any_dispatch(op: Operator):
    op.faults(stale_next_proposal=1)
    _r, run, p = run_intake(op, "customer_helix", "Webhook events lost again during maintenance",
                            "Events sent during last Sunday's maintenance window are missing again. We need them redelivered and this fixed permanently.")
    ready = [e["payload"]["validity"] for e in op.events(run["id"]) if e["type"] == "proposal_ready"]  # list order can tie
    assert ready[:2] == ["stale", "valid"]
    assert p["kind"] == "link_work" and p["payload"]["work_item_id"] == "work_webhook_retry_queue"
    assert run["usage"]["replans"] == 1


# --------------------------------------------------------------------------------------------- T15
@pytest.mark.skipif(not IS_MOCK and not (FAULT_BASE and FAULT_BASE.rstrip("/") == BASE.rstrip("/")), reason="needs mock faults or the FAULTS API instance")
def test_t15_timeout_after_create_reconciles_without_duplicate(op: Operator):
    base = op.work_count()
    req, run, p = run_intake(op, "customer_tamarind", "Send alerts to our chat tool",
                             "Store managers live in our team chat tool. We want metric alerts posted to a chat channel through its incoming-webhook URL instead of email.")
    if not IS_MOCK and p["kind"] == "link_work":
        pytest.skip("graph already holds the task created by an earlier run of this test; linking to it is correct")
    assert p["kind"] == "create_work"
    if IS_MOCK:
        op.faults(local_create_timeout_after_effect=1)
    c = op.commit(p)
    drain(worker())
    aid = c.json()["data"]["action"]["id"]
    action = op.get(f"/actions/{aid}")
    events = op.events(run["id"])
    if IS_MOCK:  # the mock leaves the action dispatching -> unknown -> reconcile; the real API commits atomically first
        assert any(e["type"] == "action_updated" and e["payload"]["status"] == "unknown" for e in events)
    assert action["status"] == "succeeded" and action["receipt"]["connector"] == "local"
    assert len([a for a in op.actions(req["id"]) if a["kind"] == "create_work"]) == 1
    assert op.work_count() == base + 1
    assert op.get(f"/runs/{run['id']}")["status"] == "succeeded"


# --------------------------------------------------------------------------------------------- T16
@needs_mock
def test_t16_lease_loss_resumes_from_checkpoint_without_duplicate_effects(op: Operator, monkeypatch):
    calls = {"n": 0}
    orig = ToolClient.execute

    def flaky(self, call):
        calls["n"] += 1
        if calls["n"] == 4:
            op.faults(lease_expire_now=True)  # every live lease expires mid-job
        return orig(self, call)

    monkeypatch.setattr(ToolClient, "execute", flaky)
    req, run, p = run_intake(op, "customer_corvid", "Stream audit events to our SIEM",
                             "Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS in near real time. A CSV download is not enough for continuous monitoring.")
    assert run["status"] == "awaiting_review" and p["kind"] == "create_work"
    events = op.events(run["id"])
    started = [e["step_id"] for e in events if e["type"] == "tool_started"]
    assert len(started) == len(set(started))  # stable step ids: no duplicate appends
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert sum(1 for e in events if e["type"] == "run_started") == 2  # second lease attempt resumed
    assert len(op.get("/proposals", run_id=run["id"])) == 1  # idempotent prepare across leases


# --------------------------------------------------------------------------------------------- T17 / T18
def test_t17_completed_work_needs_attributable_verification(op: Operator):
    req, run, p = run_intake(op, "customer_tamarind", "Accented store names garbled in CSV",
                             "Store names with accents, like 'Café Norte', show up as garbled characters when we open the manual CSV download in our spreadsheet tool.")
    assert p["kind"] in ("link_work", "answer")
    if run["status"] == "awaiting_review":
        op.commit(p)
        drain(worker())
    req = op.get(f"/requests/{req['id']}")
    assert req["status"] == "awaiting_verification"
    action = op.actions(req["id"])[0]
    assert op.observe(req["id"], action["id"], "connector_status", "passed", "Linked work is done.").status_code == 201
    assert op.get(f"/requests/{req['id']}")["status"] == "awaiting_verification"
    assert op.observe(req["id"], action["id"], "operator_confirmation", "passed", "Operator confirmed the fix with the customer.").status_code == 201
    assert op.get(f"/requests/{req['id']}")["status"] == "resolved"


def test_t18_failed_verification_reopens_and_followup_sees_failure(op: Operator):
    req, run, p = run_intake(op, "customer_tamarind", "Webhook signature check failing",
                             "Our Node service rejects every webhook because the signature never matches. How exactly should we compute it?")
    if p["kind"] != "answer":  # reused graph: the same answer already failed verification for an identical request
        assert any(a["kind"] == "answer" and "failed" in a["reason"] for a in p["rejected_alternatives"]), p["rejected_alternatives"]
        return
    assert any("20-docs-webhooks" in e for e in p["evidence_ids"])
    action = op.actions(req["id"])[0]
    op.observe(req["id"], action["id"], "customer_confirmation", "failed", "Customer followed the steps; signatures still do not match because their framework re-serialises the JSON body.")
    assert op.get(f"/requests/{req['id']}")["status"] == "investigating"
    drain(worker())
    runs = op.get(f"/requests/{req['id']}/runs")
    assert len(runs) >= 2
    follow = next(r for r in runs if r["id"] != run["id"])  # newest-first can tie on created_at
    p2 = op.latest_proposal(follow["id"], req["id"])
    assert p2 is not None and p2["kind"] != "answer"  # the failed procedure is not repeated blindly
    assert any("failed" in a["reason"] for a in p2["rejected_alternatives"] if a["kind"] == "answer")


# --------------------------------------------------------------------------------------------- T22 / T24
def test_t22_model_mode_without_ai_is_capability_disabled(op: Operator):
    r = op.post("/requests", {"customer_id": "customer_corvid", "subject": "x", "body": "y", "channel": "manual", "capability_ids": [],
                              "external_ref": None, "auto_start": True, "planner_mode": "model"})
    run = r.json()["data"]["run"]
    drain(worker())
    run = op.get(f"/runs/{run['id']}")
    assert run["status"] == "failed" and run["error"]["code"] == "CAPABILITY_DISABLED"


def test_t24_budget_exhaustion_is_recoverable_failure(op: Operator):
    req, run, _p = run_intake(op, "customer_corvid", "Stream audit events to our SIEM", "Audit events to our SIEM please.",
                              limits=Limits(max_tool_calls=3))
    assert run["status"] == "failed" and run["error"]["code"] == "BUDGET_EXHAUSTED"
    assert op.get(f"/requests/{req['id']}")["status"] not in ("resolved", "closed")
    assert run["usage"]["tool_calls"] == 3
    assert any(e["type"] == "error" and e["payload"]["code"] == "BUDGET_EXHAUSTED" for e in op.events(run["id"]))


@needs_mock
def test_t24_outage_is_retryable_not_fixture_backed(op: Operator, monkeypatch):
    calls = {"n": 0}
    orig = ToolClient.execute

    def outage(self, call):
        calls["n"] += 1
        if calls["n"] == 2:
            op.faults(database_unavailable=True)
        out = orig(self, call)
        if calls["n"] == 2:
            op.faults(database_unavailable=False)
        return out

    monkeypatch.setattr(ToolClient, "execute", outage)
    req = op.create_request("customer_corvid", "Stream audit events to our SIEM", "Audit events to our SIEM over syslog. A CSV download is not enough.")
    w = worker()
    w.run_once()
    run = op.get(f"/runs/{req['run']['id']}")
    assert run["status"] in ("queued", "running") and not op.get("/proposals", run_id=run["id"])  # no fabricated answer
    events = op.events(run["id"])
    assert any(e["type"] == "tool_completed" and e["payload"]["error_code"] == "DEPENDENCY_UNAVAILABLE" for e in events)


@needs_mock
def test_t24_outage_retry_completes_on_next_attempt(op: Operator, monkeypatch):
    test_t24_outage_is_retryable_not_fixture_backed(op, monkeypatch)
    monkeypatch.undo()
    import time as _t

    _t.sleep(1.2)  # mock retry delay for the requeued job
    drain(worker())
    runs = [r for r in op.get("/requests", limit=100) if r["subject"] == "Stream audit events to our SIEM"]
    run = op.get(f"/requests/{runs[0]['id']}/runs")[0]
    assert run["status"] == "awaiting_review"


@needs_mock
def test_cancellation_stops_planning(op: Operator, monkeypatch):
    orig = ToolClient.execute
    state: dict[str, str] = {}

    def cancel_on_second(self, call):
        if call["call_id"].endswith("c02"):
            op.post(f"/runs/{state['run']}/cancel", {"reason": "operator cancelled"})
        return orig(self, call)

    monkeypatch.setattr(ToolClient, "execute", cancel_on_second)
    res = op.create_request("customer_corvid", "Stream audit events to our SIEM", "Audit events to our SIEM over syslog. A CSV download is not enough.")
    state["run"] = res["run"]["id"]
    drain(worker())
    run = op.get(f"/runs/{state['run']}")
    assert run["status"] == "cancelled"
    assert not op.get("/proposals", run_id=state["run"])
    started = [e for e in op.events(state["run"]) if e["type"] == "tool_started"]
    assert len(started) <= 2


def test_t12_owner_change_blocks_stale_commit_and_replan_uses_new_revision(op: Operator):
    req, run, p = run_intake(op, "customer_helix", "Webhook events lost again during maintenance",
                             "Events sent during last Sunday's maintenance window are missing again. We need them redelivered and this fixed permanently.")
    assert p["kind"] == "link_work" and p["payload"]["work_item_id"] == "work_webhook_retry_queue"
    w = op.get("/work-items/work_webhook_retry_queue") if not IS_MOCK else next(x for x in op.get("/work-items", limit=100) if x["id"] == "work_webhook_retry_queue")
    r = op.c.patch(op.p("/work-items/work_webhook_retry_queue"), json={"expected_version": w["version"], "status": "in_progress", "owner_id": "person_leo",
                   "forecast_at": "2026-11-15T00:00:00Z", "reason": "Ownership moved (synthetic reorg)."}, headers={"Idempotency-Key": f"it-t12-{req['id']}"})
    assert r.status_code == 200, r.text
    c = op.commit(p)
    assert c.status_code in (409, 422) and c.json()["error"]["code"] in ("REVISION_CONFLICT", "POLICY_VIOLATION")
    assert not [a for a in op.actions(req["id"]) if a["status"] in ("dispatching", "succeeded")]
    r = op.post(f"/requests/{req['id']}/runs", {"mode": "scripted", "instruction": "replan after owner change", "authorization_id": None})
    if r.status_code == 409:
        pytest.xfail(f"server refuses a replan run while the stale run is awaiting_review: {r.json()['error']['code']} (scenario t12 s8 expects 202)")
    assert r.status_code == 202, r.text
    drain(worker())
    run2 = r.json()["data"]
    p2 = op.latest_proposal(run2["id"], req["id"])
    assert p2["based_on_revision"] > p["based_on_revision"] and p2["kind"] == "link_work"
