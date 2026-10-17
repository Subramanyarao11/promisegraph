"""Public commands: every handler goes through the idempotent `mutate` wrapper (one atomic graph write)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import domain, jobs
from ..deps import JsonBody, OperatorCtx, ReaderCtx
from ..http import body_of, mutate
from ..openapi_doc import doc

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["public commands"])


@router.post("/requests", **doc("RequestCreateResult", body="RequestCreate", status=201))
def post_request(request: Request, ctx: OperatorCtx, body: JsonBody = None) -> Response:
    cmd = body_of(body, "RequestCreate")
    return mutate(request, ctx, "/requests", cmd, lambda u: domain.intake(ctx, u, cmd))


@router.post("/requests/{request_id}/runs", **doc("Run", body="RunCreate", status=202))
def post_run(request: Request, ctx: OperatorCtx, request_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "RunCreate")
    return mutate(request, ctx, "/requests/{request_id}/runs", cmd, lambda u: domain.create_run(ctx, u, request_id, cmd))


@router.post("/runs/{run_id}/cancel", **doc("Run", body="RunCancel"))
def post_cancel(request: Request, ctx: OperatorCtx, run_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "RunCancel")
    return mutate(request, ctx, "/runs/{run_id}/cancel", cmd, lambda u: domain.cancel_run(ctx, u, run_id, cmd))


@router.post("/runs/{run_id}/resume", **doc("Run", body="RunResume", status=202))
def post_resume(request: Request, ctx: OperatorCtx, run_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "RunResume")
    return mutate(request, ctx, "/runs/{run_id}/resume", cmd, lambda u: domain.resume_run(ctx, u, run_id, cmd))


@router.post("/requests/{request_id}/replies", **doc("ReplyResult", body="ReplyCreate", status=201))
def post_reply(request: Request, ctx: OperatorCtx, request_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "ReplyCreate")
    return mutate(request, ctx, "/requests/{request_id}/replies", cmd, lambda u: domain.reply(ctx, u, request_id, cmd))


@router.post("/proposals", **doc("Proposal", body="ProposalCreate", status=201))
def post_proposal(request: Request, ctx: ReaderCtx, body: JsonBody = None) -> Response:
    """Operator, or worker with X-Job-Id/X-Lease-Token/X-Fencing-Token for a live lease."""
    cmd = body_of(body, "ProposalCreate")
    return mutate(request, ctx, "/proposals", cmd, lambda u: domain.create_proposal(ctx, u, cmd), lease=True)


@router.post("/proposals/{proposal_id}/commit", **doc("ProposalCommitResult", body="ProposalCommit", status=202))
def post_commit(request: Request, ctx: ReaderCtx, proposal_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "ProposalCommit")
    return mutate(request, ctx, "/proposals/{proposal_id}/commit", cmd,
                  lambda u: domain.commit_proposal(ctx, u, proposal_id, cmd, ctx.idem_key), lease=True)


@router.post("/requests/{request_id}/observations", **doc("ObservationResult", body="ObservationCreate", status=201))
def post_observation(request: Request, ctx: ReaderCtx, request_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "ObservationCreate")
    return mutate(request, ctx, "/requests/{request_id}/observations", cmd,
                  lambda u: domain.record_observation(ctx, u, request_id, cmd), lease=True)


@router.post("/requests/{request_id}/refresh", **doc("RefreshResult", body="RefreshCreate", status=202))
def post_refresh(request: Request, ctx: OperatorCtx, request_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "RefreshCreate")
    return mutate(request, ctx, "/requests/{request_id}/refresh", cmd, lambda u: domain.refresh(ctx, u, request_id, cmd))


@router.patch("/work-items/{work_item_id}", **doc("WorkItemUpdateResult", body="WorkItemUpdate"))
def patch_work(request: Request, ctx: OperatorCtx, work_item_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "WorkItemUpdate")
    return mutate(request, ctx, "/work-items/{work_item_id}", cmd,
                  lambda u: domain.patch_work_item(ctx, u, work_item_id, cmd))


@router.post("/decisions", **doc("Decision", body="DecisionCommand", status=201))
def post_decision(request: Request, ctx: OperatorCtx, body: JsonBody = None) -> Response:
    """201 for record/supersede, 200 for revoke (PROTOCOL rev 2 #8)."""
    cmd = body_of(body, "DecisionCommand")
    return mutate(request, ctx, "/decisions", cmd, lambda u: domain.decision_command(ctx, u, cmd))


@router.post("/imports/preview", **doc("ImportPreviewResult", body="ImportPreview", status=201))
def post_preview(request: Request, ctx: OperatorCtx, body: JsonBody = None) -> Response:
    cmd = body_of(body, "ImportPreview")
    return mutate(request, ctx, "/imports/preview", cmd, lambda u: domain.import_preview(ctx, u, cmd))


@router.post("/imports/{preview_id}/commit", **doc("ImportCommitResult", body="ImportCommit"))
def post_import_commit(request: Request, ctx: OperatorCtx, preview_id: str, body: JsonBody = None) -> Response:
    cmd = body_of(body, "ImportCommit")
    return mutate(request, ctx, "/imports/{preview_id}/commit", cmd,
                  lambda u: domain.import_commit(ctx, u, preview_id, cmd))


@router.post("/actions/{action_id}/reconcile", **doc("Action", body="ActionReconcile"))
def post_reconcile(request: Request, ctx: OperatorCtx, action_id: str, body: JsonBody = None) -> Response:
    """PROTOCOL rev 3 #20 / rev 4 #28."""
    cmd = body_of(body, "ActionReconcile")
    return mutate(request, ctx, "/actions/{action_id}/reconcile", cmd,
                  lambda u: jobs.operator_reconcile(ctx, u, action_id, cmd))


@router.post("/requests/{request_id}/close", **doc("Request", body="RequestClose"))
def post_close(request: Request, ctx: OperatorCtx, request_id: str, body: JsonBody = None) -> Response:
    """PROTOCOL rev 3 #21 / #25."""
    cmd = body_of(body, "RequestClose")
    return mutate(request, ctx, "/requests/{request_id}/close", cmd,
                  lambda u: domain.close_request(ctx, u, request_id, cmd))
