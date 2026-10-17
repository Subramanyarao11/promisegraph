"""Public reads: workspace info, lists, resources, events, request context and the read-only context tools."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import context as ctxmod
from .. import domain
from ..core import evidence_refs, is_stale
from ..deps import JsonBody, ReaderCtx
from ..errors import invalid, not_found
from ..http import body_of, lim, meta, off, ok, page, simple_list
from ..openapi_doc import doc

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["public reads"])


@router.get("/info", **doc("WorkspaceInfo"))
def info(request: Request, ctx: ReaderCtx) -> JSONResponse:
    s = ctx.settings
    rev = ctx.store.knowledge_revision(ctx.ws)
    model = ("unverified" if s.ai_configured else "unconfigured") if s.ai_enabled else "disabled"
    linear = ("unverified" if s.linear_configured else "unconfigured") if s.linear_enabled else "disabled"
    data = {"workspace_id": ctx.ws, "name": s.workspaces[ctx.ws], "clock_mode": s.clock_mode,
            "reference_time": ctx.ref_now, "planners": {"scripted": "enabled", "model": model},
            "connectors": {"local": "enabled", "linear": linear}, "auto_commit_kinds": domain.auto_commit_kinds(ctx)}
    return ok(request, ctx, data, revision=rev)


@router.get("/customers", **doc("Customer", kind="list"))
def customers(request: Request, ctx: ReaderCtx, cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    return simple_list(request, ctx, "Customer", {}, cursor, limit)


@router.get("/capabilities", **doc("CapabilitySummary", kind="list"))
def capabilities(request: Request, ctx: ReaderCtx, q: str | None = None, query: str | None = None,
                 cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    text = (q or query or "").strip().lower()
    return simple_list(request, ctx, "Capability", {}, cursor, limit, contains={"search_text": text} if text else None)


@router.get("/requests", **doc("Request", kind="list"))
def list_requests(request: Request, ctx: ReaderCtx, status: str | None = None, customer_id: str | None = None,
                  cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    return simple_list(request, ctx, "Request", {"status": status, "customer_id": customer_id}, cursor, limit,
                       order="ts", desc=True)


@router.get("/requests/{request_id}", **doc("Request"))
def get_request(request: Request, ctx: ReaderCtx, request_id: str) -> JSONResponse:
    return ok(request, ctx, domain.load(ctx, "Request", request_id, "request")["doc"])


@router.get("/requests/{request_id}/runs", **doc("Run", kind="list"))
def list_request_runs(request: Request, ctx: ReaderCtx, request_id: str, cursor: str | None = None,
                      limit: int | None = None) -> JSONResponse:
    domain.load(ctx, "Request", request_id, "request")
    return simple_list(request, ctx, "Run", {"request_id": request_id}, cursor, limit, order="created_at", desc=True)


@router.get("/requests/{request_id}/context", **doc("ContextBundle"))
def get_context(request: Request, ctx: ReaderCtx, request_id: str, depth: int = ctxmod.DEFAULT_DEPTH) -> JSONResponse:
    if depth < 1 or depth > ctxmod.MAX_DEPTH:
        raise invalid(f"depth must be 1..{ctxmod.MAX_DEPTH}", {"depth": f"1..{ctxmod.MAX_DEPTH}"})
    b = ctxmod.bundle(ctx, request_id, depth)
    return ok(request, ctx, b, revision=b["based_on_revision"])


@router.get("/runs/{run_id}", **doc("Run"))
def get_run(request: Request, ctx: ReaderCtx, run_id: str) -> JSONResponse:
    return ok(request, ctx, domain.load(ctx, "Run", run_id, "run")["doc"])


@router.get("/runs/{run_id}/events", **doc("EventsPage", kind="events"))
def run_events(request: Request, ctx: ReaderCtx, run_id: str, after_seq: int = 0,
               limit: int | None = None) -> JSONResponse:
    domain.load(ctx, "Run", run_id, "run")
    n = lim(limit, 100, 100)
    if after_seq < 0:
        raise invalid("after_seq must be >= 0", {"after_seq": ">= 0"})
    rows = ctx.store.read(ctx.ws, "MATCH (e:RunEvent) WHERE e.run_id = $rid AND e.seq > $after "
                                  "RETURN e.doc ORDER BY e.seq LIMIT $lim",
                          {"rid": run_id, "after": after_seq, "lim": n + 1})
    evs = [json.loads(r[0]) for r in rows]
    data = evs[:n]
    return JSONResponse({"data": data, "next_after_seq": data[-1]["seq"] if data else after_seq,
                         "has_more": len(evs) > n, "meta": meta(request, ctx.store, ctx.ws)})


# Read-only tools: POST bodies, but no Idempotency-Key required (PROTOCOL rev 2 #5).
@router.post("/context/search", **doc("ContextSearchResult", body="ContextSearch"))
def context_search(request: Request, ctx: ReaderCtx, body: JsonBody = None) -> JSONResponse:
    return ok(request, ctx, ctxmod.search(ctx, body_of(body, "ContextSearch")))


@router.post("/context/expand", **doc("ContextExpandResult", body="ContextExpand"))
def context_expand(request: Request, ctx: ReaderCtx, body: JsonBody = None) -> JSONResponse:
    return ok(request, ctx, ctxmod.expand(ctx, body_of(body, "ContextExpand")))


@router.get("/sources", **doc("SourceSummary", kind="list"))
def list_sources(request: Request, ctx: ReaderCtx, kind: str | None = None, origin: str | None = None,
                 cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    def summary(p: dict) -> dict:
        d = dict(p["doc"])
        d["stale"] = is_stale(d["kind"], d["observed_at"], ctx.ref_now)
        return d
    return simple_list(request, ctx, "Source", {"kind": kind, "origin": origin}, cursor, limit, mapper=summary)


@router.get("/sources/{source_id}", **doc("SourceRecord"))
def get_source(request: Request, ctx: ReaderCtx, source_id: str, revision: int | None = None) -> JSONResponse:
    head = domain.load(ctx, "Source", source_id, "source")
    rev = revision if revision is not None else int(head["latest_revision"])
    p = ctx.store.get(ctx.ws, f"{source_id}@r{rev}", "SourceRev")
    if not p:
        raise not_found("source revision", f"{source_id}@{rev}")
    return ok(request, ctx, p["doc"])


@router.get("/evidence/{evidence_id}", **doc("EvidenceRef"))
def get_evidence(request: Request, ctx: ReaderCtx, evidence_id: str) -> JSONResponse:
    """PROTOCOL rev 3 #19."""
    refs = evidence_refs(ctx, [evidence_id])
    if not refs:
        raise not_found("evidence", evidence_id)
    return ok(request, ctx, refs[0])


