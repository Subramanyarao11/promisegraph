"""Import preview/commit for the formats in contracts/PROTOCOL.md ("Import formats")."""

from __future__ import annotations

import csv
import io
import json

from .common import REFERENCE_TIME, bounded_id, canonical, contract_errors, cv, new_id, now, sha256
from .state import Workspace

COLLECTIONS = {
    # collection: (entity_type, source kind, node label)
    "customers": ("customer", "documentation", "Customer"),
    "teams": ("team", "documentation", "Team"),
    "people": ("person", "documentation", "Person"),
    "capabilities": ("capability", "documentation", "Capability"),
    "work_items": ("work_item", "work", "WorkItem"),
    "requests": ("request", "request", "Request"),
    "decisions": ("decision", "decision", "Decision"),
    "attempts": ("attempt", "observation", "Attempt"),
}
ORDER = list(COLLECTIONS)
CSV_HEADER = ["id", "title", "kind", "status", "owner_id", "capability_ids", "depends_on", "forecast_at", "observed_at"]


def _op(collection, idx, record, observed_at, location=None):
    etype, skind, label = COLLECTIONS[collection]
    return {"collection": collection, "entity_type": etype, "entity_id": record["id"], "record": record,
            "location": location or f"$.{collection}[{idx}]", "source_kind": skind, "label": label,
            "observed_at": record.get("observed_at") or record.get("created_at") or record.get("effective_at") or observed_at}


def _parse_json(ws: Workspace, content: str):
    ops, rejected = [], []
    try:
        doc = json.loads(content)
    except json.JSONDecodeError as e:
        return [], [{"location": "$", "reason": f"invalid JSON: {e}"[:1000]}]
    if not isinstance(doc, dict):
        return [], [{"location": "$", "reason": "ImportDocument must be an object"}]
    bad_records: dict[tuple[str, int], str] = {}
    doc_errors = []
    for e in cv.validator("ImportDocument").iter_errors(doc):
        p = list(e.absolute_path)
        if len(p) >= 2 and p[0] in COLLECTIONS and isinstance(p[1], int):
            bad_records.setdefault((p[0], p[1]), f"{'/'.join(map(str, p[2:])) or '<record>'}: {e.message}"[:1000])
        else:
            doc_errors.append(f"{'/'.join(map(str, p)) or '<root>'}: {e.message}"[:1000])
    if doc.get("workspace_id") != ws.id:
        doc_errors.append(f"workspace_id must equal route workspace {ws.id}")
    if doc_errors:
        return [], [{"location": "$", "reason": r} for r in doc_errors[:1000]]
    observed_at = doc["observed_at"]
    doc_customers = {c.get("id") for c in doc.get("customers", []) if isinstance(c, dict)}
    for coll in ORDER:
        for i, rec in enumerate(doc.get(coll, []) or []):
            if (coll, i) in bad_records:
                rejected.append({"location": f"$.{coll}[{i}]", "reason": bad_records[(coll, i)]})
                continue
            if coll == "requests" and rec["customer_id"] not in doc_customers and rec["customer_id"] not in ws.customers:
                rejected.append({"location": f"$.{coll}[{i}]", "reason": f"unknown customer_id {rec['customer_id']}"})
                continue
            ops.append(_op(coll, i, rec, observed_at))
    return ops, rejected


def _parse_markdown(ws: Workspace, content: str, source_key: str):
    lines = content.splitlines()
    meta = {"title": source_key, "capabilities": [], "observed_at": REFERENCE_TIME}
    start = 0
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            return [], [{"location": "md:L1", "reason": "unterminated front matter"}]
        for ln in lines[1:end]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k, v = k.strip(), v.strip()
                if k == "capabilities":
                    meta["capabilities"] = [c.strip() for c in v.split(",") if c.strip()]
                elif k in ("title", "observed_at"):
                    meta[k] = v
        start = end + 1
    body = "\n".join(lines[start:]).strip()
    if not body:
        return [], [{"location": "md", "reason": "empty markdown body"}]
    try:
        from .common import parse_ts
        parse_ts(meta["observed_at"])
        if not meta["observed_at"].endswith("Z"):
            raise ValueError
    except ValueError:
        return [], [{"location": "md:L1", "reason": "observed_at must be an RFC3339 UTC timestamp ending in Z"}]
    rec = {"id": source_key, "title": meta["title"], "capabilities": meta["capabilities"], "observed_at": meta["observed_at"], "body": body}
    loc = f"md:L{start + 1}-L{max(start + 1, len(lines))}"
    return [{"collection": "markdown", "entity_type": "source", "entity_id": source_key, "record": rec, "location": loc,
             "source_kind": "documentation", "label": "Source", "observed_at": meta["observed_at"]}], []


