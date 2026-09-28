"""Server-rendered pages for the phone UI — one HTML page per tab.

Every dynamic value goes through :func:`e` (HTML-escape). Links are only
generated for URLs we trust by construction: this UI's own routes, OpenStreetMap
map links (from shared locations) and ``https://`` pay links.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..identity import fingerprint
from ..money import fmt as fmt_money
from ..node import DEFAULT_GRANTS, GRANTS
from ..trips import KINDS, STATUSES
from .assets import CSS, ICONS, JS

if TYPE_CHECKING:
    from ..node import Node
    from ..store import Contact

TABS = ("inbox", "plans", "lists", "money", "people", "trips", "settings")
TAB_LABELS = {"inbox": "Inbox", "plans": "Plans", "lists": "Lists", "money": "Money", "people": "People", "trips": "Trips", "settings": "Settings"}
OSM_PREFIX = "https://www.openstreetmap.org/"

GRANT_HELP = {
    "plans": ("Plans", "Can propose plans to you. You still decide."),
    "autoconfirm": ("Auto-accept plans", "Plans that fit your calendar are accepted without asking you."),
    "files": ("Files", "Can send you files (end-to-end encrypted)."),
    "notes": ("Notes", "Can send you short messages."),
    "intros": ("Introductions", "Can introduce other people to you. You approve each one."),
    "lists": ("Shared lists", "Can share lists with you (groceries, who's bringing what)."),
    "money": ("Money", "Can send you expense shares and payments to confirm. Confer never moves money."),
    "location": ("Status & location", "Sensitive: can send you their status, ETA and live location. "
                 "Off by default — only for people you meet up with."),
    "trips": ("Trips", "Can add you to trips they organize (itinerary, rides, rooms, tasks, polls)."),
}


def e(value: object) -> str:
    """HTML-escape any value (attribute-safe)."""
    return html.escape("" if value is None else str(value), quote=True)


@dataclass
class Ctx:
    node: "Node"
    token: str
    tab: str
    public_url: str = ""
    nonce: str = ""
    error: str = ""
    notice: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return f"/ui/{self.token}"

    def contacts(self, active_only: bool = True) -> list["Contact"]:
        return [c for c in self.node.store.contacts() if c.status == "active" or not active_only]


# --------------------------------------------------------------------------- small builders


def linkify(text: str, allowed: tuple[str, ...] = (OSM_PREFIX,)) -> str:
    """Escape ``text``; turn only URLs starting with an ``allowed`` prefix into links."""
    out, pos = [], 0
    for m in re.finditer(r"https://[^\s<>\"']+", text):
        url = m.group(0).rstrip(".,;:)!?")
        if not any(url.startswith(p) for p in allowed if p):
            continue
        out.append(e(text[pos : m.start()]))
        out.append(f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(url)}</a>')
        pos = m.start() + len(url)
    out.append(e(text[pos:]))
    return "".join(out)


# Inbox summaries end with a CLI hint ("confer money accept <id>"); the UI shows buttons instead.
# Only the exact generated formats, anchored at the end, so user text is never cut.
_CLI_HINT = re.compile(
    r"\s*(?:Reply: confer plan respond \S+ accept\|decline\|counter"
    r"|confer plan respond \S+ accept\|decline"
    r"|Revise it: confer plan revise \S+"
    r"|Join anyway: confer plan respond \S+ accept --slots \d+"
    r"|confer intro accept \S+\s+\(or decline\)"
    r"|confer list show \S+"
    r"|(?:Confirm: )?confer money accept \S+ \(or dispute\)"
    r"|\(reply: confer note (?:'[^']*'|\"[^\"]*\") \"\.\.\.\" --reply-to \S+\))$"
)


def without_cli_hint(summary: str) -> str:
    return _CLI_HINT.sub("", summary)


def safe_pay_link(url: object) -> str:
    u = str(url or "")
    return u if u.startswith("https://") and len(u) < 500 else ""


def form(ctx: Ctx, action: str, body: str, *, tab: str | None = None, multipart: bool = False, cls: str = "") -> str:
    enc = ' enctype="multipart/form-data"' if multipart else ""
    klass = f' class="{e(cls)}"' if cls else ""
    return (f'<form method="post" action="{e(ctx.base)}/act"{enc}{klass}>'
            f'<input type="hidden" name="t" value="{e(ctx.token)}">'
            f'<input type="hidden" name="action" value="{e(action)}">'
            f'<input type="hidden" name="tab" value="{e(tab or ctx.tab)}">{body}</form>')


def hidden(name: str, value: object) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def button(label: str, cls: str = "", name: str = "", value: str = "", attrs: str = "") -> str:
    nv = f' name="{e(name)}" value="{e(value)}"' if name else ""
    klass = f' class="{e(cls)}"' if cls else ""
    return f'<button type="submit"{klass}{nv}{attrs}>{e(label)}</button>'


def field_(label: str, name: str, value: object = "", *, type_: str = "text", placeholder: str = "",
           required: bool = False, attrs: str = "") -> str:
    req = " required" if required else ""
    ph = f' placeholder="{e(placeholder)}"' if placeholder else ""
    return (f'<label class="field"><span>{e(label)}</span>'
            f'<input type="{e(type_)}" name="{e(name)}" value="{e(value)}"{ph}{req}{attrs}></label>')


def textarea(label: str, name: str, value: str = "", *, placeholder: str = "", required: bool = False, attrs: str = "") -> str:
    req = " required" if required else ""
    ph = f' placeholder="{e(placeholder)}"' if placeholder else ""
    return f'<label class="field"><span>{e(label)}</span><textarea name="{e(name)}"{ph}{req}{attrs}>{e(value)}</textarea></label>'


def check(name: str, value: object, label: str, *, checked: bool = False, help_: str = "") -> str:
    small = f"<small>{e(help_)}</small>" if help_ else ""
    return (f'<label class="check"><input type="checkbox" name="{e(name)}" value="{e(value)}"'
            f'{" checked" if checked else ""}><span>{e(label)}{small}</span></label>')


def contact_checks(ctx: Ctx, legend: str, name: str = "contact", checked: tuple[str, ...] = ()) -> str:
    people = ctx.contacts()
    if not people:
        return f'<fieldset><legend>{e(legend)}</legend><p class="empty">No contacts yet — invite someone on the People tab.</p></fieldset>'
    boxes = "".join(check(name, c.agent_id, c.name, checked=c.agent_id in checked) for c in people)
    return f"<fieldset><legend>{e(legend)}</legend>{boxes}</fieldset>"


def contact_select(ctx: Ctx, label: str, name: str = "contact", *, blank: str = "") -> str:
    opts = f'<option value="">{e(blank)}</option>' if blank else ""
    opts += "".join(f'<option value="{e(c.agent_id)}">{e(c.name)}</option>' for c in ctx.contacts())
    return f'<label class="field"><span>{e(label)}</span><select name="{e(name)}">{opts}</select></label>'


def grant_checks(selected: list[str] | tuple[str, ...]) -> str:
    return "<fieldset><legend>What they may do with your agent</legend>" + "".join(
        check("grant", g, GRANT_HELP[g][0], checked=g in selected, help_=GRANT_HELP[g][1]) for g in GRANTS
    ) + "</fieldset>"


def pill(status: str, text: str | None = None) -> str:
    return f'<span class="pill {e(status)}">{e(text or status.replace("_", " "))}</span>'


def section(title: str) -> str:
    return f"<h2>{e(title)}</h2>"


def empty(text: str) -> str:
    return f'<div class="card"><span class="empty">{e(text)}</span></div>'


def collapsible(title: str, body: str, *, open_: bool = False) -> str:
    return f'<details class="card"{" open" if open_ else ""}><summary>{e(title)}</summary>{body}</details>'


def local_date(ctx: Ctx, ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, ZoneInfo(ctx.node.tz)).strftime("%b %d")
    except (OverflowError, OSError, ValueError):
        return ""


def file_rel_path(node: "Node", path: str) -> str:
    """Path of a received file relative to ``<home>/files``, or "" if it isn't under it."""
    try:
        base = (node.home / "files").resolve()
        target = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    if target == base or not target.is_relative_to(base) or not target.is_file():
        return ""
    return target.relative_to(base).as_posix()


