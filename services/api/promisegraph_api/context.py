"""Search followed by bounded graph expansion (spec §6 "Graph boundaries", §7 "Search followed by graph expansion").

* Traversal depth: default 4, allowlisted maximum 6. Relationship types come only from the contract
  RelationType allowlist; the query text is composed from that allowlist and the validated integer depth.
* Cycles: variable-length matching uses relationship-unique trails and a hard depth bound; paths that revisit a
  node are discarded and nodes are de-duplicated with a visited set.
* Every result reports truncation; a truncated result never claims complete coverage.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .core import Ctx, evidence_refs, is_stale
from .errors import not_found
from .store import RELATION_TYPES, _decode
from .util import sha256_hex

DEFAULT_DEPTH = 4
MAX_DEPTH = 6
ROW_CAP = 2000
INTERNAL_LABELS = {"Idem", "Job", "Permit", "ImportPreview", "Completion", "Authorization", "Workspace", "RunEvent",
                   "SourceRev", "Evidence", "Receipt", "Draft"}
BUNDLE_RELATIONS = ["ABOUT", "IMPLEMENTED_BY", "TRACKED_BY", "DEPENDS_ON", "OWNED_BY"]
STOPWORDS = {"the", "and", "for", "with", "our", "are", "was", "not", "but", "you", "your", "have", "has", "this",
             "that", "from", "they", "them", "their", "will", "can", "cannot", "need", "needs", "please", "what",
             "when", "where", "which", "who", "why", "how", "any", "all", "its", "into", "about", "been", "were",
             "there", "here", "does", "did", "doing", "working", "stopped", "before", "after", "still"}
CANDIDATE_CAPS = 3
CANDIDATE_MIN_SCORE = 0.3
SEARCH_LABEL = {"request": "Request", "capability": "Capability", "work_item": "WorkItem", "source": "Source"}


def tokens(query: str) -> list[str]:
    out: list[str] = []
    for w in re.split(r"[^a-z0-9]+", query.lower()):
        if len(w) < 3 or w in STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        if w not in out:
            out.append(w)
    return out[:20] or [query.lower().strip()[:100]]


def search(ctx: Ctx, cmd: dict[str, Any]) -> dict[str, Any]:
    toks = tokens(cmd["query"])
    limit = cmd["limit"]
    cust = cmd.get("customer_id")
    cands: list[dict[str, Any]] = []
    total = 0
    for et in dict.fromkeys(cmd["entity_types"]):
        label = SEARCH_LABEL[et]
        # Fixed per-label templates; tokens/customer are parameters.
        if label == "Request":
            q = ("MATCH (n:Request) WHERE any(t IN $toks WHERE n.search_text CONTAINS t) "
                 "AND ($cust IS NULL OR n.customer_id = $cust) RETURN n.id, n.title, n.search_text LIMIT 500")
        elif label == "Capability":
            q = ("MATCH (n:Capability) WHERE any(t IN $toks WHERE n.search_text CONTAINS t) "
                 "RETURN n.id, n.title, n.search_text LIMIT 500")
        elif label == "WorkItem":
            q = ("MATCH (n:WorkItem) WHERE any(t IN $toks WHERE n.search_text CONTAINS t) "
                 "RETURN n.id, n.title, n.search_text LIMIT 500")
        else:
            q = ("MATCH (n:Source) WHERE any(t IN $toks WHERE n.search_text CONTAINS t) "
                 "RETURN n.id, n.title, n.search_text LIMIT 500")
        rows = ctx.store.read(ctx.ws, q, {"toks": toks, "cust": cust})
        for nid, title, text in rows:
            text = text or ""
            title_l = (title or "").lower()
            hits = sum(1 for t in toks if t in text)
            score = hits / len(toks) + (0.25 if all(t in title_l for t in toks) else 0.0)
            cands.append({"entity_type": et, "id": nid, "title": (title or nid)[:300], "score": round(score, 4)})
            total += 1
    cands.sort(key=lambda c: (-c["score"], c["entity_type"], c["id"]))
    selected = cands[:limit]
    for c in selected:
        c["evidence"] = evidence_refs(ctx, _entity_evidence_ids(ctx, c["entity_type"], c["id"]))[:10]
    return {"candidates": selected, "truncated": total > limit}


def _entity_evidence_ids(ctx: Ctx, et: str, nid: str) -> list[str]:
    if et == "source":
        rows = ctx.store.read(ctx.ws, "MATCH (s:Source {id: $id}) RETURN s.latest_evidence_ids", {"id": nid})
    else:
        rows = ctx.store.read(ctx.ws, "MATCH (:E {id: $id})-[:SUPPORTED_BY]->(s:Source) RETURN s.latest_evidence_ids",
                              {"id": nid})
    out: list[str] = []
    for (ids,) in rows:
        out.extend(ids or [])
    return out[:10]


# ---- traversal --------------------------------------------------------------------------------------
def _traverse(ctx: Ctx, start_id: str, rel_types: list[str], depth: int, directed: bool) -> tuple[list[tuple], bool]:
    assert all(t in RELATION_TYPES for t in rel_types)
    depth = max(1, min(int(depth), MAX_DEPTH))
    rel = "|".join(rel_types)
    arrow = "->" if directed else "-"
    q = (f"MATCH p=(s:E {{id: $id}})-[:{rel}*1..{depth}]{arrow}(t:E) "
         "RETURN [n IN nodes(p) | [n.id, n.label, n.title]], "
         "[r IN relationships(p) | [r.id, type(r), startNode(r).id, endNode(r).id, r.evidence_ids, r.confirmed]] "
         "LIMIT $cap")
    rows = ctx.store.read(ctx.ws, q, {"id": start_id, "cap": ROW_CAP})
    return rows, len(rows) >= ROW_CAP


def _node_dto(n: list) -> dict[str, Any]:
    nid, label, title = n
    return {"id": nid, "label": (label or "Entity")[:60], "title": (title or nid)[:300]}


def _path_dto(nodes: list, rels: list) -> dict[str, Any]:
    edges = []
    ev: list[str] = []
    for rid, rtype, a, b, evids, confirmed in rels:
        evids = list(evids or [])[:100]
        edges.append({"id": rid or ("e_" + sha256_hex(f"{rtype}|{a}|{b}")[:32]), "type": rtype, "from_id": a,
                      "to_id": b, "evidence_ids": evids, "confirmed": bool(confirmed) if confirmed is not None else True})
        ev.extend(evids)
    if any(not e["confirmed"] for e in edges):
        kind = "inference"
    elif all(e["evidence_ids"] for e in edges):
        kind = "source_assertion"
    else:
        kind = "derived"
    ids = [n[0] for n in nodes]
    return {"id": "path_" + sha256_hex("|".join(ids) + "#" + "|".join(e["id"] for e in edges))[:24],
            "nodes": [_node_dto(n) for n in nodes], "edges": edges, "claim_kind": kind,
            "evidence_ids": list(dict.fromkeys(ev))[:100]}


def current_evidence(ctx: Ctx, ids: list[str]) -> dict[str, list[str]]:
    """Map evidence ids to the current revision's evidence of the same source (identity when current)."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    rows = ctx.store.read(ctx.ws, "UNWIND $ids AS i MATCH (e:Evidence {id: i}) OPTIONAL MATCH (s:Source {id: e.source_id}) "
                                  "RETURN e.id, e.source_revision, s.latest_revision, s.latest_evidence_ids", {"ids": ids})
    out = {i: [i] for i in ids}
    for eid, rev, latest, latest_ids in rows:
        if latest is not None and int(rev) < int(latest) and latest_ids:
            out[eid] = list(latest_ids)
    return out