def _parse_csv(ws: Workspace, content: str):
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)
    if not rows or [h.strip() for h in rows[0]] != CSV_HEADER:
        return [], [{"location": "csv:row1", "reason": f"header must be {','.join(CSV_HEADER)}"}]
    ops, rejected = [], []
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        if len(row) != len(CSV_HEADER):
            rejected.append({"location": f"csv:row{n}", "reason": "wrong column count"})
            continue
        r = {k: v.strip() for k, v in zip(CSV_HEADER, row)}
        lst = lambda s: [x.strip() for x in s.split(";") if x.strip()]  # noqa: E731
        rec = {
            "id": r["id"], "title": r["title"], "description": "", "kind": r["kind"], "status": r["status"],
            "owner_id": r["owner_id"] or None, "connector": "local", "external_id": None, "external_url": None,
            "forecast_at": r["forecast_at"] or None, "observed_at": r["observed_at"],
            "capability_ids": lst(r["capability_ids"]), "depends_on": lst(r["depends_on"]), "acceptance_criteria": [],
        }
        doc = {"workspace_id": ws.id, "observed_at": REFERENCE_TIME, "work_items": [rec]}
        errs = contract_errors("ImportDocument", doc)
        if errs:
            rejected.append({"location": f"csv:row{n}", "reason": "; ".join(errs)[:1000]})
            continue
        ops.append(_op("work_items", n, rec, rec["observed_at"], location=f"csv:row{n}"))
    return ops, rejected


def preview(ws: Workspace, body: dict) -> dict:
    fmt, content, key = body["format"], body["content"], body["source_key"]
    if fmt == "json":
        ops, rejected = _parse_json(ws, content)
    elif fmt == "markdown":
        ops, rejected = _parse_markdown(ws, content, key)
    else:
        ops, rejected = _parse_csv(ws, content)
    changes = []
    for op in ops:
        h = sha256(canonical(op["record"]))
        prev = ws.import_hashes.get((op["entity_type"], op["entity_id"]))
        op["hash"] = h
        op["change"] = "create" if prev is None else ("noop" if prev == h else "update")
        changes.append({"entity_type": op["entity_type"], "entity_id": op["entity_id"], "change": op["change"]})
    pid = new_id("preview")
    ws.previews[pid] = {"id": pid, "ops": ops, "origin": body["origin"], "source_key": key, "format": fmt,
                        "based_on_revision": ws.revision, "committed": None,
                        "rejected_count": len(rejected)}
    return {"preview_id": pid, "valid_count": len(ops), "rejected_count": len(rejected), "changes": changes[:1000],
            "rejected": rejected[:1000], "based_on_revision": ws.revision}


def commit(ws: Workspace, preview_rec: dict) -> dict:
    imported = noop = 0
    revs, changed_entities = [], set()
    origin = preview_rec["origin"]
    pending = [op for op in preview_rec["ops"]
               if op["change"] != "noop" and ws.import_hashes.get((op["entity_type"], op["entity_id"])) != op["hash"]]
    for op in pending:  # nodes first so forward references inside one document resolve
        ws.add_node(op["entity_id"], op["label"], _title(op["record"], op["entity_id"]))
    for op in preview_rec["ops"]:
        if op["change"] == "noop" or ws.import_hashes.get((op["entity_type"], op["entity_id"])) == op["hash"]:
            noop += 1
            continue
        src, ev = _write_source(ws, op, origin)
        _apply(ws, op, ev["id"])
        ws.import_hashes[(op["entity_type"], op["entity_id"])] = op["hash"]
        revs.append({"source_id": src["id"], "revision": src["revision"]})
        changed_entities.add(op["entity_id"])
        imported += 1
    invalidated = 0
    if imported:
        ws.bump()
        invalidated = ws.invalidate_proposals(changed_entities, f"imported knowledge changed (revision {ws.revision})")
    return {"imported_count": imported, "rejected_count": preview_rec.get("rejected_count", 0), "noop_count": noop, "source_revisions": revs[:1000],
            "invalidated_proposal_count": invalidated, "knowledge_revision": ws.revision}


def _title(r: dict, eid: str) -> str:
    return r.get("name") or r.get("title") or r.get("subject") or r.get("rule_type") or r.get("procedure_key") or eid


def _write_source(ws: Workspace, op: dict, origin: str):
    rec = op["record"]
    if op["entity_type"] == "source":
        sid, body, title = rec["id"], rec["body"], rec["title"]
        excerpt = body
    else:
        sid = bounded_id("src", op["entity_id"])
        body = canonical(rec)
        name = rec.get("name") or rec.get("title") or rec.get("subject") or rec.get("rule_type") or rec.get("procedure_key") or ""
        title = f"{op['entity_type']} {op['entity_id']}: {name}"
        excerpt = body
    src, _ = ws.add_source(sid, title=title, kind=op["source_kind"], origin=origin, body=body, observed_at=op["observed_at"])
    ev = ws.add_evidence(src, excerpt=excerpt, location=op["location"], entities=[op["entity_id"]])
    return src, ev


