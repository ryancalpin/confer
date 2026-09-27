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

CONFIG_KEYS = {"name", "endpoint", "relay", "tz", "hours_start", "hours_end", "buffer_minutes", "calendar", "notify_webhook", "notify_cmd"}


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
    if a.key in ("endpoint", "relay", "notify_webhook") and a.value:
        value = check_url(a.value)
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
    c = node.set_grants(a.name, a.grants)
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

    run(node)


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
    s = sub.add_parser("grant", help="change a contact's grants, e.g. +autoconfirm or -files")
    s.add_argument("name")
    s.add_argument("grants")
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
    sub.add_parser("mcp", help="run as an MCP server over stdio (for Claude, Hermes, ...)").set_defaults(fn=cmd_mcp)
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
