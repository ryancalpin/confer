"""POST actions for the phone UI. Each action is a thin wrapper over one Node call.

An action returns ``None`` (→ 303 redirect back to its tab with a short
notice) or a dict of extra render data (→ the page is rendered directly, e.g.
to show a freshly created invite token that must not go into a URL).
Bad input raises :class:`UIError` or ``NodeError``; its message is shown in
the error banner.
"""

from __future__ import annotations

import re
import secrets
import shutil
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..node import MAX_FILE_BYTES, NodeError, _safe_name, check_url

if TYPE_CHECKING:
    from ..node import Node


class UIError(ValueError):
    """Bad form input; the message is safe to show to the owner."""


class Form:
    """Parsed form fields (``name -> [values]``) plus uploaded files (``name -> (filename, bytes)``)."""

    def __init__(self, fields: dict[str, list[str]], files: dict[str, tuple[str, bytes]] | None = None):
        self.fields, self.files = fields, files or {}

    def get(self, name: str, default: str = "") -> str:
        values = self.fields.get(name)
        return values[0].strip() if values else default

    def raw(self, name: str) -> str:
        values = self.fields.get(name)
        return values[0] if values else ""

    def all(self, name: str) -> list[str]:
        return [v.strip() for v in self.fields.get(name, []) if v.strip()]

    def flag(self, name: str) -> bool:
        return bool(self.get(name))

    def int(self, name: str, what: str, *, lo: int | None = None, hi: int | None = None, default: int | None = None) -> int | None:
        text = self.get(name)
        if not text:
            return default
        try:
            value = int(text)
        except ValueError:
            raise UIError(f"{what} must be a whole number") from None
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise UIError(f"{what} must be between {lo} and {hi}")
        return value

    def indexes(self, name: str) -> list[int]:
        try:
            return [int(v) for v in self.all(name)]
        except ValueError:
            raise UIError("bad option number") from None


Action = Callable[["Node", Form], "dict[str, Any] | None"]
ACTIONS: dict[str, tuple[Action, str]] = {}  # name -> (handler, tab to return to)
NOTICES: dict[str, str] = {}  # name -> message shown after the redirect


def action(name: str, tab: str, notice: str = "") -> Callable[[Action], Action]:
    def register(fn: Action) -> Action:
        ACTIONS[name] = (fn, tab)
        if notice:
            NOTICES[name] = notice
        return fn

    return register


def _need(value: str, what: str) -> str:
    if not value:
        raise UIError(f"{what} is required")
    return value


def _date(node: "Node", text: str, *, end: bool = False) -> datetime | None:
    """A local date (``YYYY-MM-DD``) in the node's time zone → aware UTC datetime.
    ``end=True`` means "through the end of that day"."""
    if not text:
        return None
    from ..cli import _local_dt

    try:
        dt = _local_dt(text, node.tz)
    except ValueError:
        raise UIError(f"bad date {text!r}") from None
    return dt + timedelta(days=1) if end and len(text) == 10 else dt


def _window(node: "Node", f: Form) -> tuple[datetime | None, datetime | None, tuple[dtime, dtime] | None]:
    from ..cli import _between

    start, end = _date(node, f.get("from")), _date(node, f.get("to"), end=True)
    if start and end and end <= start:
        raise UIError("the end date is before the start date")
    a, b = f.get("between_start"), f.get("between_end")
    between = None
    if a or b:
        if not (a and b):
            raise UIError("give both an earliest and a latest time, or neither")
        try:
            between = _between(f"{a}-{b}")
        except ValueError:
            raise UIError("times must look like 18:00") from None
    return start, end, between


# --------------------------------------------------------------------------- inbox


@action("dismiss", "inbox")
def _dismiss(node: "Node", f: Form) -> None:
    item_id = f.int("item_id", "item")
    if item_id is None or not node.dismiss(item_id):
        raise UIError("that item is already gone")


@action("dismiss_all", "inbox", "Cleared")
def _dismiss_all(node: "Node", f: Form) -> None:
    for item in node.inbox():
        if not item.get("actionable"):
            node.dismiss(item["id"])


@action("intro", "inbox", "Done")
def _intro(node: "Node", f: Form) -> None:
    intro_id, decision = f.get("intro_id"), f.get("decision")
    if decision == "accept":
        node.accept_intro(intro_id)
    elif decision == "decline":
        node.decline_intro(intro_id)
    else:
        raise UIError("choose accept or decline")


@action("reply_note", "inbox", "Reply sent")
def _reply_note(node: "Node", f: Form) -> None:
    item_id = f.int("item_id", "item")
    item = next((i for i in node.inbox() if i["id"] == item_id and i["kind"] == "note"), None)
    if item is None:
        raise UIError("that message is no longer in your inbox")
    contact = node.store.contact(item["contact"])
    if contact is None:
        raise UIError("that person is no longer a contact")
    node.send_note(contact.name, _need(f.raw("text").strip(), "a reply"), reply_to=str(item["payload"].get("msg_id", "")))
    node.dismiss(item["id"])


