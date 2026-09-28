import json

from confer.cli import main

from .conftest import wait_for


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_pair_plan_and_inbox(net, capsys):
    alice, bob = net.node("alice", tz="America/Chicago"), net.node("bob", tz="America/Chicago")
    ah, bh = str(alice.home), str(bob.home)
    code, out, _ = run(capsys, "--home", ah, "invite", "Bob", "--grant", "plans,notes")
    token = next(line for line in out.splitlines() if line.startswith("confer1:"))
    assert code == 0
    assert run(capsys, "--home", bh, "accept", token, "--name", "Alice")[0] == 0
    wait_for(lambda: (c := bob.store.contact(alice.identity.agent_id)) and c.status == "active", what="paired")
    code, out, _ = run(capsys, "--home", ah, "plan", "new", "Dinner", "--with", "Bob", "--slot", "2031-03-04T18:00/90", "--slot", "2031-03-05T18:00/90")
    assert code == 0 and "option 2: Wed Mar 05 18:00-19:30" in out
    wait_for(lambda: bob.plans(), what="plan at bob")
    code, out, _ = run(capsys, "--home", bh, "--json", "inbox")
    invite = [i for i in json.loads(out) if i["kind"] == "plan.invite"][0]
    assert run(capsys, "--home", bh, "plan", "respond", invite["ref"], "accept", "--slots", "2")[0] == 0
    wait_for(lambda: alice.store.plan(invite["ref"])["status"] == "confirmed", what="confirmed")
    code, out, _ = run(capsys, "--home", ah, "plan", "show", invite["ref"])
    assert "WHEN: Wed Mar 05 18:00-19:30" in out


def test_cli_errors_are_friendly(net, capsys):
    alice = net.node("alice")
    ah = str(alice.home)
    code, _, err = run(capsys, "--home", ah, "plan", "new", "X", "--with", "nobody", "--slot", "2031-03-04T18:00")
    assert code == 1 and "no contact named 'nobody'" in err
    code, _, err = run(capsys, "--home", ah, "config", "endpoint", "ftp://x")
    assert code == 1 and "bad URL" in err
    code, _, err = run(capsys, "--home", ah, "grant", "nobody", "+files")
    assert code == 1
    code, _, err = run(capsys, "--home", ah + "-missing", "whoami")
    assert code == 1 and "confer init" in err


def test_cli_introductions_and_rotation(net, capsys):
    hub, ann, ben = net.node("hub"), net.node("ann"), net.node("ben")
    net.pair(hub, ann, b_grants="intros")
    net.pair(hub, ben, b_grants="intros")
    assert run(capsys, "--home", str(hub.home), "introduce", "ann", "ben", "--note", "neighbours")[0] == 0
    wait_for(lambda: ann.intros() and ben.intros(), what="offers")
    iid = ann.intros()[0]["intro_id"]
    code, out, _ = run(capsys, "--home", str(ann.home), "intro", "list")
    assert iid in out and "offered" in out
    for n in (ann, ben):
        assert run(capsys, "--home", str(n.home), "intro", "accept", iid)[0] == 0
    wait_for(lambda: (c := ben.store.contact(ann.identity.agent_id)) and c.status == "active", what="connected")
    assert run(capsys, "--home", str(ann.home), "rotate-key")[0] == 1  # needs --yes
    code, out, _ = run(capsys, "--home", str(ann.home), "rotate-key", "--yes")
    assert code == 0 and "New key" in out
    # the CLI used its own Node instance on ann's home; the key moved on disk
    from confer.identity import Identity

    new_id = Identity.load(ann.home / "identity.key").agent_id
    wait_for(lambda: ben.store.contact(new_id), what="ben follows the rotation")
    # ann's long-running server process picks the new key up from disk
    ben.send_note("ann", "still there?")
    wait_for(lambda: [i for i in ann.inbox() if i["kind"] == "note"], what="note to the rotated key")
    assert ann.identity.agent_id == new_id


def test_cli_lists_money_and_share(net, capsys):
    alex, sam = net.node("alex"), net.node("sam")
    net.pair(alex, sam, b_grants="plans,lists,money,location")
    ah, sh = str(alex.home), str(sam.home)
    code, out, _ = run(capsys, "--home", ah, "list", "new", "Camping", "--with", "sam", "--item", "tent", "--item", "stove")
    assert code == 0 and "1. [ ] tent" in out
    lid = alex.lists()[0]["id"]
    wait_for(lambda: sam.store.get_list(lid), what="list at sam")
    assert run(capsys, "--home", sh, "list", "claim", lid, "2")[0] == 0
    wait_for(lambda: alex.get_list(lid)["items"][1]["claimed_name"] == "sam", what="claimed")
    code, out, _ = run(capsys, "--home", ah, "list", "show", lid)
    assert "stove  ← sam" in out
    code, out, _ = run(capsys, "--home", ah, "money", "split", "Campsite", "60", "--with", "sam")
    assert code == 0 and "30.00 USD" in out
    wait_for(lambda: sam.ledger(), what="request at sam")
    assert run(capsys, "--home", sh, "money", "accept", sam.ledger()[0]["id"])[0] == 0
    wait_for(lambda: alex.balances() and alex.balances()[0]["balance_cents"] == 3000, what="balance")
    code, out, _ = run(capsys, "--home", ah, "money", "balances")
    assert "sam" in out and "owes you 30.00 USD" in out
    assert run(capsys, "--home", ah, "share", "--to", "sam", "--text", "on my way", "--eta", "20")[0] == 0
    wait_for(lambda: sam.presence(), what="status")
    code, out, _ = run(capsys, "--home", sh, "presence")
    assert "on my way" in out and "ETA 20 min" in out
    assert run(capsys, "--home", ah, "config", "pay_link", "https://venmo.com/u/alex")[0] == 0
    assert run(capsys, "--home", ah, "config", "currency", "euro")[0] == 1
