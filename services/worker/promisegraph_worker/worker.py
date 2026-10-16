"""Worker main loop: claim -> route by job kind -> complete; idle backoff; no inbound port."""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Callable

import httpx

from .config import Settings
from .connectors.linear import LinearConnector
from .connectors.local import LocalTaskConnector
from .http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, Backoff, LeaseLost
from .jobs.effects import EffectRunner
from .jobs.investigate import InvestigateRunner
from .jobs.lease import LeaseKeeper, claim
from .planner.base import Planner
from .planner.model import ModelPlanner
from .planner.scripted import ScriptedPlanner
from .planner.spend import Prices, SpendConfigError, SpendGuard

log = logging.getLogger("promisegraph.worker")
KINDS = ["reconcile", "dispatch", "investigate", "refresh"]


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        linear_transport: httpx.BaseTransport | None = None,
        model_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        heartbeat_interval: float | None = None,
    ) -> None:
        self.settings = settings
        self.sleep = sleep
        self.api = ApiClient(settings.api_base_url, settings.worker_token, timeout=settings.http_timeout_seconds, transport=transport, backoff=Backoff(sleep=sleep))
        planners: dict[str, Planner] = {"scripted": ScriptedPlanner()}
        if settings.ai_enabled:
            errors = settings.model_config_errors()
            if errors:  # refuse to start in model mode (Rev 6): never run without the hard spend guard
                raise SpendConfigError("model mode refused: " + "; ".join(errors))
            guard = SpendGuard(
                budget_usd=settings.ai_budget_usd,
                prices=Prices(float(settings.ai_price_input_per_mtok), float(settings.ai_price_output_per_mtok)),
                max_output_tokens=settings.ai_max_output_tokens_per_call,
                ledger_path=settings.ledger_path,
            )
            planners["model"] = ModelPlanner(
                enabled=True, provider=settings.ai_provider, api_key=settings.ai_api_key, model=settings.ai_model, guard=guard,
                max_calls_per_run=settings.ai_max_calls_per_run, max_output_tokens=settings.ai_max_output_tokens_per_call,
                transport=model_transport, backoff=Backoff(base=1.0, cap=20.0, sleep=sleep),
            )
        # AI disabled: model runs fail with CAPABILITY_DISABLED; never silently scripted, no provider client exists.
        self.linear = LinearConnector(
            enabled=settings.linear_enabled, api_key=settings.linear_api_key, team_id=settings.linear_team_id,
            url=settings.linear_api_url, transport=linear_transport, backoff=Backoff(base=1.0, cap=30.0, sleep=sleep),
        )
        self.investigate = InvestigateRunner(self.api, settings.limits, planners)
        self.effects = EffectRunner(self.api, settings.limits, LocalTaskConnector(self.api), self.linear)
        self.heartbeat_interval = heartbeat_interval
        self._stop = threading.Event()
        self._info: dict[str, dict[str, Any]] = {}

    def stop(self) -> None:
        self._stop.set()

    def info(self, ws: str) -> dict[str, Any]:
        try:
            data, _ = self.api.call("GET", ApiClient.public(ws, "/info"))
            self._info[ws] = data or {}
        except (ApiError, ApiUnavailable, ApiTimeout):
            pass
        return self._info.get(ws, {"auto_commit_kinds": ["answer", "clarify"]})

    def process(self, ws: str, lease: dict[str, Any]) -> dict[str, Any] | None:
        keeper = LeaseKeeper(self.api, ws, lease, lease_seconds=self.settings.lease_seconds, interval=self.heartbeat_interval)
        log.info("claimed %s job %s (attempt %s, fencing %s)", lease["kind"], lease["job_id"], lease.get("attempt"), lease["fencing_token"])
        with keeper:
            try:
                if lease["kind"] == "investigate":
                    return self.investigate.run(keeper, self.info(ws))
                if lease["kind"] == "dispatch":
                    return self.effects.dispatch(keeper)
                if lease["kind"] == "reconcile":
                    return self.effects.reconcile(keeper)
                if lease["kind"] == "refresh":
                    return self.effects.refresh(keeper)
                log.warning("unknown job kind %s", lease["kind"])
            except LeaseLost as exc:
                # Stop mutating; the server's recovery claim continues from the last checkpoint.
                log.warning("lease lost on job %s: %s", lease["job_id"], exc)
                return {"lease_lost": True, "reason": str(exc)}
            except (ApiUnavailable, ApiTimeout) as exc:
                log.warning("API unavailable during job %s: %s (lease will expire; job is retried)", lease["job_id"], exc)
                return {"unavailable": True, "reason": str(exc)}
        return None

    def run_once(self) -> bool:
        """Claim and process at most one job across workspaces. Returns True if a job was processed."""
        for ws in self.settings.workspaces:
            try:
                lease = claim(self.api, ws, self.settings.worker_id, KINDS, self.settings.lease_seconds)
            except ApiError as err:
                log.warning("claim failed in %s: %s %s", ws, err.code, err.message)
                continue
            except (ApiUnavailable, ApiTimeout) as exc:
                log.warning("claim unavailable in %s: %s", ws, exc)
                continue
            if lease:
                self.process(ws, lease)
                return True
        return False

    def run_forever(self, *, max_jobs: int | None = None, exit_when_idle: bool = False) -> int:
        done, idle = 0, 0
        delay = self.settings.idle_poll_min_seconds
        while not self._stop.is_set():
            if self.run_once():
                done += 1
                idle = 0
                delay = self.settings.idle_poll_min_seconds
                if max_jobs is not None and done >= max_jobs:
                    break
                continue
            idle += 1
            if exit_when_idle and idle >= 2:
                break
            self._stop.wait(delay) if self.sleep is time.sleep else self.sleep(delay)
            delay = min(self.settings.idle_poll_max_seconds, delay * 2)
        return done


def install_signal_handlers(worker: Worker) -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.info("signal %s: stopping after the current job", signum)
        worker.stop()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
