"""Test harness: every HTTP response is validated against the contract schemas (contracts/tools/validate.py)."""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from contracts.tools.validate import errors  # noqa: E402
from mock_api.app import app, store  # noqa: E402

OP = "dev-operator-token"
WK = "dev-worker-token"
ID = r"[^/]+"

# (method, path regex relative to workspace base, schema, envelope kind)
ROUTES = [
    ("GET", r"/info", "WorkspaceInfo", "single"),
    ("GET", r"/customers", "Customer", "list"),
    ("GET", r"/capabilities", "CapabilitySummary", "list"),
    ("POST", r"/requests", "RequestCreateResult", "single"),
    ("GET", r"/requests", "Request", "list"),
    ("GET", rf"/requests/{ID}", "Request", "single"),
    ("POST", rf"/requests/{ID}/runs", "Run", "single"),
    ("GET", rf"/requests/{ID}/runs", "Run", "list"),
    ("GET", rf"/runs/{ID}", "Run", "single"),
    ("GET", rf"/runs/{ID}/events", "EventsPage", "events"),
    ("POST", rf"/runs/{ID}/cancel", "Run", "single"),
    ("POST", rf"/runs/{ID}/resume", "Run", "single"),
    ("GET", rf"/requests/{ID}/context", "ContextBundle", "single"),
    ("POST", r"/context/search", "ContextSearchResult", "single"),
    ("POST", r"/context/expand", "ContextExpandResult", "single"),
    ("GET", rf"/sources/{ID}", "SourceRecord", "single"),
    ("GET", r"/sources", "SourceSummary", "list"),
    ("POST", rf"/requests/{ID}/replies", "ReplyResult", "single"),
    ("POST", r"/proposals", "Proposal", "single"),
    ("GET", r"/proposals", "Proposal", "list"),
    ("GET", rf"/proposals/{ID}", "Proposal", "single"),
    ("POST", rf"/proposals/{ID}/commit", "ProposalCommitResult", "single"),
    ("GET", r"/actions", "Action", "list"),
    ("GET", rf"/actions/{ID}", "Action", "single"),
    ("POST", rf"/requests/{ID}/observations", "ObservationResult", "single"),
    ("POST", rf"/requests/{ID}/refresh", "RefreshResult", "single"),
    ("GET", r"/work-items", "WorkItem", "list"),
    ("PATCH", rf"/work-items/{ID}", "WorkItemUpdateResult", "single"),
    ("GET", r"/decisions", "Decision", "list"),
    ("POST", r"/decisions", "Decision", "single"),
    ("POST", r"/imports/preview", "ImportPreviewResult", "single"),
    ("POST", rf"/imports/{ID}/commit", "ImportCommitResult", "single"),
    # rev 3
    ("GET", rf"/evidence/{ID}", "EvidenceRef", "single"),
    ("GET", rf"/work-items/{ID}", "WorkItem", "single"),
    ("GET", r"/observations", "Observation", "list"),
    ("GET", r"/attempts", "Attempt", "list"),
    ("POST", rf"/actions/{ID}/reconcile", "Action", "single"),
    ("POST", rf"/requests/{ID}/close", "Request", "single"),
    # private
    ("POST", r"/jobs/claim", "JobLease", "single"),
    ("POST", rf"/jobs/{ID}/heartbeat", "JobLease", "single"),
    ("POST", rf"/jobs/{ID}/checkpoint", "CheckpointResult", "single"),
    ("POST", rf"/jobs/{ID}/complete", "CompletionReceipt", "single"),
    ("POST", rf"/actions/{ID}/begin-dispatch", "DispatchPermit", "single"),
    ("POST", rf"/actions/{ID}/execute-local", "Action", "single"),
]
BASE = re.compile(r"^/(api|internal)/v1/workspaces/[^/]+(/.*)$")
SEEN: set[tuple[str, str]] = set()
VALIDATED = {"count": 0}


def contract_check(method: str, path: str, resp) -> None:
    assert resp.headers.get("X-PromiseGraph-Mock") == "true", f"missing mock header on {method} {path}"
    m = BASE.match(path.split("?")[0])
    if resp.status_code == 204:
        assert resp.content == b""
        return
    body = resp.json()
    if resp.status_code >= 400:
        errs = errors("ErrorEnvelope", body)
        assert not errs, f"{method} {path} -> {resp.status_code} invalid ErrorEnvelope: {errs}"
        VALIDATED["count"] += 1
        return
    if not m:
        return  # /health and /__mock are not contract routes
    rel = m.group(2)
    for meth, pat, schema, kind in ROUTES:
        if meth == method and re.fullmatch(pat, rel):
            SEEN.add((meth, pat))
            if kind == "events":
                errs = errors("EventsPage", body)
            else:
                expect = {"data", "meta"} | ({"page"} if kind == "list" else set())
                assert set(body) == expect, f"{method} {path}: envelope keys {set(body)} != {expect}"
                errs = errors("Meta", body["meta"])
                if kind == "list":
                    assert set(body["page"]) == {"next_cursor"}
                    for i, item in enumerate(body["data"]):
                        errs += [f"[{i}] {e}" for e in errors(schema, item)]
                else:
                    errs += errors(schema, body["data"])
            assert not errs, f"{method} {path} -> {resp.status_code} violates {schema}: {errs[:5]}"
            VALIDATED["count"] += 1
            return
    raise AssertionError(f"no contract route matched {method} {path}")


