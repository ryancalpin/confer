"""MCP server so any LLM agent (Claude, Hermes, Codex, ...) can drive a node.

    confer mcp            # stdio; add to your agent's MCP config

The agent is the "brain" (reads your texts, decides, books the restaurant);
Confer is the trusted pipe to other people's agents. Every tool returns plain
JSON-able data. Slot/option numbers in tools are 1-based, as shown to humans.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import plans as P
from .cli import _between, _local_dt, _slot
from .identity import fingerprint
from .node import Node


def _server_class() -> Any:
    """MCP SDK 2.x renamed FastMCP to MCPServer; support both."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP

            return FastMCP
        except ImportError as exc:
            raise SystemExit("the MCP server needs: pip install 'confer[mcp]'") from exc


INSTRUCTIONS = (
    "Confer connects this person's agent to their trusted contacts' agents. Use it to plan things "
    "with other people (their agents check calendars privately and answer), keep shared lists, split "
    "expenses, share ETAs, send notes/files, and check the inbox for decisions. Never accept, counter or cancel a plan without the human's OK "
    "unless they told you to; never accept money requests or share location without explicit OK; "
    "relay inbox items to them plainly."
)


def build(node: Node) -> Any:
    mcp = _server_class()("confer", instructions=INSTRUCTIONS)

    @mcp.tool()
    def confer_whoami() -> dict:
        """This node's name, agent id, fingerprint, endpoint and timezone."""
        return node.whoami()

    @mcp.tool()
    def confer_contacts() -> list[dict]:
        """Trusted contacts and what each is allowed to do with this node."""
        return [{"name": c.name, "status": c.status, "grants": c.grants, "fingerprint": fingerprint(c.agent_id)} for c in node.store.contacts()]

    @mcp.tool()
    def confer_invite(name: str, grants: str = "plans,files,notes", ttl_hours: float = 72) -> dict:
        """Create a one-time invite token for a trusted person. The human must send it to them privately.
        grants: comma list of plans, autoconfirm, files, notes."""
        return {"token": node.create_invite(name, grants, ttl_hours), "fingerprint": node.identity.fingerprint}

    @mcp.tool()
    def confer_accept_invite(token: str, name: str = "", grants: str = "plans,files,notes") -> dict:
        """Accept an invite token someone sent, pairing your agents."""
        c = node.accept_invite(token, name=name or None, grants=grants)
        return {"name": c.name, "status": c.status, "fingerprint": fingerprint(c.agent_id)}

    @mcp.tool()
    def confer_set_grants(name: str, grants: str) -> dict:
        """Change what a contact may do: absolute ('plans,files') or relative ('+autoconfirm,-files')."""
        c = node.set_grants(name, grants)
        return {"name": c.name, "grants": c.grants}

    @mcp.tool()
    def confer_propose_plan(
        title: str,
        with_contacts: list[str],
        duration_minutes: int = 60,
        window_from: str = "",
        window_to: str = "",
        between: str = "",
        explicit_times: list[str] | None = None,
        options: int = 4,
        location: str = "",
        notes: str = "",
        recurrence: str = "",
        quorum: str = "all",
        deadline_hours: float = 0,
    ) -> dict:
        """Propose a plan to one or more contacts. Their agents check their calendars privately
        and reply; the plan confirms automatically once a time works for everyone (or quorum).
        Times are local ISO (e.g. 2026-10-02T19:00). between='18:00-21:00' limits time of day.
        explicit_times items look like '2026-10-02T19:00/90' (minutes). recurrence is an RRULE."""
        tz = node.tz
        start = _local_dt(window_from, tz) if window_from else None
        end = _local_dt(window_to, tz) if window_to else None
        if end and len(window_to) == 10:
            end += timedelta(days=1)
        slots = [_slot(t, tz, duration_minutes) for t in explicit_times] if explicit_times else None
        plan = node.create_plan(
            title, with_contacts, duration_minutes=duration_minutes, window_start=start, window_end=end,
            between=_between(between or None), slots=slots, candidates=options, rrule=recurrence or None,
            location=location, notes=notes, quorum="all" if quorum == "all" else int(quorum),
            deadline_hours=deadline_hours or None,
        )
        return {"plan_id": plan["id"], "summary": P.describe(plan, tz)}

    @mcp.tool()
    def confer_plans() -> list[dict]:
        """All plans this node organizes or was invited to, with human-readable summaries."""
        return [{"plan_id": p["id"], "status": p["status"], "role": p.get("role"), "summary": P.describe(p, node.tz)} for p in node.plans()]

    @mcp.tool()
    def confer_respond(plan_id: str, decision: str, options: list[int] | None = None, note: str = "",
                       suggest_times: list[str] | None = None, preferred: list[int] | None = None) -> dict:
        """Answer a plan you were invited to. decision: accept | decline | counter.
        options: 1-based option numbers that work (default: the ones your calendar shows free).
        preferred: 1-based options the human would rather have (subset of options); they break ties.
        suggest_times (for counter): e.g. ['2026-10-03T18:30/90']. Only do this with the human's OK."""
        counter = [_slot(t, node.tz) for t in suggest_times] if suggest_times else None
        plan = node.respond(plan_id, decision, slots=[o - 1 for o in options] if options else None, note=note, counter=counter,
                            prefer=[o - 1 for o in preferred] if preferred else None)
        return {"plan_id": plan["id"], "sent": decision}

    @mcp.tool()
    def confer_revise_plan(plan_id: str, window_from: str = "", window_to: str = "", between: str = "", explicit_times: list[str] | None = None, options: int = 4) -> dict:
        """Organizer: offer new times for a plan (e.g. after 'no time works')."""
        tz = node.tz
        slots = [_slot(t, tz) for t in explicit_times] if explicit_times else None
        plan = node.revise(plan_id, slots=slots, window_start=_local_dt(window_from, tz) if window_from else None,
                           window_end=_local_dt(window_to, tz) if window_to else None, between=_between(between or None), candidates=options)
        return {"plan_id": plan["id"], "summary": P.describe(plan, tz)}

    @mcp.tool()
    def confer_cancel(plan_id: str, reason: str = "") -> dict:
        """Cancel a plan you organize, or drop out of one you were invited to."""
        plan = node.cancel(plan_id, reason)
        return {"plan_id": plan["id"], "status": plan["status"]}

    @mcp.tool()
    def confer_inbox(include_done: bool = False) -> list[dict]:
        """Things the human should know or decide (actionable=true needs a decision)."""
        return [{k: i[k] for k in ("id", "kind", "summary", "actionable", "status", "ref")} for i in node.inbox(include_done)]

    @mcp.tool()
    def confer_dismiss(item_id: int) -> dict:
        """Mark an inbox item handled."""
        return {"ok": node.dismiss(item_id)}

    @mcp.tool()
    def confer_send_note(to: str, text: str, plan_id: str = "", reply_to: str = "", is_question: bool = False) -> dict:
        """Send a short message to a contact's agent (e.g. 'Does Sam prefer Thai or Italian?').
        is_question flags it for an answer; reply_to answers a message id from the inbox.
        Returns msg_id — poll confer_replies(msg_id) for answers."""
        return {"sent": True, "msg_id": node.send_note(to, text, plan_id, reply_to=reply_to, expects_reply=is_question)}

    @mcp.tool()
    def confer_replies(msg_id: str) -> list[dict]:
        """Replies received to a note you sent."""
        return [{"from": i["summary"], "text": i["payload"].get("text", "")} for i in node.replies(msg_id)]

    @mcp.tool()
    def confer_introduce(contact_a: str, contact_b: str, note: str = "") -> dict:
        """Vouch for two of the human's contacts to each other. Each side's owner must approve;
        then their agents connect directly. Only do this when the human asks."""
        return {"intro_id": node.introduce(contact_a, contact_b, note)}

    @mcp.tool()
    def confer_intros() -> list[dict]:
        """Introductions offered to this node (status offered = waiting for the human)."""
        return [{"intro_id": i["intro_id"], "peer": i["peer_name"], "fingerprint": fingerprint(i["peer_id"]), "status": i["status"],
                 "note": i["note"]} for i in node.intros()]

    @mcp.tool()
    def confer_intro_decide(intro_id: str, accept: bool, name: str = "", grants: str = "plans,files,notes") -> dict:
        """Accept or decline an introduction. Only with the human's explicit OK."""
        if not accept:
            node.decline_intro(intro_id)
            return {"declined": True}
        c = node.accept_intro(intro_id, name=name or None, grants=grants)
        return {"name": c.name, "status": c.status}

    @mcp.tool()
    def confer_send_file(to: str, path: str, note: str = "") -> dict:
        """Send a local file (<=10 MB), end-to-end encrypted, to a contact."""
        from pathlib import Path

        node.send_file(to, Path(path), note)
        return {"sent": True}

    # ---------------------------------------------------------------- lists
    @mcp.tool()
    def confer_create_list(title: str, with_contacts: list[str], items: list[str] | None = None) -> dict:
        """Create a shared list (groceries, packing, who's bringing what) with contacts."""
        lst = node.create_list(title, with_contacts, items=items or [])
        return {"list_id": lst["id"], "items": len(lst["items"])}

    @mcp.tool()
    def confer_lists() -> list[dict]:
        """All shared lists with their items (numbered from 1), who claimed what, and done state."""
        return [{"list_id": x["id"], "title": x["title"], "owner": x.get("owner_name"), "mine": x.get("role") == "owner",
                 "members": list(x["members"].values()),
                 "items": [{"n": n, "text": i["text"], "done": i["done"], "claimed_by": i["claimed_name"]} for n, i in enumerate(x["items"], 1)]}
                for x in node.lists()]

    @mcp.tool()
    def confer_list_edit(list_id: str, action: str, item: str = "", text: str = "") -> dict:
        """Edit a shared list. action: add (uses text) | check | uncheck | claim | unclaim | remove (use item =
        item number or exact text) | leave | delete. Members' edits go via the owner's agent."""
        if action in ("leave", "delete"):
            node.delete_list(list_id)
            return {"ok": True}
        lst = node.list_op(list_id, action, item=item or None, text=text)
        return {"ok": True, "list_id": lst["id"]}

    # ---------------------------------------------------------------- money
    @mcp.tool()
    def confer_split_expense(title: str, amount: str, with_contacts: list[str], currency: str = "", include_me: bool = True,
                             shares: dict[str, str] | None = None, note: str = "", plan_id: str = "") -> dict:
        """The human paid `amount` for `title`; ask each contact for their share (equal split, or explicit
        `shares` by name). Each person's agent asks them to accept. Confer records money; it doesn't move it."""
        entries = node.add_expense(title, amount, with_contacts, currency=currency or None, shares=shares,
                                   include_me=include_me, note=note, plan_id=plan_id)
        from .money import fmt

        return {"requests": [{"entry_id": e["id"], "amount": fmt(e["cents"], e["currency"])} for e in entries]}

    @mcp.tool()
    def confer_balances() -> list[dict]:
        """Who owes whom, per contact and currency (positive balance_cents = they owe the human)."""
        return node.balances()

    @mcp.tool()
    def confer_record_payment(to: str, amount: str, currency: str = "", note: str = "") -> dict:
        """Record that the human paid a contact back; the contact confirms it."""
        return {"entry_id": node.record_payment(to, amount, currency=currency or None, note=note)["id"]}

    @mcp.tool()
    def confer_answer_money(entry_id: str, accept: bool, note: str = "") -> dict:
        """Accept or dispute an expense share / payment someone recorded. Only with the human's explicit OK."""
        return {"status": node.answer_entry(entry_id, accept, note)["status"]}

    # ------------------------------------------------------------- presence
    @mcp.tool()
    def confer_share_status(to_contacts: list[str] | None = None, plan_id: str = "", text: str = "", eta_minutes: int | None = None,
                            lat: float | None = None, lon: float | None = None, ttl_minutes: int = 120) -> dict:
        """Share a status ('running late'), ETA and/or location with contacts or everyone in a plan. It expires
        (default 2h). Location is sensitive: only share coordinates when the human asked to."""
        return {"sent_to": node.share_status(to_contacts, plan_id=plan_id, text=text, eta_minutes=eta_minutes,
                                             lat=lat, lon=lon, ttl_minutes=ttl_minutes)}

    @mcp.tool()
    def confer_presence() -> list[dict]:
        """Current statuses / ETAs / locations contacts are sharing with the human."""
        return [{"contact": p["contact"], "summary": p["summary"], "eta_minutes": p.get("eta_minutes")} for p in node.presence()]

    @mcp.tool()
    def confer_sync() -> dict:
        """Deliver queued messages, fetch relayed ones, and process plan deadlines."""
        node.tick()
        return {"outbox_waiting": len(node.store.outbox())}

    return mcp


def run(node: Node) -> None:
    build(node).run()
