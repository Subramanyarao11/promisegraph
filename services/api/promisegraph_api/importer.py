"""Deterministic structured importer (spec §7, PROTOCOL "Import formats").

validate -> normalize -> preview (changes computed against the current graph) -> commit in ONE atomic query:
new immutable source revisions, entity upserts, record-owned edges, knowledge revision bump, and
invalidation of proposals that consulted changed entities/sources. Unchanged canonical hash -> noop.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import contract
from .core import Ctx, bump_revision, put, put_request, put_work_item, source_revision
from .errors import invalid
from .store import UnitOfWork
from .util import canonical_json, edge_id, parse_ts

COLLECTIONS = ["customers", "teams", "people", "capabilities", "work_items", "requests", "decisions", "attempts"]
ETYPE = {"customers": "customer", "teams": "team", "people": "person", "capabilities": "capability",
         "work_items": "work_item", "requests": "request", "decisions": "decision", "attempts": "attempt"}
LABEL = {"customer": "Customer", "team": "Team", "person": "Person", "capability": "Capability",
         "work_item": "WorkItem", "request": "Request", "decision": "Decision", "attempt": "Attempt",
         "document": "Source"}
SOURCE_KIND = {"customer": "documentation", "capability": "documentation", "team": "work", "person": "work",
               "work_item": "work", "request": "request", "decision": "decision", "attempt": "observation"}
SCOPE_LABEL = {"customer": "Customer", "request": "Request", "capability": "Capability", "work_item": "WorkItem"}
CSV_HEADER = ["id", "title", "kind", "status", "owner_id", "capability_ids", "depends_on", "forecast_at", "observed_at"]
VIA = "import"


@dataclass
class Rec:
    etype: str
    id: str
    data: dict[str, Any]
    location: str
    observed_at: str
    refs: list[tuple[str, str, set[str]]] = field(default_factory=list)  # (field, id, allowed labels)

    @property
    def source_id(self) -> str:
        return self.id if self.etype == "document" else f"src_{self.etype}_{self.id}"

    def title(self) -> str:
        d = self.data
        name = d.get("name") or d.get("title") or d.get("subject") or d.get("procedure_key") or d.get("rule_type") or ""
        return (f"{self.etype.replace('_', ' ').title()} {self.id}: {name}" if self.etype != "document"
                else d.get("title") or self.id)[:300]

    def hash_text(self) -> str:
        return self.data["raw"] if self.etype == "document" else self.body()

    def body(self) -> str:
        return self.data["body"] if self.etype == "document" else canonical_json(self.data)


def _refs(etype: str, d: dict[str, Any]) -> list[tuple[str, str, set[str]]]:
    owner = {"Person", "Team"}
    out: list[tuple[str, str, set[str]]] = []

    def one(f: str, v: Any, labels: set[str]) -> None:
        if v:
            out.append((f, v, labels))

    def many(f: str, vs: list | None, labels: set[str]) -> None:
        for i, v in enumerate(vs or []):
            out.append((f"{f}[{i}]", v, labels))

    if etype == "person":
        one("team_id", d.get("team_id"), {"Team"})
    elif etype == "capability":
        one("owner_id", d.get("owner_id"), owner)
        many("customer_ids", d.get("customer_ids"), {"Customer"})
    elif etype == "work_item":
        one("owner_id", d.get("owner_id"), owner)
        many("capability_ids", d.get("capability_ids"), {"Capability"})
        many("depends_on", d.get("depends_on"), {"WorkItem"})
    elif etype == "request":
        one("customer_id", d.get("customer_id"), {"Customer"})
        one("owner_id", d.get("owner_id"), owner)
        many("capability_ids", d.get("capability_ids"), {"Capability"})
        many("linked_work_ids", d.get("linked_work_ids"), {"WorkItem"})
        many("related_request_ids", d.get("related_request_ids"), {"Request"})
    elif etype == "decision":
        sc = d.get("scope") or {}
        one("scope.entity_id", sc.get("entity_id"), {SCOPE_LABEL.get(sc.get("entity_type"), "?")})
        one("author_id", d.get("author_id"), {"Person"})
        one("supersedes_id", d.get("supersedes_id"), {"Decision"})
        if d.get("rule_type") == "routing_owner" and isinstance(d.get("value"), str) and d.get("value"):
            one("value", d["value"], owner)
    elif etype == "attempt":
        sc = d.get("scope") or {}
        one("request_id", d.get("request_id"), {"Request"})
        one("work_item_id", d.get("work_item_id"), {"WorkItem"})
        one("scope.entity_id", sc.get("entity_id"), {SCOPE_LABEL.get(sc.get("entity_type"), "?")})
    elif etype == "document":
        many("capabilities", d.get("capabilities"), {"Capability"})
    return out


def _decision_value_error(d: dict[str, Any]) -> str | None:
    rt, v = d.get("rule_type"), d.get("value")
    if rt in ("scope_reduction_allowed", "date_extension_allowed") and not isinstance(v, bool):
        return f"{rt} requires a boolean value"
    if rt == "allowed_action_types":
        kinds = {"answer", "clarify", "link_work", "create_work", "escalate"}
        if not isinstance(v, list) or any(x not in kinds for x in v):
            return "allowed_action_types requires a list of ActionKind values"
    if rt in ("forbidden_workaround", "routing_owner") and (not isinstance(v, str) or not v):
        return f"{rt} requires a non-empty string value"
    if d.get("expires_at") and d.get("effective_at") and parse_ts(d["expires_at"]) <= parse_ts(d["effective_at"]):
        return "expires_at must be after effective_at"
    return None


# ---- parsing ----------------------------------------------------------------------------------------
def parse(ws: str, fmt: str, content: str, source_key: str) -> tuple[list[Rec], list[dict[str, str]]]:
    if fmt == "json":
        return _parse_json(ws, content)
    if fmt == "markdown":
        return _parse_markdown(content, source_key)
    return _parse_csv(content)


def _parse_json(ws: str, content: str) -> tuple[list[Rec], list[dict[str, str]]]:
    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        raise invalid("content is not valid JSON", {"content": str(exc)[:300]}) from exc
    if not isinstance(doc, dict):
        raise invalid("ImportDocument must be an object", {"content": "expected object"})
    top = {k: v for k, v in doc.items() if k not in COLLECTIONS}
    top_errs = {k: m for k, m in contract.errors("ImportDocument", {**top}).items()}
    if top_errs:
        raise invalid("ImportDocument validation failed", {f"content/{k}": m for k, m in top_errs.items()})
    if doc["workspace_id"] != ws:
        raise invalid("ImportDocument.workspace_id must equal the route workspace",
                      {"content/workspace_id": f"expected {ws}"})
    recs: list[Rec] = []
    rejected: list[dict[str, str]] = []
    for coll in COLLECTIONS:
        items = doc.get(coll, [])
        if not isinstance(items, list):
            raise invalid(f"{coll} must be an array", {f"content/{coll}": "expected array"})
        for i, item in enumerate(items):
            loc = f"$.{coll}[{i}]"
            errs = contract.record_errors(coll, item)
            if errs:
                path, msg = next(iter(errs.items()))
                rejected.append({"location": loc, "reason": f"{path}: {msg}"[:1000]})
                continue
            etype = ETYPE[coll]
            if etype == "decision":
                verr = _decision_value_error(item)
                if verr:
                    rejected.append({"location": loc, "reason": verr})
                    continue
            recs.append(Rec(etype, item["id"], item, loc, item.get("observed_at") or doc["observed_at"],
                            _refs(etype, item)))
    return recs, rejected


_FM = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def _parse_markdown(content: str, source_key: str) -> tuple[list[Rec], list[dict[str, str]]]:
    if len(source_key) > 128:
        raise invalid("markdown source_key is the source id and must be <= 128 chars", {"source_key": "too long"})
    meta: dict[str, Any] = {}
    body = content
    m = _FM.match(content)
    offset = 0
    if m:
        offset = m.group(0).count("\n")
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = content[m.end():]
    if not body.strip():
        raise invalid("markdown document has no body", {"content": "empty body"})
    caps = [c.strip() for c in meta.get("capabilities", "").split(",") if c.strip()]
    observed = meta.get("observed_at") or ""
    rejected: list[dict[str, str]] = []
    if observed:
        try:
            parse_ts(observed)
            if not observed.endswith("Z"):
                raise ValueError
        except ValueError:
            rejected.append({"location": "front_matter.observed_at", "reason": "observed_at must be RFC3339 UTC (Z)"})
            observed = ""
    title = meta.get("title")
    if not title:
        h = re.search(r"^#\s+(.+)$", body, re.M)
        title = h.group(1).strip() if h else source_key
    data = {"title": title[:300], "capabilities": caps, "body": body, "line_offset": offset, "raw": content}
    return [Rec("document", source_key, data, "md", observed, _refs("document", data))], rejected


def markdown_sections(body: str, line_offset: int) -> list[tuple[str, str]]:
    """Split into heading sections (paragraphs if a section exceeds 4000 chars). Location md:L<start>-L<end>."""
    lines = body.split("\n")
    sections: list[tuple[int, int]] = []
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("#") and i > start:
            sections.append((start, i - 1))
            start = i
    sections.append((start, len(lines) - 1))
    out: list[tuple[str, str]] = []
    for s, e in sections:
        while s <= e and not lines[s].strip():
            s += 1
        while e >= s and not lines[e].strip():
            e -= 1
        if s > e:
            continue
        text = "\n".join(lines[s:e + 1])
        if len(text) <= 4000:
            out.append((text, f"md:L{s + 1 + line_offset}-L{e + 1 + line_offset}"))
            continue
        cur_s = s
        for j in range(s, e + 2):
            if j == e + 1 or not lines[j].strip():
                if j - 1 >= cur_s:
                    chunk = "\n".join(lines[cur_s:j])
                    if chunk.strip():
                        out.append((chunk[:4000], f"md:L{cur_s + 1 + line_offset}-L{j + line_offset}"))
                cur_s = j + 1
    return out[:100] or [(body[:4000], f"md:L{1 + line_offset}-L{len(lines) + line_offset}")]


def _parse_csv(content: str) -> tuple[list[Rec], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise invalid("csv is empty", {"content": "missing header"}) from exc
    if [h.strip() for h in header] != CSV_HEADER:
        raise invalid("csv header mismatch", {"content": "expected " + ",".join(CSV_HEADER)})
    recs: list[Rec] = []
    rejected: list[dict[str, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if not any(c.strip() for c in row):
            continue
        loc = f"csv:L{line_no}"
        if len(row) != len(CSV_HEADER):
            rejected.append({"location": loc, "reason": f"expected {len(CSV_HEADER)} columns, got {len(row)}"})
            continue
        r = {k: v.strip() for k, v in zip(CSV_HEADER, row)}

        def lst(v: str) -> list[str]:
            return [x.strip() for x in v.split(";") if x.strip()] if v else []

        item = {"id": r["id"], "title": r["title"], "description": "", "kind": r["kind"], "status": r["status"],
                "owner_id": r["owner_id"] or None, "connector": "local", "external_id": None, "external_url": None,
                "forecast_at": r["forecast_at"] or None, "observed_at": r["observed_at"],
                "capability_ids": lst(r["capability_ids"]), "depends_on": lst(r["depends_on"]),
                "acceptance_criteria": []}
        errs = contract.record_errors("work_items", item)
        if errs:
            path, msg = next(iter(errs.items()))
            rejected.append({"location": loc, "reason": f"{path}: {msg}"[:1000]})
            continue
        recs.append(Rec("work_item", item["id"], item, loc, item["observed_at"], _refs("work_item", item)))
    return recs, rejected


# ---- planning ---------------------------------------------------------------------------------------
def plan(ctx: Ctx, recs: list[Rec], rejected: list[dict[str, str]], default_observed: str
         ) -> tuple[list[tuple[Rec, str]], list[dict[str, str]]]:
    """Resolve references (graph + this document), reject dangling/duplicates, classify create/update/noop."""
    rejected = list(rejected)
    seen: dict[tuple[str, str], int] = {}
    valid: list[Rec] = []
    for r in recs:
        key = (LABEL[r.etype], r.id)
        if key in seen:
            rejected.append({"location": r.location, "reason": f"duplicate {r.etype} id {r.id} in this import"})
            continue
        seen[key] = 1
        if not r.observed_at:
            r.observed_at = default_observed
        valid.append(r)
    ref_ids = {i for r in valid for (_f, i, _l) in r.refs} | {r.id for r in valid} | {r.source_id for r in valid}
    existing = ctx.store.get_many(ctx.ws, list(ref_ids))
    # id must not already belong to a different kind of node
    keep = []
    for r in valid:
        ex = existing.get(r.id)
        if ex and r.etype != "document" and ex.get("label") != LABEL[r.etype]:
            rejected.append({"location": r.location,
                             "reason": f"id {r.id} already used by a {ex.get('label')} node"})
            continue
        if r.etype == "document" and ex and ex.get("label") != "Source":
            rejected.append({"location": r.location, "reason": f"source_key {r.id} collides with a {ex.get('label')}"})
            continue
        keep.append(r)
    valid = keep
    # fixpoint: references must resolve to existing nodes or valid records of an allowed label
    changed = True
    while changed:
        changed = False
        available: dict[str, set[str]] = {}
        for nid, p in existing.items():
            available.setdefault(nid, set()).add(p.get("label", ""))
        for r in valid:
            available.setdefault(r.id, set()).add(LABEL[r.etype])
        nxt = []
        for r in valid:
            bad = None
            for f, ref, labels in r.refs:
                if r.etype == "document" and f.startswith("capabilities"):
                    continue  # unresolved capability links are reported per-link, document still imports
                if ref == r.id and LABEL[r.etype] in labels:
                    bad = f"{f}: self reference {ref}"
                    break
                if not (available.get(ref, set()) & labels):
                    bad = f"{f}: unknown reference {ref} (expected {'/'.join(sorted(labels))})"
                    break
            if bad:
                rejected.append({"location": r.location, "reason": bad[:1000]})
                changed = True
            else:
                nxt.append(r)
        valid = nxt
    for r in valid:
        if r.etype == "document":
            avail_caps = {nid for nid, p in existing.items() if p.get("label") == "Capability"}
            avail_caps |= {x.id for x in valid if x.etype == "capability"}
            ok = []
            for i, c in enumerate(r.data["capabilities"]):
                if c in avail_caps:
                    ok.append(c)
                else:
                    rejected.append({"location": f"front_matter.capabilities[{i}]",
                                     "reason": f"unknown capability {c}; link not created"})
            r.data["capabilities"] = ok
    out: list[tuple[Rec, str]] = []
    from .util import sha256_hex
    for r in valid:
        head = existing.get(r.source_id)
        if head is None:
            out.append((r, "create"))
        elif head.get("sha256") == sha256_hex(r.hash_text()):
            out.append((r, "noop"))
        else:
            out.append((r, "update"))
    return out, rejected


# ---- apply ------------------------------------------------------------------------------------------
def apply(ctx: Ctx, uow: UnitOfWork, planned: list[tuple[Rec, str]], origin: str, current_revision: int
          ) -> dict[str, Any]:
    changed_ids: list[str] = []
    revisions: list[dict[str, Any]] = []
    noop = 0
    ids = [r.id for r, _ in planned] + [r.source_id for r, _ in planned]
    existing = ctx.store.get_many(ctx.ws, ids)
    for r, change in planned:
        if change == "noop":
            noop += 1
            continue
        head = existing.get(r.source_id)
        excerpts = None
        if r.etype == "document":
            excerpts = markdown_sections(r.data["body"], r.data.get("line_offset", 0))
        else:
            body = r.body()
            if len(body) <= 4000:
                excerpts = [(body, r.location)]
            else:
                key = "title" if "title" in r.data else ("name" if "name" in r.data else "id")
                excerpts = [(json.dumps({key: r.data[key]}, ensure_ascii=False)[1:-1], f"{r.location}.{key}")]
        res = source_revision(ctx, uow, r.source_id, title=r.title(), kind=SOURCE_KIND.get(r.etype, "documentation"),
                              origin=origin, body=r.body(), observed_at=r.observed_at, excerpts=excerpts,
                              head=head, head_known=True, hash_text=r.hash_text())
        revisions.append({"source_id": r.source_id, "revision": res["revision"]})
        changed_ids += [r.id, r.source_id]
        _apply_entity(ctx, uow, r, res["evidence_ids"], existing.get(r.id))
    invalidated = 0
    new_rev = current_revision
    if changed_ids:
        new_rev = bump_revision(uow, current_revision)
        invalidated = invalidate_consulting(ctx, uow, changed_ids, f"import changed {len(revisions)} source(s)")
    return {"source_revisions": revisions, "noop_count": noop, "imported_count": len(revisions),
            "invalidated_proposal_count": invalidated, "knowledge_revision": new_rev}


def invalidate_consulting(ctx: Ctx, uow: UnitOfWork, changed_ids: list[str], reason: str) -> int:
    """Mark valid, uncommitted proposals that CONSULTED any changed node as stale (dependency links)."""
    rows = ctx.store.read(ctx.ws, "MATCH (p:Proposal)-[:CONSULTED]->(n:E) WHERE n.id IN $ids AND p.validity = 'valid' "
                                  "RETURN DISTINCT properties(p)", {"ids": list(set(changed_ids))})
    from .store import _decode
    count = 0
    for (props,) in rows:
        p = _decode(props)
        doc = dict(p["doc"])
        doc["validity"] = "stale"
        doc["validation_reasons"] = (doc.get("validation_reasons") or [])[:29] + [f"REVISION_CONFLICT: {reason}"[:1000]]
        doc["version"] = int(doc["version"]) + 1
        put(uow, "Proposal", doc, validity="stale", request_id=doc["request_id"], run_id=doc["run_id"])
        count += 1
    return count


def _edge(uow: UnitOfWork, t: str, a: str, b: str, ev: list[str], confirmed: bool = True) -> None:
    uow.edge(t, edge_id(t, a, b), a, b, evidence_ids=ev[:100], confirmed=confirmed, via=VIA)


def _apply_entity(ctx: Ctx, uow: UnitOfWork, r: Rec, ev: list[str], prev: dict[str, Any] | None) -> None:
    d = r.data
    version = int(prev.get("version") or 0) + 1 if prev else 1
    nid = r.id
    if r.etype == "document":
        uow.delete_edges(nid, "SUPPORTED_BY", "in", VIA)
        for c in d["capabilities"]:
            _edge(uow, "SUPPORTED_BY", c, nid, ev)
        return
    # replace record-owned edges (only those created by imports)
    owned_out = {"person": ["MEMBER_OF"], "capability": ["OWNED_BY"], "work_item": ["DEPENDS_ON", "OWNED_BY"],
                 "request": ["ABOUT", "TRACKED_BY", "RELATED_TO"], "decision": ["APPLIES_TO", "SUPERSEDES"],
                 "attempt": ["ATTEMPTED_FOR"]}.get(r.etype, [])
    owned_in = {"work_item": ["IMPLEMENTED_BY"], "request": ["SUBMITTED"]}.get(r.etype, [])
    if prev:
        for t in owned_out:
            uow.delete_edges(nid, t, "out", VIA)
        for t in owned_in:
            uow.delete_edges(nid, t, "in", VIA)
        uow.delete_edges(nid, "SUPPORTED_BY", "out", VIA)
    _edge(uow, "SUPPORTED_BY", nid, r.source_id, ev)
    ws = ctx.ws
    if r.etype == "customer":
        doc = {"id": nid, "workspace_id": ws, "name": d["name"], "version": version}
        put(uow, "Customer", doc, title=d["name"], name=d["name"], plan=d.get("plan") or "",
            constraints=d.get("constraints") or [], search_text=d["name"].lower())
    elif r.etype == "team":
        uow.node("Team", nid, doc={"id": nid, "name": d["name"]}, title=d["name"], version=version)
    elif r.etype == "person":
        uow.node("Person", nid, doc={"id": nid, "name": d["name"], "role": d["role"], "team_id": d.get("team_id"),
                                     "active": d["active"]}, title=d["name"], active=d["active"], version=version)
        if d.get("team_id"):
            _edge(uow, "MEMBER_OF", nid, d["team_id"], ev)
    elif r.etype == "capability":
        doc = {"id": nid, "name": d["name"], "aliases": d["aliases"], "status": d["status"],
               "product_version": d.get("product_version"), "owner_id": d.get("owner_id"), "version": version}
        put(uow, "Capability", doc, title=d["name"], status=d["status"], customer_ids=d.get("customer_ids") or [],
            search_text=" ".join([d["name"], *d["aliases"]]).lower())
        if d.get("owner_id"):
            _edge(uow, "OWNED_BY", nid, d["owner_id"], ev)
    elif r.etype == "work_item":
        doc = {"id": nid, "workspace_id": ws, "title": d["title"], "kind": d["kind"], "status": d["status"],
               "owner_id": d.get("owner_id"), "connector": d["connector"], "external_id": d.get("external_id"),
               "external_url": d.get("external_url"), "forecast_at": d.get("forecast_at"),
               "observed_at": d["observed_at"], "evidence_ids": ev[:100], "version": version}
        put_work_item(uow, doc, description=d.get("description") or "", source_id=r.source_id,
                      acceptance_criteria=d.get("acceptance_criteria") or [],
                      capability_ids=d.get("capability_ids") or [], depends_on=d.get("depends_on") or [])
        for c in d.get("capability_ids") or []:
            _edge(uow, "IMPLEMENTED_BY", c, nid, ev)
        for dep in d.get("depends_on") or []:
            _edge(uow, "DEPENDS_ON", nid, dep, ev)
        if d.get("owner_id"):
            _edge(uow, "OWNED_BY", nid, d["owner_id"], ev)
    elif r.etype == "request":
        doc = {"id": nid, "workspace_id": ws, "customer_id": d["customer_id"], "subject": d["subject"],
               "body": d["body"], "channel": "import", "status": d["status"], "owner_id": d.get("owner_id"),
               "source_id": r.source_id, "capability_ids": d.get("capability_ids") or [],
               "linked_work_ids": d.get("linked_work_ids") or [], "created_at": d["created_at"],
               "updated_at": d["created_at"] if not prev else ctx.now, "version": version}
        put_request(uow, doc, ts=parse_ts(d["created_at"]).timestamp())
        _edge(uow, "SUBMITTED", d["customer_id"], nid, ev)
        for c in d.get("capability_ids") or []:
            _edge(uow, "ABOUT", nid, c, ev)
        for w in d.get("linked_work_ids") or []:
            _edge(uow, "TRACKED_BY", nid, w, ev)
        for rr in d.get("related_request_ids") or []:
            # candidate association only: never confirmed duplicate/root-cause identity (T28)
            _edge(uow, "RELATED_TO", nid, rr, ev, confirmed=False)
    elif r.etype == "decision":
        doc = {"id": nid, "workspace_id": ws, "rule_type": d["rule_type"], "value": d["value"], "scope": d["scope"],
               "status": d["status"], "rationale": d["rationale"], "author_id": d["author_id"],
               "effective_at": d["effective_at"], "expires_at": d.get("expires_at"),
               "supersedes_id": d.get("supersedes_id"), "evidence_ids": ev[:100], "version": version}
        put(uow, "Decision", doc, title=f"{d['rule_type']} decision", status=d["status"], rule_type=d["rule_type"],
            scope_type=d["scope"]["entity_type"], scope_id=d["scope"]["entity_id"], created_at=ctx.now)
        _edge(uow, "APPLIES_TO", nid, d["scope"]["entity_id"], ev)
        if d.get("supersedes_id"):
            _edge(uow, "SUPERSEDES", nid, d["supersedes_id"], ev)
    elif r.etype == "attempt":
        doc = {"id": nid, "workspace_id": ws, "request_id": d["request_id"], "procedure_key": d["procedure_key"],
               "context_signature": d["context_signature"], "scope": d["scope"], "outcome": d["outcome"],
               "verification": d["verification"], "summary": d["summary"], "evidence_ids": ev[:100],
               "observed_at": d["observed_at"]}
        uow.node("Attempt", nid, doc=doc, title=f"Attempt {d['procedure_key']}", request_id=d["request_id"],
                 observed_at=d["observed_at"],
                 scope_type=d["scope"]["entity_type"], scope_id=d["scope"]["entity_id"], version=version)
        _edge(uow, "ATTEMPTED_FOR", nid, d["request_id"], ev)
        if d.get("work_item_id"):
            _edge(uow, "ATTEMPTED_FOR", nid, d["work_item_id"], ev)