# --------------------------------------------------------------------------- counts & shell


def counts(node: "Node") -> dict[str, int]:
    inbox = sum(1 for i in node.inbox() if i.get("actionable"))
    money = sum(1 for x in node.ledger() if x.get("payer") == "them" and x.get("status") == "pending")
    return {"inbox": inbox, "money": money}


def shell(ctx: Ctx, content: str) -> bytes:
    n = counts(ctx.node)
    tabs = []
    for t in TABS:
        cur = ' aria-current="page"' if t == ctx.tab else ""
        badge = f'<span class="count">{n[t] if n[t] < 100 else "99+"}</span>' if n.get(t) else ""
        tabs.append(f'<a href="{e(ctx.base)}?tab={t}"{cur}><svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[t]}</svg>'
                    f"{badge}<span>{TAB_LABELS[t]}</span></a>")
    banners = ""
    if ctx.error:
        banners += f'<div class="banner error" role="alert">{e(ctx.error)}</div>'
    if ctx.notice:
        banners += f'<div class="banner notice" role="status">{e(ctx.notice)}</div>'
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f2f2f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Confer">
<title>Confer — {e(ctx.node.name)}</title>
<style nonce="{e(ctx.nonce)}">{CSS}</style>
</head>
<body>
<header class="appbar"><h1>{TAB_LABELS[ctx.tab]}</h1><div class="who">Confer · {e(ctx.node.name)}</div></header>
<main>
{banners}
{content}
</main>
<nav class="tabs" aria-label="Sections">{"".join(tabs)}</nav>
<script nonce="{e(ctx.nonce)}">{JS}</script>
</body>
</html>
"""
    return page.encode("utf-8")


# --------------------------------------------------------------------------- plans (shared)


def respond_form(ctx: Ctx, plan: dict) -> str:
    suggested = set(plan.get("suggested", []))
    rows = []
    for i, slot in enumerate(plan.get("slots", [])):
        rows.append(
            f'<div class="row optrow"><label class="check spacer"><input type="checkbox" name="slot" value="{i}"'
            f'{" checked" if i in suggested else ""}><span>{e(ctx.node._fmt_slot(slot))}'
            f'{"<small>Your calendar is free</small>" if i in suggested else ""}</span></label>'
            f'<label class="check"><input type="checkbox" name="prefer" value="{i}"><span>Prefer</span></label></div>'
        )
    body = (hidden("plan_id", plan["id"]) + '<fieldset><legend>Which times work for you?</legend>' + "".join(rows) + "</fieldset>"
            + field_("Note (optional)", "note", placeholder="e.g. I can only stay an hour")
            + '<div class="row">' + button("Accept", "ok", "decision", "accept") + button("Decline", "danger", "decision", "decline") + "</div>")
    return form(ctx, "respond", body)


def window_fields() -> str:
    return ('<div class="pair">' + field_("From date", "from", type_="date") + field_("To date", "to", type_="date") + "</div>"
            '<div class="pair">' + field_("Earliest time", "between_start", type_="time") + field_("Latest time", "between_end", type_="time") + "</div>")


def plan_card(ctx: Ctx, plan: dict) -> str:
    node = ctx.node
    status = plan.get("status", "")
    organizer = plan.get("role") == "organizer"
    hot = awaiting_me(plan)
    out = [f'<div class="card{" hot" if hot else ""}"><div class="card-title">{e(plan.get("title", ""))}{pill(status)}</div>']
    who = "You're organizing" if organizer else f"From {plan.get('organizer_name', '')}"
    out.append(f'<div class="meta">{e(who)}</div>')
    if plan.get("location"):
        out.append(f'<div class="meta">📍 {e(plan["location"])}</div>')
    if plan.get("rrule"):
        out.append(f'<div class="meta">Repeats: {e(plan["rrule"])}</div>')
    if plan.get("notes"):
        out.append(f'<div class="meta">{e(plan["notes"])}</div>')
    slots = plan.get("slots", [])
    if plan.get("chosen") is not None and 0 <= plan["chosen"] < len(slots):
        out.append(f'<div class="meta"><strong>When:</strong> {e(node._fmt_slot(slots[plan["chosen"]]))}</div>')
    elif not hot:
        opts = "".join(f"<li>{i + 1}) {e(node._fmt_slot(s))}</li>" for i, s in enumerate(slots))
        out.append(f'<div class="meta">Options:<ul>{opts}</ul></div>')
    people = []
    for p in plan.get("participants", {}).values():
        extra = ""
        if organizer and p.get("status"):
            extra = " " + pill(p["status"])
            if p.get("status") == "accepted" and p.get("ok_slots"):
                pref = set(p.get("prefer", []))
                extra += " " + e(", ".join(f"{i + 1}{'★' if i in pref else ''}" for i in p["ok_slots"]))
            if p.get("note"):
                extra += f" — “{e(p['note'])}”"
        people.append(f"<li>{e(p.get('name', ''))}{extra}</li>")
    if people:
        out.append(f'<div class="meta">People:<ul>{"".join(people)}</ul></div>')
    if hot:
        out.append(respond_form(ctx, plan))
    elif organizer and status != "cancelled":
        revise = form(ctx, "revise", hidden("plan_id", plan["id"])
                      + '<p class="meta">Find new times in a different window. Everyone is asked again.</p>'
                      + window_fields() + button("Propose new times"))
        cancel = form(ctx, "cancel_plan", hidden("plan_id", plan["id"]) + field_("Reason (optional)", "reason")
                      + button("Cancel plan", "danger"))
        out.append(collapsible("Change or cancel", revise + cancel))
    elif not organizer and status != "cancelled":
        if status == "confirmed" and not node._attending(plan) and plan.get("chosen") is not None:
            out.append(form(ctx, "respond", hidden("plan_id", plan["id"]) + hidden("slot", plan["chosen"])
                            + button("Join at this time", "ok", "decision", "accept")))
        if plan.get("my_status") != "declined":
            out.append(form(ctx, "respond", hidden("plan_id", plan["id"]) + hidden("decision", "decline")
                            + field_("Note (optional)", "note", placeholder="Sorry, something came up")
                            + button("Can't make it", "danger")))
    out.append("</div>")
    return "".join(out)


def awaiting_me(plan: dict) -> bool:
    """A plan invitation I haven't answered yet."""
    return plan.get("role") == "participant" and plan.get("my_status") == "invited" and plan.get("status") == "proposed"


def invite_plans(node: "Node") -> list[dict]:
    return [p for p in node.plans() if awaiting_me(p)]


# --------------------------------------------------------------------------- inbox


def _intro_card(ctx: Ctx, intro: dict) -> str:
    via = ctx.node.store.contact(intro["introducer"])
    note = f'<div class="meta">“{e(intro["note"])}”</div>' if intro.get("note") else ""
    body = (hidden("intro_id", intro["intro_id"]) + '<div class="row">' + button("accept", "ok", "decision", "accept")
            + button("decline", "danger", "decision", "decline") + "</div>")
    return (f'<div class="card hot"><div class="card-title">Meet {e(intro["peer_name"])}?</div>'
            f'<div class="meta">Introduced by {e(via.name if via else "a contact")} · fingerprint <span class="mono">{e(fingerprint(intro["peer_id"]))}</span></div>'
            f"{note}{form(ctx, 'intro', body)}</div>")


def _dismiss(ctx: Ctx, item: dict, label: str = "Dismiss") -> str:
    return form(ctx, "dismiss", hidden("item_id", item["id"]) + button(label, "secondary small"))


