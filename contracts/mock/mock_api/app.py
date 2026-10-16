"""PromiseGraph protocol mock (contract 1.0.0). In-memory, single process, development/test only.

Run: cd contracts/mock && uv run uvicorn mock_api.app:app --port 8100
Every response carries `X-PromiseGraph-Mock: true`. Mock-only controls live under /__mock/.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import timedelta
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import context as ctxmod
from . import engine, importer, seed
from .common import (
    ACTION_KINDS, CONTRACT_VERSION, REFERENCE_TIME, ApiError, assert_contract, bounded_id, canonical, check_input, invalid, iso,
    new_id, not_found, now, now_dt, plus, pub, sha256,
)
from .state import Workspace, touch

PUBLIC = "/api/v1/workspaces/{wid}"
PRIVATE = "/internal/v1/workspaces/{wid}"
FAULT_NAMES = {"database_unavailable", "stale_next_proposal", "lease_expire_now", "local_create_timeout_after_effect",
               "duplicate_mutation"}


# =========================================================================== store

class Store:
    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.faults: dict[str, Any] = {}
        self.reset(True)

    def reset(self, with_seed: bool = True) -> None:
        self.workspaces = seed.build(with_seed)
        self.faults = {"database_unavailable": False, "stale_next_proposal": 0, "local_create_timeout_after_effect": 0,
                       "duplicate_mutation": 0}
        for name in filter(None, (f.strip() for f in os.environ.get("FAULTS", "").split(","))):
            self.set_fault(name, True)

    def set_fault(self, name: str, value: Any) -> None:
        if name not in FAULT_NAMES:
            raise invalid(f"unknown fault {name}", {name: f"must be one of {sorted(FAULT_NAMES)}"})
        if name == "database_unavailable":
            self.faults[name] = bool(value)
        elif name == "lease_expire_now":
            if value:
                past = now_dt() - timedelta(seconds=1)
                for ws in self.workspaces.values():
                    for j in ws.jobs.values():
                        if j["status"] == "leased":
                            j["expires_dt"] = past
        else:
            self.faults[name] = int(value) if not isinstance(value, bool) else (1 if value else 0)

    def take(self, name: str) -> bool:
        if self.faults.get(name, 0) > 0:
            self.faults[name] -= 1
            return True
        return False


store = Store()
app = FastAPI(title="PromiseGraph protocol mock", version=CONTRACT_VERSION,
              description="In-memory protocol mock of the PromiseGraph API. Not a graph database. Development/test only.")


# =========================================================================== envelopes

class Ctx:
    def __init__(self, request: Request, ws: Workspace | None, caller: str | None):
        self.request = request
        self.ws = ws
        self.caller = caller
        cid = request.headers.get("X-Correlation-Id")
        self.correlation_id = cid if cid and 1 <= len(cid) <= 128 else new_id("corr")

    def meta(self) -> dict:
        return {"contract_version": CONTRACT_VERSION, "knowledge_revision": self.ws.revision if self.ws else 0,
                "correlation_id": self.correlation_id}


def _headers(ctx_or_cid) -> dict:
    cid = ctx_or_cid.correlation_id if isinstance(ctx_or_cid, Ctx) else ctx_or_cid
    return {"X-Correlation-Id": cid}


def ok(ctx: Ctx, data: Any, schema: str | None, status: int = 200) -> JSONResponse:
    if schema:
        assert_contract(schema, data)
    return JSONResponse({"data": data, "meta": ctx.meta()}, status_code=status, headers=_headers(ctx))


def ok_list(ctx: Ctx, items: list, schema: str, next_cursor: str | None) -> JSONResponse:
    for it in items:
        assert_contract(schema, it)
    return JSONResponse({"data": items, "page": {"next_cursor": next_cursor}, "meta": ctx.meta()}, headers=_headers(ctx))


def error_response(e: ApiError, request: Request, ws: Workspace | None = None, *, unknown_revision: bool = False) -> JSONResponse:
    cid = request.headers.get("X-Correlation-Id")
    cid = cid if cid and 1 <= len(cid) <= 128 else new_id("corr")
    if unknown_revision:
        ws = None  # rev3 #24: database unreachable -> knowledge_revision 0 means "unknown"
    elif ws is None:
        ws = store.workspaces.get(request.path_params.get("wid", "")) if hasattr(request, "path_params") else None
    body = {"error": {"code": e.code, "message": e.message, "retryable": e.retryable, "details": e.details},
            "meta": {"contract_version": CONTRACT_VERSION, "knowledge_revision": ws.revision if ws else 0, "correlation_id": cid}}
    return JSONResponse(body, status_code=e.status, headers={"X-Correlation-Id": cid})


@app.exception_handler(ApiError)
async def _api_error(request: Request, e: ApiError):
    return error_response(e, request)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, e: StarletteHTTPException):
    if e.status_code == 404:
        return error_response(ApiError(404, "NOT_FOUND", f"no route {request.method} {request.url.path}"), request)
    if e.status_code == 405:
        return error_response(ApiError(405, "INVALID_INPUT", f"method {request.method} not allowed on {request.url.path}"), request)
    return error_response(ApiError(e.status_code, "INVALID_INPUT", str(e.detail)), request)


@app.exception_handler(Exception)
async def _unhandled(request: Request, e: Exception):
    return error_response(ApiError(500, "INTERNAL", f"mock internal error: {type(e).__name__}: {e}"[:2000], retryable=False), request)


@app.middleware("http")
async def mock_middleware(request: Request, call_next):
    path = request.url.path
    if store.faults.get("database_unavailable") and not path.startswith("/__mock") and path != "/health/live":
        resp = error_response(ApiError(503, "DEPENDENCY_UNAVAILABLE", "database unavailable (injected mock fault)",
                                       {"dependency": "database"}, retryable=True), request, unknown_revision=True)
    else:
        resp = await call_next(request)
    resp.headers["X-PromiseGraph-Mock"] = "true"
    return resp


# =========================================================================== auth

def _acl() -> dict[str, set[str]]:
    raw = os.environ.get("WORKSPACE_ACL", "operator:ws_demo,ws_other;worker:ws_demo,ws_other")
    out: dict[str, set[str]] = {"operator": set(), "worker": set()}
    for part in raw.split(";"):
        if ":" in part:
            role, wss = part.split(":", 1)
            out.setdefault(role.strip(), set()).update(w.strip() for w in wss.split(",") if w.strip())
    return out


def authenticate(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token")
    tok = h[len("Bearer "):].strip()
    if tok == os.environ.get("OPERATOR_TOKEN", "dev-operator-token"):
        return "operator"
    if tok == os.environ.get("WORKER_TOKEN", "dev-worker-token"):
        return "worker"
    raise ApiError(401, "UNAUTHORIZED", "invalid bearer token")


def authorize(request: Request, wid: str, *, private: bool = False, operator_only: bool = False) -> Ctx:
    caller = authenticate(request)
    if private and caller != "worker":
        raise ApiError(403, "UNAUTHORIZED", "private worker routes require the worker credential")
    ws = store.workspaces.get(wid)
    if ws is None:
        raise ApiError(404, "NOT_FOUND", f"workspace {wid} not found", {"entity": "workspace", "id": wid})
    if wid not in _acl().get(caller, set()):
        raise ApiError(403, "WORKSPACE_FORBIDDEN", f"{caller} is not permitted in workspace {wid}")
    if operator_only and caller != "operator":
        raise ApiError(403, "UNAUTHORIZED", "this command requires the operator credential")
    return Ctx(request, ws, caller)


# =========================================================================== command plumbing

async def read_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        raise ApiError(400, "INVALID_INPUT", "request body must be JSON", {"fields": {"<root>": "missing body"}})
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError(400, "INVALID_INPUT", f"invalid JSON: {e}", {"fields": {"<root>": "invalid JSON"}})


def lease_headers(ctx: Ctx) -> dict | None:
    if ctx.caller != "worker":
        return None
    h = ctx.request.headers
    vals = {"job_id": h.get("X-Job-Id"), "lease_token": h.get("X-Lease-Token"), "fencing_token": h.get("X-Fencing-Token")}
    missing = [k for k, v in vals.items() if not v]
    if missing:
        raise ApiError(422, "INVALID_INPUT", "worker mutations require X-Job-Id, X-Lease-Token and X-Fencing-Token",
                       {"fields": {f"header/{k}": "required" for k in missing}})
    return vals


async def command(request: Request, wid: str, template: str, schema_in: str | None,
                  handler: Callable[[Ctx, Any], tuple[int, Any, str]], *, private: bool = False,
                  operator_only: bool = False, idempotent: bool = True) -> JSONResponse:
    ctx = authorize(request, wid, private=private, operator_only=operator_only)
    body = await read_body(request)
    ws = ctx.ws
    key = None
    if idempotent:
        key = request.headers.get("Idempotency-Key")
        if not key or not (1 <= len(key) <= 128):
            raise ApiError(422, "INVALID_INPUT", "Idempotency-Key header (1..128 chars) is required",
                           {"fields": {"header/Idempotency-Key": "required, 1..128 chars"}})
        scope = (request.method, request.url.path, ctx.caller, key)  # rev2 #4: concrete resolved path
        digest = sha256(canonical(body))
        rec = ws.idem.get(scope)
        if rec is not None:
            if rec["hash"] != digest:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with a different payload",
                               {"idempotency_key": key})
            return JSONResponse(rec["body"], status_code=rec["status"],
                                headers={**_headers(ctx), "Idempotent-Replayed": "true"})
    if schema_in:
        check_input(schema_in, body)
    ctx.idem_key = key
    status, data, schema_out = handler(ctx, body)
    if schema_out:
        assert_contract(schema_out, data)
    out = {"data": data, "meta": ctx.meta()}
    if idempotent and 200 <= status < 300:
        ws.idem[scope] = {"hash": digest, "status": status, "body": out}
        if store.take("duplicate_mutation"):
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE",
                           "response lost after commit (injected duplicate_mutation fault); retry with the same Idempotency-Key",
                           {"fault": "duplicate_mutation"}, retryable=True)
    return JSONResponse(out, status_code=status, headers=_headers(ctx))


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        dec = base64.urlsafe_b64decode(cursor.encode() + b"==").decode()
        if not dec.startswith("o:") or int(dec[2:]) < 0:
            raise ValueError
        return int(dec[2:])
    except Exception:
        raise invalid("cursor is not valid", {"cursor": "opaque cursor from page.next_cursor"})


def newest_first(items, ts_key: str) -> list:
    """PROTOCOL.md #27: newest first by (timestamp desc, id desc)."""
    return sorted(items, key=lambda x: (x[ts_key], x["id"]), reverse=True)


