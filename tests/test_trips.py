"""Trip planning, organization and logistics — end to end."""

import pytest

from confer import trips as T
from confer.node import NodeError

from .conftest import slot, wait_for
from .test_network import items


def trip_at(node, tid):
    return node.store.get_trip(tid)


@pytest.fixture
def crew(net):
    alex, sam, priya = net.node("alex", tz="America/Chicago"), net.node("sam"), net.node("priya")
    net.pair(alex, sam)
    net.pair(alex, priya)
    trip = alex.create_trip("Tahoe", ["sam", "priya"], start_date="2031-03-20", end_date="2031-03-23", destination="Lake Tahoe")
    for n in (sam, priya):
        wait_for(lambda n=n: trip_at(n, trip["id"]), what=f"trip at {n.name}")
    return alex, sam, priya, trip


def test_trip_is_shared_with_a_packing_list(crew):
    alex, sam, priya, trip = crew
    assert items(sam, "trip") and trip["links"]["list_id"]
    wait_for(lambda: sam.store.get_list(trip["links"]["list_id"]), what="packing list")
    assert trip_at(sam, trip["id"])["destination"] == "Lake Tahoe"


def test_itinerary_logistics_rides_rooms(crew):
    alex, sam, priya, trip = crew
    tid = trip["id"]
    alex.trip_op(tid, "itinerary.add", kind="lodging", title="Cabin", start="2031-03-20T22:00:00Z", end="2031-03-23T16:00:00Z",
                 location="12 Pine Rd", confirmation="HMX42", url="https://example.com/cabin")
    sam.trip_op(tid, "itinerary.add", kind="activity", title="Ski day", start="2031-03-21T16:00:00Z")
    sam.trip_op(tid, "traveler.set", arrive={"when": "2031-03-20T19:30:00Z", "how": "UA 1234", "where": "RNO", "needs_pickup": True},
                depart={"when": "2031-03-23T18:00:00Z", "how": "UA 99"})
    alex.trip_op(tid, "ride.offer", seats=3, **{"from": "RNO airport"}, leaves_at="2031-03-20T20:00:00Z")
    wait_for(lambda: trip_at(sam, tid)["rides"], what="ride offered")
    ride = trip_at(sam, tid)["rides"][0]["id"]
    sam.trip_op(tid, "ride.join", id=ride)
    alex.trip_op(tid, "room.add", name="Loft", beds=1)
    wait_for(lambda: trip_at(priya, tid)["rooms"], what="room")
    room = trip_at(priya, tid)["rooms"][0]["id"]
    priya.trip_op(tid, "room.join", id=room)

    def settled():
        t = trip_at(priya, tid)
        return (len(t["itinerary"]) == 2 and sam.identity.agent_id in t["travelers"] and
                t["rides"][0]["passengers"] and t["rooms"][0]["occupants"])

    wait_for(settled, what="all edits visible to priya")
    t = trip_at(priya, tid)
    assert [i["title"] for i in t["itinerary"]] == ["Cabin", "Ski day"]  # sorted by time
    assert t["travelers"][sam.identity.agent_id]["arrive"]["needs_pickup"] is True
    with pytest.raises(NodeError, match="full"):  # 1 bed, priya has it
        alex.trip_op(tid, "room.join", id=room)
    # calendar feed carries the trip and timed itinerary
    ics = sam.calendar_ics()
    assert "DTSTART;VALUE=DATE:20310320" in ics and "SUMMARY:Ski day" in ics.replace("\r\n ", "")
    # members can't touch others' logistics or trip details
    with pytest.raises(NodeError, match="organizer"):
        sam.trip_op(tid, "trip.update", status="booked")