@action("send_note", "inbox", "Note sent")
def _send_note(node: "Node", f: Form) -> None:
    node.send_note(_need(f.get("contact"), "a recipient"), f.raw("text"), expects_reply=f.flag("question"))


@action("send_file", "inbox", "File sent")
def _send_file(node: "Node", f: Form) -> None:
    to = _need(f.get("contact"), "a recipient")
    if "file" not in f.files:
        raise UIError("choose a file")
    filename, data = f.files["file"]
    if not data:
        raise UIError("that file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise UIError(f"files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB")
    tmp_root = node.home / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=tmp_root))
    try:
        path = folder / _safe_name(filename or "file")
        path.write_bytes(data)
        node.send_file(to, path, f.raw("note").strip())
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@action("share_status", "inbox", "Shared")
def _share_status(node: "Node", f: Form) -> None:
    lat, lon = f.get("lat"), f.get("lon")
    try:
        la = float(lat) if lat else None
        lo = float(lon) if lon else None
        acc = int(float(f.get("accuracy"))) if f.get("accuracy") else None
    except ValueError:
        raise UIError("bad location from the browser") from None
    names = f.all("contact")
    node.share_status(names or None, plan_id=f.get("plan_id"), text=f.raw("text"), eta_minutes=f.int("eta", "ETA", lo=0, hi=1440),
                      lat=la, lon=lo, accuracy_m=acc if acc and acc > 0 else None)


@action("stop_sharing", "inbox", "Stopped sharing")
def _stop_sharing(node: "Node", f: Form) -> None:
    names = f.all("contact")
    if not names:
        raise UIError("pick at least one person")
    node.stop_sharing(names)


# --------------------------------------------------------------------------- plans


@action("respond", "inbox", "Answer sent")
def _respond(node: "Node", f: Form) -> None:
    decision = f.get("decision")
    slots = f.indexes("slot") if decision == "accept" else None
    node.respond(f.get("plan_id"), decision, slots=slots, prefer=f.indexes("prefer"), note=f.raw("note").strip())


@action("create_plan", "plans", "Plan proposed")
def _create_plan(node: "Node", f: Form) -> None:
    names = f.all("contact")
    if not names:
        raise UIError("pick at least one person")
    start, end, between = _window(node, f)
    quorum_text = f.get("quorum", "all").lower() or "all"
    if quorum_text != "all" and not quorum_text.isdigit():
        raise UIError("“how many must say yes” is 'all' or a number")
    node.create_plan(
        _need(f.get("title"), "a title"), names,
        duration_minutes=f.int("duration", "duration", lo=5, hi=24 * 60, default=60) or 60,
        window_start=start, window_end=end, between=between,
        location=f.get("location"), quorum="all" if quorum_text == "all" else int(quorum_text),
    )


@action("revise", "plans", "New times proposed")
def _revise(node: "Node", f: Form) -> None:
    start, end, between = _window(node, f)
    node.revise(f.get("plan_id"), window_start=start, window_end=end, between=between)


@action("cancel_plan", "plans", "Plan cancelled")
def _cancel_plan(node: "Node", f: Form) -> None:
    node.cancel(f.get("plan_id"), f.get("reason"))


# --------------------------------------------------------------------------- lists


@action("create_list", "lists", "List created")
def _create_list(node: "Node", f: Form) -> None:
    items = [line.strip() for line in f.raw("items").splitlines() if line.strip()]
    node.create_list(_need(f.get("title"), "a title"), f.all("contact"), items=items)


@action("list_op", "lists")
def _list_op(node: "Node", f: Form) -> None:
    op = f.get("op")
    if op not in ("add", "check", "uncheck", "claim", "unclaim", "remove"):
        raise UIError("unknown list action")
    if op == "add":
        node.list_op(f.get("list_id"), "add", text=_need(f.get("text"), "an item"))
    else:
        node.list_op(f.get("list_id"), op, item=_need(f.get("item"), "an item"))


@action("delete_list", "lists", "Done")
def _delete_list(node: "Node", f: Form) -> None:
    lst = node.get_list(f.get("list_id"))
    if lst.get("role") == "owner" and not f.flag("confirm"):
        raise UIError("tick the box to confirm deleting the list for everyone")
    node.delete_list(lst["id"])


# --------------------------------------------------------------------------- money


@action("answer_entry", "money", "Answer sent")
def _answer_entry(node: "Node", f: Form) -> None:
    decision = f.get("decision")
    if decision not in ("accept", "dispute"):
        raise UIError("choose accept or dispute")
    node.answer_entry(f.get("entry_id"), accept=decision == "accept", note=f.get("note"))


@action("add_expense", "money", "Sent to everyone in the split")
def _add_expense(node: "Node", f: Form) -> None:
    node.add_expense(_need(f.get("title"), "what it was for"), _need(f.get("amount"), "an amount"), f.all("contact"),
                     currency=f.get("currency") or None, include_me=f.flag("include_me"), note=f.get("note"))


