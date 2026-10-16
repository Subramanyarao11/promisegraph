"""LocalTaskConnector: local effects are applied atomically by the API (`execute-local`); the worker only drives them.

Real receipts come from the API. An ambiguous failure (timeout / retryable 5xx after the effect may have landed) is
retried with the *same* Idempotency-Key and body: the API dedupes by action marker and reads the receipt back, so
this never blindly recreates. If the outcome still cannot be read back, the dispatch reports `unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..http import ApiClient, ApiError, ApiTimeout, ApiUnavailable, LeaseLost, short_key
from ..jobs.lease import LeaseKeeper


@dataclass
class LocalResult:
    execution_state: str  # succeeded | failed | unknown
    action: dict[str, Any] | None
    receipt: dict[str, Any] | None
    error: dict[str, Any] | None


class LocalTaskConnector:
    name = "local"

    def __init__(self, api: ApiClient) -> None:
        self.api = api

    def execute(self, keeper: LeaseKeeper, action: dict[str, Any], *, max_replays: int = 3) -> LocalResult:
        ws = keeper.workspace_id
        body = {"job_id": keeper.job_id, **keeper.binding(), "expected_action_version": int(action["version"])}
        key = short_key("exec-local", action["id"], keeper.fencing_token, action["version"])
        last: dict[str, Any] | None = None
        for attempt in range(max_replays):
            if attempt:
                self.api.backoff.sleep(self.api.backoff.delay(attempt - 1))
            keeper.ensure()
            try:
                data, _ = self.api.call(
                    "POST",
                    ApiClient.private(ws, f"/actions/{action['id']}/execute-local"),
                    json_body=body,
                    idempotency_key=key,
                    retry=False,
                )
            except ApiError as err:
                if err.code == "LEASE_LOST":
                    keeper.mark_lost("execute-local rejected: LEASE_LOST")
                    raise LeaseLost(err.message) from err
                if err.status >= 500 or err.status == 429:
                    last = {"code": err.code, "message": err.message}
                    continue  # ambiguous: replay with the same key (server dedupes by action marker)
                # revalidation failure / invalid transition: definitive, nothing dispatched
                return LocalResult("failed", None, None, {"code": err.code, "message": err.message})
            except (ApiTimeout, ApiUnavailable) as exc:
                last = {"code": "TIMEOUT" if isinstance(exc, ApiTimeout) else "DEPENDENCY_UNAVAILABLE", "message": str(exc)}
                continue
            action_out = data or {}
            receipt = action_out.get("receipt")
            if action_out.get("status") == "succeeded" and receipt:
                return LocalResult("succeeded", action_out, receipt, None)
            return LocalResult("unknown", action_out, None, {"code": "NO_RECEIPT", "message": f"action is {action_out.get('status')} without receipt"})
        return LocalResult("unknown", None, None, last or {"code": "UNKNOWN", "message": "outcome could not be read back"})
