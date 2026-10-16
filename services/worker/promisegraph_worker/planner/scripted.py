"""ScriptedPlanner — "Scripted mode".

A deterministic stand-in for the model planner. It emits model-shaped tool calls into the real tool loop and
chooses a disposition only from facts the tools returned: capabilities and their candidates, graph paths,
work state and owners, active scoped decisions, scoped attempts, documentation evidence and coverage flags.
It never reads scenario ids, fixture names or expected outcomes, and it cannot bypass the tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import Outcome, PlanContext, PlanGenerator
from ..markers import intent_marker
from .facts import OPEN_WORK, Facts, tokens

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
    def _read_memory(self, ctx: PlanContext, facts: Facts, caps: list[str], work_ids: list[str] | None = None):
        req = ctx.request
        scopes = [{"entity_type": "customer", "entity_id": req["customer_id"]}, {"entity_type": "request", "entity_id": req["id"]}]
        scopes += [{"entity_type": "capability", "entity_id": c} for c in caps[:4]]
        args: dict[str, Any] = {"scopes": scopes}
        if work_ids:
            args["work_item_ids"] = work_ids[:5]
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

        r = yield from self._call(ctx, "search_context", {"query": query, "entity_types": ["capability", "work_item", "request"], "customer_id": req["customer_id"], "limit": 20})
        facts.ingest("search_context", r)
        if not r["ok"] and (r["error"] or {}).get("retryable"):
            return self._tool_failure("search_context", r)
        r = yield from self._call(ctx, "search_context", {"query": query, "entity_types": ["source"], "customer_id": None, "limit": 10})
        facts.ingest("search_context", r)
        if not r["ok"] and (r["error"] or {}).get("retryable"):
            return self._tool_failure("search_context", r)

        # One hop from the strongest candidates tells which capability each doc/work/request is about.
        def top(kind: str, n: int, rel: float = 0.0) -> list[str]:
            cands = sorted((c for c in facts.candidates if c["entity_type"] == kind and c["id"] not in (req["id"], req.get("source_id"))), key=lambda c: -float(c.get("score", 0)))
            best = float(cands[0].get("score", 0)) if cands else 0.0
            return [c["id"] for c in cands if float(c.get("score", 0)) >= rel * best][:n]

        # Requests first: graph expansion returns a BFS tree, so a node reached from an earlier seed loses its edge
        # to later seeds. Then expand from every capability discovered, and union both.
        # Seeds that are themselves neighbours lose their shared edge too, so capabilities are expanded separately.
        seeds = top("request", 2) + top("source", 3) + top("work_item", 4, 0.3)
        seeds += [w for w in req.get("linked_work_ids") or [] if w not in seeds]
        if seeds:
            r = yield from self._call(
                ctx, "expand_context", {"entity_ids": list(dict.fromkeys(seeds))[:10], "relation_types": ["ABOUT", "IMPLEMENTED_BY", "SUPPORTED_BY", "TRACKED_BY"], "depth": 1, "limit": 100}
            )
            facts.ingest("expand_context", r)
            if not r["ok"] and (r["error"] or {}).get("retryable"):
                return self._tool_failure("expand_context", r)
        # Verify the two strongest capability contenders one at a time: a single-seed expansion returns that
        # capability's complete neighbourhood (docs, work, prior requests, owners).
        ranked = facts.capability_ranking()
        best_direct = max((sig["direct"] for _c, _s, sig in ranked), default=0.0)
        tied = [c for c, _s, sig in ranked if best_direct > 0 and sig["direct"] >= 0.9 * best_direct]
        contenders = list(dict.fromkeys([c for c, _s, _g in ranked[:1]] + tied + [c for c, _s, _g in ranked[1:2]]))[:3]
        for cap in contenders:
            r = yield from self._call(ctx, "expand_context", {"entity_ids": [cap], "relation_types": ["ABOUT", "IMPLEMENTED_BY", "SUPPORTED_BY", "OWNED_BY"], "depth": 1, "limit": 100})
            facts.ingest("expand_context", r)
            if r["ok"]:
                facts.complete_neighbourhood[cap] = not ((r["data"] or {}).get("truncated") and len((r["data"] or {}).get("paths") or []) >= 100)

        caps, ambiguous, competing = facts.resolve_capabilities()
        compat0 = facts.work_for(caps)[0] if caps and not ambiguous else []
        if caps and not ambiguous and (set(caps) - set(caps0) or compat0):
            # Decisions scoped to the resolved capability, plus the current state of matching work (refresh
            # before relying on record excerpts that may be old).
            r = yield from self._read_memory(ctx, facts, caps, compat0[:5])
            if not r["ok"] and (r["error"] or {}).get("retryable"):
                return self._tool_failure("read_memory", r)

        ctx.extra["seen_evidence_ids"] = [ref["id"] for ref in facts.bundle.get("sources") or []]
        ctx.extra["capabilities"] = caps
        replans = 0
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
            facts.rejections.extend(reasons)
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
        coverage_text = f" Coverage limits: {'; '.join(coverage)}." if coverage else ""

        # 1. Incompatible active constraints -> visible escalation, never a silent choice.
        conflicts = facts.conflicts(decisions)
        if conflicts:
            a, b, why = conflicts[0]
            ids = [a["id"], b["id"]]
            ev = facts.valid_evidence((a.get("evidence_ids") or []) + (b.get("evidence_ids") or []))
            return Draft(
                kind="escalate",
                payload={"reason": _clip(f"Conflicting active decisions {a['id']} and {b['id']} ({why}). An operator must decide which constraint applies.", 4000), "owner_id": None, "team_id": None},
                evidence_ids=ev,
                explanation=_clip(f"Active decisions {ids[0]} ({a['scope']['entity_type']}) and {ids[1]} ({b['scope']['entity_type']}) are incompatible: {why}. The planner may not pick one.{coverage_text}", 6000),
                rejected_alternatives=[_alt("answer", f"Any answer would silently apply one of the conflicting decisions {ids[0]} / {ids[1]}.", ev)],
                summary=f"Escalated: conflicting decisions {ids[0]} / {ids[1]}.",
            )

        # 2. Ambiguous capability match -> one targeted question.
        if ambiguous:
            titles = [c.get("title") or facts.title_of(c["id"]) for c in competing]
            ev = facts.valid_evidence([r["id"] for c in competing for r in c.get("evidence") or []] + [r["id"] for c in competing for r in facts.doc_evidence([c["id"]])[:1]])
            if ctx.clarifications >= 2:
                return self._escalate_unclear(facts, ev, f"Still ambiguous after {ctx.clarifications} clarifications between: {', '.join(titles)}.")
            question = f"To make sure we help with the right thing: do you mean {' or '.join(titles)}?"
            return Draft(
                kind="clarify",
                payload={"question": _clip(question, 2000), "missing_fields": ["capability"]},
                evidence_ids=ev,
                explanation=_clip(f"The request matches {len(titles)} capabilities equally ({', '.join(titles)}) and no document, work item or prior request separates them; choosing one would be a guess.{coverage_text}", 6000),
                rejected_alternatives=[_alt("create_work", "An underspecified task would be created just to complete the workflow.", [])],
                summary=f"Clarification: ambiguous between {', '.join(titles)}.",
            )

        # 3. Nothing decisive to work from.
        if not caps:
            missing = [m for m in facts.missing_information if "capability" in m.lower()][:3] or ["affected capability or product area"]
            ev = facts.valid_evidence(r["id"] for r in facts.bundle.get("sources") or [] if r.get("source_id") == req.get("source_id"))
            if ctx.clarifications >= 2:
                return self._escalate_unclear(facts, ev, "No capability could be established after clarification.")
            return Draft(
                kind="clarify",
                payload={"question": "Could you tell us which product area or feature this concerns, and what outcome you expect?", "missing_fields": [_clip(m, 200) for m in missing]},
                evidence_ids=ev,
                explanation=f"No capability matched the request with supporting evidence.{coverage_text}",
                rejected_alternatives=[_alt("create_work", "No capability or owner could be identified for a task.", [])],
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
            rejected.append(_alt(
                "answer",
                f"Procedure '{a['procedure_key']}' already failed in scope {a['scope']['entity_type']}:{a['scope']['entity_id']} under context '{a['context_signature']}' (attempt {a['id']}); not repeating it blindly.",
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
                rejected.append(_alt("link_work", f"Work '{w['title']}' ({w['id']}) is cancelled; it will not deliver this request.", facts.valid_evidence(w.get("evidence_ids") or [])))
        targets.sort(key=lambda w: (-facts.work_relevance(w["id"], caps), w.get("status") == "done", facts.age_days(w.get("observed_at")) or 0.0))
        work_strength = facts.work_relevance(targets[0]["id"], caps) if targets else 0.0
        docs = facts.doc_sources(caps)
        gaps = facts.doc_gaps(caps)
        best_resolution = max(usable, key=lambda a: facts.score_of(a.get("request_id", "")), default=None)
        resolution_strength = facts.score_of(best_resolution.get("request_id", "")) if best_resolution else 0.0
        doc_strength = max((facts.doc_relevance(sid, sc, caps) for sid, sc in docs), default=0.0) if not failed and not gaps else 0.0
        # A docs-only answer must clearly beat matching work; a verified prior resolution only has to match it.
        answer_wins = (docs and resolution_strength > work_strength) or (facts.request_is_question() and doc_strength > 1.2 * work_strength)

        # 4. Answer: documentation (plus a verified prior resolution when one applies) beats the best work match.
        if answer_wins and permitted("answer") and not gaps:
            refs = facts.doc_evidence(caps)
            if refs and not facts.doc_gaps(caps):
                return self._answer(facts, caps, refs, best_resolution if resolution_strength and resolution_strength >= doc_strength else None, rejected, coverage_text)

        # 5. Existing compatible work.
        if targets and permitted("link_work"):
            target = targets[0]
            if target["id"] not in facts.expanded_targets:  # graph edges survive a replan; only state is re-read
                r = yield from self._call(ctx, "expand_context", {"entity_ids": [target["id"]], "relation_types": ["DEPENDS_ON", "OWNED_BY", "IMPLEMENTED_BY"], "depth": 4, "limit": 40})
                facts.ingest("expand_context", r)
                facts.expanded_targets.add(target["id"])
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
                payload={"reason": _clip(f"The only verified remedy (attempt {a['id']}) is forbidden for this scope by decision {d['id']}, and no existing work covers the request.", 4000), "owner_id": routing, "team_id": None},
                evidence_ids=ev,
                explanation=_clip(f"A verified procedure would address {cap_titles}, but decision {d['id']} forbids it ({_clip(d.get('rationale', ''), 300)}). No alternative was found.{coverage_text}", 6000),
                rejected_alternatives=(rejected + [_alt("answer", f"Remedy from attempt {a['id']} is forbidden by decision {d['id']}.", ev)])[:5],
                summary=f"Escalated: forbidden workaround with no alternative ({d['id']}).",
            )

        # 7. Absence of work cannot be inferred from incomplete retrieval: check the capability's work directly.
        complete = facts.complete_neighbourhood.get(caps[0])
        if complete is None:
            r = yield from self._call(ctx, "expand_context", {"entity_ids": [caps[0]], "relation_types": ["IMPLEMENTED_BY", "OWNED_BY", "SUPPORTED_BY"], "depth": 1, "limit": 100})
            facts.ingest("expand_context", r)
            complete = r["ok"] and not ((r.get("data") or {}).get("truncated") and len((r.get("data") or {}).get("paths") or []) >= 100)
        if not complete:
            return self._escalate_unclear(
                facts, facts.valid_evidence(facts.known_evidence_ids[:5]),
                f"Coverage incomplete (work implementing {cap_titles} could not be fully listed): absence of existing work cannot be established, so new work is not proposed.",
            )

        # 8. Genuinely new work.
        if not permitted("create_work"):
            return self._escalate_policy(facts, decisions, "create_work")
        return self._create(ctx, facts, caps, similar, decisions, rejected, gaps, coverage_text)

    # -- drafts -----------------------------------------------------------------------------------------
    def _answer(self, facts: Facts, caps: list[str], refs: list[dict[str, Any]], resolution: dict[str, Any] | None, rejected: list[dict[str, Any]], coverage_text: str) -> Draft:
        req = facts.request
        cap_titles = ", ".join(facts.title_of(c) for c in caps)
        best_ref = max(refs[:5], key=lambda r: len(tokens(r.get("excerpt")) & tokens(facts.request_text())))
        ev = facts.valid_evidence([r["id"] for r in refs[:3]] + list((resolution or {}).get("evidence_ids") or []))
        parts = [
            f"Thanks for reaching out about \"{_clip(req['subject'], 150)}\".",
            f"Our documentation ({best_ref['source_id']}) says: \"{_clip(best_ref['excerpt'], 700)}\"",
        ]
        if resolution:
            parts.append(f"A previously verified resolution in a matching context applies: {_clip(resolution['summary'], 500)}")
        parts.append("Please let us know whether this resolves it for you.")
        criteria = [_clip(f"Requester confirms that '{req['subject']}' is resolved", 1000)]
        if resolution:
            criteria.append(_clip(f"Verified procedure '{resolution['procedure_key']}' applies in the same scope", 1000))
        stale = [r for r in refs[:3] if r.get("stale")]
        return Draft(
            kind="answer",
            payload={"draft_text": _clip(" ".join(parts), 12000), "resolution_criteria": criteria},
            evidence_ids=ev,
            explanation=_clip(
                f"Capability {cap_titles} is documented in {best_ref['source_id']}"
                + (f" and attempt {resolution['id']} ({resolution['procedure_key']}) was verified in an applicable scope" if resolution else "")
                + f"; the documentation matches this request better than any open work item, so no task is needed.{' Some cited evidence is stale.' if stale else ''}{coverage_text}",
                6000,
            ),
            rejected_alternatives=(rejected + [_alt("create_work", "Documented resolution exists; a task would be unnecessary work.", ev[:3])])[:5],
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
        notes: list[str] = []
        owner_issues = []
        for wid in chain:
            status = (facts.work.get(wid) or {}).get("status")
            if status in ("done", "cancelled"):
                continue
            owners = facts.owners_of(wid)
            dead = [o for o in owners if o in inactive]
            if not owners:
                owner_issues.append((wid, "has no recorded owner"))
            elif len(dead) == len(owners):
                owner_issues.append((wid, f"is owned only by inactive {', '.join(dead)}"))
        own = facts.owners_of(target["id"])
        target_unowned = target.get("status") not in ("done", None) and (not own or all(o in inactive for o in own))
        if target_unowned and not routing_active:
            why = "has no owner" if not own else f"is owned only by inactive {', '.join(own)}"
            return Draft(
                kind="escalate",
                payload={"reason": _clip(f"Matching work '{target['title']}' ({target['id']}) {why} and no active routing owner applies; the request would wait on unowned work.", 4000), "owner_id": None, "team_id": None},
                evidence_ids=path_ev,
                explanation=_clip(f"Work {target['id']} ({target.get('status')}) matches {cap_titles}, but it {why}. "
                                  f"{self._state_notes(facts, target)}{coverage_text}", 6000),
                rejected_alternatives=(rejected + [_alt("link_work", f"Linking to {target['id']} leaves the request waiting on unowned work.", path_ev[:5])])[:5],
                summary=f"Escalated: matching work {target['id']} has no active owner.",
            )
        if target.get("status") == "done":
            notes.append("The work is already completed: this request now needs verification against the customer's case, not waiting.")
        notes.append(self._state_notes(facts, target))
        if len(chain) > 1:
            notes.append(f"Dependency path: {' -> '.join(chain)}.")
        for wid, why in owner_issues:
            notes.append(f"Critical-path item {wid} {why}" + (f"; routing decision {routing_decision['id']} names active owner {routing_active}." if routing_active and routing_decision else "; escalate ownership before relying on this path."))
        return Draft(
            kind="link_work",
            payload={"work_item_id": target["id"], "comment_text": None},
            evidence_ids=path_ev,
            explanation=_clip(
                f"Existing work '{target['title']}' ({target['id']}) is {target.get('status') or 'of unknown status'} and tracks {cap_titles}"
                f"{' (owner ' + ', '.join(own) + ')' if own else ''}. {' '.join(n for n in notes if n)}{coverage_text}",
                6000,
            ),
            rejected_alternatives=(rejected + [_alt("create_work", f"Existing work {target['id']} covers {cap_titles}; a new task would duplicate it.", path_ev[:5])])[:5],
            summary=f"Link to existing work {target['id']} ({target.get('status')}).",
        )

    @staticmethod
    def _state_notes(facts: Facts, w: dict[str, Any]) -> str:
        notes = []
        if w.get("status") is None:
            notes.append("Its current status is unknown (not observable through the tools); verify it before relying on this link.")
        if w.get("status") != "done":
            if w.get("forecast_at"):
                notes.append(f"A forecast of {w['forecast_at']} is recorded (observed {w.get('observed_at')}); it is not a commitment.")
            else:
                notes.append("No reliable delivery forecast is recorded; no date can be promised.")
        if facts.is_stale(w.get("observed_at")):
            days = facts.age_days(w.get("observed_at")) or 0
            notes.append(f"Its state was last observed {days:.0f} days before the reference time and may be stale; refresh before relying on it.")
        return " ".join(notes)

    def _create(self, ctx: PlanContext, facts: Facts, caps: list[str], similar: list[str], decisions: list[dict[str, Any]], rejected: list[dict[str, Any]], gaps: list[tuple[str, str]], coverage_text: str) -> Draft:
        req = facts.request
        cap = caps[0]
        cap_titles = ", ".join(facts.title_of(c) for c in caps)
        routing, routing_decision = facts.routing_owner(decisions)
        inactive = facts.inactive_people()
        cap_people = [o for o in facts.owners_of(cap) if facts.label_of(o) in ("person", "")]
        owner = routing if routing and routing not in inactive else next((p for p in cap_people if p not in inactive), None)
        if owner and routing_decision and owner == routing:
            owner_note = f"Owner {owner} per routing decision {routing_decision['id']}."
        elif owner:
            owner_note = f"Owner {owner} (capability owner)."
        else:
            dead = sorted(inactive & set(cap_people))
            owner_note = "Unassigned: no active accountable owner is recorded for this capability" + (f" (inactive: {', '.join(dead)})" if dead else "") + "."
        for wid in similar[:4]:
            w = facts.work.get(wid) or {}
            their = [c for c in facts.caps_of_work(wid) if c not in caps]
            reasons = []
            excluded = facts.excluded_by_request(wid, caps)
            if excluded:
                reasons.append(f"the request explicitly rules out {', '.join(sorted(excluded))}")
            if their and not any(c in caps for c in facts.caps_of_work(wid)):
                reasons.append(f"it tracks a different capability ({', '.join(facts.title_of(c) for c in their)})")
            if w.get("status") == "cancelled":
                reasons.append("it is cancelled")
            if not reasons:
                reasons.append(f"it matches weakly and has no confirmed link to {cap_titles}")
            cand_ev = [x["id"] for c in facts.candidates if c["id"] == wid for x in c.get("evidence") or []]
            ev = facts.valid_evidence(list(w.get("evidence_ids") or []) + cand_ev)
            rejected.append(_alt("link_work", f"'{facts.title_of(wid)}' ({wid}) is similar but {'; '.join(reasons)}.", ev))
        connector = "linear" if ctx.connector_state("linear") in ("enabled", "verified", "unverified") else "local"
        gap_ev = [r["id"] for r in facts.evidence.values() if r.get("source_id") in {g[0] for g in gaps}][:2]
        ev = facts.valid_evidence(
            [r["id"] for r in facts.bundle.get("sources") or [] if r.get("source_id") == req.get("source_id")]
            + gap_ev
            + [r["id"] for r in facts.doc_evidence(caps)[:2]]
            + (list(routing_decision.get("evidence_ids") or []) if routing_decision else [])
        ) or facts.valid_evidence(facts.known_evidence_ids[:3])
        gap_text = f" Documentation states: \"{_clip(gaps[0][1], 300)}\"." if gaps else ""
        description = (
            f"Customer request {req['id']} ({req['customer_id']}): {_clip(req['body'], 1500)}\n\n"
            f"Capability: {cap_titles}.{gap_text} {owner_note}\n"
            f"Similar work reviewed and rejected: {', '.join(s for s in similar[:4]) or 'none'}.\n"
            f"Intent marker: {intent_marker(req['id'], str((ctx.run or {}).get('id')), cap, req['subject'])}"
        )
        return Draft(
            kind="create_work",
            payload={
                "title": _clip(f"{req['subject']} ({facts.title_of(cap)})", 300),
                "description": _clip(description, 16000),
                "acceptance_criteria": [
                    _clip(f"{facts.title_of(cap)} supports the requested behaviour: {req['subject']}", 2000),
                    _clip(f"Requester of {req['id']} confirms the outcome (attributable verification)", 2000),
                    "Documentation is updated with a citable source revision",
                ],
                "owner_id": owner,
                "capability_id": cap,
                "dependency_ids": [],
                "connector": connector,
            },
            evidence_ids=ev,
            explanation=_clip(
                f"No open or completed work covering this request was found for {cap_titles} after search and graph expansion.{gap_text} "
                f"{owner_note} Destination connector: {connector}.{coverage_text}",
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
            rejected_alternatives=[_alt("create_work", "Evidence is insufficient to justify new work.", [])],
            summary=_clip(f"Escalated: {reason}", 300),
        )

    @staticmethod
    def _escalate_policy(facts: Facts, decisions: list[dict[str, Any]], kind: str) -> Draft:
        ds = [d for d in decisions if d["rule_type"] == "allowed_action_types"]
        ev = facts.valid_evidence(e for d in ds for e in d.get("evidence_ids") or [])
        reason = f"Action kind {kind} is not permitted by active decision(s) {', '.join(d['id'] for d in ds)}."
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
                body = f"{action.get('connector')} receipt {target_id} observed; current work state not available in request context."
                verification, ev, stage = "inconclusive", [], "observe"
            else:
                st = work.get("status")
                if st == "done":
                    body = f"Work '{work['title']}' ({work['id']}) is completed as observed at {work.get('observed_at')}. Customer resolution is NOT verified; request needs operator/customer verification."
                    stage = "verify"
                elif st == "cancelled":
                    body = f"Work '{work['title']}' ({work['id']}) is cancelled; it will not deliver this request. Reinvestigation or escalation required."
                    stage = "observe"
                else:
                    body = f"Work '{work['title']}' ({work['id']}) is {st} as observed at {work.get('observed_at')}; request awaits the work. No delivery date is implied."
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
