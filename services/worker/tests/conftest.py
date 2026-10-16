from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))  # contracts/tools/validate.py (coordinator-owned validator, read-only use)


def meta(rev: int = 7) -> dict[str, Any]:
    return {"contract_version": "1.0.0", "knowledge_revision": rev, "correlation_id": "c-test"}


def ok(data: Any, status: int = 200, rev: int = 7) -> httpx.Response:
    return httpx.Response(status, json={"data": data, "meta": meta(rev)})


def err(status: int, code: str, message: str = "x", retryable: bool = False, details: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message, "retryable": retryable, "details": details or {}}, "meta": meta()}, headers=headers)


class Router:
    """Tiny programmable fake API: register (method, path-suffix) -> handler; records every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.calls: list[httpx.Request] = []

    def on(self, method: str, suffix: str, handler: Callable[[httpx.Request], httpx.Response] | httpx.Response) -> "Router":
        h = handler if callable(handler) else (lambda _r, resp=handler: resp)
        self.routes.insert(0, (method, suffix, h))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for method, suffix, h in self.routes:
            if request.method == method and request.url.path.endswith(suffix):
                return h(request)
        return err(404, "NOT_FOUND", f"no route {request.method} {request.url.path}")

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [json.loads(c.content or b"{}") for c in self.calls if c.url.path.endswith(suffix)]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture()
def router() -> Router:
    return Router()


def no_sleep(_s: float) -> None:
    return None
