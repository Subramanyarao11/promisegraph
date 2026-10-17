"""Process liveness and database readiness (absolute paths, no auth)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import contract
from ..deps import StoreDep
from ..errors import ApiError
from ..http import error_body

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request, store: StoreDep) -> JSONResponse:
    if not store.ping():
        err = ApiError(503, "DEPENDENCY_UNAVAILABLE", "graph database unavailable", retryable=True)
        return JSONResponse(error_body(request, err, None, None), status_code=503)
    return JSONResponse({"status": "ready", "database": "ok", "contract": contract.contract_hash_line()})
