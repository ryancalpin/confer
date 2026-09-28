"""Status, ETA and location sharing — short-lived and opt-in.

``presence.update`` carries a status line ("leaving now"), an ETA in minutes
and/or a coordinate, with an expiry (default 2 h, max 24 h). Receivers keep
only the *latest* update per contact and drop it when it expires, so no
location history ever accumulates. Receiving requires the ``location``
grant, which is off by default.

The node has no GPS: coordinates come from the phone inbox (browser
geolocation, only when you tap "share location") or from your agent/CLI.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Callable

from .store import Contact

if TYPE_CHECKING:
    from .node import Node

DEFAULT_TTL_MIN = 120
MAX_TTL_MIN = 24 * 60
MAX_TEXT = 280


def _coord(value: object, lo: float, hi: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(v, 5) if math.isfinite(v) and lo <= v <= hi else None


def map_link(lat: float, lon: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"


def describe(name: str, p: dict) -> str:
    bits = []
    if p.get("text"):
        bits.append(p["text"])
    if p.get("eta_minutes") is not None:
        bits.append(f"ETA {p['eta_minutes']} min")
    if p.get("lat") is not None and p.get("lon") is not None:
        acc = f" ±{p['accuracy_m']} m" if p.get("accuracy_m") else ""
        bits.append(f"location {map_link(p['lat'], p['lon'])}{acc}")
    if p.get("plan_title"):
        bits.append(f"for {p['plan_title']!r}")
    return f"📍 {name}: " + (" · ".join(bits) or "sharing")


class PresenceMixin:
    """Node methods for status/ETA/location. Mixed into :class:`confer.node.Node`."""

    def share_status(
        self: "Node",
        to: list[str] | None = None,
        *,
        plan_id: str = "",
        text: str = "",
        eta_minutes: int | None = None,
        lat: float | None = None,
        lon: float | None = None,
        accuracy_m: int | None = None,
        ttl_minutes: int = DEFAULT_TTL_MIN,
    ) -> list[str]:
        """Send a status/ETA/location to contacts, or to everyone in a plan
        (who is also your contact). Returns the names it went to."""
        from .node import NodeError

        people: dict[str, Contact] = {}
        for n in to or []:
            c = self._active_contact(n)
            people[c.agent_id] = c
        plan_title = ""
        if plan_id:
            plan = self._plan(plan_id)
            plan_title = plan["title"]
            for aid in [plan["organizer"], *plan["participants"]]:
                c = self.store.contact(aid)
                if c and c.status == "active" and aid != self.identity.agent_id:
                    people[aid] = c
        if not people:
            raise NodeError("share with at least one contact (or a plan whose people are your contacts)")
        la, lo = _coord(lat, -90, 90), _coord(lon, -180, 180)
        if (lat is not None or lon is not None) and (la is None or lo is None):
            raise NodeError("latitude must be -90..90 and longitude -180..180")
        if eta_minutes is not None and not 0 <= int(eta_minutes) <= 24 * 60:
            raise NodeError("ETA must be 0..1440 minutes")
        if not (text.strip() or eta_minutes is not None or la is not None):
            raise NodeError("share a status, an ETA or a location")
        ttl = max(1, min(int(ttl_minutes), MAX_TTL_MIN))
        body = {
            "text": text.strip()[:MAX_TEXT], "eta_minutes": int(eta_minutes) if eta_minutes is not None else None,
            "lat": la, "lon": lo, "accuracy_m": int(accuracy_m) if accuracy_m else None,
            "expires_at": int(self.now() + ttl * 60), "plan_id": plan_id[:64], "plan_title": plan_title[:120],
        }
        for c in people.values():
            self._send(c, "presence.update", body, kick=False)
        self.kick()
        return [c.name for c in people.values()]

    def stop_sharing(self: "Node", to: list[str]) -> None:
        for n in to:
            self._send(self._active_contact(n), "presence.clear", {}, kick=False)
        self.kick()

    def presence(self: "Node") -> list[dict]:
        live = self.store.presence(self.now())  # also drops expired rows
        for item in self.store.inbox():  # expired shares shouldn't linger in the inbox either
            if item.kind == "presence" and item.contact not in live:
                self.store.close_inbox(item.id)
        out = []
        for aid, p in live.items():
            c = self.store.contact(aid)
            name = c.name if c else aid[:8]
            out.append({"contact": name, **p, "summary": describe(name, p)})
        return out

    # ------------------------------------------------------------ handlers
    def _presence_handlers(self: "Node") -> dict[str, Callable[[Contact, dict], None]]:
        return {"presence.update": self._on_presence, "presence.clear": self._on_presence_clear}

    def _on_presence(self: "Node", contact: Contact, body: dict) -> None:
        from .node import Rejected

        if not contact.can("location"):
            raise Rejected(f"{self.name} isn't receiving status/location from you")
        exp = body.get("expires_at")
        if not isinstance(exp, (int, float)) or exp <= self.now():
            return  # stale by the time it arrived
        exp = min(float(exp), self.now() + MAX_TTL_MIN * 60)
        eta = body.get("eta_minutes")
        p = {
            "text": str(body.get("text", ""))[:MAX_TEXT],
            "eta_minutes": eta if isinstance(eta, int) and not isinstance(eta, bool) and 0 <= eta <= 1440 else None,
            "lat": _coord(body.get("lat"), -90, 90), "lon": _coord(body.get("lon"), -180, 180),
            "accuracy_m": body.get("accuracy_m") if isinstance(body.get("accuracy_m"), int) and 0 < body["accuracy_m"] < 100000 else None,
            "plan_id": str(body.get("plan_id", ""))[:64], "plan_title": str(body.get("plan_title", ""))[:120],
            "updated_at": self.now(), "expires_at": exp,
        }
        if p["lat"] is None or p["lon"] is None:
            p["lat"] = p["lon"] = None
        self.store.set_presence(contact.agent_id, p, exp)
        # one live item per person: replace their previous update in the inbox
        self.store.close_inbox(ref="presence:" + contact.agent_id)
        self._inbox("presence", describe(contact.name, p), ref="presence:" + contact.agent_id, contact=contact.agent_id)

    def _on_presence_clear(self: "Node", contact: Contact, body: dict) -> None:
        self.store.clear_presence(contact.agent_id)
        self.store.close_inbox(ref="presence:" + contact.agent_id)
