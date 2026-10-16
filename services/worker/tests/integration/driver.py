"""Test-only driver: operator-side HTTP calls + an in-process worker against a mock/real API (PG_IT_BASE_URL)."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from typing import Any

import httpx

from promisegraph_worker.config import Settings
from promisegraph_worker.worker import Worker

BASE = os.environ.get("PG_IT_BASE_URL", "http://localhost:8101")
OP = os.environ.get("OPERATOR_TOKEN", "dev-operator-token")
WK = os.environ.get("WORKER_TOKEN", "dev-worker-token")
WS = "ws_demo"


class Operator:
    def __init__(self, ws: str = WS) -> None:
        self.ws = ws
        self.c = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {OP}"}, timeout=10)

    def p(self, path: str) -> str:
        return f"/api/v1/workspaces/{self.ws}{path}"

    def get(self, path: str, **params: Any) -> Any:
        r = self.c.get(self.p(path), params=params or None)
        r.raise_for_status()
        return r.json()["data"]

    def post(self, path: str, body: dict[str, Any], key: str | None = None) -> httpx.Response:
        return self.c.post(self.p(path), json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex})

    def create_request(self, customer_id: str, subject: str, body: str) -> dict[str, Any]:
        r = self.post("/requests", {"customer_id": customer_id, "subject": subject, "body": body, "channel": "manual", "capability_ids": [],
                                    "external_ref": None, "auto_start": True, "planner_mode": "scripted"})
        assert r.status_code == 201, r.text
        return r.json()["data"]

    def latest_proposal(self, run_id: str, request_id: str) -> dict[str, Any] | None:
        """Latest by the run's ordered events (list order can tie within one created_at second)."""
        ready = [e["payload"]["proposal_id"] for e in self.events(run_id) if e["type"] == "proposal_ready"]
        if ready:
            return self.get(f"/proposals/{ready[-1]}")
        props = self.get("/proposals", run_id=run_id, request_id=request_id)
        return props[0] if props else None

    def actions(self, request_id: str) -> list[dict[str, Any]]:
        return self.get("/actions", request_id=request_id)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return self.get(f"/runs/{run_id}/events", after_seq=0, limit=100)

    def commit(self, proposal: dict[str, Any], key: str | None = None) -> httpx.Response:
        return self.post(f"/proposals/{proposal['id']}/commit", {"expected_revision": proposal["based_on_revision"], "authorization_id": None}, key)

    def observe(self, request_id: str, action_id: str | None, kind: str, verification: str, body: str) -> httpx.Response:
        req = self.get(f"/requests/{request_id}")
        return self.post(f"/requests/{request_id}/observations", {"action_id": action_id, "kind": kind, "verification": verification, "body": body,
                                                                  "evidence_ids": [], "expected_request_version": req["version"]})

    def faults(self, **faults: Any) -> None:
        self.c.post("/__mock/faults", json=faults).raise_for_status()

    def reset(self) -> None:
        self.c.post("/__mock/reset", json={}).raise_for_status()

    def work_count(self) -> int:
        n, cursor = 0, None
        while True:
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            r = self.c.get(self.p("/work-items"), params=params)
            r.raise_for_status()
            body = r.json()
            n += len(body["data"])
            cursor = body["page"]["next_cursor"]
            if not cursor:
                return n


def worker(**overrides: Any) -> Worker:
    s = Settings(api_base_url=BASE, worker_token=WK, worker_id="worker_it", workspaces=(WS, "ws_other"), idle_poll_min_seconds=0.05, idle_poll_max_seconds=0.1)
    return Worker(replace(s, **overrides), heartbeat_interval=5)


def drain(w: Worker, max_jobs: int = 30) -> int:
    return w.run_forever(max_jobs=max_jobs, exit_when_idle=True)
