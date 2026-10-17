"""OpenAPI documentation generated from the frozen contract schemas.

Handlers return pre-built `JSONResponse` envelopes, so FastAPI never re-serializes bodies through Pydantic
(byte-for-byte contract bodies; e.g. no URL normalization). Instead each route declares its request/response
schemas by `$ref` into `contracts/bundle.schema.json`, which is merged into `components/schemas`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

REF = "#/components/schemas/"


def _ref(name: str) -> dict[str, str]:
    return {"$ref": REF + name}


def _envelope(data: str, kind: str) -> dict[str, Any]:
    if kind == "events":
        return _ref("EventsPage")
    if kind == "list":
        return {"type": "object", "additionalProperties": False, "required": ["data", "page", "meta"],
                "properties": {"data": {"type": "array", "items": _ref(data)},
                               "page": {"type": "object", "required": ["next_cursor"],
                                        "properties": {"next_cursor": {"type": ["string", "null"]}}},
                               "meta": _ref("Meta")}}
    return {"type": "object", "additionalProperties": False, "required": ["data", "meta"],
            "properties": {"data": _ref(data), "meta": _ref("Meta")}}


def doc(data: str | None = None, *, body: str | None = None, status: int = 200, kind: str = "single",
        no_content: bool = False) -> dict[str, Any]:
    """Decorator kwargs documenting a route with contract schemas (errors are always ErrorEnvelope)."""
    err = {"description": "ErrorEnvelope (contracts §9)", "content": {"application/json": {"schema": _ref("ErrorEnvelope")}}}
    responses: dict[Any, Any] = {"4XX": err, "5XX": err}
    if data:
        responses[status] = {"description": f"{data} envelope",
                             "content": {"application/json": {"schema": _envelope(data, kind)}}}
    if no_content:
        responses[204] = {"description": "Nothing to claim"}
    out: dict[str, Any] = {"responses": responses, "status_code": status}
    if body:
        out["openapi_extra"] = {"requestBody": {"required": True, "content": {"application/json": {"schema": _ref(body)}}}}
    return out


def _rewrite(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: (REF + v.split("#/$defs/")[-1] if k == "$ref" and isinstance(v, str) and "#/$defs/" in v
                    else _rewrite(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite(x) for x in node]
    return node


def install(app: FastAPI, contracts_dir: Path) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes,
                             description="PromiseGraph API (contract 1.0.0). Bodies are the frozen contract DTOs.")
        bundle = json.loads((contracts_dir / "bundle.schema.json").read_text())
        comps = schema.setdefault("components", {})
        comps.setdefault("schemas", {}).update(_rewrite(bundle["$defs"]))
        comps["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer",
                                               "description": "OPERATOR_TOKEN or WORKER_TOKEN"}}
        for path, ops in schema.get("paths", {}).items():
            if path.startswith("/health"):
                continue
            for op in ops.values():
                op["security"] = [{"bearer": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
