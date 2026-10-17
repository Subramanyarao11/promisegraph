"""Contract envelopes, pagination helpers and the idempotent mutation wrapper (shared by all routers)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import contract, jobs
from .core import Ctx
from .errors import ApiError, StoreGuardFailed, conflict, invalid
from .store import Store, UnitOfWork
from .util import decode_cursor, encode_cursor, payload_hash, sha256_hex

CONTRACT_VERSION = "1.0.0"
_CORR = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def correlation_id(request: Request) -> str:
    c = request.headers.get("X-Correlation-Id")
    return c if c and _CORR.match(c) else "corr_" + uuid.uuid4().hex[:16]


def meta(request: Request, store: Store | None, ws: str | None, revision: int | None = None) -> dict[str, Any]:
    """`knowledge_revision` 0 means unknown (PROTOCOL rev 3 #24). Blocking when it has to read the graph."""
    if revision is None:
        revision = 0
        if store is not None and ws and ws in store.settings.workspaces:
            try:
                revision = store.knowledge_revision(ws)
            except Exception:  # noqa: BLE001 - meta on error paths must not mask the error
                revision = 0
    return {"contract_version": CONTRACT_VERSION, "knowledge_revision": int(revision),
            "correlation_id": request.state.correlation_id}


def error_body(request: Request, err: ApiError, store: Store | None, ws: str | None,
               revision: int | None = None) -> dict[str, Any]:
    details = err.details if len(err.details) <= 50 else {"truncated": True}
    return {"error": {"code": err.code, "message": err.message, "retryable": err.retryable, "details": details},
            "meta": meta(request, store, ws, revision)}


def ok(request: Request, ctx: Ctx, data: Any, status: int = 200, revision: int | None = None) -> JSONResponse:
    return JSONResponse({"data": data, "meta": meta(request, ctx.store, ctx.ws, revision)}, status_code=status)


def page(request: Request, ctx: Ctx, items: list, offset: int, limit: int, more: bool) -> JSONResponse:
    return JSONResponse({"data": items, "page": {"next_cursor": encode_cursor(offset + limit) if more else None},
                         "meta": meta(request, ctx.store, ctx.ws)})


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


def simple_list(request: Request, ctx: Ctx, label: str, filters: dict, cursor: str | None, limit: int | None,
                order: str = "id", desc: bool = False, contains: dict | None = None,
                mapper: Callable[[dict], dict] | None = None) -> JSONResponse:
    n = lim(limit)
    o = off(cursor)
    rows = ctx.store.find(ctx.ws, label, filters, order=order, desc=desc, skip=o, limit=n + 1, contains=contains)
    items = [mapper(r) if mapper else r["doc"] for r in rows[:n]]
    return page(request, ctx, items, o, n, len(rows) > n)


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


def mutate(request: Request, ctx: Ctx, route: str, body: Any, fn: Callable[[UnitOfWork], tuple[int, Any]],
           lease: bool = False) -> Response:
    """Idempotent command: replay check -> lease -> domain -> ONE atomic write including the idempotency record."""
    store = ctx.store
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
            out = error_body(request, data, store, ctx.ws, revision)
        else:
            out = {"data": data, "meta": meta(request, store, ctx.ws, revision)}
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
