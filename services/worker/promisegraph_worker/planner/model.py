"""ModelPlanner: live model planning over the identical 8 tools, policies and API (PROTOCOL.md Rev 6).

* Provider-neutral loop; provider adapters live in `providers.py` (OpenAI chosen; Anthropic kept).
* Hard spend guard (`spend.py`): worst-case reservation before every call, settle from actual usage, refuse and fail
  the run with CAPABILITY_DISABLED / reason AI_BUDGET_EXHAUSTED when the budget would be exceeded.
* Citations: `prepare_action.evidence_ids` must be ids that tools returned in this run; otherwise the call is
  refused locally (the model gets a tool error) and nothing reaches the API.
* Compact context: short system prompt, the compact tool catalog, and tool results trimmed to excerpts by id.
* Usage events: one `tool_completed` event per model call, `tool_name: "model"`, no prompts or reasoning stored.
* With AI_ENABLED=false the planner cannot be constructed, so no provider call is possible.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from ..http import Backoff
from ..tools import TOOL_NAMES, TOOL_SCHEMAS
from .base import CapabilityDisabled, Outcome, PlanContext, PlanGenerator
from .providers import PROVIDERS, ProviderError
from .spend import BudgetExhausted, SpendGuard, estimate_tokens

SYSTEM_PROMPT = (
    "You investigate one customer request for PromiseGraph using only the provided tools. Gather evidence (search, "
    "expand, memory), then call prepare_action exactly once with one kind: answer, clarify, link_work, create_work or "
    "escalate. Cite only evidence ids that tools returned. Prefer existing compatible work over new work; ask one "
    "targeted question if the request is ambiguous; escalate conflicting active decisions; never recommend a "
    "forbidden workaround or promise a date without a recorded forecast. If the proposal is valid and its kind is "
    "in auto_commit_kinds, call commit_action. Then reply with a one-sentence summary and no tool call.\n"
    "Write `explanation` and every rejected alternative `reason` for a support lead: short plain sentences, work "
    "titles in quotes, people's names, dates like '13 Oct'. Never put ids, scope keys, tool names or byte limits in "
    "that prose (ids belong only in evidence_ids). Example explanation: \"Engineering already has this: 'Restore "
    "scheduled exports' (owner Maya Raman). It's blocked behind 'Queue v2 cutover', whose owner has left, so we "
    "can't promise a date.\" Example reason: \"Not 'Run now' again: it already failed for Acme on 13 Oct.\" "
    "Mention incomplete information only if it changes the recommendation, in one sentence.\n"
    "Grounding: answer, link_work and create_work must cite at least one documentation or work-item evidence id "
    "(from search_context/expand_context/read_memory results); the request's own text is never enough. Search "
    "sources for documentation, then call read_memory with the capability you identified in `scopes` so verified "
    "and failed prior attempts for that capability are included."
)
RESULT_CHARS = 6000


def compact(value: Any, *, depth: int = 0) -> Any:
    """Trim a tool result for the prompt: long strings cut, long lists shortened. Ids are always kept."""
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + "…"
    if isinstance(value, list):
        out = [compact(v, depth=depth + 1) for v in value[:8]]
        if len(value) > 8:
            out.append(f"…{len(value) - 8} more")
        return out
    if isinstance(value, dict):
        return {k: compact(v, depth=depth + 1) for k, v in value.items()}
    return value


def tool_result_text(result: dict[str, Any]) -> str:
    payload = {"ok": result["ok"], "error": result.get("error"), "data": compact(result.get("data"))}
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…[truncated; ask a narrower question]"


def returned_evidence_ids(data: Any, out: set[str]) -> None:
    """Collect evidence ids a tool actually returned (EvidenceRefs and evidence_ids lists)."""
    if isinstance(data, dict):
        if isinstance(data.get("id"), str) and "excerpt" in data and "source_id" in data:
            out.add(data["id"])
        for key, val in data.items():
            if key == "evidence_ids" and isinstance(val, list):
                out.update(v for v in val if isinstance(v, str))
            else:
                returned_evidence_ids(val, out)
    elif isinstance(data, list):
        for v in data:
            returned_evidence_ids(v, out)


def evidence_sources(data: Any, out: dict[str, str]) -> None:
    """evidence id -> source id for every EvidenceRef a tool returned."""
    if isinstance(data, dict):
        if isinstance(data.get("id"), str) and "excerpt" in data and isinstance(data.get("source_id"), str):
            out[data["id"]] = data["source_id"]
        for v in data.values():
            evidence_sources(v, out)
    elif isinstance(data, list):
        for v in data:
            evidence_sources(v, out)


def capability_ids(data: Any) -> list[str]:
    """Capability candidates (search) and capability nodes (expand), in returned order."""
    out: list[str] = []
    if isinstance(data, dict):
        for c in data.get("candidates") or []:
            if isinstance(c, dict) and c.get("entity_type") == "capability":
                out.append(c["id"])
        for n in data.get("nodes") or []:
            if isinstance(n, dict) and str(n.get("label", "")).lower() == "capability":
                out.append(n["id"])
    return out


class ModelPlanner:
    mode = "model"
    label = "Model mode"

    def __init__(
        self,
        *,
        enabled: bool,
        provider: str,
        api_key: str,
        model: str,
        guard: SpendGuard | None,
        max_calls_per_run: int = 12,
        max_output_tokens: int = 1500,
        transport: httpx.BaseTransport | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        if not enabled:
            raise CapabilityDisabled("model planner is disabled (AI_ENABLED=false); no model calls are made")
        if provider not in PROVIDERS or not api_key or not model or guard is None:
            raise CapabilityDisabled("model planner is not configured (AI_PROVIDER/AI_API_KEY/AI_MODEL/spend guard)")
        self.provider_name, self.model, self._api_key = provider, model, api_key
        self.guard = guard
        self.max_calls_per_run = max_calls_per_run
        self.max_output_tokens = max_output_tokens
        self._transport, self._backoff = transport, backoff

    @staticmethod
    def tools() -> list[dict[str, Any]]:
        return [{"name": n, "description": s["description"], "input_schema": s["input_schema"]} for n, s in TOOL_SCHEMAS.items()]

    @staticmethod
    def brief(ctx: PlanContext) -> str:
        req = ctx.request
        return json.dumps({
            "request": {k: req.get(k) for k in ("id", "customer_id", "subject", "body", "status", "capability_ids", "linked_work_ids")},
            "resume_stage": ctx.resume_stage, "pending_action_id": ctx.pending_action_id, "reply_source_id": ctx.extra.get("reply_source_id"),
            "auto_commit_kinds": ctx.auto_commit_kinds, "reference_time": ctx.reference_time,
            "connectors": (ctx.info or {}).get("connectors"),
        }, separators=(",", ":"), ensure_ascii=False)

    def _fail(self, code: str, message: str, *, retryable: bool = False, reason: str | None = None, stage: str = "investigate") -> Outcome:
        error = {"code": code, "message": message}
        if reason:
            error["details"] = {"reason": reason}
        return Outcome("retryable_failure" if retryable else "failed", stage, "queued" if retryable else "failed", f"Model mode: {message}", error=error)

    def plan(self, ctx: PlanContext) -> PlanGenerator:
        if ctx.resume_stage in ("observe", "verify"):
            # Post-dispatch observation is deterministic bookkeeping: no model calls, no spend (Rev 6 budget stays
            # for investigation). Same tools and policies as scripted mode.
            from .scripted import ScriptedPlanner

            return (yield from ScriptedPlanner()._observe(ctx))
        run_id = str((ctx.run or {}).get("id") or "norun")
        emit: Callable[[dict[str, Any]], None] = ctx.extra.get("emit_model_usage") or (lambda _p: None)
        provider = PROVIDERS[self.provider_name](api_key=self._api_key, model=self.model, transport=self._transport, backoff=self._backoff)
        provider.start(SYSTEM_PROMPT, self.brief(ctx), self.tools())
        seen: set[str] = set()
        evidence_source: dict[str, str] = {}
        capabilities_seen: list[str] = []
        own_sources = {ctx.request.get("source_id")} - {None}
        proposal_ids: list[str] = []
        action_ids: list[str] = []
        kind: str | None = None
        while True:
            if self.guard.run_calls(run_id) >= self.max_calls_per_run:
                return self._fail("BUDGET_EXHAUSTED", f"model call limit per run reached ({self.max_calls_per_run})")
            call_id = ctx.new_call_id() + ":model"
            estimate = estimate_tokens(provider.request_preview(self.max_output_tokens))
            try:
                reservation_id, _reserved = self.guard.reserve(estimate, run_id=run_id)
            except BudgetExhausted as exc:
                emit({"call_id": call_id, "ok": False, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                      "budget_remaining_usd": round(max(0.0, exc.budget - exc.spent), 6), "error_code": "AI_BUDGET_EXHAUSTED"})
                return self._fail("CAPABILITY_DISABLED", str(exc), reason="AI_BUDGET_EXHAUSTED")
            try:
                reply = provider.complete(self.max_output_tokens)
            except ProviderError as exc:
                self.guard.release(reservation_id, billed_unknown=exc.billed_unknown)
                emit({"call_id": call_id, "ok": False, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                      "budget_remaining_usd": round(self.guard.remaining(), 6), "error_code": exc.code})
                return self._fail(exc.code, str(exc), retryable=exc.retryable)
            cost, remaining = self.guard.settle(reservation_id, reply.input_tokens, reply.output_tokens)
            emit({"call_id": call_id, "ok": True, "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                  "cost_usd": round(cost, 6), "budget_remaining_usd": round(remaining, 6)})

            if not reply.tool_calls:
                summary = (reply.text or "").strip()[:2000]
                if action_ids:
                    return Outcome("succeeded", "act", "queued", summary or "Committed.", kind=kind, proposal_ids=proposal_ids, action_ids=action_ids)
                if proposal_ids:
                    return Outcome("waiting", "decide", "awaiting_review", summary or "Awaiting review.", kind=kind, proposal_ids=proposal_ids, awaiting="review")
                if ctx.resume_stage in ("observe", "verify"):
                    return Outcome("succeeded", "observe", "succeeded", summary or "Observed.", action_ids=[ctx.pending_action_id] if ctx.pending_action_id else [])
                return self._fail("NO_PROPOSAL", "model finished without a proposal")

            results: list[tuple[str, str, bool]] = []
            for tc in reply.tool_calls:
                local_error = None
                if tc.parse_error:
                    local_error = {"code": "INVALID_INPUT", "message": tc.parse_error}
                elif tc.name not in TOOL_NAMES:
                    local_error = {"code": "INVALID_INPUT", "message": f"unknown tool {tc.name!r}"}
                elif tc.name == "prepare_action":
                    cited = [e for e in tc.arguments.get("evidence_ids") or [] if isinstance(e, str)]
                    unknown = [e for e in cited if e not in seen]
                    grounded = [e for e in cited if e in seen and evidence_source.get(e) not in own_sources]
                    if unknown:
                        local_error = {"code": "EVIDENCE_INVALID", "message": f"cite only evidence ids returned by tools; not returned: {unknown[:10]}"}
                    elif tc.arguments.get("kind") in ("answer", "link_work", "create_work") and not grounded:
                        local_error = {"code": "EVIDENCE_INVALID", "message": "cite at least one documentation or work-item evidence id; the request's own text is not evidence. Search sources or expand the capability first."}
                elif tc.name == "read_memory" and capabilities_seen:
                    scopes = list(tc.arguments.get("scopes") or [])
                    if not any(sc.get("entity_type") == "capability" for sc in scopes if isinstance(sc, dict)):
                        # Include capability-scoped attempts/decisions, as scripted mode does.
                        tc.arguments["scopes"] = (scopes + [{"entity_type": "capability", "entity_id": c} for c in capabilities_seen[:2]])[:8]
                if local_error:
                    results.append((tc.id, json.dumps({"ok": False, "error": local_error}), True))
                    continue
                result = yield {"call_id": ctx.new_call_id(), "name": tc.name, "arguments": tc.arguments}
                if result["ok"]:
                    returned_evidence_ids(result.get("data"), seen)
                    evidence_sources(result.get("data"), evidence_source)
                    for cid in capability_ids(result.get("data")):
                        if cid not in capabilities_seen:
                            capabilities_seen.append(cid)
                    data = result.get("data") or {}
                    if tc.name == "prepare_action" and data.get("id"):
                        proposal_ids.append(data["id"])
                        kind = data.get("kind")
                        if data.get("validity") != "valid":
                            ctx.replan(f"proposal {data.get('validity')}")
                    if tc.name == "commit_action" and (data.get("action") or {}).get("id"):
                        action_ids.append(data["action"]["id"])
                results.append((tc.id, tool_result_text(result), not result["ok"]))
            provider.add_tool_results(results)
