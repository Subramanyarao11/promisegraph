from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Contract error (contracts §9). `details` is a small JSON object (<= 50 keys)."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message[:2000] or code
        self.details = details or {}
        self.retryable = retryable


def invalid(message: str, fields: dict[str, str] | None = None) -> ApiError:
    return ApiError(422, "INVALID_INPUT", message, {"fields": fields or {}})


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} {ident} not found", {"entity": what, "id": ident})


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, code, message, details)


def policy(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(422, "POLICY_VIOLATION", message, details)


def evidence_invalid(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(422, "EVIDENCE_INVALID", message, details)


def unavailable(message: str = "Graph database unavailable") -> ApiError:
    return ApiError(503, "DEPENDENCY_UNAVAILABLE", message, {}, retryable=True)


class StoreGuardFailed(Exception):
    """A guarded single-query write was rejected by its in-query guard (rolled back by FalkorDB)."""
