"""Stable effect markers used to find an effect again after a lost response."""

from __future__ import annotations

import re

from .http import sha256_hex

INTENT_RE = re.compile(r"pg-intent-([0-9a-f]{16})")


def intent_marker(*parts: str) -> str:
    """Embedded in create_work descriptions (before the action id exists) so a timed-out local create can be found."""
    return f"pg-intent-{sha256_hex(':'.join(parts))[:16]}"


def action_marker(action_id: str) -> str:
    """Embedded in provider-side (Linear) descriptions once the action id exists."""
    return f"[promisegraph-action:{action_id}]"