def _item_card(ctx: Ctx, item: dict, entries: dict[str, dict]) -> str:
    kind, payload = item.get("kind", ""), item.get("payload") or {}
    hot = bool(item.get("actionable"))
    allowed: tuple[str, ...] = (OSM_PREFIX,)
    extra = []
    if kind == "money":
        entry = entries.get(item.get("ref", ""))
        if entry:
            link = safe_pay_link(entry.get("pay_link"))
            allowed += (link,) if link else ()
            if entry.get("payer") == "them" and entry.get("status") in ("pending", "disputed"):
                body = (hidden("entry_id", entry["id"]) + field_("Note (optional)", "note")
                        + '<div class="row">' + button("Accept", "ok", "decision", "accept")
                        + (button("Dispute", "danger", "decision", "dispute") if entry["status"] == "pending" else "") + "</div>")
                extra.append(form(ctx, "answer_entry", body))
                if link:
                    extra.append(f'<a class="btn secondary" href="{e(link)}" target="_blank" rel="noopener noreferrer">Open pay link</a>')
            elif entry.get("payer") == "me" and entry.get("status") in ("pending", "disputed"):
                extra.append(form(ctx, "cancel_entry", hidden("entry_id", entry["id"]) + button("Cancel this entry", "secondary small")))
    elif kind == "note" and payload.get("expects_reply") and hot:
        body = (hidden("item_id", item["id"]) + textarea("Your reply", "text", required=True, attrs=' maxlength="4000"')
                + button("Send reply"))
        extra.append(form(ctx, "reply_note", body))
    elif kind == "file" and payload.get("path"):
        rel = file_rel_path(ctx.node, str(payload["path"]))
        if rel:
            href = f"{ctx.base}/file?path={urllib.parse.quote(rel, safe='')}"
            extra.append(f'<a class="btn secondary" href="{e(href)}" download>Download</a>')
    if kind.startswith("plan.") or kind == "list":
        tab = "lists" if kind == "list" else "plans"
        extra.append(f'<a class="btn secondary" href="{e(ctx.base)}?tab={tab}">Open {TAB_LABELS[tab]}</a>')
    summary = without_cli_hint(str(item.get("summary", "")))
    return (f'<div class="card{" hot" if hot else ""}"><div>{linkify(summary, allowed)}</div>'
            f'{"".join(extra)}<div class="row">{_dismiss(ctx, item)}</div></div>')


def _share_status_form(ctx: Ctx) -> str:
    plans = [p for p in ctx.node.plans() if p.get("status") in ("proposed", "confirmed")]
    opts = '<option value="">— or pick a plan —</option>' + "".join(
        f'<option value="{e(p["id"])}">{e(p.get("title", ""))}</option>' for p in plans)
    body = (field_("Status", "text", placeholder="Leaving now", attrs=' maxlength="280"')
            + field_("ETA (minutes, optional)", "eta", type_="number", attrs=' min="0" max="1440" inputmode="numeric"')
            + contact_checks(ctx, "Send to")
            + f'<label class="field"><span>Everyone in a plan</span><select name="plan_id">{opts}</select></label>'
            + hidden("lat", "") + hidden("lon", "") + hidden("accuracy", "")
            + '<p class="meta">Only people who allow “Status &amp; location” from you will receive it. It expires after 2 hours.</p>'
            + '<div class="row">' + button("Share status")
            + '<button type="button" class="secondary" data-geo="1">Share my location</button></div>'
            + '<div class="meta js-status" aria-live="polite"></div>')
    stop = form(ctx, "stop_sharing", contact_checks(ctx, "Stop sharing with") + button("Stop sharing", "secondary"))
    return collapsible("Share status / location", form(ctx, "share_status", body) + stop)


def page_inbox(ctx: Ctx) -> str:
    node = ctx.node
    out = []
    invites = invite_plans(node)
    invite_ids = {p["id"] for p in invites}
    intros = [i for i in node.intros() if i["status"] == "offered" and i["expires_at"] > node.now()]
    intro_ids = {i["intro_id"] for i in intros}
    entries = {x["id"]: x for x in node.ledger()}
    items = [i for i in node.inbox()
             if not (i["kind"] in ("plan.invite", "plan.reminder") and i["ref"] in invite_ids)
             and not (i["kind"] == "intro" and i["ref"] in intro_ids)
             and i["kind"] != "presence"]  # shown (while fresh) under "Live now" instead
    attention = [i for i in items if i.get("actionable")]
    rest = [i for i in items if not i.get("actionable")]

    if invites:
        out.append(section("Plan invitations"))
        out.extend(plan_card(ctx, p) for p in invites)
    if intros:
        out.append(section("Introductions"))
        out.extend(_intro_card(ctx, i) for i in intros)
    if attention:
        out.append(section("Needs attention"))
        out.extend(_item_card(ctx, i, entries) for i in attention)
    live = node.presence()
    if live:
        out.append(section("Live now"))
        out.extend(f'<div class="card">{linkify(p["summary"])}</div>' for p in live)
    if rest:
        out.append(section("Updates"))
        out.extend(_item_card(ctx, i, entries) for i in rest)
        out.append(form(ctx, "dismiss_all", button("Dismiss all updates", "secondary wide")))
    if not (invites or intros or attention or rest):
        out.append(empty("You're all caught up."))

    out.append(section("Send"))
    people = ctx.contacts()
    if people:
        note = (contact_select(ctx, "To") + textarea("Message", "text", required=True, attrs=' maxlength="4000"')
                + check("question", "1", "This is a question (ask for a reply)") + button("Send note"))
        out.append(collapsible("Send a note", form(ctx, "send_note", note)))
        upload = (contact_select(ctx, "To") + '<label class="field"><span>File (up to 10 MB)</span><input type="file" name="file" required></label>'
                  + field_("Note (optional)", "note") + button("Send file"))
        out.append(collapsible("Send a file", form(ctx, "send_file", upload, multipart=True)))
        out.append(_share_status_form(ctx))
    else:
        out.append(empty("Add a contact on the People tab to send notes, files and your status."))
    return "".join(out)


# --------------------------------------------------------------------------- plans tab


def page_plans(ctx: Ctx) -> str:
    plans = ctx.node.plans()
    out = []
    new = (field_("What", "title", required=True, placeholder="Dinner", attrs=' maxlength="120"')
           + contact_checks(ctx, "With")
           + field_("How long (minutes)", "duration", 60, type_="number", attrs=' min="5" max="1440" inputmode="numeric"')
           + window_fields()
           + field_("Where (optional)", "location", attrs=' maxlength="200"')
           + field_("How many must say yes", "quorum", "all", placeholder="all, or a number")
           + '<p class="meta">Confer finds times you are free and asks everyone’s agent which work.</p>'
           + button("Propose plan", "wide"))
    out.append(collapsible("New plan", form(ctx, "create_plan", new), open_=not plans))
    invites = [p for p in plans if awaiting_me(p)]
    invite_ids = {p["id"] for p in invites}
    active = [p for p in plans if p["id"] not in invite_ids and p.get("status") != "cancelled"]
    cancelled = [p for p in plans if p.get("status") == "cancelled"]
    if invites:
        out.append(section("Waiting for your answer"))
        out.extend(plan_card(ctx, p) for p in invites)
    out.append(section("Plans"))
    out.extend(plan_card(ctx, p) for p in active)
    if not active:
        out.append(empty("No active plans."))
    if cancelled:
        out.append(collapsible(f"Cancelled ({len(cancelled)})", "".join(plan_card(ctx, p) for p in cancelled)))
    return "".join(out)


# --------------------------------------------------------------------------- lists tab


