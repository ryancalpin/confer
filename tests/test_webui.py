"""Tests for the mobile-first web inbox (src/confer/webui.py, /ui/ routes)."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

import pytest

from confer.server import ConferServer

from .conftest import slot, wait_for


# ------------------------------------------------------------------ helpers


def base_url(node) -> str:
    return node.config["endpoint"]


def ui_token(node) -> str:
    return node.config["ui_token"]


def get_ui(node, token=None) -> bytes:
    """Fetch the UI page; uses the correct token by default."""
    tok = token if token is not None else ui_token(node)
    url = f"{base_url(node)}/ui/{tok}"
    return urllib.request.urlopen(url).read()


def post_act(node, fields: dict, token=None) -> urllib.request.Request:
    """POST to /ui/<token>/act with the given form fields."""
    tok = token if token is not None else ui_token(node)
    # Add the hidden 't' defense-in-depth field unless caller overrides
    if "t" not in fields:
        fields = {**fields, "t": tok}
    data = urllib.parse.urlencode(fields).encode()
    url = f"{base_url(node)}/ui/{tok}/act"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return req


def send_act(node, fields: dict, token=None) -> bytes:
    """Send POST /act and follow redirect (urllib does NOT follow 303, so we do it ourselves)."""
    tok = token if token is not None else ui_token(node)
    req = post_act(node, fields, token=tok)
    try:
        resp = urllib.request.urlopen(req)
        return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303):
            # Follow redirect to the UI page
            loc = e.headers.get("Location", "")
            if not loc.startswith("http"):
                loc = base_url(node) + loc
            return urllib.request.urlopen(loc).read()
        raise


# ------------------------------------------------------------------ tests


def test_ui_page_renders_with_correct_token(net):
    node = net.node("alice")
    body = get_ui(node)
    assert b"<!doctype html" in body.lower()
    assert b"Confer" in body
    assert node.name.encode() in body


def test_ui_page_404_with_wrong_token(net):
    node = net.node("alice")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get_ui(node, token="wrongtoken")
    assert exc.value.code == 404


def test_ui_security_headers_present(net):
    node = net.node("alice")
    tok = ui_token(node)
    url = f"{base_url(node)}/ui/{tok}"
    resp = urllib.request.urlopen(url)
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert "no-store" in headers.get("cache-control", "")
    assert "DENY" in headers.get("x-frame-options", "")
    assert "no-referrer" in headers.get("referrer-policy", "")
    csp = headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_xss_plan_title_escaped(net):
    """A malicious plan title must be HTML-escaped, not injected."""
    alice = net.node("alice")
    bob = net.node("bob")
    net.pair(alice, bob)
    evil_title = '<script>alert(1)</script>'
    alice.create_plan(evil_title, ["bob"], slots=[slot(4, 19)])
    wait_for(lambda: bob.plans(), what="plan at bob")
    body = get_ui(bob).decode("utf-8")
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_proposed_plan_shows_slots_and_accept_form(net):
    """A plan in 'proposed' state shows slot checkboxes and Accept/Decline buttons."""
    alice = net.node("alice")
    bob = net.node("bob")
    net.pair(alice, bob)
    alice.create_plan("Team Lunch", ["bob"], slots=[slot(5, 12), slot(6, 12)], location="Cafe Roma")
    wait_for(lambda: bob.plans(), what="plan at bob")
    body = get_ui(bob).decode("utf-8")
    assert "Team Lunch" in body
    assert "Cafe Roma" in body
    # Should have checkboxes for both slots
    assert body.count('name="slot"') == 2
    assert 'value="accept"' in body
    assert 'value="decline"' in body


def test_accept_plan_via_post_confirms_end_to_end(net):
    """POST /act with action=respond&decision=accept confirms the plan."""
    alice = net.node("alice")
    bob = net.node("bob")
    net.pair(alice, bob)
    plan = alice.create_plan("Dinner", ["bob"], slots=[slot(7, 19)])
    wait_for(lambda: bob.store.plan(plan["id"]), what="plan at bob")

    # Accept via web UI — slot 0 selected
    fields = {
        "action": "respond",
        "plan_id": plan["id"],
        "decision": "accept",
        "slot": "0",
    }
    send_act(bob, fields)

    wait_for(
        lambda: (p := alice.store.plan(plan["id"])) and p["status"] == "confirmed",
        what="alice sees confirmed",
    )
    bob_plan = bob.store.plan(plan["id"])
    assert bob_plan["status"] == "confirmed"


def test_dismiss_inbox_item(net):
    """POST /act with action=dismiss closes the inbox item."""
    alice = net.node("alice")
    bob = net.node("bob")
    net.pair(alice, bob)
    # Pairing creates a pair item in alice's inbox
    items = wait_for(lambda: alice.inbox(), what="alice inbox")
    item = items[0]
    item_id = item["id"]

    send_act(alice, {"action": "dismiss", "item_id": str(item_id)})

    # Item should now be done
    remaining = [i for i in alice.inbox() if i["id"] == item_id]
    assert not remaining


def test_ui_token_auto_generated_and_saved(net):
    """ConferServer generates a ui_token if the node config doesn't have one."""
    node = net.node("alice")
    token = node.config.get("ui_token", "")
    assert token, "ui_token should have been generated"
    assert len(token) >= 20


def test_ui_url_property(net):
    """ConferServer.ui_url returns the correct secret URL."""
    alice = net.node("alice")
    srv = net.servers[0]
    url = srv.ui_url
    token = alice.config["ui_token"]
    assert url.endswith(f"/ui/{token}")
    assert url.startswith("http")


def test_introduction_can_be_approved_from_the_phone(net):
    import urllib.parse
    import urllib.request

    from .conftest import wait_for

    hub, ann, ben = net.node("hub"), net.node("ann"), net.node("ben")
    net.pair(hub, ann, b_grants="intros")
    net.pair(hub, ben, b_grants="intros")
    iid = hub.introduce("ann", "ben")
    wait_for(lambda: ann.store.intro(iid) and ben.store.intro(iid), what="offers")
    ben.accept_intro(iid)
    token = ann.config["ui_token"]
    page = urllib.request.urlopen(f"{ann.config['endpoint']}/ui/{token}").read().decode()
    assert "Meet ben?" in page and iid in page
    data = urllib.parse.urlencode({"t": token, "action": "intro", "intro_id": iid, "decision": "accept"}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{ann.config['endpoint']}/ui/{token}/act", data=data))
    wait_for(lambda: (c := ann.store.contact(ben.identity.agent_id)) and c.status == "active", what="connected")
