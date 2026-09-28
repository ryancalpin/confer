"""End-to-end: real nodes, real HTTP, real crypto."""

import json
import urllib.request

import pytest

from confer.envelope import seal
from confer.identity import Identity
from confer.node import NodeError
from confer.transport import DeliveryError, HttpTransport

from .conftest import slot, wait_for


def items(node, kind=None):
    return [i for i in node.inbox() if kind is None or i["kind"] == kind]


def plan_status(node, plan_id):
    p = node.store.plan(plan_id)
    return p and p["status"]


def test_pairing_is_mutual_and_single_use(net):
    alice, bob, eve = net.node("alice"), net.node("bob"), net.node("eve")
    token = alice.create_invite("Bob", "plans,autoconfirm")
    bob.accept_invite(token, grants="plans,files")
    wait_for(lambda: (c := bob.store.contact(alice.identity.agent_id)) and c.status == "active", what="bob paired")
    a_view = alice.store.contact(bob.identity.agent_id)
    assert a_view.name == "Bob" and a_view.grants == ["autoconfirm", "plans"]
    assert bob.store.contact(alice.identity.agent_id).grants == ["files", "plans"]
    assert items(alice, "pair") and items(bob, "pair")
    # the same token can't be reused by someone else
    eve.accept_invite(token)
    wait_for(lambda: items(eve, "delivery.failed"), what="eve rejected")
    assert alice.store.contact(eve.identity.agent_id) is None


def test_dinner_with_manual_accept(net):
    alice = net.node("alice", busy=[slot(3, 19)])
    bob = net.node("bob", busy=[slot(4, 19)])
    net.pair(alice, bob)
    plan = alice.create_plan("Dinner", ["bob"], slots=[slot(4, 19, 90), slot(5, 19, 90)], location="Luigi's")
    invite = wait_for(lambda: items(bob, "plan.invite"), what="invite at bob")[0]
    assert invite["actionable"] and invite["payload"]["suggested"] == [1]  # bob busy on day 4
    bob_plan = bob.store.plan(plan["id"])
    assert "status" not in bob_plan["participants"][bob.identity.agent_id]  # wire view only
    bob.respond(plan["id"], "accept")  # defaults to calendar-suggested options
    wait_for(lambda: plan_status(alice, plan["id"]) == "confirmed", what="alice confirmed")
    wait_for(lambda: plan_status(bob, plan["id"]) == "confirmed", what="bob confirmed")
    assert alice.store.plan(plan["id"])["chosen"] == 1
    assert not [i for i in items(bob, "plan.invite")]  # invite closed once answered
    ics = (bob.home / "calendar.ics").read_text()
    assert "SUMMARY:Dinner" in ics and "LOCATION:Luigi's" in ics
    # confirmed plans now count as busy for both
    assert not alice.availability().is_free(slot(5, 19))
    assert not bob.availability().is_free(slot(5, 19))


def test_autoconfirm_grant(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob, b_grants="plans,autoconfirm")
    plan = alice.create_plan("Coffee", ["bob"], slots=[slot(6, 9)])
    wait_for(lambda: plan_status(alice, plan["id"]) == "confirmed", what="auto confirmed")
    assert items(bob, "plan.auto") and not items(bob, "plan.invite")


def test_grants_are_enforced(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob, b_grants="notes")  # bob only accepts notes from alice
    alice.create_plan("Dinner", ["bob"], slots=[slot(6, 19)])
    wait_for(lambda: items(alice, "delivery.failed"), what="plan refused")
    assert not bob.plans()
    alice.send_note("bob", "hi from alice's agent")
    wait_for(lambda: items(bob, "note"), what="note")
    alice.send_file("bob", __file__)
    wait_for(lambda: len(items(alice, "delivery.failed")) == 2, what="file refused")


