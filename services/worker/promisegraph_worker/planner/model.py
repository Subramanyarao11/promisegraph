"""ModelPlanner adapter skeleton (G4). Anthropic Messages API tool use over the identical tool catalog.

It makes NO network calls unless AI_ENABLED=true and AI_API_KEY is set: construction with AI disabled raises
CapabilityDisabled, so a `planner_mode: model` run fails honestly with CAPABILITY_DISABLED and is never silently
replaced by the ScriptedPlanner. The official `anthropic` SDK is imported lazily and is not yet in the lockfile;
live activation, dependency pinning and evaluation are G4 work.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..tools import TOOL_SCHEMAS
from .base import CapabilityDisabled, Outcome, PlanContext, PlanGenerator

SYSTEM_PROMPT = (
    "You are PromiseGraph's customer-operations investigator. Use the tools to gather evidence from the workspace "
    "graph, then prepare exactly one typed proposal (answer, clarify, link_work, create_work or escalate) citing only "
    "evidence ids returned by tools. Never promise dates without a recorded forecast. Prefer existing work over new "
    "work, ask one targeted question when the request is ambiguous, and escalate conflicting active decisions. "
    "Commit only kinds the workspace allows the worker to commit. Finish with a one-paragraph summary."
)
MAX_TURNS = 14


class ModelPlanner:
    mode = "model"
    label = "Model mode"

    def __init__(self, *, enabled: bool, api_key: str, model: str = "claude-opus-5-5", client_factory: Callable[[str], Any] | None = None) -> None:
        if not enabled or not api_key:
            raise CapabilityDisabled("model planner is disabled (AI_ENABLED=false or AI_API_KEY missing); no model calls are made")
        self.model = model or "claude-opus-5-5"
        self._client_factory = client_factory or self._default_client
        self._api_key = api_key
        self._client: Any = None

    @staticmethod
    def _default_client(api_key: str) -> Any:  # pragma: no cover - G4
        import anthropic  # lazy: only when enabled

        return anthropic.Anthropic(api_key=api_key)

    def tools(self) -> list[dict[str, Any]]:
        return [{"name": n, "description": s["description"], "input_schema": s["input_schema"]} for n, s in TOOL_SCHEMAS.items()]

    def plan(self, ctx: PlanContext) -> PlanGenerator:  # pragma: no cover - exercised live at G4 only
        if self._client is None:
            self._client = self._client_factory(self._api_key)
        req = ctx.request
        brief = {
            "request": {k: req.get(k) for k in ("id", "customer_id", "subject", "body", "status", "capability_ids", "linked_work_ids")},
            "resume_stage": ctx.resume_stage,
            "pending_action_id": ctx.pending_action_id,
            "auto_commit_kinds": ctx.auto_commit_kinds,
            "reference_time": ctx.reference_time,
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(brief)}]
        proposal_ids: list[str] = []
        action_ids: list[str] = []
        for _ in range(MAX_TURNS):
            response = self._client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=self.tools(),
                messages=messages,
                output_config={"effort": "medium"},
            )
            if response.stop_reason == "refusal":
                return Outcome("failed", "investigate", "failed", "Model refused the request.", error={"code": "MODEL_REFUSAL", "message": "refusal"})
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                text = " ".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
                if action_ids:
                    return Outcome("succeeded", "act", "queued", text[:2000] or "Committed.", proposal_ids=proposal_ids, action_ids=action_ids)
                if proposal_ids:
                    return Outcome("waiting", "decide", "awaiting_review", text[:2000] or "Awaiting review.", proposal_ids=proposal_ids, awaiting="review")
                return Outcome("failed", "investigate", "failed", text[:2000] or "No proposal.", error={"code": "NO_PROPOSAL", "message": "model finished without a proposal"})
            results = []
            for block in uses:
                result = yield {"call_id": ctx.new_call_id(), "name": block.name, "arguments": dict(block.input)}
                data = result.get("data") or {}
                if block.name == "prepare_action" and result["ok"]:
                    proposal_ids.append(data["id"])
                if block.name == "commit_action" and result["ok"] and (data.get("action") or {}).get("id"):
                    action_ids.append(data["action"]["id"])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)[:24000], "is_error": not result["ok"]})
            messages.append({"role": "user", "content": results})
        return Outcome("failed", "investigate", "failed", "Model turn limit reached.", error={"code": "BUDGET_EXHAUSTED", "message": "model turn limit"})
