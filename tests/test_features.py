"""End-to-end tests for v0.2 features: preferences, holds, reminders, threaded
notes, vouched introductions, key rotation, ordered delivery."""

from confer.envelope import canonical, seal
from confer.identity import Identity, b64e

from .conftest import slot, wait_for
from .test_network import items, plan_status


def test_preferences_steer_the_chosen_time(net):
    org, a, b = net.node("org"), net.node("a"), net.node("b")
    net.pair(org, a)
    net.pair(org, b)
    plan = org.create_plan("Brunch", ["a", "b"], slots=[slot(8, 10), slot(9, 10)])
    for p in (a, b):
        wait_for(lambda p=p: p.store.plan(plan["id"]), what="plan arrived")
    a.respond(plan["id"], "accept", slots=[0, 1], prefer=[1])
    b.respond(plan["id"], "accept", slots=[0, 1], prefer=[1])
    wait_for(lambda: plan_status(org, plan["id"]) == "confirmed", what="confirmed")
    assert org.store.plan(plan["id"])["chosen"] == 1  # later, but preferred by both


def test_tentative_holds_prevent_double_booking(net):
    alice, bob, cat, dan = net.node("alice"), net.node("bob"), net.node("cat"), net.node("dan")
    net.pair(alice, cat)
    net.pair(alice, dan)
    net.pair(bob, cat)
    p1 = alice.create_plan("Dinner", ["cat", "dan"], slots=[slot(10, 19), slot(11, 19)])
    wait_for(lambda: cat.store.plan(p1["id"]), what="p1 at cat")
    cat.respond(p1["id"], "accept", slots=[0])  # cat holds day 10 while dan decides...
    wait_for(lambda: alice.store.plan(p1["id"])["participants"][cat.identity.agent_id]["status"] == "accepted", what="cat answered")
    assert plan_status(alice, p1["id"]) == "proposed"
    p2 = bob.create_plan("Drinks", ["cat"], slots=[slot(10, 19), slot(12, 19)])
    invite = wait_for(lambda: [i for i in items(cat, "plan.invite") if i["ref"] == p2["id"]], what="p2 invite")[0]
    assert invite["payload"]["suggested"] == [1]  # day 10 is held for alice's plan
    # organizers hold their own candidates too
    assert not alice.availability().is_free(slot(11, 19))


def test_reminders_nudge_only_stragglers_once(net):
    org, a, b = net.node("org"), net.node("a"), net.node("b")
    net.pair(org, a)
    net.pair(org, b)
    plan = org.create_plan("Party", ["a", "b"], slots=[slot(8, 20)])
    for p in (a, b):
        wait_for(lambda p=p: p.store.plan(plan["id"]), what="plan arrived")
    a.respond(plan["id"], "accept")
    wait_for(lambda: org.store.plan(plan["id"])["participants"][a.identity.agent_id]["status"] == "accepted", what="a answered")
    org.config["nudge_after_hours"] = 0
    assert org.send_reminders() == 1  # only b
    assert org.send_reminders() == 0  # once
    wait_for(lambda: items(b, "plan.reminder"), what="b reminded")
    assert not items(a, "plan.reminder")


