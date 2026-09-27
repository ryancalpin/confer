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
