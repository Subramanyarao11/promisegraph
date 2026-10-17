"""PromiseGraph API (Agent A): public /api/v1 and private /internal/v1 routes over FalkorDB.

Routers: routes/health.py, routes/reads.py (public reads + read-only context tools), routes/commands.py
(idempotent public commands), routes/private.py (worker protocol). Auth/workspace binding: deps.py.
Envelopes and the idempotent write wrapper: http.py. OpenAPI from the frozen schemas: openapi_doc.py.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import contract, openapi_doc
from .config import Settings
from .errors import ApiError, invalid
from .http import CONTRACT_VERSION, correlation_id, error_body
from .routes import commands, health, private, reads
from .store import Store

log = logging.getLogger("promisegraph.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    contract.configure(settings.contracts_dir)
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield  # FalkorDB connects lazily on first use, so startup never fails when the database is down (T21)
        await run_in_threadpool(store.close)

    app = FastAPI(title="PromiseGraph API", version=CONTRACT_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store

    @app.middleware("http")
    async def correlation(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.correlation_id = correlation_id(request)
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = request.state.correlation_id
        return response

    # Sync handler on purpose: filling meta.knowledge_revision reads FalkorDB (blocking client), so Starlette must
    # run it in the threadpool rather than on the event loop.
    @app.exception_handler(ApiError)
    def api_error(request: Request, err: ApiError) -> JSONResponse:
        ws = request.path_params.get("workspace_id")
        if err.code == "DEPENDENCY_UNAVAILABLE":
            ws = None  # do not touch the database again to fill meta
        return JSONResponse(error_body(request, err, store, ws), status_code=err.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = {"/".join(str(x) for x in e.get("loc", [])): str(e.get("msg"))[:300] for e in exc.errors()[:40]}
        return JSONResponse(error_body(request, invalid("request validation failed", fields), None, None),
                            status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Unknown routes / wrong methods also answer with the contract ErrorEnvelope (not `{"detail": ...}`)."""
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, f"HTTP_{exc.status_code}")
        err = ApiError(exc.status_code, code, str(exc.detail) or code)
        return JSONResponse(error_body(request, err, None, None), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        err = ApiError(500, "INTERNAL", f"internal error: {type(exc).__name__}")
        return JSONResponse(error_body(request, err, None, None), status_code=500)

    for r in (health.router, reads.router, commands.router, private.router):
        app.include_router(r)
    openapi_doc.install(app, settings.contracts_dir)
    return app


app = create_app()
