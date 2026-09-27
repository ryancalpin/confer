import pytest

from confer import plans as P

from .conftest import slot


def make(quorum="all", n=3, deadline=None):
    return P.new_plan(
        title="Dinner", organizer="org", organizer_name="Org",
        participants={f"p{i}": f"P{i}" for i in range(n)},
        slots=[slot(3, 19), slot(4, 19), slot(5, 19)], quorum=quorum, deadline=deadline,
    )


def test_all_must_agree_on_a_common_slot():
    plan = make()
    P.record_response(plan, "p0", "accept", [1, 2])
    P.record_response(plan, "p1", "accept", [0, 2])
    assert P.tally(plan) == ("proposed", None)
    P.record_response(plan, "p2", "accept", [2, 1])
    assert P.tally(plan) == ("confirmed", 2)


def test_no_common_slot_needs_reschedule():
    plan = make(n=2)
    P.record_response(plan, "p0", "accept", [0])
    P.record_response(plan, "p1", "accept", [1])
    assert P.tally(plan) == ("needs_reschedule", None)


def test_decline_under_all_quorum_reschedules_immediately():
    plan = make()
    P.record_response(plan, "p0", "decline", [])
    assert P.tally(plan)[0] == "needs_reschedule"


def test_numeric_quorum_confirms_early_and_picks_earliest():
    plan = make(quorum=2, n=5)
    P.record_response(plan, "p0", "accept", [1, 2])
    assert P.tally(plan)[0] == "proposed"
    P.record_response(plan, "p1", "accept", [2, 1])
    assert P.tally(plan) == ("confirmed", 1)


def test_deadline_forces_outcome():
    plan = make(quorum=2, deadline=100.0)
    P.record_response(plan, "p0", "accept", [0])
    assert P.tally(plan, now=50)[0] == "proposed"
    assert P.tally(plan, now=150)[0] == "needs_reschedule"


def test_validation():
    plan = make()
    with pytest.raises(P.PlanError):
        P.record_response(plan, "stranger", "accept", [0])
    with pytest.raises(P.PlanError):
        P.record_response(plan, "p0", "accept", [9])  # no valid slot
    with pytest.raises(P.PlanError):
        P.new_plan(title="x", organizer="o", organizer_name="O", participants={"a": "A"}, slots=[slot(3, 1)], quorum=0)
    wire = P.wire_view(plan)
    assert "status" not in wire["participants"]["p0"]  # others' answers aren't shared
    with pytest.raises(P.PlanError):
        P.validate_wire_plan(wire, organizer="someone-else")
    assert P.validate_wire_plan(wire, organizer="org")["id"] == plan["id"]


def test_revise_resets_answers():
    plan = make()
    P.record_response(plan, "p0", "decline", [])
    P.revise(plan, [slot(9, 19)])
    assert plan["rev"] == 2 and plan["participants"]["p0"]["status"] == "invited" and len(plan["slots"]) == 1


@pytest.mark.parametrize("rrule", ["FREQ=SECONDLY", "FREQ=WEEKLY\rEND:VEVENT\rBEGIN:VEVENT\rSUMMARY:Phish", "FREQ=HOURLY"])
def test_hostile_recurrence_is_refused(rrule):
    wire = P.wire_view(make())
    wire["rrule"] = rrule
    with pytest.raises(P.PlanError, match="rrule"):
        P.validate_wire_plan(wire, organizer="org")