def test_note_threads(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    q = alice.send_note("bob", "Does Sam prefer Thai or Italian?", expects_reply=True)
    item = wait_for(lambda: items(bob, "note"), what="question")[0]
    assert item["actionable"] and item["payload"]["msg_id"] == q
    bob.send_note("alice", "Thai, no cilantro", reply_to=q)
    wait_for(lambda: alice.replies(q), what="reply")
    assert alice.replies(q)[0]["payload"]["text"] == "Thai, no cilantro"


def test_vouched_introduction_both_orders(net):
    hub, ann, ben, cy = net.node("hub"), net.node("ann"), net.node("ben"), net.node("cy")
    for p in (ann, ben, cy):
        net.pair(hub, p, b_grants="plans,intros")
    # ann accepts first
    iid = hub.introduce("ann", "ben", note="you both climb")
    wait_for(lambda: ann.store.intro(iid) and ben.store.intro(iid), what="offers")
    assert [i for i in items(ann, "intro") if i["actionable"]]
    ann.accept_intro(iid)
    wait_for(lambda: ben.store.intro(iid)["peer_ready"], what="ben knows ann is willing")
    ben.accept_intro(iid, grants="plans")
    wait_for(lambda: (c := ann.store.contact(ben.identity.agent_id)) and c.status == "active", what="ann connected")
    assert ben.store.contact(ann.identity.agent_id).status == "active"
    # and they can now plan directly, no hub involved
    p = ann.create_plan("Climb", ["ben"], slots=[slot(14, 17)])
    wait_for(lambda: ben.store.plan(p["id"]), what="direct plan")
    # a declined intro never connects
    iid2 = hub.introduce("ann", "cy")
    wait_for(lambda: cy.store.intro(iid2) and ann.store.intro(iid2), what="offers 2")
    cy.decline_intro(iid2)
    ann.accept_intro(iid2)
    wait_for(lambda: items(ann, "delivery.failed"), what="declined side refuses")
    assert ann.store.contact(cy.identity.agent_id).status == "pending"


def test_introductions_need_the_grant(net):
    hub, ann, ben = net.node("hub"), net.node("ann"), net.node("ben")
    net.pair(hub, ann, b_grants="plans")  # ann does not accept intros from hub
    net.pair(hub, ben, b_grants="intros")
    hub.introduce("ann", "ben")
    wait_for(lambda: items(hub, "delivery.failed"), what="refused")
    assert not ann.intros()


def test_key_rotation_keeps_relationships_and_plans(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    plan = alice.create_plan("Dinner", ["bob"], slots=[slot(15, 19)])
    wait_for(lambda: bob.store.plan(plan["id"]), what="plan at bob")
    old_id = alice.identity.agent_id
    new_id = alice.rotate_key()
    wait_for(lambda: bob.store.contact(new_id), what="bob learned the new key")
    assert bob.store.contact(old_id) is None
    assert bob.store.plan(plan["id"])["organizer"] == new_id
    bob.respond(plan["id"], "accept")  # answer reaches alice under her new key
    wait_for(lambda: plan_status(bob, plan["id"]) == "confirmed", what="confirmed after rotation")
    # messages still addressed to the old key are accepted during the grace period
    late = seal(bob.identity, old_id, "note", {"text": "late"})
    assert alice.receive(late)["ok"]


def test_forged_key_rotation_is_refused(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    attacker = Identity.generate()
    ts = int(alice.now())
    # alice's (real) envelope but a proof signed by someone else's key
    bad_proof = b64e(Identity.generate().sign(canonical({"old": alice.identity.agent_id, "new": attacker.agent_id, "ts": ts})))
    alice._send(alice.store.contact(bob.identity.agent_id), "key.rotate", {"new_id": attacker.agent_id, "ts": ts, "proof": bad_proof})
    wait_for(lambda: items(alice, "delivery.failed"), what="refused")
    assert bob.store.contact(alice.identity.agent_id)


def test_delivery_to_a_peer_stays_in_order(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    bob_srv = net.servers[1]
    port = bob_srv.port
    bob_srv.stop()
    for i in range(3):
        alice.send_note("bob", f"msg {i}")
    wait_for(lambda: alice.store.outbox()[0]["attempts"] >= 1, what="first attempt failed")
    from confer.server import ConferServer

    net.servers.append(ConferServer(bob, "127.0.0.1", port, tick_seconds=0.2).start())
    alice.store._x("UPDATE outbox SET next_at=0")
    alice.flush()
    wait_for(lambda: len(items(bob, "note")) == 3, what="all notes")
    assert [i["payload"]["text"] for i in items(bob, "note")] == ["msg 0", "msg 1", "msg 2"]


def test_nudge_spam_and_intro_flood_are_bounded(net):
    from confer import node as N

    org, a = net.node("org"), net.node("a")
    net.pair(org, a, b_grants="plans,intros")
    plan = org.create_plan("Party", ["a"], slots=[slot(8, 20)])
    wait_for(lambda: a.store.plan(plan["id"]), what="plan")
    c = org.store.contact(a.identity.agent_id)
    for _ in range(5):
        org._send(c, "plan.nudge", {"plan_id": plan["id"], "rev": 1})
    wait_for(lambda: items(a, "plan.reminder"), what="reminder")
    wait_for(lambda: not org.store.outbox(), what="all delivered")
    assert len(items(a, "plan.reminder")) == 1
    a.respond(plan["id"], "accept")
    assert not items(a, "plan.reminder")  # answering closes it
    for i in range(N.MAX_PENDING_INTROS + 2):
        org._send(c, "intro.offer", {"intro_id": f"intro{i:04d}x", "peer": {"id": Identity.generate().agent_id, "name": f"p{i}"}})
    wait_for(lambda: len(items(org, "delivery.failed")) == 2, what="flood refused")
    assert len(a.intros()) == N.MAX_PENDING_INTROS


def test_secret_paths_are_not_logged(net, caplog):
    import logging
    import urllib.request

    alice = net.node("alice")
    with caplog.at_level(logging.DEBUG, logger="confer.server"):
        urllib.request.urlopen(f"{alice.config['endpoint']}/ui/{alice.config['ui_token']}").read()
    assert alice.config["ui_token"] not in caplog.text and "/ui/[redacted]" in caplog.text
