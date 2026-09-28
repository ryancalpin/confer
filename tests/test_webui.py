"""Tests for the mobile-first web inbox (src/confer/webui.py, /ui/ routes)."""

from __future__ import annotations

import html as _html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from confer.node import DEFAULT_GRANTS
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
    # the confirmation reaches bob asynchronously
    wait_for(lambda: bob.store.plan(plan["id"])["status"] == "confirmed", what="bob sees confirmed")


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


# ------------------------------------------------------------ full management UI


def get_tab(node, tab: str, token=None) -> str:
    tok = token if token is not None else ui_token(node)
    return urllib.request.urlopen(f"{base_url(node)}/ui/{tok}?tab={urllib.parse.quote(tab)}").read().decode()


def act(node, **fields) -> str:
    return send_act(node, fields).decode()


def act_multi(node, pairs: list[tuple[str, str]]) -> str:
    """POST a form with repeated fields (checkbox groups), following the redirect."""
    tok = ui_token(node)
    data = urllib.parse.urlencode([("t", tok), *pairs]).encode()
    req = urllib.request.Request(f"{base_url(node)}/ui/{tok}/act", data=data)
    try:
        return urllib.request.urlopen(req).read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == 303:
            return urllib.request.urlopen(base_url(node) + exc.headers["Location"]).read().decode()
        raise


def post_multipart(node, fields: dict, filename: str, data: bytes, token=None):
    tok = token if token is not None else ui_token(node)
    boundary = "----confer-test-boundary-7d3f"
    chunks = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
              for k, v in {"t": tok, **fields}.items()]
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{base_url(node)}/ui/{tok}/act", data=b"".join(chunks),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        return urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        if exc.code == 303:
            return urllib.request.urlopen(base_url(node) + exc.headers["Location"])
        raise


def banner_error(page: str) -> str:
    m = re.search(r'<div class="banner error"[^>]*>(.*?)</div>', page, re.S)
    return _html.unescape(m.group(1)) if m else ""


def saved_config(node) -> dict:
    return json.loads((node.home / "config.json").read_text())


@pytest.mark.parametrize("tab", ["inbox", "plans", "lists", "money", "people", "settings"])
def test_every_tab_renders(net, tab):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    alice.create_plan("Dinner", ["bob"], slots=[slot(4, 19)])
    alice.create_list("BBQ", ["bob"], items=["buns"])
    alice.add_expense("Pizza", "30", ["bob"])
    wait_for(lambda: bob.plans() and bob.lists() and bob.ledger(), what="bob has data")
    for node in (alice, bob):
        page = get_tab(node, tab)
        assert f'?tab={tab}" aria-current="page"' in page
        assert "<nav" in page and not banner_error(page)


def test_unknown_tab_falls_back_to_inbox(net):
    alice = net.node("alice")
    page = get_tab(alice, "<script>")
    assert "<h1>Inbox</h1>" in page and "?tab=<script>" not in page


