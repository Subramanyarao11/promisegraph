"""Typed HTTP access to the PromiseGraph API (public `/api/v1` and private `/internal/v1`).

The worker never talks to the graph. Every call carries the worker service token. Errors are surfaced as
`ApiError` with the contract error code; transport problems are split into `ApiUnavailable` (request never
reached the server -> safe to retry) and `ApiTimeout` (the server may have applied the mutation -> ambiguous).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

CONTRACT_VERSION = "1.0.0"


class WorkerError(Exception):
    pass


@dataclass
class ApiError(WorkerError):
    status: int
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] | None = None

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.status} {self.code}: {self.message}"


class ApiUnavailable(WorkerError):
    """Connection could not be established / transport failed before the request was delivered."""


class ApiTimeout(WorkerError):
    """Request was sent but no response arrived; a mutation may or may not have been applied."""


class LeaseLost(WorkerError):
    """The job lease is lost/expired; stop all mutations for this job."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(obj: Any) -> str:
    data = obj if isinstance(obj, (bytes, str)) else canonical_json(obj)
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def short_key(*parts: Any, limit: int = 128) -> str:
    """Deterministic idempotency key / step id; hashed when longer than the contract bound."""
    key = ":".join(str(p) for p in parts)
    if len(key) <= limit:
        return key
    return key[: limit - 41] + "~" + hashlib.sha1(key.encode()).hexdigest()


@dataclass
class Backoff:
    base: float = 0.25
    factor: float = 2.0
    cap: float = 8.0
    attempts: int = 4
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = random.random

    def delay(self, attempt: int, hint: float | None = None) -> float:
        if hint is not None and hint >= 0:
            return min(hint, 60.0)
        raw = min(self.cap, self.base * (self.factor ** attempt))
        return raw * (0.5 + 0.5 * self.jitter())


def retry_after_hint(resp: httpx.Response | None, details: dict[str, Any] | None) -> float | None:
    if details:
        for key in ("retry_after_seconds", "retry_after"):
            if isinstance(details.get(key), (int, float)):
                return float(details[key])
    if resp is not None:
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                return None
    return None


@dataclass
class LeaseHeaders:
    job_id: str
    lease_token: str
    fencing_token: int

    def headers(self) -> dict[str, str]:
        return {"X-Job-Id": self.job_id, "X-Lease-Token": self.lease_token, "X-Fencing-Token": str(self.fencing_token)}


class ApiClient:
    """Thin JSON client. One instance per worker; workspace is passed per call (bound by the server ACL)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.backoff = backoff or Backoff()
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    # -- paths -----------------------------------------------------------------------------------
    @staticmethod
    def public(workspace_id: str, path: str) -> str:
        return f"/api/v1/workspaces/{workspace_id}{path}"

    @staticmethod
    def private(workspace_id: str, path: str) -> str:
        return f"/internal/v1/workspaces/{workspace_id}{path}"

    # -- core ------------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        lease: LeaseHeaders | None = None,
        retry: bool = True,
        retry_on_timeout: bool = True,
        correlation_id: str | None = None,
    ) -> httpx.Response | None:
        """Send a request with bounded backoff. Returns the response (None for 204).

        Retries: connection failures, 429, retryable 5xx/503 envelopes. Read timeouts are retried only when
        `retry_on_timeout` (safe for GETs and idempotency-keyed commands whose replay returns the original).
        """
        headers = {"X-Correlation-Id": correlation_id or f"wk-{uuid.uuid4().hex[:16]}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if lease:
            headers.update(lease.headers())
        attempts = self.backoff.attempts if retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, json=json_body, params=params, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = ApiUnavailable(f"{method} {path}: {exc.__class__.__name__}")
                if attempt + 1 < attempts:
                    self.backoff.sleep(self.backoff.delay(attempt))
                    continue
                raise last_exc from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                last_exc = ApiTimeout(f"{method} {path}: {exc.__class__.__name__}")
                if retry_on_timeout and attempt + 1 < attempts:
                    self.backoff.sleep(self.backoff.delay(attempt))
                    continue
                raise last_exc from exc
            if resp.status_code == 204:
                return None
            if resp.status_code < 400:
                return resp
            err = self._error(resp)
            transient = resp.status_code == 429 or (resp.status_code >= 500 and (err.retryable or resp.status_code in (502, 503, 504)))
            if transient and attempt + 1 < attempts and (retry_on_timeout or resp.status_code in (429, 503)):
                self.backoff.sleep(self.backoff.delay(attempt, retry_after_hint(resp, err.details)))
                continue
            raise err
        raise last_exc or WorkerError("unreachable")  # pragma: no cover

    @staticmethod
    def _error(resp: httpx.Response) -> ApiError:
        try:
            body = resp.json()
            e = body.get("error") or {}
            return ApiError(
                status=resp.status_code,
                code=str(e.get("code") or f"HTTP_{resp.status_code}"),
                message=str(e.get("message") or resp.reason_phrase),
                retryable=bool(e.get("retryable", False)),
                details=e.get("details") or {},
                meta=body.get("meta"),
            )
        except (ValueError, AttributeError):
            return ApiError(status=resp.status_code, code=f"HTTP_{resp.status_code}", message=resp.text[:200] or resp.reason_phrase)

    def call(self, method: str, path: str, **kw: Any) -> tuple[Any, dict[str, Any]]:
        """Returns `(data, envelope)` for a `{data, meta}` envelope, `(None, {})` for 204."""
        resp = self.request(method, path, **kw)
        if resp is None:
            return None, {}
        body = resp.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"], body
        return body, {"data": body}