def refresh_evidence(ctx: Ctx, paths: list[dict], edges: list[dict] | None = None) -> None:
    """D4: a current edge must cite current-revision evidence, never only a superseded revision."""
    all_edges = [e for p in paths for e in p["edges"]] + list(edges or [])
    m = current_evidence(ctx, [i for e in all_edges for i in e["evidence_ids"]])
    for e in all_edges:
        e["evidence_ids"] = list(dict.fromkeys(j for i in e["evidence_ids"] for j in m.get(i, [i])))[:100]
    for p in paths:
        p["evidence_ids"] = list(dict.fromkeys(i for e in p["edges"] for i in e["evidence_ids"]))[:100]


def _maximal(rows: list[tuple]) -> tuple[list[tuple], set[str]]:
    """Drop paths revisiting a node; keep only paths that are not a prefix of another kept path."""
    simple = []
    visited: set[str] = set()
    for nodes, rels in rows:
        ids = tuple(n[0] for n in nodes)
        if len(set(ids)) != len(ids):
            continue
        visited.update(ids)
        simple.append((ids, nodes, rels))
    prefixes = set()
    for ids, _n, _r in simple:
        for k in range(1, len(ids)):
            prefixes.add(ids[:k])
    seen = set()
    out = []
    for ids, nodes, rels in simple:
        if ids in prefixes or ids in seen:
            continue
        seen.add(ids)
        out.append((nodes, rels))
    rank = {"Person": 0, "Team": 0, "WorkItem": 1, "Capability": 2, "Request": 3}
    out.sort(key=lambda p: (-len(p[0]), rank.get(p[0][-1][1], 5), [n[0] for n in p[0]]))
    return out, visited


