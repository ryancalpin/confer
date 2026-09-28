"""Trips: planning, organization and logistics for a group getaway.

A trip is owned by one node (the organizer) — same hub-and-spoke model as
plans and lists. Members send ``trip.op`` edits; the owner validates, applies
and broadcasts the new snapshot (``trip.share``). Sections:

  itinerary  flights, lodging, activities, transport, meals — with times,
             places, confirmation codes and links
  travelers  each member's own arrival/departure (how, when, needs pickup)
  rides      carpools: a driver offers seats, others join
  rooms      who sleeps where (capacity-checked)
  tasks      "book the cabin" — assignee, due date, done
  polls      "Which Airbnb?" — one vote per member, closable
  links      a shared packing list; expenses tagged with the trip id

Receiving a trip requires the ``trips`` grant.
"""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .availability import UTC, Interval, fmt_dt, parse_dt
from .store import Contact

if TYPE_CHECKING:
    from .node import Node

MAX_MEMBERS = 50
LIMITS = {"itinerary": 200, "tasks": 200, "polls": 20, "rides": 20, "rooms": 30}
MAX_TEXT = 300
MAX_OPTIONS = 10
KINDS = ("flight", "lodging", "activity", "transport", "meal", "other")
STATUSES = ("planning", "booked", "cancelled")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[A-Za-z0-9]{4,24}$")

OPS = (
    "itinerary.add", "itinerary.update", "itinerary.remove",
    "traveler.set",
    "ride.offer", "ride.join", "ride.leave", "ride.cancel",
    "room.add", "room.join", "room.leave", "room.remove",
    "task.add", "task.done", "task.undone", "task.assign", "task.remove",
    "poll.add", "poll.vote", "poll.close",
    "leave",
)
OWNER_OPS = ("trip.update",)  # details, dates, status: owner only (applied locally)


class TripError(ValueError):
    pass


def _nid() -> str:
    return secrets.token_hex(6)


def _t(value: Any, n: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:n]


def _url(value: Any) -> str:
    v = str(value or "").strip()
    return v[:500] if v.startswith("https://") and "\n" not in v and "\r" not in v else ""


def _when(value: Any) -> str:
    if not value:
        return ""
    try:
        return fmt_dt(parse_dt(str(value)))
    except (ValueError, TypeError) as exc:
        raise TripError(f"bad time {value!r} (use ISO 8601 with a timezone)") from exc


def _day(value: Any) -> str:
    v = str(value or "")
    if not _DATE.match(v):
        raise TripError(f"bad date {value!r} (use YYYY-MM-DD)")
    date.fromisoformat(v)
    return v


def _pick_id(given: Any) -> str:
    return given if isinstance(given, str) and _ID.match(given) else _nid()


def _find(items: list[dict], item_id: Any) -> dict:
    for i in items:
        if i["id"] == item_id:
            return i
    raise TripError("no such item")


def _cap(trip: dict, section: str) -> None:
    if len(trip[section]) >= LIMITS[section]:
        raise TripError(f"at most {LIMITS[section]} {section}")


def new_trip(*, title: str, owner: str, owner_name: str, members: dict[str, str], start_date: str, end_date: str,
             destination: str = "", notes: str = "", tz: str = "UTC") -> dict:
    s, e = _day(start_date), _day(end_date)
    if e < s:
        raise TripError("the trip ends before it starts")
    if len(members) > MAX_MEMBERS:
        raise TripError(f"at most {MAX_MEMBERS} members")
    return {
        "id": secrets.token_hex(8), "rev": 1, "title": _t(title, 120) or "Trip", "destination": _t(destination, 200),
        "start_date": s, "end_date": e, "tz": tz, "notes": _t(notes, 2000), "status": "planning",
        "owner": owner, "owner_name": owner_name, "members": dict(members),
        "itinerary": [], "travelers": {}, "rides": [], "rooms": [], "tasks": [], "polls": [], "links": {},
    }


