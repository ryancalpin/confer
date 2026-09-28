"""``confer`` command line. Every command is a thin wrapper over :class:`Node`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import plans as P
from .availability import Interval
from .node import GRANTS, Node, NodeError, check_url

CONFIG_KEYS = {"name", "endpoint", "relay", "tz", "hours_start", "hours_end", "buffer_minutes", "calendar", "notify_webhook", "notify_cmd",
               "currency", "pay_link", "tentative_holds", "trips_block_calendar", "nudge_after_hours"}


def home_dir(args: argparse.Namespace) -> Path:
    return Path(args.home or os.environ.get("CONFER_HOME") or "~/.confer").expanduser()


def _local_dt(text: str, tz: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return (dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))).astimezone(ZoneInfo("UTC"))


def _window(args: argparse.Namespace, tz: str) -> tuple[datetime | None, datetime | None]:
    start = _local_dt(args.start, tz) if args.start else None
    end = _local_dt(args.end, tz) if args.end else None
    if end and args.end and len(args.end) == 10:  # a bare date means "through the end of that day"
        end += timedelta(days=1)
    return start, end


def _between(spec: str | None) -> tuple[dtime, dtime] | None:
    if not spec:
        return None
    a, b = spec.split("-")
    return dtime.fromisoformat(a), dtime.fromisoformat(b)


def _slot(spec: str, tz: str, default_minutes: int = 60) -> Interval:
    """``2026-10-02T19:00`` or ``2026-10-02T19:00/90`` (minutes)."""
    when, _, mins = spec.partition("/")
    start = _local_dt(when, tz)
    return Interval(start, start + timedelta(minutes=int(mins or default_minutes)))


def _indexes(spec: str | None) -> list[int] | None:
    return None if not spec else [int(x) - 1 for x in spec.split(",") if x.strip()]


def _print(obj: object, as_json: bool) -> None:
    print(json.dumps(obj, indent=2, default=str) if as_json else obj)


def cmd_init(a: argparse.Namespace) -> None:
    node = Node.init(home_dir(a), a.name, endpoint=a.endpoint or "", relay=a.relay or "", tz=a.tz)
    w = node.whoami()
    print(f"Created Confer node for {w['name']} at {home_dir(a)}\n  agent id:    {w['agent_id']}\n  fingerprint: {w['fingerprint']}")
    if not (a.endpoint or a.relay):
        print("Next: `confer config endpoint https://<your-public-url>` (or `confer config relay <relay-url>`)")


def cmd_whoami(a: argparse.Namespace, node: Node) -> None:
    w = node.whoami()
    _print(w if a.json else "\n".join(f"{k:12} {v}" for k, v in w.items()), a.json)


def cmd_config(a: argparse.Namespace, node: Node) -> None:
    if a.key is None:
        _print({k: v for k, v in node.config.items() if k != "feed_token"}, True)
        return
    if a.key not in CONFIG_KEYS:
        raise NodeError(f"unknown key; choose from {', '.join(sorted(CONFIG_KEYS))}")
    if a.value is None:
        print(node.config.get(a.key, ""))
        return
    value: object = a.value
    if a.key in ("endpoint", "relay", "notify_webhook", "pay_link") and a.value:
        value = check_url(a.value, allow_query=a.key in ("notify_webhook", "pay_link"))
        if a.key == "pay_link" and not str(value).startswith("https://"):
            raise NodeError("pay_link must start with https://")
    if a.key in ("tentative_holds", "trips_block_calendar"):
        value = a.value.lower() in ("1", "true", "yes", "on")
    if a.key == "nudge_after_hours":
        value = float(a.value)
    if a.key == "currency":
        value = a.value.upper()
        if len(value) != 3 or not value.isalpha():
            raise NodeError("currency must be a 3-letter code like USD")
    if a.key == "buffer_minutes":
        value = int(a.value)
    if a.key == "tz":
        ZoneInfo(a.value)
    node.config[a.key] = value
    node.save_config()
    print(f"{a.key} = {value}")


def cmd_invite(a: argparse.Namespace, node: Node) -> None:
    token = node.create_invite(a.name, a.grant, a.ttl_hours)
    print(f"Send this to {a.name} privately (it works once, for {a.ttl_hours:g}h):\n\n{token}\n")
    print(f"They run:  confer accept '{token[:24]}…'")
    print(f"Your fingerprint: {node.identity.fingerprint}  (compare it with them to be sure)")


def cmd_accept(a: argparse.Namespace, node: Node) -> None:
    c = node.accept_invite(a.token, name=a.name, grants=a.grant)
    from .identity import fingerprint

    print(f"Pairing request sent to {c.name} (fingerprint {fingerprint(c.agent_id)}). Grants you gave them: {', '.join(c.grants) or 'none'}.")


def cmd_contacts(a: argparse.Namespace, node: Node) -> None:
    from .identity import fingerprint

    rows = [{"name": c.name, "status": c.status, "grants": c.grants, "fingerprint": fingerprint(c.agent_id), "endpoint": c.endpoint, "relay": c.relay} for c in node.store.contacts()]
    if a.json:
        return _print(rows, True)
    if not rows:
        print("No contacts yet. `confer invite <name>` to add a trusted person.")
    for r in rows:
        print(f"{r['name']:20} {r['status']:8} {','.join(r['grants']) or '-':30} {r['fingerprint']}")


def cmd_grant(a: argparse.Namespace, node: Node) -> None:
    c = node.set_grants(a.name, ",".join(a.grants))
    print(f"{c.name}: {', '.join(c.grants) or 'no grants'}")


def cmd_remove(a: argparse.Namespace, node: Node) -> None:
    node.remove_contact(a.name)
    print(f"Removed {a.name}.")


def cmd_plan_new(a: argparse.Namespace, node: Node) -> None:
    start, end = _window(a, node.tz)
    slots = [_slot(s, node.tz, a.duration) for s in a.slot] if a.slot else None
    quorum: str | int = "all" if a.quorum == "all" else int(a.quorum)
    plan = node.create_plan(
        a.title, [w.strip() for w in a.with_.split(",") if w.strip()], duration_minutes=a.duration,
        window_start=start, window_end=end, between=_between(a.between), slots=slots, candidates=a.options,
        rrule=a.rrule, location=a.location or "", notes=a.notes or "", quorum=quorum, deadline_hours=a.deadline_hours,
    )
    _print(plan if a.json else "Proposed:\n" + P.describe(plan, node.tz), a.json)


def cmd_plan_list(a: argparse.Namespace, node: Node) -> None:
    plans = node.plans()
    if a.json:
        return _print(plans, True)
    if not plans:
        print("No plans.")
    for p in plans:
        mine = p.get("my_status", "organizer") if p.get("role") == "participant" else "organizer"
        print(f"{p['id']}  {p['status']:17} {mine:10} {p['title']}")


def cmd_plan_show(a: argparse.Namespace, node: Node) -> None:
    plan = node._plan(a.id)
    _print(plan if a.json else P.describe(plan, node.tz), a.json)


def cmd_plan_respond(a: argparse.Namespace, node: Node) -> None:
    counter = [_slot(s, node.tz) for s in a.suggest] if a.suggest else None
    plan = node.respond(a.id, a.decision, slots=_indexes(a.slots), note=a.note or "", counter=counter, prefer=_indexes(a.prefer))
    print(f"Sent {a.decision} for {plan['title']!r}.")


def cmd_plan_revise(a: argparse.Namespace, node: Node) -> None:
    start, end = _window(a, node.tz)
    slots = [_slot(s, node.tz, a.duration or 60) for s in a.slot] if a.slot else None
    plan = node.revise(a.id, slots=slots, duration_minutes=a.duration, window_start=start, window_end=end, between=_between(a.between), candidates=a.options)
    print("Re-proposed:\n" + P.describe(plan, node.tz))


def cmd_plan_cancel(a: argparse.Namespace, node: Node) -> None:
    plan = node.cancel(a.id, a.reason or "")
    print(f"Cancelled {plan['title']!r}." if plan.get("role") == "organizer" else f"Declined {plan['title']!r}.")


def cmd_inbox(a: argparse.Namespace, node: Node) -> None:
    items = node.inbox(a.all)
    if a.json:
        return _print(items, True)
    if not items:
        print("Inbox empty.")
    for i in items:
        flag = "❗" if i["actionable"] and i["status"] == "open" else "  "
        print(f"{flag}#{i['id']:<4} {i['summary']}")


def cmd_done(a: argparse.Namespace, node: Node) -> None:
    for item in a.ids:
        node.dismiss(item)
    print("OK")


def cmd_send_file(a: argparse.Namespace, node: Node) -> None:
    node.send_file(a.to, Path(a.path), a.note or "")
    print(f"Queued {a.path} for {a.to} (end-to-end encrypted).")


def cmd_note(a: argparse.Namespace, node: Node) -> None:
    msg_id = node.send_note(a.to, a.text, reply_to=a.reply_to or "", expects_reply=a.ask)
    print(f"Sent to {a.to} (message {msg_id}).")


def cmd_introduce(a: argparse.Namespace, node: Node) -> None:
    node.introduce(a.a, a.b, a.note or "")
    print(f"Introduced {a.a} and {a.b}. They'll each be asked to approve.")


def cmd_intro_list(a: argparse.Namespace, node: Node) -> None:
    from .identity import fingerprint

    rows = node.intros()
    if a.json:
        return _print(rows, True)
    if not rows:
        print("No introductions.")
    for r in rows:
        via = node.store.contact(r["introducer"])
        print(f"{r['intro_id']}  {r['status']:9} {r['peer_name']} (fp {fingerprint(r['peer_id'])}) via {via.name if via else '?'}")


def cmd_intro_accept(a: argparse.Namespace, node: Node) -> None:
    c = node.accept_intro(a.id, name=a.name, grants=a.grant)
    print(f"Accepted — connecting with {c.name}." if c.status == "pending" else f"Connected with {c.name}.")


def cmd_intro_decline(a: argparse.Namespace, node: Node) -> None:
    node.decline_intro(a.id)
    print("Declined.")


def cmd_rotate_key(a: argparse.Namespace, node: Node) -> None:
    if not a.yes:
        raise NodeError("this replaces your identity key; every contact is told automatically. Re-run with --yes")
    from .identity import fingerprint

    new_id = node.rotate_key()
    print(f"New key in place (fingerprint {fingerprint(new_id)}). Contacts are being notified; "
          "the old key keeps working for 30 days for messages already in flight.")


def cmd_remind(a: argparse.Namespace, node: Node) -> None:
    print(f"Sent {node.send_reminders()} reminder(s).")


def _names(spec: str | None) -> list[str]:
    return [n.strip() for n in (spec or "").split(",") if n.strip()]


def _show_list(lst: dict) -> str:
    mine = "yours" if lst.get("role") == "owner" else f"from {lst.get('owner_name', '?')}"
    lines = [f"{lst['title']}  ({mine}, with {', '.join(lst['members'].values()) or 'nobody'})  id={lst['id']}"]
    for n, i in enumerate(lst["items"], 1):
        who = f"  ← {i['claimed_name']}" if i["claimed_name"] else ""
        lines.append(f"  {n:>2}. [{'x' if i['done'] else ' '}] {i['text']}{who}")
    return "\n".join(lines)


def cmd_list_new(a: argparse.Namespace, node: Node) -> None:
    lst = node.create_list(a.title, _names(a.with_), items=a.item or [])
    _print(lst if a.json else "Shared:\n" + _show_list(lst), a.json)


def cmd_list_ls(a: argparse.Namespace, node: Node) -> None:
    lists = node.lists()
    if a.json:
        return _print(lists, True)
    if not lists:
        print("No lists.")
    for lst in lists:
        done = sum(i["done"] for i in lst["items"])
        print(f"{lst['id']}  {lst['title']}  {done}/{len(lst['items'])} done  ({'owner' if lst.get('role') == 'owner' else lst.get('owner_name')})")


def cmd_list_show(a: argparse.Namespace, node: Node) -> None:
    lst = node.get_list(a.id)
    _print(lst if a.json else _show_list(lst), a.json)


def cmd_list_edit(a: argparse.Namespace, node: Node) -> None:
    op = a.list_cmd
    if op == "add":
        node.list_op(a.id, "add", text=" ".join(a.text))
    elif op in ("leave", "delete"):
        node.delete_list(a.id)
        return print("Done.")
    else:
        node.list_op(a.id, op, item=a.item)
    print(_show_list(node.get_list(a.id)) + ("" if node.get_list(a.id).get("role") == "owner" else "\n(sent to the owner; refreshes when they confirm)"))


def cmd_money_split(a: argparse.Namespace, node: Node) -> None:
    shares = dict(kv.split("=", 1) for kv in _names(a.shares)) if a.shares else None
    entries = node.add_expense(a.title, a.amount, _names(a.with_), currency=a.currency, shares=shares,
                               include_me=not a.exclude_me, note=a.note or "", plan_id=a.plan or "")
    from .money import fmt

    for e in entries:
        c = node.store.contact(e["contact"])
        print(f"Asked {c.name if c else '?'} for {fmt(e['cents'], e['currency'])} ({e['id']})")


def cmd_money_balances(a: argparse.Namespace, node: Node) -> None:
    rows = node.balances()
    if a.json:
        return _print(rows, True)
    if not rows:
        print("All square — no shared expenses yet.")
    from .money import fmt

    for r in rows:
        b = r["balance_cents"]
        line = f"{r['contact']:16} " + ("owes you " + fmt(b, r["currency"]) if b > 0 else "you owe " + fmt(-b, r["currency"]) if b < 0 else "square")
        if r["pending_cents"]:
            line += f"   (pending {fmt(r['pending_cents'], r['currency'])})"
        if r["disputed_cents"]:
            line += f"   (disputed {fmt(r['disputed_cents'], r['currency'])})"
        print(line)


def cmd_money_ledger(a: argparse.Namespace, node: Node) -> None:
    rows = node.ledger(a.with_)
    if a.json:
        return _print(rows, True)
    from .money import fmt

    for e in rows:
        c = node.store.contact(e["contact"])
        who = c.name if c else "?"
        arrow = f"{who} owes you" if (e["payer"] == "me") == (e["kind"] == "expense") else f"you owe {who}"
        if e["kind"] == "settle":
            arrow = f"you paid {who}" if e["payer"] == "me" else f"{who} paid you"
        print(f"{e['id']}  {e['status']:9} {e['title'][:30]:30} {fmt(e['cents'], e['currency']):>14}  {arrow}")


def cmd_money_pay(a: argparse.Namespace, node: Node) -> None:
    e = node.record_payment(a.to, a.amount, currency=a.currency, note=a.note or "")
    print(f"Recorded; {a.to} will be asked to confirm ({e['id']}).")


def cmd_money_answer(a: argparse.Namespace, node: Node) -> None:
    e = node.answer_entry(a.id, a.money_cmd == "accept", a.note or "")
    print(f"{e['title']}: {e['status']}.")


def cmd_money_cancel(a: argparse.Namespace, node: Node) -> None:
    node.cancel_entry(a.id)
    print("Cancelled.")


def cmd_share(a: argparse.Namespace, node: Node) -> None:
    names = node.share_status(_names(a.to) or None, plan_id=a.plan or "", text=a.text or "", eta_minutes=a.eta,
                              lat=a.lat, lon=a.lon, ttl_minutes=a.ttl)
    print(f"Shared with {', '.join(names)} for {a.ttl} min.")


def cmd_unshare(a: argparse.Namespace, node: Node) -> None:
    node.stop_sharing(_names(a.to))
    print("Stopped.")


def cmd_presence(a: argparse.Namespace, node: Node) -> None:
    rows = node.presence()
    if a.json:
        return _print(rows, True)
    if not rows:
        print("Nobody is sharing a status with you right now.")
    for r in rows:
        print(r["summary"])


def _when_arg(text: str | None, tz: str) -> str:
    return _local_dt(text, tz).strftime("%Y-%m-%dT%H:%M:%SZ") if text else ""


def _nth(trip: dict, section: str, n: str) -> str:
    items = trip[section]
    if n.isdigit() and 1 <= int(n) <= len(items):
        return items[int(n) - 1]["id"]
    if any(i["id"] == n for i in items):
        return n
    raise NodeError(f"no {section[:-1]} #{n}")


def _show_trip(node: Node, t: dict) -> str:
    tz = node.tz
    fmt = lambda iso: _local_dt(iso.replace("Z", "+00:00"), tz).astimezone(ZoneInfo(tz)).strftime("%a %b %d %H:%M") if iso else ""  # noqa: E731
    who = ", ".join([t["owner_name"] + " (organizer)", *t["members"].values()])
    lines = [f"🧳 {t['title']}{' — ' + t['destination'] if t['destination'] else ''}  [{t['status']}]  {t['start_date']} → {t['end_date']}  id={t['id']}",
             f"   with {who}"]
    if t["notes"]:
        lines.append(f"   {t['notes']}")
    if t["itinerary"]:
        lines.append("  Itinerary")
        for i in t["itinerary"]:
            conf = f"  #{i['confirmation']}" if i["confirmation"] else ""
            lines.append(f"   • {fmt(i['start']) or '(no time)':16} {i['kind']:9} {i['title']}{' @ ' + i['location'] if i['location'] else ''}{conf}")
    if t["travelers"]:
        lines.append("  Arrivals / departures")
        for tr in t["travelers"].values():
            ar, de = tr["arrive"], tr["depart"]
            pickup = " — needs pickup" if ar["needs_pickup"] else ""
            lines.append(f"   • {tr['name']}: in {fmt(ar['when']) or '?'} {ar['how']} {ar['where']}{pickup}; out {fmt(de['when']) or '?'} {de['how']}")
    for n, r in enumerate(t["rides"], 1):
        riders = ", ".join(r["passengers"].values()) or "empty"
        lines.append(f"  🚗 ride {n}: {r['driver_name']} from {r['from'] or '?'} {fmt(r['leaves_at'])} — {len(r['passengers'])}/{r['seats']} ({riders})")
    for n, r in enumerate(t["rooms"], 1):
        lines.append(f"  🛏️ room {n}: {r['name']} — {len(r['occupants'])}/{r['beds']} ({', '.join(r['occupants'].values()) or 'empty'})")
    for n, k in enumerate(t["tasks"], 1):
        lines.append(f"  {'☑' if k['done'] else '☐'} task {n}: {k['text']}{' — ' + k['assignee_name'] if k['assignee_name'] else ''}{' (due ' + k['due'] + ')' if k['due'] else ''}")
    for n, p in enumerate(t["polls"], 1):
        tally = {o["id"]: 0 for o in p["options"]}
        for v in p["votes"].values():
            tally[v] = tally.get(v, 0) + 1
        opts = "  ".join(f"{i + 1}) {o['text']} ×{tally[o['id']]}" for i, o in enumerate(p["options"]))
        lines.append(f"  🗳️ poll {n}{' (closed)' if p['closed'] else ''}: {p['question']}  {opts}")
    if t["links"].get("list_id"):
        lines.append(f"  📝 packing list: confer list show {t['links']['list_id']}")
    return "\n".join(lines)


def cmd_trip(a: argparse.Namespace, node: Node) -> None:
    c, tz = a.trip_cmd, node.tz
    if c == "new":
        t = node.create_trip(a.title, _names(a.with_), start_date=a.start, end_date=a.end, destination=a.dest or "",
                             notes=a.notes or "", packing_list=not a.no_packing)
        return _print(t if a.json else "Created:\n" + _show_trip(node, t), a.json)
    if c == "ls":
        trips = node.trips()
        if a.json:
            return _print(trips, True)
        if not trips:
            print("No trips.")
        for t in trips:
            print(f"{t['id']}  {t['start_date']} → {t['end_date']}  {t['status']:9} {t['title']}{' — ' + t['destination'] if t['destination'] else ''}")
        return None
    t = node.get_trip(a.id)
    if c == "show":
        return _print(t if a.json else _show_trip(node, t), a.json)
    if c == "budget":
        from .money import fmt

        b = node.trip_budget(a.id)
        for ccy, v in b.items():
            print(f"{ccy}: others owe you {fmt(v['you_paid_shares'], ccy)} for this trip; you owe {fmt(v['you_owe'], ccy)}")
        return print("No expenses tagged with this trip yet (use confer money split ... --plan <trip id>).") if not b else None
    op, args = {
        "add": ("itinerary.add", lambda: {"kind": a.kind, "title": a.title, "start": _when_arg(a.start, tz), "end": _when_arg(a.end, tz),
                                           "location": a.where or "", "confirmation": a.conf or "", "details": a.details or "", "url": a.url or ""}),
        "remove": ("itinerary.remove", lambda: {"id": _nth(t, "itinerary", a.n)}),
        "arrive": ("traveler.set", None), "depart": ("traveler.set", None),
        "ride": ("ride.offer", lambda: {"seats": a.seats, "from": a.origin or "", "leaves_at": _when_arg(a.leaves, tz)}),
        "join-ride": ("ride.join", lambda: {"id": _nth(t, "rides", a.n)}),
        "room": ("room.add", lambda: {"name": a.name, "beds": a.beds}),
        "join-room": ("room.join", lambda: {"id": _nth(t, "rooms", a.n)}),
        "task": ("task.add", lambda: {"text": a.text, "assignee": a.for_ or "", "due": a.due or ""}),
        "done": ("task.done", lambda: {"id": _nth(t, "tasks", a.n)}),
        "poll": ("poll.add", lambda: {"question": a.question, "options": a.options}),
        "vote": ("poll.vote", lambda: {"id": _nth(t, "polls", a.n), "option": f"o{int(a.option) - 1}"}),
        "update": ("trip.update", lambda: {k: v for k, v in (("title", a.title), ("destination", a.dest), ("start_date", a.start),
                                                               ("end_date", a.end), ("status", a.status), ("notes", a.notes)) if v}),
        "leave": ("leave", lambda: {}),
        "op": (a.op if c == "op" else "", lambda: json.loads(a.args or "{}")),
    }[c]
    if c in ("arrive", "depart"):
        mine = t["travelers"].get(node.identity.agent_id) or {"arrive": {}, "depart": {}, "notes": ""}
        mine = {k: mine.get(k) for k in ("arrive", "depart", "notes")}
        mine[c] = {"when": _when_arg(a.when, tz), "how": a.how or "", "where": a.where or "", "needs_pickup": bool(getattr(a, "pickup", False))}
        node.trip_op(a.id, "traveler.set", **mine)
    elif c == "leave":
        node.cancel_trip(a.id)
    else:
        node.trip_op(a.id, op, **args())
    fresh = node.get_trip(a.id) if node.store.get_trip(t["id"]) and not node.store.get_trip(t["id"]).get("leaving") else None
    print(_show_trip(node, fresh) if fresh and t.get("role") == "owner" else "Sent to the organizer; you'll see it once they confirm.")


def cmd_tick(a: argparse.Namespace, node: Node) -> None:
    node.tick()
    pending = node.store.outbox()
    print(f"Outbox: {len(pending)} waiting" + (f" (last error: {pending[0]['last_error']})" if pending and pending[0]["last_error"] else ""))


def cmd_serve(a: argparse.Namespace, node: Node) -> None:
    from .server import ConferServer

    srv = ConferServer(node, a.host, a.port, public_url=a.public_url or "", relay=a.relay, tick_seconds=a.tick)
    print(f"Confer node {node.name!r} listening on http://{a.host}:{srv.port} (public: {srv.public_url}){' + relay' if a.relay else ''}", flush=True)
    print(f"Calendar feed: {srv.public_url}/calendar/{node.config.get('feed_token')}.ics", flush=True)
    print(f"Phone inbox:   {srv.ui_url}  (keep this URL private)", flush=True)
    srv.serve_forever()


def cmd_relay(a: argparse.Namespace) -> None:
    from .server import ConferServer

    home = home_dir(a)
    node = Node(home) if (home / "config.json").exists() else Node.init(home, "relay")
    srv = ConferServer(node, a.host, a.port, public_url=a.public_url or "", relay=True)
    print(f"Confer relay listening on http://{a.host}:{srv.port}", flush=True)
    srv.serve_forever()


def cmd_mcp(a: argparse.Namespace, node: Node) -> None:
    from .mcp_server import run

    run(node, http=a.http, host=a.host, port=a.port)


def cmd_api(a: argparse.Namespace, node: Node) -> None:
    import secrets as _secrets

    if a.api_cmd == "disable":
        node.config["api_token"] = ""
        node.save_config()
        return print("REST API disabled.")
    if a.api_cmd in ("enable", "rotate") or not node.config.get("api_token"):
        if a.api_cmd == "enable" and node.config.get("api_token"):
            return print("Already enabled. `confer api token` shows it; `confer api rotate` replaces it.")
        node.config["api_token"] = _secrets.token_urlsafe(32)
        node.save_config()
    base = node.config.get("endpoint") or "http://127.0.0.1:3067"
    print(f"REST API: {base}/api/v1   (OpenAPI: {base}/api/v1/openapi.json)")
    print(f"Token:    {node.config['api_token']}")
    print("Keep the token secret. Tools that act for you also need the header 'Confer-Human-Approved: true'.")


def cmd_tools(a: argparse.Namespace, node: Node | None = None) -> None:
    from . import tools as T

    if a.tools_cmd == "list":
        for t in T.TOOLS.values():
            print(f"{'⚠ ' if t.human_ok else '  '}{t.name:26} {t.description[:90]}")
        return
    print(json.dumps(T.export(a.format, a.base_url or ""), indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="confer", description="Open agent-to-agent coordination for personal AI assistants.")
    p.add_argument("--home", help="node directory (default $CONFER_HOME or ~/.confer)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create this node's identity")
    s.add_argument("name")
    s.add_argument("--endpoint", help="public base URL where peers reach this node")
    s.add_argument("--relay", help="relay base URL, if this node isn't publicly reachable")
    s.add_argument("--tz", default="UTC")
    s.set_defaults(fn=cmd_init, needs_node=False)

    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    s = sub.add_parser("config", help="show or set settings")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    grants_help = f"comma list from {', '.join(GRANTS)} (default plans,files,notes,intros)"
    s = sub.add_parser("invite", help="create a one-time invite for a trusted person")
    s.add_argument("name")
    s.add_argument("--grant", help=grants_help)
    s.add_argument("--ttl-hours", type=float, default=72)
    s.set_defaults(fn=cmd_invite)
    s = sub.add_parser("accept", help="accept someone's invite")
    s.add_argument("token")
    s.add_argument("--name", help="what to call them")
    s.add_argument("--grant", help=grants_help)
    s.set_defaults(fn=cmd_accept)
    sub.add_parser("contacts").set_defaults(fn=cmd_contacts)
    s = sub.add_parser("grant", help="change a contact's grants, e.g. +autoconfirm,-files")
    s.add_argument("name")
    s.add_argument("grants", nargs="+", help="absolute (plans,files) or relative (+location,-money)")
    s.set_defaults(fn=cmd_grant)
    s = sub.add_parser("remove", help="remove a contact")
    s.add_argument("name")
    s.set_defaults(fn=cmd_remove)

    plan = sub.add_parser("plan", help="make plans with contacts").add_subparsers(dest="plan_cmd", required=True)
    s = plan.add_parser("new")
    s.add_argument("title")
    s.add_argument("--with", dest="with_", required=True, help="comma-separated contact names")
    s.add_argument("--duration", type=int, default=60, help="minutes")
    s.add_argument("--from", dest="start", help="window start (local date/time), default: in 1h")
    s.add_argument("--to", dest="end", help="window end (local date/time), default: +7 days")
    s.add_argument("--between", help="local time-of-day range, e.g. 18:00-21:00")
    s.add_argument("--slot", action="append", help="explicit option, e.g. 2026-10-02T19:00/90 (repeatable)")
    s.add_argument("--options", type=int, default=4, help="how many options to offer")
    s.add_argument("--rrule", help="recurrence, e.g. FREQ=WEEKLY;BYDAY=TH")
    s.add_argument("--location")
    s.add_argument("--notes")
    s.add_argument("--quorum", default="all", help="'all' or a number of participants")
    s.add_argument("--deadline-hours", type=float)
    s.set_defaults(fn=cmd_plan_new)
    plan.add_parser("list").set_defaults(fn=cmd_plan_list)
    s = plan.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_plan_show)
    s = plan.add_parser("respond")
    s.add_argument("id")
    s.add_argument("decision", choices=sorted(P.DECISIONS))
    s.add_argument("--slots", help="options that work, e.g. 1,3 (default: the ones your calendar shows free)")
    s.add_argument("--suggest", action="append", help="counter time, e.g. 2026-10-03T18:30/90 (repeatable)")
    s.add_argument("--prefer", help="options you'd rather have, e.g. 2 (must also work)")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_plan_respond)
    s = plan.add_parser("revise")
    s.add_argument("id")
    s.add_argument("--from", dest="start")
    s.add_argument("--to", dest="end")
    s.add_argument("--between")
    s.add_argument("--duration", type=int)
    s.add_argument("--slot", action="append")
    s.add_argument("--options", type=int, default=4)
    s.set_defaults(fn=cmd_plan_revise)
    s = plan.add_parser("cancel")
    s.add_argument("id")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_plan_cancel)

    s = sub.add_parser("inbox", help="things for you to know or decide")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_inbox)
    s = sub.add_parser("done", help="dismiss inbox items")
    s.add_argument("ids", nargs="+", type=int)
    s.set_defaults(fn=cmd_done)
    s = sub.add_parser("send-file")
    s.add_argument("to")
    s.add_argument("path")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_send_file)
    s = sub.add_parser("note", help="send a short message to a contact's agent")
    s.add_argument("to")
    s.add_argument("text")
    s.add_argument("--ask", action="store_true", help="this is a question; flag it for an answer")
    s.add_argument("--reply-to", help="message id you're answering")
    s.set_defaults(fn=cmd_note)
    s = sub.add_parser("introduce", help="vouch for two of your contacts to each other")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_introduce)
    intro = sub.add_parser("intro", help="introductions offered to you").add_subparsers(dest="intro_cmd", required=True)
    intro.add_parser("list").set_defaults(fn=cmd_intro_list)
    s = intro.add_parser("accept")
    s.add_argument("id")
    s.add_argument("--name", help="what to call them")
    s.add_argument("--grant", help=grants_help)
    s.set_defaults(fn=cmd_intro_accept)
    s = intro.add_parser("decline")
    s.add_argument("id")
    s.set_defaults(fn=cmd_intro_decline)
    s = sub.add_parser("rotate-key", help="move to a fresh identity key (contacts follow automatically)")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_rotate_key)
    sub.add_parser("remind", help="nudge people who haven't answered your plans").set_defaults(fn=cmd_remind)
    lst = sub.add_parser("list", help="shared lists (groceries, packing, who's bringing what)").add_subparsers(dest="list_cmd", required=True)
    s = lst.add_parser("new")
    s.add_argument("title")
    s.add_argument("--with", dest="with_", required=True, help="comma-separated contact names")
    s.add_argument("--item", action="append", help="initial item (repeatable)")
    s.set_defaults(fn=cmd_list_new)
    lst.add_parser("ls").set_defaults(fn=cmd_list_ls)
    s = lst.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_list_show)
    s = lst.add_parser("add")
    s.add_argument("id")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_list_edit)
    for op, help_ in (("check", "mark done"), ("uncheck", "mark not done"), ("claim", "\"I'll bring it\""), ("unclaim", "give it back"), ("remove", "delete an item")):
        s = lst.add_parser(op, help=help_)
        s.add_argument("id")
        s.add_argument("item", help="item number (as shown), id or exact text")
        s.set_defaults(fn=cmd_list_edit)
    for op in ("leave", "delete"):
        s = lst.add_parser(op)
        s.add_argument("id")
        s.set_defaults(fn=cmd_list_edit)

    money = sub.add_parser("money", help="split expenses and settle up").add_subparsers(dest="money_cmd", required=True)
    s = money.add_parser("split", help="you paid; ask others for their share")
    s.add_argument("title")
    s.add_argument("amount")
    s.add_argument("--with", dest="with_", required=True)
    s.add_argument("--currency")
    s.add_argument("--shares", help="explicit shares, e.g. Sam=40,Priya=20")
    s.add_argument("--exclude-me", action="store_true", help="split only among the others")
    s.add_argument("--note")
    s.add_argument("--plan", help="link to a plan id")
    s.set_defaults(fn=cmd_money_split)
    money.add_parser("balances").set_defaults(fn=cmd_money_balances)
    s = money.add_parser("ledger")
    s.add_argument("--with", dest="with_")
    s.set_defaults(fn=cmd_money_ledger)
    s = money.add_parser("pay", help="record that you paid someone back")
    s.add_argument("to")
    s.add_argument("amount")
    s.add_argument("--currency")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_money_pay)
    for op in ("accept", "dispute"):
        s = money.add_parser(op)
        s.add_argument("id")
        s.add_argument("--note")
        s.set_defaults(fn=cmd_money_answer)
    s = money.add_parser("cancel")
    s.add_argument("id")
    s.set_defaults(fn=cmd_money_cancel)

    trip = sub.add_parser("trip", help="trip planning: itinerary, logistics, rides, rooms, tasks, polls").add_subparsers(dest="trip_cmd", required=True)
    s = trip.add_parser("new")
    s.add_argument("title")
    s.add_argument("--with", dest="with_", required=True)
    s.add_argument("--start", required=True, help="YYYY-MM-DD")
    s.add_argument("--end", required=True, help="YYYY-MM-DD")
    s.add_argument("--dest", help="destination")
    s.add_argument("--notes")
    s.add_argument("--no-packing", action="store_true", help="don't create a shared packing list")
    trip.add_parser("ls")
    for name in ("show", "budget", "leave"):
        trip.add_parser(name).add_argument("id")
    s = trip.add_parser("add", help="add an itinerary item")
    s.add_argument("id")
    s.add_argument("title")
    s.add_argument("--kind", default="other", choices=["flight", "lodging", "activity", "transport", "meal", "other"])
    s.add_argument("--start", help="local date/time, e.g. 2031-03-20T15:00")
    s.add_argument("--end")
    s.add_argument("--where")
    s.add_argument("--conf", help="confirmation code")
    s.add_argument("--details")
    s.add_argument("--url", help="https link (booking page, map)")
    s = trip.add_parser("remove", help="remove itinerary item #n")
    s.add_argument("id")
    s.add_argument("n")
    for leg in ("arrive", "depart"):
        s = trip.add_parser(leg, help=f"your {leg} details")
        s.add_argument("id")
        s.add_argument("--when")
        s.add_argument("--how", help="e.g. UA 1234, driving, train")
        s.add_argument("--where", help="airport/station/address")
        if leg == "arrive":
            s.add_argument("--pickup", action="store_true", help="you need a ride from there")
    s = trip.add_parser("ride", help="offer seats in your car")
    s.add_argument("id")
    s.add_argument("--seats", type=int, required=True)
    s.add_argument("--from", dest="origin")
    s.add_argument("--leaves")
    s = trip.add_parser("join-ride")
    s.add_argument("id")
    s.add_argument("n")
    s = trip.add_parser("room", help="add a room (organizer)")
    s.add_argument("id")
    s.add_argument("name")
    s.add_argument("--beds", type=int, default=2)
    s = trip.add_parser("join-room")
    s.add_argument("id")
    s.add_argument("n")
    s = trip.add_parser("task")
    s.add_argument("id")
    s.add_argument("text")
    s.add_argument("--for", dest="for_", help="contact name or 'me'")
    s.add_argument("--due", help="YYYY-MM-DD")
    s = trip.add_parser("done", help="mark task #n done")
    s.add_argument("id")
    s.add_argument("n")
    s = trip.add_parser("poll")
    s.add_argument("id")
    s.add_argument("question")
    s.add_argument("options", nargs="+")
    s = trip.add_parser("vote")
    s.add_argument("id")
    s.add_argument("n", help="poll number")
    s.add_argument("option", help="option number")
    s = trip.add_parser("update", help="organizer: change details/status")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--dest")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--notes")
    s.add_argument("--status", choices=["planning", "booked", "cancelled"])
    s = trip.add_parser("op", help="any trip operation with JSON args (advanced)")
    s.add_argument("id")
    s.add_argument("op")
    s.add_argument("args", nargs="?", help='JSON, e.g. \'{"id": "...", "option": "o1"}\'')
    for p_ in trip.choices.values():
        p_.set_defaults(fn=cmd_trip)

    s = sub.add_parser("share", help="share a status, ETA or location (expires)")
    s.add_argument("--to", help="comma-separated contacts")
    s.add_argument("--plan", help="everyone in this plan")
    s.add_argument("--text")
    s.add_argument("--eta", type=int, help="minutes")
    s.add_argument("--lat", type=float)
    s.add_argument("--lon", type=float)
    s.add_argument("--ttl", type=int, default=120, help="minutes until it disappears")
    s.set_defaults(fn=cmd_share)
    s = sub.add_parser("unshare", help="stop sharing status/location")
    s.add_argument("--to", required=True)
    s.set_defaults(fn=cmd_unshare)
    sub.add_parser("presence", help="who's sharing a status/ETA/location with you").set_defaults(fn=cmd_presence)

    sub.add_parser("tick", help="deliver queued messages, poll relay, check deadlines").set_defaults(fn=cmd_tick)

    s = sub.add_parser("serve", help="run this node's server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=3067)
    s.add_argument("--public-url", help="override advertised URL (default: config endpoint)")
    s.add_argument("--relay", action="store_true", help="also act as a relay for other nodes")
    s.add_argument("--tick", type=float, default=30.0)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("relay", help="run a standalone relay (untrusted store-and-forward)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=3068)
    s.add_argument("--public-url")
    s.set_defaults(fn=cmd_relay, needs_node=False)
    s = sub.add_parser("mcp", help="run as an MCP server (stdio by default) for Claude, Codex, Gemini, Cursor, Hermes, ...")
    s.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio (localhost only)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=3069)
    s.set_defaults(fn=cmd_mcp)
    api_p = sub.add_parser("api", help="REST API for harnesses, automations and the iOS app").add_subparsers(dest="api_cmd", required=True)
    for name, help_ in (("enable", "create a token and turn the API on"), ("token", "show the URL and token"),
                        ("rotate", "replace the token"), ("disable", "turn the API off")):
        api_p.add_parser(name, help=help_).set_defaults(fn=cmd_api)
    tools_p = sub.add_parser("tools", help="list or export agent tool definitions").add_subparsers(dest="tools_cmd", required=True)
    tools_p.add_parser("list").set_defaults(fn=cmd_tools, needs_node=False)
    s = tools_p.add_parser("export", help="JSON tool definitions for function-calling frameworks")
    s.add_argument("--format", default="openai", choices=["openai", "anthropic", "gemini", "mcp", "openapi"])
    s.add_argument("--base-url", help="server URL to put in the OpenAPI document")
    s.set_defaults(fn=cmd_tools, needs_node=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "needs_node", True):
            node = Node(home_dir(args))
            try:
                args.fn(args, node)
            finally:
                node.close()
        else:
            args.fn(args)
    except (NodeError, P.PlanError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