def _list_card(ctx: Ctx, lst: dict) -> str:
    me = ctx.node.identity.agent_id
    owner = lst.get("role") == "owner"
    members = ", ".join(lst.get("members", {}).values())
    who = f"Yours · shared with {members}" if owner else f"{lst.get('owner_name', '')}'s list · with {members}"
    rows = []
    for item in lst.get("items", []):
        lid, iid = lst["id"], item["id"]
        done = bool(item.get("done"))
        tick = form(ctx, "list_op", hidden("list_id", lid) + hidden("item", iid) + hidden("op", "uncheck" if done else "check")
                    + f'<button type="submit" class="tick" aria-label="{"Uncheck" if done else "Check off"} {e(item["text"])}">{"✓" if done else ""}</button>')
        claimed = item.get("claimed_by")
        who_has = f'<div class="meta">🙋 {e(item.get("claimed_name") or "someone")} is bringing it</div>' if claimed else ""
        added = f'<div class="meta">added by {e(item["added_by_name"])}</div>' if item.get("added_by_name") and not claimed else ""
        if not claimed:
            claim = form(ctx, "list_op", hidden("list_id", lid) + hidden("item", iid) + hidden("op", "claim") + button("I'll bring it", "secondary small"))
        elif claimed == me or owner:
            claim = form(ctx, "list_op", hidden("list_id", lid) + hidden("item", iid) + hidden("op", "unclaim") + button("Unclaim", "secondary small"))
        else:
            claim = ""
        remove = (form(ctx, "list_op", hidden("list_id", lid) + hidden("item", iid) + hidden("op", "remove")
                       + f'<button type="submit" class="secondary small" aria-label="Remove {e(item["text"])}">✕</button>') if owner else "")
        rows.append(f'<li class="{"done" if done else ""}">{tick}<div class="text">{e(item["text"])}{who_has}{added}</div>{claim}{remove}</li>')
    items = f'<ul class="items">{"".join(rows)}</ul>' if rows else '<p class="empty">No items yet.</p>'
    add = form(ctx, "list_op", hidden("list_id", lst["id"]) + hidden("op", "add")
               + '<div class="row"><input type="text" name="text" placeholder="Add an item" maxlength="200" required class="spacer">'
               + button("Add") + "</div>")
    if owner:
        end = form(ctx, "delete_list", hidden("list_id", lst["id"]) + check("confirm", "1", "Yes, delete it for everyone")
                   + button("Delete list", "danger small"))
    else:
        end = form(ctx, "delete_list", hidden("list_id", lst["id"]) + button("Leave list", "secondary small"))
    return (f'<div class="card"><div class="card-title">{e(lst.get("title", ""))}</div><div class="meta">{e(who)}</div>'
            f"{items}{add}{collapsible('More', end) if owner else end}</div>")


def page_lists(ctx: Ctx) -> str:
    lists = ctx.node.lists()
    new = (field_("Title", "title", required=True, placeholder="BBQ on Saturday", attrs=' maxlength="120"')
           + contact_checks(ctx, "Share with")
           + textarea("Items (one per line)", "items", placeholder="burgers\nbuns\ncharcoal")
           + button("Create list", "wide"))
    out = [collapsible("New list", form(ctx, "create_list", new), open_=not lists)]
    out.append(section("Lists"))
    out.extend(_list_card(ctx, lst) for lst in lists)
    if not lists:
        out.append(empty("No lists yet."))
    return "".join(out)


# --------------------------------------------------------------------------- money tab


def page_money(ctx: Ctx) -> str:
    node = ctx.node
    ledger = node.ledger()
    names = {c.agent_id: c.name for c in node.store.contacts()}
    out = [section("Balances")]
    bal = node.balances()
    if bal:
        rows = []
        for b in bal:
            c, amt = b["balance_cents"], fmt_money(abs(b["balance_cents"]), b["currency"])
            line = (f'{e(b["contact"])} owes you <span class="amount pos">{e(amt)}</span>' if c > 0 else
                    f'You owe {e(b["contact"])} <span class="amount neg">{e(amt)}</span>' if c < 0 else
                    f'{e(b["contact"])} · settled up ({e(b["currency"])})')
            bits = []
            for key, label in (("pending_cents", "waiting for confirmation"), ("disputed_cents", "disputed")):
                if b[key]:
                    side = "they owe" if b[key] > 0 else "you owe"
                    bits.append(f"{fmt_money(abs(b[key]), b['currency'])} {side}, {label}")
            meta = f'<div class="meta">{e(" · ".join(bits))}</div>' if bits else ""
            rows.append(f"<div>{line}{meta}</div>")
        out.append('<div class="card">' + '<hr class="sep">'.join(rows) + "</div>")
    else:
        out.append(empty("No shared expenses yet."))

    waiting = [x for x in ledger if x.get("payer") == "them" and x.get("status") == "pending"]
    if waiting:
        out.append(section("Waiting for you"))
        for x in waiting:
            who = names.get(x["contact"], "someone")
            what = (f"{who} paid {fmt_money(x.get('total_cents', x['cents']), x['currency'])} for {x['title']!r}; your share is "
                    f"{fmt_money(x['cents'], x['currency'])}") if x["kind"] == "expense" else f"{who} says they paid you {fmt_money(x['cents'], x['currency'])}"
            note = f'<div class="meta">“{e(x["note"])}”</div>' if x.get("note") else ""
            link = safe_pay_link(x.get("pay_link"))
            pay = f'<a class="btn secondary" href="{e(link)}" target="_blank" rel="noopener noreferrer">Open pay link</a>' if link else ""
            body = (hidden("entry_id", x["id"]) + field_("Note (optional)", "note") + '<div class="row">'
                    + button("Accept", "ok", "decision", "accept") + button("Dispute", "danger", "decision", "dispute") + "</div>")
            out.append(f'<div class="card hot"><div>{e(what)}</div>{note}{form(ctx, "answer_entry", body)}{pay}</div>')

    ccy = node.config.get("currency", "USD")
    people = ctx.contacts()
    out.append(section("Record"))
    if people:
        expense = (field_("What for", "title", required=True, placeholder="Groceries", attrs=' maxlength="120"')
                   + '<div class="pair">' + field_("Total you paid", "amount", required=True, placeholder="42.00", attrs=' inputmode="decimal"')
                   + field_("Currency", "currency", ccy, attrs=' maxlength="3" autocapitalize="characters"') + "</div>"
                   + contact_checks(ctx, "Split with") + check("include_me", "1", "Include me in the split", checked=True)
                   + field_("Note (optional)", "note", attrs=' maxlength="500"') + button("Ask for their shares", "wide"))
        out.append(collapsible("I paid for something", form(ctx, "add_expense", expense)))
        payment = (contact_select(ctx, "I paid back") + '<div class="pair">'
                   + field_("Amount", "amount", required=True, placeholder="20.00", attrs=' inputmode="decimal"')
                   + field_("Currency", "currency", ccy, attrs=' maxlength="3" autocapitalize="characters"') + "</div>"
                   + field_("Note (optional)", "note", attrs=' maxlength="500"') + button("Record payment", "wide"))
        out.append(collapsible("I paid someone back", form(ctx, "record_payment", payment)))
    else:
        out.append(empty("Add a contact on the People tab to split expenses."))

    out.append(section("History"))
    if ledger:
        rows = []
        for x in reversed(ledger):
            who = names.get(x["contact"], "someone")
            if x["kind"] == "settle":
                desc = f"You paid {who}" if x["payer"] == "me" else f"{who} paid you"
            else:
                desc = f"{x['title']} — {'you paid; ' + who + ' owes' if x['payer'] == 'me' else who + ' paid; you owe'}"
            act = ""
            if x["payer"] == "me" and x["status"] in ("pending", "disputed"):
                act = form(ctx, "cancel_entry", hidden("entry_id", x["id"]) + button("Cancel", "secondary small"))
            elif x["payer"] == "them" and x["status"] == "disputed":
                act = form(ctx, "answer_entry", hidden("entry_id", x["id"]) + button("Accept after all", "secondary small", "decision", "accept"))
            rows.append(f'<li><div class="text">{e(desc)}<div class="meta">{e(local_date(ctx, x.get("created_at", 0)))} · '
                        f'<span class="amount">{e(fmt_money(x["cents"], x["currency"]))}</span> {pill(x["status"])}</div></div>{act}</li>')
        out.append(f'<div class="card"><ul class="items">{"".join(rows)}</ul></div>')
    else:
        out.append(empty("Nothing recorded yet."))
    return "".join(out)


# --------------------------------------------------------------------------- people tab


def _contact_card(ctx: Ctx, c: "Contact") -> str:
    route = " · ".join(x for x in (c.endpoint, f"relay {c.relay}" if c.relay else "") if x) or "no address"
    grants = form(ctx, "set_grants", hidden("contact", c.agent_id) + grant_checks(c.grants) + button("Save permissions"))
    remove = form(ctx, "remove_contact", hidden("contact", c.agent_id)
                  + check("confirm", "1", f"Yes, remove {c.name}") + button("Remove contact", "danger small"))
    status = "" if c.status == "active" else pill("pending", "pairing…")
    return (f'<details class="card"><summary>{e(c.name)}{status}</summary>'
            f'<div class="meta">Fingerprint <span class="mono">{e(fingerprint(c.agent_id))}</span></div>'
            f'<div class="meta">{e(route)}</div>{grants}{remove}</details>')


