"""Dispatch, reconcile and refresh jobs (connector I/O happens only here, after accepted intent is persisted)."""

from __future__ import annotations

from typing import Any

from ..config import Limits
from ..connectors.linear import LinearConnector, LinearError
from ..connectors.local import LocalTaskConnector
from ..http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, LeaseLost, canonical_json, sha256_hex, short_key
from ..planner.base import CapabilityDisabled
from .lease import LeaseKeeper, iso, now_utc, parse_ts
from .session import JobSession

from ..markers import INTENT_RE


class EffectRunner:
    def __init__(self, api: ApiClient, limits: Limits, local: LocalTaskConnector, linear: LinearConnector) -> None:
        self.api = api
        self.limits = limits
        self.local = local
        self.linear = linear

    def _get(self, ws: str, path: str, **kw: Any) -> Any:
        data, _ = self.api.call("GET", ApiClient.public(ws, path), **kw)
        return data

    def _get_env(self, ws: str, path: str) -> tuple[Any, dict[str, Any]]:
        return self.api.call("GET", ApiClient.public(ws, path))

    def _session(self, keeper: LeaseKeeper) -> tuple[JobSession, dict[str, Any] | None]:
        run = self._get(keeper.workspace_id, f"/runs/{keeper.run_id}") if keeper.run_id else None
        return JobSession(self.api, keeper, limits=self.limits, run=run, mode=(run or {}).get("mode", "scripted")), run

    @staticmethod
    def _dispatch_result(action_id: str, state: str, receipt: dict[str, Any] | None, error: dict[str, Any] | None) -> dict[str, Any]:
        return {"action_id": action_id, "execution_state": state, "receipt": receipt, "error": error, "next_verification": "pending"}

    # -- dispatch -----------------------------------------------------------------------------------------
    def dispatch(self, keeper: LeaseKeeper) -> dict[str, Any]:
        ws = keeper.workspace_id
        session, run = self._session(keeper)
        action, env = self._get_env(ws, f"/actions/{keeper.action_id}")
        aid = action["id"]
        status = action["status"]
        if status == "succeeded" and action.get("receipt"):
            return self._complete(session, "succeeded", self._dispatch_result(aid, "succeeded", action["receipt"], None))
        if status in ("failed", "cancelled"):
            return self._complete(session, "failed", self._dispatch_result(aid, status, None, action.get("error") or {"code": status.upper(), "message": f"action {status}"}))
        if status == "unknown":
            return self._complete(session, "unknown", self._dispatch_result(aid, "unknown", None, {"code": "UNKNOWN", "message": "awaiting reconciliation"}))
        if status == "accepted" and (run or {}).get("status") == "cancelled":
            # Not yet started: cancellation stops it. (An effect already in flight would still be recorded.)
            return self._complete(session, "failed", self._dispatch_result(aid, "cancelled", None, {"code": "CANCELLED", "message": "run cancelled before dispatch"}))
        if action["connector"] == "local":
            res = self.local.execute(keeper, action, max_replays=1)
            if res.execution_state == "succeeded":
                return self._complete(session, "succeeded", self._dispatch_result(aid, "succeeded", res.receipt, None))
            if res.execution_state == "failed":
                return self._complete(session, "failed", self._dispatch_result(aid, "failed", None, res.error))
            # Ambiguous: the effect may exist. Never re-run blindly; reconcile first.
            return self._complete(session, "unknown", self._dispatch_result(aid, "unknown", None, res.error))
        return self._dispatch_linear(keeper, session, action, env)

    def _dispatch_linear(self, keeper: LeaseKeeper, session: JobSession, action: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
        ws = keeper.workspace_id
        aid = action["id"]
        if not self.linear.enabled:
            return self._complete(session, "failed", self._dispatch_result(aid, "failed", None, {"code": "CAPABILITY_DISABLED", "message": "linear connector is not configured in this worker"}))
        proposal = self._get(ws, f"/proposals/{action['proposal_id']}")
        if action["status"] == "dispatching":
            # A previous holder may have sent the create: look before acting.
            return self._linear_lookup_then(session, action, proposal, redispatch=False)
        remote: list[dict[str, Any]] = []
        target = None
        if proposal["kind"] == "link_work":
            work = self._get(ws, f"/work-items/{proposal['payload']['work_item_id']}")
            if work.get("external_id"):
                target = self.linear.read_issue(work["external_id"])
                if target:
                    remote.append({"target_id": target.id[:128], "provider_version": target.updated_at, "observed_at": iso(now_utc())})
        revision = ((env.get("meta") or {}).get("knowledge_revision"))
        body = {
            "job_id": keeper.job_id,
            **keeper.binding(),
            "expected_action_version": int(action["version"]),
            "expected_revision": int(revision if isinstance(revision, int) else proposal["based_on_revision"]),
            "remote_preconditions": remote,
        }
        keeper.ensure()
        try:
            permit, _ = self.api.call(
                "POST", ApiClient.private(ws, f"/actions/{aid}/begin-dispatch"), json_body=body,
                idempotency_key=short_key("begin", aid, keeper.fencing_token, action["version"]),
            )
        except ApiError as err:
            if err.code == "LEASE_LOST":
                keeper.mark_lost("begin-dispatch LEASE_LOST")
                raise LeaseLost(err.message) from err
            return self._complete(session, "failed", self._dispatch_result(aid, "failed", None, {"code": err.code, "message": err.message}))
        if permit["payload_hash"] != sha256_hex(canonical_json(proposal["payload"])):
            return self._complete(session, "failed", self._dispatch_result(aid, "failed", None, {"code": "PAYLOAD_MISMATCH", "message": "permit payload hash differs from the validated proposal; not sending"}))
        expires = parse_ts(permit["expires_at"])
        if expires and now_utc() >= expires:
            return self._complete(session, "unknown", self._dispatch_result(aid, "unknown", None, {"code": "PERMIT_EXPIRED", "message": "permit expired before send; nothing sent but action is dispatching"}))
        try:
            if proposal["kind"] == "create_work":
                pl = proposal["payload"]
                issue = self.linear.create_issue(action_id=aid, title=pl["title"], description=self._linear_description(pl))
                issue = self.linear.read_issue(issue.id) or issue  # readback
            elif target is not None:
                issue = target
            else:
                return self._complete(session, "failed", self._dispatch_result(aid, "failed", None, {"code": "POLICY_VIOLATION", "message": f"no linear effect for kind {proposal['kind']}"}))
        except LinearError as err:
            if err.kind == "ambiguous":
                return self._complete(session, "unknown", self._dispatch_result(aid, "unknown", None, {"code": err.code, "message": str(err)[:500]}))
            return self._complete(session, "failed", self._dispatch_result(aid, "failed", None, {"code": err.code, "message": str(err)[:500]}))
        receipt = issue.receipt(f"receipt_{aid}", iso(now_utc()))
        return self._complete(session, "succeeded", self._dispatch_result(aid, "succeeded", receipt, None))

    @staticmethod
    def _linear_description(pl: dict[str, Any]) -> str:
        crit = "\n".join(f"- {c}" for c in pl.get("acceptance_criteria") or [])
        return f"{pl['description']}\n\nAcceptance criteria:\n{crit}"

    def _linear_lookup_then(self, session: JobSession, action: dict[str, Any], proposal: dict[str, Any], *, redispatch: bool) -> dict[str, Any]:
        aid = action["id"]
        try:
            found = self.linear.find_by_marker(aid)
        except LinearError as err:
            return self._complete(session, "unknown", self._dispatch_result(aid, "unknown", None, {"code": err.code, "message": str(err)[:500]}))
        if len(found) >= 1:
            issue = self.linear.read_issue(found[0].id) or found[0]
            return self._complete(session, "succeeded", self._dispatch_result(aid, "succeeded", issue.receipt(f"receipt_{aid}", iso(now_utc())), None))
        return self._complete(session, "unknown", self._dispatch_result(aid, "unknown", None, {"code": "NOT_FOUND_INCONCLUSIVE", "message": "marker search found nothing; absence is not proof"}))

    # -- reconcile ----------------------------------------------------------------------------------------
    def reconcile(self, keeper: LeaseKeeper) -> dict[str, Any]:
        ws = keeper.workspace_id
        session, _run = self._session(keeper)
        action = self._get(ws, f"/actions/{keeper.action_id}")
        aid = action["id"]
        lookup: list[str] = []

        def result(established: bool, receipt: dict[str, Any] | None) -> dict[str, Any]:
            return {"action_id": aid, "lookup_evidence": lookup[:20], "established": established, "receipt": receipt}

        if action["status"] == "succeeded" and action.get("receipt"):
            lookup.append(f"action {aid} already has receipt {action['receipt']['target_id']}")
            return self._complete(session, "succeeded", result(True, action["receipt"]))
        proposal = self._get(ws, f"/proposals/{action['proposal_id']}")
        if action["connector"] == "linear":
            if not self.linear.enabled:
                lookup.append("linear connector disabled in this worker; cannot look up provider state")
                return self._complete(session, "unknown", result(False, None))
            try:
                found = self.linear.find_by_marker(aid)
            except (LinearError, CapabilityDisabled) as err:
                lookup.append(f"marker search failed: {err}")
                return self._complete(session, "unknown", result(False, None))
            lookup.append(f"linear marker search for {aid}: {len(found)} issue(s)")
            if found:
                issue = self.linear.read_issue(found[0].id) or found[0]
                if len(found) > 1:
                    lookup.append("multiple issues carry the marker: " + ", ".join(i.identifier for i in found))
                return self._complete(session, "succeeded", result(True, issue.receipt(f"receipt_{aid}", iso(now_utc()))))
            lookup.append("no issue found; search absence is inconclusive -> operator reconciliation")
            return self._complete(session, "unknown", result(False, None))
        # local connector: look the effect up by its stable intent marker
        if proposal["kind"] == "create_work":
            m = INTENT_RE.search(proposal["payload"].get("description", ""))
            if m:
                marker = m.group(0)
                try:
                    data, _ = self.api.call("POST", ApiClient.public(ws, "/context/search"), json_body={"query": m.group(1), "entity_types": ["work_item"], "customer_id": None, "limit": 5})
                except (ApiError, ApiUnavailable, ApiTimeout) as err:
                    lookup.append(f"work search failed: {err}")
                    return self._complete(session, "unknown", result(False, None))
                hits = [
                    c for c in (data or {}).get("candidates") or []
                    if any(marker in (ev.get("excerpt") or "") for ev in c.get("evidence") or [])
                ]
                lookup.append(f"local work search for marker {marker}: {len(hits)} match(es)")
                if len(hits) == 1:
                    lookup.append(f"found {hits[0]['id']}")
                    return self._complete(session, "succeeded", result(True, None))
                if len(hits) > 1:
                    lookup.append("multiple matches; uniqueness cannot be established")
                return self._complete(session, "unknown", result(False, None))
        if proposal["kind"] == "link_work":
            req = self._get(ws, f"/requests/{action['request_id']}")
            wid = proposal["payload"]["work_item_id"]
            lookup.append(f"request {req['id']} linked_work_ids contains {wid}: {wid in (req.get('linked_work_ids') or [])}")
            if wid in (req.get("linked_work_ids") or []):
                return self._complete(session, "succeeded", result(True, None))
        lookup.append(f"no reliable lookup for local {proposal['kind']} effect -> operator reconciliation")
        return self._complete(session, "unknown", result(False, None))

    # -- refresh ------------------------------------------------------------------------------------------
    def refresh(self, keeper: LeaseKeeper) -> dict[str, Any]:
        ws = keeper.workspace_id
        session, _run = self._session(keeper)
        cp = keeper.checkpoint or {}
        request_id = cp.get("request_id")
        work_ids: list[str] = []
        observation_ids: list[str] = []
        notes: list[str] = []
        if request_id:
            req = self._get(ws, f"/requests/{request_id}")
            ctx = self._get(ws, f"/requests/{request_id}/context")
            for w in (ctx or {}).get("related_work") or []:
                if w["id"] not in (req.get("linked_work_ids") or []):
                    continue
                work_ids.append(w["id"])
                if w.get("connector") != "linear" or not w.get("external_id"):
                    continue
                if not self.linear.enabled:
                    notes.append(f"{w['id']}: linear disabled; external state not refreshed")
                    continue
                try:
                    issue = self.linear.read_issue(w["external_id"])
                except LinearError as err:
                    notes.append(f"{w['id']}: readback failed ({err.code})")
                    continue
                if issue is None:
                    notes.append(f"{w['id']}: issue not found")
                    continue
                req = self._get(ws, f"/requests/{request_id}")
                body = f"Linear {issue.identifier} is '{issue.state_name}' ({issue.work_status}) as read at {iso(now_utc())}; this is connector status, not customer verification."
                keeper.ensure()
                data, _ = self.api.call(
                    "POST", ApiClient.public(ws, f"/requests/{request_id}/observations"),
                    json_body={"action_id": None, "kind": "connector_status", "verification": "pending", "body": body, "evidence_ids": [], "expected_request_version": req["version"]},
                    idempotency_key=short_key("refresh-obs", keeper.job_id, w["id"]), lease=keeper.headers(),
                )
                observation_ids.append(data["observation"]["id"])
        result = {"work_item_ids": work_ids, "observation_ids": observation_ids, "affected_request_ids": [request_id] if request_id else [], "notes": notes[:20]}
        return self._complete(session, "succeeded", result)

    # -- common -------------------------------------------------------------------------------------------
    @staticmethod
    def _complete(session: JobSession, outcome: str, result: dict[str, Any]) -> dict[str, Any]:
        receipt = session.complete(outcome, result)
        return {"outcome": outcome, "result": result, "receipt": receipt}
