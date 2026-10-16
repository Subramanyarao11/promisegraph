"""Worker configuration from the repo-root `.env` plus process environment (environment wins)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _worker_workspaces(acl: str) -> list[str]:
    """Parse `WORKSPACE_ACL` (e.g. `operator:ws_demo,ws_other;worker:ws_demo,ws_other`) for the worker entry."""
    for part in acl.split(";"):
        role, _, spaces = part.partition(":")
        if role.strip() == "worker":
            return [s.strip() for s in spaces.split(",") if s.strip()]
    return []


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 12
    max_replans: int = 2
    max_active_seconds: int = 90
    max_tool_result_bytes: int = 24 * 1024
    max_checkpoint_bytes: int = 64 * 1024


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "http://localhost:8000"
    worker_token: str = ""
    worker_id: str = "worker_local"
    workspaces: tuple[str, ...] = ("ws_demo",)
    lease_seconds: int = 60
    http_timeout_seconds: float = 10.0
    idle_poll_min_seconds: float = 0.5
    idle_poll_max_seconds: float = 5.0
    ai_enabled: bool = False
    ai_provider: str = ""
    ai_model: str = ""  # required when AI is enabled (Rev 6 #38): no default model
    ai_api_key: str = field(default="", repr=False)
    ai_budget_usd: float = 8.0
    ai_price_input_per_mtok: float | None = None
    ai_price_output_per_mtok: float | None = None
    ai_ledger_path: str = ".ai-ledger.json"
    ai_max_output_tokens_per_call: int = 1500
    ai_max_calls_per_run: int = 12
    linear_enabled: bool = False
    linear_api_key: str = field(default="", repr=False)
    linear_team_id: str = ""
    linear_api_url: str = "https://api.linear.app/graphql"
    limits: Limits = Limits()

    @property
    def linear_configured(self) -> bool:
        return self.linear_enabled and bool(self.linear_api_key) and bool(self.linear_team_id)

    @property
    def model_configured(self) -> bool:
        return self.ai_enabled and bool(self.ai_api_key) and bool(self.ai_model)

    @property
    def ledger_path(self) -> Path:
        p = Path(self.ai_ledger_path)
        return p if p.is_absolute() else REPO_ROOT / p

    def model_config_errors(self) -> list[str]:
        """Reasons model mode must refuse to start (Rev 6 #38/#39). Empty when AI is disabled."""
        if not self.ai_enabled:
            return []
        errs = []
        if self.ai_provider not in ("openai", "anthropic"):
            errs.append("AI_PROVIDER must be openai or anthropic")
        if not self.ai_model:
            errs.append("AI_MODEL is required")
        if not self.ai_api_key:
            errs.append("AI_API_KEY is required")
        if self.ai_price_input_per_mtok is None or self.ai_price_output_per_mtok is None:
            errs.append("AI_PRICE_INPUT_PER_MTOK and AI_PRICE_OUTPUT_PER_MTOK are required (no built-in price table)")
        if not (0 < self.ai_budget_usd <= 10):
            errs.append(f"AI_BUDGET_USD must be in (0, 10]; got {self.ai_budget_usd}")
        return errs


def load_settings(env_file: Path | None = None, environ: dict[str, str] | None = None) -> Settings:
    merged = load_dotenv(env_file or REPO_ROOT / ".env")
    merged.update(environ if environ is not None else os.environ)
    workspaces = [s.strip() for s in merged.get("WORKER_WORKSPACES", "").split(",") if s.strip()]
    if not workspaces:
        workspaces = _worker_workspaces(merged.get("WORKSPACE_ACL", "")) or ["ws_demo"]
    return Settings(
        api_base_url=merged.get("API_BASE_URL", "http://localhost:8000").rstrip("/"),
        worker_token=merged.get("WORKER_TOKEN", ""),
        worker_id=merged.get("WORKER_ID", "") or f"worker_{os.uname().nodename.split('.')[0]}_{os.getpid()}"[:128],
        workspaces=tuple(workspaces),
        ai_enabled=_bool(merged.get("AI_ENABLED")),
        ai_provider=merged.get("AI_PROVIDER", ""),
        ai_model=merged.get("AI_MODEL", ""),
        ai_api_key=merged.get("AI_API_KEY", ""),
        ai_budget_usd=_float(merged.get("AI_BUDGET_USD")) if _float(merged.get("AI_BUDGET_USD")) is not None else 8.0,
        ai_price_input_per_mtok=_float(merged.get("AI_PRICE_INPUT_PER_MTOK")),
        ai_price_output_per_mtok=_float(merged.get("AI_PRICE_OUTPUT_PER_MTOK")),
        ai_ledger_path=merged.get("AI_LEDGER_PATH", "") or ".ai-ledger.json",
        ai_max_output_tokens_per_call=int(_float(merged.get("AI_MAX_OUTPUT_TOKENS_PER_CALL")) or 1500),
        ai_max_calls_per_run=int(_float(merged.get("AI_MAX_CALLS_PER_RUN")) or 12),
        linear_enabled=_bool(merged.get("LINEAR_ENABLED")),
        linear_api_key=merged.get("LINEAR_API_KEY", ""),
        linear_team_id=merged.get("LINEAR_TEAM_ID", ""),
    )
