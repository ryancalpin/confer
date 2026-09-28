"""Validated node settings, shared by every remote surface (REST API, MCP,
phone app). Remote callers can never set things that run programs or read
local files — those stay CLI-only (``confer config``)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .node import Node

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CLI_ONLY = ("notify_cmd",)  # runs a program; a leaked token must not become code execution
SECRET = ("feed_token", "ui_token", "api_token")
EDITABLE = ("name", "endpoint", "relay", "tz", "hours_start", "hours_end", "buffer_minutes", "calendar", "tentative_holds",
            "trips_block_calendar", "currency", "pay_link", "notify_webhook", "nudge_after_hours")


class SettingsError(ValueError):
    pass


def public_view(node: "Node") -> dict[str, Any]:
    cfg = {k: node.config.get(k) for k in EDITABLE}
    cfg["notify_cmd_set"] = bool(node.config.get("notify_cmd"))
    return cfg


def _url(value: Any, what: str, *, query: bool, https: bool = False) -> str:
    from .node import NodeError, check_url

    if not value:
        return ""
    try:
        url = check_url(str(value), allow_query=query)
    except NodeError as exc:
        raise SettingsError(f"{what}: {exc}") from exc
    if https and not url.startswith("https://"):
        raise SettingsError(f"{what} must start with https://")
    return url


def validate(key: str, value: Any, *, remote: bool = True) -> Any:
    """Return the cleaned value for ``key`` or raise SettingsError."""
    if key in CLI_ONLY and remote:
        raise SettingsError(f"{key} can only be set with the CLI")
    if key in SECRET:
        raise SettingsError(f"{key} is managed by Confer")
    if key not in EDITABLE and key not in CLI_ONLY:
        raise SettingsError(f"unknown setting {key!r}")
    v = value.strip() if isinstance(value, str) else value
    if key == "name":
        if not isinstance(v, str) or not 1 <= len(v) <= 60:
            raise SettingsError("name must be 1-60 characters")
        return v
    if key in ("endpoint", "relay"):
        return _url(v, key, query=False)
    if key in ("pay_link",):
        return _url(v, key, query=True, https=True)
    if key == "notify_webhook":
        return _url(v, key, query=True)
    if key == "calendar":
        if not v:
            return ""
        s = str(v)
        if s.startswith("webcal://"):
            s = "https://" + s[len("webcal://"):]
            _url(s, key, query=True)
            return str(v)
        if s.startswith(("http://", "https://")):
            return _url(s, key, query=True)
        if remote:
            raise SettingsError("calendar must be an http(s):// or webcal:// URL (local files only via the CLI)")
        return s
    if key == "tz":
        try:
            ZoneInfo(str(v))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError(f"unknown timezone {v!r}") from exc
        return str(v)
    if key in ("hours_start", "hours_end"):
        if not isinstance(v, str) or not _HHMM.match(v):
            raise SettingsError(f"{key} must be HH:MM")
        return v
    if key == "buffer_minutes":
        try:
            n = int(v)
        except (TypeError, ValueError) as exc:
            raise SettingsError("buffer_minutes must be a number") from exc
        if not 0 <= n <= 240:
            raise SettingsError("buffer_minutes must be 0-240")
        return n
    if key == "nudge_after_hours":
        try:
            x = float(v)
        except (TypeError, ValueError) as exc:
            raise SettingsError("nudge_after_hours must be a number") from exc
        if not 0 <= x <= 24 * 30:
            raise SettingsError("nudge_after_hours must be 0-720")
        return x
    if key in ("tentative_holds", "trips_block_calendar"):
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")
    if key == "currency":
        c = str(v).upper()
        if not re.match(r"^[A-Z]{3}$", c):
            raise SettingsError("currency must be a 3-letter code like USD")
        return c
    if key == "notify_cmd":
        return str(v)
    raise SettingsError(f"unhandled setting {key!r}")  # pragma: no cover


def update(node: "Node", changes: dict[str, Any], *, remote: bool = True) -> dict[str, Any]:
    """Validate every change first, then apply all of them (all-or-nothing)."""
    if node.config.get("hours_start") and ("hours_start" in changes or "hours_end" in changes):
        start = changes.get("hours_start", node.config.get("hours_start"))
        end = changes.get("hours_end", node.config.get("hours_end"))
        if isinstance(start, str) and isinstance(end, str) and _HHMM.match(start) and _HHMM.match(end) and end <= start:
            raise SettingsError("hours_end must be after hours_start")
    cleaned = {k: validate(k, v, remote=remote) for k, v in changes.items()}
    node.config.update(cleaned)
    node.save_config()
    return public_view(node)