def _apply(ws: Workspace, op: dict, ev: str) -> None:
    r, et, eid = op["record"], op["entity_type"], op["entity_id"]
    t = now()
    ws.drop_edges_by(eid)
    ws.add_node(eid, op["label"], _title(r, eid))
    edge = lambda typ, a, b: ws.add_edge(typ, a, b, [ev], asserted_by=eid)  # noqa: E731

    def version_of(coll):
        old = coll.get(eid)
        return old["version"] + 1 if old else 1

    if et == "customer":
        ws.customers[eid] = {"id": eid, "workspace_id": ws.id, "name": r["name"], "version": version_of(ws.customers),
                             "_plan": r.get("plan"), "_constraints": r.get("constraints", [])}
    elif et == "team":
        ws.teams[eid] = dict(r)
    elif et == "person":
        ws.people[eid] = dict(r)
        if r.get("team_id"):
            edge("RELATED_TO", eid, r["team_id"])
    elif et == "capability":
        ws.capabilities[eid] = {"id": eid, "name": r["name"], "aliases": r["aliases"], "status": r["status"],
                                "product_version": r["product_version"], "owner_id": r["owner_id"],
                                "version": version_of(ws.capabilities), "_customer_ids": r["customer_ids"]}
        if r["owner_id"]:
            edge("OWNED_BY", eid, r["owner_id"])
        for c in r["customer_ids"]:
            edge("RELATED_TO", c, eid)
    elif et == "work_item":
        ws.work_items[eid] = {
            "id": eid, "workspace_id": ws.id, "title": r["title"], "kind": r["kind"], "status": r["status"],
            "owner_id": r["owner_id"], "connector": r["connector"], "external_id": r["external_id"],
            "external_url": r["external_url"], "forecast_at": r["forecast_at"], "observed_at": r["observed_at"],
            "evidence_ids": [ev], "version": version_of(ws.work_items), "_description": r.get("description", ""),
            "_capability_ids": r["capability_ids"], "_depends_on": r["depends_on"],
            "_acceptance_criteria": r.get("acceptance_criteria", []),
        }
        for c in r["capability_ids"]:
            edge("IMPLEMENTED_BY", c, eid)
        for d in r["depends_on"]:
            edge("DEPENDS_ON", eid, d)
        if r["owner_id"]:
            edge("OWNED_BY", eid, r["owner_id"])
    elif et == "request":
        old = ws.requests.get(eid)
        ws.requests[eid] = {
            "id": eid, "workspace_id": ws.id, "customer_id": r["customer_id"], "subject": r["subject"], "body": r["body"],
            "channel": "import", "status": r["status"], "owner_id": r["owner_id"], "source_id": bounded_id("src", eid),
            "capability_ids": r["capability_ids"], "linked_work_ids": r["linked_work_ids"], "created_at": r["created_at"],
            "updated_at": t, "version": old["version"] + 1 if old else 1, "_related": r["related_request_ids"],
        }
        edge("SUBMITTED", r["customer_id"], eid)
        for c in r["capability_ids"]:
            edge("ABOUT", eid, c)
        for w in r["linked_work_ids"]:
            edge("TRACKED_BY", eid, w)
        for q in r["related_request_ids"]:
            edge("RELATED_TO", eid, q)
    elif et == "decision":
        ws.decisions[eid] = {
            "id": eid, "workspace_id": ws.id, "rule_type": r["rule_type"], "value": r["value"], "scope": r["scope"],
            "status": r["status"], "rationale": r["rationale"], "author_id": r["author_id"], "effective_at": r["effective_at"],
            "expires_at": r["expires_at"], "supersedes_id": r["supersedes_id"], "evidence_ids": [ev],
            "version": version_of(ws.decisions),
        }
        edge("APPLIES_TO", eid, r["scope"]["entity_id"])
        if r["supersedes_id"]:
            edge("SUPERSEDES", eid, r["supersedes_id"])
    elif et == "attempt":
        ws.attempts[eid] = {
            "id": eid, "workspace_id": ws.id, "request_id": r["request_id"], "procedure_key": r["procedure_key"],
            "context_signature": r["context_signature"], "scope": r["scope"], "outcome": r["outcome"],
            "verification": r["verification"], "summary": r["summary"], "evidence_ids": [ev], "observed_at": r["observed_at"],
        }
        edge("ATTEMPTED_FOR", eid, r["request_id"])
        if r.get("work_item_id"):
            edge("RELATED_TO", eid, r["work_item_id"])
        edge("APPLIES_TO", eid, r["scope"]["entity_id"])
    elif et == "source":
        ws.add_node(eid, "Source", r["title"])
        for c in r["capabilities"]:
            edge("SUPPORTED_BY", c, eid)