def test_tasks_and_polls_notify_the_right_people(crew):
    alex, sam, priya, trip = crew
    tid = trip["id"]
    alex.trip_op(tid, "task.add", text="Book the cabin", assignee="sam", due="2031-03-01")
    alex.trip_op(tid, "poll.add", question="Dinner Saturday?", options=["Sushi", "Pizza", "Cook in"])
    wait_for(lambda: [i for i in items(sam, "trip") if "you're on" in i["summary"]], what="task assigned to sam")
    assert not [i for i in items(priya, "trip") if "you're on" in i["summary"]]
    wait_for(lambda: [i for i in items(priya, "trip") if "Dinner Saturday" in i["summary"]], what="poll at priya")
    poll = trip_at(priya, tid)["polls"][0]
    priya.trip_op(tid, "poll.vote", id=poll["id"], option="o1")
    sam.trip_op(tid, "poll.vote", id=poll["id"], option="o1")
    task = trip_at(sam, tid)["tasks"][0]
    sam.trip_op(tid, "task.done", id=task["id"])
    wait_for(lambda: len(trip_at(alex, tid)["polls"][0]["votes"]) == 2 and trip_at(alex, tid)["tasks"][0]["done"], what="votes+done")
    alex.trip_op(tid, "poll.close", id=poll["id"])
    wait_for(lambda: trip_at(priya, tid)["polls"][0]["closed"], what="closed")
    with pytest.raises(NodeError, match="closed"):
        alex.trip_op(tid, "poll.vote", id=poll["id"], option="o0")


def test_trip_blocks_calendar_and_tracks_budget(crew):
    alex, sam, priya, trip = crew
    assert not sam.availability().is_free(slot(21, 18))  # away on the trip
    alex.add_expense("Cabin", "600", ["sam", "priya"], plan_id=trip["id"])
    for n in (sam, priya):
        wait_for(lambda n=n: n.ledger(), what="expense")
        n.answer_entry(n.ledger()[0]["id"], accept=True)
    assert alex.trip_budget(trip["id"]) == {"USD": {"you_paid_shares": 40000, "you_owe": 0}}
    assert sam.trip_budget(trip["id"])["USD"]["you_owe"] == 20000


def test_leave_and_cancel(crew):
    alex, sam, priya, trip = crew
    tid = trip["id"]
    priya.trip_op(tid, "leave")
    assert all(t["id"] != tid for t in priya.trips())
    wait_for(lambda: trip_at(priya, tid) is None, what="priya out")
    assert priya.identity.agent_id not in trip_at(alex, tid)["members"]
    alex.cancel_trip(tid)
    wait_for(lambda: trip_at(sam, tid)["status"] == "cancelled", what="cancelled at sam")
    assert sam.availability().is_free(slot(21, 18))  # no longer blocked


def test_trips_need_the_grant(net):
    alex, sam = net.node("alex"), net.node("sam")
    net.pair(alex, sam, b_grants="plans")
    alex.create_trip("Nope", ["sam"], start_date="2031-04-01", end_date="2031-04-02", packing_list=False)
    wait_for(lambda: items(alex, "delivery.failed"), what="refused")
    assert not sam.trips()


def test_validation_of_hostile_snapshots():
    good = T.new_trip(title="x", owner="O", owner_name="o", members={"M": "m"}, start_date="2031-01-01", end_date="2031-01-02")
    assert T.validate_wire_trip(T.wire(good), "O")["title"] == "x"
    bad = T.wire(good) | {"itinerary": [{"id": "../../etc", "title": "x"}]}
    with pytest.raises(T.TripError):
        T.validate_wire_trip(bad, "O")
    with pytest.raises(T.TripError):
        T.validate_wire_trip(T.wire(good), "someone-else")
    huge = T.wire(good) | {"tasks": [{"id": f"t{i:04d}", "text": "x"} for i in range(T.LIMITS["tasks"] + 1)]}
    with pytest.raises(T.TripError):
        T.validate_wire_trip(huge, "O")
    xss = T.wire(good) | {"itinerary": [{"id": "abcd", "title": "t", "url": "javascript:alert(1)"}]}
    assert T.validate_wire_trip(xss, "O")["itinerary"][0]["url"] == ""
    with pytest.raises(T.TripError):
        T.new_trip(title="x", owner="O", owner_name="o", members={}, start_date="2031-01-05", end_date="2031-01-02")