def page_people(ctx: Ctx) -> str:
    node = ctx.node
    me = node.whoami()
    out = []
    token = ctx.extra.get("invite_token")
    if token:
        out.append(f'<div class="card hot"><div class="card-title">Invite for {e(ctx.extra.get("invite_name", ""))}</div>'
                   '<p class="meta">Send this privately (text or message it). It works once and expires in 72 hours.</p>'
                   f'<textarea id="invite-token" class="token" readonly>{e(token)}</textarea>'
                   '<div class="row"><button type="button" data-copy="invite-token">Copy</button>'
                   '<button type="button" class="secondary" data-share="invite-token" hidden>Share…</button></div>'
                   '<div class="meta js-status" aria-live="polite"></div></div>')
    out.append(f'<div class="card"><div class="card-title">You: {e(me["name"])}</div>'
               f'<div class="meta">Your fingerprint <span class="mono">{e(me["fingerprint"])}</span></div>'
               '<div class="meta">Compare fingerprints in person or on a call to be sure you paired with the right agent.</div></div>')
    invite = (field_("Their name", "name", required=True, placeholder="Sam", attrs=' maxlength="60"')
              + grant_checks(DEFAULT_GRANTS) + button("Create invite", "wide"))
    out.append(collapsible("Invite someone", form(ctx, "create_invite", invite), open_=not ctx.contacts(False)))
    accept = (textarea("Invite you received", "token", required=True, placeholder="confer1:…", attrs=' class="token"')
              + field_("What you call them (optional)", "name", attrs=' maxlength="60"')
              + grant_checks(DEFAULT_GRANTS) + button("Accept invite", "wide"))
    out.append(collapsible("I got an invite", form(ctx, "accept_invite", accept)))
    contacts = ctx.contacts(False)
    out.append(section(f"Contacts ({len(contacts)})"))
    out.extend(_contact_card(ctx, c) for c in contacts)
    if not contacts:
        out.append(empty("No contacts yet."))
    if len(ctx.contacts()) >= 2:
        intro = (contact_select(ctx, "Introduce", "a") + contact_select(ctx, "to", "b")
                 + field_("Note (optional)", "note", placeholder="You both love climbing", attrs=' maxlength="500"')
                 + '<p class="meta">Each of them approves before their agents connect.</p>' + button("Introduce", "wide"))
        out.append(collapsible("Introduce two people", form(ctx, "introduce", intro)))
    return "".join(out)


# --------------------------------------------------------------------------- settings tab


def page_settings(ctx: Ctx) -> str:
    cfg = ctx.node.config
    cal = str(cfg.get("calendar", ""))
    body = (field_("Your name", "name", cfg.get("name", ""), required=True, attrs=' maxlength="60"')
            + field_("Public endpoint", "endpoint", cfg.get("endpoint", ""), type_="url", placeholder="https://you.example.ts.net")
            + field_("Relay (optional)", "relay", cfg.get("relay", ""), type_="url")
            + field_("Time zone", "tz", cfg.get("tz", "UTC"), placeholder="America/New_York")
            + '<div class="pair">' + field_("Available from", "hours_start", cfg.get("hours_start", "08:00"), type_="time")
            + field_("Available until", "hours_end", cfg.get("hours_end", "22:00"), type_="time") + "</div>"
            + field_("Buffer between plans (minutes)", "buffer_minutes", cfg.get("buffer_minutes", 0), type_="number", attrs=' min="0" max="240"')
            + field_("Calendar feed (busy times)", "calendar", cal, placeholder="https://… or webcal://… .ics URL")
            + check("tentative_holds", "1", "Hold offered times while a plan is being decided", checked=bool(cfg.get("tentative_holds", True)))
            + field_("Default currency", "currency", cfg.get("currency", "USD"), attrs=' maxlength="3" autocapitalize="characters"')
            + field_("Pay link (shown to people who owe you)", "pay_link", cfg.get("pay_link", ""), type_="url", placeholder="https://venmo.com/u/you")
            + field_("Remind people after (hours)", "nudge_after_hours", cfg.get("nudge_after_hours", 24), type_="number", attrs=' min="1" max="720" step="any"')
            + button("Save settings", "wide"))
    out = [form(ctx, "save_settings", f'<div class="card">{body}</div>')]
    if cfg.get("notify_cmd"):
        out.append('<div class="card"><div class="card-title">Notification command</div>'
                   f'<div class="mono">{e(cfg["notify_cmd"])}</div>'
                   '<div class="meta">Runs a program on the node, so it can only be changed with the CLI: <code>confer config notify_cmd …</code></div></div>')
    out.append('<div class="card"><div class="card-title">Notification webhook</div>'
               f'<div class="meta">{"Set" if cfg.get("notify_webhook") else "Not set"}. It receives every inbox event, so it can only '
               'be changed with the CLI: <code>confer config notify_webhook https://…</code></div></div>')
    feed = f"{ctx.public_url}/calendar/{cfg.get('feed_token', '')}.ics"
    out.append(section("Calendar"))
    out.append('<div class="card"><div class="card-title">Subscribe to your confirmed plans</div>'
               '<p class="meta">Add this URL to your phone’s calendar as a subscription. Keep it private.</p>'
               f'<textarea id="feed-url" class="token" readonly>{e(feed)}</textarea>'
               '<div class="row"><button type="button" data-copy="feed-url">Copy</button></div><div class="meta js-status"></div></div>')
    out.append(section("Security"))
    rotate_ui = (check("confirm", "1", "Yes, make a new link") + button("Rotate this page's link", "secondary"))
    out.append(collapsible("Rotate this page’s link", '<p class="meta">Use this if the link leaked. The old link stops working immediately; '
                           "you will be taken to the new one — bookmark it.</p>" + form(ctx, "rotate_ui", rotate_ui)))
    rotate_key = (check("confirm", "1", "Yes, rotate my key") + button("Rotate identity key", "danger"))
    out.append(collapsible("Rotate identity key", '<p class="meta">Moves your agent to a fresh keypair; every contact is told automatically. '
                           "Your fingerprint changes.</p>" + form(ctx, "rotate_key", rotate_key)))
    return "".join(out)


# --------------------------------------------------------------------------- trips tab


def _fmt_isodate(iso: str) -> str:
    """Display an ISO UTC datetime string as a local date/time."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M")
    except (ValueError, TypeError):
        return iso


def _fmt_tripdate(node: "Node", iso: str) -> str:
    """Format a UTC ISO time string in the node's local timezone."""
    if not iso:
        return ""
    try:
        tz = ZoneInfo(node.tz)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%b %d %H:%M")
    except (ValueError, TypeError, Exception):
        return iso


def _fmt_tripday(node: "Node", iso: str) -> str:
    """Format a UTC ISO time string as just the local date."""
    if not iso:
        return ""
    try:
        tz = ZoneInfo(node.tz)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, Exception):
        return ""


