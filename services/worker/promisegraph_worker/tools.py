"""Typed HTTP tool client: the only way planners touch PromiseGraph state.

Planner call shape: `{call_id, name, arguments}`. Result shape: `{call_id, ok, data, error, meta}` with exactly
one of data/error non-null. Arguments are validated against the contract DTOs before any network I/O; the
request/run identity and lease binding come from the job, never from the planner.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any, Callable

from pydantic import ValidationError

from . import contracts_generated as dto
from .http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, LeaseLost, canonical_json, short_key

TOOL_NAMES = (
    "search_context",
    "expand_context",
    "read_evidence",
    "read_memory",
    "prepare_action",
    "commit_action",
    "inspect_action",
    "record_observation",
)
READ_ONLY = {"search_context", "expand_context", "read_evidence", "read_memory", "inspect_action"}
CACHEABLE = {"search_context", "expand_context", "read_evidence"}
WORKER_OBSERVATION_KINDS = {"connector_status"}

# Compact tool catalog shared by ScriptedPlanner (documentation) and ModelPlanner (Messages API `tools`).
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_context": {
        "description": "Bounded lexical search over permitted docs, requests, capabilities and work. Returns candidates with evidence and a truncation flag.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "entity_types": {"type": "array", "items": {"enum": ["request", "capability", "work_item", "source"]}, "minItems": 1, "maxItems": 4},
                "customer_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query", "entity_types", "customer_id", "limit"],
            "additionalProperties": False,
        },
    },
    "expand_context": {
        "description": "Follow allowlisted graph relations from known entity ids. Returns contiguous evidence paths, nodes and coverage/truncation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
                "relation_types": {"type": "array", "items": {"enum": ["SUBMITTED", "ABOUT", "IMPLEMENTED_BY", "TRACKED_BY", "DEPENDS_ON", "OWNED_BY", "RELATED_TO", "SUPPORTED_BY", "APPLIES_TO", "SUPERSEDES", "ATTEMPTED_FOR", "OBSERVED_AS", "VERIFIES"]}, "minItems": 1, "maxItems": 8},
                "depth": {"type": "integer", "minimum": 1, "maximum": 6},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["entity_ids", "relation_types", "depth", "limit"],
            "additionalProperties": False,
        },
    },
    "read_evidence": {
        "description": "Read one immutable source revision (body, hash, origin, observed/ingested times).",
        "input_schema": {
            "type": "object",
            "properties": {"source_id": {"type": "string"}, "revision": {"type": ["integer", "null"], "minimum": 1}},
            "required": ["source_id"],
            "additionalProperties": False,
        },
    },
    "read_memory": {
        "description": "Request context bundle (sources, paths, related work, decisions, attempts, missing information) plus active decisions for the given scopes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scopes": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {"entity_type": {"enum": ["customer", "request", "capability", "work_item"]}, "entity_id": {"type": "string"}},
                        "required": ["entity_type", "entity_id"],
                        "additionalProperties": False,
                    },
                },
                "depth": {"type": "integer", "minimum": 1, "maximum": 6},
                "work_item_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "Refresh current state of these work items"},
                "person_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "Owner activity (active/inactive)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "prepare_action": {
        "description": "Submit a typed proposal (answer|clarify|link_work|create_work|escalate) for authoritative validation. Cites actual evidence ids; no external effect.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"enum": ["answer", "clarify", "link_work", "create_work", "escalate"]},
                "payload": {"type": "object"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "explanation": {"type": "string", "minLength": 1, "maxLength": 6000},
                "rejected_alternatives": {"type": "array", "maxItems": 5, "items": {"type": "object"}},
                "based_on_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["kind", "payload", "evidence_ids", "explanation", "rejected_alternatives", "based_on_revision"],
            "additionalProperties": False,
        },
    },
    "commit_action": {
        "description": "Commit a valid proposal whose kind the workspace policy lets the worker commit (auto_commit_kinds). Others await an operator.",
        "input_schema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 0}},
            "required": ["proposal_id", "expected_revision"],
            "additionalProperties": False,
        },
    },
    "inspect_action": {
        "description": "Read an action's actual execution state, receipt and verification.",
        "input_schema": {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
    },
    "record_observation": {
        "description": "Record an attributable connector_status observation (never resolves a request by itself).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {"type": ["string", "null"]},
                "kind": {"enum": ["connector_status"]},
                "verification": {"enum": ["pending", "passed", "failed", "inconclusive"]},
                "body": {"type": "string", "minLength": 1, "maxLength": 4000},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action_id", "kind", "verification", "body", "evidence_ids"],
            "additionalProperties": False,
        },
    },
}


def tool_result(call_id: str, *, data: Any = None, error: dict[str, Any] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    if error is not None:
        data = None
    return {"call_id": call_id, "ok": error is None, "data": data, "error": error, "meta": meta or {}}


def _err(code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message[:2000], "retryable": retryable, "details": details or {}}


def _size(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode())


def bound_result(data: Any, limit: int) -> tuple[Any, bool, int]:
    """Trim the result to `limit` bytes of JSON. Returns (data, truncated, original_bytes).

    Trims the longest lists first (keeping order => the highest-ranked items), then long strings. Callers must
    treat `truncated` as incomplete coverage.
    """
    original = _size(data)
    if original <= limit:
        return data, False, original
    data = copy.deepcopy(data)

    def lists(node: Any, depth: int = 0) -> list[list[Any]]:
        found: list[list[Any]] = []
        if depth > 3:
            return found
        if isinstance(node, list):
            if len(node) > 1:
                found.append(node)
            for item in node:
                found.extend(lists(item, depth + 1))
        elif isinstance(node, dict):
            for value in node.values():
                found.extend(lists(value, depth + 1))
        return found

    for _ in range(200):
        if _size(data) <= limit:
            break
        candidates = lists(data)
        if not candidates:
            break
        longest = max(candidates, key=_size)
        del longest[max(1, len(longest) // 2) :]
    if _size(data) > limit:

        def clip(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: clip(v) for k, v in node.items()}
            if isinstance(node, list):
                return [clip(v) for v in node]
            if isinstance(node, str) and len(node) > 2000:
                return node[:2000] + "…[truncated]"
            return node

        data = clip(data)
    if isinstance(data, dict) and "truncated" in data:
        data["truncated"] = True
    return data, True, original


def collect_ids(data: Any, limit: int = 20) -> list[str]:
    ids: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value not in ids and len(ids) < limit:
            ids.append(value)

    if isinstance(data, dict):
        add(data.get("id"))
        for key in ("candidates", "paths", "nodes", "sources", "related_work", "decisions", "attempts"):
            for item in data.get(key) or []:
                if isinstance(item, dict):
                    add(item.get("id"))
        for key in ("proposal", "action", "observation", "context"):
            if isinstance(data.get(key), dict):
                add(data[key].get("id") or data[key].get("request_id"))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                add(item.get("id"))
    return ids


def result_count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("candidates", "paths", "sources"):
            if isinstance(data.get(key), list):
                return len(data[key])
        if isinstance(data.get("context"), dict):
            c = data["context"]
            return sum(len(c.get(k) or []) for k in ("sources", "paths", "related_work", "decisions", "attempts"))
        return 1
    return 0


def redact(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if any(s in key.lower() for s in ("token", "secret", "key", "password", "authorization")):
            out[key] = "[redacted]"
        elif isinstance(value, str) and len(value) > 300:
            out[key] = value[:300] + "…"
        elif isinstance(value, dict):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


class ToolClient:
    """Executes planner tool calls against the API on behalf of one job/run."""

    def __init__(
        self,
        api: ApiClient,
        *,
        workspace_id: str,
        request: dict[str, Any],
        run_id: str | None,
        lease_headers: Callable[[], Any],
        ensure_lease: Callable[[], None],
        auto_commit_kinds: list[str],
        max_result_bytes: int = 24 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.ws = workspace_id
        self.request = request
        self.run_id = run_id
        self._lease_headers = lease_headers
        self._ensure_lease = ensure_lease
        self.auto_commit_kinds = list(auto_commit_kinds)
        self.max_result_bytes = max_result_bytes
        self.clock = clock
        self.proposal_kinds: dict[str, str] = {}
        self.knowledge_revision: int | None = None
        self._cache: dict[str, Any] = {}

    # -- dispatch ----------------------------------------------------------------------------------
    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        call_id = call["call_id"]
        name = call.get("name")
        args = call.get("arguments") or {}
        started = self.clock()
        handler = getattr(self, f"_t_{name}", None) if name in TOOL_NAMES else None
        if handler is None:
            return tool_result(call_id, error=_err("INVALID_INPUT", f"unknown tool {name!r}"), meta={"tool": name, "duration_ms": 0})
        cache_key = None
        if name in CACHEABLE and self.knowledge_revision is not None:
            cache_key = f"{name}|{self.knowledge_revision}|{canonical_json(args)}"
            if cache_key in self._cache:
                data, meta = self._cache[cache_key]
                return tool_result(call_id, data=data, meta={**meta, "cached": True, "duration_ms": 0})
        meta: dict[str, Any] = {"tool": name}
        try:
            data, envelope = handler(call_id, args)
        except LeaseLost:
            raise
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            fields = {".".join(str(p) for p in e.get("loc", ())): e.get("msg", "") for e in exc.errors()[:10]}
            return tool_result(
                call_id,
                error=_err("INVALID_INPUT", f"arguments do not match contract: {first.get('msg', 'invalid')}", details={"fields": fields}),
                meta={**meta, "duration_ms": int((self.clock() - started) * 1000)},
            )
        except _LocalRefusal as exc:
            return tool_result(call_id, error=exc.error, meta={**meta, "duration_ms": int((self.clock() - started) * 1000), "local": True})
        except ApiError as err:
            if err.code == "LEASE_LOST":
                raise LeaseLost(err.message) from err
            return tool_result(
                call_id,
                error=_err(err.code, err.message, retryable=err.retryable, details=err.details),
                meta={**meta, "http_status": err.status, "duration_ms": int((self.clock() - started) * 1000)},
            )
        except ApiTimeout as exc:
            return tool_result(call_id, error=_err("TIMEOUT", str(exc), retryable=True), meta={**meta, "duration_ms": int((self.clock() - started) * 1000)})
        except ApiUnavailable as exc:
            return tool_result(
                call_id, error=_err("DEPENDENCY_UNAVAILABLE", str(exc), retryable=True), meta={**meta, "duration_ms": int((self.clock() - started) * 1000)}
            )
        rev = ((envelope or {}).get("meta") or {}).get("knowledge_revision")
        if isinstance(rev, int):
            self.knowledge_revision = rev
            meta["knowledge_revision"] = rev
        bounded, truncated, original = bound_result(data, self.max_result_bytes)
        meta.update({"duration_ms": int((self.clock() - started) * 1000), "bytes": original, "truncated_by_worker": truncated})
        if cache_key is not None or (name in CACHEABLE and isinstance(rev, int)):
            self._cache[f"{name}|{rev}|{canonical_json(args)}"] = (bounded, meta)
        return tool_result(call_id, data=bounded, meta=meta)

    def _p(self, path: str) -> str:
        return ApiClient.public(self.ws, path)

    # -- read tools --------------------------------------------------------------------------------
    def _t_search_context(self, call_id: str, args: dict[str, Any]):
        body = dto.ContextSearch.model_validate(args).model_dump(mode="json")
        return self.api.call("POST", self._p("/context/search"), json_body=body)

    def _t_expand_context(self, call_id: str, args: dict[str, Any]):
        body = dto.ContextExpand.model_validate(args).model_dump(mode="json")
        return self.api.call("POST", self._p("/context/expand"), json_body=body)

    def _t_read_evidence(self, call_id: str, args: dict[str, Any]):
        source_id = args.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise _LocalRefusal(_err("INVALID_INPUT", "source_id is required", details={"fields": {"source_id": "required"}}))
        params = {"revision": args["revision"]} if args.get("revision") else None
        return self.api.call("GET", self._p(f"/sources/{source_id}"), params=params)

    def _t_read_memory(self, call_id: str, args: dict[str, Any]):
        request_id = self.request["id"]
        params = {"depth": args["depth"]} if args.get("depth") else None
        context, envelope = self.api.call("GET", self._p(f"/requests/{request_id}/context"), params=params)
        scopes = args.get("scopes") or [{"entity_type": "customer", "entity_id": self.request["customer_id"]}]
        decisions: dict[str, dict[str, Any]] = {}
        for scope in scopes[:8]:
            data, _ = self.api.call(
                "GET",
                self._p("/decisions"),
                params={"scope_entity_type": scope["entity_type"], "scope_entity_id": scope["entity_id"], "active": "true", "limit": 100},
            )
            for d in data or []:
                decisions.setdefault(d["id"], d)
        for d in (context or {}).get("decisions") or []:
            decisions.setdefault(d["id"], d)
        work_items, refresh_errors = [], []
        for wid in (args.get("work_item_ids") or [])[:5]:
            try:
                w, _ = self.api.call("GET", self._p(f"/work-items/{wid}"))
                work_items.append(w)
            except ApiError as err:  # unknown item or route not offered: refresh unavailable, not a tool failure
                refresh_errors.append({"work_item_id": wid, "code": err.code})
        out = {"context": context, "decisions": list(decisions.values())}
        # Rev 5 #31: scope-filtered attempts (capability-scoped outcomes before an ABOUT link exists).
        attempts: dict[str, dict[str, Any]] = {}
        for scope in scopes[:8]:
            if scope["entity_type"] not in ("customer", "capability", "request"):
                continue
            try:
                data, _ = self.api.call("GET", self._p("/attempts"), params={"scope_entity_type": scope["entity_type"], "scope_entity_id": scope["entity_id"], "limit": 100})
            except ApiError:
                break  # route/filter not offered by this server: the context bundle is the fallback
            for a in data or []:
                if (a.get("scope") or {}).get("entity_id") == scope["entity_id"]:  # ignore servers that drop the filter
                    attempts.setdefault(a["id"], a)
        if attempts:
            out["attempts"] = list(attempts.values())
        if args.get("person_ids"):
            people, missing = [], []
            for pid in args["person_ids"][:5]:
                try:
                    person, _ = self.api.call("GET", self._p(f"/people/{pid}"))
                    people.append(person)
                except ApiError as err:
                    missing.append({"person_id": pid, "code": err.code})
            out["people"] = people
            out["people_unavailable"] = missing
        if args.get("work_item_ids"):
            out["work_items"] = work_items
            out["work_refresh_errors"] = refresh_errors
        return out, envelope

    def _t_inspect_action(self, call_id: str, args: dict[str, Any]):
        action_id = args.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise _LocalRefusal(_err("INVALID_INPUT", "action_id is required", details={"fields": {"action_id": "required"}}))
        return self.api.call("GET", self._p(f"/actions/{action_id}"))

    # -- mutating tools ----------------------------------------------------------------------------
    def _t_prepare_action(self, call_id: str, args: dict[str, Any]):
        if not self.run_id:
            raise _LocalRefusal(_err("POLICY_VIOLATION", "prepare_action requires a run-bound job"))
        body = {**args, "request_id": self.request["id"], "run_id": self.run_id}
        body = dto.ProposalCreate.model_validate(body).model_dump(mode="json")
        self._ensure_lease()
        data, envelope = self.api.call(
            "POST",
            self._p("/proposals"),
            json_body=body,
            idempotency_key=short_key("proposal", self.run_id, call_id),
            lease=self._lease_headers(),
        )
        if isinstance(data, dict) and data.get("id"):
            self.proposal_kinds[data["id"]] = data.get("kind", body["kind"])
        return data, envelope

    def _t_commit_action(self, call_id: str, args: dict[str, Any]):
        proposal_id = args.get("proposal_id")
        body = dto.ProposalCommit.model_validate({"expected_revision": args.get("expected_revision"), "authorization_id": None}).model_dump(mode="json")
        kind = self.proposal_kinds.get(proposal_id or "")
        if kind is None and proposal_id:
            proposal, _ = self.api.call("GET", self._p(f"/proposals/{proposal_id}"))
            kind = (proposal or {}).get("kind")
            if kind:
                self.proposal_kinds[proposal_id] = kind
        if kind not in self.auto_commit_kinds:
            # Deterministic policy gate: the worker never fabricates an authorization.
            raise _LocalRefusal(
                _err(
                    "POLICY_VIOLATION",
                    f"kind {kind!r} is not in workspace auto_commit_kinds; operator authorization required",
                    details={"auto_commit_kinds": self.auto_commit_kinds},
                )
            )
        self._ensure_lease()
        return self.api.call(
            "POST",
            self._p(f"/proposals/{proposal_id}/commit"),
            json_body=body,
            idempotency_key=short_key("commit", proposal_id),
            lease=self._lease_headers(),
        )

    def _t_record_observation(self, call_id: str, args: dict[str, Any]):
        if args.get("kind") not in WORKER_OBSERVATION_KINDS:
            raise _LocalRefusal(_err("POLICY_VIOLATION", "worker may only record connector_status observations; verification needs an operator, customer or acceptance check"))
        request, _ = self.api.call("GET", self._p(f"/requests/{self.request['id']}"))
        self.request = request or self.request
        body = {
            "action_id": args.get("action_id"),
            "kind": args["kind"],
            "verification": args.get("verification", "pending"),
            "body": args.get("body", ""),
            "evidence_ids": args.get("evidence_ids") or [],
            "expected_request_version": int(self.request.get("version", 1)),
        }
        body = dto.ObservationCreate.model_validate(body).model_dump(mode="json")
        self._ensure_lease()
        return self.api.call(
            "POST",
            self._p(f"/requests/{self.request['id']}/observations"),
            json_body=body,
            idempotency_key=short_key("obs", self.run_id or "norun", call_id),
            lease=self._lease_headers(),
        )


class _LocalRefusal(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error["message"])
        self.error = error