def apply_op(trip: dict, actor: str, actor_name: str, op: str, a: dict) -> bool:
    """Apply one edit to the owner's copy; raises TripError when not allowed."""
    is_owner = actor == trip["owner"]
    if actor not in trip["members"] and not is_owner:
        raise TripError("not a member of this trip")
    if op not in OPS:
        raise TripError(f"unknown trip operation {op!r}")
    name = actor_name[:60]

    if op == "leave":
        if is_owner:
            raise TripError("the organizer can't leave; cancel the trip instead")
        trip["members"].pop(actor, None)
        trip["travelers"].pop(actor, None)
        for r in trip["rides"]:
            r["passengers"].pop(actor, None)
        trip["rides"] = [r for r in trip["rides"] if r["driver_id"] != actor]
        for r in trip["rooms"]:
            r["occupants"].pop(actor, None)
        return True

    if op.startswith("itinerary."):
        items = trip["itinerary"]
        if op == "itinerary.remove":
            item = _find(items, a.get("id"))
            if not (is_owner or item["added_by"] == actor):
                raise TripError("only the organizer or whoever added it can remove it")
            items.remove(item)
            return True
        fields = {
            "kind": a.get("kind") if a.get("kind") in KINDS else "other",
            "title": _t(a.get("title"), 200), "start": _when(a.get("start")), "end": _when(a.get("end")),
            "location": _t(a.get("location"), 300), "confirmation": _t(a.get("confirmation"), 60),
            "details": _t(a.get("details"), 1000), "url": _url(a.get("url")),
        }
        if fields["start"] and fields["end"] and fields["end"] < fields["start"]:
            raise TripError("an itinerary item can't end before it starts")
        if op == "itinerary.add":
            if not fields["title"]:
                raise TripError("give the item a title")
            _cap(trip, "itinerary")
            iid = _pick_id(a.get("id"))
            if any(i["id"] == iid for i in items):
                return False  # retried add
            items.append({"id": iid, **fields, "added_by": actor, "added_by_name": name})
        else:
            item = _find(items, a.get("id"))
            item.update({k: v for k, v in fields.items() if k in a})
        items.sort(key=lambda i: (i["start"] or "9999", i["title"]))
        return True

    if op == "traveler.set":  # members edit only their own logistics
        def leg(x: Any) -> dict:
            x = x if isinstance(x, dict) else {}
            return {"when": _when(x.get("when")), "how": _t(x.get("how"), 120), "where": _t(x.get("where"), 200),
                    "needs_pickup": bool(x.get("needs_pickup"))}

        trip["travelers"][actor] = {"name": name, "arrive": leg(a.get("arrive")), "depart": leg(a.get("depart")),
                                    "notes": _t(a.get("notes"), 500)}
        return True

    if op.startswith("ride."):
        rides = trip["rides"]
        if op == "ride.offer":
            _cap(trip, "rides")
            seats = a.get("seats")
            if not isinstance(seats, int) or isinstance(seats, bool) or not 1 <= seats <= 50:
                raise TripError("seats must be 1..50")
            rid = _pick_id(a.get("id"))
            if any(r["id"] == rid for r in rides):
                return False
            rides.append({"id": rid, "driver_id": actor, "driver_name": name, "seats": seats, "from": _t(a.get("from"), 200),
                          "leaves_at": _when(a.get("leaves_at")), "passengers": {}})
            return True
        ride = _find(rides, a.get("id"))
        if op == "ride.join":
            if actor == ride["driver_id"] or actor in ride["passengers"]:
                return False
            if len(ride["passengers"]) >= ride["seats"]:
                raise TripError("that car is full")
            for r in rides:  # one car per person
                r["passengers"].pop(actor, None)
            ride["passengers"][actor] = name
        elif op == "ride.leave":
            return ride["passengers"].pop(actor, None) is not None
        elif op == "ride.cancel":
            if not (is_owner or ride["driver_id"] == actor):
                raise TripError("only the driver can cancel a ride")
            rides.remove(ride)
        return True

    if op.startswith("room."):
        rooms = trip["rooms"]
        if op == "room.add":
            _cap(trip, "rooms")
            beds = a.get("beds")
            if not isinstance(beds, int) or isinstance(beds, bool) or not 1 <= beds <= 50:
                raise TripError("beds must be 1..50")
            rid = _pick_id(a.get("id"))
            if any(r["id"] == rid for r in rooms):
                return False
            rooms.append({"id": rid, "name": _t(a.get("name"), 80) or "Room", "beds": beds, "occupants": {}})
            return True
        room = _find(rooms, a.get("id"))
        if op == "room.join":
            if actor in room["occupants"]:
                return False
            if len(room["occupants"]) >= room["beds"]:
                raise TripError("that room is full")
            for r in rooms:
                r["occupants"].pop(actor, None)
            room["occupants"][actor] = name
        elif op == "room.leave":
            return room["occupants"].pop(actor, None) is not None
        elif op == "room.remove":
            if not is_owner:
                raise TripError("only the organizer can remove rooms")
            rooms.remove(room)
        return True

    if op.startswith("task."):
        tasks = trip["tasks"]
        if op == "task.add":
            _cap(trip, "tasks")
            text = _t(a.get("text"), 200)
            if not text:
                raise TripError("empty task")
            tid = _pick_id(a.get("id"))
            if any(t["id"] == tid for t in tasks):
                return False
            task = {"id": tid, "text": text, "due": _day(a["due"]) if a.get("due") else "", "done": False,
                    "assignee_id": "", "assignee_name": "", "added_by": actor}
            _assign(trip, task, a.get("assignee"))
            tasks.append(task)
            return True
        task = _find(tasks, a.get("id"))
        if op in ("task.done", "task.undone"):
            task["done"] = op == "task.done"
        elif op == "task.assign":
            _assign(trip, task, a.get("assignee"))
        elif op == "task.remove":
            if not (is_owner or task["added_by"] == actor):
                raise TripError("only the organizer or whoever added it can remove it")
            tasks.remove(task)
        return True

    if op.startswith("poll."):
        polls = trip["polls"]
        if op == "poll.add":
            _cap(trip, "polls")
            q = _t(a.get("question"), 200)
            opts = [_t(o, 120) for o in (a.get("options") or []) if _t(o, 120)][:MAX_OPTIONS]
            if not q or len(opts) < 2:
                raise TripError("a poll needs a question and at least two options")
            pid = _pick_id(a.get("id"))
            if any(p["id"] == pid for p in polls):
                return False
            polls.append({"id": pid, "question": q, "options": [{"id": f"o{i}", "text": t} for i, t in enumerate(opts)],
                          "votes": {}, "closed": False, "added_by": actor})
            return True
        poll = _find(polls, a.get("id"))
        if op == "poll.vote":
            if poll["closed"]:
                raise TripError("that poll is closed")
            option = a.get("option")
            if option not in {o["id"] for o in poll["options"]}:
                raise TripError("no such option")
            poll["votes"][actor] = option
        elif op == "poll.close":
            if not (is_owner or poll["added_by"] == actor):
                raise TripError("only the organizer or whoever asked can close a poll")
            poll["closed"] = True
        return True
    raise TripError(f"unhandled operation {op!r}")  # pragma: no cover