def _trip_itinerary(ctx: Ctx, trip: dict) -> str:
    """Render itinerary section, grouped by day."""
    node = ctx.node
    me = node.identity.agent_id
    is_owner = trip.get("role") == "owner"
    items = trip.get("itinerary", [])
    out = [section("Itinerary")]
    if items:
        by_day: dict[str, list[dict]] = {}
        no_date: list[dict] = []
        for item in items:
            day = _fmt_tripday(node, item.get("start", ""))
            if day:
                by_day.setdefault(day, []).append(item)
            else:
                no_date.append(item)
        for day in sorted(by_day.keys()):
            out.append(f'<div class="day-group">{e(day)}</div>')
            for item in by_day[day]:
                out.append(_itin_card(ctx, trip, item, is_owner, me))
        for item in no_date:
            out.append(_itin_card(ctx, trip, item, is_owner, me))
    else:
        out.append(empty("No itinerary items yet."))

    kind_opts = "".join(f'<option value="{e(k)}">{e(k.capitalize())}</option>' for k in KINDS)
    add_body = (f'<label class="field"><span>Kind</span><select name="kind">{kind_opts}</select></label>'
                + field_("Title", "itin_title", required=True, attrs=' maxlength="200"')
                + '<div class="pair">' + field_("Start (local time)", "itin_start", type_="datetime-local") + field_("End (local time)", "itin_end", type_="datetime-local") + "</div>"
                + field_("Location", "itin_location", attrs=' maxlength="300"')
                + field_("Confirmation code", "itin_confirmation", attrs=' maxlength="60"')
                + field_("Details", "itin_details", attrs=' maxlength="1000"')
                + field_("URL (https://…)", "itin_url", type_="url", attrs=' maxlength="500"')
                + hidden("trip_id", trip["id"]) + hidden("trip_op", "itinerary.add")
                + button("Add to itinerary", "wide"))
    out.append(collapsible("Add itinerary item", form(ctx, "trip_op", add_body, tab="trips")))
    return "".join(out)


def _itin_card(ctx: Ctx, trip: dict, item: dict, is_owner: bool, me: str) -> str:
    node = ctx.node
    start = _fmt_tripdate(node, item.get("start", ""))
    end = _fmt_tripdate(node, item.get("end", ""))
    time_range = f"{e(start)} → {e(end)}" if start and end else e(start or end)
    url = item.get("url", "")
    title_html = (f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(item.get("title", ""))}</a>'
                  if url and url.startswith("https://") else e(item.get("title", "")))
    parts = [f'<div class="card"><div class="card-title">{e(item.get("kind", "").capitalize())} — {title_html}</div>']
    if time_range:
        parts.append(f'<div class="meta">{time_range}</div>')
    if item.get("location"):
        parts.append(f'<div class="meta">📍 {e(item["location"])}</div>')
    if item.get("confirmation"):
        parts.append(f'<div class="meta">Confirmation: {e(item["confirmation"])}</div>')
    if item.get("details"):
        parts.append(f'<div class="meta">{e(item["details"])}</div>')
    if item.get("added_by_name"):
        parts.append(f'<div class="meta">Added by {e(item["added_by_name"])}</div>')
    can_remove = is_owner or item.get("added_by") == me
    if can_remove:
        rm = form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "itinerary.remove")
                  + hidden("trip_item_id", item["id"]) + button("Remove", "danger secondary small"), tab="trips")
        parts.append(rm)
    parts.append("</div>")
    return "".join(parts)


def _trip_travel(ctx: Ctx, trip: dict) -> str:
    """My arrival/departure section."""
    node = ctx.node
    me = node.identity.agent_id
    my_travel = trip.get("travelers", {}).get(me, {})
    arr = my_travel.get("arrive", {})
    dep = my_travel.get("depart", {})
    # datetime-local input needs a local-time value; convert stored UTC ISO back to local
    def iso_to_local_dt(iso: str) -> str:
        if not iso:
            return ""
        try:
            tz = ZoneInfo(node.tz)
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
            return dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return ""

    body = ('<fieldset><legend>Arrival</legend>'
            + field_("When", "arr_when", iso_to_local_dt(arr.get("when", "")), type_="datetime-local")
            + field_("How (flight number, train, etc.)", "arr_how", arr.get("how", ""), attrs=' maxlength="120"')
            + field_("Arriving at", "arr_where", arr.get("where", ""), attrs=' maxlength="200"')
            + check("arr_pickup", "1", "I need a pickup", checked=bool(arr.get("needs_pickup")))
            + '</fieldset><fieldset><legend>Departure</legend>'
            + field_("When", "dep_when", iso_to_local_dt(dep.get("when", "")), type_="datetime-local")
            + field_("How", "dep_how", dep.get("how", ""), attrs=' maxlength="120"')
            + field_("Departing from", "dep_where", dep.get("where", ""), attrs=' maxlength="200"')
            + check("dep_pickup", "1", "I need a pickup for departure", checked=bool(dep.get("needs_pickup")))
            + '</fieldset>'
            + field_("Notes", "travel_notes", my_travel.get("notes", ""), attrs=' maxlength="500"')
            + hidden("trip_id", trip["id"]) + hidden("trip_op", "traveler.set")
            + button("Save my travel info", "wide"))
    return section("Your travel") + form(ctx, "trip_op", body, tab="trips")


def _trip_arrivals(ctx: Ctx, trip: dict) -> str:
    """Everyone's arrivals for the owner/members to see."""
    travelers = trip.get("travelers", {})
    out = [section("Everyone's arrivals")]
    if not travelers:
        out.append(empty("No arrival info yet."))
        return "".join(out)
    rows = []
    for _aid, tv in travelers.items():
        arr = tv.get("arrive", {})
        dep = tv.get("depart", {})
        parts = [f'<div class="card-title">{e(tv.get("name", ""))}']
        if arr.get("needs_pickup") or dep.get("needs_pickup"):
            parts.append(pill("pickup", "needs pickup"))
        parts.append("</div>")
        if arr.get("when") or arr.get("how") or arr.get("where"):
            a_str = " · ".join(x for x in (_fmt_isodate(arr.get("when", "")), arr.get("how", ""), arr.get("where", "")) if x)
            parts.append(f'<div class="meta">Arrives: {e(a_str)}</div>')
        if dep.get("when") or dep.get("how") or dep.get("where"):
            d_str = " · ".join(x for x in (_fmt_isodate(dep.get("when", "")), dep.get("how", ""), dep.get("where", "")) if x)
            parts.append(f'<div class="meta">Departs: {e(d_str)}</div>')
        if tv.get("notes"):
            parts.append(f'<div class="meta">{e(tv["notes"])}</div>')
        rows.append('<div class="card">' + "".join(parts) + "</div>")
    out.extend(rows)
    return "".join(out)


def _trip_rides(ctx: Ctx, trip: dict) -> str:
    node = ctx.node
    me = node.identity.agent_id
    is_owner = trip.get("role") == "owner"
    rides = trip.get("rides", [])
    out = [section("Rides")]
    for ride in rides:
        taken = len(ride.get("passengers", {}))
        seats_left = ride["seats"] - taken
        full = seats_left <= 0
        im_driver = ride["driver_id"] == me
        im_passenger = me in ride.get("passengers", {})
        passengers = ", ".join(ride.get("passengers", {}).values()) or "none yet"
        parts = [f'<div class="card"><div class="card-title">{e(ride["driver_name"])}\'s car']
        if full:
            parts.append(pill("cancelled", "full"))
        parts.append("</div>")
        if ride.get("from"):
            parts.append(f'<div class="meta">From: {e(ride["from"])}</div>')
        if ride.get("leaves_at"):
            parts.append(f'<div class="meta">Leaves: {e(_fmt_isodate(ride["leaves_at"]))}</div>')
        parts.append(f'<div class="meta">{e(str(seats_left))} seat{"s" if seats_left != 1 else ""} left · passengers: {e(passengers)}</div>')
        row = []
        if not im_driver and not im_passenger and not full:
            row.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "ride.join")
                           + hidden("trip_item_id", ride["id"]) + button("Join ride", "ok"), tab="trips"))
        if im_passenger:
            row.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "ride.leave")
                           + hidden("trip_item_id", ride["id"]) + button("Leave ride", "secondary"), tab="trips"))
        if im_driver or is_owner:
            row.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "ride.cancel")
                           + hidden("trip_item_id", ride["id"]) + button("Cancel ride", "danger secondary small"), tab="trips"))
        if row:
            parts.append('<div class="row">' + "".join(row) + "</div>")
        parts.append("</div>")
        out.append("".join(parts))
    if not rides:
        out.append(empty("No rides offered yet."))
    offer_body = (field_("Seats available", "ride_seats", "3", type_="number", attrs=' min="1" max="50" required')
                  + field_("Departing from", "ride_from", attrs=' maxlength="200"')
                  + field_("Leaving at (local time)", "ride_leaves_at", type_="datetime-local")
                  + hidden("trip_id", trip["id"]) + hidden("trip_op", "ride.offer")
                  + button("Offer a ride", "wide"))
    out.append(collapsible("Offer a ride", form(ctx, "trip_op", offer_body, tab="trips")))
    return "".join(out)


