"""Plain-language wording for explanations and reasons shown to a support lead.

Rules: short sentences; titles and people's names; plain dates ("13 Oct"); no internal ids, scope keys, byte limits
or tool names. Traceability lives in evidence_ids and run events, not in this prose.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .facts import Facts

KIND_WORDS = {"answer": "sending an answer", "clarify": "asking a question", "link_work": "linking to existing work",
              "create_work": "creating new work", "escalate": "escalating"}


def clean_name(title: str | None) -> str:
    """'Maya (synthetic)' -> 'Maya'; keeps real names untouched."""
    return re.sub(r"\s*\([^)]*\)", "", str(title or "")).strip()


def day(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{d.day} {d.strftime('%b')}"


def quoted(title: str) -> str:
    return f"'{title}'"


class Words:
    def __init__(self, facts: Facts) -> None:
        self.f = facts

    def name(self, pid: str | None) -> str | None:
        """Display name when a tool returned one; None otherwise (never an id)."""
        if not pid:
            return None
        p = self.f.people.get(pid)
        if p and p.get("name"):
            return clean_name(p["name"])
        n = self.f.nodes.get(pid)
        if n and n.get("title") and n["title"] != pid:
            return clean_name(n["title"])
        return None

    def person(self, pid: str | None) -> str:
        return self.name(pid) or "the owner on record"

    def has_left(self, pid: str) -> bool:
        return pid in self.f.inactive_people()

    def work(self, wid: str) -> str:
        t = self.f.title_of(wid)
        return quoted(t) if t and t != wid else "the related work item"

    def cap(self, cid: str) -> str:
        t = self.f.title_of(cid)
        return t if t and t != cid else "this area"

    def caps(self, cids: list[str]) -> str:
        return " / ".join(self.cap(c) for c in cids) or "this area"

    def doc(self, sid: str) -> str:
        t = self.f.title_of(sid)
        return quoted(t) if t and t != sid else "our documentation"

    def customer(self, cid: str | None) -> str:
        n = self.f.nodes.get(cid or "")
        if n and n.get("title") and n["title"] != cid:
            return clean_name(n["title"])
        return "this customer" if cid == self.f.request.get("customer_id") else "another customer"

    def scope(self, scope: dict[str, Any]) -> str:
        et, eid = scope.get("entity_type"), scope.get("entity_id")
        if et == "customer":
            return f"for {self.customer(eid)}"
        if et == "capability":
            return f"for {self.cap(eid)}"
        if et == "work_item":
            return f"for {self.work(eid)}"
        return "for this request" if eid == self.f.request.get("id") else "for an earlier request"

    @staticmethod
    def procedure(attempt: dict[str, Any]) -> str:
        """The fix as people call it: a quoted phrase from the attempt summary, else the procedure name in words."""
        m = re.search(r"['‘“\"]([^'’”\"]{3,40})['’”\"]", attempt.get("summary") or "")
        if m:
            return quoted(m.group(1))
        words = str(attempt.get("procedure_key") or "that fix").replace("_", " ").strip()
        return quoted(words[:1].upper() + words[1:])

    @staticmethod
    def rule(decision: dict[str, Any]) -> str:
        text = re.sub(r"^\s*synthetic (policy|rule)\s*:\s*", "", str(decision.get("rationale") or ""), flags=re.I).strip().rstrip(".")
        return text or "a company rule"

    def owner_phrase(self, owners: list[str]) -> str:
        """'owner Maya Raman' / 'whose owner Dana Whitlock has left' style fragments."""
        if not owners:
            return "nobody owns it"
        names = [self.person(o) for o in owners]
        if all(self.has_left(o) for o in owners):
            return f"its owner {' and '.join(names)} has left"
        return f"owner {' and '.join(names)}"
