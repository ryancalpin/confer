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

from dateutil.rrule import rrulestr

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
    spread,
)
from .envelope import SEEN_TTL_SECONDS, EnvelopeError, MAX_AGE_SECONDS, open_, seal
from .identity import Identity, b64d, b64e, fingerprint
from .store import Contact, Store
from .transport import DeliveryError, HttpTransport

log = logging.getLogger("confer")

GRANTS = ("plans", "autoconfirm", "files", "notes")
DEFAULT_GRANTS = ["plans", "files", "notes"]
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


class ConfirmedPlansProvider:
    """My own confirmed plans count as busy time for future scheduling."""

    def __init__(self, node: "Node"):
        self.node = node

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        out: list[Interval] = []
        window = Interval(start, end)
        for plan in self.node.store.plans():
            if plan["status"] != "confirmed" or plan.get("chosen") is None or not self.node._attending(plan):
                continue
            slot = Interval.from_wire(plan["slots"][plan["chosen"]])
            if plan.get("rrule"):
                span = slot.end - slot.start
                for s in rrulestr(plan["rrule"], dtstart=slot.start).between(start - span, end, inc=True):
                    iv = Interval(s, s + span)
                    if iv.overlaps(window):
                        out.append(iv)
            elif slot.overlaps(window):
                out.append(slot)
        return out


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
        self.identity = Identity.load(self.home / "identity.key")
        self.store = Store(self.home / "state.db")
        self.transport = transport or HttpTransport()
        self.clock = clock
        self._provider = provider
        self._flush_lock = threading.Lock()
        # the server replaces this with a non-blocking wake-up of its flusher thread
        self.kick: Callable[[], None] = self.flush

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

    def availability(self) -> Availability:
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
            CompositeProvider(base, ConfirmedPlansProvider(self)),
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
        if rrule:
            try:
                rrulestr(rrule, dtstart=datetime(2020, 1, 1))
            except (ValueError, TypeError) as exc:
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
        )
        plan["role"] = "organizer"
        self.store.save_plan(plan)
        for c in people:
            self._send(c, "plan.propose", {"plan": P.wire_view(plan)}, kick=False)
        self.kick()
        return plan

    def _plan(self, plan_id: str) -> dict:
        plan = self.store.plan(plan_id)
        if not plan:
            raise NodeError(f"no plan {plan_id!r}")
        return plan

    def plans(self) -> list[dict]:
        return self.store.plans()

    def _attending(self, plan: dict) -> bool:
        if plan.get("role") == "organizer":
            return True
        return plan.get("my_status") == "accepted" and plan.get("chosen") in plan.get("my_ok_slots", [])

    def respond(self, plan_id: str, decision: str, *, slots: list[int] | None = None, note: str = "", counter: list[Interval] | None = None) -> dict:
        """Participant answers a proposal. ``slots`` are 0-based option indexes."""
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
            if decision == "counter" and not counter:
                raise NodeError("a counter needs at least one suggested time")
            plan.update(my_status={"accept": "accepted", "decline": "declined", "counter": "countered"}[decision], my_ok_slots=ok)
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"], kind="plan.invite")
        organizer = self.store.contact(plan["organizer"])
        if not organizer:
            raise NodeError("the organizer is no longer a contact")
        self._send(organizer, "plan.respond", {
            "plan_id": plan["id"],
            "rev": plan["rev"],
            "decision": decision,
            "ok_slots": ok,
            "note": note[:1000],
            "counter": [c.to_wire() for c in counter or []],
        })
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
                slots = spread(self.availability().candidates(start, end, dur, between=between, rrule=plan.get("rrule")), candidates)
                if not slots:
                    raise NodeError("you have no free time matching that window")
            P.revise(plan, slots)
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

    def _broadcast(self, plan: dict, type_: str, body: dict) -> None:
        for aid in plan["participants"]:
            c = self.store.contact(aid)
            if c:
                self._send(c, type_, body, kick=False)
        self.kick()

    def _finalize_if_ready(self, plan: dict) -> None:
        """Organizer: tally, and announce the outcome if it changed. Caller holds the tx lock."""
        status, chosen = P.tally(plan, self.now())
        if status == plan["status"]:
            return
        plan["status"], plan["chosen"] = status, chosen
        plan["updated_at"] = self.now()
        self.store.save_plan(plan)
        if status == "confirmed":
            when = self._fmt_slot(plan["slots"][chosen])
            self._inbox("plan.confirmed", f"✅ {plan['title']} is set for {when}.", ref=plan["id"])
            self._broadcast(plan, "plan.final", {"plan": P.wire_view(plan)})
            self._write_calendar()
        elif status == "needs_reschedule":
            counters = [c for p in plan["participants"].values() for c in p.get("counter", [])]
            hint = ""
            if counters:
                hint = " Suggestions: " + "; ".join(self._fmt_slot(c) for c in counters[:5]) + "."
            self._inbox("plan.reschedule", f"No time works for everyone for {plan['title']}.{hint} "
                        f"Revise it: confer plan revise {plan['id']}", ref=plan["id"], actionable=True,
                        payload={"counters": counters})

    def tick(self) -> None:
        """Periodic work: relay pickup, outbox retries, plan deadlines."""
        self.poll_relay()
        self.flush()
        for plan in self.store.plans():
            if plan.get("role") == "organizer" and plan["status"] == "proposed" and plan.get("deadline") and self.now() > plan["deadline"]:
                with self.store.transaction():
                    fresh = self._plan(plan["id"])
                    if fresh["status"] == "proposed":
                        self._finalize_if_ready(fresh)

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

    def send_note(self, to: str, text: str, plan_id: str = "") -> None:
        if not text.strip():
            raise NodeError("empty note")
        self._send(self._active_contact(to), "note", {"text": text[:MAX_NOTE_CHARS], "plan_id": plan_id})

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
        try:
            for out_id, to_id, env, attempts in list(self.store.due(self.now())):
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
        finally:
            self._flush_lock.release()
        return sent

    def poll_relay(self) -> int:
        relay = self.config.get("relay")
        if not relay:
            return 0
        try:
            envs = self.transport.relay_fetch(relay, self.identity)
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
        self.transport.relay_ack(relay, self.identity, [d for d in done if d])
        return len(done)

    # ----------------------------------------------------------- receiving
    def receive(self, env: dict) -> dict:
        """Authenticate, de-duplicate, authorize and dispatch one envelope."""
        opened = open_(self.identity, env, now=self.now())
        if not self.store.mark_seen(opened.id, SEEN_TTL_SECONDS, self.now()):
            return {"ok": True, "duplicate": True}
        try:
            contact = self.store.contact(opened.sender)
            if opened.type == "pair.request":
                self._on_pair_request(opened.sender, contact, opened.body)
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
            self.store._x("DELETE FROM seen WHERE env_id=?", (opened.id,))  # let a retry through
            raise
        return {"ok": True}

    def _handlers(self) -> dict[str, Callable[[Contact, dict], None]]:
        return {
            "pair.accept": self._on_pair_accept,
            "plan.propose": self._on_plan_propose,
            "plan.respond": self._on_plan_respond,
            "plan.final": self._on_plan_final,
            "plan.cancel": self._on_plan_cancel,
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
            suggested = self.availability().free_indices(slots, wire["rrule"])
            plan = {**wire, "role": "participant", "my_status": "invited", "my_ok_slots": [], "suggested": suggested,
                    "created_at": existing["created_at"] if existing else self.now(), "updated_at": self.now()}
            self.store.save_plan(plan)
            self.store.close_inbox(ref=plan["id"])
        options = ", ".join(f"{i + 1}) {self._fmt_slot(plan['slots'][i])}" for i in range(len(plan["slots"])))
        if contact.can("autoconfirm") and suggested:
            self.respond(plan["id"], "accept", slots=suggested, note="auto-confirmed by agent")
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
            P.record_response(plan, contact.agent_id, decision, list(body.get("ok_slots") or []), str(body.get("note", "")), counter)
            self.store.save_plan(plan)
            if decision == "counter":
                sugg = "; ".join(self._fmt_slot(c) for c in plan["participants"][contact.agent_id]["counter"])
                self._inbox("plan.counter", f"↩️ {contact.name} suggests other times for {plan['title']!r}: {sugg}", ref=plan["id"], contact=contact.agent_id)
            self._finalize_if_ready(plan)

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
        self._inbox("note", f"💬 {contact.name}: {text}", ref=str(body.get("plan_id", ""))[:64], contact=contact.agent_id, payload={"text": text})

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
                f"DTSTART:{iv.start:%Y%m%dT%H%M%SZ}",
                f"DTEND:{iv.end:%Y%m%dT%H%M%SZ}",
                f"SUMMARY:{_ics_text(plan['title'])}",
                f"DESCRIPTION:{_ics_text('With ' + plan.get('organizer_name', '') + ', ' + who + ('. ' + plan['notes'] if plan.get('notes') else ''))}",
            ]
            if plan.get("location"):
                lines.append(f"LOCATION:{_ics_text(plan['location'])}")
            if plan.get("rrule"):
                lines.append(f"RRULE:{plan['rrule'].removeprefix('RRULE:')}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"

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


def _ics_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")
