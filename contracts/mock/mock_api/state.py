"""In-memory workspace state plus graph/source primitives. Not a graph database."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .common import (
    FRESHNESS_DAYS, ApiError, bounded_id, canonical, clock_now, new_id, not_found, now, parse_ts, pub, sha256,
)


class Workspace:
    def __init__(self, wid: str, name: str):
        self.id = wid
        self.name = name
        self.revision = 0
        self.auto_commit_kinds = ["answer", "clarify"]
        # knowledge
        self.customers: dict[str, dict] = {}
        self.teams: dict[str, dict] = {}
        self.people: dict[str, dict] = {}
        self.capabilities: dict[str, dict] = {}
        self.work_items: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.sources: dict[str, list[dict]] = {}
        self.evidence: dict[str, dict] = {}
        self.ev_entities: dict[str, set[str]] = defaultdict(set)
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.attempts: dict[str, dict] = {}
        self.import_hashes: dict[tuple[str, str], str] = {}
        # execution
        self.proposals: dict[str, dict] = {}
        self.actions: dict[str, dict] = {}
        self.observations: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = defaultdict(list)
        self.jobs: dict[str, dict] = {}
        self.job_seq = 0
        self.fencing = 0
        self.permits: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.local_effects: dict[str, dict] = {}  # action_id -> effect record
        self.drafts: dict[str, dict] = {}
        self.escalations: dict[str, dict] = {}
        self.idem: dict[tuple, dict] = {}
        self.previews: dict[str, dict] = {}
        self.seeded_files: list[str] = []

    # ------------------------------------------------------------------ revision
    def bump(self) -> int:
        self.revision += 1
        return self.revision

    # ------------------------------------------------------------------ lookups
    def get(self, coll: str, ident: str, kind: str) -> dict:
        obj = getattr(self, coll).get(ident)
        if obj is None:
            raise not_found(kind, ident)
        return obj

    def entity_exists(self, ident: str) -> bool:
        return any(ident in c for c in (self.customers, self.people, self.teams, self.capabilities, self.work_items,
                                        self.requests, self.decisions, self.attempts, self.sources))

    # ------------------------------------------------------------------ sources & evidence
    def add_source(self, sid: str, *, title: str, kind: str, origin: str, body: str, observed_at: str) -> tuple[dict, bool]:
        """Append an immutable revision if the body changed. Returns (record, created_new_revision)."""
        revs = self.sources.setdefault(sid, [])
        digest = sha256(body)
        if revs and revs[-1]["sha256"] == digest:
            return revs[-1], False
        rec = {
            "id": sid, "workspace_id": self.id, "revision": len(revs) + 1, "title": title[:300] or sid,
            "kind": kind, "origin": origin, "body": body, "sha256": digest, "observed_at": observed_at,
            "ingested_at": now(), "supersedes_revision": revs[-1]["revision"] if revs else None,
        }
        revs.append(rec)
        self.nodes.setdefault(sid, {"id": sid, "label": "Source", "title": rec["title"]})
        return rec, True

    def add_evidence(self, src: dict, *, excerpt: str, location: str, entities: list[str] = (), ev_id: str | None = None) -> dict:
        ev_id = ev_id or bounded_id("ev", f"{src['id']}_r{src['revision']}")
        base, n = ev_id, 1
        while ev_id in self.evidence and (self.evidence[ev_id]["source_id"], self.evidence[ev_id]["source_revision"], self.evidence[ev_id]["location"]) != (src["id"], src["revision"], location):
            n += 1
            ev_id = bounded_id("ev", f"{base}_{n}")[:128]
        ev = {
            "id": ev_id, "source_id": src["id"], "source_revision": src["revision"], "origin": src["origin"],
            "excerpt": (excerpt or src["body"])[:4000] or src["title"], "location": location[:500],
            "observed_at": src["observed_at"], "ingested_at": src["ingested_at"], "stale": False,
            "_kind": src["kind"],
        }
        self.evidence[ev_id] = ev
        for e in entities:
            self.ev_entities[ev_id].add(e)
        return ev

    def is_stale(self, observed_at: str, kind: str) -> bool:
        if kind not in ("work", "observation"):
            return False
        return parse_ts(observed_at) < clock_now() - timedelta(days=FRESHNESS_DAYS)

    def evidence_view(self, ev_id: str) -> dict | None:
        ev = self.evidence.get(ev_id)
        if not ev:
            return None
        out = pub(ev)
        out["stale"] = self.is_stale(ev["observed_at"], ev["_kind"])
        return out

    def source_summary(self, sid: str) -> dict:
        rec = self.sources[sid][-1]
        return {
            "id": rec["id"], "revision": rec["revision"], "title": rec["title"], "kind": rec["kind"],
            "origin": rec["origin"], "sha256": rec["sha256"], "observed_at": rec["observed_at"],
            "ingested_at": rec["ingested_at"], "stale": self.is_stale(rec["observed_at"], rec["kind"]),
        }

    # ------------------------------------------------------------------ graph
    def add_node(self, ident: str, label: str, title: str) -> None:
        self.nodes[ident] = {"id": ident, "label": label[:60], "title": (title or ident)[:300]}

    def add_edge(self, etype: str, from_id: str, to_id: str, evidence_ids: list[str], *, confirmed: bool = True,
                 asserted_by: str | None = None, edge_id: str | None = None) -> dict | None:
        if from_id not in self.nodes or to_id not in self.nodes:
            return None
        eid = edge_id or "edge_" + sha256(f"{etype}|{from_id}|{to_id}")[:16]
        edge = self.edges.get(eid)
        if edge:
            for e in evidence_ids:
                if e not in edge["evidence_ids"]:
                    edge["evidence_ids"].append(e)
        else:
            edge = {"id": eid, "type": etype, "from_id": from_id, "to_id": to_id,
                    "evidence_ids": list(dict.fromkeys(evidence_ids)), "confirmed": confirmed, "_by": asserted_by}
            self.edges[eid] = edge
        for e in evidence_ids:
            self.ev_entities[e].update((from_id, to_id))
        return edge

    def drop_edges_by(self, asserted_by: str) -> None:
        for eid in [k for k, v in self.edges.items() if v.get("_by") == asserted_by]:
            del self.edges[eid]

    # ------------------------------------------------------------------ proposals
    def consulted(self, proposal: dict) -> set[str]:
        return proposal.get("_consulted", set())

    def invalidate_proposals(self, entity_ids: set[str], reason: str) -> int:
        n = 0
        for p in self.proposals.values():
            if p["validity"] != "valid":
                continue
            action = next((a for a in self.actions.values() if a["proposal_id"] == p["id"]), None)
            if action and action["status"] not in ("proposed", "accepted"):
                continue
            if self.consulted(p) & entity_ids:
                p["validity"] = "stale"
                p["validation_reasons"] = (p["validation_reasons"] + [reason[:1000]])[-30:]
                p["version"] += 1
                n += 1
        return n


def touch(obj: dict) -> dict:
    obj["version"] += 1
    if "updated_at" in obj:
        obj["updated_at"] = now()
    return obj


def must(cond: bool, status: int, code: str, message: str, details: dict | None = None) -> None:
    if not cond:
        raise ApiError(status, code, message, details)


def canonical_hash(obj) -> str:
    return sha256(canonical(obj))


def new_job_id() -> str:
    return new_id("job")
