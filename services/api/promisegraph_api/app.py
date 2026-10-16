"""PromiseGraph API (Agent A): public /api/v1 and private /internal/v1 routes over FalkorDB."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Callable

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import context as ctxmod
from . import contract, domain, jobs
from .config import Settings
from .core import Ctx, evidence_refs, is_stale
from .errors import ApiError, StoreGuardFailed, conflict, invalid, not_found
from .store import Store, UnitOfWork
from .util import decode_cursor, encode_cursor, payload_hash, sha256_hex

log = logging.getLogger("promisegraph.api")
CONTRACT_VERSION = "1.0.0"
PUB = "/api/v1/workspaces/{workspace_id}"
PRIV = "/internal/v1/workspaces/{workspace_id}"
_CORR = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    contract.configure(settings.contracts_dir)
    store = Store(settings)
    app = FastAPI(title="PromiseGraph API", version=CONTRACT_VERSION)
    app.state.settings = settings
    app.state.store = store

    # ---- envelopes ------------------------------------------------------------------------------------
    def corr(request: Request) -> str:
        c = request.headers.get("X-Correlation-Id")
        return c if c and _CORR.match(c) else "corr_" + uuid.uuid4().hex[:16]

    def meta(request: Request, ws: str | None, revision: int | None = None) -> dict[str, Any]:
        if revision is None:
            revision = 0
            if ws and ws in settings.workspaces:
                try:
                    revision = store.knowledge_revision(ws)
                except Exception:  # noqa: BLE001 - meta on error paths must not mask the error
                    revision = 0
        return {"contract_version": CONTRACT_VERSION, "knowledge_revision": int(revision),
                "correlation_id": request.state.correlation_id}

    def error_body(request: Request, err: ApiError, ws: str | None, revision: int | None = None) -> dict:
        details = err.details if len(err.details) <= 50 else {"truncated": True}
        return {"error": {"code": err.code, "message": err.message, "retryable": err.retryable, "details": details},
                "meta": meta(request, ws, revision)}

    @app.middleware("http")
    async def correlation(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.correlation_id = corr(request)
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = request.state.correlation_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, err: ApiError) -> JSONResponse:
        ws = request.path_params.get("workspace_id")
        if err.code == "DEPENDENCY_UNAVAILABLE":
            ws = None  # do not touch the database again to fill meta
        return JSONResponse(error_body(request, err, ws), status_code=err.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = {"/".join(str(x) for x in e.get("loc", [])): str(e.get("msg"))[:300] for e in exc.errors()[:40]}
        err = invalid("request validation failed", fields)
        return JSONResponse(error_body(request, err, None), status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        err = ApiError(500, "INTERNAL", f"internal error: {type(exc).__name__}")
        return JSONResponse(error_body(request, err, None), status_code=500)

    def ok(request: Request, ws: str, data: Any, status: int = 200, revision: int | None = None) -> JSONResponse:
        return JSONResponse({"data": data, "meta": meta(request, ws, revision)}, status_code=status)

    def page(request: Request, ws: str, items: list, offset: int, limit: int, more: bool) -> JSONResponse:
        return JSONResponse({"data": items, "page": {"next_cursor": encode_cursor(offset + limit) if more else None},
                             "meta": meta(request, ws)})

    # ---- auth -----------------------------------------------------------------------------------------
    def authenticate(request: Request) -> str:
        h = request.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        if tok and settings.operator_token and tok == settings.operator_token:
            return "operator"
        if tok and settings.worker_token and tok == settings.worker_token:
            return "worker"
        raise ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token")

    def make_ctx(request: Request, ws: str, private: bool = False, operator_only: bool = False) -> Ctx:
        role = authenticate(request)
        if ws not in settings.workspaces:
            raise not_found("workspace", ws)
        if ws not in settings.acl.get(role, set()):
            raise ApiError(403, "WORKSPACE_FORBIDDEN", "caller is not permitted in this workspace")
        if private and role != "worker":
            raise ApiError(403, "UNAUTHORIZED", "private routes require the worker service credential")
        if operator_only and role != "operator":
            raise ApiError(403, "UNAUTHORIZED", "operator credential required")
        actor = "person_operator" if role == "operator" else "worker:unknown"
        return Ctx(ws=ws, role=role, actor_id=actor, store=store, settings=settings,
                   correlation_id=request.state.correlation_id)

    def bind_worker_lease(request: Request, ctx: Ctx) -> None:
        """Worker mutations on public routes must carry a live lease (PROTOCOL: Authentication)."""
        if ctx.role != "worker":
            return
        jid = request.headers.get("X-Job-Id")
        tok = request.headers.get("X-Lease-Token")
        fen = request.headers.get("X-Fencing-Token")
        if not (jid and tok and fen):
            raise invalid("worker mutations require X-Job-Id, X-Lease-Token and X-Fencing-Token",
                          {"headers": "missing lease headers"})
        try:
            fencing = int(fen)
        except ValueError as exc:
            raise invalid("X-Fencing-Token must be an integer", {"headers/X-Fencing-Token": "integer"}) from exc
        job = jobs.check_lease(ctx, jid, tok, fencing)
        ctx.lease = {"job_id": jid, "lease_token": tok, "fencing_token": fencing}
        ctx.job = job
        ctx.actor_id = f"worker:{job.get('worker_id') or 'unknown'}"[:128]

    # ---- idempotent mutation wrapper --------------------------------------------------------------------
    def mutate(request: Request, ctx: Ctx, route: str, body: Any, fn: Callable[[UnitOfWork], tuple[int, Any]],
               lease: bool = False) -> Response:
        key = request.headers.get("Idempotency-Key")
        if not key or len(key) > 128:
            raise invalid("Idempotency-Key header (1..128 chars) is required", {"headers/Idempotency-Key": "required"})
        ctx.idem_key = key
        scope = sha256_hex(f"{ctx.ws}|{request.method} {request.url.path}|{ctx.role}|{key}")
        idem_id = f"idem_{scope[:40]}"
        phash = payload_hash(body)
        with store.write_lock:
            rec = store.get(ctx.ws, idem_id, "Idem")
            if rec:  # replay is checked before any lease/revision check
                if rec.get("payload_hash") != phash:
                    raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with a different body",
                                   {"idempotency_key": key})
                resp = JSONResponse(json.loads(rec["body"]), status_code=int(rec["status_code"]))
                resp.headers["Idempotent-Replay"] = "true"
                return resp
            if lease:
                bind_worker_lease(request, ctx)
            uow = UnitOfWork()
            status, data = fn(uow)
            ws_rows = uow.nodes.get("Workspace", {})
            revision = None
            if ws_rows:
                revision = next(iter(ws_rows.values())).get("knowledge_revision")
            if revision is None:
                revision = store.knowledge_revision(ctx.ws)
            if isinstance(data, ApiError):
                out = error_body(request, data, ctx.ws, revision)
            else:
                out = {"data": data, "meta": meta(request, ctx.ws, revision)}
            uow.node("Idem", idem_id, payload_hash=phash, status_code=status, body=json.dumps(out), route=route,
                     created_at=ctx.now, title="idempotency record")
            try:
                store.commit(ctx.ws, uow)
            except StoreGuardFailed as exc:
                if ctx.lease or uow.lease_guard:
                    lg = uow.lease_guard or {}
                    job = store.get(ctx.ws, lg.get("id", ""), "Job") if lg else None
                    if not job or job.get("status") != "leased" or job.get("lease_token") != lg.get("lease_token") \
                            or int(job.get("fencing_token") or 0) != int(lg.get("fencing_token") or 0) \
                            or float(job.get("expires_epoch") or 0) < ctx.now_epoch:
                        raise conflict("LEASE_LOST", "lease lost before the write was applied", {}) from exc
                raise conflict("REVISION_CONFLICT", "a concurrent change invalidated this command; refresh and retry",
                               {}) from exc
            if ctx.post_commit:
                ctx.post_commit()
            if ctx.fault_after_commit:
                raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "injected fault: response lost after local effect",
                               {"fault": "local_create_timeout_after_effect"}, retryable=True)
        return JSONResponse(out, status_code=status)

    def body_of(body: Any, name: str) -> dict[str, Any]:
        return contract.check(name, body if body is not None else {})

    def lim(limit: int | None, default: int = 25, maximum: int = 100) -> int:
        if limit is None:
            return default
        if limit < 1 or limit > maximum:
            raise invalid(f"limit must be 1..{maximum}", {"limit": f"1..{maximum}"})
        return limit

    def off(cursor: str | None) -> int:
        try:
            return decode_cursor(cursor)
        except ValueError as exc:
            raise invalid("invalid cursor", {"cursor": "invalid"}) from exc

    # ---- health ---------------------------------------------------------------------------------------
    @app.get("/health/live")
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request) -> JSONResponse:
        if not store.ping():
            err = ApiError(503, "DEPENDENCY_UNAVAILABLE", "graph database unavailable", retryable=True)
            return JSONResponse(error_body(request, err, None), status_code=503)
        return JSONResponse({"status": "ready", "database": "ok", "contract": contract.contract_hash_line()})

    # ---- workspace reads ------------------------------------------------------------------------------
    @app.get(PUB + "/info")
    def info(request: Request, workspace_id: str) -> JSONResponse:
        ctx = make_ctx(request, workspace_id)
        rev = store.knowledge_revision(workspace_id)
        model = ("unverified" if settings.ai_configured else "unconfigured") if settings.ai_enabled else "disabled"
        linear = ("unverified" if settings.linear_configured else "unconfigured") if settings.linear_enabled else "disabled"
        data = {"workspace_id": workspace_id, "name": settings.workspaces[workspace_id], "clock_mode": settings.clock_mode,
                "reference_time": ctx.ref_now, "planners": {"scripted": "enabled", "model": model},
                "connectors": {"local": "enabled", "linear": linear},
                "auto_commit_kinds": domain.auto_commit_kinds(ctx)}
        return ok(request, workspace_id, data, revision=rev)

    def simple_list(request: Request, ws: str, label: str, filters: dict, cursor: str | None, limit: int | None,
                    order: str = "id", desc: bool = False, contains: dict | None = None,
                    mapper: Callable[[dict], dict] | None = None) -> JSONResponse:
        n = lim(limit)
        o = off(cursor)
        rows = store.find(ws, label, filters, order=order, desc=desc, skip=o, limit=n + 1, contains=contains)
        items = [mapper(r) if mapper else r["doc"] for r in rows[:n]]
        return page(request, ws, items, o, n, len(rows) > n)

    @app.get(PUB + "/customers")
    def customers(request: Request, workspace_id: str, cursor: str | None = None, limit: int | None = None):
        make_ctx(request, workspace_id)
        return simple_list(request, workspace_id, "Customer", {}, cursor, limit)

    @app.get(PUB + "/capabilities")
    def capabilities(request: Request, workspace_id: str, q: str | None = None, query: str | None = None,
                     cursor: str | None = None, limit: int | None = None):
        make_ctx(request, workspace_id)
        text = (q or query or "").strip().lower()
        return simple_list(request, workspace_id, "Capability", {}, cursor, limit,
                           contains={"search_text": text} if text else None)

    @app.get(PUB + "/requests")
    def list_requests(request: Request, workspace_id: str, status: str | None = None, customer_id: str | None = None,
                      cursor: str | None = None, limit: int | None = None):
        make_ctx(request, workspace_id)
        return simple_list(request, workspace_id, "Request", {"status": status, "customer_id": customer_id}, cursor,
                           limit, order="ts", desc=True)

    @app.get(PUB + "/requests/{request_id}")
    def get_request(request: Request, workspace_id: str, request_id: str):
        ctx = make_ctx(request, workspace_id)
        return ok(request, workspace_id, domain.load(ctx, "Request", request_id, "request")["doc"])

    @app.get(PUB + "/runs/{run_id}")
    def get_run(request: Request, workspace_id: str, run_id: str):
        ctx = make_ctx(request, workspace_id)
        return ok(request, workspace_id, domain.load(ctx, "Run", run_id, "run")["doc"])

    @app.get(PUB + "/runs/{run_id}/events")
    def run_events(request: Request, workspace_id: str, run_id: str, after_seq: int = 0, limit: int | None = None):
        ctx = make_ctx(request, workspace_id)
        domain.load(ctx, "Run", run_id, "run")
        n = lim(limit, 100, 100)
        if after_seq < 0:
            raise invalid("after_seq must be >= 0", {"after_seq": ">= 0"})
        rows = store.read(workspace_id, "MATCH (e:RunEvent) WHERE e.run_id = $rid AND e.seq > $after "
                                        "RETURN e.doc ORDER BY e.seq LIMIT $lim",
                          {"rid": run_id, "after": after_seq, "lim": n + 1})
        evs = [json.loads(r[0]) for r in rows]
        data = evs[:n]
        return JSONResponse({"data": data, "next_after_seq": data[-1]["seq"] if data else after_seq,
                             "has_more": len(evs) > n, "meta": meta(request, workspace_id)})

    @app.get(PUB + "/requests/{request_id}/context")
    def get_context(request: Request, workspace_id: str, request_id: str, depth: int = ctxmod.DEFAULT_DEPTH):
        ctx = make_ctx(request, workspace_id)
        if depth < 1 or depth > ctxmod.MAX_DEPTH:
            raise invalid(f"depth must be 1..{ctxmod.MAX_DEPTH}", {"depth": f"1..{ctxmod.MAX_DEPTH}"})
        b = ctxmod.bundle(ctx, request_id, depth)
        return ok(request, workspace_id, b, revision=b["based_on_revision"])

    @app.post(PUB + "/context/search")
    def context_search(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id)
        cmd = body_of(body, "ContextSearch")
        return ok(request, workspace_id, ctxmod.search(ctx, cmd))

    @app.post(PUB + "/context/expand")
    def context_expand(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id)
        cmd = body_of(body, "ContextExpand")
        return ok(request, workspace_id, ctxmod.expand(ctx, cmd))

    @app.get(PUB + "/sources")
    def list_sources(request: Request, workspace_id: str, kind: str | None = None, origin: str | None = None,
                     cursor: str | None = None, limit: int | None = None):
        ctx = make_ctx(request, workspace_id)

        def summary(p: dict) -> dict:
            d = dict(p["doc"])
            d["stale"] = is_stale(d["kind"], d["observed_at"], ctx.ref_now)
            return d
        return simple_list(request, workspace_id, "Source", {"kind": kind, "origin": origin}, cursor, limit,
                           mapper=summary)

    @app.get(PUB + "/sources/{source_id}")
    def get_source(request: Request, workspace_id: str, source_id: str, revision: int | None = None):
        ctx = make_ctx(request, workspace_id)
        head = domain.load(ctx, "Source", source_id, "source")
        rev = revision if revision is not None else int(head["latest_revision"])
        p = store.get(workspace_id, f"{source_id}@r{rev}", "SourceRev")
        if not p:
            raise not_found("source revision", f"{source_id}@{rev}")
        return ok(request, workspace_id, p["doc"])

    @app.get(PUB + "/evidence/{evidence_id}")
    def get_evidence(request: Request, workspace_id: str, evidence_id: str):
        """Convenience read (not in the frozen matrix): resolve one EvidenceRef by id."""
        ctx = make_ctx(request, workspace_id)
        refs = evidence_refs(ctx, [evidence_id])
        if not refs:
            raise not_found("evidence", evidence_id)
        return ok(request, workspace_id, refs[0])

    @app.get(PUB + "/requests/{request_id}/runs")
    def list_request_runs(request: Request, workspace_id: str, request_id: str, cursor: str | None = None,
                          limit: int | None = None):
        ctx = make_ctx(request, workspace_id)
        domain.load(ctx, "Request", request_id, "request")
        return simple_list(request, workspace_id, "Run", {"request_id": request_id}, cursor, limit,
                           order="created_at", desc=True)

    @app.get(PUB + "/proposals")
    def list_proposals(request: Request, workspace_id: str, request_id: str | None = None, run_id: str | None = None,
                       cursor: str | None = None, limit: int | None = None):
        make_ctx(request, workspace_id)
        return simple_list(request, workspace_id, "Proposal", {"request_id": request_id, "run_id": run_id}, cursor,
                           limit, order="created_at", desc=True)

    @app.get(PUB + "/proposals/{proposal_id}")
    def get_proposal(request: Request, workspace_id: str, proposal_id: str):
        ctx = make_ctx(request, workspace_id)
        return ok(request, workspace_id, domain.load(ctx, "Proposal", proposal_id, "proposal")["doc"])

    @app.get(PUB + "/actions")
    def list_actions(request: Request, workspace_id: str, request_id: str | None = None, status: str | None = None,
                     cursor: str | None = None, limit: int | None = None):
        make_ctx(request, workspace_id)
        return simple_list(request, workspace_id, "Action", {"request_id": request_id, "status": status}, cursor, limit,
                           order="ts", desc=True)

    @app.get(PUB + "/actions/{action_id}")
    def get_action(request: Request, workspace_id: str, action_id: str):
        ctx = make_ctx(request, workspace_id)
        return ok(request, workspace_id, domain.load(ctx, "Action", action_id, "action")["doc"])

    @app.get(PUB + "/work-items")
    def list_work(request: Request, workspace_id: str, status: str | None = None, customer_id: str | None = None,
                  cursor: str | None = None, limit: int | None = None):
        make_ctx(request, workspace_id)
        if not customer_id:
            return simple_list(request, workspace_id, "WorkItem", {"status": status}, cursor, limit)
        n, o = lim(limit), off(cursor)
        rows = store.read(workspace_id, "MATCH (:Customer {id: $c})-[:SUBMITTED]->(:Request)-[:TRACKED_BY]->(n:WorkItem) "
                                        "WHERE $s IS NULL OR n.status = $s RETURN DISTINCT n.id, n.doc ORDER BY n.id "
                                        "SKIP $o LIMIT $l", {"c": customer_id, "s": status, "o": o, "l": n + 1})
        items = [json.loads(r[1]) for r in rows]
        return page(request, workspace_id, items[:n], o, n, len(items) > n)

    @app.get(PUB + "/work-items/{work_item_id}")
    def get_work(request: Request, workspace_id: str, work_item_id: str):
        ctx = make_ctx(request, workspace_id)
        return ok(request, workspace_id, domain.load(ctx, "WorkItem", work_item_id, "work item")["doc"])

    @app.get(PUB + "/people/{person_id}")
    def get_person(request: Request, workspace_id: str, person_id: str):
        """Rev 5 #33."""
        ctx = make_ctx(request, workspace_id)
        p = domain.load(ctx, "Person", person_id, "person")
        d = p["doc"]
        return ok(request, workspace_id, {"id": d["id"], "name": d["name"], "role": d["role"],
                                          "team_id": d.get("team_id"), "active": bool(d["active"]),
                                          "version": int(p.get("version") or 1)})

    @app.get(PUB + "/decisions")
    def list_decisions(request: Request, workspace_id: str, scope_entity_type: str | None = None,
                       scope_entity_id: str | None = None, active: bool | None = None, cursor: str | None = None,
                       limit: int | None = None):
        ctx = make_ctx(request, workspace_id)
        entity_type, entity_id = scope_entity_type, scope_entity_id
        n, o = lim(limit), off(cursor)
        rows = store.find(workspace_id, "Decision", {"scope_type": entity_type, "scope_id": entity_id},
                          order="created_at")
        docs = [ctxmod.effective_decision(r["doc"], ctx.ref_now) for r in rows]
        if active is not None:
            docs = [d for d in docs if (d["status"] == "active" and d["effective_at"] <= ctx.ref_now) == active]
        return page(request, workspace_id, docs[o:o + n], o, n, len(docs) > o + n)

    @app.get(PUB + "/observations")
    def list_observations(request: Request, workspace_id: str, request_id: str | None = None,
                          cursor: str | None = None, limit: int | None = None):
        """Convenience read (not in the frozen matrix)."""
        make_ctx(request, workspace_id)
        return simple_list(request, workspace_id, "Observation", {"request_id": request_id}, cursor, limit,
                           order="observed_at", desc=True)

    @app.get(PUB + "/attempts")
    def list_attempts(request: Request, workspace_id: str, request_id: str | None = None,
                      scope_entity_type: str | None = None, scope_entity_id: str | None = None,
                      work_item_id: str | None = None, cursor: str | None = None, limit: int | None = None):
        """Rev 3 #19 / rev 5 #31; `work_item_id` (attempts ATTEMPTED_FOR that work item) is an A extension."""
        make_ctx(request, workspace_id)
        if work_item_id:
            n, o = lim(limit), off(cursor)
            rows = store.read(workspace_id, "MATCH (a:Attempt)-[:ATTEMPTED_FOR]->(:WorkItem {id: $w}) RETURN DISTINCT "
                                            "a.id, a.doc, a.observed_at ORDER BY a.observed_at DESC, a.id DESC "
                                            "SKIP $o LIMIT $l", {"w": work_item_id, "o": o, "l": n + 1})
            items = [json.loads(r[1]) for r in rows]
            return page(request, workspace_id, items[:n], o, n, len(items) > n)
        return simple_list(request, workspace_id, "Attempt", {"request_id": request_id, "scope_type": scope_entity_type,
                                                              "scope_id": scope_entity_id}, cursor, limit,
                           order="observed_at", desc=True)

    # ---- public commands ------------------------------------------------------------------------------
    @app.post(PUB + "/requests")
    def post_request(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "RequestCreate")
        return mutate(request, ctx, "/requests", cmd, lambda u: domain.intake(ctx, u, cmd))

    @app.post(PUB + "/requests/{request_id}/runs")
    def post_run(request: Request, workspace_id: str, request_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "RunCreate")
        return mutate(request, ctx, "/requests/{request_id}/runs", cmd,
                      lambda u: domain.create_run(ctx, u, request_id, cmd))

    @app.post(PUB + "/runs/{run_id}/cancel")
    def post_cancel(request: Request, workspace_id: str, run_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "RunCancel")
        return mutate(request, ctx, "/runs/{run_id}/cancel", cmd,
                      lambda u: domain.cancel_run(ctx, u, run_id, cmd))

    @app.post(PUB + "/runs/{run_id}/resume")
    def post_resume(request: Request, workspace_id: str, run_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "RunResume")
        return mutate(request, ctx, "/runs/{run_id}/resume", cmd,
                      lambda u: domain.resume_run(ctx, u, run_id, cmd))

    @app.post(PUB + "/requests/{request_id}/replies")
    def post_reply(request: Request, workspace_id: str, request_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "ReplyCreate")
        return mutate(request, ctx, "/requests/{request_id}/replies", cmd,
                      lambda u: domain.reply(ctx, u, request_id, cmd))

    @app.post(PUB + "/proposals")
    def post_proposal(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id)
        cmd = body_of(body, "ProposalCreate")
        return mutate(request, ctx, "/proposals", cmd, lambda u: domain.create_proposal(ctx, u, cmd), lease=True)

    @app.post(PUB + "/proposals/{proposal_id}/commit")
    def post_commit(request: Request, workspace_id: str, proposal_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id)
        cmd = body_of(body, "ProposalCommit")
        return mutate(request, ctx, "/proposals/{proposal_id}/commit", cmd,
                      lambda u: domain.commit_proposal(ctx, u, proposal_id, cmd, ctx.idem_key), lease=True)

    @app.post(PUB + "/requests/{request_id}/observations")
    def post_observation(request: Request, workspace_id: str, request_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id)
        cmd = body_of(body, "ObservationCreate")
        return mutate(request, ctx, "/requests/{request_id}/observations", cmd,
                      lambda u: domain.record_observation(ctx, u, request_id, cmd), lease=True)

    @app.post(PUB + "/requests/{request_id}/refresh")
    def post_refresh(request: Request, workspace_id: str, request_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "RefreshCreate")
        return mutate(request, ctx, "/requests/{request_id}/refresh", cmd,
                      lambda u: domain.refresh(ctx, u, request_id, cmd))

    @app.patch(PUB + "/work-items/{work_item_id}")
    def patch_work(request: Request, workspace_id: str, work_item_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "WorkItemUpdate")
        return mutate(request, ctx, "/work-items/{work_item_id}", cmd,
                      lambda u: domain.patch_work_item(ctx, u, work_item_id, cmd))

    @app.post(PUB + "/decisions")
    def post_decision(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "DecisionCommand")
        return mutate(request, ctx, "/decisions", cmd, lambda u: domain.decision_command(ctx, u, cmd))

    @app.post(PUB + "/imports/preview")
    def post_preview(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "ImportPreview")
        return mutate(request, ctx, "/imports/preview", cmd, lambda u: domain.import_preview(ctx, u, cmd))

    @app.post(PUB + "/imports/{preview_id}/commit")
    def post_import_commit(request: Request, workspace_id: str, preview_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, operator_only=True)
        cmd = body_of(body, "ImportCommit")
        return mutate(request, ctx, "/imports/{preview_id}/commit", cmd,
                      lambda u: domain.import_commit(ctx, u, preview_id, cmd))

    @app.post(PUB + "/actions/{action_id}/reconcile")
    def post_reconcile(request: Request, workspace_id: str, action_id: str, body: Any = Body(None)):
        """PROTOCOL rev 3 #20 / rev 4 #28."""
        ctx = make_ctx(request, workspace_id, operator_only=True)
        b = body_of(body, "ActionReconcile")
        return mutate(request, ctx, "/actions/{action_id}/reconcile", b,
                      lambda u: jobs.operator_reconcile(ctx, u, action_id, b))

    @app.post(PUB + "/requests/{request_id}/close")
    def post_close(request: Request, workspace_id: str, request_id: str, body: Any = Body(None)):
        """PROTOCOL rev 3 #21."""
        ctx = make_ctx(request, workspace_id, operator_only=True)
        b = body_of(body, "RequestClose")
        return mutate(request, ctx, "/requests/{request_id}/close", b,
                      lambda u: domain.close_request(ctx, u, request_id, b))

    # ---- private worker protocol ----------------------------------------------------------------------
    @app.post(PRIV + "/jobs/claim")
    def post_claim(request: Request, workspace_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, private=True)
        cmd = body_of(body, "JobClaim")
        with store.write_lock:
            lease = jobs.claim(ctx, cmd)
        if lease is None:
            return Response(status_code=204, headers={"X-Correlation-Id": request.state.correlation_id})
        return ok(request, workspace_id, lease)

    @app.post(PRIV + "/jobs/{job_id}/heartbeat")
    def post_heartbeat(request: Request, workspace_id: str, job_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, private=True)
        cmd = body_of(body, "LeaseBinding")
        with store.write_lock:
            try:
                lease = jobs.heartbeat(ctx, job_id, cmd)
            except StoreGuardFailed as exc:
                raise conflict("LEASE_LOST", "lease expired during heartbeat", {"job_id": job_id}) from exc
        return ok(request, workspace_id, lease)

    @app.post(PRIV + "/jobs/{job_id}/checkpoint")
    def post_checkpoint(request: Request, workspace_id: str, job_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, private=True)
        cmd = body_of(body, "JobCheckpoint")
        return mutate(request, ctx, "/jobs/{job_id}/checkpoint", cmd,
                      lambda u: jobs.checkpoint(ctx, u, job_id, cmd))

    @app.post(PRIV + "/jobs/{job_id}/complete")
    def post_complete(request: Request, workspace_id: str, job_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, private=True)
        cmd = body_of(body, "JobComplete")
        return mutate(request, ctx, "/jobs/{job_id}/complete", cmd,
                      lambda u: jobs.complete(ctx, u, job_id, cmd))

    @app.post(PRIV + "/actions/{action_id}/begin-dispatch")
    def post_begin(request: Request, workspace_id: str, action_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, private=True)
        cmd = body_of(body, "BeginDispatch")
        return mutate(request, ctx, "/actions/{action_id}/begin-dispatch", cmd,
                      lambda u: jobs.begin_dispatch(ctx, u, action_id, cmd))

    @app.post(PRIV + "/actions/{action_id}/execute-local")
    def post_execute_local(request: Request, workspace_id: str, action_id: str, body: Any = Body(None)):
        ctx = make_ctx(request, workspace_id, private=True)
        cmd = body_of(body, "ExecuteLocal")
        return mutate(request, ctx, "/actions/{action_id}/execute-local", cmd,
                      lambda u: jobs.execute_local(ctx, u, action_id, cmd))

    return app


app = create_app()
