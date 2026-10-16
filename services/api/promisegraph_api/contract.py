"""Contract binding: validate command bodies against contracts/commands.schema.json (coordinator-owned)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .errors import invalid

CONTRACT_VERSION = "1.0.0"
RES_URI = "https://promisegraph.local/contracts/resources.schema.json"
CMD_URI = "https://promisegraph.local/contracts/commands.schema.json"

_contracts_dir: Path | None = None


def configure(contracts_dir: Path) -> None:
    global _contracts_dir
    _contracts_dir = contracts_dir
    _load.cache_clear()
    _validator.cache_clear()
    _subschema_validator.cache_clear()


@lru_cache(maxsize=1)
def _load() -> tuple[dict, dict, Registry]:
    assert _contracts_dir is not None, "contract.configure() not called"
    res = json.loads((_contracts_dir / "resources.schema.json").read_text())
    cmd = json.loads((_contracts_dir / "commands.schema.json").read_text())
    reg = Registry().with_resources([(RES_URI, Resource.from_contents(res)), (CMD_URI, Resource.from_contents(cmd))])
    return res, cmd, reg


def contract_hash_line() -> str:
    assert _contracts_dir is not None
    return (_contracts_dir / "CONTRACT_HASH").read_text().strip()


@lru_cache(maxsize=256)
def _validator(name: str) -> Draft202012Validator:
    res, cmd, reg = _load()
    base = RES_URI if name in res["$defs"] else CMD_URI
    return Draft202012Validator({"$ref": f"{base}#/$defs/{name}"}, registry=reg)


@lru_cache(maxsize=32)
def _subschema_validator(name: str, *path: str) -> Draft202012Validator:
    _res, _cmd, reg = _load()
    return Draft202012Validator({"$ref": f"{CMD_URI}#/$defs/{name}/" + "/".join(path)}, registry=reg)


def errors(name: str, value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    v = _validator(name)
    for e in sorted(v.iter_errors(value), key=lambda e: list(map(str, e.absolute_path))):
        path = "/".join(map(str, e.absolute_path)) or "<root>"
        if len(out) < 40 and path not in out:
            out[path] = e.message[:300]
    return out


def check(name: str, value: Any) -> dict[str, Any]:
    """Validate a command body; raise 422 INVALID_INPUT with `details.fields` on failure."""
    if not isinstance(value, dict):
        raise invalid(f"{name} must be a JSON object", {"<root>": "expected object"})
    errs = errors(name, value)
    if errs:
        # oneOf (ProposalCreate) reports opaque messages; narrow to the declared kind when possible
        if name == "ProposalCreate" and isinstance(value.get("kind"), str):
            kind = value["kind"]
            payload_name = f"Payload_{kind}"
            try:
                sub = errors(payload_name, value.get("payload"))
            except KeyError:
                sub = {}
            if sub:
                errs = {f"payload/{k}" if k != "<root>" else "payload": m for k, m in sub.items()}
        raise invalid(f"{name} validation failed", errs)
    return value


def record_errors(collection: str, record: Any) -> dict[str, str]:
    """Validate one ImportDocument record of `collection` against its item schema."""
    v = _subschema_validator("ImportDocument", "properties", collection, "items")
    out: dict[str, str] = {}
    for e in v.iter_errors(record):
        path = "/".join(map(str, e.absolute_path)) or "<root>"
        out.setdefault(path, e.message[:300])
    return out


def resource_errors(name: str, value: Any) -> dict[str, str]:
    return errors(name, value)
