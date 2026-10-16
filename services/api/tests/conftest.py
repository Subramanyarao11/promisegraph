"""Test harness: real FalkorDB, throwaway graph prefix per test session, every response contract-validated."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from contracts.tools.validate import errors as contract_errors  # noqa: E402

from promisegraph_api.app import create_app  # noqa: E402
from promisegraph_api.config import Settings  # noqa: E402

OP = "test-operator-token"
WK = "test-worker-token"


def make_settings(prefix: str, **over: Any) -> Settings:
    s = Settings.from_env({"DATABASE_GRAPH_PREFIX": prefix, "OPERATOR_TOKEN": OP, "WORKER_TOKEN": WK,
                           "WORKSPACE_ACL": "operator:ws_demo,ws_other;worker:ws_demo,ws_other",
                           "CLOCK_MODE": "scenario", "AI_ENABLED": "false", "LINEAR_ENABLED": "false", "FAULTS": "",
                           "DATABASE_HOST": "localhost", "DATABASE_PORT": "6380"})
    for k, v in over.items():
        setattr(s, k, v)
    return s


def check(name: str, value: Any) -> None:
    errs = contract_errors(name, value)
    assert not errs, f"{name} contract violation: {errs[:5]}"


class Api:
    """Thin client that validates every envelope against the frozen contract."""

    def __init__(self, client: TestClient, ws: str = "ws_demo", token: str = OP):
        self.c = client
        self.ws = ws
        self.token = token
        self.lease: dict[str, Any] | None = None

    def as_worker(self) -> "Api":
        return Api(self.c, self.ws, WK)

    def in_ws(self, ws: str) -> "Api":
        return Api(self.c, ws, self.token)

    def _headers(self, key: str | None, extra: dict | None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if key:
            h["Idempotency-Key"] = key
        if self.lease:
            h.update({"X-Job-Id": self.lease["job_id"], "X-Lease-Token": self.lease["lease_token"],
                      "X-Fencing-Token": str(self.lease["fencing_token"])})
        h.update(extra or {})
        return h

    def url(self, path: str, private: bool = False) -> str:
        base = "/internal/v1" if private else "/api/v1"
        return f"{base}/workspaces/{self.ws}{path}"

    def _validate(self, r, schema: str | None, list_of: str | None, expect: int | None) -> Any:
        if expect is None:
            assert r.status_code in (200, 204), r.text[:800]
            expect = r.status_code
        assert r.status_code == expect, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:800]}"
        if r.status_code == 204:
            return None
        body = r.json()
        if r.status_code >= 400:
            check("ErrorEnvelope", body)
            return body
        if schema == "EventsPage":
            check("EventsPage", body)
            return body
        if "meta" in body:
            check("Meta", body["meta"])
        if list_of:
            assert set(body) == {"data", "page", "meta"}, body.keys()
            for item in body["data"]:
                check(list_of, item)
            return body
        if schema:
            assert set(body) == {"data", "meta"}, body.keys()
            check(schema, body["data"])
        return body

    def get(self, path: str, schema: str | None = None, list_of: str | None = None, expect: int = 200,
            params: dict | None = None, private: bool = False) -> Any:
        r = self.c.get(self.url(path, private), headers=self._headers(None, None), params=params)
        return self._validate(r, schema, list_of, expect)

    def post(self, path: str, body: Any, schema: str | None = None, expect: int = 200, key: str | None = "auto",
             private: bool = False, headers: dict | None = None, method: str = "POST") -> Any:
        if key == "auto":
            key = "k_" + uuid.uuid4().hex
        r = self.c.request(method, self.url(path, private), headers=self._headers(key, headers), json=body)
        return self._validate(r, schema, None, expect)

    def patch(self, path: str, body: Any, schema: str | None = None, expect: int = 200, key: str | None = "auto") -> Any:
        return self.post(path, body, schema, expect, key, method="PATCH")

    def revision(self) -> int:
        return self.get("/info", "WorkspaceInfo")["meta"]["knowledge_revision"]

    # -- domain helpers --
    def import_doc(self, fmt: str, content: str, source_key: str, origin: str = "synthetic") -> dict:
        pv = self.post("/imports/preview", {"format": fmt, "origin": origin, "content": content,
                                            "source_key": source_key}, "ImportPreviewResult", 201)["data"]
        res = self.post(f"/imports/{pv['preview_id']}/commit", {"expected_revision": pv["based_on_revision"]},
                        "ImportCommitResult", 200)["data"]
        return {"preview": pv, "commit": res}

    def claim(self, kinds: list[str] | None = None, worker_id: str = "worker_test", lease_seconds: int = 60):
        w = self.as_worker()
        body = w.post("/jobs/claim", {"worker_id": worker_id, "kinds": kinds or ["investigate", "dispatch", "reconcile",
                                                                                   "refresh"],
                                      "lease_seconds": lease_seconds}, "JobLease", private=True, key=None,
                      expect=None)
        return body["data"] if body else None

    def worker_with(self, lease: dict) -> "Api":
        w = self.as_worker()
        w.lease = lease
        return w


def lease_body(lease: dict, **extra: Any) -> dict:
    return {"lease_token": lease["lease_token"], "fencing_token": lease["fencing_token"], **extra}


def cp(stage: str = "investigate", calls: list[str] | None = None, pending: str | None = None) -> dict:
    return {"completed_call_ids": calls or [], "pending_action_id": pending, "stage": stage,
            "usage": {"tool_calls": len(calls or []), "replans": 0, "active_ms": 10}}


SEED = {
    "workspace_id": "ws_demo",
    "observed_at": "2026-10-15T09:00:00Z",
    "customers": [
        {"id": "cust_acme", "name": "Acme (synthetic)", "plan": "enterprise", "constraints": []},
        {"id": "cust_globex", "name": "Globex (synthetic)", "plan": None, "constraints": []},
    ],
    "teams": [{"id": "team_data", "name": "Data platform"}],
    "people": [
        {"id": "person_maya", "name": "Maya", "role": "Engineer", "team_id": "team_data", "active": True},
        {"id": "person_li", "name": "Li", "role": "Engineer", "team_id": "team_data", "active": True},
        {"id": "person_gone", "name": "Former", "role": "Engineer", "team_id": None, "active": False},
    ],
    "capabilities": [
        {"id": "cap_exports", "name": "Scheduled exports", "aliases": ["csv export", "export schedule"],
         "status": "active", "product_version": "4.2", "owner_id": "team_data", "customer_ids": []},
        {"id": "cap_sso", "name": "Single sign-on", "aliases": ["saml"], "status": "active",
         "product_version": None, "owner_id": None, "customer_ids": []},
    ],
    "work_items": [
        {"id": "work_export_issue", "title": "Restore scheduled exports", "description": "Exports blocked by worker migration",
         "kind": "ticket", "status": "blocked", "owner_id": "person_maya", "connector": "local", "external_id": None,
         "external_url": None, "forecast_at": None, "observed_at": "2026-10-15T09:00:00Z",
         "capability_ids": ["cap_exports"], "depends_on": ["work_migration"], "acceptance_criteria": []},
        {"id": "work_migration", "title": "Export worker migration", "description": "Move export worker to v2",
         "kind": "milestone", "status": "in_progress", "owner_id": "person_li", "connector": "local",
         "external_id": None, "external_url": None, "forecast_at": "2026-10-20T00:00:00Z",
         "observed_at": "2026-10-15T09:00:00Z", "capability_ids": [], "depends_on": [], "acceptance_criteria": []},
        {"id": "work_sso_done", "title": "SAML metadata refresh", "description": "Shipped",
         "kind": "ticket", "status": "done", "owner_id": "person_maya", "connector": "local", "external_id": None,
         "external_url": None, "forecast_at": None, "observed_at": "2026-10-15T09:00:00Z",
         "capability_ids": ["cap_sso"], "depends_on": [], "acceptance_criteria": []},
    ],
    "requests": [
        {"id": "req_prior_exports", "customer_id": "cust_globex", "subject": "Exports failing nightly",
         "body": "Nightly CSV export schedule fails.", "status": "awaiting_work", "owner_id": None,
         "capability_ids": ["cap_exports"], "linked_work_ids": ["work_export_issue"], "related_request_ids": [],
         "created_at": "2026-10-10T08:00:00Z"},
        {"id": "req_similar", "customer_id": "cust_acme", "subject": "Export schedule looks wrong",
         "body": "The export schedule shows the wrong timezone.", "status": "open", "owner_id": None,
         "capability_ids": ["cap_exports"], "linked_work_ids": [], "related_request_ids": ["req_prior_exports"],
         "created_at": "2026-10-11T08:00:00Z"},
    ],
    "decisions": [
        {"id": "dec_no_public_links", "rule_type": "forbidden_workaround", "value": "manual_public_export_link",
         "scope": {"entity_type": "customer", "entity_id": "cust_acme"}, "status": "active",
         "rationale": "Export links must remain authenticated.", "author_id": "person_maya",
         "effective_at": "2026-10-01T00:00:00Z", "expires_at": None, "supersedes_id": None},
    ],
    "attempts": [
        {"id": "att_retry_globex", "request_id": "req_prior_exports", "work_item_id": "work_export_issue",
         "procedure_key": "retry_schedule", "context_signature": "cap_exports:worker-v1",
         "scope": {"entity_type": "customer", "entity_id": "cust_globex"}, "outcome": "failed",
         "verification": "failed", "summary": "Retrying did not restore exports.",
         "observed_at": "2026-10-12T08:00:00Z"},
    ],
}


@pytest.fixture(scope="session")
def prefix() -> str:
    return f"pgtest_{uuid.uuid4().hex[:8]}_"


@pytest.fixture()
def app_factory(prefix: str):
    created = []

    def make(**over: Any):
        p = over.pop("prefix", None) or f"{prefix}{uuid.uuid4().hex[:6]}_"
        app = create_app(make_settings(p, **over))
        created.append(app)
        return app
    yield make
    for app in created:
        st = app.state.store
        if st.settings.db_port != 6380:
            continue
        for ws in st.settings.workspaces:
            try:
                st.drop_graph(ws)
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture()
def api(app_factory) -> Api:
    app = app_factory()
    return Api(TestClient(app))


@pytest.fixture()
def seeded(api: Api) -> Api:
    out = api.import_doc("json", json.dumps(SEED), "seed_core")
    assert out["commit"]["imported_count"] > 0
    return api