def _assign(trip: dict, task: dict, assignee: Any) -> None:
    if not assignee:
        task.update(assignee_id="", assignee_name="")
        return
    people = {trip["owner"]: trip["owner_name"], **trip["members"]}
    if assignee not in people:
        raise TripError("assign tasks to someone on the trip")
    task.update(assignee_id=assignee, assignee_name=people[assignee])


def update_details(trip: dict, a: dict) -> None:
    """Owner-only edits of the trip itself."""
    if "title" in a:
        trip["title"] = _t(a["title"], 120) or trip["title"]
    if "destination" in a:
        trip["destination"] = _t(a["destination"], 200)
    if "notes" in a:
        trip["notes"] = _t(a["notes"], 2000)
    s = _day(a["start_date"]) if a.get("start_date") else trip["start_date"]
    e = _day(a["end_date"]) if a.get("end_date") else trip["end_date"]
    if e < s:
        raise TripError("the trip ends before it starts")
    trip["start_date"], trip["end_date"] = s, e
    if "status" in a:
        if a["status"] not in STATUSES:
            raise TripError(f"status must be one of {', '.join(STATUSES)}")
        trip["status"] = a["status"]


WIRE_KEYS = ("id", "rev", "title", "destination", "start_date", "end_date", "tz", "notes", "status", "owner", "owner_name",
             "members", "itinerary", "travelers", "rides", "rooms", "tasks", "polls", "links")


def wire(trip: dict) -> dict:
    return {k: trip[k] for k in WIRE_KEYS}