def _trip_rooms(ctx: Ctx, trip: dict) -> str:
    node = ctx.node
    me = node.identity.agent_id
    is_owner = trip.get("role") == "owner"
    rooms = trip.get("rooms", [])
    out = [section("Rooms")]
    for room in rooms:
        taken = len(room.get("occupants", {}))
        full = taken >= room["beds"]
        im_in = me in room.get("occupants", {})
        occupants = ", ".join(room.get("occupants", {}).values()) or "no one yet"
        parts = [f'<div class="card"><div class="card-title">{e(room["name"])}']
        if full:
            parts.append(pill("cancelled", "full"))
        parts.append("</div>")
        parts.append(f'<div class="meta">{e(str(room["beds"]))} bed{"s" if room["beds"] != 1 else ""} · {e(occupants)}</div>')
        row = []
        if not im_in and not full:
            row.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "room.join")
                           + hidden("trip_item_id", room["id"]) + button("Join room", "ok"), tab="trips"))
        if im_in:
            row.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "room.leave")
                           + hidden("trip_item_id", room["id"]) + button("Leave room", "secondary"), tab="trips"))
        if is_owner:
            row.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "room.remove")
                           + hidden("trip_item_id", room["id"]) + button("Remove", "danger secondary small"), tab="trips"))
        if row:
            parts.append('<div class="row">' + "".join(row) + "</div>")
        parts.append("</div>")
        out.append("".join(parts))
    if not rooms:
        out.append(empty("No rooms added yet."))
    if is_owner:
        add_body = (field_("Room name", "room_name", placeholder="Master bedroom", attrs=' maxlength="80"')
                    + field_("Beds", "room_beds", "2", type_="number", attrs=' min="1" max="50" required')
                    + hidden("trip_id", trip["id"]) + hidden("trip_op", "room.add")
                    + button("Add room", "wide"))
        out.append(collapsible("Add a room", form(ctx, "trip_op", add_body, tab="trips")))
    return "".join(out)


def _trip_tasks(ctx: Ctx, trip: dict) -> str:
    node = ctx.node
    me = node.identity.agent_id
    is_owner = trip.get("role") == "owner"
    tasks = trip.get("tasks", [])
    out = [section("Tasks")]
    if tasks:
        rows = []
        for task in tasks:
            mine = task.get("assignee_id") == me
            done = task.get("done", False)
            op = "task.undone" if done else "task.done"
            tick = form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", op)
                        + hidden("trip_item_id", task["id"])
                        + f'<button type="submit" class="tick" aria-label="{"Uncheck" if done else "Check off"} {e(task["text"])}">{"✓" if done else ""}</button>',
                        tab="trips")
            assignee = f'<div class="meta">→ {e(task.get("assignee_name", ""))}</div>' if task.get("assignee_name") else ""
            due = f'<div class="meta">Due: {e(task.get("due", ""))}</div>' if task.get("due") else ""
            can_remove = is_owner or task.get("added_by") == me
            rm = ""
            if can_remove:
                rm = form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "task.remove")
                          + hidden("trip_item_id", task["id"])
                          + f'<button type="submit" class="secondary small" aria-label="Remove task">✕</button>', tab="trips")
            text_cls = "done" if done else ""
            name_cls = ' class="mine"' if mine and not done else ""
            rows.append(f'<li class="{text_cls}">{tick}<div class="text"><span{name_cls}>{e(task["text"])}</span>{assignee}{due}</div>{rm}</li>')
        out.append(f'<div class="card"><ul class="items">{"".join(rows)}</ul></div>')
    else:
        out.append(empty("No tasks yet."))

    # Add task form — assignee options: me + all trip members
    people = {trip["owner"]: trip.get("owner_name", ""), **trip.get("members", {})}
    assignee_opts = '<option value="">— unassigned —</option><option value="me">Me</option>'
    for aid, aname in people.items():
        if aid != node.identity.agent_id:
            assignee_opts += f'<option value="{e(aid)}">{e(aname)}</option>'
    add_body = (field_("Task", "task_text", required=True, attrs=' maxlength="200"')
                + f'<label class="field"><span>Assign to</span><select name="task_assignee">{assignee_opts}</select></label>'
                + field_("Due date (optional)", "task_due", type_="date")
                + hidden("trip_id", trip["id"]) + hidden("trip_op", "task.add")
                + button("Add task", "wide"))
    out.append(collapsible("Add task", form(ctx, "trip_op", add_body, tab="trips")))
    return "".join(out)


def _trip_polls(ctx: Ctx, trip: dict) -> str:
    node = ctx.node
    me = node.identity.agent_id
    is_owner = trip.get("role") == "owner"
    polls = trip.get("polls", [])
    out = [section("Polls")]
    for poll in polls:
        my_vote = poll.get("votes", {}).get(me, "")
        votes = poll.get("votes", {})
        total = len(votes)
        parts = [f'<div class="card"><div class="card-title">{e(poll["question"])}']
        if poll.get("closed"):
            parts.append(pill("cancelled", "closed"))
        parts.append("</div>")
        if not poll.get("closed"):
            # Show vote buttons
            vote_btns = []
            for opt in poll.get("options", []):
                count = sum(1 for v in votes.values() if v == opt["id"])
                active = " ok" if my_vote == opt["id"] else " secondary"
                vote_btns.append(form(ctx, "trip_op",
                                      hidden("trip_id", trip["id"]) + hidden("trip_op", "poll.vote")
                                      + hidden("trip_item_id", poll["id"]) + hidden("poll_option", opt["id"])
                                      + f'<button type="submit" class="{active.strip()}">{e(opt["text"])} ({count})</button>', tab="trips"))
            parts.append('<div class="row">' + "".join(vote_btns) + "</div>")
        else:
            # Show results
            rows = []
            for opt in poll.get("options", []):
                count = sum(1 for v in votes.values() if v == opt["id"])
                pct = f"{count}/{total}" if total else "0/0"
                winner = my_vote == opt["id"]
                rows.append(f'<div class="{"mine" if winner else ""}">{e(opt["text"])}: {e(pct)}</div>')
            parts.extend(rows)
        if total:
            parts.append(f'<div class="meta">{e(str(total))} vote{"s" if total != 1 else ""} so far</div>')
        can_close = not poll.get("closed") and (is_owner or poll.get("added_by") == me)
        if can_close:
            parts.append(form(ctx, "trip_op", hidden("trip_id", trip["id"]) + hidden("trip_op", "poll.close")
                              + hidden("trip_item_id", poll["id"]) + button("Close poll", "secondary small"), tab="trips"))
        parts.append("</div>")
        out.append("".join(parts))
    if not polls:
        out.append(empty("No polls yet."))
    # Add poll form
    add_body = (field_("Question", "poll_question", required=True, attrs=' maxlength="200"')
                + textarea("Options (one per line, at least 2)", "poll_options", required=True, placeholder="Cabin in the woods\nBeach house\nCity hotel")
                + hidden("trip_id", trip["id"]) + hidden("trip_op", "poll.add")
                + button("Add poll", "wide"))
    out.append(collapsible("Add poll", form(ctx, "trip_op", add_body, tab="trips")))
    return "".join(out)