def expand(ctx: Ctx, cmd: dict[str, Any]) -> dict[str, Any]:
    ids = list(dict.fromkeys(cmd["entity_ids"]))
    found = ctx.store.get_many(ctx.ws, ids)
    for i in ids:
        if i not in found or found[i].get("label") in INTERNAL_LABELS:
            raise not_found("entity", i)
    rel_types = list(dict.fromkeys(cmd["relation_types"]))
    limit = cmd["limit"]
    all_rows: list[tuple] = []
    truncated = False
    for i in ids:
        rows, capped = _traverse(ctx, i, rel_types, cmd["depth"], directed=False)
        all_rows.extend(rows)
        truncated = truncated or capped
    maximal, visited = _maximal(all_rows)
    truncation = "limit" if truncated else None
    if len(maximal) > limit:
        truncated, truncation = True, "limit"
    paths = [_path_dto(n, r) for n, r in maximal[:limit]]
    nodes: dict[str, dict] = {}
    for i in ids:
        p = found[i]
        nodes[i] = {"id": i, "label": p.get("label", "Entity"), "title": (p.get("title") or i)[:300]}
    for p in paths:
        for n in p["nodes"]:
            nodes.setdefault(n["id"], n)
    node_list = list(nodes.values())
    if len(node_list) > 200:
        truncated, truncation = True, "limit"
        node_list = node_list[:200]
    node_ids = [n["id"] for n in node_list]
    # all allowlisted edges among the returned nodes (shared-neighbour edges included), real direction
    erows = ctx.store.read(ctx.ws, "MATCH (a:E)-[r]->(b:E) WHERE a.id IN $ids AND b.id IN $ids AND type(r) IN $types "
                                   "RETURN r.id, type(r), a.id, b.id, r.evidence_ids, r.confirmed ORDER BY r.id LIMIT 501",
                           {"ids": node_ids, "types": rel_types})
    if len(erows) > 500:
        truncated, truncation = True, "limit"
    edges = [_path_dto([[a, "E", a], [b, "E", b]], [row])["edges"][0] for row in erows[:500] for a, b in [(row[2], row[3])]]
    refresh_evidence(ctx, paths, edges)
    if truncation is None:
        # depth bound: does any node reached at the maximum depth have an unexplored allowlisted neighbour?
        frontier = list({n[-1][0] for n, _r in maximal if len(n) - 1 >= cmd["depth"]})
        if frontier:
            hit = ctx.store.read(ctx.ws, "MATCH (a:E)-[r]-(b:E) WHERE a.id IN $f AND type(r) IN $types "
                                         "AND NOT b.id IN $seen RETURN count(r) > 0",
                                 {"f": frontier, "types": rel_types, "seen": list(visited | set(ids))})
            if hit and hit[0][0]:
                truncated, truncation = True, "depth"
    return {"paths": paths, "nodes": node_list, "edges": edges, "truncated": truncated, "truncation": truncation,
            "visited_count": len(visited | set(ids))}


# ---- decisions / attempts (memory) -----------------------------------------------------------------
def effective_decision(doc: dict[str, Any], ref_now: str) -> dict[str, Any]:
    """Expiry is evaluated against the (scenario) clock; history is preserved, status is reported as expired."""
    d = dict(doc)
    if d["status"] == "active" and d.get("expires_at") and d["expires_at"] <= ref_now:
        d["status"] = "expired"
    return d


