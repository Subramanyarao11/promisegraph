"""Planner protocol shared by the ScriptedPlanner and the ModelPlanner.

A planner is a generator: it yields model-shaped tool calls `{call_id, name, arguments}`, receives each tool
result `{call_id, ok, data, error, meta}`, and finally returns an `Outcome`. It never touches HTTP directly,
so both planners run under the identical tool client, policies and budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Protocol

ToolCall = dict[str, Any]
ToolResult = dict[str, Any]


@dataclass
class Outcome:
    completion: str  # JobCompletion: succeeded | waiting | retryable_failure | failed
    stage: str  # RunStage reached
    run_status_hint: str  # RunStatus the worker expects (A owns the transition)
    summary: str
    kind: str | None = None
    proposal_ids: list[str] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    awaiting: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)  # (step, type, payload)


@dataclass
class PlanContext:
    workspace_id: str
    request: dict[str, Any]
    run: dict[str, Any] | None
    info: dict[str, Any]
    new_call_id: Callable[[], str]
    replan: Callable[[str], None]
    resume_stage: str | None = None
    pending_action_id: str | None = None
    clarifications: int = 0
    seen_evidence_ids: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def auto_commit_kinds(self) -> list[str]:
        return list(self.info.get("auto_commit_kinds") or ["answer", "clarify"])

    @property
    def reference_time(self) -> str | None:
        return self.info.get("reference_time")

    def connector_state(self, name: str) -> str:
        return ((self.info.get("connectors") or {}).get(name)) or "unavailable"


PlanGenerator = Generator[ToolCall, ToolResult, Outcome]


class Planner(Protocol):
    mode: str
    label: str

    def plan(self, ctx: PlanContext) -> PlanGenerator: ...


class CapabilityDisabled(Exception):
    """Requested planner/connector is not enabled; never silently substitute another one."""
