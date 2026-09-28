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
# notify_cmd runs a program; notify_webhook sends every inbox event somewhere. A leaked
# API token must not turn into code execution or a permanent eavesdropping channel.
CLI_ONLY = ("notify_cmd", "notify_webhook")
SECRET = ("feed_token", "ui_token", "api_token")
EDITABLE = ("name", "endpoint", "relay", "tz", "hours_start", "hours_end", "buffer_minutes", "calendar", "tentative_holds",
            "trips_block_calendar", "currency", "pay_link", "nudge_after_hours")


class SettingsError(ValueError):
    pass


def public_view(node: "Node") -> dict[str, Any]:
    cfg = {k: node.config.get(k) for k in EDITABLE}
    cfg["notify_cmd_set"] = bool(node.config.get("notify_cmd"))
    cfg["notify_webhook_set"] = bool(node.config.get("notify_webhook"))
    return cfg


def _public_host(url: str, what: str) -> None:
    """Refuse URLs that resolve to loopback, private, link-local (cloud metadata) or
    otherwise non-public addresses — a remote caller must not aim the node's own
    fetches at its network (SSRF)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SettingsError(f"{what}: can't resolve {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise SettingsError(f"{what} must point to a public internet address")


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
        fetch = "https://" + s[len("webcal://"):] if s.startswith("webcal://") else s
        if fetch.startswith(("http://", "https://")):
            _url(fetch, key, query=True)
            if remote:  # the secret iCal URL must not travel in cleartext, nor point inward
                if not fetch.startswith("https://"):
                    raise SettingsError("calendar must be an https:// or webcal:// URL")
                _public_host(fetch, "calendar")
            return s
        if remote:
            raise SettingsError("calendar must be an https:// or webcal:// URL (local files only via the CLI)")
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
    cleaned = {k: validate(k, v, remote=remote) for k, v in changes.items()}
    if "hours_start" in cleaned or "hours_end" in cleaned:  # compare the *cleaned* values
        start = cleaned.get("hours_start", node.config.get("hours_start", "08:00"))
        end = cleaned.get("hours_end", node.config.get("hours_end", "22:00"))
        if end <= start:
            raise SettingsError("hours_end must be after hours_start")
    node.config.update(cleaned)
    node.save_config()
    return public_view(node)