@router.get("/proposals", **doc("Proposal", kind="list"))
def list_proposals(request: Request, ctx: ReaderCtx, request_id: str | None = None, run_id: str | None = None,
                   cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    return simple_list(request, ctx, "Proposal", {"request_id": request_id, "run_id": run_id}, cursor, limit,
                       order="created_at", desc=True)


@router.get("/proposals/{proposal_id}", **doc("Proposal"))
def get_proposal(request: Request, ctx: ReaderCtx, proposal_id: str) -> JSONResponse:
    return ok(request, ctx, domain.load(ctx, "Proposal", proposal_id, "proposal")["doc"])


@router.get("/actions", **doc("Action", kind="list"))
def list_actions(request: Request, ctx: ReaderCtx, request_id: str | None = None, status: str | None = None,
                 cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    return simple_list(request, ctx, "Action", {"request_id": request_id, "status": status}, cursor, limit,
                       order="ts", desc=True)


@router.get("/actions/{action_id}", **doc("Action"))
def get_action(request: Request, ctx: ReaderCtx, action_id: str) -> JSONResponse:
    return ok(request, ctx, domain.load(ctx, "Action", action_id, "action")["doc"])


@router.get("/work-items", **doc("WorkItem", kind="list"))
def list_work(request: Request, ctx: ReaderCtx, status: str | None = None, customer_id: str | None = None,
              cursor: str | None = None, limit: int | None = None) -> JSONResponse:
    if not customer_id:
        return simple_list(request, ctx, "WorkItem", {"status": status}, cursor, limit)
    n, o = lim(limit), off(cursor)
    rows = ctx.store.read(ctx.ws, "MATCH (:Customer {id: $c})-[:SUBMITTED]->(:Request)-[:TRACKED_BY]->(n:WorkItem) "
                                  "WHERE $s IS NULL OR n.status = $s RETURN DISTINCT n.id, n.doc ORDER BY n.id "
                                  "SKIP $o LIMIT $l", {"c": customer_id, "s": status, "o": o, "l": n + 1})
    items = [json.loads(r[1]) for r in rows]
    return page(request, ctx, items[:n], o, n, len(items) > n)


@router.get("/work-items/{work_item_id}", **doc("WorkItem"))
def get_work(request: Request, ctx: ReaderCtx, work_item_id: str) -> JSONResponse:
    return ok(request, ctx, domain.load(ctx, "WorkItem", work_item_id, "work item")["doc"])


@router.get("/people/{person_id}", **doc("PersonSummary"))
def get_person(request: Request, ctx: ReaderCtx, person_id: str) -> JSONResponse:
    """PROTOCOL rev 5 #33."""
    p = domain.load(ctx, "Person", person_id, "person")
    d = p["doc"]
    return ok(request, ctx, {"id": d["id"], "name": d["name"], "role": d["role"], "team_id": d.get("team_id"),
                             "active": bool(d["active"]), "version": int(p.get("version") or 1)})


@router.get("/decisions", **doc("Decision", kind="list"))
def list_decisions(request: Request, ctx: ReaderCtx, scope_entity_type: str | None = None,
                   scope_entity_id: str | None = None, active: bool | None = None, cursor: str | None = None,
                   limit: int | None = None) -> JSONResponse:
    n, o = lim(limit), off(cursor)
    rows = ctx.store.find(ctx.ws, "Decision", {"scope_type": scope_entity_type, "scope_id": scope_entity_id},
                          order="created_at")
    docs = [ctxmod.effective_decision(r["doc"], ctx.ref_now) for r in rows]
    if active is not None:
        docs = [d for d in docs if (d["status"] == "active" and d["effective_at"] <= ctx.ref_now) == active]
    return page(request, ctx, docs[o:o + n], o, n, len(docs) > o + n)


@router.get("/observations", **doc("Observation", kind="list"))
def list_observations(request: Request, ctx: ReaderCtx, request_id: str | None = None, cursor: str | None = None,
                      limit: int | None = None) -> JSONResponse:
    """PROTOCOL rev 3 #19."""
    return simple_list(request, ctx, "Observation", {"request_id": request_id}, cursor, limit,
                       order="observed_at", desc=True)


@router.get("/attempts", **doc("Attempt", kind="list"))
def list_attempts(request: Request, ctx: ReaderCtx, request_id: str | None = None,
                  scope_entity_type: str | None = None, scope_entity_id: str | None = None,
                  work_item_id: str | None = None, cursor: str | None = None,
                  limit: int | None = None) -> JSONResponse:
    """Rev 3 #19 / rev 5 #31; `work_item_id` (attempts ATTEMPTED_FOR that work item) is an A extension."""
    if work_item_id:
        n, o = lim(limit), off(cursor)
        rows = ctx.store.read(ctx.ws, "MATCH (a:Attempt)-[:ATTEMPTED_FOR]->(:WorkItem {id: $w}) RETURN DISTINCT "
                                      "a.id, a.doc, a.observed_at ORDER BY a.observed_at DESC, a.id DESC "
                                      "SKIP $o LIMIT $l", {"w": work_item_id, "o": o, "l": n + 1})
        items = [json.loads(r[1]) for r in rows]
        return page(request, ctx, items[:n], o, n, len(items) > n)
    return simple_list(request, ctx, "Attempt", {"request_id": request_id, "scope_type": scope_entity_type,
                                                 "scope_id": scope_entity_id}, cursor, limit,
                       order="observed_at", desc=True)
