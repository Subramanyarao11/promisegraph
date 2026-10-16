"""Job lease: atomic claim, background heartbeat renewal, fencing and lease-loss detection."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, LeaseHeaders, LeaseLost


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeaseKeeper:
    """Holds the current fenced lease for one job and renews it in a background thread.

    Every worker mutation calls `ensure()` first. A `409 LEASE_LOST` from any endpoint, a heartbeat
    rejection, or local expiry without a successful renewal marks the lease lost; the job must stop
    mutating and let the server's recovery claim proceed.
    """

    def __init__(
        self,
        api: ApiClient,
        workspace_id: str,
        lease: dict[str, Any],
        *,
        lease_seconds: int = 60,
        interval: float | None = None,
        clock: Callable[[], datetime] = now_utc,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self.api = api
        self.workspace_id = workspace_id
        self.job_id: str = lease["job_id"]
        self.kind: str = lease["kind"]
        self.run_id: str | None = lease.get("run_id")
        self.action_id: str | None = lease.get("action_id")
        self.attempt: int = lease.get("attempt", 1)
        self.checkpoint: dict[str, Any] | None = lease.get("checkpoint")
        self._lease_token: str = lease["lease_token"]
        self._fencing: int = int(lease["fencing_token"])
        self._expires_at = parse_ts(lease.get("expires_at"))
        self._lock = threading.Lock()
        self._lost = threading.Event()
        self._stop = threading.Event()
        self.lost_reason: str | None = None
        self.interval = interval if interval is not None else max(1.0, lease_seconds / 3)
        self.clock = clock
        self.on_tick = on_tick
        self.heartbeats = 0
        self._thread: threading.Thread | None = None

    # -- binding ---------------------------------------------------------------------------------
    @property
    def lease_token(self) -> str:
        with self._lock:
            return self._lease_token

    @property
    def fencing_token(self) -> int:
        with self._lock:
            return self._fencing

    def binding(self) -> dict[str, Any]:
        with self._lock:
            return {"lease_token": self._lease_token, "fencing_token": self._fencing}

    def headers(self) -> LeaseHeaders:
        with self._lock:
            return LeaseHeaders(self.job_id, self._lease_token, self._fencing)

    @property
    def lost(self) -> bool:
        if not self._lost.is_set() and self._expires_at is not None and self.clock() >= self._expires_at:
            self.mark_lost("lease expired locally before renewal")
        return self._lost.is_set()

    def mark_lost(self, reason: str) -> None:
        if not self._lost.is_set():
            self.lost_reason = reason
            self._lost.set()

    def ensure(self) -> None:
        if self.lost:
            raise LeaseLost(self.lost_reason or "lease lost")

    # -- heartbeat -------------------------------------------------------------------------------
    def heartbeat(self) -> bool:
        if self._lost.is_set():
            return False
        try:
            data, _ = self.api.call(
                "POST",
                ApiClient.private(self.workspace_id, f"/jobs/{self.job_id}/heartbeat"),
                json_body=self.binding(),
                retry=False,
            )
        except ApiError as err:
            if err.status == 409 or err.code in ("LEASE_LOST", "INVALID_TRANSITION"):
                self.mark_lost(f"heartbeat rejected: {err.code}")
            return False
        except (ApiUnavailable, ApiTimeout):
            return False  # transient; local expiry check still protects mutations
        if isinstance(data, dict):
            with self._lock:
                self._lease_token = data.get("lease_token", self._lease_token)
                self._fencing = int(data.get("fencing_token", self._fencing))
                self._expires_at = parse_ts(data.get("expires_at")) or self._expires_at
        self.heartbeats += 1
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self._lost.is_set():
                return
            self.heartbeat()
            if self.on_tick:
                try:
                    self.on_tick()
                except Exception:  # noqa: BLE001 - tick hooks must never kill renewal
                    pass

    def start(self) -> "LeaseKeeper":
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{self.job_id}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def __enter__(self) -> "LeaseKeeper":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def claim(api: ApiClient, workspace_id: str, worker_id: str, kinds: list[str], lease_seconds: int = 60) -> dict[str, Any] | None:
    data, _ = api.call(
        "POST",
        ApiClient.private(workspace_id, "/jobs/claim"),
        json_body={"worker_id": worker_id, "kinds": kinds, "lease_seconds": lease_seconds},
    )
    return data or None


def sleep_until(deadline: float) -> None:  # pragma: no cover - trivial
    delay = deadline - time.monotonic()
    if delay > 0:
        time.sleep(delay)