def test_group_plan_decline_revise_confirm(net):
    org = net.node("org")
    people = [net.node(n, busy=[slot(3, 19)] if n == "cat" else []) for n in ("ann", "ben", "cat")]
    for p in people:
        net.pair(org, p)
    plan = org.create_plan("Game night", ["ann", "ben", "cat"], slots=[slot(3, 19), slot(4, 19)], rrule="FREQ=WEEKLY;COUNT=4")
    for p in people:
        wait_for(lambda p=p: p.store.plan(plan["id"]), what=f"{p.name} got plan")
    people[0].respond(plan["id"], "accept", slots=[0, 1])
    people[1].respond(plan["id"], "accept", slots=[0])
    people[2].respond(plan["id"], "counter", counter=[slot(5, 20)], note="Thursday instead?")
    wait_for(lambda: plan_status(org, plan["id"]) == "needs_reschedule", what="reschedule")
    assert items(org, "plan.counter") and items(org, "plan.reschedule")[0]["actionable"]
    org.revise(plan["id"], slots=[slot(5, 20)])
    for p in people:
        wait_for(lambda p=p: p.store.plan(plan["id"])["rev"] == 2, what="rev 2")
        p.respond(plan["id"], "accept", slots=[0])
    wait_for(lambda: plan_status(org, plan["id"]) == "confirmed", what="confirmed")
    for p in people:
        wait_for(lambda p=p: plan_status(p, plan["id"]) == "confirmed", what=f"{p.name} confirmed")
    assert "RRULE:FREQ=WEEKLY;COUNT=4" in (people[2].home / "calendar.ics").read_text()


def test_quorum_and_late_dropout(net):
    org = net.node("org")
    people = [net.node(n) for n in ("a", "b", "c")]
    for p in people:
        net.pair(org, p)
    plan = org.create_plan("Book club", ["a", "b", "c"], slots=[slot(8, 18)], quorum=2)
    for p in people:
        wait_for(lambda p=p: p.store.plan(plan["id"]), what="plan arrived")
    people[0].respond(plan["id"], "accept")
    people[1].respond(plan["id"], "accept")
    wait_for(lambda: plan_status(people[2], plan["id"]) == "confirmed", what="c told about confirmation")
    assert any("Join anyway" in i["summary"] for i in items(people[2], "plan.confirmed"))
    people[1].cancel(plan["id"], "sick")
    wait_for(lambda: items(org, "plan.dropout"), what="dropout noticed")


