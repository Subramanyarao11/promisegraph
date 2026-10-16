"""Shared domain helpers: request context, immutable sources/evidence, revisions, run events, jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .config import REFERENCE_TIME, Settings
from .store import WORKSPACE_NODE, Store, UnitOfWork
from .util import edge_id, iso, new_id, parse_ts, sha256_hex, utcnow

FRESHNESS_DAYS = 7
STALE_KINDS = {"work", "observation"}
DEFAULT_BUDGET = {"max_tool_calls": 12, "max_replans": 2, "max_active_seconds": 90, "extensions_used": 0}
LIFETIME_MAX_TOOL_CALLS = 24
LIFETIME_MAX_ACTIVE_SECONDS = 180
MAX_JOB_ATTEMPTS = 5
JOB_KIND_PRIORITY = {"reconcile": 0, "dispatch": 1, "investigate": 2, "refresh": 3}
ACTIVE_JOB_STATUSES = ("queued", "leased", "blocked")
TERMINAL_RUN = ("succeeded", "failed", "cancelled")


@dataclass
class Ctx:
    ws: str
    role: str  # "operator" | "worker"
    actor_id: str
    store: Store
    settings: Settings
    correlation_id: str
    now_dt: Any = field(default_factory=utcnow)
    lease: dict[str, Any] | None = None  # worker lease headers on public mutations
    job: dict[str, Any] | None = None  # validated job props when `lease` is present
    idem_key: str = ""
    post_commit: Any = None  # callable run after a successful commit (e.g. local readback verification)
    fault_after_commit: bool = False  # test-profile FAULTS injection

    @property
    def now(self) -> str:
        return iso(self.now_dt)

    @property
    def now_epoch(self) -> float:
        return self.now_dt.timestamp()

    @property
    def ref_now(self) -> str:
        """Scenario clock for freshness/deadlines/decision expiry; audit timestamps use `now`."""
        return REFERENCE_TIME if self.settings.clock_mode == "scenario" else self.now

    def revision(self) -> int:
        return self.store.knowledge_revision(self.ws)


# ---- revisions --------------------------------------------------------------------------------------
def bump_revision(uow: UnitOfWork, current: int) -> int:
    uow.guard_revision(current)
    uow.node("Workspace", WORKSPACE_NODE, knowledge_revision=current + 1)
    return current + 1


# ---- sources and evidence ---------------------------------------------------------------------------
def is_stale(kind: str, observed_at: str, ref_now: str) -> bool:
    if kind not in STALE_KINDS:
        return False
    try:
        return parse_ts(observed_at) < parse_ts(ref_now) - timedelta(days=FRESHNESS_DAYS)
    except ValueError:
        return True


def source_revision(ctx: Ctx, uow: UnitOfWork, source_id: str, *, title: str, kind: str, origin: str, body: str,
                    observed_at: str, excerpts: list[tuple[str, str]] | None = None,
                    head: dict[str, Any] | None = None, head_known: bool = False) -> dict[str, Any]:
    """Append an immutable SourceRecord revision (no-op when the body hash is unchanged).

    `excerpts` = [(excerpt, location)]; default one excerpt of the whole body (<= 4000 chars).
    Returns {source_id, revision, evidence_ids, changed, previous_revision}.
    """
    if not head_known:
        head = ctx.store.get(ctx.ws, source_id, "Source")
    sha = sha256_hex(body)
    if head and head.get("sha256") == sha:
        return {"source_id": source_id, "revision": int(head["latest_revision"]),
                "evidence_ids": list(head.get("latest_evidence_ids") or []), "changed": False,
                "previous_revision": int(head["latest_revision"])}
    prev = int(head["latest_revision"]) if head else None
    rev = (prev or 0) + 1
    title = (title or source_id)[:300]
    record = {"id": source_id, "workspace_id": ctx.ws, "revision": rev, "title": title, "kind": kind,
              "origin": origin, "body": body, "sha256": sha, "observed_at": observed_at,
              "ingested_at": ctx.now, "supersedes_revision": prev}
    rev_node = f"{source_id}@r{rev}"
    if excerpts is None:
        excerpts = [(body[:4000], "body")]
    ev_ids = []
    for i, (excerpt, location) in enumerate(excerpts[:100], start=1):
        ev_id = f"ev_{source_id}_r{rev}_{i}" if len(source_id) < 100 else f"ev_{sha256_hex(source_id)[:24]}_r{rev}_{i}"
        ev_ids.append(ev_id)
        ev = {"id": ev_id, "source_id": source_id, "source_revision": rev, "origin": origin,
              "excerpt": excerpt[:4000], "location": location[:500], "observed_at": observed_at,
              "ingested_at": ctx.now, "stale": False}
        uow.node("Evidence", ev_id, doc=ev, source_id=source_id, source_revision=rev, kind=kind)
        uow.edge("HAS_EVIDENCE", edge_id("HAS_EVIDENCE", rev_node, ev_id), rev_node, ev_id)
    uow.node("SourceRev", rev_node, doc=record, source_id=source_id, revision=rev)
    summary = {k: record[k] for k in ("id", "revision", "title", "kind", "origin", "sha256", "observed_at",
                                      "ingested_at")}
    uow.node("Source", source_id, doc=summary, title=title, kind=kind, origin=origin, latest_revision=rev,
             sha256=sha, observed_at=observed_at, latest_evidence_ids=ev_ids,
             search_text=(title + "\n" + body).lower()[:20000])
    uow.edge("HAS_REVISION", edge_id("HAS_REVISION", source_id, rev_node), source_id, rev_node)
    return {"source_id": source_id, "revision": rev, "evidence_ids": ev_ids, "changed": True,
            "previous_revision": prev}


def evidence_refs(ctx: Ctx, ids: list[str]) -> list[dict[str, Any]]:
    """Resolve EvidenceRefs (stale computed now: superseded revision or freshness policy)."""
    if not ids:
        return []
    rows = ctx.store.read(ctx.ws, "UNWIND $ids AS i MATCH (e:Evidence {id: i}) "
                                  "OPTIONAL MATCH (s:Source {id: e.source_id}) "
                                  "RETURN e.doc, e.kind, s.latest_revision", {"ids": list(dict.fromkeys(ids))})
    by_id = {}
    for doc, kind, latest in rows:
        ev = json.loads(doc)
        ev["stale"] = bool((latest is not None and ev["source_revision"] < int(latest))
                           or is_stale(kind or "", ev["observed_at"], ctx.ref_now))
        by_id[ev["id"]] = ev
    return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]


def missing_evidence(ctx: Ctx, ids: list[str]) -> list[str]:
    if not ids:
        return []
    found = ctx.store.read(ctx.ws, "UNWIND $ids AS i MATCH (e:Evidence {id: i}) RETURN e.id", {"ids": list(set(ids))})
    have = {r[0] for r in found}
    return [i for i in ids if i not in have]


# ---- entities ---------------------------------------------------------------------------------------
def put(uow: UnitOfWork, label: str, doc: dict[str, Any], **props: Any) -> None:
    if "version" in doc:
        props.setdefault("version", doc["version"])
    uow.node(label, doc["id"], doc=doc, **props)


def bump(doc: dict[str, Any], now: str, **changes: Any) -> dict[str, Any]:
    out = dict(doc)
    out.update(changes)
    out["version"] = int(doc["version"]) + 1
    if "updated_at" in doc:
        out["updated_at"] = now
    return out


def request_props(doc: dict[str, Any]) -> dict[str, Any]:
    return {"status": doc["status"], "customer_id": doc["customer_id"], "created_at": doc["created_at"],
            "source_id": doc["source_id"], "search_text": (doc["subject"] + "\n" + doc["body"]).lower()[:20000],
            "title": doc["subject"]}


def put_request(uow: UnitOfWork, doc: dict[str, Any], **x: Any) -> None:
    put(uow, "Request", doc, **request_props(doc), **x)


def put_run(uow: UnitOfWork, doc: dict[str, Any], checkpoint: dict | None = None, **x: Any) -> None:
    props: dict[str, Any] = {"request_id": doc["request_id"], "status": doc["status"], "created_at": doc["created_at"],
                             "title": f"Run {doc['id']}"}
    if checkpoint is not None:
        props["checkpoint"] = json.dumps(checkpoint, sort_keys=True)
    props.update(x)
    put(uow, "Run", doc, **props)


def put_action(uow: UnitOfWork, doc: dict[str, Any], **x: Any) -> None:
    put(uow, "Action", doc, request_id=doc["request_id"], run_id=doc["run_id"], status=doc["status"],
        proposal_id=doc["proposal_id"], created_at=doc["created_at"], title=f"{doc['kind']} action", **x)


def put_work_item(uow: UnitOfWork, doc: dict[str, Any], description: str = "", **x: Any) -> None:
    put(uow, "WorkItem", doc, status=doc["status"], connector=doc["connector"], title=doc["title"],
        description=description, search_text=(doc["title"] + "\n" + description).lower()[:20000], **x)


def new_run_doc(ctx: Ctx, request_id: str, mode: str, revision: int) -> dict[str, Any]:
    return {"id": new_id("run"), "workspace_id": ctx.ws, "request_id": request_id, "mode": mode,
            "status": "queued", "stage": "investigate", "based_on_revision": revision, "event_seq": 0,
            "budget": dict(DEFAULT_BUDGET), "usage": {"tool_calls": 0, "replans": 0, "active_ms": 0},
            "error": None, "created_at": ctx.now, "updated_at": ctx.now, "version": 1}


def err_obj(code: str, message: str, retryable: bool = False, details: dict | None = None) -> dict[str, Any]:
    return {"code": code[:100], "message": (message or code)[:2000], "retryable": retryable,
            "details": details or {}}


# ---- events -----------------------------------------------------------------------------------------
def append_event(ctx: Ctx, uow: UnitOfWork, run: dict[str, Any], etype: str, payload: dict[str, Any],
                 step_id: str | None = None, tool_name: str | None = None) -> tuple[dict, dict]:
    """Append an immutable RunEvent; returns (event, updated run doc). Caller writes the run doc."""
    seq = int(run["event_seq"]) + 1
    ev = {"id": f"evt_{run['id']}_{seq}", "run_id": run["id"], "seq": seq, "type": etype, "occurred_at": ctx.now,
          "step_id": step_id, "tool_name": tool_name, "payload": payload}
    uow.node("RunEvent", ev["id"], doc=ev, run_id=run["id"], seq=seq, step_id=step_id or "")
    run = dict(run)
    run["event_seq"] = seq
    return ev, run


# ---- jobs -------------------------------------------------------------------------------------------
def enqueue_job(ctx: Ctx, uow: UnitOfWork, kind: str, *, run_id: str | None, request_id: str | None,
                action_id: str | None = None, checkpoint: dict | None = None, status: str = "queued",
                blocked_by: str | None = None, delay_s: float = 0.0, attempt: int = 0,
                target: str | None = None) -> str:
    job_id = new_id("job")
    uow.node("Job", job_id, kind=kind, status=status, run_id=run_id or "", request_id=request_id or "",
             action_id=action_id or "", checkpoint=json.dumps(checkpoint) if checkpoint is not None else "",
             attempt=attempt, fencing_token=0, version=0, lease_token="", expires_epoch=0.0, lease_seconds=60,
             due_epoch=ctx.now_epoch + delay_s, seq=ctx.now_epoch, blocked_by=blocked_by or "",
             worker_id="", created_at=ctx.now, target=target or "", title=f"{kind} job")
    return job_id


def active_jobs(ctx: Ctx, run_id: str) -> list[dict[str, Any]]:
    return ctx.store.find(ctx.ws, "Job", {"run_id": run_id, "status": list(ACTIVE_JOB_STATUSES)}, order="seq")


def job_checkpoint(job: dict[str, Any]) -> dict | None:
    raw = job.get("checkpoint")
    return json.loads(raw) if raw else None


def lease_dto(job: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": job["id"], "kind": job["kind"], "run_id": job.get("run_id") or None,
            "action_id": job.get("action_id") or None, "attempt": max(1, int(job.get("attempt") or 1)),
            "lease_token": job["lease_token"], "fencing_token": int(job["fencing_token"]),
            "expires_at": iso_epoch(float(job["expires_epoch"])), "checkpoint": job_checkpoint(job)}


def iso_epoch(epoch: float) -> str:
    from datetime import datetime, timezone
    return iso(datetime.fromtimestamp(epoch, tz=timezone.utc))


def run_checkpoint(run_props: dict[str, Any]) -> dict | None:
    raw = run_props.get("checkpoint")
    return json.loads(raw) if raw else None
