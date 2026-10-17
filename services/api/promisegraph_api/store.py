"""FalkorDB repository.

Design notes (atomicity, verified against FalkorDB v4.20.7 + falkordb==1.8.0):

* Every workspace has its own graph `<DATABASE_GRAPH_PREFIX><workspace_id>`; the id is first checked
  against the server-owned registry (config.Settings.workspaces), never taken raw from callers.
* FalkorDB has no multi-query transactions. A *single* GRAPH.QUERY is atomic: if it raises, its writes are
  rolled back (undo log). Every domain mutation is therefore compiled into ONE query (`UnitOfWork`):
  guards -> edge deletions -> node upserts -> edge merges. Guards raise a runtime division-by-zero when a
  precondition (entity version, job lease/fencing token, knowledge revision, absent idempotency record) does
  not hold, which aborts and rolls back the whole write. All query text is composed only from fixed clause
  templates and server-side allowlists of labels / relationship types; all values are parameters.
* Reads are separate queries and are not snapshot-consistent with each other. Mutations run under one
  in-process writer lock (single API writer, spec §6), so read-validate-write sequences cannot interleave
  with another API write; the in-query guards are a second line of defence (e.g. lease expiry).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import redis
from falkordb import FalkorDB

from .config import Settings
from .errors import ApiError, StoreGuardFailed, unavailable

NODE_LABELS = {
    "Workspace", "Customer", "Team", "Person", "Capability", "WorkItem", "Request", "Source", "SourceRev",
    "Evidence", "Decision", "Attempt", "Observation", "Run", "RunEvent", "Job", "Proposal", "Action",
    "Receipt", "Authorization", "Idem", "ImportPreview", "Draft", "EscalationCase", "Permit", "Completion",
}
# Contract RelationType enum (traversable by context expand) + internal relations.
RELATION_TYPES = ["SUBMITTED", "ABOUT", "IMPLEMENTED_BY", "TRACKED_BY", "DEPENDS_ON", "OWNED_BY", "RELATED_TO",
                  "SUPPORTED_BY", "APPLIES_TO", "SUPERSEDES", "ATTEMPTED_FOR", "OBSERVED_AS", "VERIFIES"]
INTERNAL_REL_TYPES = ["HAS_REVISION", "HAS_EVIDENCE", "MEMBER_OF", "CONSULTED", "EXECUTES", "HAS_RECEIPT",
                      "INVESTIGATES", "HAS_DRAFT", "HAS_CASE", "HAS_REPLY"]
EDGE_TYPES = set(RELATION_TYPES) | set(INTERNAL_REL_TYPES)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")

INDEXES = [("E", "id"), ("Job", "status"), ("RunEvent", "run_id"), ("Proposal", "request_id"),
           ("Action", "request_id"), ("Run", "request_id"), ("Evidence", "source_id"),
           ("SourceRev", "source_id"), ("Decision", "status"), ("Request", "status")]
WORKSPACE_NODE = "workspace"


@dataclass
class UnitOfWork:
    """One atomic graph write. Build it, then `Store.commit(ws, uow)`."""

    nodes: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # label -> id -> props
    edges: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # type -> id -> {from,to,props}
    edge_deletes: list[dict[str, Any]] = field(default_factory=list)  # {node, type, dir, via}
    version_guards: list[dict[str, Any]] = field(default_factory=list)  # {id, version}
    absent_guards: list[str] = field(default_factory=list)
    lease_guard: dict[str, Any] | None = None
    revision_guard: int | None = None

    def node(self, label: str, node_id: str, doc: dict | None = None, **props: Any) -> None:
        assert label in NODE_LABELS, label
        p = self.nodes.setdefault(label, {}).setdefault(node_id, {"label": label})
        if doc is not None:
            p["doc"] = json.dumps(doc, sort_keys=True)
        for k, v in props.items():
            assert _IDENT.match(k), k
            if isinstance(v, (dict,)) or (isinstance(v, list) and v and isinstance(v[0], (dict, list))):
                v = json.dumps(v, sort_keys=True)
            p[k] = v

    def edge(self, rel_type: str, eid: str, from_id: str, to_id: str, **props: Any) -> None:
        assert rel_type in EDGE_TYPES, rel_type
        self.edges.setdefault(rel_type, {})[eid] = {"id": eid, "from": from_id, "to": to_id,
                                                     "props": {"id": eid, **props}}

    def delete_edges(self, node_id: str, rel_type: str, direction: str = "out", via: str | None = None) -> None:
        """Delete `node_id`'s edges of `rel_type` (optionally only those whose `via` provenance matches)."""
        assert rel_type in EDGE_TYPES, rel_type
        assert direction in ("out", "in")
        self.edge_deletes.append({"node": node_id, "type": rel_type, "dir": direction, "via": via})

    def guard_version(self, node_id: str, version: int) -> None:
        self.version_guards.append({"id": node_id, "version": version})

    def guard_absent(self, node_id: str) -> None:
        self.absent_guards.append(node_id)

    def guard_lease(self, job_id: str, lease_token: str, fencing_token: int, now_epoch: float) -> None:
        self.lease_guard = {"id": job_id, "lease_token": lease_token, "fencing_token": fencing_token,
                            "now": now_epoch}

    def guard_revision(self, revision: int) -> None:
        self.revision_guard = revision

    def compile(self) -> tuple[str, dict[str, Any]]:
        parts: list[str] = []
        params: dict[str, Any] = {}
        n = 0

        def nxt() -> str:
            nonlocal n
            n += 1
            return f"_c{n}"

        if self.revision_guard is not None:
            params["rev_guard"] = self.revision_guard
            params["ws_node"] = WORKSPACE_NODE
            parts.append("OPTIONAL MATCH (w:E {id: $ws_node}) "
                         "WITH 1 / (CASE WHEN w IS NOT NULL AND w.knowledge_revision = $rev_guard "
                         f"THEN 1 ELSE 0 END) AS {nxt()}")
        if self.lease_guard is not None:
            params["lease"] = self.lease_guard
            parts.append("OPTIONAL MATCH (lj:E {id: $lease.id}) "
                         "WITH 1 / (CASE WHEN lj IS NOT NULL AND lj.status = 'leased' "
                         "AND lj.lease_token = $lease.lease_token AND lj.fencing_token = $lease.fencing_token "
                         f"AND lj.expires_epoch >= $lease.now THEN 1 ELSE 0 END) AS {nxt()}")
        if self.version_guards:
            params["vguards"] = self.version_guards
            parts.append("UNWIND $vguards AS vg OPTIONAL MATCH (vx:E {id: vg.id}) "
                         "WITH 1 / (CASE WHEN vx IS NOT NULL AND vx.version = vg.version THEN 1 ELSE 0 END) AS g "
                         f"WITH count(g) AS {nxt()}")
        if self.absent_guards:
            params["aguards"] = self.absent_guards
            parts.append("UNWIND $aguards AS ag OPTIONAL MATCH (ax:E {id: ag}) "
                         "WITH 1 / (CASE WHEN ax IS NULL THEN 1 ELSE 0 END) AS g "
                         f"WITH count(g) AS {nxt()}")
        for direction, pattern in (("out", "(da:E {id: d.node})-[dr]->()"), ("in", "()-[dr]->(da:E {id: d.node})")):
            rows = [d for d in self.edge_deletes if d["dir"] == direction]
            if rows:
                params[f"edels_{direction}"] = rows
                parts.append(f"UNWIND $edels_{direction} AS d OPTIONAL MATCH {pattern} "
                             "WHERE type(dr) = d.type AND (d.via IS NULL OR dr.via = d.via) "
                             f"DELETE dr WITH count(*) AS {nxt()}")
        for i, (label, rows) in enumerate(sorted(self.nodes.items())):
            assert label in NODE_LABELS
            key = f"n{i}"
            params[key] = [{"id": nid, "props": {"id": nid, **props}} for nid, props in rows.items()]
            parts.append(f"UNWIND ${key} AS row MERGE (x{i}:E:{label} {{id: row.id}}) SET x{i} += row.props "
                         f"WITH count(*) AS {nxt()}")
        for i, (rel_type, rows) in enumerate(sorted(self.edges.items())):
            assert rel_type in EDGE_TYPES
            key = f"r{i}"
            params[key] = list(rows.values())
            parts.append(f"UNWIND ${key} AS e OPTIONAL MATCH (ea{i}:E {{id: e.from}}) "
                         f"OPTIONAL MATCH (eb{i}:E {{id: e.to}}) "
                         f"WITH e, ea{i}, eb{i}, 1 / (CASE WHEN ea{i} IS NULL OR eb{i} IS NULL THEN 0 ELSE 1 END) AS chk "
                         f"MERGE (ea{i})-[er{i}:{rel_type} {{id: e.id}}]->(eb{i}) SET er{i} += e.props "
                         f"WITH count(*) AS {nxt()}")
        if not parts:
            return "", {}
        return " ".join(parts) + " RETURN 1", params


