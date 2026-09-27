"""Availability: who is busy when, and which candidate slots work.

Privacy model: a node never ships its calendar anywhere. Peers only learn the
answer to "which of *these* proposed slots work for you?" — and only after the
owner (or an ``autoconfirm`` grant they set) says so.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

UTC = timezone.utc


def parse_dt(value: str) -> datetime:
    """Parse ISO-8601; naive values are rejected on the wire."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must include a UTC offset: {value!r}")
    return dt.astimezone(UTC)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, order=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("intervals must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("interval end must be after start")

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def to_wire(self) -> dict:
        return {"start": fmt_dt(self.start), "end": fmt_dt(self.end)}

    @classmethod
    def from_wire(cls, data: dict) -> "Interval":
        return cls(parse_dt(data["start"]), parse_dt(data["end"]))


def occurrences(slot: Interval, rrule: str | None, count: int = 4) -> list[Interval]:
    """First ``count`` occurrences of a (possibly recurring) slot."""
    if not rrule:
        return [slot]
    rule = rrulestr(rrule, dtstart=slot.start)
    span = slot.end - slot.start
    out: list[Interval] = []
    for start in rule:
        out.append(Interval(start, start + span))
        if len(out) >= count:
            break
    return out


class BusyProvider(Protocol):
    def busy(self, start: datetime, end: datetime) -> list[Interval]: ...


class StaticProvider:
    """Busy intervals held in memory (tests, or a JSON file of intervals)."""

    def __init__(self, intervals: Iterable[Interval] = ()):
        self.intervals = sorted(intervals)

    @classmethod
    def from_json(cls, path: Path) -> "StaticProvider":
        return cls(Interval.from_wire(i) for i in json.loads(path.read_text()))

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        window = Interval(start, end)
        return [i for i in self.intervals if i.overlaps(window)]


class ICSProvider:
    """Busy time from an .ics file or URL (Google/iCloud/Outlook "secret iCal
    address" all work). Needs the ``ics`` extra. Recurring events expand."""

    def __init__(self, source: str, cache_seconds: int = 300):
        self.source = source
        self.cache_seconds = cache_seconds
        self._cached: tuple[float, bytes] | None = None

    def _raw(self) -> bytes:
        if self._cached and time.time() - self._cached[0] < self.cache_seconds:
            return self._cached[1]
        if self.source.startswith(("http://", "https://", "webcal://")):
            url = self.source.replace("webcal://", "https://", 1)
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (user-configured)
                data = resp.read()
        else:
            data = Path(self.source).expanduser().read_bytes()
        self._cached = (time.time(), data)
        return data

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        try:
            import icalendar
            import recurring_ical_events
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError("ICS calendars need: pip install 'confer[ics]'") from exc
        cal = icalendar.Calendar.from_ical(self._raw())
        out: list[Interval] = []
        for ev in recurring_ical_events.of(cal).between(start, end):
            if str(ev.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
                continue
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            s = ev.decoded("DTSTART")
            e = ev.decoded("DTEND") if ev.get("DTEND") else None
            if isinstance(s, datetime) and s.tzinfo is None:
                s = s.replace(tzinfo=UTC)
            if not isinstance(s, datetime):  # all-day event
                s = datetime.combine(s, dtime.min, UTC)
                e = datetime.combine(e, dtime.min, UTC) if isinstance(e, date) else s + timedelta(days=1)
            if e is None:
                e = s + (ev.decoded("DURATION") if ev.get("DURATION") else timedelta(hours=1))
            if isinstance(e, datetime) and e.tzinfo is None:
                e = e.replace(tzinfo=UTC)
            if e > s:
                out.append(Interval(s, e))
        return sorted(out)


class CompositeProvider:
    def __init__(self, *providers: BusyProvider):
        self.providers = providers

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        out: list[Interval] = []
        for p in self.providers:
            out.extend(p.busy(start, end))
        return sorted(out)


@dataclass
class Hours:
    """Hours the owner is willing to be scheduled, in their local timezone."""

    tz: str = "UTC"
    start: dtime = dtime(8, 0)
    end: dtime = dtime(22, 0)
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # Monday=0

    def contains(self, slot: Interval) -> bool:
        zone = ZoneInfo(self.tz)
        s, e = slot.start.astimezone(zone), slot.end.astimezone(zone)
        if s.date() != (e - timedelta(microseconds=1)).date():
            return False  # never auto-schedule across local midnight
        # a slot ending exactly at local midnight ends "at 24:00", past any end-of-hours
        end_t = dtime.max if (e.time() == dtime(0) and e.date() != s.date()) else e.time()
        return s.weekday() in self.days and self.start <= s.time() and end_t <= self.end

    @classmethod
    def from_config(cls, cfg: dict) -> "Hours":
        def t(v: str) -> dtime:
            h, m = v.split(":")
            return dtime(int(h), int(m))

        return cls(
            tz=cfg.get("tz", "UTC"),
            start=t(cfg.get("hours_start", "08:00")),
            end=t(cfg.get("hours_end", "22:00")),
            days=tuple(cfg.get("days", (0, 1, 2, 3, 4, 5, 6))),
        )


class Availability:
    def __init__(self, provider: BusyProvider, hours: Hours | None = None, buffer_minutes: int = 0):
        self.provider = provider
        self.hours = hours or Hours()
        self.buffer = timedelta(minutes=buffer_minutes)

    def is_free(self, slot: Interval, rrule: str | None = None) -> bool:
        for occ in occurrences(slot, rrule):
            if not self.hours.contains(occ):
                return False
            padded = Interval(occ.start - self.buffer, occ.end + self.buffer)
            if any(b.overlaps(padded) for b in self.provider.busy(padded.start, padded.end)):
                return False
        return True

    def free_indices(self, slots: list[Interval], rrule: str | None = None) -> list[int]:
        return [i for i, s in enumerate(slots) if self.is_free(s, rrule)]

    def candidates(
        self,
        window_start: datetime,
        window_end: datetime,
        duration: timedelta,
        *,
        between: tuple[dtime, dtime] | None = None,
        step: timedelta = timedelta(minutes=30),
        rrule: str | None = None,
        limit: int = 200,
    ) -> list[Interval]:
        """Free slots of ``duration`` inside the window, optionally restricted to
        a local time-of-day range (e.g. 18:00-21:00 for dinner)."""
        zone = ZoneInfo(self.hours.tz)
        out: list[Interval] = []
        cursor = _ceil_to(window_start, step)
        while cursor + duration <= window_end and len(out) < limit:
            slot = Interval(cursor, cursor + duration)
            local_s, local_e = slot.start.astimezone(zone), slot.end.astimezone(zone)
            in_range = between is None or (
                local_s.date() == local_e.date() and between[0] <= local_s.time() and local_e.time() <= between[1]
            )
            if in_range and self.is_free(slot, rrule):
                out.append(slot)
            cursor += step
        return out


def _ceil_to(dt: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    secs = step.total_seconds()
    n = -(-(dt - epoch).total_seconds() // secs)
    return epoch + timedelta(seconds=n * secs)


def spread(slots: list[Interval], n: int) -> list[Interval]:
    """Pick up to ``n`` candidates, preferring different days so a proposal
    offers real choice instead of six back-to-back half hours."""
    chosen: list[Interval] = []
    seen_days: set[date] = set()
    for s in slots:
        if s.start.date() not in seen_days:
            chosen.append(s)
            seen_days.add(s.start.date())
        if len(chosen) >= n:
            return sorted(chosen)
    for s in slots:
        if len(chosen) >= n:
            break
        if s not in chosen and all(not s.overlaps(c) for c in chosen):
            chosen.append(s)
    return sorted(chosen)