def _trip_budget(ctx: Ctx, trip: dict) -> str:
    from ..money import fmt as fmt_m
    out = [section("Budget")]
    try:
        budget = ctx.node.trip_budget(trip["id"])
    except Exception:
        budget = {}
    if budget:
        rows = []
        for ccy, b in budget.items():
            paid = fmt_m(b.get("you_paid_shares", 0), ccy)
            owe = fmt_m(b.get("you_owe", 0), ccy)
            rows.append(f'<div>You paid: <span class="amount pos">{e(paid)}</span> · You owe: <span class="amount neg">{e(owe)}</span> ({e(ccy)})</div>')
        out.append('<div class="card">' + "<hr class='sep'>".join(rows) + "</div>")
        out.append(f'<p class="meta"><a href="{e(ctx.base)}?tab=money">View full balances on Money tab</a></p>')
    else:
        out.append(empty("No trip expenses recorded yet."))
    # Quick add expense shortcut
    ccy = ctx.node.config.get("currency", "USD")
    people = {trip["owner"]: trip.get("owner_name", ""), **trip.get("members", {})}
    me = ctx.node.identity.agent_id
    # contact checkboxes for trip members (excluding self)
    contact_boxes = []
    for aid, aname in people.items():
        if aid != me:
            c = ctx.node.store.contact(aid)
            if c:
                contact_boxes.append(check("contact", c.agent_id, c.name))
    if contact_boxes:
        expense_body = (field_("What for", "title", required=True, attrs=' maxlength="120"')
                        + '<div class="pair">' + field_("Amount you paid", "amount", required=True, attrs=' inputmode="decimal"')
                        + field_("Currency", "currency", ccy, attrs=' maxlength="3" autocapitalize="characters"') + "</div>"
                        + '<fieldset><legend>Split with</legend>' + "".join(contact_boxes) + '</fieldset>'
                        + check("include_me", "1", "Include me in the split", checked=True)
                        + hidden("plan_id", trip["id"])
                        + button("Add expense", "wide"))
        out.append(collapsible("Add expense for this trip", form(ctx, "add_expense", expense_body, tab="trips")))
    return "".join(out)


def _trip_detail(ctx: Ctx, trip: dict) -> str:
    """Full trip detail page."""
    node = ctx.node
    is_owner = trip.get("role") == "owner"
    me = node.identity.agent_id
    out = []

    # Header
    members = ", ".join(trip.get("members", {}).values()) or "no members"
    owner_name = trip.get("owner_name", "")
    role_display = "You're organizing" if is_owner else f"Organized by {owner_name}"
    dates = f"{trip.get('start_date', '')} → {trip.get('end_date', '')}"
    status = trip.get("status", "planning")
    parts = [f'<div class="card"><div class="card-title">{e(trip.get("title", ""))}{pill(status)}</div>']
    if trip.get("destination"):
        parts.append(f'<div class="meta">📍 {e(trip["destination"])}</div>')
    parts.append(f'<div class="meta">{e(dates)}</div>')
    parts.append(f'<div class="meta">{e(role_display)}</div>')
    parts.append(f'<div class="meta">People: {e(members)}</div>')
    if trip.get("notes"):
        parts.append(f'<div class="meta">{e(trip["notes"])}</div>')
    parts.append("</div>")
    out.append("".join(parts))

    # Owner edit form
    if is_owner:
        status_opts = "".join(f'<option value="{e(s)}"{" selected" if s == status else ""}>{e(s.capitalize())}</option>' for s in STATUSES)
        edit_body = (field_("Title", "trip_title", trip.get("title", ""), required=True, attrs=' maxlength="120"')
                     + field_("Destination", "trip_destination", trip.get("destination", ""), attrs=' maxlength="200"')
                     + '<div class="pair">' + field_("Start date", "trip_start_date", trip.get("start_date", ""), type_="date")
                     + field_("End date", "trip_end_date", trip.get("end_date", ""), type_="date") + "</div>"
                     + textarea("Notes", "trip_notes", trip.get("notes", ""), attrs=' maxlength="2000"')
                     + f'<label class="field"><span>Status</span><select name="trip_status">{status_opts}</select></label>'
                     + hidden("trip_id", trip["id"]) + hidden("trip_op", "trip.update")
                     + button("Save trip details", "wide"))
        out.append(collapsible("Edit trip details", form(ctx, "trip_op", edit_body, tab="trips")))

    # Sections
    out.append(_trip_itinerary(ctx, trip))
    out.append(_trip_travel(ctx, trip))
    out.append(_trip_arrivals(ctx, trip))
    out.append(_trip_rides(ctx, trip))
    out.append(_trip_rooms(ctx, trip))
    out.append(_trip_tasks(ctx, trip))
    out.append(_trip_polls(ctx, trip))
    out.append(_trip_budget(ctx, trip))

    # Packing list link
    list_id = trip.get("links", {}).get("list_id")
    if list_id:
        out.append(section("Packing list"))
        out.append(f'<div class="card"><a href="{e(ctx.base)}?tab=lists">Open packing list on Lists tab</a></div>')

    # Leave / cancel trip
    if is_owner and status != "cancelled":
        cancel_body = (check("confirm", "1", "Yes, cancel the trip for everyone")
                       + hidden("trip_id", trip["id"]) + button("Cancel trip", "danger"))
        out.append(collapsible("Cancel trip", form(ctx, "cancel_trip", cancel_body, tab="trips")))
    elif not is_owner:
        leave_body = (check("confirm", "1", "Yes, leave this trip")
                      + hidden("trip_id", trip["id"]) + hidden("trip_op", "leave")
                      + button("Leave trip", "danger secondary small"))
        out.append(collapsible("Leave trip", form(ctx, "trip_op", leave_body, tab="trips")))
    return "".join(out)


def page_trips(ctx: Ctx) -> str:
    """Trips tab: list of trips + detail view when ?trip=<id>."""
    node = ctx.node
    trip_id = ctx.extra.get("trip_id") or ""
    if not trip_id:
        # Try to get from the original query string — store it in extra via the GET handler
        pass  # trip_id is injected via ctx.extra["trip_id"] from handle_get

    if trip_id:
        try:
            trip = node.get_trip(trip_id)
            return _trip_detail(ctx, trip)
        except Exception:
            pass  # fall through to list

    # Trip list
    trips = sorted(node.trips(), key=lambda t: (t.get("status") == "cancelled", t.get("start_date", "")))
    out = []

    contacts = ctx.contacts()
    new_body = (field_("Title", "trip_title", required=True, placeholder="Summer cabin", attrs=' maxlength="120"')
                + field_("Destination", "trip_destination", placeholder="Lake Tahoe", attrs=' maxlength="200"')
                + '<div class="pair">' + field_("Start date", "trip_start_date", type_="date", required=True)
                + field_("End date", "trip_end_date", type_="date", required=True) + "</div>"
                + (contact_checks(ctx, "Who's coming") if contacts else '<p class="empty">No contacts yet — invite someone on the People tab.</p>')
                + check("packing_list", "1", "Create a shared packing list", checked=True)
                + textarea("Notes (optional)", "trip_notes", attrs=' maxlength="2000"')
                + button("Create trip", "wide"))
    out.append(collapsible("New trip", form(ctx, "create_trip", new_body, tab="trips"), open_=not trips))

    out.append(section("Trips"))
    if not trips:
        out.append(empty("No trips yet."))
    else:
        active = [t for t in trips if t.get("status") != "cancelled"]
        cancelled = [t for t in trips if t.get("status") == "cancelled"]
        for trip in active:
            dest = f" · {trip['destination']}" if trip.get("destination") else ""
            dates = f"{trip.get('start_date', '')} → {trip.get('end_date', '')}"
            status = trip.get("status", "planning")
            role = trip.get("role", "member")
            href = f"{e(ctx.base)}?tab=trips&amp;trip={e(trip['id'])}"
            out.append(f'<a href="{href}" class="card card-link">'
                       f'<div class="card-title">{e(trip.get("title", ""))}{pill(status)}{pill(role)}</div>'
                       f'<div class="meta">{e(dates)}{e(dest)}</div></a>')
        if cancelled:
            inner = []
            for trip in cancelled:
                dates = f"{trip.get('start_date', '')} → {trip.get('end_date', '')}"
                href = f"{e(ctx.base)}?tab=trips&amp;trip={e(trip['id'])}"
                inner.append(f'<a href="{href}" class="card card-link">'
                             f'<div class="card-title">{e(trip.get("title", ""))}{pill("cancelled")}</div>'
                             f'<div class="meta">{e(dates)}</div></a>')
            out.append(collapsible(f"Cancelled ({len(cancelled)})", "".join(inner)))
    return "".join(out)


PAGES = {"inbox": page_inbox, "plans": page_plans, "lists": page_lists, "money": page_money,
         "people": page_people, "trips": page_trips, "settings": page_settings}


def render(ctx: Ctx) -> bytes:
    return shell(ctx, PAGES[ctx.tab](ctx))
