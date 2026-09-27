"""The Confer node: one person's agent endpoint.

A node holds an identity, a trusted-contact list with per-contact grants, the
plans it organizes or was invited to, and an inbox for its human. Every
outbound message goes through a durable outbox (retries with backoff), and
every inbound envelope is authenticated, de-duplicated and permission-checked
before a handler sees it.

Grants are what a contact may do *with my node*:
  plans        may propose plans to me (I decide, unless autoconfirm)
  autoconfirm  plans that fit my calendar are accepted without asking me
  files        may send me files (end-to-end encrypted)
  notes        may send me short messages
  intros       may introduce other people to me (I still approve each one)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import shlex
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import plans as P
from .availability import (
    UTC,
    Availability,
    BusyProvider,
    CompositeProvider,
    Hours,
    ICSProvider,
    Interval,
    StaticProvider,
    expand,
    normalize_rrule,
    spread,
)
from .envelope import SEEN_TTL_SECONDS, EnvelopeError, MAX_AGE_SECONDS, canonical, open_, seal
from .identity import Identity, b64d, b64e, fingerprint, verify
from .store import Contact, Store
from .transport import DeliveryError, HttpTransport

log = logging.getLogger("confer")

GRANTS = ("plans", "autoconfirm", "files", "notes", "intros")
DEFAULT_GRANTS = ["plans", "files", "notes", "intros"]
KEY_GRACE_SECONDS = 30 * 24 * 3600  # keep answering on a rotated-away key this long
INTRO_TTL_SECONDS = 14 * 24 * 3600
MAX_PENDING_INTROS = 20  # per introducer
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_NOTE_CHARS = 4000
INVITE_PREFIX = "confer1:"


class Rejected(Exception):
    """The envelope is authentic but refused; the sender should not retry."""


class NodeError(RuntimeError):
    """A local operation cannot proceed (bad input, unknown contact, ...)."""


def parse_grants(spec: str | list[str] | None, base: list[str] | None = None) -> list[str]:
    """``"plans,files"`` -> list; ``"+autoconfirm,-files"`` edits ``base``."""
    if spec is None:
        return list(base if base is not None else DEFAULT_GRANTS)
    items = [s.strip() for s in (spec.split(",") if isinstance(spec, str) else spec) if s.strip()]
    relative = all(i[0] in "+-" for i in items) and items
    out = set(base or []) if relative else set()
    for item in items:
        name = item.lstrip("+-")
        if name not in GRANTS:
            raise NodeError(f"unknown grant {name!r}; choose from {', '.join(GRANTS)}")
        (out.discard if item.startswith("-") else out.add)(name)
    return sorted(out)


class PlansProvider:
    """My own plans as busy time: confirmed plans I'm attending, plus *tentative
    holds* — times I've offered (as organizer) or accepted (as participant) on
    plans that haven't resolved yet — so two concurrent negotiations can't both
    land on the same evening. ``exclude`` skips the plan being (re)evaluated."""

    def __init__(self, node: "Node", exclude: str | None = None):
        self.node = node
        self.exclude = exclude

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        out: list[Interval] = []
        window = Interval(start, end)
        holds = bool(self.node.config.get("tentative_holds", True))
        for plan in self.node.store.plans():
            if plan["id"] == self.exclude:
                continue
            for idx in self._busy_slots(plan, holds):
                slot = Interval.from_wire(plan["slots"][idx])
                out.extend(expand(slot, plan.get("rrule"), plan.get("tz", "UTC"), count=None, window=window))
        return out

    def _busy_slots(self, plan: dict, holds: bool) -> list[int]:
        if plan["status"] == "confirmed" and plan.get("chosen") is not None and self.node._attending(plan):
            return [plan["chosen"]]
        if holds and plan["status"] == "proposed":
            if plan.get("role") == "organizer":
                return list(range(len(plan["slots"])))
            if plan.get("my_status") == "accepted":
                return list(plan.get("my_ok_slots", []))
        return []


class Node:
    def __init__(
        self,
        home: Path,
        *,
        transport: HttpTransport | None = None,
        provider: BusyProvider | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.home = Path(home).expanduser()
        cfg_path = self.home / "config.json"
        if not cfg_path.exists():
            raise NodeError(f"no Confer node at {self.home} — run `confer init` first")
        self.config: dict[str, Any] = json.loads(cfg_path.read_text())
        self.clock = clock
        self._load_identity()
        self.store = Store(self.home / "state.db")
        self.transport = transport or HttpTransport()
        self._provider = provider
        self._flush_lock = threading.Lock()
        # the server replaces this with a non-blocking wake-up of its flusher thread
        self.kick: Callable[[], None] = self.flush

    def _load_identity(self) -> None:
        path = self.home / "identity.key"
        self._key_mtime = path.stat().st_mtime_ns
        self.identity = Identity.load(path)
        # after `rotate_key`, the previous key keeps working for a grace period so
        # messages already in flight (or queued at peers) can still be opened
        self.old_identity: Identity | None = None
        old_path = self.home / "identity.old.key"
        if old_path.exists() and self.clock() - float(self.config.get("rotated_at", 0)) < KEY_GRACE_SECONDS:
            self.old_identity = Identity.load(old_path)

    def _refresh_identity(self) -> None:
        """Another process (e.g. `confer rotate-key` while `confer serve` runs) may
        have replaced the key on disk; pick it up instead of rejecting mail to it."""
        try:
            changed = (self.home / "identity.key").stat().st_mtime_ns != self._key_mtime
        except OSError:
            return
        if changed:
            self.config = json.loads((self.home / "config.json").read_text())
            self._load_identity()

    # ------------------------------------------------------------------ setup
    @classmethod
    def init(cls, home: Path, name: str, *, endpoint: str = "", relay: str = "", tz: str = "UTC", **kw: Any) -> "Node":
        home = Path(home).expanduser()
        if (home / "config.json").exists():
            raise NodeError(f"a node already exists at {home}")
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        Identity.generate().save(home / "identity.key")
        cfg = {
            "name": name,
            "endpoint": check_url(endpoint) if endpoint else "",
            "relay": check_url(relay) if relay else "",
            "tz": tz,
            "hours_start": "08:00",
            "hours_end": "22:00",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "buffer_minutes": 0,
            "calendar": "",  # .ics path/URL, or a .json list of busy intervals
            "notify_webhook": "",
            "notify_cmd": "",
            "feed_token": secrets.token_urlsafe(18),
            "tentative_holds": True,
        }
        (home / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        return cls(home, **kw)

    def save_config(self) -> None:
        (self.home / "config.json").write_text(json.dumps(self.config, indent=2) + "\n")

    def close(self) -> None:
        self.store.close()

    @property
    def name(self) -> str:
        return self.config.get("name", "")

    @property
    def tz(self) -> str:
        return self.config.get("tz", "UTC")

    def now(self) -> float:
        return self.clock()

    def availability(self, exclude_plan: str | None = None) -> Availability:
        base = self._provider
        if base is None:
            src = self.config.get("calendar", "")
            if not src:
                base = StaticProvider()
            elif src.endswith(".json"):
                base = StaticProvider.from_json(Path(src).expanduser())
            else:
                base = ICSProvider(src)
        return Availability(
            CompositeProvider(base, PlansProvider(self, exclude=exclude_plan)),
            Hours.from_config(self.config),
            int(self.config.get("buffer_minutes", 0)),
        )

    def whoami(self) -> dict:
        return {
            "name": self.name,
            "agent_id": self.identity.agent_id,
            "fingerprint": self.identity.fingerprint,
            "endpoint": self.config.get("endpoint", ""),
            "relay": self.config.get("relay", ""),
            "tz": self.tz,
        }

    # ------------------------------------------------------------ contacts
    def _contact_named(self, name: str) -> Contact:
        c = self.store.contact_by_name(name) or self.store.contact(name)
        if not c:
            raise NodeError(f"no contact named {name!r}")
        return c

    def _active_contact(self, name: str) -> Contact:
        c = self._contact_named(name)
        if c.status != "active":
            raise NodeError(f"{c.name} hasn't finished pairing yet")
        return c

    def _unique_name(self, wanted: str, agent_id: str) -> str:
        name, n = (wanted.strip() or "contact")[:60], 2
        while (c := self.store.contact_by_name(name)) and c.agent_id != agent_id:
            name, n = f"{wanted.strip()[:56]} ({n})", n + 1
        return name

    def set_grants(self, name: str, spec: str | list[str]) -> Contact:
        c = self._contact_named(name)
        c.grants = parse_grants(spec, c.grants)
        self.store.upsert_contact(c)
        return c

    def remove_contact(self, name: str) -> None:
        self.store.remove_contact(self._contact_named(name).agent_id)

    # ------------------------------------------------------------- pairing
    def create_invite(self, name: str, grants: str | list[str] | None = None, ttl_hours: float = 72) -> str:
        """One-time invite for a trusted person. Share it privately (text it)."""
        if not (self.config.get("endpoint") or self.config.get("relay")):
            raise NodeError("set an endpoint or relay first (`confer config endpoint https://...`) so peers can reach you")
        secret = secrets.token_bytes(24)
        self.store.add_invite(hashlib.sha256(secret).hexdigest(), name, parse_grants(grants), self.now() + ttl_hours * 3600)
        token = {
            "id": self.identity.agent_id,
            "name": self.name,
            "endpoint": self.config.get("endpoint", ""),
            "relay": self.config.get("relay", ""),
            "secret": b64e(secret),
        }
        return INVITE_PREFIX + b64e(json.dumps(token, separators=(",", ":")).encode())

    @staticmethod
    def decode_invite(token: str) -> dict:
        token = token.strip()
        if not token.startswith(INVITE_PREFIX):
            raise NodeError("not a Confer invite")
        try:
            data = json.loads(b64d(token[len(INVITE_PREFIX) :]))
            fingerprint(data["id"])  # validates the key
            b64d(data["secret"])
        except Exception as exc:
            raise NodeError("corrupt invite") from exc
        if not (data.get("endpoint") or data.get("relay")):
            raise NodeError("invite has no endpoint or relay")
        return data

    def accept_invite(self, token: str, *, name: str | None = None, grants: str | list[str] | None = None) -> Contact:
        data = self.decode_invite(token)
        if data["id"] == self.identity.agent_id:
            raise NodeError("that invite is your own")
        if not (self.config.get("endpoint") or self.config.get("relay")):
            raise NodeError("set an endpoint or relay first so the inviter can reach you back")
        contact = Contact(
            agent_id=data["id"],
            name=self._unique_name(name or data.get("name") or "contact", data["id"]),
            endpoint=_clean_url(data.get("endpoint")),
            relay=_clean_url(data.get("relay")),
            grants=parse_grants(grants),
            status="pending",
            created_at=self.now(),
        )
        existing = self.store.contact(contact.agent_id)
        if existing and existing.status == "active":
            contact.status = "active"
        self.store.upsert_contact(contact)
        self._send(contact, "pair.request", {
            "secret": data["secret"],
            "name": self.name,
            "endpoint": self.config.get("endpoint", ""),
            "relay": self.config.get("relay", ""),
        })
        return contact

    # --------------------------------------------------------- introductions
    def introduce(self, a: str, b: str, note: str = "") -> str:
        """Vouch for two of your contacts to each other. Each side's owner approves;
        once both have, their agents pair directly — no token to pass around."""
        ca, cb = self._active_contact(a), self._active_contact(b)
        if ca.agent_id == cb.agent_id:
            raise NodeError("introduce two different people")
        intro_id = secrets.token_hex(12)
        for to, peer in ((ca, cb), (cb, ca)):
            self._send(to, "intro.offer", {
                "intro_id": intro_id, "note": note[:500],
                "peer": {"id": peer.agent_id, "name": peer.name, "endpoint": peer.endpoint, "relay": peer.relay},
            }, kick=False)
        self.kick()
        return intro_id

    def accept_intro(self, intro_id: str, *, name: str | None = None, grants: str | list[str] | None = None) -> Contact:
        intro = self._open_intro(intro_id)
        contact = Contact(
            agent_id=intro["peer_id"], name=self._unique_name(name or intro["peer_name"], intro["peer_id"]),
            endpoint=intro["endpoint"], relay=intro["relay"], grants=parse_grants(grants),
            status="active" if intro["peer_ready"] else "pending", created_at=self.now(),
        )
        self.store.upsert_contact(contact)
        intro["status"] = "done" if intro["peer_ready"] else "accepted"
        self.store.save_intro(intro)
        self.store.close_inbox(ref=intro_id)
        self._send(contact, "pair.intro", {"intro_id": intro_id, "name": self.name,
                                           "endpoint": self.config.get("endpoint", ""), "relay": self.config.get("relay", "")})
        if intro["peer_ready"]:
            self._connected_via_intro(contact, intro)
        return contact

    def decline_intro(self, intro_id: str) -> None:
        intro = self._open_intro(intro_id)
        intro["status"] = "declined"
        self.store.save_intro(intro)
        self.store.close_inbox(ref=intro_id)

    def intros(self) -> list[dict]:
        return self.store.intros()

    def _open_intro(self, intro_id: str) -> dict:
        intro = self.store.intro(intro_id)
        if not intro or intro["status"] != "offered":
            raise NodeError(f"no pending introduction {intro_id!r}")
        if intro["expires_at"] < self.now():
            raise NodeError("that introduction has expired")
        return intro

    def _connected_via_intro(self, contact: Contact, intro: dict) -> None:
        via = self.store.contact(intro["introducer"])
        self._inbox("pair", f"🤝 Connected with {contact.name} (introduced by {via.name if via else 'a contact'}; "
                    f"fingerprint {fingerprint(contact.agent_id)}). Grants: {', '.join(contact.grants) or 'none'}.", contact=contact.agent_id)

    # ---------------------------------------------------------- key rotation
    def rotate_key(self) -> str:
        """Move this node to a fresh keypair. Every contact gets a ``key.rotate``
        signed by the old key *and* proving possession of the new one; the old key
        keeps decrypting in-flight messages for 30 days. Returns the new agent id."""
        old, new = self.identity, Identity.generate()
        ts = int(self.now())
        proof = b64e(new.sign(canonical({"old": old.agent_id, "new": new.agent_id, "ts": ts})))
        for c in self.store.contacts():
            if c.status == "active":
                self._send(c, "key.rotate", {"new_id": new.agent_id, "ts": ts, "proof": proof}, kick=False)
        old.save(self.home / "identity.old.key")
        new.save(self.home / "identity.key")
        self._key_mtime = (self.home / "identity.key").stat().st_mtime_ns
        self.config["rotated_at"] = self.now()
        self.save_config()
        self.store.rename_agent(old.agent_id, new.agent_id)  # my own plans
        self.identity, self.old_identity = new, old
        self.kick()
        return new.agent_id

    # --------------------------------------------------------------- plans
    def create_plan(
        self,
        title: str,
        with_: list[str],
        *,
        duration_minutes: int = 60,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        between: tuple[dtime, dtime] | None = None,
        slots: list[Interval] | None = None,
        candidates: int = 4,
        rrule: str | None = None,
        location: str = "",
        notes: str = "",
        quorum: str | int = "all",
        deadline_hours: float | None = None,
    ) -> dict:
        people = [self._active_contact(n) for n in with_]
        if len({c.agent_id for c in people}) != len(people):
            raise NodeError("a contact is listed twice")
        try:
            rrule = normalize_rrule(rrule)
        except ValueError as exc:
            raise NodeError(f"bad recurrence rule: {exc}") from exc
        if slots is None:
            start = window_start or datetime.fromtimestamp(self.now(), tz=UTC) + timedelta(hours=1)
            end = window_end or start + timedelta(days=7)
            free = self.availability().candidates(start, end, timedelta(minutes=duration_minutes), between=between, rrule=rrule)
            slots = spread(free, max(1, candidates))
            if not slots:
                raise NodeError("you have no free time matching that window")
        plan = P.new_plan(
            title=title,
            organizer=self.identity.agent_id,
            organizer_name=self.name,
            participants={c.agent_id: c.name for c in people},
            slots=slots,
            rrule=rrule,
            location=location,
            notes=notes,
            quorum=quorum,
            deadline=self.now() + deadline_hours * 3600 if deadline_hours else None,
            tz=self.tz,
        )
        plan["role"] = "organizer"
        plan["proposed_at"] = self.now()
        self.store.save_plan(plan)
        for c in people:
            self._send(c, "plan.propose", {"plan": P.wire_view(plan)}, kick=False)
        self.kick()
        return plan

    def _plan(self, plan_id: str) -> dict:
        plan = self.store.find_plan(plan_id)
        if not plan:
            raise NodeError(f"no plan {plan_id!r}")
        return plan

    def plans(self) -> list[dict]:
        return self.store.plans()

    def _attending(self, plan: dict) -> bool:
        if plan.get("role") == "organizer":
            return True
        return plan.get("my_status") == "accepted" and plan.get("chosen") in plan.get("my_ok_slots", [])

    def respond(self, plan_id: str, decision: str, *, slots: list[int] | None = None, note: str = "",
                counter: list[Interval] | None = None, prefer: list[int] | None = None, _kick: bool = True) -> dict:
        """Participant answers a proposal. ``slots`` are 0-based option indexes;
        ``prefer`` marks the ones you'd *rather* have (a subset of ``slots``)."""
        with self.store.transaction():
            plan = self._plan(plan_id)
            if plan.get("role") != "participant":
                raise NodeError("you organize this plan — use revise/cancel instead")
            if plan["status"] == "cancelled":
                raise NodeError("this plan was cancelled")
            if decision not in P.DECISIONS:
                raise NodeError(f"decision must be one of {sorted(P.DECISIONS)}")
            if decision == "accept":
                ok = sorted(set(slots if slots is not None else plan.get("suggested", [])))
                if not ok or any(not 0 <= i < len(plan["slots"]) for i in ok):
                    raise NodeError("say which options work (none of them look free on your calendar)")
            else:
                ok = []
            preferred = sorted(set(prefer or []) & set(ok))
            if decision == "counter" and not counter:
                raise NodeError("a counter needs at least one suggested time")
            plan.update(my_status={"accept": "accepted", "decline": "declined", "counter": "countered"}[decision], my_ok_slots=ok, my_prefer=preferred)
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"], kind="plan.invite")
            self.store.close_inbox(ref=plan["id"], kind="plan.reminder")
        organizer = self.store.contact(plan["organizer"])
        if not organizer:
            raise NodeError("the organizer is no longer a contact")
        self._send(organizer, "plan.respond", {
            "plan_id": plan["id"],
            "rev": plan["rev"],
            "decision": decision,
            "ok_slots": ok,
            "prefer": preferred,
            "note": note[:1000],
            "counter": [c.to_wire() for c in counter or []],
        }, kick=_kick)
        return plan

    def revise(self, plan_id: str, *, slots: list[Interval] | None = None, duration_minutes: int | None = None,
               window_start: datetime | None = None, window_end: datetime | None = None,
               between: tuple[dtime, dtime] | None = None, candidates: int = 4) -> dict:
        with self.store.transaction():
            plan = self._plan(plan_id)
            if plan.get("role") != "organizer":
                raise NodeError("only the organizer can revise a plan")
            if slots is None:
                first = Interval.from_wire(plan["slots"][0])
                dur = timedelta(minutes=duration_minutes) if duration_minutes else first.end - first.start
                start = window_start or datetime.fromtimestamp(self.now(), tz=UTC) + timedelta(hours=1)
                end = window_end or start + timedelta(days=7)
                slots = spread(self.availability(exclude_plan=plan["id"]).candidates(start, end, dur, between=between, rrule=plan.get("rrule")), candidates)
                if not slots:
                    raise NodeError("you have no free time matching that window")
            P.revise(plan, slots)
            plan["proposed_at"] = self.now()
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"])
        self._broadcast(plan, "plan.propose", {"plan": P.wire_view(plan)})
        return plan

    def cancel(self, plan_id: str, reason: str = "") -> dict:
        plan = self._plan(plan_id)
        if plan.get("role") != "organizer":
            return self.respond(plan_id, "decline", note=reason or "can't make it")
        with self.store.transaction():
            plan = self._plan(plan_id)
            plan["status"] = "cancelled"
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"])
        self._broadcast(plan, "plan.cancel", {"plan_id": plan["id"], "reason": reason[:500]})
        self._write_calendar()
        return plan

    def _broadcast(self, plan: dict, type_: str, body: dict, *, kick: bool = True) -> None:
        for aid in plan["participants"]:
            c = self.store.contact(aid)
            if c:
                self._send(c, type_, body, kick=False)
        if kick:
            self.kick()

    def _finalize_if_ready(self, plan: dict) -> None:
        """Organizer: tally, and queue the outcome if it changed. Caller holds the tx
        lock and must call ``self.kick()`` after releasing it (no network I/O under the lock)."""
        status, chosen = P.tally(plan, self.now())
        if status == plan["status"]:
            return
        plan["status"], plan["chosen"] = status, chosen
        plan["updated_at"] = self.now()
        self.store.save_plan(plan)
        if status == "confirmed":
            when = self._fmt_slot(plan["slots"][chosen])
            self._inbox("plan.confirmed", f"✅ {plan['title']} is set for {when}.", ref=plan["id"])
            self._broadcast(plan, "plan.final", {"plan": P.wire_view(plan)}, kick=False)
            self._write_calendar()
        elif status == "needs_reschedule":
            counters = [c for p in plan["participants"].values() for c in p.get("counter", [])]
            hint = ""
            if counters:
                hint = " Suggestions: " + "; ".join(self._fmt_slot(c) for c in counters[:5]) + "."
            self._inbox("plan.reschedule", f"No time works for everyone for {plan['title']}.{hint} "
                        f"Revise it: confer plan revise {plan['id']}", ref=plan["id"], actionable=True,
                        payload={"counters": counters})

    def _nudge_due(self, plan: dict) -> bool:
        """Remind stragglers once: in the last quarter before the deadline (at least
        1h before it), or after ``nudge_after_hours`` (default 24) with no deadline."""
        now, since = self.now(), plan.get("proposed_at", plan["created_at"])
        if plan.get("deadline"):
            lead = max(3600.0, (plan["deadline"] - since) * 0.25)
            return plan["deadline"] - lead <= now < plan["deadline"]
        return now - since >= float(self.config.get("nudge_after_hours", 24)) * 3600

    def send_reminders(self) -> int:
        """Organizer: nudge participants who haven't answered. Returns how many were nudged."""
        sent = 0
        for plan in self.store.plans():
            if plan.get("role") != "organizer" or plan["status"] != "proposed" or not self._nudge_due(plan):
                continue
            with self.store.transaction():
                fresh = self._plan(plan["id"])
                waiting = [aid for aid, p in fresh["participants"].items() if p["status"] == "invited" and not p.get("nudged")]
                for aid in waiting:
                    fresh["participants"][aid]["nudged"] = True
                    c = self.store.contact(aid)
                    if c:
                        self._send(c, "plan.nudge", {"plan_id": fresh["id"], "rev": fresh["rev"], "deadline": fresh.get("deadline")}, kick=False)
                        sent += 1
                if waiting:
                    self.store.save_plan(fresh)
        if sent:
            self.kick()
        return sent

    def tick(self) -> None:
        """Periodic work: relay pickup, outbox retries, reminders, plan deadlines."""
        self._refresh_identity()
        self.poll_relay()
        self.flush()
        self.send_reminders()
        for plan in self.store.plans():
            if plan.get("role") == "organizer" and plan["status"] == "proposed" and plan.get("deadline") and self.now() > plan["deadline"]:
                with self.store.transaction():
                    fresh = self._plan(plan["id"])
                    if fresh["status"] == "proposed":
                        self._finalize_if_ready(fresh)
                self.kick()

    # ------------------------------------------------------ files & notes
    def send_file(self, to: str, path: Path, note: str = "") -> None:
        path = Path(path).expanduser()
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise NodeError(f"files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB")
        self._send(self._active_contact(to), "file.send", {
            "name": path.name[:200],
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
            "note": note[:1000],
        })

    def send_note(self, to: str, text: str, plan_id: str = "", *, reply_to: str = "", expects_reply: bool = False) -> str:
        """Send a short message to a contact's agent. Returns its ``msg_id``; a reply
        arrives as a ``note`` inbox item whose payload has ``reply_to == msg_id``."""
        if not text.strip():
            raise NodeError("empty note")
        msg_id = secrets.token_hex(8)
        self._send(self._active_contact(to), "note", {
            "msg_id": msg_id, "text": text[:MAX_NOTE_CHARS], "plan_id": plan_id,
            "reply_to": reply_to[:32], "expects_reply": bool(expects_reply),
        })
        return msg_id

    def replies(self, msg_id: str) -> list[dict]:
        """Answers received to a note this node sent."""
        return [i for i in self.inbox(include_done=True) if i["kind"] == "note" and i["payload"].get("reply_to") == msg_id]

    # --------------------------------------------------------------- inbox
    def inbox(self, include_done: bool = False) -> list[dict]:
        return [asdict(i) for i in self.store.inbox(include_done)]

    def dismiss(self, item_id: int) -> bool:
        return self.store.close_inbox(item_id) > 0

    def _inbox(self, kind: str, summary: str, *, ref: str = "", contact: str = "", payload: dict | None = None, actionable: bool = False) -> int:
        item_id = self.store.add_inbox(kind, summary, ref=ref, contact=contact, payload=payload, actionable=actionable)
        self._notify({"event": kind, "item_id": item_id, "summary": summary, "ref": ref, "actionable": actionable})
        return item_id

    def _notify(self, event: dict) -> None:
        hook, cmd = self.config.get("notify_webhook"), self.config.get("notify_cmd")
        if not hook and not cmd:
            return

        def run() -> None:
            try:
                if hook:
                    req = urllib.request.Request(hook, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"}, method="POST")
                    urllib.request.urlopen(req, timeout=10).close()  # noqa: S310 (owner-configured)
                if cmd:
                    subprocess.run(shlex.split(cmd), input=json.dumps(event).encode(), timeout=30, check=False)  # noqa: S603
            except Exception as exc:  # notification failure must never break protocol handling
                log.warning("notify failed: %s", exc)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ sending
    def _send(self, contact: Contact, type_: str, body: dict, *, kick: bool = True) -> None:
        env = seal(self.identity, contact.agent_id, type_, body, now=self.now())
        self.store.enqueue(contact.agent_id, env, self.now())
        if kick:
            self.kick()

    def _deliver(self, to_id: str, env: dict) -> None:
        c = self.store.contact(to_id)
        if not c:
            raise DeliveryError("recipient is no longer a contact", permanent=True)
        errors: list[str] = []
        for kind, target in (("direct", c.endpoint), ("relay", c.relay)):
            if not target:
                continue
            try:
                if kind == "direct":
                    self.transport.send(target, env)
                else:
                    self.transport.relay_send(target, env)
                return
            except DeliveryError as exc:
                if exc.permanent and kind == "direct":
                    raise  # the peer itself refused; a relay won't change its mind
                errors.append(str(exc))
        raise DeliveryError("; ".join(errors) or "contact has no endpoint or relay")

    def flush(self) -> int:
        """Try every due outbox message once. Returns how many were delivered."""
        if not self._flush_lock.acquire(blocking=False):
            return 0
        sent = 0
        blocked: set[str] = set()  # keep per-recipient order: never overtake an earlier message
        try:
            for out_id, to_id, env, attempts, next_at in list(self.store.queue()):
                if to_id in blocked:
                    continue
                if next_at > self.now():
                    blocked.add(to_id)
                    continue
                try:
                    self._deliver(to_id, env)
                    self.store.delivered(out_id)
                    sent += 1
                except DeliveryError as exc:
                    c = self.store.contact(to_id)
                    who = c.name if c else to_id[:8]
                    expired = self.now() - env["ts"] > MAX_AGE_SECONDS - 3600
                    if exc.permanent or expired:
                        self.store.delivered(out_id)
                        self._inbox("delivery.failed", f"⚠️ Couldn't deliver a message to {who}: {exc}", contact=to_id)
                    else:
                        delay = min(3600, 15 * 2 ** min(attempts, 8))
                        self.store.retry_later(out_id, attempts + 1, self.now() + delay, str(exc))
                        blocked.add(to_id)
        finally:
            self._flush_lock.release()
        return sent

    def poll_relay(self) -> int:
        relay = self.config.get("relay")
        if not relay:
            return 0
        n = self._poll_relay_as(relay, self.identity)
        if self.old_identity:
            n += self._poll_relay_as(relay, self.old_identity)
        return n

    def _poll_relay_as(self, relay: str, ident: Identity) -> int:
        try:
            envs = self.transport.relay_fetch(relay, ident)
        except DeliveryError as exc:
            log.warning("relay poll failed: %s", exc)
            return 0
        done: list[str] = []
        for env in envs:
            try:
                self.receive(env)
                done.append(env.get("id", ""))
            except (Rejected, EnvelopeError) as exc:
                log.info("dropped relayed envelope: %s", exc)
                done.append(env.get("id", ""))
            except Exception:  # transient: leave it in the mailbox for next poll
                log.exception("error handling relayed envelope")
        self.transport.relay_ack(relay, ident, [d for d in done if d])
        return len(done)

    # ----------------------------------------------------------- receiving
    def receive(self, env: dict) -> dict:
        """Authenticate, de-duplicate, authorize and dispatch one envelope."""
        self._refresh_identity()
        ident = self.identity
        if self.old_identity and isinstance(env, dict) and env.get("to") == self.old_identity.agent_id:
            ident = self.old_identity
        opened = open_(ident, env, now=self.now())
        if not self.store.mark_seen(opened.id, SEEN_TTL_SECONDS, self.now()):
            return {"ok": True, "duplicate": True}
        try:
            contact = self.store.contact(opened.sender)
            if opened.type == "pair.request":
                self._on_pair_request(opened.sender, contact, opened.body)
            elif opened.type == "pair.intro" and (contact is None or contact.status == "pending"):
                self._on_pair_intro(opened.sender, contact, opened.body)
            elif contact is None:
                raise Rejected("unknown sender — pair first")
            elif contact.status == "pending" and opened.type != "pair.accept":
                raise Rejected("pairing not complete")
            else:
                handler = self._handlers().get(opened.type)
                if handler is None:
                    raise Rejected(f"unsupported message type {opened.type!r}")
                handler(contact, opened.body)
        except (Rejected, P.PlanError, ValueError, KeyError, TypeError) as exc:
            # malformed or unauthorized: permanent — keep it marked as seen
            raise Rejected(str(exc) or exc.__class__.__name__) from exc
        except Exception:
            # Transient local failure (disk, calendar fetch...): forget the id so the
            # sender's retry of this same envelope is processed — at-least-once
            # delivery. The envelope never took effect, so this is not a replay.
            self.store._x("DELETE FROM seen WHERE env_id=?", (opened.id,))
            raise
        return {"ok": True}

    def _handlers(self) -> dict[str, Callable[[Contact, dict], None]]:
        return {
            "pair.accept": self._on_pair_accept,
            "plan.propose": self._on_plan_propose,
            "plan.respond": self._on_plan_respond,
            "plan.final": self._on_plan_final,
            "plan.cancel": self._on_plan_cancel,
            "plan.nudge": self._on_plan_nudge,
            "intro.offer": self._on_intro_offer,
            "pair.intro": lambda c, b: None,  # already connected: nothing to do
            "key.rotate": self._on_key_rotate,
            "file.send": self._on_file,
            "note": self._on_note,
        }

    def _on_pair_request(self, sender: str, existing: Contact | None, body: dict) -> None:
        secret = b64d(str(body.get("secret", "")))
        invite = self.store.consume_invite(hashlib.sha256(secret).hexdigest(), self.now())
        if invite is None:
            raise Rejected("invite is invalid, expired or already used")
        invite_name, grants = invite
        contact = Contact(
            agent_id=sender,
            name=self._unique_name(invite_name, sender),
            endpoint=_clean_url(body.get("endpoint")),
            relay=_clean_url(body.get("relay")),
            grants=grants,
            status="active",
            created_at=existing.created_at if existing else self.now(),
        )
        self.store.upsert_contact(contact)
        self._send(contact, "pair.accept", {"name": self.name, "endpoint": self.config.get("endpoint", ""), "relay": self.config.get("relay", "")})
        self._inbox("pair", f"🤝 Paired with {contact.name} (their agent calls itself {str(body.get('name', ''))[:60]!r}; "
                    f"fingerprint {fingerprint(sender)}). Grants: {', '.join(grants) or 'none'}.", contact=sender)

    def _on_pair_accept(self, contact: Contact, body: dict) -> None:
        if contact.status == "active":
            return
        contact.status = "active"
        contact.endpoint = _clean_url(body.get("endpoint")) or contact.endpoint
        contact.relay = _clean_url(body.get("relay")) or contact.relay
        self.store.upsert_contact(contact)
        self._inbox("pair", f"🤝 {contact.name} accepted — your agents are connected (fingerprint {fingerprint(contact.agent_id)}).", contact=contact.agent_id)

    def _on_plan_propose(self, contact: Contact, body: dict) -> None:
        if not contact.can("plans"):
            raise Rejected(f"{self.name} hasn't allowed plans from you")
        wire = P.validate_wire_plan(body.get("plan"), organizer=contact.agent_id)
        if self.identity.agent_id not in wire["participants"]:
            raise Rejected("you are not a participant of this plan")
        with self.store.transaction():
            existing = self.store.plan(wire["id"])
            if existing and (existing.get("organizer") != contact.agent_id or existing["id"] != wire["id"]):
                raise Rejected("plan id collision")
            if existing and existing["rev"] >= wire["rev"]:
                return
            slots = P.slots_of(wire)
            suggested = self.availability(exclude_plan=wire["id"]).free_indices(slots, wire["rrule"], wire["tz"])
            plan = {**wire, "role": "participant", "my_status": "invited", "my_ok_slots": [], "suggested": suggested,
                    "created_at": existing["created_at"] if existing else self.now(), "updated_at": self.now()}
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"])
            auto = contact.can("autoconfirm") and bool(suggested)
            if auto:  # same transaction, so a newer revision can't slip in between
                self.respond(plan["id"], "accept", slots=suggested, note="auto-confirmed by agent", _kick=False)
        options = ", ".join(f"{i + 1}) {self._fmt_slot(plan['slots'][i])}" for i in range(len(plan["slots"])))
        if auto:
            self.kick()
            self._inbox("plan.auto", f"📅 {contact.name}'s agent proposed {plan['title']!r}; auto-accepted the times you're free "
                        f"({', '.join(str(i + 1) for i in suggested)}). Options: {options}", ref=plan["id"], contact=contact.agent_id)
            return
        free = ", ".join(str(i + 1) for i in suggested) or "none"
        where = f" at {plan['location']}" if plan.get("location") else ""
        self._inbox("plan.invite", f"📅 {contact.name} wants to plan {plan['title']!r}{where}. Options: {options}. "
                    f"Your calendar is free for: {free}. Reply: confer plan respond {plan['id']} accept|decline|counter",
                    ref=plan["id"], contact=contact.agent_id, actionable=True, payload={"suggested": suggested})

    def _on_plan_respond(self, contact: Contact, body: dict) -> None:
        with self.store.transaction():
            plan = self.store.plan(str(body.get("plan_id", "")))
            if not plan or plan.get("role") != "organizer" or plan["id"] != body.get("plan_id"):
                raise Rejected("no such plan")
            if contact.agent_id not in plan["participants"]:
                raise Rejected("not a participant of this plan")
            if body.get("rev") != plan["rev"] or plan["status"] == "cancelled":
                return  # answer to an older revision — ignore
            decision = body.get("decision")
            counter = body.get("counter") or []
            if plan["status"] == "confirmed":
                if decision in ("decline", "counter"):
                    P.record_response(plan, contact.agent_id, "decline", [], note=str(body.get("note", "")))
                    self.store.save_plan(plan)
                    self._inbox("plan.dropout", f"⚠️ {contact.name} can no longer make {plan['title']!r}.", ref=plan["id"], contact=contact.agent_id, actionable=True)
                return
            P.record_response(plan, contact.agent_id, decision, list(body.get("ok_slots") or []), str(body.get("note", "")), counter,
                              prefer=list(body.get("prefer") or []))
            self.store.save_plan(plan)
            if decision == "counter":
                sugg = "; ".join(self._fmt_slot(c) for c in plan["participants"][contact.agent_id]["counter"])
                self._inbox("plan.counter", f"↩️ {contact.name} suggests other times for {plan['title']!r}: {sugg}", ref=plan["id"], contact=contact.agent_id)
            self._finalize_if_ready(plan)
        self.kick()

    def _on_plan_final(self, contact: Contact, body: dict) -> None:
        wire = P.validate_wire_plan(body.get("plan"), organizer=contact.agent_id)
        if wire["status"] != "confirmed" or wire["chosen"] is None:
            raise Rejected("final plan must be confirmed with a chosen slot")
        with self.store.transaction():
            plan = self.store.plan(wire["id"])
            if not plan or plan.get("organizer") != contact.agent_id or plan["id"] != wire["id"]:
                raise Rejected("unknown plan")
            if wire["rev"] != plan["rev"]:
                return
            plan.update(status="confirmed", chosen=wire["chosen"], updated_at=self.now())
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"], kind="plan.invite")
        when = self._fmt_slot(plan["slots"][plan["chosen"]])
        if self._attending(plan):
            self._inbox("plan.confirmed", f"✅ {plan['title']!r} with {contact.name} is confirmed for {when}.", ref=plan["id"], contact=contact.agent_id)
        else:
            self._inbox("plan.confirmed", f"ℹ️ {plan['title']!r} went ahead for {when} (not a time you picked). "
                        f"Join anyway: confer plan respond {plan['id']} accept --slots {plan['chosen'] + 1}", ref=plan["id"], contact=contact.agent_id)
        self._write_calendar()

    def _on_plan_cancel(self, contact: Contact, body: dict) -> None:
        with self.store.transaction():
            plan = self.store.plan(str(body.get("plan_id", "")))
            if not plan or plan.get("organizer") != contact.agent_id or plan["id"] != body.get("plan_id"):
                raise Rejected("unknown plan")
            plan.update(status="cancelled", updated_at=self.now())
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"])
        reason = f" ({str(body.get('reason'))[:200]})" if body.get("reason") else ""
        self._inbox("plan.cancelled", f"❌ {contact.name} cancelled {plan['title']!r}{reason}.", ref=plan["id"], contact=contact.agent_id)
        self._write_calendar()

    def _on_intro_offer(self, contact: Contact, body: dict) -> None:
        if not contact.can("intros"):
            raise Rejected(f"{self.name} hasn't allowed introductions from you")
        peer = body.get("peer") or {}
        peer_id, intro_id = str(peer.get("id", "")), str(body.get("intro_id", ""))
        fingerprint(peer_id)  # validates the key
        if not (8 <= len(intro_id) <= 64 and intro_id.isalnum()):
            raise Rejected("bad introduction id")
        if peer_id in (self.identity.agent_id, contact.agent_id):
            raise Rejected("bad introduction")
        existing = self.store.contact(peer_id)
        if existing and existing.status == "active":
            return  # already connected
        prior = self.store.intro(intro_id)
        if prior and (prior["peer_id"] != peer_id or prior["introducer"] != contact.agent_id):
            raise Rejected("introduction id collision")
        if prior:
            return
        pending = [i for i in self.store.intros() if i["introducer"] == contact.agent_id and i["status"] == "offered" and i["expires_at"] > self.now()]
        if len(pending) >= MAX_PENDING_INTROS:
            raise Rejected("too many pending introductions from you")
        name = str(peer.get("name", ""))[:60] or "contact"
        self.store.save_intro({
            "intro_id": intro_id, "peer_id": peer_id, "peer_name": name,
            "endpoint": _clean_url(peer.get("endpoint")), "relay": _clean_url(peer.get("relay")),
            "introducer": contact.agent_id, "note": str(body.get("note", ""))[:500], "status": "offered",
            "peer_ready": 0, "expires_at": self.now() + INTRO_TTL_SECONDS, "created_at": self.now(),
        })
        note = f' — "{str(body.get("note"))[:200]}"' if body.get("note") else ""
        self._inbox("intro", f"👋 {contact.name} wants to introduce you to {name} (fingerprint {fingerprint(peer_id)}){note}. "
                    f"confer intro accept {intro_id}  (or decline)", ref=intro_id, contact=contact.agent_id, actionable=True)

    def _on_pair_intro(self, sender: str, existing: Contact | None, body: dict) -> None:
        intro = self.store.intro(str(body.get("intro_id", "")))
        if not intro or intro["peer_id"] != sender or intro["expires_at"] < self.now():
            raise Rejected("no matching introduction")
        if intro["status"] == "declined":
            raise Rejected("introduction declined")
        if intro["status"] == "offered":  # my owner hasn't decided yet; remember the peer is willing
            intro["peer_ready"] = 1
            self.store.save_intro(intro)
            return
        if existing is None:
            raise Rejected("no matching introduction")
        existing.status = "active"
        existing.endpoint = _clean_url(body.get("endpoint")) or existing.endpoint
        existing.relay = _clean_url(body.get("relay")) or existing.relay
        self.store.upsert_contact(existing)
        intro["status"] = "done"
        self.store.save_intro(intro)
        self._send(existing, "pair.accept", {"name": self.name, "endpoint": self.config.get("endpoint", ""), "relay": self.config.get("relay", "")})
        self._connected_via_intro(existing, intro)

    def _on_key_rotate(self, contact: Contact, body: dict) -> None:
        new_id, ts = str(body.get("new_id", "")), body.get("ts")
        fingerprint(new_id)
        if not isinstance(ts, int) or abs(self.now() - ts) > MAX_AGE_SECONDS:
            raise Rejected("stale key rotation")
        binding = canonical({"old": contact.agent_id, "new": new_id, "ts": ts})
        if not verify(new_id, binding, b64d(str(body.get("proof", "")))):
            raise Rejected("new key did not sign the rotation")
        if self.store.contact(new_id):
            raise Rejected("that key already belongs to a contact")
        if new_id in (self.identity.agent_id, self.old_identity.agent_id if self.old_identity else None):
            raise Rejected("rotation target collides with this node's own key")
        self.store.rename_agent(contact.agent_id, new_id)
        self._inbox("key", f"🔑 {contact.name}'s agent moved to a new key (fingerprint {fingerprint(new_id)}). "
                    "This is normal after a key rotation; if they didn't do it, remove them.", contact=new_id)

    def _on_plan_nudge(self, contact: Contact, body: dict) -> None:
        plan = self.store.plan(str(body.get("plan_id", "")))
        if not plan or plan.get("organizer") != contact.agent_id:
            raise Rejected("unknown plan")
        if body.get("rev") != plan["rev"] or plan["status"] != "proposed" or plan.get("my_status") != "invited":
            return  # already answered or superseded
        if any(i["kind"] == "plan.reminder" and i["ref"] == plan["id"] for i in self.inbox()):
            return  # one open reminder per plan, however often the organizer nudges
        due = ""
        if isinstance(body.get("deadline"), (int, float)):
            from zoneinfo import ZoneInfo

            due = " before " + datetime.fromtimestamp(body["deadline"], ZoneInfo(self.tz)).strftime("%a %H:%M")
        self._inbox("plan.reminder", f"⏰ {contact.name} is waiting on your answer for {plan['title']!r}{due}. "
                    f"confer plan respond {plan['id']} accept|decline", ref=plan["id"], contact=contact.agent_id, actionable=True)

    def _on_file(self, contact: Contact, body: dict) -> None:
        if not contact.can("files"):
            raise Rejected(f"{self.name} hasn't allowed files from you")
        data = base64.b64decode(str(body.get("data", "")), validate=True)
        if len(data) > MAX_FILE_BYTES:
            raise Rejected("file too large")
        if hashlib.sha256(data).hexdigest() != body.get("sha256"):
            raise Rejected("file checksum mismatch")
        folder = self.home / "files" / _safe_name(contact.name)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime(self.now()))}-{_safe_name(str(body.get('name', 'file')))}"
        dest.write_bytes(data)
        note = f' — "{str(body.get("note"))[:200]}"' if body.get("note") else ""
        self._inbox("file", f"📎 {contact.name} sent {dest.name} ({len(data):,} bytes){note}. Saved to {dest}", contact=contact.agent_id,
                    payload={"path": str(dest), "sha256": body.get("sha256")})

    def _on_note(self, contact: Contact, body: dict) -> None:
        if not contact.can("notes"):
            raise Rejected(f"{self.name} hasn't allowed notes from you")
        text = str(body.get("text", ""))[:MAX_NOTE_CHARS]
        msg_id, reply_to = str(body.get("msg_id", ""))[:32], str(body.get("reply_to", ""))[:32]
        asks = bool(body.get("expects_reply"))
        verb = "replied" if reply_to else ("asks" if asks else "says")
        hint = f" (reply: confer note {contact.name!r} \"...\" --reply-to {msg_id})" if asks and msg_id else ""
        self._inbox("note", f"💬 {contact.name} {verb}: {text}{hint}", ref=str(body.get("plan_id", ""))[:64], contact=contact.agent_id,
                    actionable=asks, payload={"text": text, "msg_id": msg_id, "reply_to": reply_to, "expects_reply": asks})

    # ------------------------------------------------------------ calendar
    def _fmt_slot(self, slot: dict) -> str:
        from zoneinfo import ZoneInfo

        iv = Interval.from_wire(slot)
        zone = ZoneInfo(self.tz)
        a, b = iv.start.astimezone(zone), iv.end.astimezone(zone)
        return f"{a:%a %b %d %H:%M}-{b:%H:%M} {a.tzname()}"

    def calendar_ics(self) -> str:
        """Confirmed plans I'm attending, as an iCalendar feed."""
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Confer//EN", "CALSCALE:GREGORIAN"]
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now()))
        for plan in self.store.plans():
            if plan["status"] != "confirmed" or plan.get("chosen") is None or not self._attending(plan):
                continue
            iv = Interval.from_wire(plan["slots"][plan["chosen"]])
            who = ", ".join(p["name"] for p in plan["participants"].values())
            lines += [
                "BEGIN:VEVENT",
                f"UID:{plan['id']}@confer",
                f"DTSTAMP:{stamp}",
                *_ics_times(iv, plan.get("tz", "UTC") if plan.get("rrule") else "UTC"),
                f"SUMMARY:{_ics_text(plan['title'])}",
                f"DESCRIPTION:{_ics_text('With ' + plan.get('organizer_name', '') + ', ' + who + ('. ' + plan['notes'] if plan.get('notes') else ''))}",
            ]
            if plan.get("location"):
                lines.append(f"LOCATION:{_ics_text(plan['location'])}")
            if plan.get("rrule"):
                lines.append(f"RRULE:{plan['rrule'].removeprefix('RRULE:')}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"

    def _write_calendar(self) -> None:
        (self.home / "calendar.ics").write_text(self.calendar_ics())


def check_url(value: str) -> str:
    """Validate an http(s) base URL given by the owner; returns it without a trailing slash."""
    from urllib.parse import urlparse

    v = value.strip().rstrip("/")
    try:
        u = urlparse(v)
        u.port  # raises on a malformed port
    except ValueError as exc:
        raise NodeError(f"bad URL {value!r}: {exc}") from exc
    if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or u.netloc.endswith(":"):
        raise NodeError(f"bad URL {value!r}: need http(s)://host[:port][/path]")
    return v


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name).strip(" .") or "file"
    return base[:120]


def _clean_url(value: Any) -> str:
    v = str(value or "").strip().rstrip("/")
    return v if v.startswith(("http://", "https://")) and len(v) < 500 else ""


def _ics_times(iv: Interval, tz: str) -> list[str]:
    if tz == "UTC":
        return [f"DTSTART:{iv.start:%Y%m%dT%H%M%SZ}", f"DTEND:{iv.end:%Y%m%dT%H%M%SZ}"]
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tz)  # recurring: wall-clock time in the organizer's zone survives DST
    return [f"DTSTART;TZID={tz}:{iv.start.astimezone(z):%Y%m%dT%H%M%S}", f"DTEND;TZID={tz}:{iv.end.astimezone(z):%Y%m%dT%H%M%S}"]


def _ics_fold(line: str) -> str:
    """RFC 5545 §3.1: content lines are folded at 75 octets, never inside a UTF-8 sequence."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, i = [], 0
    while i < len(raw):
        end = i + (75 if i == 0 else 74)  # continuation lines start with one space
        while end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        parts.append(raw[i:end].decode("utf-8"))
        i = end
    return "\r\n ".join(parts)


def _ics_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")