def decisions_for_scopes(ctx: Ctx, scopes: list[tuple[str, str]], only_active: bool = True) -> list[dict[str, Any]]:
    if not scopes:
        return []
    rows = ctx.store.read(ctx.ws, "UNWIND $scopes AS sc MATCH (d:Decision) WHERE d.scope_type = sc[0] AND d.scope_id = sc[1] "
                                  "RETURN DISTINCT d.doc", {"scopes": [list(s) for s in scopes]})
    out = []
    for (doc,) in rows:
        d = effective_decision(json.loads(doc), ctx.ref_now)
        if only_active and (d["status"] != "active" or d["effective_at"] > ctx.ref_now):
            continue
        out.append(d)
    out.sort(key=lambda d: (d["effective_at"], d["id"]))
    return out


def attempts_for(ctx: Ctx, request_id: str, scopes: list[tuple[str, str]], work_ids: list[str] | None = None
                 ) -> list[dict[str, Any]]:
    rows = ctx.store.read(ctx.ws, "MATCH (a:Attempt) WHERE a.request_id = $rid OR "
                                  "any(sc IN $scopes WHERE a.scope_type = sc[0] AND a.scope_id = sc[1]) "
                                  "RETURN a.doc", {"rid": request_id, "scopes": [list(s) for s in scopes]})
    out_map = {d["id"]: d for d in (json.loads(r[0]) for r in rows)}
    if work_ids:
        # attempts made for the same work items (e.g. on an earlier request), but only within an applicable scope:
        # a customer- or request-scoped attempt of ANOTHER customer never leaks (T09)
        cust = next((sc[1] for sc in scopes if sc[0] == "customer"), None)
        wrows = ctx.store.read(ctx.ws, "MATCH (a:Attempt)-[:ATTEMPTED_FOR]->(w:WorkItem) WHERE w.id IN $w "
                                       "OPTIONAL MATCH (r:Request {id: a.scope_id}) RETURN DISTINCT a.doc, r.customer_id",
                               {"w": work_ids})
        for doc, scope_req_customer in wrows:
            d = json.loads(doc)
            st, sid = d["scope"]["entity_type"], d["scope"]["entity_id"]
            if st == "customer" and sid != cust:
                continue
            if st == "request" and scope_req_customer != cust:
                continue
            out_map.setdefault(d["id"], d)
    out = list(out_map.values())
    out.sort(key=lambda a: (a["observed_at"], a["id"]), reverse=True)
    return out


