"""ScriptedPlanner decides from returned facts. All ids here are invented (none from fixtures), so a pass shows the
logic does not key on seed/scenario ids. The fake responder only answers the 8 tool calls."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from promisegraph_worker.planner.base import PlanContext
from promisegraph_worker.planner.scripted import ScriptedPlanner

T = "2026-10-15T00:00:00Z"


def ev(eid: str, src: str, excerpt: str, loc: str = "body") -> dict[str, Any]:
    return {"id": eid, "source_id": src, "source_revision": 1, "origin": "synthetic", "excerpt": excerpt, "location": loc, "observed_at": T, "ingested_at": T, "stale": False}


def edge(t: str, a: str, b: str, e: str = "ev_g") -> dict[str, Any]:
    return {"id": f"{t}:{a}:{b}", "type": t, "from_id": a, "to_id": b, "evidence_ids": [e], "confirmed": True}


def node(i: str, label: str, title: str) -> dict[str, Any]:
    return {"id": i, "label": label, "title": title}


class World:
    def __init__(self) -> None:
        self.request = {"id": "rq_new", "customer_id": "cu_a", "subject": "", "body": "", "capability_ids": [], "linked_work_ids": [], "source_id": "sr_new", "version": 1}
        self.candidates: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.work: dict[str, dict[str, Any]] = {}
        self.validity = ["valid"]
        self.calls: list[dict[str, Any]] = []

    def cap(self, cid: str, title: str, score: float = 0.0, owner: str | None = None) -> None:
        self.nodes[cid] = node(cid, "Capability", title)
        if score:
            self.candidates.append({"entity_type": "capability", "id": cid, "title": title, "score": score, "evidence": []})
        if owner:
            self.nodes[owner] = node(owner, "Person", owner)
            self.edges.append(edge("OWNED_BY", cid, owner))

    def doc(self, sid: str, caps: list[str], text: str, score: float) -> None:
        self.nodes[sid] = node(sid, "Source", sid)
        self.candidates.append({"entity_type": "source", "id": sid, "title": sid, "score": score, "evidence": [ev(f"ev_{sid}", sid, text)]})
        self.edges += [edge("SUPPORTED_BY", c, sid) for c in caps]

    def item(self, wid: str, title: str, cap: str, status: str, score: float, owner: str | None = "pp_owner", depends: list[str] | None = None) -> None:
        self.work[wid] = {"id": wid, "workspace_id": "w", "title": title, "kind": "ticket", "status": status, "owner_id": owner, "connector": "local", "external_id": None,
                          "external_url": None, "forecast_at": None, "observed_at": "2026-10-14T00:00:00Z", "evidence_ids": [f"ev_{wid}"], "version": 1}
        self.nodes[wid] = node(wid, "WorkItem", title)
        if score:
            self.candidates.append({"entity_type": "work_item", "id": wid, "title": title, "score": score, "evidence": [ev(f"ev_{wid}", f"sr_{wid}", title)]})
        self.edges.append(edge("IMPLEMENTED_BY", cap, wid))
        if owner:
            self.edges.append(edge("OWNED_BY", wid, owner))
        for d in depends or []:
            self.edges.append(edge("DEPENDS_ON", wid, d))

    def prior(self, rid: str, title: str, cap: str, work: str | None, score: float, text: str) -> None:
        self.nodes[rid] = node(rid, "Request", title)
        self.candidates.append({"entity_type": "request", "id": rid, "title": title, "score": score, "evidence": [ev(f"ev_{rid}", f"sr_{rid}", text)]})
        self.edges.append(edge("ABOUT", rid, cap))
        if work:
            self.edges.append(edge("TRACKED_BY", rid, work))

    # -- tool responder -------------------------------------------------------------------------------------
    def respond(self, call: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(call)
        name, a = call["name"], call["arguments"]
        if name == "read_memory":
            data = {"context": {"request_id": self.request["id"], "based_on_revision": 5, "sources": [ev("ev_req", self.request["source_id"], self.request["body"] or "x")],
                                "paths": [], "related_work": [w for w in self.work.values() if w["id"] in self.request["linked_work_ids"]],
                                "decisions": [], "attempts": copy.deepcopy(self.attempts), "missing_information": [], "truncated": False},
                    "decisions": [d for d in self.decisions if any(d["scope"]["entity_id"] == s["entity_id"] for s in a.get("scopes", []))]}
            if a.get("work_item_ids"):
                data["work_items"] = [self.work[w] for w in a["work_item_ids"] if w in self.work]
            return self.ok(call, data)
        if name == "search_context":
            return self.ok(call, {"candidates": [c for c in self.candidates if c["entity_type"] in a["entity_types"]], "truncated": False})
        if name == "expand_context":
            ids = set(a["entity_ids"])
            es = [e for e in self.edges if (e["from_id"] in ids or e["to_id"] in ids) and e["type"] in a["relation_types"]]
            paths = [{"id": f"p{i}", "nodes": [self.nodes.get(e["from_id"], node(e["from_id"], "X", e["from_id"])), self.nodes.get(e["to_id"], node(e["to_id"], "X", e["to_id"]))],
                      "edges": [e], "claim_kind": "source_assertion", "evidence_ids": e["evidence_ids"]} for i, e in enumerate(es)]
            return self.ok(call, {"paths": paths, "nodes": list(self.nodes.values()), "truncated": False, "visited_count": len(paths)})
        if name == "prepare_action":
            validity = self.validity.pop(0) if self.validity else "valid"
            return self.ok(call, {"id": f"pr_{len(self.calls)}", "kind": a["kind"], "payload": a["payload"], "validity": validity, "validation_reasons": [] if validity == "valid" else ["changed"],
                                  "based_on_revision": a["based_on_revision"], "explanation": a["explanation"], "rejected_alternatives": a["rejected_alternatives"], "evidence_ids": a["evidence_ids"]})
        if name == "commit_action":
            return self.ok(call, {"action": {"id": "ac_1"}, "job_id": "jb_d"})
        if name == "inspect_action":
            return self.ok(call, self.action)
        if name == "record_observation":
            return self.ok(call, {"observation": {"id": "ob_1"}, "request": self.request})
        return {"call_id": call["call_id"], "ok": False, "data": None, "error": {"code": "INVALID_INPUT", "message": "?", "retryable": False, "details": {}}, "meta": {}}

    @staticmethod
    def ok(call: dict[str, Any], data: Any) -> dict[str, Any]:
        return {"call_id": call["call_id"], "ok": True, "data": data, "error": None, "meta": {"knowledge_revision": 5}}


def run(world: World, **ctx_kw) -> tuple[Any, PlanContext]:
    n = {"i": 0}
    replans: list[str] = []

    def new_id() -> str:
        n["i"] += 1
        return f"cl{n['i']:02d}"

    info = {"auto_commit_kinds": ["answer", "clarify"], "reference_time": "2026-10-16T04:30:00Z", "connectors": {"local": "enabled", "linear": "disabled"}}
    ctx = PlanContext(workspace_id="w", request=world.request, run={"id": "rn_1"}, info=info, new_call_id=new_id, replan=replans.append, **ctx_kw)
    gen = ScriptedPlanner().plan(ctx)
    result = None
    try:
        while True:
            call = gen.send(result) if result is not None else next(gen)
            assert call["name"] in {"search_context", "expand_context", "read_evidence", "read_memory", "prepare_action", "commit_action", "inspect_action", "record_observation"}
            assert set(call) == {"call_id", "name", "arguments"}
            result = world.respond(call)
    except StopIteration as stop:
        ctx.extra["replans"] = replans
        return stop.value, ctx


def proposal(world: World) -> dict[str, Any]:
    return [c for c in world.calls if c["name"] == "prepare_action"][-1]["arguments"]


@pytest.fixture()
def w() -> World:
    return World()


def test_answer_from_docs_and_verified_resolution(w: World):
    w.request.update(subject="How do I rotate widget keys?", body="How do I rotate the widget signing keys for our tenant?")
    w.cap("ca_widgets", "Widget keys", 0.3, owner="pp_x")
    w.doc("dc_widgets", ["ca_widgets"], "Rotate widget signing keys under Settings > Keys; old keys stay valid for 24 hours.", 0.6)
    w.prior("rq_old", "Rotate widget keys", "ca_widgets", None, 0.5, "how to rotate widget signing keys")
    w.attempts.append({"id": "at_ok", "request_id": "rq_old", "procedure_key": "rotate_keys_ui", "context_signature": "ca_widgets:v1", "scope": {"entity_type": "capability", "entity_id": "ca_widgets"},
                       "outcome": "succeeded", "verification": "passed", "summary": "Rotated keys via Settings.", "evidence_ids": ["ev_at_ok"], "observed_at": T})
    out, _ = run(w)
    p = proposal(w)
    assert p["kind"] == "answer" and "ev_dc_widgets" in p["evidence_ids"] and out.completion == "succeeded"
    assert [c["name"] for c in w.calls][-1] == "commit_action"  # answer is in auto_commit_kinds


def test_ambiguous_capability_asks_one_targeted_question(w: World):
    w.request.update(subject="Enable syncing", body="Please enable syncing for our org.")
    w.cap("ca_s1", "Contact syncing", 0.2)
    w.cap("ca_s2", "Calendar syncing", 0.2)
    w.doc("dc_sync", ["ca_s1", "ca_s2"], "Syncing comes in two flavours.", 0.4)
    out, _ = run(w)
    p = proposal(w)
    assert p["kind"] == "clarify" and "Contact syncing" in p["payload"]["question"] and "Calendar syncing" in p["payload"]["question"]
    assert p["payload"]["missing_fields"] == ["capability"] and out.completion == "succeeded"


def test_problem_report_links_work_tracked_by_similar_request_and_reads_state(w: World):
    w.request.update(subject="Gizmo uploads failing again", body="Our gizmo uploads are failing again since Monday.")
    w.cap("ca_g", "Gizmo uploads", 0.3)
    w.item("wk_fix", "Repair gizmo upload pipeline", "ca_g", "blocked", 0.1, depends=["wk_dep"])
    w.item("wk_dep", "Storage migration", "ca_g", "in_progress", 0.0, owner="pp_gone")
    w.prior("rq_prev", "Gizmo uploads failing", "ca_g", "wk_fix", 0.6, "gizmo uploads failing since monday")
    w.doc("dc_g", ["ca_g"], "Gizmo uploads accept files up to 2 GB.", 0.2)
    out, _ = run(w)
    p = proposal(w)
    assert p["kind"] == "link_work" and p["payload"] == {"work_item_id": "wk_fix", "comment_text": None}
    assert any(c["name"] == "read_memory" and c["arguments"].get("work_item_ids") for c in w.calls)  # refresh state
    assert "no date can be promised" in p["explanation"] and "wk_dep" in p["explanation"]
    assert out.completion == "waiting" and out.awaiting == "review"  # link needs an operator
    assert not any(c["name"] == "commit_action" for c in w.calls)


def test_done_work_link_states_verification_needed(w: World):
    w.request.update(subject="Umlauts broken in PDF", body="Umlauts render as boxes in the PDF report again.")
    w.cap("ca_pdf", "PDF reports", 0.3)
    w.item("wk_font", "PDF umlauts render as boxes", "ca_pdf", "done", 0.5)
    run(w)
    p = proposal(w)
    assert p["kind"] == "link_work" and "needs verification" in p["explanation"]


def test_create_work_rejects_similar_with_evidence_and_ruled_out_work(w: World):
    w.request.update(subject="Send reports to Teams", body="We want reports posted to a Teams channel instead of email.")
    w.cap("ca_rep", "Report delivery", 0.3, owner="pp_rep")
    w.item("wk_email", "Email digest for reports", "ca_rep", "open", 0.4)
    w.doc("dc_rep", ["ca_rep"], "Reports are delivered by email. Chat channels are not supported.", 0.5)
    out, _ = run(w)
    p = proposal(w)
    assert p["kind"] == "create_work" and p["payload"]["owner_id"] == "pp_rep" and p["payload"]["capability_id"] == "ca_rep"
    assert p["payload"]["acceptance_criteria"] and p["payload"]["connector"] == "local"
    alt = [a for a in p["rejected_alternatives"] if a["kind"] == "link_work"]
    assert alt and "wk_email" in alt[0]["reason"] and alt[0]["evidence_ids"]
    assert out.completion == "waiting"


def test_create_work_unassigned_when_owner_inactive(w: World):
    w.request.update(subject="Need XML export", body="We need an XML export of invoices.")
    w.cap("ca_inv", "Invoice export", 0.3, owner="pp_left")
    w.nodes["pp_left"] = node("pp_left", "Person", "Pat (inactive)")
    run(w)
    p = proposal(w)
    assert p["kind"] == "create_work" and p["payload"]["owner_id"] is None and "Unassigned" in p["payload"]["description"]


def test_conflicting_active_decisions_escalate_citing_both(w: World):
    w.request.update(subject="Extend trial", body="Please extend our trial by 30 days.")
    w.cap("ca_trial", "Trials", 0.4)
    for did, scope, val in (("dz_1", {"entity_type": "customer", "entity_id": "cu_a"}, True), ("dz_2", {"entity_type": "capability", "entity_id": "ca_trial"}, False)):
        w.decisions.append({"id": did, "workspace_id": "w", "rule_type": "date_extension_allowed", "value": val, "scope": scope, "status": "active", "rationale": "r", "author_id": "pp",
                            "effective_at": "2026-01-01T00:00:00Z", "expires_at": None, "supersedes_id": None, "evidence_ids": [f"ev_{did}"], "version": 1})
    run(w)
    p = proposal(w)
    assert p["kind"] == "escalate" and "incompatible" in p["explanation"] and {"ev_dz_1", "ev_dz_2"} <= set(p["evidence_ids"])


def test_expired_decision_is_not_applied(w: World):
    w.request.update(subject="Extend trial", body="Please extend our trial by 30 days.")
    w.cap("ca_trial", "Trials", 0.4)
    for did, val, exp in (("dz_1", True, "2026-01-01T00:00:00Z"), ("dz_2", False, None)):
        w.decisions.append({"id": did, "workspace_id": "w", "rule_type": "date_extension_allowed", "value": val, "scope": {"entity_type": "customer", "entity_id": "cu_a"}, "status": "active",
                            "rationale": "r", "author_id": "pp", "effective_at": "2025-01-01T00:00:00Z", "expires_at": exp, "supersedes_id": None, "evidence_ids": [], "version": 1})
    run(w)
    assert proposal(w)["kind"] != "escalate" or "incompatible" not in proposal(w)["explanation"]


def test_policy_disallows_create_escalates(w: World):
    w.request.update(subject="Need XML export", body="We need an XML export of invoices.")
    w.cap("ca_inv", "Invoice export", 0.3, owner="pp_x")
    w.decisions.append({"id": "dz_p", "workspace_id": "w", "rule_type": "allowed_action_types", "value": ["answer", "clarify", "escalate"], "scope": {"entity_type": "customer", "entity_id": "cu_a"},
                        "status": "active", "rationale": "freeze", "author_id": "pp", "effective_at": "2026-01-01T00:00:00Z", "expires_at": None, "supersedes_id": None, "evidence_ids": [], "version": 1})
    run(w)
    p = proposal(w)
    assert p["kind"] == "escalate" and "create_work" in p["payload"]["reason"]


def _scoped_failure_world(customer: str) -> World:
    w = World()
    w.request.update(customer_id=customer, subject="How do we stop duplicate pings?", body="How do we stop duplicate pings from the widget?")
    w.cap("ca_ping", "Widget pings", 0.3)
    w.doc("dc_ping", ["ca_ping"], "To stop duplicate pings, enable dedupe in the widget settings.", 0.6)
    w.attempts.append({"id": "at_fail", "request_id": "rq_x", "procedure_key": "enable_dedupe", "context_signature": "cu_a:ca_ping:v2", "scope": {"entity_type": "customer", "entity_id": "cu_a"},
                       "outcome": "failed", "verification": "failed", "summary": "Dedupe did not help for cu_a on v2.", "evidence_ids": ["ev_at_fail"], "observed_at": T})
    w.item("wk_ping", "Server-side dedupe of duplicate pings", "ca_ping", "open", 0.2)
    return w


def test_scoped_failed_workaround_changes_plan_only_for_that_customer():
    affected = _scoped_failure_world("cu_a")
    run(affected)
    pa = proposal(affected)
    assert pa["kind"] == "link_work" and any(a["kind"] == "answer" and "at_fail" in a["reason"] for a in pa["rejected_alternatives"])
    other = _scoped_failure_world("cu_b")
    run(other)
    assert proposal(other)["kind"] == "answer"  # another customer's failure does not apply


def test_forbidden_only_remedy_escalates_without_repeating_it(w: World):
    w.request.update(subject="Share board publicly", body="We want the board shared with partners.")
    w.cap("ca_share", "Board sharing", 0.4)
    w.attempts.append({"id": "at_pub", "request_id": "rq_p", "procedure_key": "open_link", "context_signature": "ca_share", "scope": {"entity_type": "capability", "entity_id": "ca_share"},
                       "outcome": "succeeded", "verification": "passed", "summary": "Used an open link.", "evidence_ids": [], "observed_at": T})
    w.decisions.append({"id": "dz_f", "workspace_id": "w", "rule_type": "forbidden_workaround", "value": "open_link", "scope": {"entity_type": "customer", "entity_id": "cu_a"}, "status": "active",
                        "rationale": "security", "author_id": "pp", "effective_at": "2026-01-01T00:00:00Z", "expires_at": None, "supersedes_id": None, "evidence_ids": ["ev_dz_f"], "version": 1})
    run(w)
    p = proposal(w)
    assert p["kind"] == "escalate" and "open_link" not in str(p["payload"])


def test_stale_proposal_replans_with_refreshed_context(w: World):
    w.request.update(subject="Need XML export", body="We need an XML export of invoices.")
    w.cap("ca_inv", "Invoice export", 0.3, owner="pp_x")
    w.validity = ["stale", "valid"]
    out, ctx = run(w)
    names = [c["name"] for c in w.calls]
    assert names.count("prepare_action") == 2 and ctx.extra["replans"] and names[names.index("prepare_action") + 1] == "read_memory"


def test_no_capability_clarifies_missing_field(w: World):
    w.request.update(subject="Help", body="It does not do the thing.")
    run(w)
    p = proposal(w)
    assert p["kind"] == "clarify" and p["payload"]["missing_fields"]


def test_continuation_observes_and_records_connector_status_only(w: World):
    w.request.update(linked_work_ids=["wk_done"])
    w.cap("ca_c", "C", 0)
    w.item("wk_done", "Thing", "ca_c", "done", 0)
    w.action = {"id": "ac_9", "kind": "link_work", "status": "succeeded", "receipt": {"target_id": "lnk"}, "connector": "local"}
    out, _ = run(w, resume_stage="observe", pending_action_id="ac_9")
    obs = [c for c in w.calls if c["name"] == "record_observation"][0]["arguments"]
    assert obs["kind"] == "connector_status" and obs["verification"] == "pending" and "NOT verified" in obs["body"]
    assert out.completion == "succeeded" and out.stage == "verify"


def test_continuation_after_clarify_reports_waiting(w: World):
    w.action = {"id": "ac_c", "kind": "clarify", "status": "succeeded", "receipt": {"target_id": "draft"}, "connector": "local"}
    out, _ = run(w, resume_stage="observe", pending_action_id="ac_c")
    assert out.run_status_hint == "waiting_for_input" and [c["name"] for c in w.calls] == ["inspect_action"]
