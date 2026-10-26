"""Shared helpers for the CRM Beanie models."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware UTC now — matches the convention in lists.py / tags.py."""
    return datetime.now(timezone.utc)
