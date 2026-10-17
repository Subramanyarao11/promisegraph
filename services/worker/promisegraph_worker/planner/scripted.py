"""ScriptedPlanner — "Scripted mode".

A deterministic stand-in for the model planner. It emits model-shaped tool calls into the real tool loop and
chooses a disposition only from facts the tools returned: capabilities and their candidates, graph paths,
work state and owners, active scoped decisions, scoped attempts, documentation evidence and coverage flags.
It never reads scenario ids, fixture names or expected outcomes, and it cannot bypass the tools.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .base import Outcome, PlanContext, PlanGenerator
from ..markers import intent_marker
from .facts import OPEN_WORK, Facts, tokens
from .wording import KIND_WORDS, Words, day

LABEL = "Scripted mode"
EXPAND_RELATIONS = ["ABOUT", "IMPLEMENTED_BY", "TRACKED_BY", "DEPENDS_ON", "OWNED_BY", "SUPPORTED_BY", "APPLIES_TO", "ATTEMPTED_FOR"]
REVALIDATION_CODES = {"REVISION_CONFLICT", "EVIDENCE_INVALID", "POLICY_VIOLATION", "INVALID_INPUT", "INVALID_TRANSITION"}


@dataclass
class Draft:
    kind: str
    payload: dict[str, Any]
    evidence_ids: list[str]
    explanation: str
    rejected_alternatives: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def arguments(self, revision: int) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": self.payload,
            "evidence_ids": self.evidence_ids,
            "explanation": self.explanation[:6000],
            "rejected_alternatives": self.rejected_alternatives[:5],
            "based_on_revision": revision,
        }


def _alt(kind: str, reason: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {"kind": kind, "reason": reason[:2000], "evidence_ids": list(evidence_ids or [])[:100]}


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class ScriptedPlanner:
    mode = "scripted"
    label = LABEL

    # -- entry ---------------------------------------------------------------------------------------
    def plan(self, ctx: PlanContext) -> PlanGenerator:
        if ctx.resume_stage in ("observe", "verify"):
            return (yield from self._observe(ctx))
        return (yield from self._investigate(ctx))

    @staticmethod
    def _call(ctx: PlanContext, name: str, arguments: dict[str, Any]):
        return (yield {"call_id": ctx.new_call_id(), "name": name, "arguments": arguments})

    @staticmethod
    def _tool_failure(name: str, result: dict[str, Any], stage: str = "investigate") -> Outcome:
        err = result.get("error") or {}
        retryable = bool(err.get("retryable")) or err.get("code") in ("DEPENDENCY_UNAVAILABLE", "RATE_LIMITED", "TIMEOUT")
        return Outcome(
            completion="retryable_failure" if retryable else "failed",
            stage=stage,
            run_status_hint="queued" if retryable else "failed",
            summary=f"{LABEL}: {name} failed with {err.get('code')}; stopping without a proposal.",
            error={"code": err.get("code", "TOOL_FAILED"), "message": _clip(err.get("message", "tool failed"), 500), "tool": name},
        )

    # -- investigation -------------------------------------------------------------------------------
    def _read_memory(self, ctx: PlanContext, facts: Facts, caps: list[str], work_ids: list[str] | None = None, person_ids: list[str] | None = None):
        req = ctx.request
        scopes = [{"entity_type": "customer", "entity_id": req["customer_id"]}, {"entity_type": "request", "entity_id": req["id"]}]
        scopes += [{"entity_type": "capability", "entity_id": c} for c in caps[:4]]
        # Attempts scoped to similar prior requests this run discovered (their outcomes inform this plan).
        similar_reqs = [c["id"] for c in sorted(facts.candidates, key=lambda c: -float(c.get("score", 0)))
                        if c["entity_type"] == "request" and c["id"] != req["id"] and c["id"] not in facts.near_duplicates()][:2]
        scopes += [{"entity_type": "request", "entity_id": r} for r in similar_reqs]
        args: dict[str, Any] = {"scopes": scopes[:8]}
        if work_ids:
            args["work_item_ids"] = work_ids[:5]
        if person_ids:
            args["person_ids"] = person_ids[:5]
        result = yield from self._call(ctx, "read_memory", args)
        facts.ingest("read_memory", result)
        return result

    def _investigate(self, ctx: PlanContext) -> PlanGenerator:
        req = ctx.request
        facts = Facts.new(req, ctx.reference_time)
        caps0 = list(req.get("capability_ids") or [])

        r = yield from self._read_memory(ctx, facts, caps0)
        if not r["ok"]:
            return self._tool_failure("read_memory", r)

        # A clarification reply arrives as new request evidence (the reply source named in the checkpoint, or
        # evidence not seen before the question was asked).
        reply_source = ctx.extra.get("reply_source_id")
        reply_bits = [
            ref["excerpt"]
            for ref in facts.bundle.get("sources") or []
            if (reply_source and ref.get("source_id") == reply_source)
            or (not reply_source and ctx.clarifications and ref["id"] not in ctx.seen_evidence_ids and ref.get("source_id") != req.get("source_id"))
        ]
        if reply_source and not reply_bits:
            r = yield from self._call(ctx, "read_evidence", {"source_id": reply_source, "revision": None})
            facts.ingest("read_evidence", r)
            if r["ok"] and isinstance(r["data"], dict):
                reply_bits = [r["data"].get("body", "")]
        facts.reply_text = _clip(" ".join(reply_bits), 400)
        query = _clip(f"{facts.reply_text} {req['subject']}" if facts.reply_text else f"{req['subject']} {req['body']}", 500)

        # Separate slots per kind: on a reused graph earlier copies of this request would otherwise crowd out work
        # items. Rev 5 #32: customer_id filters request candidates only.
        for types, cust in ((["capability", "work_item"], None), (["request"], req["customer_id"])):
            r = yield from self._call(ctx, "search_context", {"query": query, "entity_types": types, "customer_id": cust, "limit": 20})
            facts.ingest("search_context", r)
            if not r["ok"] and (r["error"] or {}).get("retryable"):
                return self._tool_failure("search_context", r)
        r = yield from self._call(ctx, "search_context", {"query": query, "entity_types": ["source"], "customer_id": None, "limit": 20})
        facts.ingest("search_context", r)
        if not r["ok"] and (r["error"] or {}).get("retryable"):
            return self._tool_failure("search_context", r)

        # One hop from the strongest candidates tells which capability each doc/work/request is about.
        dups = facts.near_duplicates()

        def top(kind: str, n: int, rel: float = 0.0, max_dups: int = 0) -> list[str]:
            """Best candidates of a kind. Earlier copies of this same request (reused graphs) keep at most `max_dups`
            slots: their tracked work matters, but they must not crowd out documentation and other evidence."""
            cands = sorted((c for c in facts.candidates if c["entity_type"] == kind and c["id"] not in (req["id"], req.get("source_id"))), key=lambda c: -float(c.get("score", 0)))
            dup_c = [c["id"] for c in cands if c["id"] in dups][:max_dups]
            seen_titles: set[str] = set()
            unique = []
            for c in cands:  # repeated copies of one request collapse to a single seed
                if c["id"] in dups or c.get("title") in seen_titles:
                    continue
                seen_titles.add(c.get("title"))
                unique.append(c)
            cands = unique
            best = float(cands[0].get("score", 0)) if cands else 0.0
            return dup_c + [c["id"] for c in cands if float(c.get("score", 0)) >= rel * best][:n]

        # Requests first: graph expansion returns a BFS tree, so a node reached from an earlier seed loses its edge
        # to later seeds. Then expand from every capability discovered, and union both.
        # Seeds that are themselves neighbours lose their shared edge too, so capabilities are expanded separately.
        seeds = top("request", 3, max_dups=1) + top("source", 3) + top("work_item", 3, 0.3)
        seeds += [w for w in req.get("linked_work_ids") or [] if w not in seeds]
        if seeds:
            r = yield from self._call(
                ctx, "expand_context", {"entity_ids": list(dict.fromkeys(seeds))[:10], "relation_types": ["ABOUT", "IMPLEMENTED_BY", "SUPPORTED_BY", "TRACKED_BY", "OWNED_BY"], "depth": 1, "limit": 100}
            )
            facts.ingest("expand_context", r)
            if not r["ok"] and (r["error"] or {}).get("retryable"):
                return self._tool_failure("expand_context", r)
        # Verify the two strongest capability contenders one at a time: a single-seed expansion returns that
        # capability's complete neighbourhood (docs, work, prior requests, owners).
        ranked = facts.capability_ranking()
        best_direct = max((sig["direct"] for _c, _s, sig in ranked), default=0.0)
        tied = [c for c, _s, sig in ranked if best_direct > 0 and sig["direct"] >= 0.9 * best_direct]
        close = [c for c, sc, _g in ranked[1:2] if ranked and sc >= 0.8 * ranked[0][1]]
        contenders = list(dict.fromkeys([c for c, _s, _g in ranked[:1]] + tied + close))[:3]  # only those that can change the outcome
        budget_left = ctx.extra.get("budget_left")
        if callable(budget_left):  # keep room for memory, a target read, prepare, commit and one replan
            contenders = contenders[: max(1, budget_left() - 6)]
        for cap in contenders:  # Rev 5 edges cover visited nodes only; a capability's own neighbourhood still needs this
            r = yield from self._call(ctx, "expand_context", {"entity_ids": [cap], "relation_types": ["ABOUT", "IMPLEMENTED_BY", "SUPPORTED_BY", "OWNED_BY"], "depth": 1, "limit": 100})
            facts.ingest("expand_context", r)
            if r["ok"]:
                d = r["data"] or {}
                facts.complete_neighbourhood[cap] = d.get("truncation") != "limit" if "truncation" in d else not (d.get("truncated") and len(d.get("paths") or []) >= 100)

        caps, ambiguous, competing = facts.resolve_capabilities()
        compat0 = facts.work_for(caps)[0] if caps and not ambiguous else []
        if caps and not ambiguous and (set(caps) - set(caps0) or compat0):
            # Decisions scoped to the resolved capability, plus the current state of matching work (refresh
            # before relying on record excerpts that may be old).
            owners = list(dict.fromkeys([o for w_ in compat0[:3] for o in facts.owners_of(w_)] + [o for c in caps for o in facts.owners_of(c)]))
            people = [o for o in owners if facts.label_of(o) in ("person", "")][:5]
            r = yield from self._read_memory(ctx, facts, caps, compat0[:5], people)
            if not r["ok"] and (r["error"] or {}).get("retryable"):
                return self._tool_failure("read_memory", r)

        ctx.extra["seen_evidence_ids"] = [ref["id"] for ref in facts.bundle.get("sources") or []]
        ctx.extra["capabilities"] = caps
        replans = 0
        tried: set[tuple[str, str, tuple[str, ...]]] = set()
        while True:
            draft = yield from self._decide(ctx, facts, caps, ambiguous, competing)
            revision = int(facts.bundle.get("based_on_revision") or ctx.extra.get("knowledge_revision") or 0)
            r = yield from self._call(ctx, "prepare_action", draft.arguments(revision))
            proposal = r.get("data") if r["ok"] else None
            failure_code = None if r["ok"] else (r["error"] or {}).get("code")
            if r["ok"] and proposal.get("validity") == "valid":
                if draft.kind == "clarify":
                    ctx.extra["clarify_question"] = draft.payload["question"]
                    ctx.extra["clarified"] = True
                outcome = yield from self._after_valid(ctx, draft, proposal)
                if outcome is not None:
                    return outcome
                failure_code = "REVISION_CONFLICT"
            elif not r["ok"] and failure_code not in REVALIDATION_CODES:
                return self._tool_failure("prepare_action", r, stage="prepare")
            reasons = [str(x) for x in ((proposal or {}).get("validation_reasons") or [failure_code or "invalid"])]
            facts.note_rejection(reasons)
            signature = (draft.kind, json.dumps(draft.payload, sort_keys=True), tuple(sorted(draft.evidence_ids)))
            if signature in tried and (proposal or {}).get("validity") != "stale":
                # The same draft was already refused: replanning would burn budget on an identical proposal.
                return Outcome(
                    completion="failed", stage="prepare", run_status_hint="failed", kind=draft.kind,
                    proposal_ids=[proposal["id"]] if proposal else [],
                    summary=f"{LABEL}: identical proposal refused again; stopping instead of replanning: {_clip('; '.join(reasons), 400)}",
                    error={"code": "PROPOSAL_INVALID", "message": _clip("; ".join(reasons), 500)},
                )
            tried.add(signature)
            replans += 1
            if (proposal or {}).get("validity") == "invalid" and replans >= 2:
                return Outcome(
                    completion="failed",
                    stage="prepare",
                    run_status_hint="failed",
                    kind=draft.kind,
                    proposal_ids=[proposal["id"]] if proposal else [],
                    summary=f"{LABEL}: proposal remained invalid after revalidation: {_clip('; '.join(reasons), 400)}",
                    error={"code": "PROPOSAL_INVALID", "message": _clip("; ".join(reasons), 500)},
                )
            ctx.replan(f"proposal {(proposal or {}).get('validity', failure_code)}: {_clip('; '.join(reasons), 300)}")
            # Refresh affected context before replanning (the API marked the basis stale/invalid).
            facts.bundle, facts.decisions, facts.attempts, facts.work = {}, {}, {}, {}
            r = yield from self._read_memory(ctx, facts, caps)
            if not r["ok"]:
                return self._tool_failure("read_memory", r)

    def _after_valid(self, ctx: PlanContext, draft: Draft, proposal: dict[str, Any]):
        evidence = draft.evidence_ids
        if draft.kind not in ctx.auto_commit_kinds:
            return Outcome(
                completion="waiting",
                stage="decide",
                run_status_hint="awaiting_review",
                kind=draft.kind,
                proposal_ids=[proposal["id"]],
                evidence_ids=evidence,
                awaiting="review",
                summary=f"{LABEL}: {draft.summary} Awaiting operator review (kind {draft.kind} needs authorization).",
            )
        r = yield from self._call(ctx, "commit_action", {"proposal_id": proposal["id"], "expected_revision": proposal["based_on_revision"]})
        if r["ok"]:
            action = (r["data"] or {}).get("action") or {}
            return Outcome(
                completion="succeeded",
                stage="act",
                run_status_hint="queued",
                kind=draft.kind,
                proposal_ids=[proposal["id"]],
                action_ids=[action["id"]] if action.get("id") else [],
                evidence_ids=evidence,
                summary=f"{LABEL}: {draft.summary} Committed under workspace policy; dispatch queued.",
            )
        code = (r["error"] or {}).get("code")
        if code == "POLICY_VIOLATION":
            return Outcome(
                completion="waiting",
                stage="decide",
                run_status_hint="awaiting_review",
                kind=draft.kind,
                proposal_ids=[proposal["id"]],
                evidence_ids=evidence,
                awaiting="review",
                summary=f"{LABEL}: {draft.summary} Commit refused by policy; awaiting operator review.",
            )
        if code in ("REVISION_CONFLICT", "INVALID_TRANSITION"):
            return None  # stale basis -> replan
        return self._tool_failure("commit_action", r, stage="act")

    # -- disposition ----------------------------------------------------------------------------------
    def _decide(self, ctx: PlanContext, facts: Facts, caps: list[str], ambiguous: bool, competing: list[dict[str, Any]]):
        req = facts.request
        compat, similar = facts.work_for(caps)
        decisions = facts.applicable_decisions(caps, compat)
        coverage = facts.coverage_notes()
        ctx.extra["coverage_notes"] = coverage  # recorded in run events (proposal_ready), not in the prose
        coverage_text = ""
        say = Words(facts)

        # 1. Incompatible active constraints -> visible escalation, never a silent choice.
        conflicts = facts.conflicts(decisions)
        if conflicts:
            a, b, why = conflicts[0]
            ids = [a["id"], b["id"]]
            ev = facts.valid_evidence((a.get("evidence_ids") or []) + (b.get("evidence_ids") or []))
            rules = f"'{say.rule(a)}' ({say.scope(a['scope'])}) versus '{say.rule(b)}' ({say.scope(b['scope'])})"
            return Draft(
                kind="escalate",
                payload={"reason": _clip(f"Two company rules conflict here: {rules}. Someone needs to decide which one applies.", 4000), "owner_id": None, "team_id": None},
                evidence_ids=ev,
                explanation=_clip(f"Two company rules that both apply here conflict: {rules}. We won't pick one quietly, so this needs a decision.", 6000),
                rejected_alternatives=[_alt("answer", "Not answering yet: any reply would quietly apply one of the two conflicting rules.", ev)],
                summary=f"Escalated: conflicting decisions {ids[0]} / {ids[1]}.",
            )

        # 2. Ambiguous capability match -> one targeted question.
        if ambiguous:
            titles = [c.get("title") or facts.title_of(c["id"]) for c in competing]
            ev = facts.valid_evidence([r["id"] for c in competing for r in c.get("evidence") or []] + [r["id"] for c in competing for r in facts.doc_evidence([c["id"]])[:1]])
            if ctx.clarifications >= 2:
                return self._escalate_unclear(facts, ev, f"We've asked {ctx.clarifications} times and it's still unclear whether this is about {' or '.join(titles)}.")
            question = f"To make sure we help with the right thing: do you mean {' or '.join(titles)}?"
            return Draft(
                kind="clarify",
                payload={"question": _clip(question, 2000), "missing_fields": ["capability"]},
                evidence_ids=ev,
                explanation=_clip(f"This could mean {' or '.join(titles)}, and nothing we found tells them apart. We're asking one question instead of guessing.", 6000),
                rejected_alternatives=[_alt("create_work", "No task yet: we'd be guessing what the customer actually needs.", [])],
                summary=f"Clarification: ambiguous between {', '.join(titles)}.",
            )

        # 3. Nothing decisive to work from.
        if not caps:
            missing = [m for m in facts.missing_information if "capability" in m.lower()][:3] or ["affected capability or product area"]
            ev = facts.valid_evidence(r["id"] for r in facts.bundle.get("sources") or [] if r.get("source_id") == req.get("source_id"))
            if ctx.clarifications >= 2:
                return self._escalate_unclear(facts, ev, "Even after asking, we still can't tell which part of the product this is about.")
            return Draft(
                kind="clarify",
                payload={"question": "Could you tell us which product area or feature this concerns, and what outcome you expect?", "missing_fields": [_clip(m, 200) for m in missing]},
                evidence_ids=ev,
                explanation="We couldn't tell which part of the product this is about, so we're asking.",
                rejected_alternatives=[_alt("create_work", "No task yet: there's nothing specific enough to hand to a team.", [])],
                summary="Clarification: missing decisive field (capability).",
            )

        cap_titles = ", ".join(facts.title_of(c) for c in caps)
        allowed = facts.allowed_kinds(decisions)
        forbidden = facts.forbidden_values(decisions)
        failed = facts.failed_attempts(caps, compat)
        failed_keys = {a["procedure_key"] for a in failed}
        verified = facts.verified_resolutions(caps, compat)
        usable = [a for a in verified if a["procedure_key"] not in failed_keys and a["procedure_key"] not in forbidden and a.get("request_id") != req["id"]]
        forbidden_hits = [(a, forbidden[a["procedure_key"]]) for a in verified if a["procedure_key"] in forbidden]
        rejected: list[dict[str, Any]] = []
        for a in failed[:2]:
            when = f" on {day(a.get('observed_at'))}" if day(a.get("observed_at")) else ""
            whom = f" {say.scope(a['scope'])}" if a["scope"]["entity_type"] in ("customer", "capability") else " on this request" if a.get("request_id") == req["id"] else " on a similar request"
            rejected.append(_alt(
                "answer",
                f"Not {say.procedure(a)} again: it already failed{whom}{when}.",
                facts.valid_evidence(a.get("evidence_ids") or []),
            ))

        def permitted(kind: str) -> bool:
            return (allowed is None or kind in allowed) and kind not in failed_keys

        # Strength of each route = how well its best evidence matches this request.
        linked = set(req.get("linked_work_ids") or [])
        items = [facts.work[w] for w in compat if w in facts.work]
        for wid in compat:
            if wid not in facts.work:  # state not observed by any tool: still a candidate, the API validates it
                items.append({"id": wid, "title": facts.title_of(wid), "status": None, "owner_id": None, "evidence_ids": [], "observed_at": None, "forecast_at": None})
        targets = [w for w in items if w["id"] not in linked and (w.get("status") in OPEN_WORK or w.get("status") in ("done", None))]
        for w in items:
            if w.get("status") == "cancelled":
                rejected.append(_alt("link_work", f"Not {say.work(w['id'])}: that work was cancelled, so it won't deliver this.", facts.valid_evidence(w.get("evidence_ids") or [])))
        targets.sort(key=lambda w: (-facts.work_relevance(w["id"], caps), w.get("status") == "done", facts.age_days(w.get("observed_at")) or 0.0))
        work_strength = facts.work_relevance(targets[0]["id"], caps) if targets else 0.0
        docs = facts.doc_sources(caps)
        gaps = facts.doc_gaps(caps)
        best_resolution = max(usable, key=lambda a: facts.score_of(a.get("request_id", "")), default=None)
        resolution_strength = facts.score_of(best_resolution.get("request_id", "")) if best_resolution else 0.0
        # A failed procedure blocks documentation answers when the requester reports a malfunction (the documented
        # remedy is what failed); an unrelated how-to question is not blocked by someone else's failed fix.
        blocking_failures = [a for a in failed if facts.reports_problem() or a.get("procedure_key") in ("answer",)]
        doc_strength = max((facts.doc_relevance(sid, sc, caps) for sid, sc in docs), default=0.0) if not blocking_failures and not gaps else 0.0
        # A docs-only answer must clearly beat matching work; a verified prior resolution only has to match it.
        answer_wins = (docs and resolution_strength > work_strength) or (facts.request_is_question() and doc_strength > 1.2 * work_strength)

        # 4. Answer: documentation (plus a verified prior resolution when one applies) beats the best work match.
        if answer_wins and permitted("answer") and not gaps:
            refs = facts.doc_evidence(caps)
            if refs and not facts.doc_gaps(caps):
                draft = self._answer(facts, caps, refs, best_resolution if resolution_strength and resolution_strength >= doc_strength else None, rejected, coverage_text, forbidden)
                if draft is not None:
                    return draft

        # 5. Existing compatible work.
        if targets and permitted("link_work"):
            target = targets[0]
            if target["id"] not in facts.expanded_targets:  # graph edges survive a replan; only state is re-read
                r = yield from self._call(ctx, "expand_context", {"entity_ids": [target["id"]], "relation_types": ["DEPENDS_ON", "OWNED_BY", "IMPLEMENTED_BY"], "depth": 4, "limit": 40})
                facts.ingest("expand_context", r)
                facts.expanded_targets.add(target["id"])
                chain_owners = [o for w_ in [target["id"]] + facts.prerequisites(target["id"]) for o in facts.owners_of(w_)
                                if o not in facts.people and facts.label_of(o) in ("person", "")]
                budget_left = ctx.extra.get("budget_left")
                if chain_owners and (not callable(budget_left) or budget_left() > 3):  # who owns the critical path, and are they still here
                    r = yield from self._read_memory(ctx, facts, caps, None, list(dict.fromkeys(chain_owners))[:5])
            return self._link_or_escalate(facts, caps, target, decisions, rejected, coverage_text, docs)
        if targets and not permitted("link_work"):
            return self._escalate_policy(facts, decisions, "link_work")

        # 6. Forbidden workaround is the only known remedy and no work covers the request.
        if forbidden_hits:
            a, d = forbidden_hits[0]
            ev = facts.valid_evidence(list(d.get("evidence_ids") or []) + list(a.get("evidence_ids") or []))
            routing, _d = facts.routing_owner(decisions)
            return Draft(
                kind="escalate",
                payload={"reason": _clip(f"The only fix we know is ruled out by a company rule ('{say.rule(d)}'), and no existing work covers this.", 4000), "owner_id": routing, "team_id": None},
                evidence_ids=ev,
                explanation=_clip(f"A fix has worked before for {say.caps(caps)}, but a company rule rules it out here: '{say.rule(d)}'. Nothing else covers this request, so it needs a decision.", 6000),
                rejected_alternatives=(rejected + [_alt("answer", f"Not the earlier fix: a company rule rules it out ('{say.rule(d)}').", ev)])[:5],
                summary=f"Escalated: forbidden workaround with no alternative ({d['id']}).",
            )

        # 7. Absence of work cannot be inferred from incomplete retrieval: check the capability's work directly.
        complete = facts.complete_neighbourhood.get(caps[0])
        if complete is None:
            r = yield from self._call(ctx, "expand_context", {"entity_ids": [caps[0]], "relation_types": ["IMPLEMENTED_BY", "OWNED_BY", "SUPPORTED_BY"], "depth": 1, "limit": 100})
            facts.ingest("expand_context", r)
            d = r.get("data") or {}
            complete = r["ok"] and (d.get("truncation") != "limit" if "truncation" in d else not (d.get("truncated") and len(d.get("paths") or []) >= 100))
        if not complete:
            return self._escalate_unclear(
                facts, facts.valid_evidence(facts.known_evidence_ids[:5]),
                f"We couldn't check all existing work for {say.caps(caps)}, so we can't be sure nothing already covers this. Treat this as a best guess and confirm before creating new work.",
            )

        # 8. Genuinely new work.
        if not permitted("create_work"):
            return self._escalate_policy(facts, decisions, "create_work")
        return self._create(ctx, facts, caps, similar, decisions, rejected, gaps, coverage_text)

    # -- drafts -----------------------------------------------------------------------------------------
    def _answer(self, facts: Facts, caps: list[str], refs: list[dict[str, Any]], resolution: dict[str, Any] | None, rejected: list[dict[str, Any]],
                coverage_text: str, forbidden: dict[str, dict[str, Any]] | None = None) -> Draft | None:
        req = facts.request
        cap_titles = ", ".join(facts.title_of(c) for c in caps)
        forbidden = forbidden or {}

        avoid = dict(forbidden)
        for a in facts.failed_attempts(caps):  # never re-recommend a procedure that already failed in this scope
            avoid.setdefault(a["procedure_key"], a)

        def safe(text: str) -> str:
            """Drop sentences that advocate a forbidden workaround or a procedure that already failed here."""
            if not avoid:
                return text
            keep = [sen for sen in re.split(r"(?<=[.!?])\s+", text) if not facts.forbidden_phrase_hit(sen, avoid)]
            return " ".join(keep)

        if resolution and facts.forbidden_phrase_hit(resolution.get("summary", "") + " " + resolution.get("procedure_key", ""), forbidden):
            resolution = None
        want = tokens(facts.request_text()) - facts.generic_tokens(caps)
        scored = []  # (overlap, ref, sentence) across cited documentation, forbidden advice removed
        for ref in refs[:5]:
            for sen in re.split(r"(?<=[.!?])\s+|\n+", safe(ref.get("excerpt", ""))):
                sen = re.sub(r"^#+\s*", "", sen).strip()
                if len(sen) > 20:
                    scored.append((len(tokens(sen) & want), ref, sen))
        scored.sort(key=lambda t: -t[0])
        if not scored or scored[0][0] < 1:
            return None  # nothing relevant left to say once forbidden advice is removed
        best_ref = scored[0][1]
        quoted = " ".join(sen for _n, ref, sen in scored[:3] if ref is best_ref)
        ev = facts.valid_evidence([r["id"] for r in refs[:3]] + list((resolution or {}).get("evidence_ids") or []))
        parts = [
            f"Thanks for reaching out about \"{_clip(req['subject'], 150)}\".",
            f"Our documentation ({Words(facts).doc(best_ref['source_id'])}) says: \"{_clip(quoted, 700)}\"",
        ]
        if resolution:
            parts.append(f"This has worked before in the same setup: {_clip(resolution['summary'], 500)}")
        parts.append("Please let us know whether this resolves it for you.")
        criteria = [_clip(f"Requester confirms that '{req['subject']}' is resolved", 1000)]
        if resolution:
            criteria.append(_clip(f"The earlier confirmed fix ({Words(facts).procedure(resolution)}) works for this customer too", 1000))
        stale = [r for r in refs[:3] if r.get("stale")]
        say = Words(facts)
        worked = ""
        if resolution:
            when = f" on {day(resolution.get('observed_at'))}" if day(resolution.get("observed_at")) else ""
            worked = f" The same fix, {say.procedure(resolution)}, was confirmed to work{when}."
        return Draft(
            kind="answer",
            payload={"draft_text": _clip(" ".join(parts), 12000), "resolution_criteria": criteria},
            evidence_ids=ev,
            explanation=_clip(
                f"Our documentation ({say.doc(best_ref['source_id'])}) already covers this.{worked} No new work is needed."
                + (" Some of that documentation is old, so double-check it before sending." if stale else ""),
                6000,
            ),
            rejected_alternatives=(rejected + [_alt("create_work", "No task needed: the documentation already answers this.", ev[:3])])[:5],
            summary=f"Answer drafted from {best_ref['source_id']}" + (f" and verified attempt {resolution['id']}." if resolution else "."),
        )

    def _link_or_escalate(self, facts: Facts, caps: list[str], target: dict[str, Any], decisions: list[dict[str, Any]], rejected: list[dict[str, Any]], coverage_text: str, docs: list[tuple[str, float]]) -> Draft:
        cap_titles = ", ".join(facts.title_of(c) for c in caps)
        chain = [target["id"]] + facts.prerequisites(target["id"])
        inactive = facts.inactive_people()
        routing, routing_decision = facts.routing_owner(decisions)
        routing_active = routing if routing and routing not in inactive else None
        doc_ev = [r["id"] for r in facts.doc_evidence(caps)[:1]]
        path_ev = facts.valid_evidence(
            list(target.get("evidence_ids") or [])
            + [e for i in range(len(chain) - 1) for e in facts.edge_evidence(chain[i], chain[i + 1])]
            + [e for c in caps for e in facts.edge_evidence(c, target["id"])]
            + doc_ev
            + (list(routing_decision.get("evidence_ids") or []) if routing_decision else [])
        ) or facts.valid_evidence(facts.known_evidence_ids[:3])
        say = Words(facts)
        own = facts.owners_of(target["id"])
        target_unowned = target.get("status") not in ("done", None) and (not own or all(o in inactive for o in own))
        title = say.work(target["id"])
        if target_unowned and not routing_active:
            who = "nobody owns it" if not own else f"its owner {say.person(own[0])} has left"
            return Draft(
                kind="escalate",
                payload={"reason": _clip(f"{title} matches this request, but {who}. Someone needs to own it before we can update the customer.", 4000), "owner_id": None, "team_id": None},
                evidence_ids=path_ev,
                explanation=_clip(f"Engineering has matching work, {title}, but {who}, so nobody is driving it. {self._state_notes(facts, target)}", 6000),
                rejected_alternatives=(rejected + [_alt("link_work", f"Not just linking to {title}: nobody would be driving it.", path_ev[:5])])[:5],
                summary=f"Escalated: matching work {target['id']} has no active owner.",
            )
        sentences: list[str] = []
        active_own = [o for o in own if o not in inactive and say.name(o)]
        owner_txt = f" (owner {say.name(active_own[0])})" if active_own else ""
        if target.get("status") == "done":
            sentences.append(f"Engineering already finished this: {title}{owner_txt}. We should check with the customer that it fixed their case.")
        else:
            sentences.append(f"Engineering already has this: {title}{owner_txt}.")
        open_chain = [w for w in chain[1:] if (facts.work.get(w) or {}).get("status") not in ("done", "cancelled")]
        unowned = [w for w in open_chain if not facts.owners_of(w) or all(o in inactive for o in facts.owners_of(w))]
        blocker = (unowned or open_chain or [None])[0]  # name the blocker that actually has the ownership problem
        no_date = target.get("status") != "done" and not target.get("forecast_at")
        if blocker:
            b_own = facts.owners_of(blocker)
            left = [o for o in b_own if o in inactive]
            clause = f"It's blocked behind {say.work(blocker)}"
            if b_own and len(left) == len(b_own):
                clause += f", whose owner {say.person(left[0])} has left"
            elif not b_own:
                clause += ", which nobody owns"
            sentences.append(clause + (", so we can't promise a date." if no_date else "."))
        elif no_date:
            sentences.append("There's no delivery date on record, so we can't promise one.")
        sentences.append(self._state_notes(facts, target, include_date=not no_date))
        chain_issue = any((not facts.owners_of(w)) or all(o in inactive for o in facts.owners_of(w)) for w in chain[1:]
                          if (facts.work.get(w) or {}).get("status") not in ("done", "cancelled"))
        if routing_active and (chain_issue or not own):
            sentences.append(f"Company rule: route these to {say.person(routing_active)}.")
        return Draft(
            kind="link_work",
            payload={"work_item_id": target["id"], "comment_text": None},
            evidence_ids=path_ev,
            explanation=_clip(" ".join(x for x in sentences if x), 6000),
            rejected_alternatives=(rejected + [_alt("create_work", f"No new task: {title} already covers this.", path_ev[:5])])[:5],
            summary=f"Link to existing work {target['id']} ({target.get('status')}).",
        )

    @staticmethod
    def _state_notes(facts: Facts, w: dict[str, Any], include_date: bool = True) -> str:
        notes = []
        if w.get("status") is None:
            notes.append("We couldn't check its current status, so its state is unknown; confirm it before replying.")
        if include_date and w.get("status") != "done":
            if w.get("forecast_at"):
                notes.append(f"It's forecast for {day(w['forecast_at'])}, which is an estimate, not a promise.")
            else:
                notes.append("There's no delivery date on record, so we can't promise one.")
        if facts.is_stale(w.get("observed_at")):
            notes.append(f"Its status was last updated on {day(w.get('observed_at'))}, so it may be out of date.")
        return " ".join(notes)

    def _create(self, ctx: PlanContext, facts: Facts, caps: list[str], similar: list[str], decisions: list[dict[str, Any]], rejected: list[dict[str, Any]], gaps: list[tuple[str, str]], coverage_text: str) -> Draft:
        req = facts.request
        cap = caps[0]
        cap_titles = ", ".join(facts.title_of(c) for c in caps)
        routing, routing_decision = facts.routing_owner(decisions)
        inactive = facts.inactive_people()
        cap_people = [o for o in facts.owners_of(cap) if facts.label_of(o) in ("person", "")]
        owner = routing if routing and routing not in inactive else next((p for p in cap_people if p not in inactive), None)
        say = Words(facts)
        if owner and routing_decision and owner == routing:
            owner_note = f"It would go to {say.name(owner) or 'the routed owner'}, per the company routing rule."
        elif owner:
            owner_note = f"It would go to {say.name(owner)}, who owns {say.cap(cap)}." if say.name(owner) else f"It would go to the owner of {say.cap(cap)}."
        else:
            dead = sorted(inactive & set(cap_people))
            owner_note = "Nobody is assigned yet" + (f": {say.person(dead[0])} owned this area but has left." if dead else ", because no one owns this area.")
        for wid in similar[:4]:
            w = facts.work.get(wid) or {}
            their = [c for c in facts.caps_of_work(wid) if c not in caps]
            reasons = []
            excluded = facts.excluded_by_request(wid, caps)
            if excluded:
                said = facts.ruling_sentence(excluded)
                reasons.append(f"the customer told us \"{_clip(said, 200).rstrip('.')}\"" if said else "the customer ruled that approach out")
            if their and not any(c in caps for c in facts.caps_of_work(wid)):
                reasons.append(f"it's about {say.caps(their)}")
            if w.get("status") == "cancelled":
                reasons.append("it was cancelled")
            if not reasons:
                reasons.append("it doesn't cover what's being asked")
            cand_ev = [x["id"] for c in facts.candidates if c["id"] == wid for x in c.get("evidence") or []]
            ev = facts.valid_evidence(list(w.get("evidence_ids") or []) + cand_ev)
            rejected.append(_alt("link_work", f"Not {say.work(wid)}: {'; and '.join(reasons)}.", ev))
        connector = "linear" if ctx.connector_state("linear") in ("enabled", "verified", "unverified") else "local"
        gap_ev = [r["id"] for r in facts.evidence.values() if r.get("source_id") in {g[0] for g in gaps}][:2]
        ev = facts.valid_evidence(
            [r["id"] for r in facts.bundle.get("sources") or [] if r.get("source_id") == req.get("source_id")]
            + gap_ev
            + [r["id"] for r in facts.doc_evidence(caps)[:2]]
            + (list(routing_decision.get("evidence_ids") or []) if routing_decision else [])
        ) or facts.valid_evidence(facts.known_evidence_ids[:3])
        gap_sentence = gaps[0][1].strip().rstrip(".") if gaps else ""
        gap_text = f" Our documentation says {gap_sentence[:1].lower() + gap_sentence[1:]}." if gap_sentence else ""
        description = (
            f"What the customer asked: {_clip(req['body'], 1500)}\n\n"
            f"Area: {say.caps(caps)}.{gap_text} {owner_note}\n"
            f"Existing work we checked and ruled out: {', '.join(say.work(w_) for w_ in similar[:4]) or 'none'}.\n"
            f"Intent marker: {intent_marker(req['id'], str((ctx.run or {}).get('id')), cap, req['subject'])}"
        )
        return Draft(
            kind="create_work",
            payload={
                "title": _clip(f"{req['subject']} ({facts.title_of(cap)})", 300),
                "description": _clip(description, 16000),
                "acceptance_criteria": [
                    _clip(f"{facts.title_of(cap)} supports the requested behaviour: {req['subject']}", 2000),
                    "The customer confirms it works for them",
                    "The documentation is updated to describe it",
                ],
                "owner_id": owner,
                "capability_id": cap,
                "dependency_ids": [],
                "connector": connector,
            },
            evidence_ids=ev,
            explanation=_clip(
                f"Nothing in progress covers this, so we suggest a new task for {say.caps(caps)}.{gap_text} {owner_note}"
                + (" It would be created in Linear." if connector == "linear" else ""),
                6000,
            ),
            rejected_alternatives=rejected[:5],
            summary=f"Create new {connector} work for {facts.title_of(cap)}.",
        )

    @staticmethod
    def _escalate_unclear(facts: Facts, ev: list[str], reason: str) -> Draft:
        return Draft(
            kind="escalate",
            payload={"reason": _clip(reason, 4000), "owner_id": None, "team_id": None},
            evidence_ids=ev,
            explanation=_clip(reason, 6000),
            rejected_alternatives=[_alt("create_work", "No new task: we don't have enough to go on yet.", [])],
            summary=_clip(f"Escalated: {reason}", 300),
        )

    @staticmethod
    def _escalate_policy(facts: Facts, decisions: list[dict[str, Any]], kind: str) -> Draft:
        ds = [d for d in decisions if d["rule_type"] == "allowed_action_types"]
        ev = facts.valid_evidence(e for d in ds for e in d.get("evidence_ids") or [])
        say = Words(facts)
        rule = f" ('{say.rule(ds[0])}')" if ds else ""
        reason = f"A company rule for this customer doesn't allow {KIND_WORDS.get(kind, kind)}{rule}, so a person needs to decide."
        return Draft(
            kind="escalate",
            payload={"reason": reason, "owner_id": None, "team_id": None},
            evidence_ids=ev,
            explanation=reason,
            rejected_alternatives=[_alt(kind, reason, ev)],
            summary=f"Escalated: {kind} not permitted.",
        )

    # -- continuation after dispatch -------------------------------------------------------------------
    def _observe(self, ctx: PlanContext) -> PlanGenerator:
        action_id = ctx.pending_action_id
        if not action_id:
            return Outcome(completion="failed", stage="observe", run_status_hint="failed", summary=f"{LABEL}: continuation without an action id.", error={"code": "INVALID_INPUT", "message": "no pending action"})
        r = yield from self._call(ctx, "inspect_action", {"action_id": action_id})
        if not r["ok"]:
            return self._tool_failure("inspect_action", r, stage="observe")
        action = r["data"]
        kind, status = action.get("kind"), action.get("status")
        if status != "succeeded":
            return Outcome(
                completion="failed",
                stage="observe",
                run_status_hint="failed" if status == "failed" else "awaiting_review",
                kind=kind,
                action_ids=[action_id],
                summary=f"{LABEL}: action {action_id} is {status}; continuation does not assume success.",
                error={"code": (action.get("error") or {}).get("code") or f"ACTION_{str(status).upper()}", "message": f"action status {status}"},
            )
        receipt = action.get("receipt") or {}
        if kind == "clarify":
            question = ctx.extra.get("clarify_question") or "A clarification question was drafted; see the proposal."
            return Outcome(
                completion="succeeded",
                stage="observe",
                run_status_hint="waiting_for_input",
                kind=kind,
                action_ids=[action_id],
                summary=f"{LABEL}: clarification draft persisted (receipt {receipt.get('target_id')}); waiting for the customer's reply.",
            )
        if kind in ("link_work", "create_work"):
            facts = Facts.new(ctx.request, ctx.reference_time)
            r = yield from self._read_memory(ctx, facts, list(ctx.request.get("capability_ids") or []))
            if not r["ok"]:
                return self._tool_failure("read_memory", r, stage="observe")
            linked = list(ctx.request.get("linked_work_ids") or [])
            target_id = receipt.get("target_id")
            work = facts.work.get(target_id) or next((facts.work[w] for w in reversed(linked) if w in facts.work), None)
            if work is None:
                body = "The work was linked, but we couldn't read its current status yet."
                verification, ev, stage = "inconclusive", [], "observe"
            else:
                st = work.get("status")
                if st == "done":
                    body = f"'{work['title']}' is done (as of {day(work.get('observed_at'))}). The customer has NOT confirmed it's fixed yet, so check with them."
                    stage = "verify"
                elif st == "cancelled":
                    body = f"'{work['title']}' was cancelled, so it won't deliver this. This request needs another look."
                    stage = "observe"
                else:
                    body = f"'{work['title']}' is {str(st).replace('_', ' ')} (as of {day(work.get('observed_at'))}). We're waiting on that work; no delivery date is promised."
                    stage = "observe"
                verification = "inconclusive" if st == "cancelled" else "pending"
                ev = facts.valid_evidence(work.get("evidence_ids") or [])
            r = yield from self._call(
                ctx,
                "record_observation",
                {"action_id": action_id, "kind": "connector_status", "verification": verification, "body": _clip(body, 4000), "evidence_ids": ev},
            )
            if not r["ok"]:
                return self._tool_failure("record_observation", r, stage="observe")
            obs = (r["data"] or {}).get("observation") or {}
            return Outcome(
                completion="succeeded",
                stage=stage,
                run_status_hint="succeeded",
                kind=kind,
                action_ids=[action_id],
                evidence_ids=ev,
                summary=f"{LABEL}: {body}",
                extra={"observation_ids": [obs["id"]] if obs.get("id") else []},
            )
        stage = "verify" if kind == "answer" else "observe"
        note = "Answer draft persisted; awaiting attributable verification." if kind == "answer" else "Internal review case persisted."
        return Outcome(completion="succeeded", stage=stage, run_status_hint="succeeded", kind=kind, action_ids=[action_id], summary=f"{LABEL}: {note} Receipt {receipt.get('target_id')}.")
