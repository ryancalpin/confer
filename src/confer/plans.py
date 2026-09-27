"""Plan model and the organizer's tally — pure functions, no I/O.

Topology is hub-and-spoke: the organizer's node owns the plan and is the only
node that may propose, revise, finalize or cancel it. Participants answer with
the subset of candidate slots that work for them. Participants never need to
be contacts of each other.

Plan status: proposed -> confirmed | needs_reschedule | cancelled
Participant status: invited -> accepted | declined | countered
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .availability import Interval, normalize_rrule

MAX_SLOTS = 20
MAX_PARTICIPANTS = 200

PLAN_STATUSES = {"proposed", "confirmed", "needs_reschedule", "cancelled"}
DECISIONS = {"accept", "decline", "counter"}


class PlanError(ValueError):
    pass


def new_plan(
    *,
    title: str,
    organizer: str,
    organizer_name: str,
    participants: dict[str, str],
    slots: list[Interval],
    rrule: str | None = None,
    location: str = "",
    notes: str = "",
    quorum: str | int = "all",
    deadline: float | None = None,
    tz: str = "UTC",
) -> dict:
    if not title.strip():
        raise PlanError("plan needs a title")
    if not participants:
        raise PlanError("plan needs at least one participant")
    if len(participants) > MAX_PARTICIPANTS:
        raise PlanError(f"at most {MAX_PARTICIPANTS} participants")
    if not slots:
        raise PlanError("no candidate slots")
    quorum = _quorum(quorum)
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:16],
        "rev": 1,
        "title": title.strip()[:200],
        "organizer": organizer,
        "organizer_name": organizer_name,
        "participants": {
            aid: {"name": name, "status": "invited", "ok_slots": [], "prefer": [], "note": "", "counter": []}
            for aid, name in participants.items()
        },
        "slots": [s.to_wire() for s in slots[:MAX_SLOTS]],
        "rrule": normalize_rrule(rrule),
        "tz": _tz(tz),
        "location": location[:500],
        "notes": notes[:2000],
        "quorum": quorum,
        "deadline": deadline,
        "status": "proposed",
        "chosen": None,
        "created_at": now,
        "updated_at": now,
    }


def wire_view(plan: dict) -> dict:
    """What participants see: everything except other people's answers/notes."""
    keys = ("id", "rev", "title", "organizer", "organizer_name", "slots", "rrule", "tz", "location", "notes", "quorum", "deadline", "status", "chosen")
    out = {k: plan.get(k) for k in keys}
    out["participants"] = {aid: {"name": p["name"]} for aid, p in plan["participants"].items()}
    return out


def validate_wire_plan(data: Any, organizer: str) -> dict:
    """Validate a plan received from ``organizer``'s node before storing it."""
    if not isinstance(data, dict):
        raise PlanError("plan must be an object")
    if data.get("organizer") != organizer:
        raise PlanError("only the organizer may send this plan")
    if not isinstance(data.get("id"), str) or not (1 <= len(data["id"]) <= 64) or not data["id"].isalnum():
        raise PlanError("bad plan id")
    if not isinstance(data.get("rev"), int) or data["rev"] < 1:
        raise PlanError("bad plan revision")
    slots = data.get("slots")
    if not isinstance(slots, list) or not 1 <= len(slots) <= MAX_SLOTS:
        raise PlanError("bad slots")
    parsed = [Interval.from_wire(s) for s in slots]
    for s in parsed:
        if s.end - s.start > timedelta(days=14):
            raise PlanError("slot too long")
    parts = data.get("participants")
    if not isinstance(parts, dict) or not 1 <= len(parts) <= MAX_PARTICIPANTS:
        raise PlanError("bad participants")
    if data.get("status") not in PLAN_STATUSES:
        raise PlanError("bad status")
    rrule = data.get("rrule")
    if rrule is not None and not isinstance(rrule, str):
        raise PlanError("bad rrule")
    try:
        rrule = normalize_rrule(rrule)
    except ValueError as exc:
        raise PlanError(f"bad rrule: {exc}") from exc
    return {
        "id": data["id"],
        "rev": data["rev"],
        "title": str(data.get("title", ""))[:200],
        "organizer": organizer,
        "organizer_name": str(data.get("organizer_name", ""))[:100],
        "participants": {str(a): {"name": str((p or {}).get("name", ""))[:100]} for a, p in parts.items()},
        "slots": [s.to_wire() for s in parsed],
        "rrule": rrule,
        "tz": _tz(data.get("tz", "UTC")),
        "location": str(data.get("location", ""))[:500],
        "notes": str(data.get("notes", ""))[:2000],
        "quorum": _quorum(data.get("quorum", "all")),
        "deadline": data.get("deadline") if isinstance(data.get("deadline"), (int, float)) else None,
        "status": data["status"],
        "chosen": data.get("chosen") if isinstance(data.get("chosen"), int) and 0 <= data["chosen"] < len(parsed) else None,
    }


def _tz(tz: Any) -> str:
    try:
        if not isinstance(tz, str) or len(tz) > 64:
            raise ValueError
        ZoneInfo(tz)
        return tz
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise PlanError(f"bad timezone {tz!r}") from exc


