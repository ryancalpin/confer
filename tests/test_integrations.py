"""Tool catalog, exports, REST API, Python client and MCP (stdio)."""

import json
import sys
import urllib.error
import urllib.request

import pytest

from confer import tools as T
from confer.client import ConferAPIError, ConferClient

from .conftest import wait_for


def test_catalog_is_complete_and_exports_every_format():
    assert len(T.TOOLS) >= 36
    for t in T.TOOLS.values():
        assert t.description and t.name.startswith("confer_")
        s = t.input_schema()
        assert s["type"] == "object" and set(s.get("required", [])) <= set(s["properties"])
    n = len(T.TOOLS)
    assert len(T.export("openai")) == n and len(T.export("anthropic")) == n and len(T.export("mcp")) == n
    gem = T.export("gemini")[0]["functionDeclarations"]
    assert len(gem) == n and "additionalProperties" not in json.dumps(gem)
    spec = T.export("openapi", "https://x.example")
    assert len(spec["paths"]) == n and spec["servers"][0]["url"] == "https://x.example"
    risky = {t.name for t in T.TOOLS.values() if t.human_ok}
    assert {"confer_answer_money", "confer_share_status", "confer_respond", "confer_intro_decide"} <= risky
    assert all(d["description"].startswith("[needs the human's OK]") for d in T.export("anthropic") if d["name"] in risky)


def test_call_validates_arguments(tmp_path):
    from confer.node import Node

    node = Node.init(tmp_path / "n", "Alex")
    with pytest.raises(T.ToolError, match="unknown argument"):
        T.call(node, "confer_whoami", {"x": 1})
    with pytest.raises(T.ToolError, match="missing"):
        T.call(node, "confer_dismiss", {})
    with pytest.raises(T.ToolError, match="must be integer"):
        T.call(node, "confer_dismiss", {"item_id": "7"})
    with pytest.raises(T.ToolError, match="must be string"):
        T.call(node, "confer_create_list", {"title": "x", "with_contacts": [1]})
    with pytest.raises(T.ToolError, match="CLI"):
        T.call(node, "confer_update_settings", {"changes": {"notify_cmd": "sh -c evil"}})
    with pytest.raises(T.ToolError, match="local files"):
        T.call(node, "confer_update_settings", {"changes": {"calendar": "/etc/passwd"}})
    assert T.call(node, "confer_update_settings", {"changes": {"currency": "eur", "hours_end": "21:00"}})["currency"] == "EUR"


def _req(url, token=None, body=None, headers=None, method=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=h, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_rest_api_auth_approval_and_errors(net):
    alex = net.node("alex")
    base = alex.config["endpoint"]
    assert _req(base + "/api/v1/tools")[0] == 404  # off by default
    alex.config["api_token"] = "t0ken-for-tests"
    alex.save_config()
    assert _req(base + "/api/v1/tools")[0] == 401
    assert _req(base + "/api/v1/tools", "wrong")[0] == 401
    assert _req(base + "/api/v1/tools/confer_whoami", "wrong", {})[0] == 401
    code, out = _req(base + "/api/v1/tools", "t0ken-for-tests")
    assert code == 200 and any(t["name"] == "confer_trips" for t in out["result"])
    code, out = _req(base + "/api/v1/tools/confer_whoami", "t0ken-for-tests", {})
    assert code == 200 and out["result"]["name"] == "alex"
    code, out = _req(base + "/api/v1/tools/confer_update_settings", "t0ken-for-tests", {"changes": {"currency": "GBP"}})
    assert code == 428 and "Confer-Human-Approved" in out["error"]
    code, out = _req(base + "/api/v1/tools/confer_update_settings", "t0ken-for-tests", {"changes": {"currency": "GBP"}},
                     {"Confer-Human-Approved": "true"})
    assert code == 200 and alex.config["currency"] == "GBP"
    code, out = _req(base + "/api/v1/tools/confer_create_list", "t0ken-for-tests", {"title": "x", "with_contacts": ["nobody"]})
    assert code == 400 and "no contact named" in out["error"]
    code, out = _req(base + "/api/v1/tools/nope", "t0ken-for-tests", {})
    assert code == 404
    code, spec = _req(base + "/api/v1/openapi.json")
    assert code == 200 and spec["openapi"].startswith("3.1")


def test_client_drives_a_trip_between_two_nodes(net):
    alex, sam = net.node("alex"), net.node("sam")
    net.pair(alex, sam)
    for n in (alex, sam):
        n.config["api_token"] = f"tok-{n.name}"
        n.save_config()
    a = ConferClient(alex.config["endpoint"], "tok-alex")
    s = ConferClient(sam.config["endpoint"], "tok-sam")
    trip = a.call("confer_create_trip", title="Tahoe", with_contacts=["sam"], start_date="2031-03-20", end_date="2031-03-23")
    wait_for(lambda: s.call("confer_trips"), what="trip at sam via API")
    s.call("confer_trip_edit", trip_id=trip["trip_id"], op="traveler.set",
           args={"arrive": {"when": "2031-03-20T13:30", "how": "UA 1234", "needs_pickup": True}})
    wait_for(lambda: a.call("confer_trips")[0]["travelers"], what="arrival reached alex")
    assert a.call("confer_trips")[0]["travelers"][0]["arrive"]["how"] == "UA 1234"
    with pytest.raises(ConferAPIError) as exc:
        a.call("confer_split_expense", title="Cabin", amount="100", with_contacts=["sam"])
    assert exc.value.needs_approval
    a.call("confer_split_expense", approved=True, title="Cabin", amount="100", with_contacts=["sam"], plan_id=trip["trip_id"])
    wait_for(lambda: [e for e in s.call("confer_ledger") if e["needs_my_answer"]], what="request at sam")
    assert "needs_human_ok" in s.dispatch("confer_answer_money", {"entry_id": s.call("confer_ledger")[0]["id"], "accept": True})
    assert s.schemas("openai")[0]["type"] == "function" and s.schemas("anthropic")[0]["input_schema"]


def test_mcp_stdio_lists_every_tool(tmp_path):
    mcp = pytest.importorskip("mcp")
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from confer.node import Node

    Node.init(tmp_path / "n", "Alex").close()

    async def main():
        params = StdioServerParameters(command=sys.executable, args=["-m", "confer.cli", "--home", str(tmp_path / "n"), "mcp"])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                listed = await session.list_tools()
                res = await session.call_tool("confer_whoami", {})
                return {t.name for t in listed.tools}, res

    names, res = asyncio.run(main())
    assert names == set(T.TOOLS)
    assert "Alex" in res.content[0].text
    del mcp