def conflicting(decisions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_rule: dict[str, list[dict]] = {}
    for d in decisions:
        by_rule.setdefault(d["rule_type"], []).append(d)
    out = []
    for rule, ds in by_rule.items():
        values = {json.dumps(d["value"], sort_keys=True) for d in ds}
        if rule != "forbidden_workaround" and len(values) > 1:
            out.append(ds)
    return out


def request_scopes(req: dict[str, Any], work_ids: list[str] | None = None) -> list[tuple[str, str]]:
    sc = [("request", req["id"]), ("customer", req["customer_id"])]
    sc += [("capability", c) for c in req.get("capability_ids") or []]
    sc += [("work_item", w) for w in (work_ids or [])]
    return list(dict.fromkeys(sc))


# ---- bundle -----------------------------------------------------------------------------------------
def bundle(ctx: Ctx, request_id: str, depth: int) -> dict[str, Any]:
    reqp = ctx.store.get(ctx.ws, request_id, "Request")
    if not reqp:
        raise not_found("request", request_id)
    req = reqp["doc"]
    revision = ctx.revision()
    rows, truncated = _traverse(ctx, request_id, BUNDLE_RELATIONS, depth, directed=True)
    maximal, _visited = _maximal(rows)
    rel_rows = ctx.store.read(ctx.ws, "MATCH p=(s:E {id: $id})-[:RELATED_TO]-(t:Request) "
                                      "RETURN [n IN nodes(p) | [n.id, n.label, n.title]], "
                                      "[r IN relationships(p) | [r.id, type(r), startNode(r).id, endNode(r).id, "
                                      "r.evidence_ids, r.confirmed]] LIMIT 20", {"id": request_id})
    path_rows = maximal + [tuple(r) for r in rel_rows]
    candidates: list[dict[str, Any]] = []
    src0 = ctx.store.get(ctx.ws, req["source_id"], "Source")
    req_ev = list(src0.get("latest_evidence_ids") or []) if src0 else []
    if not req.get("capability_ids"):
        # D5: bounded, UNCONFIRMED candidate context from the request text; never persisted (T28)
        found = search(ctx, {"query": (req["subject"] + " " + req["body"])[:500], "entity_types": ["capability"],
                             "customer_id": None, "limit": CANDIDATE_CAPS})["candidates"]
        candidates = [c for c in found if c["score"] >= CANDIDATE_MIN_SCORE]
        for c in candidates:
            crow, ccap = _traverse(ctx, c["id"], ["IMPLEMENTED_BY", "DEPENDS_ON", "OWNED_BY"], max(1, depth - 1),
                                   directed=True)
            truncated = truncated or ccap
            cmax, _ = _maximal(crow) if crow else ([([[c["id"], "Capability", c["title"]]], [])], set())
            head_node = [request_id, "Request", req["subject"]]
            cand_edge = ["cand_" + sha256_hex(f"ABOUT|{request_id}|{c['id']}")[:30], "ABOUT", request_id, c["id"],
                         req_ev, False]
            for nodes, rels in cmax:
                path_rows.append(([head_node] + list(nodes), [cand_edge] + list(rels)))
    if len(path_rows) > 20:
        truncated = True
    paths = [_path_dto(n, r) for n, r in path_rows[:20]]
    refresh_evidence(ctx, paths)
    work_ids: list[str] = []
    for nodes, _r in path_rows:
        for nid, label, _t in nodes:
            if label == "WorkItem" and nid not in work_ids:
                work_ids.append(nid)
    for w in req.get("linked_work_ids") or []:
        if w not in work_ids:
            work_ids.append(w)
    wprops = ctx.store.get_many(ctx.ws, work_ids)
    related_work = [wprops[w]["doc"] for w in work_ids if w in wprops][:30]
    if len(work_ids) > 30:
        truncated = True
    scopes = request_scopes(req, [w["id"] for w in related_work])
    decisions = decisions_for_scopes(ctx, scopes)
    attempts = attempts_for(ctx, request_id, scopes, [w["id"] for w in related_work])
    ev_ids: list[str] = []
    src = ctx.store.get(ctx.ws, req["source_id"], "Source")
    if src:
        ev_ids += list(src.get("latest_evidence_ids") or [])
    for p in paths:
        ev_ids += p["evidence_ids"]
    for w in related_work:
        ev_ids += w["evidence_ids"]
    for d in decisions:
        ev_ids += d["evidence_ids"]
    for a in attempts:
        ev_ids += a["evidence_ids"]
    ev_ids = list(dict.fromkeys(ev_ids))
    if len(ev_ids) > 100:
        truncated = True
    sources = evidence_refs(ctx, ev_ids[:100])
    missing: list[str] = []
    if not req.get("capability_ids"):
        if candidates:
            missing.append("No capability is confirmed for this request; candidate capabilities inferred from the "
                           "request text (unconfirmed): " + ", ".join(c["id"] for c in candidates) + ".")
        else:
            missing.append("No confirmed capability is linked to this request, and no candidate capability matched.")
    if not related_work:
        missing.append(f"No tracked or implementing work found within {depth} hops.")
    owner_ids = [w["owner_id"] for w in related_work if w.get("owner_id")]
    owners = ctx.store.get_many(ctx.ws, owner_ids)
    for w in related_work:
        if not w.get("owner_id"):
            missing.append(f"Work item {w['id']} has no owner.")
        elif w["owner_id"] in owners and owners[w["owner_id"]].get("active") is False:
            missing.append(f"Owner {w['owner_id']} of work item {w['id']} is inactive.")
        if is_stale("work", w["observed_at"], ctx.ref_now):
            missing.append(f"Work item {w['id']} status observation is stale (observed {w['observed_at']}).")
    for group in conflicting(decisions):
        missing.append("Conflicting active decisions for " + group[0]["rule_type"] + ": "
                       + ", ".join(d["id"] for d in group) + "; clarify or escalate.")
    if truncated:
        missing.append("Context was truncated; coverage is incomplete.")
    return {"request_id": request_id, "based_on_revision": revision, "sources": sources, "paths": paths,
            "related_work": related_work, "decisions": decisions[:30], "attempts": attempts[:30],
            "missing_information": [m[:1000] for m in missing][:30], "truncated": truncated}


def node_titles(ctx: Ctx, ids: list[str]) -> dict[str, dict]:
    return {k: _decode(v) for k, v in ctx.store.get_many(ctx.ws, ids).items()}
