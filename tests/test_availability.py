from datetime import time as dtime, timedelta

from confer.availability import Availability, Hours, ICSProvider, Interval, StaticProvider, spread

from .conftest import at, slot


def test_busy_and_hours():
    av = Availability(StaticProvider([slot(3, 18, 90)]), Hours(tz="UTC", start=dtime(9), end=dtime(22)))
    assert not av.is_free(slot(3, 19))  # overlaps busy
    assert av.is_free(slot(3, 20))
    assert not av.is_free(slot(3, 7))  # before hours
    assert not av.is_free(slot(3, 21, 90))  # runs past 22:00
    assert not av.is_free(Interval(at(3, 23), at(4, 1)))  # crosses midnight


def test_buffer_minutes():
    av = Availability(StaticProvider([slot(3, 18)]), buffer_minutes=15)
    assert not av.is_free(Interval(at(3, 19, 10), at(3, 20)))
    assert av.is_free(Interval(at(3, 19, 15), at(3, 20)))


def test_timezone_hours():
    # 18:00 in Chicago (CDT, UTC-5 in March 2031 after DST) is 23:00 UTC
    av = Availability(StaticProvider(), Hours(tz="America/Chicago", start=dtime(17), end=dtime(21)))
    assert av.is_free(slot(20, 23))
    assert not av.is_free(slot(20, 15))


def test_candidates_between_and_spread():
    av = Availability(StaticProvider([slot(3, 18, 180)]))
    c = av.candidates(at(3, 0), at(7, 0), timedelta(minutes=90), between=(dtime(18), dtime(21)))
    assert c and all(s.start.hour >= 18 and s.end.hour <= 21 for s in c)
    assert not any(s.start.day == 3 for s in c)  # day 3 is fully booked 18-21
    picked = spread(c, 3)
    assert len({s.start.date() for s in picked}) == 3


def test_recurring_plan_checks_future_occurrences():
    # busy in two weeks' time on the same weekday: a weekly plan must not fit
    av = Availability(StaticProvider([slot(17, 19)]))
    assert av.is_free(slot(3, 19))
    assert not av.is_free(slot(3, 19), rrule="FREQ=WEEKLY")
    assert av.is_free(slot(3, 19), rrule="FREQ=WEEKLY;COUNT=2")


def test_ics_provider(tmp_path):
    ics = tmp_path / "cal.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n"
        "BEGIN:VEVENT\r\nUID:1\r\nDTSTART:20310303T180000Z\r\nDTEND:20310303T190000Z\r\nSUMMARY:Gym\r\nRRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:2\r\nDTSTART:20310310T180000Z\r\nDTEND:20310310T190000Z\r\nTRANSP:TRANSPARENT\r\nSUMMARY:FYI\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:3\r\nDTSTART;VALUE=DATE:20310312\r\nDTEND;VALUE=DATE:20310313\r\nSUMMARY:Trip\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    av = Availability(ICSProvider(str(ics)))
    assert not av.is_free(slot(4, 18))  # recurring occurrence
    assert av.is_free(slot(6, 18))  # after COUNT
    assert av.is_free(slot(10, 18))  # transparent event doesn't block
    assert not av.is_free(slot(12, 12))  # all-day event