def test_cancel_propagates(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    plan = alice.create_plan("Lunch", ["bob"], slots=[slot(7, 12)])
    wait_for(lambda: bob.store.plan(plan["id"]), what="plan at bob")
    alice.cancel(plan["id"], "work came up")
    wait_for(lambda: plan_status(bob, plan["id"]) == "cancelled", what="cancel at bob")
    with pytest.raises(NodeError, match="cancelled"):
        bob.respond(plan["id"], "accept", slots=[0])


def test_encrypted_file_transfer(net, tmp_path):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    f = tmp_path / "menu.pdf"
    f.write_bytes(b"%PDF-1.4 fake menu " * 100)
    alice.send_file("bob", f, note="tonight's menu")
    item = wait_for(lambda: items(bob, "file"), what="file")[0]
    from pathlib import Path

    assert Path(item["payload"]["path"]).read_bytes() == f.read_bytes()


def test_strangers_and_replays_are_rejected(net):
    alice = net.node("alice")
    stranger = Identity.generate()
    t = HttpTransport()
    env = seal(stranger, alice.identity.agent_id, "note", {"text": "hi"})
    with pytest.raises(DeliveryError) as exc:
        t.send(alice.config["endpoint"], env)
    assert exc.value.permanent
    bob = net.node("bob")
    net.pair(alice, bob)
    env = seal(bob.identity, alice.identity.agent_id, "note", {"text": "once"})
    t.send(alice.config["endpoint"], env)
    t.send(alice.config["endpoint"], env)  # replay: acknowledged, not re-processed
    wait_for(lambda: items(alice, "note"), what="note")
    assert len(items(alice, "note")) == 1


def test_forged_plan_final_from_non_organizer_is_rejected(net):
    alice, bob, mallory = net.node("alice"), net.node("bob"), net.node("mallory")
    net.pair(alice, bob)
    net.pair(mallory, bob)
    plan = alice.create_plan("Dinner", ["bob"], slots=[slot(4, 19)])
    wait_for(lambda: bob.store.plan(plan["id"]), what="plan at bob")
    fake = dict(bob.store.plan(plan["id"]), status="confirmed", chosen=0)
    mallory._send(mallory.store.contact(bob.identity.agent_id), "plan.final", {"plan": fake})
    wait_for(lambda: items(mallory, "delivery.failed"), what="forgery refused")
    assert plan_status(bob, plan["id"]) == "proposed"


def test_offline_peer_gets_message_later(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    bob_endpoint = bob.config["endpoint"]
    net.servers[1].stop()  # bob goes offline
    alice.send_note("bob", "are you there?")
    wait_for(lambda: alice.store.outbox() and alice.store.outbox()[0]["attempts"] >= 1, what="retry scheduled")
    from confer.server import ConferServer

    port = int(bob_endpoint.rsplit(":", 1)[1])
    net.servers.append(ConferServer(bob, "127.0.0.1", port, tick_seconds=0.2).start())
    alice.store._x("UPDATE outbox SET next_at=0")
    alice.flush()
    wait_for(lambda: items(bob, "note"), what="late delivery")


def test_relay_for_unreachable_nodes(net):
    relay = net.relay()
    alice = net.node("alice", via_relay=relay, serve=False)
    bob = net.node("bob", via_relay=relay, serve=False)
    token = alice.create_invite("Bob")
    bob.accept_invite(token)

    def sync():
        alice.tick()
        bob.tick()

    wait_for(lambda: (sync(), (c := bob.store.contact(alice.identity.agent_id)) and c.status == "active")[1], what="paired via relay")
    plan = alice.create_plan("Walk", ["bob"], slots=[slot(9, 10)])
    wait_for(lambda: (sync(), bob.store.plan(plan["id"]))[1], what="plan via relay")
    bob.respond(plan["id"], "accept")
    wait_for(lambda: (sync(), plan_status(bob, plan["id"]) == "confirmed")[1], what="confirmed via relay")


def test_agent_card_and_calendar_feed(net):
    alice = net.node("alice")
    base = alice.config["endpoint"]
    card = json.load(urllib.request.urlopen(base + "/.well-known/agent-card.json"))
    assert card["supportedInterfaces"][0]["url"] == base + "/a2a"
    assert card["capabilities"]["extensions"][0]["params"]["agent_id"] == alice.identity.agent_id
    feed = urllib.request.urlopen(f"{base}/calendar/{alice.config['feed_token']}.ics").read().decode()
    assert feed.startswith("BEGIN:VCALENDAR")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/calendar/wrong.ics")


def test_plain_a2a_text_message_gets_explanation(net):
    alice = net.node("alice")
    req = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
           "params": {"message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": "hello"}]}}}
    r = urllib.request.Request(alice.config["endpoint"] + "/a2a", data=json.dumps(req).encode(), headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(r))
    assert "Confer node" in resp["result"]["message"]["parts"][0]["text"]


def test_calendar_lines_are_folded_and_naive_until_is_normalized(net):
    alice, bob = net.node("alice", tz="America/Chicago"), net.node("bob")
    net.pair(alice, bob, b_grants="plans,autoconfirm")
    title = "Very long recurring planning session with lots of words and ünïcødé " * 3
    plan = alice.create_plan(title, ["bob"], slots=[slot(6, 19)], rrule="FREQ=WEEKLY;UNTIL=20311231T000000")
    assert plan["rrule"] == "FREQ=WEEKLY;UNTIL=20311231T000000Z"
    wait_for(lambda: plan_status(alice, plan["id"]) == "confirmed", what="confirmed")
    ics = alice.calendar_ics()
    lines = ics.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    assert "DTSTART;TZID=America/Chicago:20310306T130000" in ics
    unfolded = ics.replace("\r\n ", "")
    assert "ünïcødé" in unfolded


def test_generated_options_are_never_in_the_past(net):
    import time
    from datetime import datetime, timedelta, timezone

    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    plan = alice.create_plan("Coffee", ["bob"], window_start=today, window_end=today + timedelta(days=3), duration_minutes=30)
    assert all(datetime.fromisoformat(s["start"].replace("Z", "+00:00")).timestamp() > time.time() for s in plan["slots"])
