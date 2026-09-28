"""MCP server so any MCP-capable agent (Claude, Codex, Gemini CLI, Cursor, VS Code,
Goose, Hermes, OpenClaw, ...) can drive a node.

    confer mcp                              # stdio (most clients)
    confer mcp --http --port 3069           # streamable HTTP on 127.0.0.1 (local clients)

Tools come from the single catalog in :mod:`confer.tools`.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from .node import Node
from .tools import INSTRUCTIONS, TOOLS, Tool, ToolError, call


def _server_class() -> Any:
    """MCP SDK 2.x renamed FastMCP to MCPServer; support both."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP

            return FastMCP
        except ImportError as exc:
            raise SystemExit("the MCP server needs: pip install 'confer[mcp]'") from exc


def _bind(node: Node, t: Tool) -> Any:
    """A function with the tool's signature minus ``node``, which the MCP SDK
    introspects to build the schema; calls go through tools.call (validation)."""
    hints = t.hints()

    @functools.wraps(t.fn)
    def bound(**kwargs: Any) -> Any:
        try:
            return call(node, t.name, kwargs)
        except ToolError as exc:
            raise ValueError(str(exc)) from exc

    params = [p.replace(annotation=hints.get(p.name, Any), kind=inspect.Parameter.KEYWORD_ONLY) for p in t.params]
    bound.__signature__ = inspect.Signature(params, return_annotation=hints.get("return", Any))  # type: ignore[attr-defined]
    bound.__annotations__ = {p.name: p.annotation for p in params} | {"return": hints.get("return", Any)}
    del bound.__wrapped__  # stop inspect from following back to the node-taking original
    bound.__doc__ = t.full_description()
    return bound


def build(node: Node) -> Any:
    mcp = _server_class()("confer", instructions=INSTRUCTIONS)
    for t in TOOLS.values():
        mcp.tool(name=t.name, description=t.full_description())(_bind(node, t))
    return mcp


def run(node: Node, *, http: bool = False, host: str = "127.0.0.1", port: int = 3069) -> None:
    server = build(node)
    if not http:
        server.run()
        return
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("MCP over HTTP has no authentication here; it only binds to localhost. "
                         "For remote agents use the REST API (confer api enable) behind TLS.")
    try:
        server.run("streamable-http", host=host, port=port)
    except TypeError:  # older SDKs take host/port from settings
        server.settings.host, server.settings.port = host, port
        server.run("streamable-http")