def _decode(props: dict[str, Any]) -> dict[str, Any]:
    out = dict(props)
    if isinstance(out.get("doc"), str):
        out["doc"] = json.loads(out["doc"])
    return out


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._db: FalkorDB | None = None
        self._schema_ready: set[str] = set()
        self.write_lock = threading.RLock()
        self._conn_lock = threading.Lock()

    # ---- connection -------------------------------------------------------------------------------
    def _client(self) -> FalkorDB:
        with self._conn_lock:
            if self._db is None:
                try:
                    self._db = FalkorDB(host=self.settings.db_host, port=self.settings.db_port,
                                        socket_timeout=5, socket_connect_timeout=2)
                except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError) as exc:
                    raise unavailable(f"Graph database unavailable: {type(exc).__name__}") from exc
            return self._db

    def close(self) -> None:
        """Release pooled connections (called from the app lifespan on shutdown)."""
        with self._conn_lock:
            if self._db is not None:
                try:
                    self._db.connection.close()
                except Exception:  # noqa: BLE001 - shutdown must not fail
                    pass
                self._db = None
                self._schema_ready.clear()

    def graph_name(self, ws: str) -> str:
        if ws not in self.settings.workspaces:  # defensive: callers resolve the workspace first
            raise ApiError(404, "NOT_FOUND", f"workspace {ws} not found")
        return f"{self.settings.graph_prefix}{ws}"

    def ping(self) -> bool:
        try:
            return bool(self._client().connection.ping())
        except ApiError:
            return False
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError):
            return False

    def _run(self, ws: str, query: str, params: dict[str, Any] | None, read_only: bool) -> list[list[Any]]:
        g = self._client().select_graph(self.graph_name(ws))
        try:
            if ws not in self._schema_ready:
                self._ensure_schema(ws, g)
            res = g.ro_query(query, params) if read_only else g.query(query, params)
            return res.result_set
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError,
                redis.exceptions.BusyLoadingError, OSError) as exc:
            raise unavailable(f"Graph database unavailable: {type(exc).__name__}") from exc
        except redis.exceptions.ResponseError as exc:
            if "Division by zero" in str(exc):
                raise StoreGuardFailed(str(exc)) from exc
            raise

    def _ensure_schema(self, ws: str, g: Any) -> None:
        for label, prop in INDEXES:
            try:
                g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except redis.exceptions.ResponseError as exc:
                if "already indexed" not in str(exc).lower():
                    raise
        g.query("MERGE (w:E:Workspace {id: $id}) ON CREATE SET w.knowledge_revision = 0, w.label = 'Workspace'",
                {"id": WORKSPACE_NODE})
        self._schema_ready.add(ws)

    def read(self, ws: str, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        return self._run(ws, query, params, read_only=True)

    def write(self, ws: str, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        return self._run(ws, query, params, read_only=False)

    def commit(self, ws: str, uow: UnitOfWork) -> None:
        q, p = uow.compile()
        if q:
            self.write(ws, q, p)

    def drop_graph(self, ws: str) -> None:
        try:
            self._client().select_graph(self.graph_name(ws)).delete()
        except redis.exceptions.ResponseError:
            pass
        self._schema_ready.discard(ws)

    # ---- generic reads (fixed templates) -----------------------------------------------------------
    def knowledge_revision(self, ws: str) -> int:
        rows = self.read(ws, "MATCH (w:E {id: $id}) RETURN w.knowledge_revision", {"id": WORKSPACE_NODE})
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def get(self, ws: str, node_id: str, label: str | None = None) -> dict[str, Any] | None:
        """Return all node properties (with `doc` JSON-decoded) or None."""
        rows = self.read(ws, "MATCH (n:E {id: $id}) RETURN properties(n)", {"id": node_id})
        if not rows:
            return None
        props = _decode(rows[0][0])
        if label is not None and props.get("label") != label:
            return None
        return props

    def get_doc(self, ws: str, node_id: str, label: str) -> dict[str, Any] | None:
        p = self.get(ws, node_id, label)
        return p["doc"] if p else None

    def get_many(self, ws: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        rows = self.read(ws, "UNWIND $ids AS i MATCH (n:E {id: i}) RETURN properties(n)", {"ids": list(set(ids))})
        return {r[0]["id"]: _decode(r[0]) for r in rows}

    def find(self, ws: str, label: str, filters: dict[str, Any] | None = None, order: str = "id",
             desc: bool = False, skip: int = 0, limit: int | None = None,
             contains: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Equality filters on allowlisted scalar properties (identifiers only; values are parameters)."""
        assert label in NODE_LABELS
        assert _IDENT.match(order)
        clauses, params = [], {}
        for i, (k, v) in enumerate((filters or {}).items()):
            assert _IDENT.match(k)
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                clauses.append(f"n.{k} IN $f{i}")
                params[f"f{i}"] = list(v)
            else:
                clauses.append(f"n.{k} = $f{i}")
                params[f"f{i}"] = v
        for i, (k, v) in enumerate((contains or {}).items()):
            assert _IDENT.match(k)
            clauses.append(f"n.{k} CONTAINS $c{i}")
            params[f"c{i}"] = v
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (f"MATCH (n:{label}){where} RETURN properties(n) ORDER BY n.{order}{' DESC' if desc else ''}, "
             f"n.id{' DESC' if desc else ''}")
        if skip:
            q += " SKIP $skip"
            params["skip"] = skip
        if limit is not None:
            q += " LIMIT $limit"
            params["limit"] = limit
        return [_decode(r[0]) for r in self.read(ws, q, params)]

    def count(self, ws: str, label: str, filters: dict[str, Any] | None = None) -> int:
        assert label in NODE_LABELS
        clauses, params = [], {}
        for i, (k, v) in enumerate((filters or {}).items()):
            assert _IDENT.match(k)
            if v is None:
                continue
            clauses.append(f"n.{k} = $f{i}")
            params[f"f{i}"] = v
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return int(self.read(ws, f"MATCH (n:{label}){where} RETURN count(n)", params)[0][0])

    def out_neighbors(self, ws: str, node_id: str, rel_type: str) -> list[dict[str, Any]]:
        assert rel_type in EDGE_TYPES
        rows = self.read(ws, f"MATCH (:E {{id: $id}})-[:{rel_type}]->(m:E) RETURN properties(m)", {"id": node_id})
        return [_decode(r[0]) for r in rows]

    def in_neighbors(self, ws: str, node_id: str, rel_type: str) -> list[dict[str, Any]]:
        assert rel_type in EDGE_TYPES
        rows = self.read(ws, f"MATCH (m:E)-[:{rel_type}]->(:E {{id: $id}}) RETURN properties(m)", {"id": node_id})
        return [_decode(r[0]) for r in rows]

    def edge_count(self, ws: str, from_id: str, rel_type: str, to_id: str) -> int:
        assert rel_type in EDGE_TYPES
        rows = self.read(ws, f"MATCH (:E {{id: $a}})-[r:{rel_type}]->(:E {{id: $b}}) RETURN count(r)",
                         {"a": from_id, "b": to_id})
        return int(rows[0][0])
