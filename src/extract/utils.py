"""Utility helpers shared by low-level parsers."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Iterable, cast


SESSION_TITLE_MAX_CHARS = 120


def as_dict(value: Any) -> dict[str, Any]:
    """Return *value* as a plain `dict[str, Any]` or an empty dict."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def as_list(value: Any) -> list[Any]:
    """Return *value* as a list or an empty list."""
    return cast(list[Any], value) if isinstance(value, list) else []


def normalize_session_title(value: Any, fallback: str = "", max_chars: int = SESSION_TITLE_MAX_CHARS) -> str:
    """Normalize a session title for safe storage and display."""
    text = str(value or "").strip()
    if not text:
        text = str(fallback or "").strip()
    if not text:
        return ""

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line:
        text = first_line

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text

    return text[: max(1, max_chars - 1)].rstrip() + "…"


def parse_timestamp_ms(value: Any) -> int | None:
    """Coerce common timestamp shapes to Unix milliseconds."""
    if value is None:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            value = int(stripped)
        else:
            try:
                dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)

    if isinstance(value, (int, float)):
        numeric = int(value)
        if numeric < 10_000_000_000:
            return numeric * 1000
        return numeric

    return None


def timestamp_ms_to_iso(timestamp_ms: int | None) -> str:
    """Render a Unix millisecond timestamp as ISO-8601."""
    if timestamp_ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return dt.isoformat()


def flatten_text_content(value: Any) -> str:
    """Best-effort text flattening for strings or content-block lists."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type", "")).strip()
            if block_type == "text" and isinstance(item.get("text"), str):
                if item["text"].strip():
                    parts.append(item["text"])
            elif block_type == "thinking" and isinstance(item.get("thinking"), str):
                if item["thinking"].strip():
                    parts.append(item["thinking"])
            elif isinstance(item.get("value"), str) and item["value"].strip():
                parts.append(item["value"])
            elif isinstance(item.get("content"), str) and item["content"].strip():
                parts.append(item["content"])
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "message", "output", "result"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
    return ""


def compact_join(values: Iterable[str]) -> str:
    """Join non-empty strings with newlines."""
    return "\n".join(value for value in values if value)