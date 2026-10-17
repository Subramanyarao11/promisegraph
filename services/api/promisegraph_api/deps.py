"""FastAPI dependencies: settings, store, and the authenticated + workspace-bound request context.

Order of checks (PROTOCOL rev 2 #15): bearer token (401) -> known workspace (404) -> workspace ACL
(403 WORKSPACE_FORBIDDEN) -> credential class for the route (403 UNAUTHORIZED). The graph is selected only after
these checks, from the server-owned workspace registry. These dependencies do no I/O (no FalkorDB access), so they
are `async def` and run on the event loop without a threadpool hop; everything that touches the blocking FalkorDB
client (handlers, the ApiError handler) is sync `def` and runs in the threadpool.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, Depends, Request

from .config import Settings
from .core import Ctx
from .errors import ApiError, not_found
from .store import Store


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_store(request: Request) -> Store:
    return request.app.state.store


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[Store, Depends(get_store)]


def authenticate(request: Request, settings: Settings) -> str:
    h = request.headers.get("Authorization", "")
    tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
    if tok and settings.operator_token and tok == settings.operator_token:
        return "operator"
    if tok and settings.worker_token and tok == settings.worker_token:
        return "worker"
    raise ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token")


def _ctx(request: Request, workspace_id: str, settings: Settings, store: Store, *, private: bool = False,
         operator_only: bool = False) -> Ctx:
    role = authenticate(request, settings)
    if workspace_id not in settings.workspaces:
        raise not_found("workspace", workspace_id)
    if workspace_id not in settings.acl.get(role, set()):
        raise ApiError(403, "WORKSPACE_FORBIDDEN", "caller is not permitted in this workspace")
    if private and role != "worker":
        raise ApiError(403, "UNAUTHORIZED", "private routes require the worker service credential")
    if operator_only and role != "operator":
        raise ApiError(403, "UNAUTHORIZED", "operator credential required")
    actor = "person_operator" if role == "operator" else "worker:unknown"
    return Ctx(ws=workspace_id, role=role, actor_id=actor, store=store, settings=settings,
               correlation_id=request.state.correlation_id)


async def reader_ctx(request: Request, workspace_id: str, settings: SettingsDep, store: StoreDep) -> Ctx:
    """Operator or worker (public reads, context tools, and lease-bound worker mutations)."""
    return _ctx(request, workspace_id, settings, store)


async def operator_ctx(request: Request, workspace_id: str, settings: SettingsDep, store: StoreDep) -> Ctx:
    return _ctx(request, workspace_id, settings, store, operator_only=True)


async def worker_ctx(request: Request, workspace_id: str, settings: SettingsDep, store: StoreDep) -> Ctx:
    return _ctx(request, workspace_id, settings, store, private=True)


ReaderCtx = Annotated[Ctx, Depends(reader_ctx)]
OperatorCtx = Annotated[Ctx, Depends(operator_ctx)]
WorkerCtx = Annotated[Ctx, Depends(worker_ctx)]

# Command bodies are validated against the frozen JSON schemas (contract.check), not re-modelled in Pydantic,
# so the exact contract (oneOf payloads, nullability, bounds) and its error details stay authoritative.
JsonBody = Annotated[Any, Body()]
