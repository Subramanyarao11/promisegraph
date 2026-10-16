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
    ai_model: str = "claude-opus-5-5"
    ai_api_key: str = field(default="", repr=False)
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
        return self.ai_enabled and bool(self.ai_api_key)


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
        ai_model=merged.get("AI_MODEL", "") or "claude-opus-5-5",
        ai_api_key=merged.get("AI_API_KEY", ""),
        linear_enabled=_bool(merged.get("LINEAR_ENABLED")),
        linear_api_key=merged.get("LINEAR_API_KEY", ""),
        linear_team_id=merged.get("LINEAR_TEAM_ID", ""),
    )
