"""Configuration from environment, with repo-root `.env` as defaults (never overriding real env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_DIR.parent.parent
REFERENCE_TIME = "2026-10-16T04:30:00Z"

# Server-owned workspace registry. The workspace id is never used as a raw graph name from input:
# it is looked up here, then mapped to `<prefix><workspace_id>`.
DEFAULT_WORKSPACES = {"ws_demo": "Demo workspace (synthetic)", "ws_other": "Isolation workspace (synthetic)"}


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _parse_acl(raw: str) -> dict[str, set[str]]:
    acl: dict[str, set[str]] = {}
    for part in raw.split(";"):
        if ":" not in part:
            continue
        role, wss = part.split(":", 1)
        acl[role.strip()] = {w.strip() for w in wss.split(",") if w.strip()}
    return acl


@dataclass
class Settings:
    db_host: str = "localhost"
    db_port: int = 6380
    graph_prefix: str = "pg_"
    worker_token: str = ""
    operator_token: str = ""
    acl: dict[str, set[str]] = field(default_factory=dict)
    clock_mode: str = "scenario"
    ai_enabled: bool = False
    ai_configured: bool = False
    linear_enabled: bool = False
    linear_configured: bool = False
    faults: set[str] = field(default_factory=set)
    contracts_dir: Path = REPO_ROOT / "contracts"
    workspaces: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_WORKSPACES))

    @classmethod
    def from_env(cls, overrides: dict[str, str] | None = None) -> "Settings":
        env = _load_dotenv(REPO_ROOT / ".env")
        env.update({k: v for k, v in os.environ.items()})
        if overrides:
            env.update(overrides)

        def b(name: str) -> bool:
            return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            db_host=env.get("DATABASE_HOST", "localhost"),
            db_port=int(env.get("DATABASE_PORT", "6380")),
            graph_prefix=env.get("DATABASE_GRAPH_PREFIX", "pg_") or "pg_",
            worker_token=env.get("WORKER_TOKEN", ""),
            operator_token=env.get("OPERATOR_TOKEN", ""),
            acl=_parse_acl(env.get("WORKSPACE_ACL", "operator:ws_demo,ws_other;worker:ws_demo,ws_other")),
            clock_mode="live" if env.get("CLOCK_MODE", "scenario") == "live" else "scenario",
            ai_enabled=b("AI_ENABLED"),
            ai_configured=bool(env.get("AI_PROVIDER") and env.get("AI_MODEL") and env.get("AI_API_KEY")),
            linear_enabled=b("LINEAR_ENABLED"),
            linear_configured=bool(env.get("LINEAR_API_KEY") and env.get("LINEAR_TEAM_ID")),
            faults={f.strip() for f in env.get("FAULTS", "").split(",") if f.strip()},
            contracts_dir=Path(env.get("CONTRACTS_DIR", str(REPO_ROOT / "contracts"))),
        )