class Api:
    def __init__(self, client: TestClient, ws: str = "ws_demo"):
        self.c = client
        self.ws = ws

    def call(self, method, path, json=None, *, token=OP, private=False, key="auto", headers=None, ws=None, params=None):
        base = f"/{'internal' if private else 'api'}/v1/workspaces/{ws or self.ws}"
        h = {"Authorization": f"Bearer {token}"} if token else {}
        if key == "auto" and method in ("POST", "PATCH"):
            key = str(uuid.uuid4())
        if key and key != "auto":
            h["Idempotency-Key"] = key
        h.update(headers or {})
        resp = self.c.request(method, base + path, json=json, headers=h, params=params)
        contract_check(method, base + path, resp)
        return resp

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.call("POST", path, json, **kw)

    def patch(self, path, json=None, **kw):
        return self.call("PATCH", path, json, **kw)

    def fault(self, body: dict):
        r = self.c.post("/__mock/faults", json=body)
        assert r.status_code == 200 and r.headers["X-PromiseGraph-Mock"] == "true", r.text
        return r.json()

    def rev(self) -> int:
        return self.get("/info").json()["meta"]["knowledge_revision"]


class Worker:
    """Minimal worker double that drives the private protocol like Agent B would."""

    def __init__(self, api: Api, worker_id: str = "worker_test"):
        self.api = api
        self.worker_id = worker_id

    def claim(self, kinds=("investigate", "dispatch", "reconcile", "refresh"), expect=200):
        r = self.api.post("/jobs/claim", {"worker_id": self.worker_id, "kinds": list(kinds), "lease_seconds": 60},
                          token=WK, private=True, key=None)
        assert r.status_code == expect, r.text
        return r.json()["data"] if r.status_code == 200 else None

    @staticmethod
    def hdrs(lease):
        return {"X-Job-Id": lease["job_id"], "X-Lease-Token": lease["lease_token"], "X-Fencing-Token": str(lease["fencing_token"])}

    def bind(self, lease):
        return {"lease_token": lease["lease_token"], "fencing_token": lease["fencing_token"]}

    def checkpoint(self, lease, etype, payload, *, stage="investigate", tool_name=None, step_id=None, calls=(),
                   pending_action_id=None, expect=200, expected_event_seq=None):
        seq = expected_event_seq
        if seq is None:
            seq = self.api.get(f"/runs/{lease['run_id']}", token=WK).json()["data"]["event_seq"]
        body = {**self.bind(lease), "step_id": step_id or f"step_{uuid.uuid4().hex[:8]}", "expected_event_seq": seq,
                "event": {"type": etype, "tool_name": tool_name, "payload": payload},
                "checkpoint": {"completed_call_ids": list(calls), "pending_action_id": pending_action_id, "stage": stage,
                               "usage": {"tool_calls": len(calls), "replans": 0, "active_ms": 10}}}
        r = self.api.post(f"/jobs/{lease['job_id']}/checkpoint", body, token=WK, private=True)
        assert r.status_code == expect, r.text
        return r

    def complete(self, lease, outcome, result, *, expect=200, completion_id=None):
        body = {**self.bind(lease), "completion_id": completion_id or f"cmp_{uuid.uuid4().hex[:8]}", "outcome": outcome,
                "result": result}
        r = self.api.post(f"/jobs/{lease['job_id']}/complete", body, token=WK, private=True)
        assert r.status_code == expect, r.text
        return r.json()["data"] if r.status_code == 200 else r.json()

    def propose(self, lease, request_id, kind, payload, evidence_ids, based_on_revision, expect=201):
        body = {"request_id": request_id, "run_id": lease["run_id"], "kind": kind, "payload": payload,
                "evidence_ids": evidence_ids, "explanation": f"Scripted mode test proposal ({kind}).",
                "rejected_alternatives": [], "based_on_revision": based_on_revision}
        r = self.api.post("/proposals", body, token=WK, headers=self.hdrs(lease))
        assert r.status_code == expect, r.text
        return r.json()["data"] if r.status_code == 201 else r.json()

    def commit(self, lease, proposal, expect=202, revision=None):
        r = self.api.post(f"/proposals/{proposal['id']}/commit",
                          {"expected_revision": proposal["based_on_revision"] if revision is None else revision,
                           "authorization_id": None}, token=WK, headers=self.hdrs(lease))
        assert r.status_code == expect, r.text
        return r.json()["data"] if r.status_code == 202 else r.json()

    def execute_local(self, lease, action, expect=200):
        body = {"job_id": lease["job_id"], **self.bind(lease), "expected_action_version": action["version"]}
        r = self.api.post(f"/actions/{action['id']}/execute-local", body, token=WK, private=True)
        assert r.status_code == expect, r.text
        return r.json()["data"] if r.status_code == 200 else r.json()


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.delenv("WORKSPACE_ACL", raising=False)
    store.reset(True)
    yield
    store.reset(True)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client):
    return Api(client)


@pytest.fixture
def worker(api):
    return Worker(api)


def new_request(api: Api, *, subject="Scheduled exports stopped again", body="Our scheduled exports stopped running.",
                caps=("cap_scheduled_exports",), auto_start=True, mode="scripted", customer="customer_acme"):
    r = api.post("/requests", {"customer_id": customer, "subject": subject, "body": body, "channel": "manual",
                               "capability_ids": list(caps), "external_ref": None, "auto_start": auto_start,
                               "planner_mode": mode})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def pytest_sessionfinish(session, exitstatus):
    missing = [(m, p) for m, p, _, _ in ROUTES if (m, p) not in SEEN]
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"contract-validated responses: {VALIDATED['count']}; routes exercised: "
                      f"{len(ROUTES) - len(missing)}/{len(ROUTES)}" + (f"; missing: {missing}" if missing else ""))