def test_badges_count_actionable_items_and_money_requests(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    alice.add_expense("Pizza", "30", ["bob"])
    wait_for(lambda: bob.ledger(), what="request at bob")
    page = get_tab(bob, "inbox")
    assert re.search(r'tab=money"><svg[^>]*>.*?</svg><span class="count">1</span>', page, re.S)
    assert re.search(r'tab=inbox" aria-current="page"><svg[^>]*>.*?</svg><span class="count">1</span>', page, re.S)


def test_csp_has_a_nonce_matching_the_script_tag(net):
    alice = net.node("alice")
    resp = urllib.request.urlopen(f"{base_url(alice)}/ui/{ui_token(alice)}?tab=people")
    csp = resp.headers["Content-Security-Policy"]
    page = resp.read().decode()
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in page
    for part in ("default-src 'none'", "img-src data:", "form-action 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert part in csp
    assert page.count("<script") == 1 and " onclick=" not in page and " onload=" not in page
    again = urllib.request.urlopen(f"{base_url(alice)}/ui/{ui_token(alice)}").headers["Content-Security-Policy"]
    assert again != csp  # a fresh nonce per response


def test_settings_update_persists_and_is_validated(net):
    alice = net.node("alice")
    alice.config["notify_cmd"] = "notify-send confer"
    alice.save_config()
    page = get_tab(alice, "settings")
    assert 'name="notify_cmd"' not in page and "notify-send confer" in page  # read-only
    assert f"/calendar/{alice.config['feed_token']}.ics" in page
    good = {"action": "save_settings", "tab": "settings", "name": "Alice A", "endpoint": alice.config["endpoint"], "relay": "",
            "tz": "Europe/London", "hours_start": "09:00", "hours_end": "21:30", "buffer_minutes": "15",
            "calendar": "webcal://cal.example.com/me.ics", "tentative_holds": "1", "currency": "eur",
            "pay_link": "https://pay.example.com/alice", "notify_webhook": "", "nudge_after_hours": "12",
            "notify_cmd": "touch /tmp/pwned"}  # must be ignored
    page = act(alice, **good)
    assert not banner_error(page) and "Settings saved" in page
    cfg = saved_config(alice)
    assert cfg["name"] == "Alice A" and cfg["tz"] == "Europe/London" and cfg["hours_end"] == "21:30"
    assert cfg["buffer_minutes"] == 15 and cfg["currency"] == "EUR" and cfg["calendar"].startswith("webcal://")
    assert cfg["notify_cmd"] == "notify-send confer" and cfg["nudge_after_hours"] == 12
    for key, bad in (("calendar", "/etc/passwd"), ("calendar", "file:///etc/passwd"), ("calendar", "~/cal.json"),
                     ("tz", "Mars/Olympus"), ("hours_start", "25:00"), ("buffer_minutes", "999"),
                     ("pay_link", "http://insecure.example.com"), ("pay_link", "javascript:alert(1)"),
                     ("notify_webhook", "ftp://x"), ("currency", "EURO"), ("endpoint", "not a url")):
        page = act(alice, **{**good, key: bad})
        assert banner_error(page), f"{key}={bad!r} was accepted"
    cfg = saved_config(alice)
    assert cfg["calendar"] == "webcal://cal.example.com/me.ics" and cfg["tz"] == "Europe/London"
    act(alice, **{k: v for k, v in good.items() if k != "tentative_holds"})
    assert saved_config(alice)["tentative_holds"] is False  # unticked checkbox = off


def test_settings_keep_a_cli_configured_local_calendar(net):
    alice = net.node("alice")
    alice.config["calendar"] = "/home/alice/busy.json"
    alice.save_config()
    fields = {"action": "save_settings", "name": "alice", "endpoint": alice.config["endpoint"], "tz": "UTC",
              "hours_start": "08:00", "hours_end": "22:00", "calendar": "/home/alice/busy.json", "currency": "USD"}
    assert not banner_error(act(alice, **fields))
    assert alice.config["calendar"] == "/home/alice/busy.json"
    assert banner_error(act(alice, **{**fields, "calendar": "/home/alice/other.json"}))


def test_grants_edit_via_form(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    bob_id = bob.identity.agent_id
    page = get_tab(alice, "people")
    assert "Status &amp; location" in page and "Sensitive" in page
    act_multi(alice, [("action", "set_grants"), ("tab", "people"), ("contact", bob_id), ("grant", "notes"), ("grant", "location")])
    assert alice.store.contact(bob_id).grants == ["location", "notes"]
    page = act_multi(alice, [("action", "set_grants"), ("contact", bob_id), ("grant", "superpowers")])
    assert "unknown grant" in banner_error(page)
    assert banner_error(act(alice, action="remove_contact", contact=bob_id))  # needs the confirm box
    act(alice, action="remove_contact", contact=bob_id, confirm="1")
    assert alice.store.contact(bob_id) is None


def test_invite_created_in_ui_is_accepted_via_the_other_ui(net):
    alice, bob = net.node("alice"), net.node("bob")
    page = act_multi(alice, [("action", "create_invite"), ("tab", "people"), ("name", "Bob"),
                             *[("grant", g) for g in DEFAULT_GRANTS]])
    m = re.search(r'<textarea id="invite-token"[^>]*>([^<]+)</textarea>', page)
    assert m, "invite token not shown"
    token = _html.unescape(m.group(1))
    assert token.startswith("confer1:") and 'data-copy="invite-token"' in page and 'data-share="invite-token"' in page
    wrapped = token[:40] + "\n" + token[40:]  # phones wrap long pastes
    act_multi(bob, [("action", "accept_invite"), ("token", wrapped), ("name", "Alice"), ("grant", "plans"), ("grant", "notes")])
    wait_for(lambda: (c := bob.store.contact(alice.identity.agent_id)) and c.status == "active", what="bob paired")
    wait_for(lambda: alice.store.contact(bob.identity.agent_id), what="alice paired")
    assert alice.store.contact(bob.identity.agent_id).grants == sorted(DEFAULT_GRANTS)
    assert bob.store.contact(alice.identity.agent_id).grants == ["notes", "plans"]
    assert "Alice" in get_tab(bob, "people")


def test_list_create_add_claim_via_ui_reaches_member(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    act_multi(alice, [("action", "create_list"), ("title", "Picnic"), ("contact", bob.identity.agent_id), ("items", "bread\r\ncheese\r\n")])
    lst = wait_for(lambda: bob.lists(), what="list at bob")[0]
    assert [i["text"] for i in lst["items"]] == ["bread", "cheese"]
    act(bob, action="list_op", list_id=lst["id"], op="add", text="grapes")
    wait_for(lambda: len(alice.get_list(lst["id"])["items"]) == 3, what="owner got add")
    bread = next(i for i in alice.get_list(lst["id"])["items"] if i["text"] == "bread")

    def item_at(node):
        return next(i for i in node.get_list(lst["id"])["items"] if i["id"] == bread["id"])

    act(bob, action="list_op", list_id=lst["id"], op="claim", item=bread["id"])
    wait_for(lambda: item_at(alice)["claimed_name"] == "bob", what="claim at owner")
    act(alice, action="list_op", list_id=lst["id"], op="check", item=bread["id"])
    wait_for(lambda: item_at(bob)["done"] and len(bob.get_list(lst["id"])["items"]) == 3, what="changes reach member")
    page = get_tab(bob, "lists")
    assert "bob is bringing it" in page and "grapes" in page
    assert banner_error(act(alice, action="delete_list", list_id=lst["id"]))  # needs confirm
    act(alice, action="delete_list", list_id=lst["id"], confirm="1")
    wait_for(lambda: not bob.lists(), what="closed for bob")


def test_expense_via_ui_creates_request_and_accept_via_ui_updates_balances(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    alice.config["pay_link"] = "https://pay.example.com/alice"
    alice.save_config()
    act_multi(alice, [("action", "add_expense"), ("title", "Groceries"), ("amount", "30.00"), ("currency", "USD"),
                      ("contact", bob.identity.agent_id), ("include_me", "1"), ("note", "")])
    item = wait_for(lambda: [i for i in bob.inbox() if i["kind"] == "money" and i["actionable"]], what="request at bob")[0]
    page = get_tab(bob, "inbox")
    assert 'value="answer_entry"' in page and 'href="https://pay.example.com/alice"' in page
    act(bob, action="answer_entry", entry_id=item["ref"], decision="accept", tab="inbox")
    wait_for(lambda: alice.balances() and alice.balances()[0]["balance_cents"] == 1500, what="alice balance")
    assert bob.balances()[0]["balance_cents"] == -1500
    assert "owes you" in get_tab(alice, "money") and "You owe" in get_tab(bob, "money")
    act(bob, action="record_payment", contact=alice.identity.agent_id, amount="5", currency="USD")
    entry = next(x for x in bob.ledger() if x["kind"] == "settle")
    act(bob, action="cancel_entry", entry_id=entry["id"])
    assert next(x for x in bob.ledger() if x["id"] == entry["id"])["status"] == "cancelled"


def test_share_status_via_ui_reaches_contact_that_granted_location(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob, b_grants="notes,location")  # bob accepts status/location from alice
    act_multi(alice, [("action", "share_status"), ("text", "Leaving now"), ("eta", "15"), ("contact", bob.identity.agent_id),
                      ("lat", "51.50135"), ("lon", "-0.14189"), ("accuracy", "12"), ("plan_id", "")])
    got = wait_for(lambda: bob.presence(), what="presence at bob")[0]
    assert got["text"] == "Leaving now" and got["eta_minutes"] == 15 and got["lat"] == 51.50135
    page = get_tab(bob, "inbox")
    assert '<a href="https://www.openstreetmap.org/?mlat=51.50135' in page
    act_multi(alice, [("action", "stop_sharing"), ("contact", bob.identity.agent_id)])
    wait_for(lambda: not bob.presence(), what="cleared")


def test_note_reply_via_ui(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    act(alice, action="send_note", contact="bob", text="Pizza or tacos?", question="1")
    item = wait_for(lambda: [i for i in bob.inbox() if i["kind"] == "note"], what="note at bob")[0]
    assert 'value="reply_note"' in get_tab(bob, "inbox")
    act(bob, action="reply_note", item_id=str(item["id"]), text="Tacos")
    wait_for(lambda: alice.replies(item["payload"]["msg_id"]), what="reply at alice")
    assert not [i for i in bob.inbox() if i["id"] == item["id"]]


def test_file_upload_via_ui_arrives_and_downloads(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    data = bytes(range(256)) * 40 + b"\r\n--" + b"\r\n\r\n\n\r" + bytes(range(255, -1, -1))
    resp = post_multipart(alice, {"action": "send_file", "tab": "inbox", "contact": bob.identity.agent_id, "note": "the doc"},
                          "report v2.pdf", data)
    assert "File sent" in resp.read().decode()
    assert not [p for p in (alice.home / "tmp").rglob("*") if p.is_file()]  # temp copy removed
    item = wait_for(lambda: [i for i in bob.inbox() if i["kind"] == "file"], what="file at bob")[0]
    assert (bob.home / "files").resolve() in Path(item["payload"]["path"]).resolve().parents
    page = get_tab(bob, "inbox")
    href = _html.unescape(re.search(r'href="(/ui/[^"]+/file\?path=[^"]+)"', page).group(1))
    got = urllib.request.urlopen(base_url(bob) + href)
    assert got.read() == data
    assert got.headers["Content-Disposition"].startswith("attachment")
    assert got.headers["X-Content-Type-Options"] == "nosniff"


def test_uploaded_html_downloads_as_octet_stream(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    post_multipart(alice, {"action": "send_file", "contact": "bob"}, "page.html", b"<script>alert(1)</script>")
    item = wait_for(lambda: [i for i in bob.inbox() if i["kind"] == "file"], what="file at bob")[0]
    rel = Path(item["payload"]["path"]).resolve().relative_to((bob.home / "files").resolve()).as_posix()
    got = urllib.request.urlopen(f"{base_url(bob)}/ui/{ui_token(bob)}/file?path={urllib.parse.quote(rel)}")
    assert got.headers["Content-Type"] == "application/octet-stream"


def test_file_download_refuses_path_traversal(net):
    bob = net.node("bob")
    (bob.home / "files" / "x").mkdir(parents=True)
    (bob.home / "files" / "x" / "ok.txt").write_text("fine")
    base = f"{base_url(bob)}/ui/{ui_token(bob)}/file?path="
    assert urllib.request.urlopen(base + "x/ok.txt").read() == b"fine"
    for evil in ("../config.json", "../identity.key", "x/../../config.json", "/etc/passwd", "..%2fconfig.json", "", "x"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + (evil if "%" in evil else urllib.parse.quote(evil, safe="")))
        assert exc.value.code == 404, evil
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base_url(bob)}/ui/wrongtoken/file?path=x/ok.txt")


def test_multipart_is_only_accepted_for_file_upload(net):
    alice = net.node("alice")
    with pytest.raises(urllib.error.HTTPError) as exc:
        post_multipart(alice, {"action": "save_settings", "name": "x"}, "a.txt", b"hi")
    assert exc.value.code == 400
    big = urllib.parse.urlencode({"t": ui_token(alice), "action": "send_note", "text": "x" * (70 * 1024)}).encode()
    with pytest.raises(urllib.error.HTTPError) as exc:  # urlencoded forms stay capped at 64 KB
        urllib.request.urlopen(urllib.request.Request(f"{base_url(alice)}/ui/{ui_token(alice)}/act", data=big))
    assert exc.value.code == 413


def test_post_requires_the_hidden_token(net):
    alice = net.node("alice")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(post_act(alice, {"action": "rotate_ui", "confirm": "1", "t": "nope"}))
    assert exc.value.code == 403


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_redirects_stay_on_known_internal_tabs(net):
    alice = net.node("alice")
    opener = urllib.request.build_opener(_NoRedirect)
    for tab in ("https://evil.example", "//evil.example", "settings"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(post_act(alice, {"action": "dismiss_all", "tab": tab}))
        assert exc.value.code == 303
        loc = exc.value.headers["Location"]
        assert loc.startswith(f"/ui/{ui_token(alice)}?tab=") and loc.split("tab=")[1].split("&")[0] in ("inbox", "settings")


def test_rotate_ui_link_invalidates_the_old_token(net):
    alice = net.node("alice")
    old = ui_token(alice)
    assert banner_error(act(alice, action="rotate_ui"))  # needs the confirm box
    assert ui_token(alice) == old
    page = act(alice, action="rotate_ui", confirm="1", tab="settings")
    new = ui_token(alice)
    assert new != old and "New link created" in page and saved_config(alice)["ui_token"] == new
    with pytest.raises(urllib.error.HTTPError) as exc:
        get_ui(alice, token=old)
    assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(post_act(alice, {"action": "dismiss_all", "t": old}, token=old))
    assert exc.value.code == 404
    assert b"<h1>Inbox</h1>" in get_ui(alice, token=new)


def test_plan_create_respond_with_preferences_and_cancel_via_ui(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    act_multi(alice, [("action", "create_plan"), ("title", "Climbing"), ("contact", bob.identity.agent_id), ("duration", "90"),
                      ("from", "2031-03-04"), ("to", "2031-03-06"), ("between_start", "18:00"), ("between_end", "21:00"),
                      ("location", "The Wall"), ("quorum", "all")])
    plan = wait_for(lambda: bob.plans(), what="plan at bob")[0]
    assert len(plan["slots"]) >= 2 and plan["location"] == "The Wall"
    assert "Climbing" in get_tab(bob, "plans")
    act_multi(bob, [("action", "respond"), ("tab", "plans"), ("plan_id", plan["id"]), ("decision", "accept"),
                    ("slot", "0"), ("slot", "1"), ("prefer", "1")])
    wait_for(lambda: alice.store.plan(plan["id"])["status"] == "confirmed", what="confirmed")
    assert alice.store.plan(plan["id"])["chosen"] == 1  # the preferred option wins
    act(alice, action="cancel_plan", plan_id=plan["id"], reason="rain")
    wait_for(lambda: bob.store.plan(plan["id"])["status"] == "cancelled", what="cancelled at bob")
    assert "pick at least one person" in banner_error(act(alice, action="create_plan", title="x"))


def test_cli_hints_are_hidden_but_user_text_is_kept():
    from confer.webui.pages import without_cli_hint

    assert without_cli_hint("💵 bob says they paid you 5.00 USD. Confirm: confer money accept ab12 (or dispute)") == \
        "💵 bob says they paid you 5.00 USD."
    assert without_cli_hint("💬 bob asks: hi? (reply: confer note 'bob' \"...\" --reply-to 9f2e)") == "💬 bob asks: hi?"
    assert without_cli_hint("💬 bob says: let's confer plan details later") == "💬 bob says: let's confer plan details later"


def test_xss_in_list_items_and_notes_is_escaped(net):
    alice, bob = net.node("alice"), net.node("bob")
    net.pair(alice, bob)
    evil = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    alice.create_list(evil, ["bob"], items=[evil])
    alice.send_note("bob", evil)
    wait_for(lambda: bob.lists() and [i for i in bob.inbox() if i["kind"] == "note"], what="evil at bob")
    for tab in ("inbox", "lists"):
        page = get_tab(bob, tab)
        assert "<script>alert" not in page and "<img src=x" not in page
        assert "&lt;script&gt;alert" in page