def _quorum(q: Any) -> str | int:
    if q == "all":
        return "all"
    if isinstance(q, int) and not isinstance(q, bool) and q >= 1:
        return q
    raise PlanError("quorum must be 'all' or a positive integer")


def slots_of(plan: dict) -> list[Interval]:
    return [Interval.from_wire(s) for s in plan["slots"]]


def record_response(plan: dict, participant: str, decision: str, ok_slots: list[int], note: str = "",
                    counter: list[dict] | None = None, prefer: list[int] | None = None) -> None:
    if participant not in plan["participants"]:
        raise PlanError("not a participant of this plan")
    if decision not in DECISIONS:
        raise PlanError(f"decision must be one of {sorted(DECISIONS)}")
    n = len(plan["slots"])
    ok = sorted({i for i in ok_slots if isinstance(i, int) and 0 <= i < n})
    if decision == "accept" and not ok:
        raise PlanError("accept needs at least one workable slot")
    p = plan["participants"][participant]
    p["status"] = {"accept": "accepted", "decline": "declined", "counter": "countered"}[decision]
    p["ok_slots"] = ok if decision == "accept" else []
    # preferred options are a subset of workable ones; they only break ties between eligible slots
    p["prefer"] = sorted({i for i in (prefer or []) if i in ok}) if decision == "accept" else []
    p["note"] = str(note)[:1000]
    p["counter"] = [Interval.from_wire(c).to_wire() for c in (counter or [])][:MAX_SLOTS] if decision == "counter" else []
    plan["updated_at"] = time.time()


def _required(plan: dict) -> int:
    q = plan.get("quorum", "all")
    return len(plan["participants"]) if q == "all" else max(1, min(int(q), len(plan["participants"])))


def tally(plan: dict, now: float | None = None) -> tuple[str, int | None]:
    """Decide the organizer's next state. Returns ``(status, chosen_slot)``.

    * quorum "all": confirm the earliest slot every participant accepted, once
      everyone has answered; any decline/counter means reschedule.
    * quorum N: confirm a slot that N participants accepted as soon as that
      happens; reschedule if it becomes impossible (or the deadline passes
      without it).

    Among eligible slots, the one the most people marked as *preferred* wins;
    remaining ties go to the earliest.
    """
    if plan["status"] in ("confirmed", "cancelled"):
        return plan["status"], plan.get("chosen")
    parts = plan["participants"].values()
    need = _required(plan)
    counts = [0] * len(plan["slots"])
    prefs = [0] * len(plan["slots"])
    for p in parts:
        if p["status"] == "accepted":
            for i in p["ok_slots"]:
                counts[i] += 1
            for i in p.get("prefer", []):
                prefs[i] += 1
    pending = sum(1 for p in parts if p["status"] == "invited")
    best = [i for i, c in enumerate(counts) if c >= need]
    if best and (plan.get("quorum", "all") != "all" or pending == 0):
        return "confirmed", min(best, key=lambda i: (-prefs[i], plan["slots"][i]["start"]))
    # can any slot still reach quorum if every pending participant accepts it?
    reachable = any(c + pending >= need for c in counts)
    past_deadline = plan.get("deadline") is not None and (now or time.time()) > plan["deadline"]
    if not reachable or past_deadline:
        return "needs_reschedule", None
    return "proposed", None


def revise(plan: dict, slots: list[Interval]) -> None:
    if not slots:
        raise PlanError("no candidate slots")
    plan["rev"] += 1
    plan["slots"] = [s.to_wire() for s in slots[:MAX_SLOTS]]
    plan["status"] = "proposed"
    plan["chosen"] = None
    for p in plan["participants"].values():
        p.update(status="invited", ok_slots=[], prefer=[], note="", counter=[], nudged=False)
    plan["updated_at"] = time.time()


def describe(plan: dict, tz: str = "UTC") -> str:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)

    def fmt(s: dict) -> str:
        iv = Interval.from_wire(s)
        a, b = iv.start.astimezone(zone), iv.end.astimezone(zone)
        return f"{a:%a %b %d %H:%M}-{b:%H:%M}"

    lines = [f"{plan['title']}  [{plan['status']}]  id={plan['id']} rev={plan['rev']}"]
    lines.append(f"  organizer: {plan.get('organizer_name') or plan['organizer'][:8]}")
    if plan.get("location"):
        lines.append(f"  where: {plan['location']}")
    if plan.get("rrule"):
        lines.append(f"  repeats: {plan['rrule']}")
    if plan.get("chosen") is not None:
        lines.append(f"  WHEN: {fmt(plan['slots'][plan['chosen']])} ({tz})")
    else:
        for i, s in enumerate(plan["slots"], 1):
            lines.append(f"  option {i}: {fmt(s)} ({tz})")
    for p in plan["participants"].values():
        extra = ""
        if p.get("status") == "accepted":
            extra = " options " + ",".join(str(i + 1) + ("*" if i in p.get("prefer", []) else "") for i in p["ok_slots"])
        if p.get("note"):
            extra += f' — "{p["note"]}"'
        if "status" in p:
            lines.append(f"  - {p['name']}: {p['status']}{extra}")
        else:
            lines.append(f"  - {p['name']}")
    return "\n".join(lines)
