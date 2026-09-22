from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_hubspot_datetime(value: Any) -> datetime | None:
    """Parse HubSpot timestamps (epoch ms, epoch s, or ISO-ish strings) to UTC."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "undefined"}:
        return None

    if text.isdigit():
        number = int(text)
        if number >= 1_000_000_000_000:
            return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc)
        if number >= 1_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(text[:10] if len(text) >= 10 else text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