def validate_wire_trip(data: Any, owner: str) -> dict:
    """Sanitize a trip received from its owner: types, sizes, and references."""
    if not isinstance(data, dict) or data.get("owner") != owner:
        raise TripError("only the organizer may share this trip")
    tid = data.get("id")
    if not isinstance(tid, str) or not re.match(r"^[A-Za-z0-9]{8,32}$", tid):
        raise TripError("bad trip id")
    if not isinstance(data.get("rev"), int) or data["rev"] < 1:
        raise TripError("bad trip revision")
    members = data.get("members")
    if not isinstance(members, dict) or len(members) > MAX_MEMBERS:
        raise TripError("bad members")
    for section, cap in LIMITS.items():
        if not isinstance(data.get(section), list) or len(data[section]) > cap:
            raise TripError(f"bad {section}")
    try:
        from zoneinfo import ZoneInfo

        tz = str(data.get("tz", "UTC"))[:64]
        ZoneInfo(tz)
    except Exception as exc:
        raise TripError("bad timezone") from exc
    names = {str(k)[:64]: _t(v, 60) for k, v in members.items()}

    def ids(d: Any) -> dict:
        return {str(k)[:64]: _t(v, 60) for k, v in list(d.items())[:MAX_MEMBERS]} if isinstance(d, dict) else {}

    def sid(x: Any) -> str:
        v = x.get("id") if isinstance(x, dict) else None
        if not isinstance(v, str) or not _ID.match(v):
            raise TripError("bad item id")
        return v

    out = {
        "id": tid, "rev": data["rev"], "title": _t(data.get("title"), 120) or "Trip", "destination": _t(data.get("destination"), 200),
        "start_date": _day(data.get("start_date")), "end_date": _day(data.get("end_date")), "tz": tz,
        "notes": _t(data.get("notes"), 2000), "status": data.get("status") if data.get("status") in STATUSES else "planning",
        "owner": owner, "owner_name": _t(data.get("owner_name"), 60), "members": names,
        "itinerary": [{"id": sid(i), "kind": i.get("kind") if i.get("kind") in KINDS else "other", "title": _t(i.get("title"), 200),
                       "start": _when(i.get("start")), "end": _when(i.get("end")), "location": _t(i.get("location"), 300),
                       "confirmation": _t(i.get("confirmation"), 60), "details": _t(i.get("details"), 1000), "url": _url(i.get("url")),
                       "added_by": str(i.get("added_by", ""))[:64], "added_by_name": _t(i.get("added_by_name"), 60)}
                      for i in data["itinerary"]],
        "travelers": {}, "links": {},
        "rides": [{"id": sid(r), "driver_id": str(r.get("driver_id", ""))[:64], "driver_name": _t(r.get("driver_name"), 60),
                   "seats": r["seats"] if isinstance(r.get("seats"), int) and 1 <= r["seats"] <= 50 else 1,
                   "from": _t(r.get("from"), 200), "leaves_at": _when(r.get("leaves_at")), "passengers": ids(r.get("passengers"))}
                  for r in data["rides"]],
        "rooms": [{"id": sid(r), "name": _t(r.get("name"), 80), "beds": r["beds"] if isinstance(r.get("beds"), int) and 1 <= r["beds"] <= 50 else 1,
                   "occupants": ids(r.get("occupants"))} for r in data["rooms"]],
        "tasks": [{"id": sid(t), "text": _t(t.get("text"), 200), "due": _day(t["due"]) if t.get("due") else "", "done": bool(t.get("done")),
                   "assignee_id": str(t.get("assignee_id", ""))[:64], "assignee_name": _t(t.get("assignee_name"), 60),
                   "added_by": str(t.get("added_by", ""))[:64]} for t in data["tasks"]],
        "polls": [{"id": sid(p), "question": _t(p.get("question"), 200), "closed": bool(p.get("closed")), "added_by": str(p.get("added_by", ""))[:64],
                   "options": [{"id": str(o.get("id", ""))[:8], "text": _t(o.get("text"), 120)} for o in (p.get("options") or [])[:MAX_OPTIONS] if isinstance(o, dict)],
                   "votes": {str(k)[:64]: str(v)[:8] for k, v in list((p.get("votes") or {}).items())[:MAX_MEMBERS + 1]}}
                  for p in data["polls"]],
    }
    trav = data.get("travelers") if isinstance(data.get("travelers"), dict) else {}
    for k, v in list(trav.items())[:MAX_MEMBERS + 1]:
        if not isinstance(v, dict):
            continue
        out["travelers"][str(k)[:64]] = {
            "name": _t(v.get("name"), 60), "notes": _t(v.get("notes"), 500),
            **{leg: {"when": _when((v.get(leg) or {}).get("when")), "how": _t((v.get(leg) or {}).get("how"), 120),
                     "where": _t((v.get(leg) or {}).get("where"), 200), "needs_pickup": bool((v.get(leg) or {}).get("needs_pickup"))}
               for leg in ("arrive", "depart")},
        }
    links = data.get("links") if isinstance(data.get("links"), dict) else {}
    if isinstance(links.get("list_id"), str) and re.match(r"^[A-Za-z0-9]{8,32}$", links["list_id"]):
        out["links"]["list_id"] = links["list_id"]
    return out