def paginate(request: Request, items: list) -> tuple[list, str | None]:
    q = request.query_params
    try:
        limit = int(q.get("limit", 25))
    except ValueError:
        raise invalid("limit must be an integer", {"limit": "integer 1..100"})
    if not 1 <= limit <= 100:
        raise invalid("limit must be between 1 and 100", {"limit": "integer 1..100"})
    offset = decode_cursor(q.get("cursor"))
    page = items[offset:offset + limit]
    nxt = None
    if offset + limit < len(items):
        nxt = base64.urlsafe_b64encode(f"o:{offset + limit}".encode()).decode().rstrip("=")
    return page, nxt


def qbool(v: str | None) -> bool | None:
    if v is None:
        return None
    if v.lower() in ("true", "1"):
        return True
    if v.lower() in ("false", "0"):
        return False
    raise invalid("boolean query parameter expected", {"active": "true|false"})


def qint(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    v = request.query_params.get(name)
    if v is None:
        return default
    try:
        n = int(v)
    except ValueError:
        raise invalid(f"{name} must be an integer", {name: f"integer {lo}..{hi}"})
    if not lo <= n <= hi:
        raise invalid(f"{name} out of range", {name: f"integer {lo}..{hi}"})
    return n


def worker_job(ctx: Ctx, *, run_id: str | None = None, request_id: str | None = None) -> dict | None:
    """Validate X-Job-Id/X-Lease-Token/X-Fencing-Token for worker public mutations (409 LEASE_LOST)."""
    h = lease_headers(ctx)
    if h is None:
        return None
    job = engine.check_lease(ctx.ws, h["job_id"], h["lease_token"], h["fencing_token"], kinds=("investigate",), run_id=run_id)
    if request_id is not None and (not job["run_id"] or ctx.ws.runs[job["run_id"]]["request_id"] != request_id):
        raise ApiError(409, "LEASE_LOST", f"job {job['id']} is not bound to request {request_id}", {"job_id": job["id"]})
    return job


# =========================================================================== health / info / mock controls

@app.get("/health/live")
async def health_live():
    return {"status": "live", "mock": True, "contract_version": CONTRACT_VERSION}


@app.get("/health/ready")
async def health_ready():
    return {"status": "ready", "mock": True, "contract_version": CONTRACT_VERSION, "database": "in-memory (mock)",
            "workspaces": sorted(store.workspaces)}


@app.post("/__mock/reset")
async def mock_reset(request: Request):
    raw = await request.body()
    opts = json.loads(raw) if raw else {}
    store.reset(bool(opts.get("seed", True)))
    return {"reset": True, "workspaces": {k: v.revision for k, v in store.workspaces.items()}, "faults": store.faults}


@app.get("/__mock/faults")
async def mock_faults_get():
    return {"faults": store.faults}


@app.post("/__mock/faults")
async def mock_faults(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise ApiError(400, "INVALID_INPUT", "invalid JSON")
    if not isinstance(body, dict):
        raise invalid("faults body must be an object")
    for k, v in body.items():
        store.set_fault(k, v)
    return {"faults": store.faults}


@app.get(PUBLIC + "/info")
async def info(request: Request, wid: str):
    ctx = authorize(request, wid)
    data = {
        "workspace_id": wid, "name": ctx.ws.name,
        "clock_mode": "live" if os.environ.get("CLOCK_MODE") == "live" else "scenario",
        "reference_time": iso(now_dt()) if os.environ.get("CLOCK_MODE") == "live" else REFERENCE_TIME,
        # Honest labels: the mock plans nothing itself (the worker's scripted planner does); it never calls a model or Linear.
        "planners": {"scripted": "enabled", "model": "unverified" if engine.ai_enabled() else "disabled"},
        "connectors": {"local": "enabled", "linear": "unverified" if engine.linear_enabled() else "unconfigured"},
        "auto_commit_kinds": ctx.ws.auto_commit_kinds,
    }
    return ok(ctx, data, "WorkspaceInfo")


# =========================================================================== customers / capabilities

@app.get(PUBLIC + "/customers")
async def customers(request: Request, wid: str):
    ctx = authorize(request, wid)
    items = [pub(c) for c in sorted(ctx.ws.customers.values(), key=lambda c: c["id"])]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Customer", nxt)


@app.get(PUBLIC + "/capabilities")
async def capabilities(request: Request, wid: str):
    ctx = authorize(request, wid)
    q = (request.query_params.get("query") or "").lower().strip()
    items = []
    for c in sorted(ctx.ws.capabilities.values(), key=lambda c: c["id"]):
        if q and q not in c["name"].lower() and not any(q in a.lower() for a in c["aliases"]) and q not in c["id"]:
            continue
        items.append(pub(c))
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "CapabilitySummary", nxt)


# =========================================================================== requests

@app.post(PUBLIC + "/requests")
async def create_request(request: Request, wid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        if b["customer_id"] not in ws.customers:
            raise invalid("unknown customer_id", {"customer_id": f"{b['customer_id']} does not exist"})
        bad = [c for c in b["capability_ids"] if c not in ws.capabilities]
        if bad:
            raise invalid("unknown capability_ids", {"capability_ids": f"unknown: {', '.join(bad)}"[:500]})
        rid, t = new_id("request"), now()
        src, _ = ws.add_source(bounded_id("src", rid), title=f"Request: {b['subject']}", kind="request", origin="synthetic",
                               body=b["body"], observed_at=t)
        ev = ws.add_evidence(src, excerpt=b["body"], location="body", entities=[rid])
        req = {"id": rid, "workspace_id": ws.id, "customer_id": b["customer_id"], "subject": b["subject"], "body": b["body"],
               "channel": b["channel"], "status": "open", "owner_id": None, "source_id": src["id"],
               "capability_ids": list(dict.fromkeys(b["capability_ids"])), "linked_work_ids": [], "created_at": t,
               "updated_at": t, "version": 1, "_external_ref": b["external_ref"]}
        ws.requests[rid] = req
        ws.add_node(rid, "Request", b["subject"])
        ws.add_edge("SUBMITTED", b["customer_id"], rid, [ev["id"]], asserted_by=rid)
        for c in req["capability_ids"]:
            ws.add_edge("ABOUT", rid, c, [ev["id"]], asserted_by=rid)
        ws.bump()
        run = engine.create_run(ws, req, b["planner_mode"]) if b["auto_start"] else None
        return 201, {"request": pub(req), "run": pub(run) if run else None}, "RequestCreateResult"
    return await command(request, wid, "POST /requests", "RequestCreate", h, operator_only=True)


@app.get(PUBLIC + "/requests")
async def list_requests(request: Request, wid: str):
    ctx = authorize(request, wid)
    st, cu = request.query_params.get("status"), request.query_params.get("customer_id")
    items = [pub(r) for r in sorted(ctx.ws.requests.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)
             if (not st or r["status"] == st) and (not cu or r["customer_id"] == cu)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Request", nxt)


@app.get(PUBLIC + "/requests/{rid}")
async def get_request(request: Request, wid: str, rid: str):
    ctx = authorize(request, wid)
    return ok(ctx, pub(ctx.ws.get("requests", rid, "request")), "Request")


@app.post(PUBLIC + "/requests/{rid}/runs")
async def create_run(request: Request, wid: str, rid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        req = ws.get("requests", rid, "request")
        mine = [r for r in ws.runs.values() if r["request_id"] == rid]
        latest = max(mine, key=lambda r: (r["created_at"], r["id"])) if mine else None
        reviewing = latest if latest and latest["status"] == "awaiting_review" else None  # PROTOCOL.md #34
        if any(r["status"] in engine.ACTIVE_RUN and r is not reviewing for r in mine):
            raise ApiError(409, "INVALID_TRANSITION", "request already has an active run")
        if b["authorization_id"] is not None and b["authorization_id"] not in ws.authorizations:
            raise ApiError(422, "POLICY_VIOLATION", f"authorization {b['authorization_id']} does not exist")
        run = engine.create_run(ws, req, b["mode"], b["instruction"], b["authorization_id"])
        if reviewing:
            engine.supersede_run(ws, reviewing, run["id"])
        return 202, pub(run), "Run"
    return await command(request, wid, "POST /requests/{request_id}/runs", "RunCreate", h, operator_only=True)


# =========================================================================== runs

@app.get(PUBLIC + "/runs/{run_id}")
async def get_run(request: Request, wid: str, run_id: str):
    ctx = authorize(request, wid)
    return ok(ctx, pub(ctx.ws.get("runs", run_id, "run")), "Run")


@app.get(PUBLIC + "/requests/{rid}/runs")
async def list_request_runs(request: Request, wid: str, rid: str):
    """rev2 #1: Run list for a request, newest first."""
    ctx = authorize(request, wid)
    ctx.ws.get("requests", rid, "request")
    items = [pub(r) for r in newest_first(ctx.ws.runs.values(), "created_at") if r["request_id"] == rid]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Run", nxt)


@app.get(PUBLIC + "/runs/{run_id}/events")
async def run_events(request: Request, wid: str, run_id: str):
    ctx = authorize(request, wid)
    ctx.ws.get("runs", run_id, "run")
    after = qint(request, "after_seq", 0, 0, 2**31)
    limit = qint(request, "limit", 100, 1, 100)
    evs = [e for e in ctx.ws.events[run_id] if e["seq"] > after]
    data = evs[:limit]
    body = {"data": data, "next_after_seq": data[-1]["seq"] if data else after, "has_more": len(evs) > limit,
            "meta": ctx.meta()}
    assert_contract("EventsPage", body)
    return JSONResponse(body, headers=_headers(ctx))


@app.post(PUBLIC + "/runs/{run_id}/cancel")
async def cancel_run(request: Request, wid: str, run_id: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        run = ws.get("runs", run_id, "run")
        if run["status"] in ("succeeded", "failed", "cancelled"):
            raise ApiError(409, "INVALID_TRANSITION", f"run is {run['status']}")
        engine.cancel_run(ws, run, b["reason"])
        return 200, pub(run), "Run"
    return await command(request, wid, "POST /runs/{run_id}/cancel", "RunCancel", h, operator_only=True)


@app.post(PUBLIC + "/runs/{run_id}/resume")
async def resume_run(request: Request, wid: str, run_id: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        run = ws.get("runs", run_id, "run")
        if b["expected_run_version"] != run["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_run_version does not match", {"version": run["version"]})
        if run["status"] != "failed" or (run["error"] or {}).get("code") == "CAPABILITY_DISABLED":
            raise ApiError(409, "INVALID_TRANSITION", "only a recoverable failed run can be resumed")
        bud = run["budget"]
        if bud["extensions_used"] >= 1:
            raise ApiError(422, "POLICY_VIOLATION", "budget extension already used (policy allows one)")
        bud["max_tool_calls"] = min(bud["max_tool_calls"] + b["additional_tool_calls"], engine.LIFETIME_TOOL_CALLS)
        bud["max_active_seconds"] = min(bud["max_active_seconds"] + b["additional_active_seconds"], engine.LIFETIME_ACTIVE_SECONDS)
        bud["extensions_used"] += 1
        run["error"] = None
        cp = run.get("_checkpoint")
        engine.set_run(run, "queued", (cp or {}).get("stage") or "investigate")
        engine.enqueue(ws, "investigate", run=run,  # rev2 #18
                       checkpoint={**(cp or {}), "resume_stage": (cp or {}).get("stage") or "investigate", "resume_reason": b["reason"]})
        return 202, pub(run), "Run"
    return await command(request, wid, "POST /runs/{run_id}/resume", "RunResume", h, operator_only=True)


# =========================================================================== context

@app.get(PUBLIC + "/requests/{rid}/context")
async def request_context(request: Request, wid: str, rid: str):
    ctx = authorize(request, wid)
    req = ctx.ws.get("requests", rid, "request")
    depth = qint(request, "depth", 4, 1, 6)
    return ok(ctx, ctxmod.bundle(ctx.ws, req, depth), "ContextBundle")


@app.post(PUBLIC + "/context/search")
async def context_search(request: Request, wid: str):
    ctx = authorize(request, wid)
    body = await read_body(request)
    check_input("ContextSearch", body)
    if body["customer_id"] is not None and body["customer_id"] not in ctx.ws.customers:
        raise not_found("customer", body["customer_id"])
    return ok(ctx, ctxmod.search(ctx.ws, body), "ContextSearchResult")


@app.post(PUBLIC + "/context/expand")
async def context_expand(request: Request, wid: str):
    ctx = authorize(request, wid)
    body = await read_body(request)
    check_input("ContextExpand", body)
    for e in body["entity_ids"]:
        if e not in ctx.ws.nodes:
            raise not_found("entity", e)
    return ok(ctx, ctxmod.expand(ctx.ws, body), "ContextExpandResult")


# =========================================================================== sources

@app.get(PUBLIC + "/sources/{sid}")
async def get_source(request: Request, wid: str, sid: str):
    ctx = authorize(request, wid)
    revs = ctx.ws.sources.get(sid)
    if not revs:
        raise not_found("source", sid)
    rev = qint(request, "revision", revs[-1]["revision"], 1, 2**31)
    if rev > len(revs):
        raise ApiError(404, "NOT_FOUND", f"source {sid} has no revision {rev}", {"entity": "source", "id": sid, "revision": rev})
    return ok(ctx, revs[rev - 1], "SourceRecord")


@app.get(PUBLIC + "/sources")
async def list_sources(request: Request, wid: str):
    ctx = authorize(request, wid)
    kind, origin = request.query_params.get("kind"), request.query_params.get("origin")
    items = [ctx.ws.source_summary(s) for s in sorted(ctx.ws.sources)]
    items = [s for s in items if (not kind or s["kind"] == kind) and (not origin or s["origin"] == origin)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "SourceSummary", nxt)


# =========================================================================== replies

@app.post(PUBLIC + "/requests/{rid}/replies")
async def reply(request: Request, wid: str, rid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        req = ws.get("requests", rid, "request")
        if b["expected_request_version"] != req["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_request_version does not match", {"version": req["version"]})
        t = now()
        src, _ = ws.add_source(bounded_id("src", new_id("reply")), title=f"Reply on {rid}", kind="request",
                               origin="synthetic", body=b["body"], observed_at=t)
        ws.add_evidence(src, excerpt=b["body"], location="body", entities=[rid])
        ws.add_edge("RELATED_TO", src["id"], rid, [], asserted_by=src["id"])
        ws.bump()
        touch(req)
        runs = sorted((r for r in ws.runs.values() if r["request_id"] == rid), key=lambda r: r["created_at"])
        waiting = next((r for r in reversed(runs) if r["status"] == "waiting_for_input"), None)
        active = next((r for r in reversed(runs) if r["status"] in ("queued", "running", "awaiting_review")), None)
        if waiting:
            run = waiting
            cp = dict(run.get("_checkpoint") or {})
            cp.update(resume_stage="investigate", reply_source_id=src["id"])  # rev2 #18
            engine.set_run(run, "queued", "investigate")
            engine.enqueue(ws, "investigate", run=run, checkpoint=cp)
        elif active:
            run = active
        else:
            run = engine.create_run(ws, req, runs[-1]["mode"] if runs else "scripted", "follow-up after reply")
        return 201, {"source_id": src["id"], "source_revision": src["revision"], "run": pub(run)}, "ReplyResult"
    return await command(request, wid, "POST /requests/{request_id}/replies", "ReplyCreate", h, operator_only=True)


# =========================================================================== proposals / actions

@app.post(PUBLIC + "/proposals")
async def create_proposal(request: Request, wid: str):
    def h(ctx: Ctx, b: dict):
        worker_job(ctx, run_id=b["run_id"])
        if store.take("stale_next_proposal"):
            ctx.ws.bump()  # simulate a concurrent knowledge change
        p = engine.create_proposal(ctx.ws, b)
        return 201, pub(p), "Proposal"
    return await command(request, wid, "POST /proposals", "ProposalCreate", h)


@app.get(PUBLIC + "/proposals")
async def list_proposals(request: Request, wid: str):
    """rev2 #1: Proposal list filtered by request_id/run_id, newest first."""
    ctx = authorize(request, wid)
    rq, rn = request.query_params.get("request_id"), request.query_params.get("run_id")
    items = [pub(p) for p in newest_first(ctx.ws.proposals.values(), "created_at")
             if (not rq or p["request_id"] == rq) and (not rn or p["run_id"] == rn)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Proposal", nxt)


@app.get(PUBLIC + "/proposals/{pid}")
async def get_proposal(request: Request, wid: str, pid: str):
    ctx = authorize(request, wid)
    return ok(ctx, pub(ctx.ws.get("proposals", pid, "proposal")), "Proposal")


@app.post(PUBLIC + "/proposals/{pid}/commit")
async def commit_proposal(request: Request, wid: str, pid: str):
    def h(ctx: Ctx, b: dict):
        p = ctx.ws.get("proposals", pid, "proposal")
        worker_job(ctx, run_id=p["run_id"])
        action, job = engine.commit_proposal(ctx.ws, p, b, caller=ctx.caller, idem_key=ctx.idem_key)
        return 202, {"action": pub(action), "job_id": job["id"]}, "ProposalCommitResult"
    return await command(request, wid, "POST /proposals/{proposal_id}/commit", "ProposalCommit", h)


@app.get(PUBLIC + "/actions")
async def list_actions(request: Request, wid: str):
    ctx = authorize(request, wid)
    rq, st = request.query_params.get("request_id"), request.query_params.get("status")
    items = [pub(a) for a in sorted(ctx.ws.actions.values(), key=lambda a: (a["created_at"], a["_seq"]), reverse=True)
             if (not rq or a["request_id"] == rq) and (not st or a["status"] == st)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Action", nxt)


@app.get(PUBLIC + "/actions/{aid}")
async def get_action(request: Request, wid: str, aid: str):
    ctx = authorize(request, wid)
    return ok(ctx, pub(ctx.ws.get("actions", aid, "action")), "Action")


# =========================================================================== observations / refresh

@app.post(PUBLIC + "/requests/{rid}/observations")
async def observe(request: Request, wid: str, rid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        req = ws.get("requests", rid, "request")
        job = worker_job(ctx, request_id=rid)
        if job is not None and b["kind"] != "connector_status":
            raise ApiError(422, "POLICY_VIOLATION", "the worker may only record connector_status observations; "
                           "verification kinds require an attributable operator/customer/acceptance source")
        if b["expected_request_version"] != req["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_request_version does not match", {"version": req["version"]})
        actor = f"worker:{job['worker_id']}"[:128] if job else "person_operator"
        obs = engine.record_observation(ws, req, b, actor)
        return 201, {"observation": obs, "request": pub(req)}, "ObservationResult"
    return await command(request, wid, "POST /requests/{request_id}/observations", "ObservationCreate", h)


@app.post(PUBLIC + "/requests/{rid}/refresh")
async def refresh(request: Request, wid: str, rid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        ws.get("requests", rid, "request")
        for j in ws.jobs.values():
            if j["kind"] == "refresh" and j["request_id"] == rid and j["status"] == "queued":
                return 202, {"job_id": j["id"], "coalesced": True}, "RefreshResult"
        req = ws.requests[rid]
        job = engine.enqueue(ws, "refresh", request_id=rid,  # rev3 #22: run_id stays null; target lives in checkpoint
                             checkpoint={"request_id": rid, "reason": b["reason"], "work_item_ids": list(req["linked_work_ids"])})
        return 202, {"job_id": job["id"], "coalesced": False}, "RefreshResult"
    return await command(request, wid, "POST /requests/{request_id}/refresh", "RefreshCreate", h, operator_only=True)


# =========================================================================== work items

@app.get(PUBLIC + "/work-items")
async def list_work(request: Request, wid: str):
    ctx = authorize(request, wid)
    ws = ctx.ws
    st, cu = request.query_params.get("status"), request.query_params.get("customer_id")
    allowed = None
    if cu:
        allowed = set()
        for r in ws.requests.values():
            if r["customer_id"] == cu:
                allowed.update(r["linked_work_ids"])
        caps = {c["id"] for c in ws.capabilities.values() if cu in c.get("_customer_ids", [])}
        allowed.update(w["id"] for w in ws.work_items.values() if set(w.get("_capability_ids", [])) & caps)
    items = [pub(w) for w in sorted(ws.work_items.values(), key=lambda w: w["id"])
             if (not st or w["status"] == st) and (allowed is None or w["id"] in allowed)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "WorkItem", nxt)


@app.patch(PUBLIC + "/work-items/{work_id}")
async def patch_work(request: Request, wid: str, work_id: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        w = ws.get("work_items", work_id, "work_item")
        if w["connector"] != "local":
            raise ApiError(422, "POLICY_VIOLATION", "external work items are provider-owned; use refresh/readback")
        if b["expected_version"] != w["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_version does not match", {"version": w["version"]})
        t = now()
        w.update(status=b["status"], owner_id=b["owner_id"], forecast_at=b["forecast_at"], observed_at=t)
        w["version"] += 1
        src, _ = ws.add_source(bounded_id("src", f"{work_id}_operator"), title=f"Operator update of {work_id}",
                               kind="work", origin="owned_permitted",
                               body=canonical({"work_item_id": work_id, "status": b["status"], "owner_id": b["owner_id"],
                                               "forecast_at": b["forecast_at"], "reason": b["reason"], "observed_at": t}),
                               observed_at=t)
        ev = ws.add_evidence(src, excerpt=f"{work_id} set to {b['status']}: {b['reason']}", location="body", entities=[work_id])
        w["evidence_ids"] = (w["evidence_ids"] + [ev["id"]])[-100:]
        ws.drop_edges_by(f"{work_id}#owner")
        if b["owner_id"] and b["owner_id"] in ws.nodes:
            ws.add_edge("OWNED_BY", work_id, b["owner_id"], [ev["id"]], asserted_by=f"{work_id}#owner")
        ws.bump()
        ws.invalidate_proposals({work_id}, f"work item {work_id} changed (knowledge_revision {ws.revision})")
        for r in ws.requests.values():
            if work_id in r["linked_work_ids"]:
                if b["status"] == "done" and r["status"] == "awaiting_work":
                    engine.set_request(r, "awaiting_verification")
                elif b["status"] == "cancelled" and r["status"] in ("awaiting_work", "awaiting_verification"):
                    engine.set_request(r, "investigating")
        return 200, {"work_item": pub(w), "knowledge_revision": ws.revision}, "WorkItemUpdateResult"
    return await command(request, wid, "PATCH /work-items/{work_item_id}", "WorkItemUpdate", h, operator_only=True)


# =========================================================================== decisions

RULE_VALUE = {
    "forbidden_workaround": lambda v: isinstance(v, str) and len(v) > 0,
    "scope_reduction_allowed": lambda v: isinstance(v, bool),
    "date_extension_allowed": lambda v: isinstance(v, bool),
    "allowed_action_types": lambda v: isinstance(v, list) and all(x in ACTION_KINDS for x in v),
    "routing_owner": lambda v: isinstance(v, str) and len(v) > 0,
}


@app.get(PUBLIC + "/decisions")
async def list_decisions(request: Request, wid: str):
    ctx = authorize(request, wid)
    q = request.query_params
    et, eid, active = q.get("scope_entity_type"), q.get("scope_entity_id"), qbool(q.get("active"))
    from .context import _decision_active
    items = [pub(d) for d in sorted(ctx.ws.decisions.values(), key=lambda d: (d["effective_at"], d["id"]))
             if (not et or d["scope"]["entity_type"] == et) and (not eid or d["scope"]["entity_id"] == eid)
             and (active is None or _decision_active(d) == active)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Decision", nxt)


@app.post(PUBLIC + "/decisions")
async def decide(request: Request, wid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        if b["expected_revision"] != ws.revision:
            raise ApiError(409, "REVISION_CONFLICT", "expected_revision does not match current knowledge_revision",
                           {"knowledge_revision": ws.revision})
        if not RULE_VALUE[b["rule_type"]](b["value"]):
            raise invalid(f"value has the wrong type for rule {b['rule_type']}", {"value": "rule-specific type"})
        if b["rule_type"] == "routing_owner" and b["value"] not in ws.people:
            raise invalid("routing_owner must reference an existing person", {"value": "unknown person"})
        for e in b["evidence_ids"]:
            if e not in ws.evidence:
                raise invalid(f"evidence {e} does not exist", {"evidence_ids": f"unknown evidence {e}"})
        if not ws.entity_exists(b["scope"]["entity_id"]):
            raise invalid("scope entity does not exist", {"scope/entity_id": "unknown entity"})
        op, t = b["operation"], now()
        target = None
        if op == "record":
            if b["decision_id"] is not None:
                raise invalid("decision_id must be null for record", {"decision_id": "must be null"})
        else:
            if b["decision_id"] is None:
                raise invalid(f"{op} requires decision_id", {"decision_id": "required"})
            target = ws.get("decisions", b["decision_id"], "decision")
            if target["status"] != "active":
                raise ApiError(409, "INVALID_TRANSITION", f"decision is {target['status']}")
        src, _ = ws.add_source(bounded_id("src", new_id("decision")), title=f"Decision {op}: {b['rule_type']}", kind="decision",
                               origin="owned_permitted", body=canonical(b), observed_at=t)
        ev = ws.add_evidence(src, excerpt=b["rationale"], location="body")
        if op == "revoke":
            target["status"] = "revoked"
            target["version"] += 1
            target["evidence_ids"] = (target["evidence_ids"] + [ev["id"]])[-100:]
            ws.ev_entities[ev["id"]].add(target["id"])
            result, status = target, 200
        else:
            did = new_id("decision")
            d = {"id": did, "workspace_id": ws.id, "rule_type": b["rule_type"], "value": b["value"], "scope": b["scope"],
                 "status": "active", "rationale": b["rationale"], "author_id": "person_operator", "effective_at": b["effective_at"],
                 "expires_at": b["expires_at"], "supersedes_id": target["id"] if target else None,
                 "evidence_ids": list(dict.fromkeys(b["evidence_ids"] + [ev["id"]]))[:100], "version": 1}
            ws.decisions[did] = d
            ws.ev_entities[ev["id"]].add(did)
            ws.add_node(did, "Decision", f"{b['rule_type']}")
            ws.add_edge("APPLIES_TO", did, b["scope"]["entity_id"], [ev["id"]], asserted_by=did)
            if target:
                target["status"] = "superseded"
                target["version"] += 1
                ws.add_edge("SUPERSEDES", did, target["id"], [ev["id"]], asserted_by=did)
            result, status = d, 201
        ws.bump()
        ws.invalidate_proposals({b["scope"]["entity_id"]} | ({target["id"]} if target else set()),
                                f"decision {op} changed applicable policy (knowledge_revision {ws.revision})")
        return status, pub(result), "Decision"
    return await command(request, wid, "POST /decisions", "DecisionCommand", h, operator_only=True)


# =========================================================================== imports

@app.post(PUBLIC + "/imports/preview")
async def import_preview(request: Request, wid: str):
    def h(ctx: Ctx, b: dict):
        if len(b["content"].encode()) > 1024 * 1024:
            raise invalid("content exceeds 1 MB", {"content": "max 1 MB"})
        return 201, importer.preview(ctx.ws, b), "ImportPreviewResult"
    return await command(request, wid, "POST /imports/preview", "ImportPreview", h, operator_only=True)


@app.post(PUBLIC + "/imports/{preview_id}/commit")
async def import_commit(request: Request, wid: str, preview_id: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        pv = ws.previews.get(preview_id)
        if not pv:
            raise not_found("import preview", preview_id)
        if pv["committed"] is not None:
            raise ApiError(409, "INVALID_TRANSITION", "preview already committed")
        if b["expected_revision"] != ws.revision or pv["based_on_revision"] != ws.revision:
            raise ApiError(409, "REVISION_CONFLICT", "knowledge changed since preview; preview again",
                           {"knowledge_revision": ws.revision, "preview_based_on_revision": pv["based_on_revision"]})
        res = importer.commit(ws, pv)
        pv["committed"] = res
        return 200, res, "ImportCommitResult"
    return await command(request, wid, "POST /imports/{preview_id}/commit", "ImportCommit", h, operator_only=True)


# =========================================================================== private job protocol

# =========================================================================== rev 3 additions (PROTOCOL.md items 19-21)

@app.get(PUBLIC + "/evidence/{ev_id}")
async def get_evidence(request: Request, wid: str, ev_id: str):
    ctx = authorize(request, wid)
    ev = ctx.ws.evidence_view(ev_id)
    if ev is None:
        raise not_found("evidence", ev_id)
    return ok(ctx, ev, "EvidenceRef")


@app.get(PUBLIC + "/work-items/{work_id}")
async def get_work_item(request: Request, wid: str, work_id: str):
    ctx = authorize(request, wid)
    return ok(ctx, pub(ctx.ws.get("work_items", work_id, "work_item")), "WorkItem")


@app.get(PUBLIC + "/observations")
async def list_observations(request: Request, wid: str):
    ctx = authorize(request, wid)
    rq = request.query_params.get("request_id")
    items = [pub(o) for o in newest_first(ctx.ws.observations.values(), "observed_at") if not rq or o["request_id"] == rq]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Observation", nxt)


@app.get(PUBLIC + "/attempts")
async def list_attempts(request: Request, wid: str):
    ctx = authorize(request, wid)
    q = request.query_params
    rq, et, eid = q.get("request_id"), q.get("scope_entity_type"), q.get("scope_entity_id")  # PROTOCOL.md #31
    items = [pub(a) for a in newest_first(ctx.ws.attempts.values(), "observed_at")
             if (not rq or a["request_id"] == rq) and (not et or a["scope"]["entity_type"] == et)
             and (not eid or a["scope"]["entity_id"] == eid)]
    page, nxt = paginate(request, items)
    return ok_list(ctx, page, "Attempt", nxt)


@app.get(PUBLIC + "/people/{person_id}")
async def get_person(request: Request, wid: str, person_id: str):
    """PROTOCOL.md #33."""
    ctx = authorize(request, wid)
    p = ctx.ws.get("people", person_id, "person")
    data = {"id": p["id"], "name": p["name"], "role": p["role"], "team_id": p.get("team_id"),
            "active": p.get("active", True), "version": p.get("version", 1)}
    return ok(ctx, data, "PersonSummary")


@app.post(PUBLIC + "/actions/{aid}/reconcile")
async def reconcile_action(request: Request, wid: str, aid: str):
    def h(ctx: Ctx, b: dict):
        if b["outcome"] == "succeeded" and b["target"] is None:
            raise invalid("outcome succeeded requires a target", {"target": "required when outcome=succeeded"})
        ws = ctx.ws
        for e in b["evidence_ids"]:
            if e not in ws.evidence:
                raise invalid(f"evidence {e} does not exist", {"evidence_ids": f"unknown evidence {e}"})
        action = ws.get("actions", aid, "action")
        return 200, pub(engine.operator_reconcile(ws, action, b)), "Action"
    return await command(request, wid, "POST /actions/{action_id}/reconcile", "ActionReconcile", h, operator_only=True)


@app.post(PUBLIC + "/requests/{rid}/close")
async def close_request(request: Request, wid: str, rid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        req = ws.get("requests", rid, "request")
        if req["status"] == "closed":
            raise ApiError(409, "INVALID_TRANSITION", "request is already closed")
        if b["expected_request_version"] != req["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_request_version does not match", {"version": req["version"]})
        for run in ws.runs.values():  # explicit closure stops planning; effects/receipts may still be recorded
            if run["request_id"] == rid and run["status"] in engine.ACTIVE_RUN:
                engine.cancel_run(ws, run, f"request closed: {b['reason']}")
        req["_close_reason"] = b["reason"]
        engine.set_request(req, "closed")
        return 200, pub(req), "Request"
    return await command(request, wid, "POST /requests/{request_id}/close", "RequestClose", h, operator_only=True)


@app.post(PRIVATE + "/jobs/claim")
async def claim(request: Request, wid: str):
    ctx = authorize(request, wid, private=True)
    body = await read_body(request)
    check_input("JobClaim", body)
    lease = engine.claim(ctx.ws, body)
    if lease is None:
        return Response(status_code=204, headers=_headers(ctx))
    return ok(ctx, lease, "JobLease")


@app.post(PRIVATE + "/jobs/{job_id}/heartbeat")
async def heartbeat(request: Request, wid: str, job_id: str):
    ctx = authorize(request, wid, private=True)
    body = await read_body(request)
    check_input("LeaseBinding", body)
    engine.get_job(ctx.ws, job_id)
    job = engine.check_lease(ctx.ws, job_id, body["lease_token"], body["fencing_token"])
    return ok(ctx, engine.heartbeat(ctx.ws, job), "JobLease")


@app.post(PRIVATE + "/jobs/{job_id}/checkpoint")
async def checkpoint(request: Request, wid: str, job_id: str):
    def h(ctx: Ctx, b: dict):
        engine.get_job(ctx.ws, job_id)
        job = engine.check_lease(ctx.ws, job_id, b["lease_token"], b["fencing_token"])
        return 200, engine.checkpoint(ctx.ws, job, b), "CheckpointResult"
    return await command(request, wid, "POST /jobs/{job_id}/checkpoint", "JobCheckpoint", h, private=True)


@app.post(PRIVATE + "/jobs/{job_id}/complete")
async def complete(request: Request, wid: str, job_id: str):
    def h(ctx: Ctx, b: dict):
        job = engine.get_job(ctx.ws, job_id)
        prior = job["completions"].get(b["completion_id"])
        if prior is not None:  # same completion id (any attempt) -> original receipt
            return 200, prior, "CompletionReceipt"
        if job["status"] == "completed":
            raise ApiError(409, "LEASE_LOST", "job is already completed", {"job_id": job_id})
        job = engine.check_lease(ctx.ws, job_id, b["lease_token"], b["fencing_token"])
        return 200, engine.complete(ctx.ws, job, b), "CompletionReceipt"
    return await command(request, wid, "POST /jobs/{job_id}/complete", "JobComplete", h, private=True)


@app.post(PRIVATE + "/actions/{aid}/begin-dispatch")
async def begin_dispatch(request: Request, wid: str, aid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        action = ws.get("actions", aid, "action")
        engine.check_lease(ws, b["job_id"], b["lease_token"], b["fencing_token"], kinds=("dispatch",), action_id=aid)
        if action["connector"] != "linear":
            raise ApiError(422, "POLICY_VIOLATION", "local actions use execute-local, not begin-dispatch")
        if not engine.linear_enabled():
            raise ApiError(503, "CAPABILITY_DISABLED", "linear connector is unconfigured", retryable=False)
        if action["status"] != "accepted":
            raise ApiError(409, "INVALID_TRANSITION", f"action is {action['status']}")
        if b["expected_action_version"] != action["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_action_version does not match", {"version": action["version"]})
        if b["expected_revision"] != ws.revision:
            raise ApiError(409, "REVISION_CONFLICT", "expected_revision does not match current knowledge_revision",
                           {"knowledge_revision": ws.revision})
        engine.revalidate(ws, action)
        action["status"] = "dispatching"
        touch(action)
        pid = new_id("permit")
        permit = {"permit_id": pid, "expires_at": iso(plus(15)), "payload_hash": engine.payload_hash(ws, action),
                  "action_version": action["version"]}
        ws.permits[pid] = {**permit, "action_id": aid, "remote_preconditions": b["remote_preconditions"]}
        return 200, permit, "DispatchPermit"
    return await command(request, wid, "POST /actions/{action_id}/begin-dispatch", "BeginDispatch", h, private=True)


@app.post(PRIVATE + "/actions/{aid}/execute-local")
async def execute_local(request: Request, wid: str, aid: str):
    def h(ctx: Ctx, b: dict):
        ws = ctx.ws
        action = ws.get("actions", aid, "action")
        engine.check_lease(ws, b["job_id"], b["lease_token"], b["fencing_token"], kinds=("dispatch",), action_id=aid)
        if action["connector"] != "local":
            raise ApiError(422, "POLICY_VIOLATION", "only local-connector actions can execute locally")
        if action["status"] == "succeeded" and action["receipt"]:
            return 200, pub(action), "Action"  # readback of an already-applied effect
        if action["status"] == "dispatching" and aid in ws.local_effects:
            eff = ws.local_effects[aid]  # effect applied before a lost response: dedupe by action marker, read back
            return 200, pub(engine.finalize_local(ws, action, eff)), "Action"
        if action["status"] != "accepted":
            raise ApiError(409, "INVALID_TRANSITION", f"action is {action['status']}")
        if b["expected_action_version"] != action["version"]:
            raise ApiError(409, "REVISION_CONFLICT", "expected_action_version does not match", {"version": action["version"]})
        engine.revalidate(ws, action)
        action["status"] = "dispatching"
        touch(action)
        eff = engine.perform_local_effect(ws, action)
        if store.take("local_create_timeout_after_effect"):
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "local store timed out after the effect may have been applied "
                           "(injected fault); outcome unknown — report unknown or retry with the same Idempotency-Key",
                           {"fault": "local_create_timeout_after_effect", "action_id": aid}, retryable=True)
        return 200, pub(engine.finalize_local(ws, action, eff)), "Action"
    return await command(request, wid, "POST /actions/{action_id}/execute-local", "ExecuteLocal", h, private=True)
