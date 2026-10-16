"""Deterministic fact extraction from tool results.

Everything here is derived from data the tools returned (context bundle, decisions, attempts, search
candidates, graph paths, source records). No scenario ids, fixture names or customer names are consulted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

OPEN_WORK = {"open", "in_progress", "blocked"}
ACTION_KINDS = {"answer", "clarify", "link_work", "create_work", "escalate"}
STALE_AFTER = timedelta(days=7)
DIRECT_TIE = 0.9  # request words match two capabilities about equally ...
AGGREGATE_TIE = 0.8  # ... and no other evidence separates them
WORK_RELEVANCE = 0.6  # a work candidate must score >= 60% of the best work match to count as compatible
MIN_CAPABILITY_SCORE = 0.03

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set(
    "a an and are as at be but by can could do does for from has have how i in is it its of on or our please so that the "
    "their them there these they this to up us was we what when where which who why will with would you your about into "
    "than then also any all just need needs want wants get got like yes no not".split()
)
_NEG_WHOLE = ("not enough", "not sufficient", "isn't enough", "is not enough", "cannot", "can't", "can not", "not supported",
              "unsupported", "does not work", "doesn't work", "won't work", "not an option", "not available", "does not support",
              "doesn't support", "is not possible", "not possible")
_NEG_AFTER = ("instead of", "rather than", "other than")
_CAPABILITY_GAP_CUES = ("not supported", "unsupported", "not available", "does not support", "doesn't support", "no support",
                        "isn't supported", "not yet supported")
_GAP_CUES = ("not supported", "unsupported", "not available", "does not support", "doesn't support", "is not possible",
             "not possible", "cannot", "can't", "no support", "not yet supported", "isn't supported")


def stem(tok: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(tok) > len(suffix) + 3 and tok.endswith(suffix):
            return tok[: -len(suffix)]
    return tok


def tokens(text: str | None) -> set[str]:
    return {stem(t) for t in _TOKEN.findall(str(text or "").lower()) if t not in _STOP and len(t) > 1}


def _clauses(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", str(text or "").lower())
    return [c for c in re.split(r"[.;!?]+|,\s*but\s+|\s+but\s+|\n#+", flat) if c.strip()]


def negated_tokens(text: str | None) -> set[str]:
    """Tokens the requester explicitly rules out ("a CSV download is not enough", "instead of email")."""
    out: set[str] = set()
    for clause in _clauses(text or ""):
        for cue in _NEG_AFTER:
            if cue in clause:
                out |= tokens(clause.split(cue, 1)[1])
        if any(cue in clause for cue in _NEG_WHOLE):
            out |= tokens(clause)
    return out


def gap_clauses(text: str | None) -> list[str]:
    """Clauses in documentation that state something is not supported/available."""
    return [c.strip() for c in _clauses(text or "") if any(cue in c for cue in _GAP_CUES)]


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _label(node: dict[str, Any] | None) -> str:
    return re.sub(r"[^a-z]", "", str((node or {}).get("label", "")).lower())


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, bool):
        return [str(value).lower()]
    return [str(value)] if value not in (None, "") else []


@dataclass
class Facts:
    request: dict[str, Any]
    reference_time: datetime
    bundle: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    work: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    paths: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    known_evidence_ids: list[str] = field(default_factory=list)
    truncated: dict[str, bool] = field(default_factory=dict)
    missing_information: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    reply_text: str = ""
    prior_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    unrefreshed: list[str] = field(default_factory=list)
    complete_neighbourhood: dict[str, bool] = field(default_factory=dict)
    rejected_evidence: set[str] = field(default_factory=set)
    expanded_targets: set[str] = field(default_factory=set)
    edges_complete: bool = False
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)  # validation reasons from refused proposals (replan input)

    # -- ingestion ---------------------------------------------------------------------------------
    @classmethod
    def new(cls, request: dict[str, Any], reference_time: str | None) -> "Facts":
        return cls(request=request, reference_time=_ts(reference_time) or datetime.now(timezone.utc))

    def _record(self, ref: dict[str, Any]) -> None:
        """Structured import evidence: location `$.<collection>[i]`, excerpt = canonical record JSON (PROTOCOL.md)."""
        loc = str(ref.get("location") or "")
        if not loc.startswith("$."):
            return
        try:
            rec = json.loads(ref.get("excerpt") or "")
        except ValueError:
            return  # truncated/quoted field: not a full record
        if not isinstance(rec, dict) or not rec.get("id"):
            return
        coll = loc[2:].split("[", 1)[0]
        ev = [ref["id"]]

        def edge(etype: str, a: str, b: str) -> None:
            eid = f"rec:{etype}:{a}:{b}"
            if eid not in self.edges and not any(e.get("type") == etype and e.get("from_id") == a and e.get("to_id") == b for e in self.edges.values()):
                self.edges[eid] = {"id": eid, "type": etype, "from_id": a, "to_id": b, "evidence_ids": ev, "confirmed": True}

        if coll == "work_items":
            wid = rec["id"]
            if wid not in self.work or self.work[wid].get("_from_record"):
                self.work[wid] = {
                    "id": wid, "title": rec.get("title", wid), "kind": rec.get("kind"), "status": rec.get("status"), "owner_id": rec.get("owner_id"),
                    "connector": rec.get("connector", "local"), "external_id": rec.get("external_id"), "forecast_at": rec.get("forecast_at"),
                    "observed_at": rec.get("observed_at") or ref.get("observed_at"), "evidence_ids": ev, "_from_record": True, "_stale_ref": ref.get("stale"),
                }
            for cap in rec.get("capability_ids") or []:
                edge("IMPLEMENTED_BY", cap, wid)
                self.nodes.setdefault(cap, {"id": cap, "label": "Capability", "title": cap})
            for dep in rec.get("depends_on") or []:
                edge("DEPENDS_ON", wid, dep)
            if rec.get("owner_id"):
                edge("OWNED_BY", wid, rec["owner_id"])
            self.nodes.setdefault(wid, {"id": wid, "label": "WorkItem", "title": rec.get("title", wid)})
        elif coll == "attempts" and rec.get("procedure_key") and rec.get("scope"):
            self.attempts.setdefault(rec["id"], {**rec, "evidence_ids": rec.get("evidence_ids") or ev})
        elif coll == "requests":
            rid = rec["id"]
            for cap in rec.get("capability_ids") or []:
                edge("ABOUT", rid, cap)
                self.nodes.setdefault(cap, {"id": cap, "label": "Capability", "title": cap})
            for wid in rec.get("linked_work_ids") or []:
                edge("TRACKED_BY", rid, wid)
            self.prior_requests[rid] = rec

    def _ev(self, ids: Iterable[Any]) -> None:
        for i in ids or []:
            if isinstance(i, str) and i not in self.known_evidence_ids:
                self.known_evidence_ids.append(i)

    def ingest(self, name: str, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.errors.append({"tool": name, **(result.get("error") or {})})
            return
        data = result.get("data")
        if result.get("meta", {}).get("truncated_by_worker"):
            self.truncated[f"{name}:worker"] = True
        if name == "read_memory":
            ctx = (data or {}).get("context") or {}
            self.bundle = ctx
            self.truncated["context"] = bool(ctx.get("truncated"))
            self.missing_information = list(ctx.get("missing_information") or [])
            for ref in ctx.get("sources") or []:
                self.evidence[ref["id"]] = ref
                self._ev([ref["id"]])
                self._record(ref)
            for w in ctx.get("related_work") or []:
                self.work[w["id"]] = w  # authoritative current state beats record excerpts
                self._ev(w.get("evidence_ids"))
            for d in list(ctx.get("decisions") or []) + list((data or {}).get("decisions") or []):
                self.decisions[d["id"]] = d
                self._ev(d.get("evidence_ids"))
            for a in (data or {}).get("attempts") or []:
                self.attempts[a["id"]] = a
                self._ev(a.get("evidence_ids"))
            for person in (data or {}).get("people") or []:
                self.people[person["id"]] = person
            for err in (data or {}).get("work_refresh_errors") or []:
                self.unrefreshed.append(err["work_item_id"])
            for w in (data or {}).get("work_items") or []:
                self.work[w["id"]] = w  # refreshed authoritative state
                self._ev(w.get("evidence_ids"))
            for a in ctx.get("attempts") or []:
                self.attempts[a["id"]] = a
                self._ev(a.get("evidence_ids"))
            self._paths(ctx.get("paths") or [])
        elif name == "search_context":
            self.truncated["search"] = bool((data or {}).get("truncated")) or self.truncated.get("search", False)
            for c in (data or {}).get("candidates") or []:
                if all(c["id"] != x["id"] for x in self.candidates):
                    self.candidates.append(c)
                for ref in c.get("evidence") or []:
                    self.evidence[ref["id"]] = ref
                    self._ev([ref["id"]])
                    self._record(ref)
        elif name == "expand_context":
            self.truncated["expand"] = bool((data or {}).get("truncated")) or self.truncated.get("expand", False)
            for n in (data or {}).get("nodes") or []:
                self.nodes[n["id"]] = n
            self._paths((data or {}).get("paths") or [])
            if isinstance((data or {}).get("edges"), list):  # Rev 5 #30: every allowlisted edge among visited nodes
                self.edges_complete = True
                for e in data["edges"]:
                    self.edges[e["id"]] = e
                    self._ev(e.get("evidence_ids"))
            if (data or {}).get("truncation") == "limit":
                self.truncated["expand_limit"] = True
        elif name == "read_evidence":
            if isinstance(data, dict) and data.get("id"):
                self.sources[data["id"]] = data

    def _paths(self, paths: list[dict[str, Any]]) -> None:
        for p in paths:
            self.paths.append(p)
            self._ev(p.get("evidence_ids"))
            for n in p.get("nodes") or []:
                self.nodes.setdefault(n["id"], n)
            for e in p.get("edges") or []:
                self.edges[e["id"]] = e
                self._ev(e.get("evidence_ids"))

    # -- graph helpers -----------------------------------------------------------------------------
    def neighbors(self, entity_id: str, edge_type: str | None = None, *, confirmed_only: bool = False) -> list[str]:
        out: list[str] = []
        for e in self.edges.values():
            if edge_type and e.get("type") != edge_type:
                continue
            if confirmed_only and not e.get("confirmed", False):
                continue
            if e.get("from_id") == entity_id:
                out.append(e["to_id"])
            elif e.get("to_id") == entity_id:
                out.append(e["from_id"])
        return out

    def edge_evidence(self, a: str, b: str) -> list[str]:
        ids: list[str] = []
        for e in self.edges.values():
            if {e.get("from_id"), e.get("to_id")} == {a, b}:
                ids.extend(e.get("evidence_ids") or [])
        return ids

    def label_of(self, entity_id: str) -> str:
        if entity_id in self.work:
            return "workitem"
        node = self.nodes.get(entity_id)
        if node:
            return _label(node)
        for c in self.candidates:
            if c["id"] == entity_id:
                return {"work_item": "workitem"}.get(c["entity_type"], c["entity_type"])
        return ""

    def title_of(self, entity_id: str) -> str:
        if entity_id in self.work:
            return self.work[entity_id].get("title", entity_id)
        if entity_id in self.nodes:
            return self.nodes[entity_id].get("title", entity_id)
        for c in self.candidates:
            if c["id"] == entity_id:
                return c.get("title", entity_id)
        return entity_id

    # -- capabilities --------------------------------------------------------------------------------
    def capability_candidates(self) -> list[dict[str, Any]]:
        return sorted((c for c in self.candidates if c["entity_type"] == "capability"), key=lambda c: -float(c.get("score", 0)))

    def score_of(self, entity_id: str) -> float:
        return max((float(c.get("score", 0)) for c in self.candidates if c["id"] == entity_id), default=0.0)

    def reports_problem(self) -> bool:
        text = f"{self.request.get('subject', '')} {self.request.get('body', '')} {self.reply_text}".lower()
        return bool(re.search(r"\b(fail\w*|stopped|broken|break\w*|error\w*|skipp?\w*|missing|lost|garbl\w*|not (working|arriving|running)|"
                              r"did not run|didn't run|doesn't work|does not work|outage|again|still)\b", text))

    def near_duplicates(self) -> set[str]:
        """Candidates whose text is (almost) this request's own text: earlier copies of the same request."""
        mine = tokens(f"{self.request.get('subject', '')} {self.request.get('body', '')}")
        if len(mine) < 3:
            return set()
        out = set()
        for c in self.candidates:
            if c["entity_type"] not in ("request", "source"):
                continue
            text = c.get("title", "") + " " + " ".join(ev.get("excerpt", "") for ev in c.get("evidence") or [])
            if len(mine & tokens(text)) >= 0.9 * len(mine):
                out.add(c["id"])
        return out

    def request_is_question(self) -> bool:
        text = f"{self.request.get('subject', '')} {self.request.get('body', '')} {self.reply_text}".lower()
        return "?" in text or bool(re.search(r"\b(how (do|can|should|to)|what (is|are|should)|can (we|i|you)|is (it|there|this))\b", text))

    def request_text(self) -> str:
        return f"{self.request.get('subject', '')} {self.request.get('body', '')} {self.reply_text}"

    def capability_signals(self) -> dict[str, dict[str, float]]:
        """Per-capability evidence: direct match, best supporting doc/source, best implementing work, best similar
        prior request (via ABOUT). Each signal is the max candidate score reaching the capability over a graph edge."""
        own = {self.request["id"], self.request.get("source_id")}
        signals: dict[str, dict[str, float]] = {}

        def bump(cap: str, kind: str, score: float) -> None:
            if self.label_of(cap) != "capability" and not any(c["id"] == cap and c["entity_type"] == "capability" for c in self.candidates):
                return
            sig = signals.setdefault(cap, {"direct": 0.0, "doc": 0.0, "work": 0.0, "request": 0.0})
            sig[kind] = max(sig[kind], score)

        for c in self.candidates:
            score = float(c.get("score", 0))
            if c["id"] in own:
                continue
            if c["entity_type"] == "capability":
                bump(c["id"], "direct", score)
            elif c["entity_type"] == "source":
                for cap in self.neighbors(c["id"], "SUPPORTED_BY"):
                    bump(cap, "doc", score)
            elif c["entity_type"] == "work_item":
                for cap in self.neighbors(c["id"], "IMPLEMENTED_BY"):
                    bump(cap, "work", score)
            elif c["entity_type"] == "request":
                for cap in self.neighbors(c["id"], "ABOUT"):
                    bump(cap, "request", score)
        return signals

    @staticmethod
    def aggregate(sig: dict[str, float]) -> float:
        return sig["direct"] + 0.5 * (sig["doc"] + sig["work"] + sig["request"])

    def capability_ranking(self) -> list[tuple[str, float, dict[str, float]]]:
        sigs = self.capability_signals()
        ruled_out = negated_tokens(self.request_text())
        sigs = {c: s for c, s in sigs.items() if not (tokens(self.title_of(c)) & ruled_out)}
        # Ties break on the request's own words (direct match), never on id spelling.
        return sorted(((c, self.aggregate(s), s) for c, s in sigs.items()), key=lambda t: (-round(t[1], 2), -t[2]["direct"], -t[1], t[0]))

    def resolve_capabilities(self) -> tuple[list[str], bool, list[dict[str, Any]]]:
        """Returns (capability_ids, ambiguous, competing_candidates).

        Confirmed links win. Otherwise the request is ambiguous when its own words match two capabilities about
        equally (direct scores tie) and graph evidence (docs, work, similar requests) does not separate them.
        """
        confirmed = list(self.request.get("capability_ids") or [])
        for cap in self.neighbors(self.request["id"], "ABOUT", confirmed_only=True):
            if cap not in confirmed:
                confirmed.append(cap)
        if confirmed:
            return confirmed, False, []
        ranking = [r for r in self.capability_ranking() if r[1] >= MIN_CAPABILITY_SCORE]
        if not ranking:
            return [], False, []
        top_id, top_score, top_sig = ranking[0]
        competing = [top_id]
        for cap, score, sig in ranking[1:]:
            direct_tie = top_sig["direct"] > 0 and sig["direct"] >= DIRECT_TIE * top_sig["direct"] and top_sig["direct"] >= DIRECT_TIE * sig["direct"]
            if direct_tie and score >= AGGREGATE_TIE * top_score:
                competing.append(cap)
        if len(competing) >= 2:
            by_id = {c["id"]: c for c in self.candidates if c["entity_type"] == "capability"}
            return [], True, [by_id.get(c, {"id": c, "title": self.title_of(c), "evidence": []}) for c in competing[:4]]
        return [top_id], False, []

    # -- time ------------------------------------------------------------------------------------------
    def age_days(self, observed_at: str | None) -> float | None:
        ts = _ts(observed_at)
        if ts is None:
            return None
        return (self.reference_time - ts).total_seconds() / 86400

    def is_stale(self, observed_at: str | None) -> bool:
        ts = _ts(observed_at)
        return ts is not None and self.reference_time - ts > STALE_AFTER

    # -- decisions -------------------------------------------------------------------------------------
    def scope_applies(self, scope: dict[str, Any], caps: list[str], work_ids: Iterable[str] = ()) -> bool:
        et, eid = scope.get("entity_type"), scope.get("entity_id")
        if et == "customer":
            return eid == self.request.get("customer_id")
        if et == "request":
            return eid == self.request.get("id")
        if et == "capability":
            return eid in caps
        if et == "work_item":
            return eid in set(work_ids) or eid in (self.request.get("linked_work_ids") or [])
        return False

    def applicable_decisions(self, caps: list[str], work_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        work_ids = list(work_ids)
        out = []
        for d in self.decisions.values():
            if d.get("status") != "active":
                continue
            eff, exp = _ts(d.get("effective_at")), _ts(d.get("expires_at"))
            if eff and eff > self.reference_time:
                continue
            if exp and exp <= self.reference_time:
                continue
            if self.scope_applies(d.get("scope") or {}, caps, work_ids):
                out.append(d)
        return out

    @staticmethod
    def conflicts(decisions: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
        """Incompatible active constraints: the planner may not quietly choose one."""
        found = []
        by_type: dict[str, list[dict[str, Any]]] = {}
        for d in decisions:
            by_type.setdefault(d["rule_type"], []).append(d)
        for rule, ds in by_type.items():
            for i, a in enumerate(ds):
                for b in ds[i + 1 :]:
                    va, vb = a.get("value"), b.get("value")
                    if rule in ("routing_owner", "scope_reduction_allowed", "date_extension_allowed") and va != vb:
                        found.append((a, b, f"{rule}: {va!r} vs {vb!r}"))
                    elif rule == "allowed_action_types" and not (set(_values(va)) & set(_values(vb))):
                        found.append((a, b, f"allowed_action_types have no common kind: {va!r} vs {vb!r}"))
        return found

    @staticmethod
    def forbidden_values(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for d in decisions:
            if d["rule_type"] == "forbidden_workaround":
                for v in _values(d.get("value")):
                    out[v] = d
        return out

    @staticmethod
    def allowed_kinds(decisions: list[dict[str, Any]]) -> set[str] | None:
        sets = [set(_values(d.get("value"))) for d in decisions if d["rule_type"] == "allowed_action_types"]
        if not sets:
            return None
        allowed = sets[0]
        for s in sets[1:]:
            allowed &= s
        return allowed

    @staticmethod
    def routing_owner(decisions: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
        owners = [d for d in decisions if d["rule_type"] == "routing_owner" and isinstance(d.get("value"), str) and d["value"]]
        if len({d["value"] for d in owners}) == 1:
            return owners[0]["value"], owners[0]
        return None, None

    # -- attempts ----------------------------------------------------------------------------------
    def attempt_applies(self, a: dict[str, Any], caps: list[str], work_ids: Iterable[str] = ()) -> bool:
        if not self.scope_applies(a.get("scope") or {}, caps, work_ids):
            return False
        if a.get("procedure_key") in ACTION_KINDS:
            # Outcome of verifying an earlier *action* (not a reusable procedure): it bears on this request only if
            # it was this request or a similar prior request, never on every request of the same customer.
            rid = a.get("request_id")
            if rid == self.request["id"] or rid in self.near_duplicates():
                return True
            return any(c["id"] == rid and c["entity_type"] == "request" and float(c.get("score", 0)) >= 0.5 for c in self.candidates)
        # Context signature `<capability>:<context>`: a different capability context does not carry over.
        sig = str(a.get("context_signature") or "")
        head = sig.split(":", 1)[0]
        if head and caps and head not in caps and self.label_of(head) == "capability":
            return False
        return True

    def failed_attempts(self, caps: list[str], work_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        return [
            a
            for a in self.attempts.values()
            if (a.get("outcome") == "failed" or a.get("verification") == "failed") and self.attempt_applies(a, caps, work_ids)
        ]

    def verified_resolutions(self, caps: list[str], work_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        return [
            a
            for a in self.attempts.values()
            if a.get("outcome") == "succeeded" and a.get("verification") == "passed" and self.attempt_applies(a, caps, work_ids)
        ]

    # -- people ------------------------------------------------------------------------------------
    def inactive_people(self) -> set[str]:
        out: set[str] = {pid for pid, p in self.people.items() if p.get("active") is False}  # Rev 5 #33, authoritative
        for text in self.missing_information + self.rejections:
            if "inactive" in text.lower() or "no longer" in text.lower() or "left" in text.lower().split():
                out.update(re.findall(r"\b(?:person|team)_[A-Za-z0-9_\-]+", text))
        for n in self.nodes.values():
            if n["id"] in self.people:
                continue
            if _label(n) == "person" and re.search(r"\binactive\b|\bformer\b|\bdeparted\b", str(n.get("title", "")), re.I):
                out.add(n["id"])
        return out

    def owners_of(self, entity_id: str) -> list[str]:
        owners: list[str] = []
        w = self.work.get(entity_id)
        if w and w.get("owner_id"):
            owners.append(w["owner_id"])
        for o in self.neighbors(entity_id, "OWNED_BY"):
            if o not in owners and self.label_of(o) in ("person", "team", ""):
                owners.append(o)
        return owners

    def prerequisites(self, work_id: str, max_depth: int = 6) -> list[str]:
        """Transitive DEPENDS_ON prerequisites (dependent -> prerequisite)."""
        out: list[str] = []
        frontier = [work_id]
        for _ in range(max_depth):
            nxt = []
            for wid in frontier:
                for e in self.edges.values():
                    if e.get("type") == "DEPENDS_ON" and e.get("from_id") == wid and e["to_id"] not in out and e["to_id"] != work_id:
                        out.append(e["to_id"])
                        nxt.append(e["to_id"])
            frontier = nxt
            if not frontier:
                break
        return out

    # -- work ----------------------------------------------------------------------------------------
    def excluded_by_request(self, entity_id: str, caps: list[str]) -> set[str]:
        """Title tokens of the entity that the requester explicitly ruled out (minus capability-generic words)."""
        generic: set[str] = set()
        for c in caps:
            generic |= tokens(self.title_of(c))
        return (tokens(self.title_of(entity_id)) - generic) & (negated_tokens(self.request_text()) - generic)

    def generic_tokens(self, caps: list[str]) -> set[str]:
        out: set[str] = set()
        for c in caps:
            out |= tokens(self.title_of(c))
        return out

    def distinctive_overlap(self, entity_id: str, caps: list[str], *, title_only: bool = False) -> set[str]:
        """Request words shared with the entity's title (and evidence excerpts), excluding capability-generic words."""
        text = self.title_of(entity_id)
        if not title_only:
            text += " " + " ".join(ev.get("excerpt", "") for c in self.candidates if c["id"] == entity_id for ev in c.get("evidence") or [])
        return (tokens(text) & tokens(self.request_text())) - self.generic_tokens(caps)

    def similar_request_boost(self, work_id: str) -> tuple[float, str | None]:
        """Best similar prior request (search candidate, not this one) already tracked by this work."""
        best, rid = 0.0, None
        for c in self.candidates:
            if c["entity_type"] != "request" or c["id"] == self.request["id"]:
                continue
            if work_id not in self.neighbors(c["id"], "TRACKED_BY") or float(c.get("score", 0)) <= best:
                continue
            caps_of_req = [x for x in self.neighbors(c["id"], "ABOUT")]
            # Following a similar request to its work only makes sense for a reported malfunction, not a new want.
            if self.reports_problem() and len(self.distinctive_overlap(c["id"], caps_of_req)) >= 1:
                best, rid = float(c.get("score", 0)), c["id"]
        return best, rid

    def work_relevance(self, work_id: str, caps: list[str]) -> float:
        boost, _ = self.similar_request_boost(work_id)
        return max(self.score_of(work_id), boost) + 0.03 * min(len(self.distinctive_overlap(work_id, caps)), 5)

    def gap_need(self, caps: list[str]) -> set[str]:
        """Request words that documentation explicitly says are unsupported (e.g. 'chat'): the unmet need."""
        want = tokens(self.request_text()) - self.generic_tokens(caps)
        need: set[str] = set()
        for _sid, clause in self.doc_gaps(caps):
            if any(cue in clause for cue in _CAPABILITY_GAP_CUES):  # "not available/supported", not procedural "cannot"
                need |= tokens(clause) & want
        return need

    def forbidden_phrase_hit(self, text: str, forbidden: Iterable[str]) -> bool:
        """True when text advocates a forbidden workaround: it carries at least half (min 2) of the value's words."""
        words = tokens(text)
        labelled_workaround = "workaround" in text.lower()
        for value in forbidden:
            vt = tokens(str(value).replace("_", " "))
            if vt and len(vt & words) >= max(2, (len(vt) + 1) // 2):
                return True
            if vt and labelled_workaround and vt & words:  # "temporary workaround: create a share link"
                return True
        return False

    def work_for(self, caps: list[str]) -> tuple[list[str], list[str]]:
        """(compatible work ids ranked by relevance, similar-but-incompatible work ids).

        Compatible: already tracked by this request, or implementing a resolved capability, not explicitly ruled out by
        the requester, and either sharing a distinctive title word with the request or already tracking a similar
        prior request. Shared capability vocabulary alone ("scheduled exports") is not a match.
        """
        linked = list(self.request.get("linked_work_ids") or [])
        for wid in self.neighbors(self.request["id"], "TRACKED_BY"):
            if wid not in linked:
                linked.append(wid)
        pool: list[str] = [c["id"] for c in self.candidates if c["entity_type"] == "work_item"]
        for c in self.candidates:
            if c["entity_type"] == "request" and c["id"] != self.request["id"]:
                pool += [w for w in self.neighbors(c["id"], "TRACKED_BY") if w not in pool]
        compatible: list[str] = list(linked)
        similar: list[str] = []
        for wid in pool:
            if wid in compatible or wid in similar:
                continue
            implements = any(cap in caps for cap in self.neighbors(wid, "IMPLEMENTED_BY"))
            boost, _ = self.similar_request_boost(wid)
            matched = bool(self.distinctive_overlap(wid, caps, title_only=True)) or boost > 0
            # When documentation states the requested thing is unsupported, only work that addresses that gap counts.
            need = self.gap_need(caps)
            addresses_gap = not need or bool(tokens(self.title_of(wid)) & need)
            if implements and matched and addresses_gap and not self.excluded_by_request(wid, caps):
                compatible.append(wid)
            elif self.score_of(wid) > 0 or boost > 0:
                similar.append(wid)
        compatible[len(linked):] = sorted(compatible[len(linked):], key=lambda w: -self.work_relevance(w, caps))
        similar.sort(key=lambda w: -max(self.score_of(w), self.similar_request_boost(w)[0]))
        return compatible, similar

    def caps_of_work(self, work_id: str) -> list[str]:
        return [c for c in self.neighbors(work_id, "IMPLEMENTED_BY") if self.label_of(c) == "capability"]

    # -- documentation -------------------------------------------------------------------------------
    def is_record_source(self, source_id: str) -> bool:
        """Structured import records (`$.collection[i]` locations) are not documentation (PROTOCOL import rules)."""
        rec = self.sources.get(source_id)
        if rec is not None:
            return rec.get("kind") != "documentation"
        locs = [str(r.get("location") or "") for r in self.evidence.values() if r.get("source_id") == source_id]
        return bool(locs) and all(loc.startswith("$.") for loc in locs)

    def doc_sources(self, caps: list[str]) -> list[tuple[str, float]]:
        """Documentation sources supporting a resolved capability, ranked by match score (source id, score)."""
        out: dict[str, float] = {}
        for c in self.candidates:
            if c["entity_type"] != "source" or c["id"] in (self.request.get("source_id"),):
                continue
            if not any(cap in self.neighbors(c["id"], "SUPPORTED_BY") for cap in caps):
                continue
            rec = self.sources.get(c["id"])
            if rec is not None and rec.get("kind") != "documentation":
                continue
            out[c["id"]] = max(out.get(c["id"], 0.0), float(c.get("score", 0)))
        for cap in caps:
            for nid in self.neighbors(cap, "SUPPORTED_BY"):
                if self.label_of(nid) == "source" and nid not in out:
                    rec = self.sources.get(nid)
                    if rec is None or rec.get("kind") == "documentation":
                        out[nid] = self.score_of(nid)
        out = {sid: sc for sid, sc in out.items() if not self.is_record_source(sid)}
        return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

    def doc_relevance(self, source_id: str, score: float, caps: list[str]) -> float:
        text = " ".join(r.get("excerpt", "") for r in self.evidence.values() if r.get("source_id") == source_id)
        overlap = (tokens(text) & tokens(self.request_text())) - self.generic_tokens(caps)
        return score + 0.03 * min(len(overlap), 5)

    def doc_evidence(self, caps: list[str]) -> list[dict[str, Any]]:
        """EvidenceRefs (actually returned by a tool) from documentation supporting the capabilities, best first."""
        order = [sid for sid, _ in self.doc_sources(caps)]
        refs = [ref for ref in self.evidence.values() if ref.get("source_id") in order]
        return sorted(refs, key=lambda r: order.index(r["source_id"]))

    def doc_gaps(self, caps: list[str]) -> list[tuple[str, str]]:
        """(source_id, clause) where supporting documentation states that what the requester asks for is unsupported."""
        generic: set[str] = set()
        for c in caps:
            generic |= tokens(self.title_of(c))
        want = tokens(self.request_text()) - generic
        found = []
        for sid, _ in self.doc_sources(caps):
            texts = [r.get("excerpt", "") for r in self.evidence.values() if r.get("source_id") == sid]
            if sid in self.sources:
                texts.append(self.sources[sid].get("body", ""))
            for clause in gap_clauses(" ".join(texts)):
                if tokens(clause) & want:
                    found.append((sid, clause))
        return found

    def doc_source_ids(self, caps: list[str]) -> list[str]:
        return [sid for sid, _ in self.doc_sources(caps)]

    def note_rejection(self, reasons: Iterable[str]) -> None:
        """Validation reasons that name evidence ids: never cite those again in a replan."""
        for reason in reasons:
            self.rejections.append(reason)
            for eid in self.known_evidence_ids:
                if eid in reason:
                    self.rejected_evidence.add(eid)

    def valid_evidence(self, ids: Iterable[str]) -> list[str]:
        """Only ids that a tool actually returned (the API validates them again), minus ids a validation rejected."""
        out: list[str] = []
        for i in ids:
            if i in self.known_evidence_ids and i not in out and i not in self.rejected_evidence:
                out.append(i)
        return out[:100]

    def coverage_notes(self) -> list[str]:
        notes = []
        if self.truncated.get("context"):
            notes.append("request context was truncated")
        if self.truncated.get("expand_limit"):
            notes.append("graph expansion hit its result limit")
        if any(k.endswith(":worker") for k in self.truncated if self.truncated[k]):
            notes.append("a tool result exceeded the 24 KB context bound and was trimmed")
        for err in self.errors:
            notes.append(f"{err.get('tool')} failed ({err.get('code')})")
        if self.unrefreshed:
            notes.append(f"current state of {', '.join(self.unrefreshed[:3])} could not be refreshed")
        return notes

    @property
    def coverage_complete(self) -> bool:
        return not self.coverage_notes()