@action("record_payment", "money", "Payment recorded — they'll confirm it")
def _record_payment(node: "Node", f: Form) -> None:
    node.record_payment(_need(f.get("contact"), "who you paid"), _need(f.get("amount"), "an amount"),
                        currency=f.get("currency") or None, note=f.get("note"))


@action("cancel_entry", "money", "Entry cancelled")
def _cancel_entry(node: "Node", f: Form) -> None:
    node.cancel_entry(f.get("entry_id"))


# --------------------------------------------------------------------------- people


@action("set_grants", "people", "Permissions saved")
def _set_grants(node: "Node", f: Form) -> None:
    node.set_grants(_need(f.get("contact"), "a contact"), f.all("grant"))


@action("remove_contact", "people", "Contact removed")
def _remove_contact(node: "Node", f: Form) -> None:
    if not f.flag("confirm"):
        raise UIError("tick the box to confirm removing this contact")
    node.remove_contact(_need(f.get("contact"), "a contact"))


@action("create_invite", "people")
def _create_invite(node: "Node", f: Form) -> dict[str, Any]:
    name = _need(f.get("name"), "their name")[:60]
    return {"invite_token": node.create_invite(name, f.all("grant")), "invite_name": name}


@action("accept_invite", "people", "Invite accepted — connecting…")
def _accept_invite(node: "Node", f: Form) -> None:
    token = "".join(f.raw("token").split())  # phones like to wrap long pasted text
    node.accept_invite(_need(token, "the invite"), name=f.get("name") or None, grants=f.all("grant"))


@action("introduce", "people", "Introduction sent")
def _introduce(node: "Node", f: Form) -> None:
    node.introduce(_need(f.get("a"), "two people"), _need(f.get("b"), "two people"), f.get("note"))


# --------------------------------------------------------------------------- settings

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _url_or_empty(value: str, what: str, *, https_only: bool = False) -> str:
    if not value:
        return ""
    try:
        url = check_url(value)
    except NodeError:
        raise UIError(f"{what} must be an http(s) URL like https://host/path") from None
    if https_only and not url.startswith("https://"):
        raise UIError(f"{what} must start with https://")
    return url


def _calendar_url(value: str) -> str:
    """Only remote calendars can be set from the web — never a path on the node's disk."""
    if not value:
        return ""
    u = urlparse(value)
    if u.scheme not in ("http", "https", "webcal") or not u.hostname or any(ch.isspace() for ch in value) or len(value) > 2000:
        raise UIError("the calendar must be an http(s):// or webcal:// URL (local files can only be set with the CLI)")
    return value


@action("save_settings", "settings", "Settings saved")
def _save_settings(node: "Node", f: Form) -> None:
    cfg = node.config
    new: dict[str, Any] = {}
    new["name"] = _need(f.get("name"), "your name")[:60]
    new["endpoint"] = _url_or_empty(f.get("endpoint"), "the endpoint")
    new["relay"] = _url_or_empty(f.get("relay"), "the relay")
    tz = f.get("tz") or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise UIError(f"unknown time zone {tz!r} (try something like Europe/London)") from None
    new["tz"] = tz
    for key, what in (("hours_start", "the start of your hours"), ("hours_end", "the end of your hours")):
        value = f.get(key)
        if not _HHMM.match(value):
            raise UIError(f"{what} must look like 08:00")
        new[key] = value
    if new["hours_start"] >= new["hours_end"]:
        raise UIError("your available hours must end after they start")
    new["buffer_minutes"] = f.int("buffer_minutes", "the buffer", lo=0, hi=240, default=0)
    cal = f.get("calendar")
    # a local path already configured with the CLI may be kept, never set or changed from here
    new["calendar"] = cal if cal and cal == cfg.get("calendar") else _calendar_url(cal)
    new["tentative_holds"] = f.flag("tentative_holds")
    ccy = f.get("currency", "USD").upper()
    if not re.fullmatch(r"[A-Z]{3}", ccy):
        raise UIError("currency must be a 3-letter code like USD")
    new["currency"] = ccy
    new["pay_link"] = _url_or_empty(f.get("pay_link"), "the pay link", https_only=True)
    new["notify_webhook"] = _url_or_empty(f.get("notify_webhook"), "the webhook")
    nudge = f.get("nudge_after_hours") or "24"
    try:
        hours = float(nudge)
    except ValueError:
        raise UIError("reminder hours must be a number") from None
    if not 1 <= hours <= 720:
        raise UIError("reminder hours must be between 1 and 720")
    new["nudge_after_hours"] = int(hours) if hours.is_integer() else hours
    cfg.update(new)  # note: notify_cmd is deliberately not editable from the web
    node.save_config()


@action("rotate_ui", "settings", "New link created — bookmark this page")
def _rotate_ui(node: "Node", f: Form) -> None:
    if not f.flag("confirm"):
        raise UIError("tick the box to confirm")
    node.config["ui_token"] = secrets.token_urlsafe(24)
    node.save_config()


@action("rotate_key", "settings", "Key rotated — your contacts are being told")
def _rotate_key(node: "Node", f: Form) -> None:
    if not f.flag("confirm"):
        raise UIError("tick the box to confirm")
    node.rotate_key()
