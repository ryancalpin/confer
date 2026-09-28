"""Shared lists, expense splitting, status/ETA/location — end to end."""

import pytest

from confer.money import split_equal, to_cents
from confer.node import NodeError

from .conftest import slot, wait_for
from .test_network import items


def test_shared_list_edits_flow_through_the_owner(net):
    alex, sam, priya = net.node("alex"), net.node("sam"), net.node("priya")
    net.pair(alex, sam)
    net.pair(alex, priya)
    lst = alex.create_list("BBQ", ["sam", "priya"], items=["burgers", "buns"])
    wait_for(lambda: sam.store.get_list(lst["id"]) and priya.store.get_list(lst["id"]), what="shared")
    assert items(sam, "list")
    sam.list_op(lst["id"], "add", text="charcoal")
    sam.list_op(lst["id"], "claim", item="buns")
    wait_for(lambda: len(priya.store.get_list(lst["id"])["items"]) == 3, what="priya sees sam's add")
    got = {i["text"]: i for i in priya.store.get_list(lst["id"])["items"]}
    assert got["buns"]["claimed_name"] == "sam"
    with pytest.raises(NodeError):  # already claimed by sam
        alex.list_op(lst["id"], "claim", item="buns")
    priya.list_op(lst["id"], "check", item=1)
    wait_for(lambda: alex.store.get_list(lst["id"])["items"][0]["done"], what="checked")
    priya.list_op(lst["id"], "leave")
    wait_for(lambda: priya.store.get_list(lst["id"]) is None and "priya" not in alex.store.get_list(lst["id"])["members"].values(), what="left")
    alex.delete_list(lst["id"])
    wait_for(lambda: sam.store.get_list(lst["id"]) is None, what="closed for sam")


def test_lists_need_the_grant_and_membership(net):
    alex, sam, eve = net.node("alex"), net.node("sam"), net.node("eve")
    net.pair(alex, sam, b_grants="plans")  # sam doesn't accept lists from alex
    net.pair(alex, eve)
    alex.create_list("Secret", ["sam"])
    wait_for(lambda: items(alex, "delivery.failed"), what="refused")
    lst = alex.create_list("Party", ["eve"])
    wait_for(lambda: eve.store.get_list(lst["id"]), what="eve has it")
    # a non-member contact can't edit
    net.pair(sam, alex)
    sam_view = dict(eve.store.get_list(lst["id"]), role="member")
    sam.store.save_list(sam_view)
    sam.list_op(lst["id"], "add", text="crash the party")
    wait_for(lambda: len(items(sam, "delivery.failed")) >= 1, what="non-member refused")
    assert all(i["text"] != "crash the party" for i in alex.store.get_list(lst["id"])["items"])


def test_money_math():
    assert to_cents("96.50") == 9650 and to_cents("$1,200") == 120000
    assert split_equal(1000, 3) == [334, 333, 333] and sum(split_equal(1001, 4)) == 1001
    with pytest.raises(ValueError):
        to_cents("-5")


def test_split_accept_dispute_and_settle(net):
    alex, sam, priya = net.node("alex"), net.node("sam"), net.node("priya")
    net.pair(alex, sam)
    net.pair(alex, priya)
    alex.config["pay_link"] = "https://venmo.com/u/alex"
    entries = alex.add_expense("Dinner", "90.00", ["sam", "priya"], note="Luigi's")
    assert [e["cents"] for e in entries] == [3000, 3000]
    req = wait_for(lambda: items(sam, "money"), what="sam asked")[0]
    assert req["actionable"] and "venmo.com/u/alex" in req["summary"]
    wait_for(lambda: items(priya, "money"), what="priya asked")
    sam.answer_entry(req["ref"], accept=True)
    priya.answer_entry(items(priya, "money")[0]["ref"], accept=False, note="I only had a salad")
    wait_for(lambda: any("disputed" in i["summary"] for i in items(alex, "money")), what="dispute noticed")
    bal = {b["contact"]: b for b in alex.balances()}
    assert bal["sam"]["balance_cents"] == 3000 and bal["priya"]["balance_cents"] == 0
    assert {b["contact"]: b for b in sam.balances()}["alex"]["balance_cents"] == -3000
    sam.record_payment("alex", "30")
    pay = wait_for(lambda: [i for i in items(alex, "money") if "paid you" in i["summary"]], what="payment claim")[0]
    alex.answer_entry(pay["ref"], accept=True)
    wait_for(lambda: {b["contact"]: b for b in sam.balances()}["alex"]["balance_cents"] == 0, what="settled at sam")
    assert {b["contact"]: b for b in alex.balances()}["sam"]["balance_cents"] == 0


def test_explicit_shares_and_grant(net):
    alex, sam = net.node("alex"), net.node("sam")
    net.pair(alex, sam, b_grants="plans")  # no money grant
    with pytest.raises(NodeError, match="more than the total"):
        alex.add_expense("Tickets", "50", ["sam"], shares={"sam": "60"})
    alex.add_expense("Tickets", "50", ["sam"], shares={"sam": "20"})
    wait_for(lambda: items(alex, "delivery.failed"), what="refused without grant")
    assert not sam.ledger()


def test_eta_and_location_are_opt_in_and_ephemeral(net):
    alex, sam, priya = net.node("alex"), net.node("sam"), net.node("priya")
    net.pair(alex, sam, b_grants="plans,location")
    net.pair(alex, priya, b_grants="plans")  # priya doesn't accept location
    plan = alex.create_plan("Dinner", ["sam"], slots=[slot(20, 19)])
    wait_for(lambda: sam.store.plan(plan["id"]), what="plan")
    alex.share_status(plan_id=plan["id"], text="leaving now", eta_minutes=15, lat=41.88, lon=-87.63, accuracy_m=30)
    p = wait_for(lambda: sam.presence(), what="presence")[0]
    assert p["eta_minutes"] == 15 and "openstreetmap.org" in p["summary"] and "Dinner" in p["summary"]
    alex.share_status(["sam"], text="parking", eta_minutes=2)
    wait_for(lambda: sam.presence()[0]["eta_minutes"] == 2, what="update replaces")
    assert len([i for i in items(sam, "presence")]) == 1  # one live item per person
    assert sam.presence()[0]["lat"] is None  # new update without a location clears the old one
    alex.share_status(["priya"], text="hi")
    wait_for(lambda: items(alex, "delivery.failed"), what="priya refuses")
    with pytest.raises(NodeError):
        alex.share_status(["sam"], lat=200, lon=0)
    alex.stop_sharing(["sam"])
    wait_for(lambda: not sam.presence(), what="cleared")
    # expiry: nothing is kept after it lapses
    alex.share_status(["sam"], text="brb", ttl_minutes=1)
    wait_for(lambda: sam.presence(), what="brb")
    sam.clock = lambda: alex.now() + 120
    assert sam.presence() == []
