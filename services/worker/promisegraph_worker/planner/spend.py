"""Hard AI spend guard (PROTOCOL.md Rev 6 #39).

Before every model call the worker reserves the worst case
`input_tokens_estimate × in_price + max_output_tokens × out_price`. If `spent + reserved + reservation > budget`
no call is made. Actual usage from the provider response replaces the reservation. The ledger is a JSON file
written atomically (temp file + fsync + rename) under an exclusive lock, so it survives restarts and concurrent
workers on one host. Reservations left open by a crash count as spent (conservative).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

MAX_BUDGET_USD = 10.0


class SpendConfigError(Exception):
    """Model mode must not start: missing prices, budget above the operator ceiling, or no model."""


class BudgetExhausted(Exception):
    def __init__(self, spent: float, reservation: float, budget: float) -> None:
        super().__init__(f"AI budget exhausted: spent ${spent:.4f} + reservation ${reservation:.4f} > budget ${budget:.2f}")
        self.spent, self.reservation, self.budget = spent, reservation, budget


@dataclass(frozen=True)
class Prices:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens * self.input_per_mtok / 1e6 + output_tokens * self.output_per_mtok / 1e6


def estimate_tokens(payload: Any) -> int:
    """Conservative input estimate: ~3 characters per token over the serialized request (over-reserves on purpose)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return len(text) // 3 + 16


class SpendGuard:
    def __init__(self, *, budget_usd: float, prices: Prices, max_output_tokens: int, ledger_path: Path) -> None:
        if budget_usd <= 0 or budget_usd > MAX_BUDGET_USD:
            raise SpendConfigError(f"AI_BUDGET_USD must be in (0, {MAX_BUDGET_USD:.0f}]; got {budget_usd}")
        if prices.input_per_mtok <= 0 or prices.output_per_mtok <= 0:
            raise SpendConfigError("AI_PRICE_INPUT_PER_MTOK and AI_PRICE_OUTPUT_PER_MTOK must be positive")
        if max_output_tokens <= 0:
            raise SpendConfigError("AI_MAX_OUTPUT_TOKENS_PER_CALL must be positive")
        self.budget = float(budget_usd)
        self.prices = prices
        self.max_output_tokens = int(max_output_tokens)
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- ledger I/O ---------------------------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self._read()
                yield state
                self._write(state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "spent_usd": 0.0, "open_reservations": {}, "calls": 0, "runs": {}}
        state = json.loads(self.path.read_text() or "{}")
        state.setdefault("spent_usd", 0.0)
        state.setdefault("open_reservations", {})
        state.setdefault("calls", 0)
        state.setdefault("runs", {})
        return state

    def _write(self, state: dict[str, Any]) -> None:
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # -- accounting ---------------------------------------------------------------------------------------
    @staticmethod
    def committed(state: dict[str, Any]) -> float:
        """Spent plus every open reservation (crash-orphaned reservations stay counted)."""
        return float(state["spent_usd"]) + sum(float(r["usd"]) for r in state["open_reservations"].values())

    def remaining(self) -> float:
        with self._locked() as state:
            return max(0.0, self.budget - self.committed(state))

    def run_calls(self, run_id: str) -> int:
        with self._locked() as state:
            return int(state["runs"].get(run_id, 0))

    def reserve(self, input_tokens_estimate: int, *, run_id: str) -> tuple[str, float]:
        reservation = self.prices.cost(input_tokens_estimate, self.max_output_tokens)
        with self._locked() as state:
            committed = self.committed(state)
            if committed + reservation > self.budget:
                raise BudgetExhausted(committed, reservation, self.budget)
            rid = uuid.uuid4().hex
            state["open_reservations"][rid] = {"usd": round(reservation, 8), "run_id": run_id, "at": time.time()}
            state["runs"][run_id] = int(state["runs"].get(run_id, 0)) + 1
            state["calls"] = int(state["calls"]) + 1
        return rid, reservation

    def settle(self, reservation_id: str, input_tokens: int, output_tokens: int) -> tuple[float, float]:
        """Replace the reservation with actual cost. Returns (cost_usd, budget_remaining_usd)."""
        cost = self.prices.cost(input_tokens, output_tokens)
        with self._locked() as state:
            state["open_reservations"].pop(reservation_id, None)
            state["spent_usd"] = round(float(state["spent_usd"]) + cost, 8)
            remaining = max(0.0, self.budget - self.committed(state))
        return cost, remaining

    def release(self, reservation_id: str, *, billed_unknown: bool) -> None:
        """Provider refused before work (4xx): release. Outcome unknown (timeout/5xx): keep it as spent."""
        with self._locked() as state:
            r = state["open_reservations"].pop(reservation_id, None)
            if r and billed_unknown:
                state["spent_usd"] = round(float(state["spent_usd"]) + float(r["usd"]), 8)
