"""Materialize command, result, envelope and private-job DTO schemas (G0).

Source of truth: docs/spec/PromiseGraph-Contracts.md (contract 1.0.0).
Writes contracts/commands.schema.json, which references contracts/resources.schema.json.
Run: python3 scripts/build_contracts.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = "resources.schema.json#/$defs/"


def ref(name):
    return {"$ref": RES + name}


def lref(name):
    return {"$ref": "#/$defs/" + name}


ID = {"type": "string", "minLength": 1, "maxLength": 128}
TS = {"type": "string", "format": "date-time", "pattern": "Z$"}


def s(lo, hi):
    return {"type": "string", "minLength": lo, "maxLength": hi}


def nullable(x):
    return {"anyOf": [x, {"type": "null"}]}


def arr(x, lo=0, hi=100):
    out = {"type": "array", "items": x, "maxItems": hi}
    if lo:
        out["minItems"] = lo
    return out


def integer(lo=None, hi=None):
    out = {"type": "integer"}
    if lo is not None:
        out["minimum"] = lo
    if hi is not None:
        out["maximum"] = hi
    return out


def obj(props, optional=()):
    return {
        "type": "object",
        "properties": props,
        "required": [k for k in props if k not in optional],
        "additionalProperties": False,
    }


def envelope(data):
    return obj({"data": data, "meta": ref("Meta")})


def list_envelope(item):
    return obj({"data": arr(item), "page": obj({"next_cursor": nullable(s(1, 500))}), "meta": ref("Meta")})


LEASE = {"lease_token": s(1, 500), "fencing_token": integer(1)}
CONNECTOR_STATE = {"enum": ["enabled", "disabled", "unconfigured", "unverified", "verified", "unavailable"]}

defs = {}

# ---------------------------------------------------------------- public commands
defs["RequestCreate"] = obj({
    "customer_id": ID,
    "subject": s(1, 200),
    "body": s(1, 20000),
    "channel": {"enum": ["manual", "import", "connector"]},
    "capability_ids": arr(ID),
    "external_ref": nullable(s(1, 500)),
    "auto_start": {"type": "boolean"},
    "planner_mode": ref("PlannerMode"),
})
defs["RunCreate"] = obj({"mode": ref("PlannerMode"), "instruction": s(0, 4000), "authorization_id": nullable(ID)})
defs["RunCancel"] = obj({"reason": s(1, 2000)})
defs["RunResume"] = obj({
    "expected_run_version": integer(1),
    "reason": s(1, 2000),
    "additional_tool_calls": integer(0, 12),
    "additional_active_seconds": integer(0, 90),
})
defs["ReplyCreate"] = obj({"body": s(1, 20000), "external_ref": nullable(s(1, 500)), "expected_request_version": integer(1)})
defs["ContextSearch"] = obj({
    "query": s(1, 500),
    "entity_types": arr({"enum": ["request", "capability", "work_item", "source"]}, 1, 4),
    "customer_id": nullable(ID),
    "limit": integer(1, 20),
})
defs["RelationType"] = {"enum": [
    "SUBMITTED", "ABOUT", "IMPLEMENTED_BY", "TRACKED_BY", "DEPENDS_ON", "OWNED_BY", "RELATED_TO",
    "SUPPORTED_BY", "APPLIES_TO", "SUPERSEDES", "ATTEMPTED_FOR", "OBSERVED_AS", "VERIFIES",
]}
defs["ContextExpand"] = obj({
    "entity_ids": arr(ID, 1, 10),
    "relation_types": arr(lref("RelationType"), 1, 8),
    "depth": integer(1, 6),
    "limit": integer(1, 100),
})

PAYLOADS = {
    "answer": obj({"draft_text": s(1, 12000), "resolution_criteria": arr(s(1, 1000), 0, 20)}),
    "clarify": obj({"question": s(1, 2000), "missing_fields": arr(s(1, 200), 0, 20)}),
    "link_work": obj({"work_item_id": ID, "comment_text": nullable(s(0, 4000))}),
    "create_work": obj({
        "title": s(1, 300), "description": s(1, 16000), "acceptance_criteria": arr(s(1, 2000), 0, 30),
        "owner_id": nullable(ID), "capability_id": nullable(ID), "dependency_ids": arr(ID), "connector": ref("Connector"),
    }),
    "escalate": obj({"reason": s(1, 4000), "owner_id": nullable(ID), "team_id": nullable(ID)}),
}
for kind, payload in PAYLOADS.items():
    defs[f"Payload_{kind}"] = payload
common_proposal = {
    "request_id": ID, "run_id": ID, "evidence_ids": arr(ID), "explanation": s(1, 6000),
    "rejected_alternatives": arr(ref("Alternative"), 0, 5), "based_on_revision": integer(0),
}
defs["ProposalCreate"] = {"oneOf": [
    obj({**common_proposal, "kind": {"const": k}, "payload": lref(f"Payload_{k}")}) for k in PAYLOADS
]}
defs["ProposalCommit"] = obj({"expected_revision": integer(0), "authorization_id": nullable(ID)})
defs["ObservationCreate"] = obj({
    "action_id": nullable(ID),
    "kind": {"enum": ["operator_confirmation", "customer_confirmation", "acceptance_check", "connector_status"]},
    "verification": ref("Verification"),
    "body": s(1, 4000),
    "evidence_ids": arr(ID),
    "expected_request_version": integer(1),
})
defs["RefreshCreate"] = obj({"reason": s(1, 2000)})
defs["WorkItemUpdate"] = obj({
    "expected_version": integer(1), "status": ref("WorkStatus"), "owner_id": nullable(ID),
    "forecast_at": nullable(TS), "reason": s(1, 2000),
})
defs["DecisionCommand"] = obj({
    "operation": {"enum": ["record", "supersede", "revoke"]},
    "decision_id": nullable(ID),
    "rule_type": {"enum": ["forbidden_workaround", "scope_reduction_allowed", "date_extension_allowed",
                           "allowed_action_types", "routing_owner"]},
    "value": {"anyOf": [s(0, 4000), {"type": "boolean"}, arr(s(1, 100), 0, 20)]},
    "scope": ref("Scope"),
    "rationale": s(1, 4000),
    "effective_at": TS,
    "expires_at": nullable(TS),
    "evidence_ids": arr(ID),
    "expected_revision": integer(0),
})
defs["ImportPreview"] = obj({
    "format": {"enum": ["markdown", "json", "csv"]},
    "origin": ref("Origin"),
    "content": s(1, 1048576),
    "source_key": s(1, 200),
})
defs["ImportCommit"] = obj({"expected_revision": integer(0)})

# ---------------------------------------------------------------- public results
defs["ConnectorState"] = CONNECTOR_STATE
defs["WorkspaceInfo"] = obj({
    "workspace_id": ID,
    "name": s(1, 200),
    "clock_mode": {"enum": ["scenario", "live"]},
    "reference_time": TS,
    "planners": obj({"scripted": CONNECTOR_STATE, "model": CONNECTOR_STATE}),
    "connectors": obj({"local": CONNECTOR_STATE, "linear": CONNECTOR_STATE}),
    "auto_commit_kinds": arr(ref("ActionKind"), 0, 5),
})
defs["CapabilitySummary"] = obj({
    "id": ID, "name": s(1, 200), "aliases": arr(s(1, 200), 0, 50),
    "status": {"enum": ["active", "beta", "deprecated", "planned"]},
    "product_version": nullable(s(1, 50)), "owner_id": nullable(ID), "version": integer(1),
})
defs["RequestCreateResult"] = obj({"request": ref("Request"), "run": nullable(ref("Run"))})
defs["ReplyResult"] = obj({"source_id": ID, "source_revision": integer(1), "run": ref("Run")})
defs["SearchCandidate"] = obj({
    "entity_type": {"enum": ["request", "capability", "work_item", "source"]},
    "id": ID, "title": s(1, 300), "score": {"type": "number"}, "evidence": arr(ref("EvidenceRef"), 0, 10),
})
defs["ContextSearchResult"] = obj({"candidates": arr(lref("SearchCandidate"), 0, 20), "truncated": {"type": "boolean"}})
defs["ContextExpandResult"] = obj({
    "paths": arr(ref("EvidencePath"), 0, 100),
    "nodes": arr(ref("GraphNode"), 0, 200),
    "truncated": {"type": "boolean"},
    "visited_count": integer(0),
})
defs["ProposalCommitResult"] = obj({"action": ref("Action"), "job_id": ID})
defs["ObservationResult"] = obj({"observation": ref("Observation"), "request": ref("Request")})
defs["RefreshResult"] = obj({"job_id": ID, "coalesced": {"type": "boolean"}})
defs["WorkItemUpdateResult"] = obj({"work_item": ref("WorkItem"), "knowledge_revision": integer(0)})
defs["SourceSummary"] = obj({
    "id": ID, "revision": integer(1), "title": s(1, 300),
    "kind": {"enum": ["documentation", "request", "work", "decision", "observation"]},
    "origin": ref("Origin"), "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    "observed_at": TS, "ingested_at": TS, "stale": {"type": "boolean"},
})
defs["ImportChange"] = obj({
    "entity_type": s(1, 60), "entity_id": ID, "change": {"enum": ["create", "update", "noop"]},
})
defs["ImportRejection"] = obj({"location": s(1, 500), "reason": s(1, 1000)})
defs["ImportPreviewResult"] = obj({
    "preview_id": ID, "valid_count": integer(0), "rejected_count": integer(0),
    "changes": arr(lref("ImportChange"), 0, 1000), "rejected": arr(lref("ImportRejection"), 0, 1000),
    "based_on_revision": integer(0),
})
defs["SourceRevisionRef"] = obj({"source_id": ID, "revision": integer(1)})
defs["ImportCommitResult"] = obj({
    "imported_count": integer(0), "rejected_count": integer(0), "noop_count": integer(0),
    "source_revisions": arr(lref("SourceRevisionRef"), 0, 1000),
    "invalidated_proposal_count": integer(0), "knowledge_revision": integer(0),
})
defs["EventsPage"] = obj({
    "data": arr(ref("RunEvent")), "next_after_seq": integer(0), "has_more": {"type": "boolean"}, "meta": ref("Meta"),
})
defs["ErrorEnvelope"] = obj({"error": ref("Error"), "meta": ref("Meta")})

# ---------------------------------------------------------------- private job protocol
defs["JobClaim"] = obj({"worker_id": s(1, 128), "kinds": arr(ref("JobKind"), 1, 4), "lease_seconds": integer(10, 300)})
defs["LeaseBinding"] = obj(dict(LEASE))
defs["NewRunEvent"] = obj({
    "type": ref("EventType"), "tool_name": nullable(s(1, 100)), "payload": {"type": "object", "maxProperties": 50},
})
defs["JobCheckpoint"] = obj({
    **LEASE, "step_id": s(1, 128), "expected_event_seq": integer(0), "event": lref("NewRunEvent"),
    "checkpoint": {"type": "object", "maxProperties": 50},
})
defs["CheckpointResult"] = obj({"event": ref("RunEvent"), "next_event_seq": integer(0)})
defs["JobComplete"] = obj({
    **LEASE, "completion_id": s(1, 128), "outcome": ref("JobCompletion"),
    "result": {"type": "object", "maxProperties": 50},
})
defs["CompletionReceipt"] = obj({
    "job_id": ID, "completion_id": s(1, 128), "outcome": ref("JobCompletion"), "recorded_at": TS,
    "run": nullable(ref("Run")), "next_job_id": nullable(ID),
})
defs["RemotePrecondition"] = obj({"target_id": ID, "provider_version": nullable(s(1, 200)), "observed_at": TS})
defs["BeginDispatch"] = obj({
    "job_id": ID, **LEASE, "expected_action_version": integer(1), "expected_revision": integer(0),
    "remote_preconditions": arr(lref("RemotePrecondition"), 0, 20),
})
defs["DispatchPermit"] = obj({"permit_id": ID, "expires_at": TS, "payload_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "action_version": integer(1)})
defs["ExecuteLocal"] = obj({"job_id": ID, **LEASE, "expected_action_version": integer(1)})

# ---------------------------------------------------------------- structured import document (format=json)
SCOPE = ref("Scope")
defs["ImportDocument"] = obj({
    "workspace_id": ID,
    "observed_at": TS,
    "customers": arr(obj({"id": ID, "name": s(1, 200), "plan": nullable(s(1, 100)),
                          "constraints": arr(s(1, 1000), 0, 20)}), 0, 1000),
    "teams": arr(obj({"id": ID, "name": s(1, 200)}), 0, 1000),
    "people": arr(obj({"id": ID, "name": s(1, 200), "role": s(1, 200), "team_id": nullable(ID),
                       "active": {"type": "boolean"}}), 0, 1000),
    "capabilities": arr(obj({
        "id": ID, "name": s(1, 200), "aliases": arr(s(1, 200), 0, 50),
        "status": {"enum": ["active", "beta", "deprecated", "planned"]}, "product_version": nullable(s(1, 50)),
        "owner_id": nullable(ID), "customer_ids": arr(ID, 0, 100),
    }), 0, 1000),
    "work_items": arr(obj({
        "id": ID, "title": s(1, 300), "description": s(0, 16000), "kind": {"enum": ["feature", "ticket", "milestone"]},
        "status": ref("WorkStatus"), "owner_id": nullable(ID), "connector": ref("Connector"),
        "external_id": nullable(ID), "external_url": nullable({"type": "string", "format": "uri"}),
        "forecast_at": nullable(TS), "observed_at": TS, "capability_ids": arr(ID), "depends_on": arr(ID),
        "acceptance_criteria": arr(s(1, 2000), 0, 30),
    }), 0, 1000),
    "requests": arr(obj({
        "id": ID, "customer_id": ID, "subject": s(1, 200), "body": s(1, 20000),
        "status": ref("RequestStatus"), "owner_id": nullable(ID), "capability_ids": arr(ID),
        "linked_work_ids": arr(ID), "related_request_ids": arr(ID), "created_at": TS,
    }), 0, 1000),
    "decisions": arr(obj({
        "id": ID, "rule_type": {"enum": ["forbidden_workaround", "scope_reduction_allowed", "date_extension_allowed",
                                         "allowed_action_types", "routing_owner"]},
        "value": {"anyOf": [s(0, 4000), {"type": "boolean"}, arr(s(1, 100), 0, 20)]},
        "scope": SCOPE, "status": ref("DecisionStatus"), "rationale": s(1, 4000), "author_id": ID,
        "effective_at": TS, "expires_at": nullable(TS), "supersedes_id": nullable(ID),
    }), 0, 1000),
    "attempts": arr(obj({
        "id": ID, "request_id": ID, "work_item_id": nullable(ID), "procedure_key": s(1, 200),
        "context_signature": s(1, 500), "scope": SCOPE, "outcome": {"enum": ["succeeded", "failed", "unknown"]},
        "verification": ref("Verification"), "summary": s(1, 4000), "observed_at": TS,
    }), 0, 1000),
}, optional=("customers", "teams", "people", "capabilities", "work_items", "requests", "decisions", "attempts"))

doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://promisegraph.local/contracts/commands.schema.json",
    "title": "PromiseGraph command, result and private job DTOs",
    "description": "Contract 1.0.0. Generated by scripts/build_contracts.py from PromiseGraph-Contracts.md. Do not edit by hand.",
    "$defs": defs,
}
out = ROOT / "contracts" / "commands.schema.json"
out.write_text(json.dumps(doc, indent=2) + "\n")
print(f"wrote {out.relative_to(ROOT)} with {len(defs)} definitions")
