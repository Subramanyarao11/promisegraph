"""Mock context retrieval: keyword search, bounded BFS expansion over an in-memory edge list, ContextBundle.

This is NOT a graph database and makes no relevance claims beyond simple token overlap.
"""

from __future__ import annotations

import re

from .common import RELATION_TYPES, clock_now, parse_ts, pub, sha256
from .state import Workspace

TOKEN = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "we", "our", "it", "be", "with", "not"}
BUNDLE_RELATIONS = [r for r in RELATION_TYPES if r != "SUBMITTED"]


def tokens(text: str) -> set[str]:
    return {t for t in TOKEN.findall(text.lower()) if t not in STOP}


def _evidence_for(ws: Workspace, entity_id: str, limit: int = 3) -> list[dict]:
    out = []
    for ev_id, ents in ws.ev_entities.items():
        if entity_id in ents:
            v = ws.evidence_view(ev_id)
            if v:
                out.append(v)
        if len(out) >= limit:
            break
    return out


def search(ws: Workspace, q: dict) -> dict:
    qt = tokens(q["query"])
    cust = q["customer_id"]
    cands = []

    def consider(etype, ident, title, text):
        if not qt:
            return
        hit = qt & tokens(text)
        if hit:
            cands.append({"entity_type": etype, "id": ident, "title": (title or ident)[:300],
                          "score": round(len(hit) / len(qt), 4), "evidence": _evidence_for(ws, ident)})

    types = set(q["entity_types"])
    if "request" in types:
        for r in ws.requests.values():
            if cust is None or r["customer_id"] == cust:
                consider("request", r["id"], r["subject"], f"{r['subject']} {r['body']}")
    if "capability" in types:
        for c in ws.capabilities.values():
            if cust is None or cust in c.get("_customer_ids", []):
                consider("capability", c["id"], c["name"], " ".join([c["name"], *c["aliases"]]))
    if "work_item" in types:
        for w in ws.work_items.values():
            consider("work_item", w["id"], w["title"], f"{w['title']} {w.get('_description', '')}")
    if "source" in types:
        for sid, revs in ws.sources.items():
            rec = revs[-1]
            consider("source", sid, rec["title"], f"{rec['title']} {rec['body']}")
    cands.sort(key=lambda c: (-c["score"], c["entity_type"], c["id"]))
    lim = q["limit"]
    return {"candidates": cands[:lim], "truncated": len(cands) > lim}


def _incident(ws: Workspace, node: str, rels: set[str]):
    for e in sorted(ws.edges.values(), key=lambda e: e["id"]):
        if e["type"] in rels and (e["from_id"] == node or e["to_id"] == node):
            yield e, (e["to_id"] if e["from_id"] == node else e["from_id"])


def bfs(ws: Workspace, starts: list[str], rels: list[str], depth: int):
    """Undirected bounded BFS with a visited set. Returns (parents, order, frontier_has_more)."""
    rs = set(rels)
    parent: dict[str, tuple[str, dict] | None] = {s: None for s in starts}
    order: list[str] = []
    frontier = list(starts)
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for e, m in _incident(ws, n, rs):
                if m in parent:
                    continue
                parent[m] = (n, e)
                order.append(m)
                nxt.append(m)
        frontier = nxt
        if not frontier:
            break
    more = any(m not in parent for n in frontier for _, m in _incident(ws, n, rs))
    return parent, order, more


def build_path(ws: Workspace, parent, target: str) -> dict:
    nodes, edges = [target], []
    cur = target
    while parent[cur] is not None:
        prev, e = parent[cur]
        edges.append(e)
        nodes.append(prev)
        cur = prev
    nodes.reverse()
    edges.reverse()
    ev = list(dict.fromkeys(x for e in edges for x in e["evidence_ids"]))
    claim = "source_assertion" if all(e["confirmed"] and e["evidence_ids"] for e in edges) else "inference"
    return {
        "id": "path_" + sha256("|".join(nodes) + "#" + "|".join(e["id"] for e in edges))[:16],
        "nodes": [dict(ws.nodes[n]) for n in nodes],
        "edges": [pub(e) for e in edges],
        "claim_kind": claim,
        "evidence_ids": ev[:100],
    }


