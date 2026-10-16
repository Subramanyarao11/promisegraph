"""LinearConnector: create/readback/search-by-marker against Linear's GraphQL API.

Disabled unless LINEAR_ENABLED=true with an API key and team id: a disabled connector raises CapabilityDisabled
before any network I/O. GraphQL `errors` are checked even on HTTP 200. Rate limits back off. A transport timeout or
5xx on a create is an *ambiguous* outcome (the issue may exist): callers report `unknown` and reconcile by the stable
action marker embedded in the description; marker-search absence is treated as inconclusive, never as proof.

Status: implemented; unverified against a live Linear workspace (no credentials provisioned).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..http import Backoff, sha256_hex
from ..markers import action_marker
from ..planner.base import CapabilityDisabled

ISSUE_FIELDS = "id identifier url title description updatedAt state { name type } assignee { id }"

# Linear workflow state types -> WorkStatus
STATE_MAP = {"triage": "open", "backlog": "open", "unstarted": "open", "started": "in_progress", "completed": "done", "canceled": "cancelled"}


class LinearError(Exception):
    def __init__(self, kind: str, message: str, *, code: str = "LINEAR_ERROR", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # definitive | ambiguous | rate_limited
        self.code = code
        self.retry_after = retry_after


@dataclass
class LinearIssue:
    id: str
    identifier: str
    url: str | None
    title: str
    state_type: str | None
    state_name: str | None
    updated_at: str | None
    raw: dict[str, Any]

    @property
    def work_status(self) -> str:
        return STATE_MAP.get(self.state_type or "", "open")

    def receipt(self, receipt_id: str, observed_at: str) -> dict[str, Any]:
        return {
            "id": receipt_id[:128],
            "connector": "linear",
            "target_id": self.id[:128],
            "target_url": self.url,
            "observed_at": observed_at,
            "result_hash": sha256_hex({"id": self.id, "identifier": self.identifier, "title": self.title, "state": self.state_type, "updatedAt": self.updated_at}),
        }




class LinearConnector:
    name = "linear"

    def __init__(
        self,
        *,
        enabled: bool,
        api_key: str,
        team_id: str,
        url: str = "https://api.linear.app/graphql",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        backoff: Backoff | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = enabled and bool(api_key) and bool(team_id)
        self.team_id = team_id
        self.url = url
        self.backoff = backoff or Backoff(base=1.0, cap=30.0, attempts=4)
        self.clock = clock
        self._client = (
            httpx.Client(timeout=timeout, transport=transport, headers={"Authorization": api_key, "Content-Type": "application/json"})
            if self.enabled
            else None
        )

    @property
    def status(self) -> str:
        return "unverified" if self.enabled else "disabled"

    def _require(self) -> httpx.Client:
        if not self.enabled or self._client is None:
            raise CapabilityDisabled("linear connector is disabled or unconfigured (LINEAR_ENABLED/LINEAR_API_KEY/LINEAR_TEAM_ID)")
        return self._client

    # -- transport -------------------------------------------------------------------------------------
    def graphql(self, query: str, variables: dict[str, Any], *, mutation: bool = False) -> dict[str, Any]:
        client = self._require()
        for attempt in range(self.backoff.attempts):
            try:
                resp = client.post(self.url, json={"query": query, "variables": variables})
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # request never reached Linear: safe to retry even for a mutation
                err = LinearError("unsent", f"connect failed: {exc}", code="DEPENDENCY_UNAVAILABLE")
                if attempt + 1 < self.backoff.attempts:
                    self.backoff.sleep(self.backoff.delay(attempt))
                    continue
                raise err from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
                if mutation:
                    raise LinearError("ambiguous", f"timeout after sending mutation: {exc}", code="TIMEOUT") from exc
                if attempt + 1 < self.backoff.attempts:
                    self.backoff.sleep(self.backoff.delay(attempt))
                    continue
                raise LinearError("definitive", f"read timed out: {exc}", code="TIMEOUT") from exc
            retry_after = self._retry_after(resp)
            if resp.status_code == 429 or self._rate_limited(resp):
                if attempt + 1 < self.backoff.attempts:
                    self.backoff.sleep(self.backoff.delay(attempt, retry_after))
                    continue
                raise LinearError("rate_limited", "Linear rate limit", code="RATE_LIMITED", retry_after=retry_after)
            if resp.status_code >= 500:
                if mutation:
                    raise LinearError("ambiguous", f"Linear HTTP {resp.status_code} after mutation", code="DEPENDENCY_UNAVAILABLE")
                if attempt + 1 < self.backoff.attempts:
                    self.backoff.sleep(self.backoff.delay(attempt))
                    continue
                raise LinearError("definitive", f"Linear HTTP {resp.status_code}", code="DEPENDENCY_UNAVAILABLE")
            if resp.status_code >= 400:
                raise LinearError("definitive", f"Linear HTTP {resp.status_code}", code="UNAUTHORIZED" if resp.status_code in (401, 403) else "LINEAR_ERROR")
            try:
                body = resp.json()
            except ValueError as exc:
                raise LinearError("ambiguous" if mutation else "definitive", "Linear returned non-JSON") from exc
            if body.get("errors"):  # GraphQL errors arrive with HTTP 200
                msgs = "; ".join(str(e.get("message", "error"))[:200] for e in body["errors"][:3])
                raise LinearError("definitive", f"GraphQL errors: {msgs}", code="LINEAR_GRAPHQL_ERROR")
            if not isinstance(body.get("data"), dict):
                raise LinearError("ambiguous" if mutation else "definitive", "GraphQL response without data")
            return body["data"]
        raise LinearError("definitive", "exhausted retries")  # pragma: no cover

    @staticmethod
    def _rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code != 200 and resp.status_code != 400:
            return False
        try:
            errs = resp.json().get("errors") or []
        except ValueError:
            return False
        return any(((e.get("extensions") or {}).get("code") == "RATELIMITED") for e in errs)

    def _retry_after(self, resp: httpx.Response) -> float | None:
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Requests-Reset")
        if reset:
            try:
                return max(0.0, int(reset) / 1000.0 - self.clock())
            except ValueError:
                return None
        return None

    @staticmethod
    def _issue(node: dict[str, Any] | None) -> LinearIssue | None:
        if not node or not node.get("id"):
            return None
        state = node.get("state") or {}
        return LinearIssue(
            id=node["id"],
            identifier=node.get("identifier") or node["id"],
            url=node.get("url"),
            title=node.get("title") or "",
            state_type=state.get("type"),
            state_name=state.get("name"),
            updated_at=node.get("updatedAt"),
            raw=node,
        )

    # -- operations ------------------------------------------------------------------------------------
    def create_issue(self, *, action_id: str, title: str, description: str) -> LinearIssue:
        """issueCreate with the action marker in the description. Raises LinearError(kind=ambiguous) on unknown outcome."""
        query = f"mutation IssueCreate($input: IssueCreateInput!) {{ issueCreate(input: $input) {{ success issue {{ {ISSUE_FIELDS} }} }} }}"
        desc = f"{description}\n\n{action_marker(action_id)}"
        data = self.graphql(query, {"input": {"teamId": self.team_id, "title": title[:255], "description": desc}}, mutation=True)
        payload = data.get("issueCreate") or {}
        issue = self._issue(payload.get("issue"))
        if not payload.get("success") or issue is None:
            # success=false or missing fields: the effect may still exist -> reconcile, never assume absence
            raise LinearError("ambiguous", "issueCreate returned without success/issue", code="LINEAR_INCOMPLETE")
        return issue

    def read_issue(self, issue_id: str) -> LinearIssue | None:
        query = f"query Issue($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}"
        return self._issue(self.graphql(query, {"id": issue_id}).get("issue"))

    def find_by_marker(self, action_id: str) -> list[LinearIssue]:
        query = (
            "query Marker($teamId: ID!, $marker: String!) { issues(first: 5, filter: { team: { id: { eq: $teamId } }, "
            f"description: {{ contains: $marker }} }}) {{ nodes {{ {ISSUE_FIELDS} }} }} }}"
        )
        data = self.graphql(query, {"teamId": self.team_id, "marker": action_marker(action_id)})
        nodes = ((data.get("issues") or {}).get("nodes")) or []
        return [i for i in (self._issue(n) for n in nodes) if i is not None and action_marker(action_id) in (i.raw.get("description") or "")]
