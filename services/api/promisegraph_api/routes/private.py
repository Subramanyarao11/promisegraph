"""Private worker protocol (contracts §7). Worker credential only."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import jobs
from ..deps import JsonBody, WorkerCtx
from ..errors import StoreGuardFailed, conflict
from ..http import body_of, mutate, ok
from ..openapi_doc import doc

router = APIRouter(prefix="/internal/v1/workspaces/{workspace_id}", tags=["private worker"])


@router.post("/jobs/claim", **doc("JobLease", body="JobClaim", no_content=True))
def post_claim(request: Request, ctx: WorkerCtx, body: JsonBody = None) -> Response:
    """Lease operation (no Idempotency-Key): JobLease, or 204 when nothing is due."""
    cmd = body_of(body, "JobClaim")
    with ctx.store.write_lock:
        lease = jobs.claim(ctx, cmd)
    if lease is None:
        return Response(status_code=204, headers={"X-Correlation-Id": request.state.correlation_id})
    return ok(request, ctx, lease)


@router.post("/jobs/{job_id}/heartbeat", **doc("JobLease", body="LeaseBinding"))
def post_heartbeat(request: Request, ctx: WorkerCtx, job_id: str, body: JsonBody = None) -> JSONResponse:
    cmd = body_of(body, "LeaseBinding")
    with ctx.store.write_lock:
        try:
            lease = jobs.heartbeat(ctx, job_id, cmd)
        except StoreGuardFailed as exc:
            raise conflict("LEASE_LOST", "lease expired during heartbeat", {"job_id": job_id}) from exc
    return ok(request, ctx, lease)


@router.post("/jobs/{job_id}/checkpoint", **doc("CheckpointResult", body="JobCheckpoint"))
def post_checkpoint(request: Request, ctx: WorkerCtx, job_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "JobCheckpoint")
    return mutate(request, ctx, "/jobs/{job_id}/checkpoint", cmd, lambda u: jobs.checkpoint(ctx, u, job_id, cmd))


@router.post("/jobs/{job_id}/complete", **doc("CompletionReceipt", body="JobComplete"))
def post_complete(request: Request, ctx: WorkerCtx, job_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "JobComplete")
    return mutate(request, ctx, "/jobs/{job_id}/complete", cmd, lambda u: jobs.complete(ctx, u, job_id, cmd))


@router.post("/actions/{action_id}/begin-dispatch", **doc("DispatchPermit", body="BeginDispatch"))
def post_begin(request: Request, ctx: WorkerCtx, action_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "BeginDispatch")
    return mutate(request, ctx, "/actions/{action_id}/begin-dispatch", cmd,
                  lambda u: jobs.begin_dispatch(ctx, u, action_id, cmd))


@router.post("/actions/{action_id}/execute-local", **doc("Action", body="ExecuteLocal"))
def post_execute_local(request: Request, ctx: WorkerCtx, action_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "ExecuteLocal")
    return mutate(request, ctx, "/actions/{action_id}/execute-local", cmd,
                  lambda u: jobs.execute_local(ctx, u, action_id, cmd))
