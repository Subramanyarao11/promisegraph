"""HTTP-layer checks from the FastAPI-guide audit (AUDIT.md): OpenAPI from the frozen contract, ErrorEnvelope on
framework errors, DI overrides, sync error handler (no blocking I/O on the event loop), lifespan cleanup."""

from __future__ import annotations

import inspect
import json

from fastapi.testclient import TestClient

from conftest import OP, SEED, Api, check
from promisegraph_api.deps import get_store
from promisegraph_api.errors import ApiError


def _refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                out.add(v)
            else:
                _refs(v, out)
    elif isinstance(node, list):
        for x in node:
            _refs(x, out)
    return out


def test_openapi_documents_contract_schemas(api: Api):
    r = api.c.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    comps = spec["components"]["schemas"]
    for ref in _refs(spec, set()):
        assert ref.startswith("#/components/schemas/") and ref.split("/")[-1] in comps, ref
    ops = [(p, m, o) for p, d in spec["paths"].items() for m, o in d.items()]
    assert len(ops) == 47
    for p, m, o in ops:
        if p.startswith("/health"):
            continue
        assert o["security"] == [{"bearer": []}]
        assert o["responses"]["4XX"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
        assert "422" not in o["responses"] or "HTTPValidationError" not in json.dumps(o["responses"]["422"])
        ok = [c for c in o["responses"] if c[0] == "2"]
        assert ok and any("content" in o["responses"][c] for c in ok), (m, p)
    intake = spec["paths"]["/api/v1/workspaces/{workspace_id}/requests"]["post"]
    assert "201" in intake["responses"]
    assert intake["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/RequestCreate")


def test_unknown_route_and_method_use_error_envelope(api: Api):
    r = api.c.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    check("ErrorEnvelope", r.json())
    r = api.c.delete("/api/v1/workspaces/ws_demo/requests", headers={"Authorization": f"Bearer {OP}"})
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED" and "allow" in r.headers
    check("ErrorEnvelope", r.json())


def test_dependency_override_store(app_factory):
    app = app_factory()

    class Down:
        def ping(self) -> bool:
            return False
    app.dependency_overrides[get_store] = lambda: Down()
    try:
        r = TestClient(app).get("/health/ready")
        assert r.status_code == 503 and r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        check("ErrorEnvelope", r.json())
    finally:
        app.dependency_overrides.clear()


def test_api_error_handler_runs_off_event_loop_and_fills_meta(seeded: Api):
    handler = seeded.c.app.exception_handlers[ApiError]
    assert not inspect.iscoroutinefunction(handler)  # blocking FalkorDB read happens in the threadpool
    rev = seeded.revision()
    body = seeded.get("/requests/missing", expect=404)
    assert body["meta"]["knowledge_revision"] == rev > 0


def test_lifespan_closes_store(app_factory):
    app = app_factory()
    with TestClient(app) as c:
        c.post("/api/v1/workspaces/ws_demo/imports/preview",
               headers={"Authorization": f"Bearer {OP}", "Idempotency-Key": "lp"},
               json={"format": "json", "origin": "synthetic", "content": json.dumps(SEED), "source_key": "s"})
        assert app.state.store._db is not None
    assert app.state.store._db is None
