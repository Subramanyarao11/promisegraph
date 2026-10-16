"""Model provider adapters (the only provider-specific code; PROTOCOL.md Rev 6 #38).

Both adapters speak raw HTTPS through httpx and expose the same small interface:
`start(system, user)`, `complete(max_output_tokens) -> Reply`, `add_tool_results(results)`, `request_preview()`.
Errors: a body carrying an `error` object is an error even with HTTP 200; 429/5xx back off (Retry-After honoured);
other 4xx are definitive. A timeout after sending is "billed unknown" for the spend guard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import Backoff


@dataclass
class ToolUse:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    parse_error: str | None = None


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolUse]
    input_tokens: int
    output_tokens: int
    stop: str
    raw_usage: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str = "PROVIDER_ERROR", billed_unknown: bool = False, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.code, self.billed_unknown, self.retryable = status, code, billed_unknown, retryable


def _retry_after(resp: httpx.Response) -> float | None:
    for h in ("retry-after", "x-ratelimit-reset-requests"):
        v = resp.headers.get(h)
        if v:
            try:
                return float(v.rstrip("s"))
            except ValueError:
                continue
    return None


class _Base:
    url = ""

    def __init__(self, *, api_key: str, model: str, transport: httpx.BaseTransport | None = None, timeout: float = 60.0, backoff: Backoff | None = None) -> None:
        if not model:
            raise ValueError("AI_MODEL is required (no default model)")
        self.model = model
        self.backoff = backoff or Backoff(base=1.0, cap=20.0, attempts=4)
        self._client = httpx.Client(timeout=timeout, transport=transport, headers=self._headers(api_key))
        self.messages: list[dict[str, Any]] = []
        self.system = ""
        self.tools: list[dict[str, Any]] = []

    def _headers(self, api_key: str) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.backoff.attempts):
            try:
                resp = self._client.post(self.url, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # never reached the provider: not billed
                if attempt + 1 < self.backoff.attempts:
                    self.backoff.sleep(self.backoff.delay(attempt))
                    continue
                raise ProviderError(f"connect failed: {exc}", code="DEPENDENCY_UNAVAILABLE", retryable=True) from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
                raise ProviderError(f"timeout after sending: {exc}", code="TIMEOUT", billed_unknown=True, retryable=True) from exc
            try:
                data = resp.json()
            except ValueError:
                data = {}
            err = data.get("error") if isinstance(data, dict) else None
            if resp.status_code == 429 or resp.status_code >= 500 or (isinstance(err, dict) and str(err.get("type", "")) in ("overloaded_error", "rate_limit_error")):
                if attempt + 1 < self.backoff.attempts:
                    self.backoff.sleep(self.backoff.delay(attempt, _retry_after(resp)))
                    continue
                raise ProviderError(f"provider unavailable/rate limited ({resp.status_code})", status=resp.status_code,
                                    code="RATE_LIMITED" if resp.status_code == 429 else "DEPENDENCY_UNAVAILABLE",
                                    billed_unknown=resp.status_code >= 500, retryable=True)
            if resp.status_code >= 400 or err:
                msg = (err or {}).get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(f"provider error {resp.status_code}: {str(msg)[:300]}", status=resp.status_code, code="PROVIDER_ERROR")
            return data
        raise ProviderError("exhausted retries", code="DEPENDENCY_UNAVAILABLE", retryable=True)  # pragma: no cover

    def request_preview(self, max_output_tokens: int) -> dict[str, Any]:
        return self._body(max_output_tokens)

    def _body(self, max_output_tokens: int) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class OpenAIChatProvider(_Base):
    """OpenAI Chat Completions with function tools (`tools[].type = "function"`)."""

    url = "https://api.openai.com/v1/chat/completions"

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def start(self, system: str, user: str, tools: list[dict[str, Any]]) -> None:
        self.system = system
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]

    def _body(self, max_output_tokens: int) -> dict[str, Any]:
        return {"model": self.model, "messages": self.messages, "tools": self.tools, "tool_choice": "auto",
                "parallel_tool_calls": False, "max_completion_tokens": max_output_tokens}

    def complete(self, max_output_tokens: int) -> Reply:
        data = self._post(self._body(max_output_tokens))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args, perr = json.loads(raw), None
                if not isinstance(args, dict):
                    args, perr = {}, "arguments are not a JSON object"
            except ValueError as exc:
                args, perr = {}, f"invalid JSON arguments: {exc}"
            calls.append(ToolUse(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args, raw_arguments=raw, parse_error=perr))
        self.messages.append({"role": "assistant", "content": msg.get("content"), **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
        usage = data.get("usage") or {}
        return Reply(text=msg.get("content") or "", tool_calls=calls, input_tokens=int(usage.get("prompt_tokens", 0)),
                     output_tokens=int(usage.get("completion_tokens", 0)), stop=choice.get("finish_reason") or "", raw_usage=usage)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for call_id, content, _is_error in results:
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


class AnthropicMessagesProvider(_Base):
    """Anthropic Messages API with client tools (kept behind the same interface)."""

    url = "https://api.anthropic.com/v1/messages"

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def start(self, system: str, user: str, tools: list[dict[str, Any]]) -> None:
        self.system = system
        self.messages = [{"role": "user", "content": user}]
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools]

    def _body(self, max_output_tokens: int) -> dict[str, Any]:
        return {"model": self.model, "max_tokens": max_output_tokens, "system": self.system, "messages": self.messages, "tools": self.tools}

    def complete(self, max_output_tokens: int) -> Reply:
        data = self._post(self._body(max_output_tokens))
        blocks = data.get("content") or []
        calls = [ToolUse(id=b.get("id", ""), name=b.get("name", ""), arguments=b.get("input") if isinstance(b.get("input"), dict) else {}) for b in blocks if b.get("type") == "tool_use"]
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        self.messages.append({"role": "assistant", "content": blocks})
        usage = data.get("usage") or {}
        return Reply(text=text, tool_calls=calls, input_tokens=int(usage.get("input_tokens", 0)), output_tokens=int(usage.get("output_tokens", 0)),
                     stop=data.get("stop_reason") or "", raw_usage=usage)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        self.messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": content, "is_error": is_err} for cid, content, is_err in results]})


PROVIDERS = {"openai": OpenAIChatProvider, "anthropic": AnthropicMessagesProvider}
