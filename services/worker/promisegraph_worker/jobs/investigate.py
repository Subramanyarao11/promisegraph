"""Investigate jobs: the bounded planner/tool loop (fresh investigation, clarification resume, continuation)."""

from __future__ import annotations

from typing import Any

from ..config import Limits
from ..http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, LeaseLost
from ..planner.base import CapabilityDisabled, Outcome, PlanContext, Planner
from ..tools import ToolClient, collect_ids, redact, result_count
from .lease import LeaseKeeper
from .session import SCRIPTED_LABEL, BudgetExhausted, Cancelled, JobSession


class InvestigateRunner:
    def __init__(self, api: ApiClient, limits: Limits, planners: dict[str, Planner]) -> None:
        self.api = api
        self.limits = limits
        self.planners = planners

    # -- helpers -----------------------------------------------------------------------------------------
    def _get(self, ws: str, path: str, **kw: Any) -> Any:
        data, _ = self.api.call("GET", ApiClient.public(ws, path), **kw)
        return data

    def _prior_clarifications(self, ws: str, run: dict[str, Any]) -> tuple[int, str | None]:
        """Committed clarifications on this run (harness read; survives checkpoint shapes that drop worker keys)."""
        try:
            props = self._get(ws, "/proposals", params={"run_id": run["id"], "request_id": run["request_id"], "limit": 100}) or []
        except (ApiError, ApiUnavailable, ApiTimeout):
            return 0, None
        clar = [p for p in props if p.get("kind") == "clarify" and p.get("validity") == "valid"]
        question = clar[0]["payload"]["question"] if clar else None
        return len(clar), question

    # -- run ---------------------------------------------------------------------------------------------
    def run(self, keeper: LeaseKeeper, info: dict[str, Any]) -> dict[str, Any]:
        ws = keeper.workspace_id
        run = self._get(ws, f"/runs/{keeper.run_id}")
        request = self._get(ws, f"/requests/{run['request_id']}")
        mode = run.get("mode", "scripted")
        session = JobSession(self.api, keeper, limits=self.limits, run=run, mode=mode)
        cp = keeper.checkpoint or {}
        resume_stage = cp.get("resume_stage") or session.state.resume_stage
        pending_action = cp.get("action_id") or cp.get("pending_action_id") or keeper.action_id
        if run.get("status") == "cancelled":
            return self._finish(session, Outcome("failed", run.get("stage", "investigate"), "cancelled", "Run already cancelled.", error={"code": "CANCELLED", "message": "run cancelled before work started"}))

        planner = self.planners.get(mode)
        if planner is None:
            err = {"code": "CAPABILITY_DISABLED", "message": f"planner mode {mode!r} is not enabled in this worker; it is never replaced by another planner"}
            session.emit("error", "error", err)
            return self._finish(session, Outcome("failed", "investigate", "failed", err["message"], error=err))

        clarifications = max(session.state.clarifications, 0)
        clarify_question = session.state.extra.get("clarify_question")
        if resume_stage in ("observe", "verify") or cp.get("reply_source_id") or clarifications == 0:
            n, q = self._prior_clarifications(ws, run)
            clarifications = max(clarifications, n)
            clarify_question = clarify_question or q
        stage = "observe" if resume_stage in ("observe", "verify") else "investigate"
        session.state.stage = stage
        session.state.pending_action_id = pending_action if stage == "observe" else None
        session.state.resume_stage = resume_stage

        tools = ToolClient(
            self.api,
            workspace_id=ws,
            request=request,
            run_id=run["id"],
            lease_headers=keeper.headers,
            ensure_lease=keeper.ensure,
            auto_commit_kinds=list(info.get("auto_commit_kinds") or ["answer", "clarify"]),
            max_result_bytes=self.limits.max_tool_result_bytes,
        )
        tools.knowledge_revision = (run.get("based_on_revision") if isinstance(run.get("based_on_revision"), int) else None)

        def replan(reason: str) -> None:
            session.state.usage.replans += 1
            session.check_budget(needs_tool_call=False)

        ctx = PlanContext(
            workspace_id=ws,
            request=request,
            run=run,
            info=info,
            new_call_id=session.next_call_id,
            replan=replan,
            resume_stage=resume_stage,
            pending_action_id=pending_action,
            clarifications=clarifications,
            seen_evidence_ids=list(session.state.seen_evidence_ids),
            extra={"reply_source_id": cp.get("reply_source_id"), "clarify_question": clarify_question, "knowledge_revision": run.get("based_on_revision"),
                   "budget_left": lambda: session.budget.max_tool_calls - session.job_tool_calls,
                   "emit_model_usage": lambda payload: session.emit(f"{payload['call_id']}:u", "tool_completed", payload, tool_name="model")},
        )
        try:
            session.emit(f"run_started:a{keeper.attempt}", "run_started", {"mode": mode, "stage": stage, "label": SCRIPTED_LABEL if mode == "scripted" else "Model mode", "attempt": keeper.attempt, "resume_reason": cp.get("resume_reason"), "reply_source_id": cp.get("reply_source_id")})
            outcome = self._loop(session, planner, tools, ctx)
        except BudgetExhausted as exc:
            err = {"code": "BUDGET_EXHAUSTED", "message": exc.reason}
            session.emit("budget", "error", err)
            outcome = Outcome("failed", session.state.stage, "failed", f"Budget exhausted: {exc.reason}. Checkpoint preserved; an operator may resume.", error=err)
        except Cancelled:
            outcome = Outcome("failed", session.state.stage, "cancelled", "Run cancelled; planning stopped (in-flight effects are still reconciled).", error={"code": "CANCELLED", "message": "run cancelled"})
        except CapabilityDisabled as exc:
            err = {"code": "CAPABILITY_DISABLED", "message": str(exc)}
            session.emit("error", "error", err)
            outcome = Outcome("failed", "investigate", "failed", str(exc), error=err)
        # persist planner-side bookkeeping for a later resume
        session.state.seen_evidence_ids = list(ctx.extra.get("seen_evidence_ids") or session.state.seen_evidence_ids)[-200:]
        if ctx.extra.get("clarified"):
            session.state.clarifications = clarifications + 1
            session.state.extra["clarify_question"] = ctx.extra.get("clarify_question")
        return self._finish(session, outcome)

    def _loop(self, session: JobSession, planner: Planner, tools: ToolClient, ctx: PlanContext) -> Outcome:
        gen = planner.plan(ctx)
        result: dict[str, Any] | None = None
        while True:
            try:
                call = gen.send(result) if result is not None else next(gen)
            except StopIteration as stop:
                outcome: Outcome = stop.value
                return outcome
            result = self._execute(session, tools, call, ctx)

    def _execute(self, session: JobSession, tools: ToolClient, call: dict[str, Any], ctx: PlanContext) -> dict[str, Any]:
        call_id, name = call["call_id"], call["name"]
        replay = session.is_completed(call_id)
        if not replay:
            session.check_cancel(refresh=True)
            session.check_budget()
            session.emit(f"{call_id}:s", "tool_started", {"call_id": call_id, "arguments": redact(call.get("arguments") or {})}, tool_name=name)
        result = tools.execute(call)
        if isinstance(result.get("meta", {}).get("knowledge_revision"), int):
            ctx.extra["knowledge_revision"] = result["meta"]["knowledge_revision"]
        if replay:
            return result
        session.state.usage.tool_calls += 1
        session.job_tool_calls += 1
        session.state.completed_call_ids.append(call_id)
        err = result.get("error") or {}
        session.emit(
            f"{call_id}:d",
            "tool_completed",
            {
                "call_id": call_id,
                "ok": result["ok"],
                "result_count": result_count(result.get("data")),
                "ids": collect_ids(result.get("data")),
                "duration_ms": int(result.get("meta", {}).get("duration_ms", 0)),
                "error_code": err.get("code"),
                "truncated": bool(result.get("meta", {}).get("truncated_by_worker") or (isinstance(result.get("data"), dict) and result["data"].get("truncated"))),
            },
            tool_name=name,
        )
        data = result.get("data") or {}
        if name == "prepare_action" and result["ok"]:
            session.state.proposal_ids.append(data["id"])
            session.state.stage = "prepare"
            session.emit(f"{call_id}:p", "proposal_ready", {"proposal_id": data["id"], "kind": data.get("kind"), "validity": data.get("validity"),
                                                             "coverage_notes": list(ctx.extra.get("coverage_notes") or [])[:10]})
        elif name == "commit_action" and result["ok"]:
            action = data.get("action") or {}
            if action.get("id"):
                session.state.action_ids.append(action["id"])
                session.state.pending_action_id = action["id"]
                session.state.stage = "act"
        return result

    def _finish(self, session: JobSession, outcome: Outcome) -> dict[str, Any]:
        session.state.stage = outcome.stage if outcome.stage else session.state.stage
        result: dict[str, Any] = {
            "run_status_hint": outcome.run_status_hint,
            "stage": outcome.stage,
            "proposal_ids": outcome.proposal_ids or session.state.proposal_ids[-5:],
            "action_ids": outcome.action_ids,
            "summary": outcome.summary[:2000],
            "evidence_ids": outcome.evidence_ids[:100],
            "planner": session.state.mode,
            "label": SCRIPTED_LABEL if session.state.mode == "scripted" else "Model mode",
            "usage": session.state.usage.as_dict(),
        }
        if outcome.kind:
            result["kind"] = outcome.kind
        if outcome.awaiting:
            result["awaiting"] = outcome.awaiting
        if outcome.error:
            result["error"] = {"code": outcome.error.get("code", "FAILED"), "message": str(outcome.error.get("message", ""))[:2000]}
            if isinstance(outcome.error.get("details"), dict):
                result["error"]["details"] = outcome.error["details"]
        for k, v in outcome.extra.items():
            result.setdefault(k, v)
        try:
            receipt = session.complete(outcome.completion, result)
        except ApiError as err:
            if err.code == "INVALID_TRANSITION" and outcome.run_status_hint == "cancelled":
                receipt = None
            else:
                raise
        return {"outcome": outcome, "receipt": receipt, "events": session.events}
