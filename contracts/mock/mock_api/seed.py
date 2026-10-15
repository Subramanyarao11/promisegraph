"""Built-in scenario derived from fixtures/contracts/examples.json, plus optional fixtures/seed/<ws>/ import."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from . import importer
from .common import REFERENCE_TIME, REPO_ROOT, canonical, sha256
from .state import Workspace

EXAMPLES = REPO_ROOT / "fixtures" / "contracts" / "examples.json"
SEED_DIR = REPO_ROOT / "fixtures" / "seed"
R = REFERENCE_TIME


def _examples() -> dict:
    return json.loads(EXAMPLES.read_text())


def _by_schema(ex: dict, schema: str) -> list[dict]:
    return [copy.deepcopy(e["data"]) for e in ex["resource_examples"] if e["schema"] == schema]


def seed_demo(ws: Workspace) -> None:
    ex = _examples()
    one = lambda s: _by_schema(ex, s)[0]  # noqa: E731

    cust = one("Customer")
    ws.customers[cust["id"]] = {**cust, "_plan": "enterprise", "_constraints": []}
    ws.add_node(cust["id"], "Customer", cust["name"])

    ws.people.update({
        "person_maya": {"id": "person_maya", "name": "Maya (synthetic)", "role": "Engineering lead", "team_id": "team_platform", "active": True},
        "person_ops": {"id": "person_ops", "name": "Ops (synthetic)", "role": "Support operations", "team_id": "team_support", "active": True},
        "person_lee": {"id": "person_lee", "name": "Lee (synthetic, inactive)", "role": "Former engineer", "team_id": "team_platform", "active": False},
    })
    ws.teams.update({"team_platform": {"id": "team_platform", "name": "Platform (synthetic)"},
                     "team_support": {"id": "team_support", "name": "Support (synthetic)"}})
    for p in ws.people.values():
        ws.add_node(p["id"], "Person", p["name"])
    for t in ws.teams.values():
        ws.add_node(t["id"], "Team", t["name"])

    ws.capabilities["cap_scheduled_exports"] = {
        "id": "cap_scheduled_exports", "name": "Scheduled exports", "aliases": ["scheduled export", "export schedule"],
        "status": "active", "product_version": "4.2", "owner_id": "person_maya", "version": 1, "_customer_ids": [cust["id"]]}
    ws.capabilities["cap_sso_login"] = {
        "id": "cap_sso_login", "name": "SSO login", "aliases": ["single sign-on", "saml login"], "status": "beta",
        "product_version": None, "owner_id": None, "version": 1, "_customer_ids": [cust["id"]]}
    for c in ws.capabilities.values():
        ws.add_node(c["id"], "Capability", c["name"])

    # sources and evidence exactly as in the examples
    for s in _by_schema(ex, "SourceRecord"):
        ws.sources[s["id"]] = [s]
        ws.add_node(s["id"], "Source", s["title"])
    bundle = one("ContextBundle")
    for ev in bundle["sources"]:
        src = ws.sources[ev["source_id"]][0]
        ws.evidence[ev["id"]] = {**ev, "_kind": src["kind"]}

    w = one("WorkItem")
    w.update(_description="Scheduled exports are blocked by the export worker migration.",
             _capability_ids=["cap_scheduled_exports"], _depends_on=["work_export_migration"], _acceptance_criteria=["Scheduled exports run on schedule"])
    ws.work_items[w["id"]] = w
    ws.work_items["work_export_migration"] = {
        "id": "work_export_migration", "workspace_id": ws.id, "title": "Export worker migration", "kind": "feature",
        "status": "in_progress", "owner_id": "person_maya", "connector": "local", "external_id": None, "external_url": None,
        "forecast_at": None, "observed_at": R, "evidence_ids": ["ev_migration_owner_1"], "version": 1,
        "_description": "Migrate the export worker.", "_capability_ids": [], "_depends_on": [], "_acceptance_criteria": []}
    for wi in ws.work_items.values():
        ws.add_node(wi["id"], "WorkItem", wi["title"])
        for e in wi["evidence_ids"]:
            ws.ev_entities[e].add(wi["id"])

    # prior resolved request referenced by the attempt
    prior_body = "Scheduled exports were delayed last month."
    ws.sources["src_request_prior"] = [{
        "id": "src_request_prior", "workspace_id": ws.id, "revision": 1, "title": "Synthetic prior export request",
        "kind": "request", "origin": "synthetic", "body": prior_body, "sha256": sha256(prior_body), "observed_at": R,
        "ingested_at": R, "supersedes_revision": None}]
    ws.add_node("src_request_prior", "Source", "Synthetic prior export request")
    ws.evidence["ev_request_prior_1"] = {"id": "ev_request_prior_1", "source_id": "src_request_prior", "source_revision": 1,
                                         "origin": "synthetic", "excerpt": prior_body, "location": "body", "observed_at": R,
                                         "ingested_at": R, "stale": False, "_kind": "request"}
    req = one("Request")
    ws.requests["request_exports_prior"] = {
        **{k: req[k] for k in ("workspace_id", "customer_id", "channel", "owner_id", "created_at", "updated_at")},
        "id": "request_exports_prior", "subject": "Scheduled exports delayed", "body": prior_body, "status": "resolved",
        "source_id": "src_request_prior", "capability_ids": ["cap_scheduled_exports"], "linked_work_ids": [], "version": 2}
    ws.requests[req["id"]] = req
    for r in (ws.requests["request_exports_prior"], req):
        ws.add_node(r["id"], "Request", r["subject"])
    ws.ev_entities["ev_request_prior_1"].add("request_exports_prior")

    # second open request with no tracked work (create_work / clarify playground)
    sso_body = "New users cannot sign in with SSO since this morning. Which identity provider settings changed?"
    ws.sources["src_request_sso_002"] = [{
        "id": "src_request_sso_002", "workspace_id": ws.id, "revision": 1, "title": "Synthetic SSO login request",
        "kind": "request", "origin": "synthetic", "body": sso_body, "sha256": sha256(sso_body), "observed_at": R,
        "ingested_at": R, "supersedes_revision": None}]
    ws.add_node("src_request_sso_002", "Source", "Synthetic SSO login request")
    ws.evidence["ev_request_sso_1"] = {"id": "ev_request_sso_1", "source_id": "src_request_sso_002", "source_revision": 1,
                                       "origin": "synthetic", "excerpt": sso_body, "location": "body", "observed_at": R,
                                       "ingested_at": R, "stale": False, "_kind": "request"}
    ws.requests["request_sso_002"] = {
        "id": "request_sso_002", "workspace_id": ws.id, "customer_id": cust["id"], "subject": "SSO login fails for new users",
        "body": sso_body, "channel": "manual", "status": "open", "owner_id": None, "source_id": "src_request_sso_002",
        "capability_ids": ["cap_sso_login"], "linked_work_ids": [], "created_at": R, "updated_at": R, "version": 1}
    ws.add_node("request_sso_002", "Request", "SSO login fails for new users")
    ws.ev_entities["ev_request_sso_1"].add("request_sso_002")

    d = one("Decision")
    ws.decisions[d["id"]] = d
    ws.add_node(d["id"], "Decision", "Forbidden workaround: manual_public_export_link")
    a = one("Attempt")
    ws.attempts[a["id"]] = a
    ws.add_node(a["id"], "Attempt", a["procedure_key"])

    for e in bundle["paths"][0]["edges"]:
        ws.add_edge(e["type"], e["from_id"], e["to_id"], e["evidence_ids"], confirmed=e["confirmed"], edge_id=e["id"])
    ws.add_edge("SUBMITTED", cust["id"], req["id"], ["ev_request_topic_1"])
    ws.add_edge("SUBMITTED", cust["id"], "request_exports_prior", ["ev_request_prior_1"])
    ws.add_edge("SUBMITTED", cust["id"], "request_sso_002", ["ev_request_sso_1"])
    ws.add_edge("ABOUT", "request_exports_prior", "cap_scheduled_exports", ["ev_request_prior_1"])
    ws.add_edge("ABOUT", "request_sso_002", "cap_sso_login", ["ev_request_sso_1"])
    ws.add_edge("TRACKED_BY", req["id"], "work_export_issue", ["ev_capability_work_1"])
    ws.add_edge("OWNED_BY", "cap_scheduled_exports", "person_maya", ["ev_migration_owner_1"])
    ws.add_edge("APPLIES_TO", d["id"], cust["id"], ["ev_export_policy_1"])
    ws.add_edge("ATTEMPTED_FOR", a["id"], "request_exports_prior", ["ev_export_retry_1"])
    ws.add_edge("APPLIES_TO", a["id"], "cap_scheduled_exports", ["ev_export_retry_1"])
    ws.add_edge("RELATED_TO", cust["id"], "cap_scheduled_exports", ["ev_request_topic_1"])

    # completed example chain: run -> proposal -> action -> observation
    run = one("Run")
    run.update(_checkpoint=None, _instruction="", _authorization_id=None)
    ws.runs[run["id"]] = run
    final_ev = one("RunEvent")
    synth = [
        ("run_started", None, {"mode": "scripted", "stage": "investigate"}),
        ("tool_started", "search_context", {"call_id": "call_001", "arguments": {"query": "scheduled exports"}}),
        ("tool_completed", "search_context", {"call_id": "call_001", "ok": True, "result_count": 2, "ids": ["cap_scheduled_exports", "work_export_issue"], "duration_ms": 40, "error_code": None}),
        ("tool_started", "expand_context", {"call_id": "call_002", "arguments": {"entity_ids": ["request_exports_001"], "depth": 4}}),
        ("tool_completed", "expand_context", {"call_id": "call_002", "ok": True, "result_count": 1, "ids": ["path_export_owner"], "duration_ms": 55, "error_code": None}),
        ("proposal_ready", None, {"proposal_id": "proposal_exports_001", "kind": "link_work", "validity": "valid"}),
        ("action_updated", None, {"action_id": "action_exports_001", "status": "succeeded", "connector": "local"}),
    ]
    for i, (t, tool, payload) in enumerate(synth, start=1):
        ws.events[run["id"]].append({"id": f"event_exports_{i:03d}", "run_id": run["id"], "seq": i, "type": t,
                                     "occurred_at": R, "step_id": f"step_{i:03d}", "tool_name": tool, "payload": payload})
    ws.events[run["id"]].append(final_ev)

    p = one("Proposal")
    p.update(_consulted={"work_export_issue", req["id"]}, _connector="local")
    ws.proposals[p["id"]] = p
    act = one("Action")
    act["_seq"] = 1
    ws.actions[act["id"]] = act
    link_rec = {"id": act["receipt"]["target_id"], "request_id": req["id"], "work_item_id": "work_export_issue",
                "comment_draft": None, "action_id": act["id"], "created_at": R}
    ws.local_effects[act["id"]] = {"target_id": act["receipt"]["target_id"], "record": link_rec, "applied": True}
    obs = one("Observation")
    ws.observations[obs["id"]] = obs
    ws.authorizations["authorization_operator_001"] = {"id": "authorization_operator_001", "request_id": req["id"],
                                                       "proposal_id": p["id"], "actor_id": "person_operator", "created_at": R}
    ws.revision = 8


OTHER_DOC = {
    "workspace_id": "ws_other",
    "observed_at": R,
    "customers": [{"id": "customer_globex", "name": "Globex (synthetic)", "plan": "standard", "constraints": []}],
    "teams": [{"id": "team_billing", "name": "Billing (synthetic)"}],
    "people": [{"id": "person_ana", "name": "Ana (synthetic)", "role": "Billing engineer", "team_id": "team_billing", "active": True}],
    "capabilities": [{"id": "cap_billing_reports", "name": "Billing reports", "aliases": ["invoice export"], "status": "active",
                      "product_version": "2.1", "owner_id": "person_ana", "customer_ids": ["customer_globex"]}],
    "work_items": [{"id": "work_billing_fix", "title": "Fix billing report totals", "description": "Totals double-count credits.",
                    "kind": "ticket", "status": "in_progress", "owner_id": "person_ana", "connector": "local", "external_id": None,
                    "external_url": None, "forecast_at": "2026-10-20T00:00:00Z", "observed_at": R,
                    "capability_ids": ["cap_billing_reports"], "depends_on": [], "acceptance_criteria": ["Totals match ledger"]}],
    "requests": [{"id": "request_billing_001", "customer_id": "customer_globex", "subject": "Billing report totals wrong",
                  "body": "Our monthly billing report totals look doubled for credits.", "status": "open", "owner_id": None,
                  "capability_ids": ["cap_billing_reports"], "linked_work_ids": [], "related_request_ids": [], "created_at": R}],
}


def import_text(ws: Workspace, fmt: str, content: str, source_key: str, origin: str = "synthetic") -> dict:
    prev = importer.preview(ws, {"format": fmt, "origin": origin, "content": content, "source_key": source_key})
    return importer.commit(ws, ws.previews[prev["preview_id"]])


def seed_dir(ws: Workspace) -> list[str]:
    d = SEED_DIR / ws.id
    loaded = []
    if not d.is_dir():
        return loaded
    fmts = {".json": "json", ".md": "markdown", ".markdown": "markdown", ".csv": "csv"}
    for f in sorted(d.iterdir()):
        fmt = fmts.get(f.suffix.lower())
        if fmt:
            import_text(ws, fmt, f.read_text(), f.stem[:200])
            loaded.append(f.name)
    return loaded


def build(with_seed: bool = True) -> dict[str, Workspace]:
    demo = Workspace("ws_demo", "Demo workspace (protocol mock)")
    other = Workspace("ws_other", "Isolation workspace (protocol mock)")
    if with_seed:
        seed_demo(demo)
        if not (SEED_DIR / other.id).is_dir():  # built-in isolation fixture only when the coordinator corpus is absent
            import_text(other, "json", canonical(OTHER_DOC), "ws_other_builtin")
        if os.environ.get("MOCK_SEED_DIR", "1") != "0":
            for ws in (demo, other):
                ws.seeded_files = seed_dir(ws)
    return {demo.id: demo, other.id: other}