def expand(ws: Workspace, q: dict) -> dict:
    parent, order, more = bfs(ws, q["entity_ids"], q["relation_types"], q["depth"])
    lim = q["limit"]
    paths = [build_path(ws, parent, n) for n in order[:lim]]
    visited = list(parent)
    nodes = [dict(ws.nodes[n]) for n in visited[:200]]
    return {"paths": paths, "nodes": nodes, "truncated": more or len(order) > lim or len(visited) > 200,
            "visited_count": len(visited)}


def _decision_active(d: dict) -> bool:
    if d["status"] != "active":
        return False
    now = clock_now()
    if parse_ts(d["effective_at"]) > now:
        return False
    return d["expires_at"] is None or parse_ts(d["expires_at"]) > now


def bundle(ws: Workspace, req: dict, depth: int = 4, offset: int = 0) -> dict:
    parent, order, more = bfs(ws, [req["id"]], BUNDLE_RELATIONS, depth)
    visited = set(parent)
    truncated = more
    # paths to work items / people / decisions / attempts (prefix paths included); paged by cursor offset
    targets = [n for n in order if ws.nodes[n]["label"] in ("WorkItem", "Person", "Decision", "Attempt")]
    page = targets[offset:offset + 20]
    if len(targets) > offset + 20:
        truncated = True
    paths = [build_path(ws, parent, n) for n in page]

    ev_ids: list[str] = []
    for p in paths:
        ev_ids.extend(p["evidence_ids"])
    for n in [req["id"], *order]:
        for ev_id, ents in ws.ev_entities.items():
            if n in ents:
                ev_ids.append(ev_id)
    rsrc = ws.sources.get(req["source_id"])
    ev_ids = list(dict.fromkeys(ev_ids))
    sources = [v for v in (ws.evidence_view(e) for e in ev_ids) if v]
    if len(sources) > 100:
        truncated = True
    sources = sources[:100]

    work_ids = list(dict.fromkeys([*req["linked_work_ids"], *[n for n in order if n in ws.work_items]]))
    related = [pub(ws.work_items[w]) for w in work_ids if w in ws.work_items]
    if len(related) > 30:
        truncated = True
    related = related[:30]

    scope_ids = visited | {req["customer_id"], *req["capability_ids"]}
    decisions = [pub(d) for d in ws.decisions.values() if _decision_active(d) and d["scope"]["entity_id"] in scope_ids][:30]
    attempts = [pub(a) for a in ws.attempts.values()
                if a["request_id"] == req["id"] or a["scope"]["entity_id"] in scope_ids or a["id"] in visited][:30]

    missing = []
    if rsrc is None:
        missing.append("Request source record is missing.")
    if not req["capability_ids"]:
        missing.append("Request is not linked to a confirmed capability.")
    for c in req["capability_ids"]:
        if not any(e["type"] == "IMPLEMENTED_BY" and e["from_id"] == c for e in ws.edges.values()):
            missing.append(f"No tracked work found for capability {c}.")
    for w in related:
        if w["status"] in ("open", "in_progress", "blocked") and w["forecast_at"] is None:
            missing.append(f"No reliable delivery forecast is recorded for {w['id']}.")
        if w["owner_id"] is None:
            missing.append(f"No owner is recorded for {w['id']}.")
        elif w["owner_id"] in ws.people and not ws.people[w["owner_id"]].get("active", True):
            missing.append(f"Owner {w['owner_id']} of {w['id']} is inactive.")
    return {
        "request_id": req["id"], "based_on_revision": ws.revision, "sources": sources, "paths": paths,
        "related_work": related, "decisions": decisions, "attempts": attempts,
        "missing_information": [m[:1000] for m in dict.fromkeys(missing)][:30], "truncated": truncated,
    }