def trip_days(trip: dict) -> Interval:
    """The trip's whole-day span in its own timezone, as a UTC interval."""
    from zoneinfo import ZoneInfo

    z = ZoneInfo(trip.get("tz", "UTC"))
    s = datetime.combine(date.fromisoformat(trip["start_date"]), datetime.min.time(), z)
    e = datetime.combine(date.fromisoformat(trip["end_date"]) + timedelta(days=1), datetime.min.time(), z)
    return Interval(s.astimezone(UTC), e.astimezone(UTC))


class TripsMixin:
    """Node methods for trips. Mixed into :class:`confer.node.Node`."""

    def create_trip(self: "Node", title: str, with_: list[str], *, start_date: str, end_date: str, destination: str = "",
                    notes: str = "", packing_list: bool = True) -> dict:
        from .node import NodeError

        people = [self._active_contact(n) for n in with_]
        try:
            trip = new_trip(title=title, owner=self.identity.agent_id, owner_name=self.name,
                            members={c.agent_id: c.name for c in people}, start_date=start_date, end_date=end_date,
                            destination=destination, notes=notes, tz=self.tz)
        except TripError as exc:
            raise NodeError(str(exc)) from exc
        trip["role"] = "owner"
        if packing_list and people:
            trip["links"]["list_id"] = self.create_list(f"Packing — {trip['title']}", [c.name for c in people])["id"]
        self.store.save_trip(trip)
        self._broadcast_trip(trip)
        return trip

    def trips(self: "Node") -> list[dict]:
        return [t for t in self.store.all_trips() if not t.get("leaving")]

    def get_trip(self: "Node", trip_id: str) -> dict:
        from .node import NodeError

        trip = self.store.find_trip(trip_id)
        if not trip:
            raise NodeError(f"no trip {trip_id!r}")
        return trip

    def trip_op(self: "Node", trip_id: str, op: str, **args: Any) -> dict:
        """Edit a trip. Owners apply directly (``trip.update`` is owner-only);
        members send the edit to the owner's node. ``args`` per op, e.g.
        itinerary.add(kind, title, start, end, location, confirmation, details, url),
        traveler.set(arrive={when,how,where,needs_pickup}, depart={...}, notes),
        ride.offer(seats, from, leaves_at), ride.join(id), room.add(name, beds),
        room.join(id), task.add(text, assignee=<contact name or 'me'>, due),
        task.done(id), poll.add(question, options=[...]), poll.vote(id, option)."""
        from .node import NodeError

        args = dict(args)
        if "assignee" in args and args["assignee"]:
            args["assignee"] = self._resolve_person(args["assignee"])
        with self.store.transaction():
            trip = self.get_trip(trip_id)
            if trip.get("role") == "owner":
                try:
                    if op == "trip.update":
                        update_details(trip, args)
                        changed = True
                    else:
                        if op.endswith(".add") or op == "ride.offer":
                            args.setdefault("id", _nid())
                        changed = apply_op(trip, self.identity.agent_id, self.name, op, args)
                except TripError as exc:
                    raise NodeError(str(exc)) from exc
                if changed:
                    trip["rev"] += 1
                    self.store.save_trip(trip)
                    self._broadcast_trip(trip, kick=False)
            else:
                if op in OWNER_OPS:
                    raise NodeError("only the organizer can change the trip's details")
                if op not in OPS:
                    raise NodeError(f"unknown trip operation {op!r}")
                owner = self.store.contact(trip["owner"])
                if not owner:
                    raise NodeError("the organizer is no longer a contact")
                if op.endswith(".add") or op == "ride.offer":
                    args.setdefault("id", _nid())
                self._send(owner, "trip.op", {"trip_id": trip["id"], "op": op, "args": args}, kick=False)
                if op == "leave":
                    trip["leaving"] = True
                    self.store.save_trip(trip)
        self.kick()
        return trip

    def cancel_trip(self: "Node", trip_id: str) -> dict:
        trip = self.get_trip(trip_id)
        if trip.get("role") != "owner":
            return self.trip_op(trip_id, "leave")
        return self.trip_op(trip_id, "trip.update", status="cancelled")

    def trip_budget(self: "Node", trip_id: str) -> dict:
        """What you've paid and are owed for this trip (expenses tagged with its id)."""
        trip = self.get_trip(trip_id)
        out: dict[str, dict[str, int]] = {}
        for e in self.store.entries():
            if e.get("plan_id") != trip["id"] or e["kind"] != "expense" or e["status"] in ("cancelled", "disputed"):
                continue
            b = out.setdefault(e["currency"], {"you_paid_shares": 0, "you_owe": 0})
            b["you_paid_shares" if e["payer"] == "me" else "you_owe"] += e["cents"]
        return out

    def _resolve_person(self: "Node", who: str) -> str:
        """'me', a contact name, or an agent id -> agent id."""
        if who in ("me", self.name, self.identity.agent_id):
            return self.identity.agent_id
        return self._contact_named(who).agent_id

    def _broadcast_trip(self: "Node", trip: dict, kick: bool = True) -> None:
        body = {"trip": wire(trip)}
        for aid in trip["members"]:
            if c := self.store.contact(aid):
                self._send(c, "trip.share", body, kick=False)
        if kick:
            self.kick()

    # ------------------------------------------------------------ handlers
    def _trip_handlers(self: "Node") -> dict[str, Callable[[Contact, dict], None]]:
        return {"trip.share": self._on_trip_share, "trip.op": self._on_trip_op, "trip.close": self._on_trip_close}

    def _on_trip_share(self: "Node", contact: Contact, body: dict) -> None:
        from .node import Rejected

        if not contact.can("trips"):
            raise Rejected(f"{self.name} hasn't allowed trips from you")
        try:
            data = validate_wire_trip(body.get("trip"), contact.agent_id)
        except TripError as exc:
            raise Rejected(str(exc)) from exc
        me = self.identity.agent_id
        with self.store.transaction():
            existing = self.store.get_trip(data["id"])
            if existing and (existing.get("owner") != contact.agent_id or existing.get("role") == "owner"):
                raise Rejected("trip id collision")
            if me not in data["members"]:  # removed, or my leave went through
                if existing:
                    self.store.delete_trip(data["id"])
                return
            if existing and (existing["rev"] >= data["rev"] or existing.get("leaving")):
                return
            if not existing and sum(1 for t in self.store.all_trips() if t.get("owner") == contact.agent_id) >= 50:
                raise Rejected("too many trips from you")
            self.store.save_trip({**data, "role": "member"})
        # tell the human only about things that concern them
        if not existing:
            self._inbox("trip", f"🧳 {contact.name} added you to the trip {data['title']!r}"
                        f"{' to ' + data['destination'] if data['destination'] else ''} ({data['start_date']} → {data['end_date']}).",
                        ref=data["id"], contact=contact.agent_id)
            return
        old_tasks = {t["id"]: t for t in existing["tasks"]}
        for t in data["tasks"]:
            if t["assignee_id"] == me and old_tasks.get(t["id"], {}).get("assignee_id") != me and not t["done"]:
                due = f" (due {t['due']})" if t["due"] else ""
                self._inbox("trip", f"🧳 {data['title']}: you're on “{t['text']}”{due}.", ref=data["id"], contact=contact.agent_id, actionable=True)
        old_polls = {p["id"] for p in existing["polls"]}
        for p in data["polls"]:
            if p["id"] not in old_polls and not p["closed"]:
                self._inbox("trip", f"🗳️ {data['title']}: {p['question']} — " + " / ".join(o["text"] for o in p["options"]),
                            ref=data["id"], contact=contact.agent_id, actionable=True)
        if data["status"] != existing["status"]:
            self._inbox("trip", f"🧳 {data['title']} is now {data['status']}.", ref=data["id"], contact=contact.agent_id)
        self._write_calendar()

    def _on_trip_op(self: "Node", contact: Contact, body: dict) -> None:
        from .node import Rejected

        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        with self.store.transaction():
            trip = self.store.get_trip(str(body.get("trip_id", "")))
            if not trip or trip.get("role") != "owner":
                raise Rejected("no such trip")
            try:
                changed = apply_op(trip, contact.agent_id, contact.name, str(body.get("op", "")), args)
            except TripError as exc:
                raise Rejected(str(exc)) from exc
            if not changed:
                return
            trip["rev"] += 1
            self.store.save_trip(trip)
            self._broadcast_trip(trip, kick=False)
            if body.get("op") == "leave" and (c := self.store.contact(contact.agent_id)):
                self._send(c, "trip.close", {"trip_id": trip["id"]}, kick=False)
        self.kick()

    def _on_trip_close(self: "Node", contact: Contact, body: dict) -> None:
        trip = self.store.get_trip(str(body.get("trip_id", "")))
        if trip and trip.get("owner") == contact.agent_id and trip.get("role") != "owner":
            self.store.delete_trip(trip["id"])
            self._write_calendar()
