from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def edge_id(rel_type: str, from_id: str, to_id: str) -> str:
    """Normalized edge identity: one edge per (type, from, to) — repeated links MERGE onto it."""
    return "e_" + sha256_hex(f"{rel_type}|{from_id}|{to_id}")[:32]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utcnow())


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def add_seconds(s: str | datetime, seconds: float) -> str:
    dt = parse_ts(s) if isinstance(s, str) else s
    return iso(dt + timedelta(seconds=seconds))


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        if not raw.startswith("o:"):
            raise ValueError
        off = int(raw[2:])
        if off < 0:
            raise ValueError
        return off
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid cursor") from exc
