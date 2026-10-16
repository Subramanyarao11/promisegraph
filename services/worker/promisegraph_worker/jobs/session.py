"""Per-job session: resumable checkpoint state, ordered event appends, budget accounting and completion."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Limits
from ..http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, LeaseLost, short_key
from .lease import LeaseKeeper

CHECKPOINT_VERSION = 1
OWN_KEYS = {"checkpoint_version", "mode", "stage", "usage", "completed_call_ids", "pending_action_id", "resume_stage", "emitted_steps",
            "proposal_ids", "action_ids", "evidence_ids", "seen_evidence_ids", "clarifications", "worker_extra"}
SCRIPTED_LABEL = "Scripted mode"


class BudgetExhausted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Cancelled(Exception):
    pass


@dataclass
class Usage:
    tool_calls: int = 0
    replans: int = 0
    active_ms: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"tool_calls": self.tool_calls, "replans": self.replans, "active_ms": self.active_ms}


@dataclass
class Budget:
    max_tool_calls: int
    max_replans: int
    max_active_seconds: int

    @classmethod
    def from_run(cls, run: dict[str, Any] | None, limits: Limits) -> "Budget":
        b = (run or {}).get("budget") or {}
        # The run budget is authoritative; worker limits can only tighten it.
        return cls(
            max_tool_calls=min(int(b.get("max_tool_calls", limits.max_tool_calls)), limits.max_tool_calls),
            max_replans=min(int(b.get("max_replans", limits.max_replans)), limits.max_replans),
            max_active_seconds=min(int(b.get("max_active_seconds", limits.max_active_seconds)), limits.max_active_seconds),
        )


@dataclass
class CheckpointState:
    """Agent-B-owned inner checkpoint shape (versioned). Required keys per PROTOCOL.md are always present."""

    stage: str = "investigate"
    usage: Usage = field(default_factory=Usage)
    completed_call_ids: list[str] = field(default_factory=list)
    pending_action_id: str | None = None
    emitted_steps: list[str] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    seen_evidence_ids: list[str] = field(default_factory=list)
    clarifications: int = 0
    resume_stage: str | None = None
    mode: str = "scripted"
    extra: dict[str, Any] = field(default_factory=dict)
    passthrough: dict[str, Any] = field(default_factory=dict)  # API-owned keys (action_id, reply_source_id, ...)

    @classmethod
    def load(cls, raw: dict[str, Any] | None) -> "CheckpointState":
        raw = raw or {}
        u = raw.get("usage") or {}
        return cls(
            stage=raw.get("stage") or "investigate",
            usage=Usage(int(u.get("tool_calls", 0)), int(u.get("replans", 0)), int(u.get("active_ms", 0))),
            completed_call_ids=list(raw.get("completed_call_ids") or []),
            pending_action_id=raw.get("pending_action_id"),
            emitted_steps=list(raw.get("emitted_steps") or []),
            proposal_ids=list(raw.get("proposal_ids") or []),
            action_ids=list(raw.get("action_ids") or []),
            evidence_ids=list(raw.get("evidence_ids") or []),
            seen_evidence_ids=list(raw.get("seen_evidence_ids") or []),
            clarifications=int(raw.get("clarifications") or 0),
            resume_stage=raw.get("resume_stage"),
            mode=raw.get("mode") or "scripted",
            extra=dict(raw.get("worker_extra") or {}),
            passthrough={k: v for k, v in raw.items() if k not in OWN_KEYS},
        )

    def dump(self, limit_bytes: int) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            return {
                **dict(list(self.passthrough.items())[:30]),
                "checkpoint_version": CHECKPOINT_VERSION,
                "mode": self.mode,
                "stage": self.stage,
                "usage": self.usage.as_dict(),
                "completed_call_ids": self.completed_call_ids,
                "pending_action_id": self.pending_action_id,
                "resume_stage": self.resume_stage,
                "emitted_steps": self.emitted_steps,
                "proposal_ids": self.proposal_ids,
                "action_ids": self.action_ids,
                "evidence_ids": self.evidence_ids[:100],
                "seen_evidence_ids": self.seen_evidence_ids,
                "clarifications": self.clarifications,
                "worker_extra": self.extra,
            }

        out = build()
        # Bound size: drop oldest bookkeeping first (never the required keys).
        while len(json.dumps(out, separators=(",", ":"))) > limit_bytes:
            if len(self.seen_evidence_ids) > 20:
                self.seen_evidence_ids = self.seen_evidence_ids[len(self.seen_evidence_ids) // 2 :]
            elif len(self.emitted_steps) > 20:
                self.emitted_steps = self.emitted_steps[len(self.emitted_steps) // 2 :]
            elif len(self.completed_call_ids) > 20:
                self.completed_call_ids = self.completed_call_ids[len(self.completed_call_ids) // 2 :]
            elif self.extra:
                self.extra = {}
            else:
                break
            out = build()
        return out


class JobSession:
    """Binds a lease to its run: emits ordered events through `checkpoint` and enforces budgets."""

    def __init__(
        self,
        api: ApiClient,
        keeper: LeaseKeeper,
        *,
        limits: Limits,
        run: dict[str, Any] | None,
        mode: str = "scripted",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.keeper = keeper
        self.ws = keeper.workspace_id
        self.limits = limits
        self.run = run
        self.state = CheckpointState.load(keeper.checkpoint)
        self.state.mode = mode
        if run and not (keeper.checkpoint or {}).get("usage"):
            u = run.get("usage") or {}
            self.state.usage = Usage(int(u.get("tool_calls", 0)), int(u.get("replans", 0)), int(u.get("active_ms", 0)))
        if keeper.checkpoint and keeper.checkpoint.get("resume_stage"):
            self.state.resume_stage = keeper.checkpoint["resume_stage"]
        self.budget = Budget.from_run(run, limits)
        self.event_seq: int = int((run or {}).get("event_seq", 0))
        self.events: list[dict[str, Any]] = []
        self._monotonic = monotonic
        self._active_started = monotonic()
        self._active_base = self.state.usage.active_ms
        self.cancel_requested = False
        self._sid = keeper.job_id if len(keeper.job_id) <= 90 else short_key(keeper.job_id, limit=90)
        self._call_index = 0
        self.job_tool_calls = 0  # budgets apply per job; usage stays cumulative per run

    # -- ids ---------------------------------------------------------------------------------------
    def step_id(self, name: str) -> str:
        if name.startswith(self._sid + ":"):
            return short_key(name)
        return short_key(self._sid, name)

    def next_call_id(self) -> str:
        self._call_index += 1
        return short_key(self._sid, f"c{self._call_index:02d}", limit=120)

    def is_completed(self, call_id: str) -> bool:
        return call_id in self.state.completed_call_ids

    # -- usage & budget ----------------------------------------------------------------------------
    def tick(self) -> None:
        self.state.usage.active_ms = self._active_base + self.job_active_ms()

    def job_active_ms(self) -> int:
        return int((self._monotonic() - self._active_started) * 1000)

    def check_budget(self, *, needs_tool_call: bool = True) -> None:
        """Per-job limits (spec §4: 12 calls / 90 s per job); replans are cumulative per run."""
        self.tick()
        u = self.state.usage
        if needs_tool_call and self.job_tool_calls >= self.budget.max_tool_calls:
            raise BudgetExhausted(f"tool call budget exhausted ({self.job_tool_calls}/{self.budget.max_tool_calls} calls in this job)")
        if self.job_active_ms() >= self.budget.max_active_seconds * 1000:
            raise BudgetExhausted(f"active time budget exhausted ({self.job_active_ms()} ms/{self.budget.max_active_seconds} s)")
        if u.replans > self.budget.max_replans:
            raise BudgetExhausted(f"replan budget exhausted ({u.replans}/{self.budget.max_replans})")

    def check_cancel(self, *, refresh: bool = False) -> None:
        if refresh and self.keeper.run_id:
            self.refresh_run()
        if self.cancel_requested:
            raise Cancelled("run cancellation requested")

    def refresh_run(self) -> dict[str, Any] | None:
        if not self.keeper.run_id:
            return None
        try:
            run, _ = self.api.call("GET", ApiClient.public(self.ws, f"/runs/{self.keeper.run_id}"))
        except (ApiError, ApiUnavailable, ApiTimeout):
            return self.run
        if isinstance(run, dict):
            self.run = run
            if run.get("status") == "cancelled":
                self.cancel_requested = True
        return self.run

    # -- events ------------------------------------------------------------------------------------
    def emit(self, step_name: str, event_type: str, payload: dict[str, Any], *, tool_name: str | None = None) -> dict[str, Any] | None:
        """Append one typed event with the current checkpoint. Stable step ids make re-sends idempotent."""
        if not self.keeper.run_id:
            return None
        step_id = self.step_id(step_name)
        if step_id in self.state.emitted_steps:
            return None
        self.keeper.ensure()
        self.tick()
        if step_id not in self.state.emitted_steps:
            self.state.emitted_steps.append(step_id)
        body = {
            **self.keeper.binding(),
            "step_id": step_id,
            "expected_event_seq": self.event_seq,
            "event": {"type": event_type, "tool_name": tool_name, "payload": payload},
            "checkpoint": self.state.dump(self.limits.max_checkpoint_bytes - 1024),
        }
        for attempt in range(3):
            body["expected_event_seq"] = self.event_seq
            key = short_key("ckpt", step_id, self.keeper.fencing_token, self.event_seq)
            try:
                data, _ = self.api.call(
                    "POST",
                    ApiClient.private(self.ws, f"/jobs/{self.keeper.job_id}/checkpoint"),
                    json_body=body,
                    idempotency_key=key,
                )
            except ApiError as err:
                if err.code == "LEASE_LOST":
                    self.keeper.mark_lost("checkpoint rejected: LEASE_LOST")
                    raise LeaseLost(err.message) from err
                if err.code in ("REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT", "INVALID_TRANSITION") and attempt < 2:
                    # sequence moved (API appended its own event) or step replay: resync and retry.
                    run = self.refresh_run()
                    if run is not None:
                        self.event_seq = int(run.get("event_seq", self.event_seq))
                    if err.code == "INVALID_TRANSITION" and self.cancel_requested:
                        raise Cancelled("run cancelled") from err
                    continue
                raise
            event = (data or {}).get("event") or {}
            nxt = (data or {}).get("next_event_seq")
            if event.get("step_id") == step_id and isinstance(event.get("seq"), int) and event["seq"] <= self.event_seq:
                # replayed append from a previous lease: resync from the run
                run = self.refresh_run()
                self.event_seq = int((run or {}).get("event_seq", nxt or self.event_seq))
            elif isinstance(nxt, int):
                self.event_seq = nxt
            elif isinstance(event.get("seq"), int):
                self.event_seq = event["seq"]
            self.events.append(event)
            return event
        return None

    # -- completion --------------------------------------------------------------------------------
    def complete(self, outcome: str, result: dict[str, Any]) -> dict[str, Any] | None:
        self.keeper.ensure()
        self.tick()
        completion_id = short_key(self._sid, "complete", f"a{self.keeper.attempt}")
        body = {**self.keeper.binding(), "completion_id": completion_id, "outcome": outcome, "result": dict(list(result.items())[:50])}
        try:
            data, _ = self.api.call(
                "POST",
                ApiClient.private(self.ws, f"/jobs/{self.keeper.job_id}/complete"),
                json_body=body,
                idempotency_key=short_key("complete", completion_id, self.keeper.fencing_token),
            )
        except ApiError as err:
            if err.code == "LEASE_LOST":
                self.keeper.mark_lost("complete rejected: LEASE_LOST")
                raise LeaseLost(err.message) from err
            raise
        return data
