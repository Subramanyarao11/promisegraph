"""Shared helpers: time, ids, hashing, errors, contract validation (via contracts/tools/validate.py)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.tools import validate as cv  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

CONTRACT_VERSION = "1.0.0"
REFERENCE_TIME = "2026-10-16T04:30:00Z"
FRESHNESS_DAYS = 7
ACTION_KINDS = ["answer", "clarify", "link_work", "create_work", "escalate"]
RELATION_TYPES = [
    "SUBMITTED", "ABOUT", "IMPLEMENTED_BY", "TRACKED_BY", "DEPENDS_ON", "OWNED_BY", "RELATED_TO",
    "SUPPORTED_BY", "APPLIES_TO", "SUPERSEDES", "ATTEMPTED_FOR", "OBSERVED_AS", "VERIFIES",
]


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- time

def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> str:
    return iso(now_dt())


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def reference_dt() -> datetime:
    return parse_ts(REFERENCE_TIME)


def clock_now() -> datetime:
    """Clock used for freshness/deadlines (scenario reference instant unless CLOCK_MODE=live)."""
    return now_dt() if os.environ.get("CLOCK_MODE", "scenario") == "live" else reference_dt()


def plus(seconds: float) -> datetime:
    return now_dt() + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- ids / hashing

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def bounded_id(prefix: str, base: str) -> str:
    """Deterministic id <= 128 chars."""
    cand = f"{prefix}_{base}"
    if len(cand) <= 128:
        return cand
    return f"{prefix}_{sha256(base)[:40]}"


def pub(d: dict) -> dict:
    """Strip internal (underscore-prefixed) keys from a stored resource."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- errors

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None, retryable: bool | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message[:2000]
        self.details = details or {}
        if retryable is None:
            retryable = status in (429, 503)
        self.retryable = retryable


def not_found(kind: str, ident: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{kind} {ident} not found in this workspace", {"entity": kind, "id": ident})


def invalid(message: str, fields: dict | None = None) -> ApiError:
    return ApiError(422, "INVALID_INPUT", message, {"fields": fields or {}})


# --------------------------------------------------------------------------- contract validation

def _fields_from(errors) -> dict:
    fields: dict[str, str] = {}
    for e in errors:
        path = "/".join(map(str, e.absolute_path)) or "<root>"
        if path not in fields and len(fields) < 50:
            fields[path] = e.message[:500]
    return fields


_branch_cache: dict[tuple[str, int], Draft202012Validator] = {}


def _branch_validator(name: str, idx: int) -> Draft202012Validator:
    key = (name, idx)
    if key not in _branch_cache:
        res, cmd, reg = cv._load()
        base = cv.RES_URI if name in res["$defs"] else cv.CMD_URI
        _branch_cache[key] = Draft202012Validator({"$ref": f"{base}#/$defs/{name}/oneOf/{idx}"}, registry=reg)
    return _branch_cache[key]


def check_input(name: str, body: Any) -> None:
    """Validate a command body against its contract definition; raise 422 INVALID_INPUT with field details."""
    if name == "ProposalCreate":
        if not isinstance(body, dict):
            raise invalid("ProposalCreate must be an object", {"<root>": "expected object"})
        kind = body.get("kind")
        if kind not in ACTION_KINDS:
            raise invalid("ProposalCreate.kind is not a valid ActionKind", {"kind": f"must be one of {ACTION_KINDS}"})
        errs = list(_branch_validator("ProposalCreate", ACTION_KINDS.index(kind)).iter_errors(body))
    else:
        errs = list(cv.validator(name).iter_errors(body))
    if errs:
        raise invalid(f"Request body does not match {name}", _fields_from(errs))


def contract_errors(name: str, value: Any) -> list[str]:
    return cv.errors(name, value)


VALIDATE_RESPONSES = env_flag("MOCK_VALIDATE_RESPONSES", True)


def assert_contract(name: str, value: Any) -> None:
    if not VALIDATE_RESPONSES:
        return
    errs = cv.errors(name, value)
    if errs:
        raise ApiError(500, "MOCK_CONTRACT_VIOLATION", f"mock produced invalid {name}: {'; '.join(errs[:5])}", retryable=False)
