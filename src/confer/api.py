"""REST API for harnesses, automations and the iOS app.

Off until ``confer api enable`` sets ``api_token``. Every call needs
``Authorization: Bearer <api_token>``.

  GET  /api/v1/openapi.json        OpenAPI 3.1 for every tool (no token needed)
  GET  /api/v1/tools               tool list with JSON Schemas
  POST /api/v1/tools/<name>        run a tool; body = JSON arguments
                                   → 200 {"ok": true, "result": ...}
                                   → 400 {"ok": false, "error": "..."}

Tools flagged "needs the human's OK" also require the header
``Confer-Human-Approved: true``. Whoever wires Confer into an agent loop must
decide how that approval is obtained, instead of it happening by accident.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

from . import tools as T
from .lists import ListError
from .money import MoneyError
from .plans import PlanError
from .settings import SettingsError
from .trips import TripError

if TYPE_CHECKING:
    from .node import Node

log = logging.getLogger("confer.api")
PREFIX = "/api/v1"
APPROVAL_HEADER = "Confer-Human-Approved"
MAX_BODY = 1024 * 1024


def enabled(node: "Node") -> bool:
    return bool(node.config.get("api_token"))


def authorized(node: "Node", header: str | None) -> bool:
    token = str(node.config.get("api_token", ""))
    if not token or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip().encode(), token.encode())


def _json(code: int, obj: Any) -> tuple[int, bytes]:
    return code, json.dumps(obj, default=str).encode()


def handle(node: "Node", method: str, path: str, headers: Any, body: bytes, public_url: str) -> tuple[int, bytes]:
    """Route one API request. ``headers`` is an http.client.HTTPMessage-like mapping."""
    from .node import NodeError

    sub = path[len(PREFIX):] or "/"
    if method == "GET" and sub == "/openapi.json":
        return _json(200, T.openapi(public_url))
    if not authorized(node, headers.get("Authorization")):
        return _json(401, {"ok": False, "error": "missing or wrong API token"})
    if method == "GET" and sub == "/tools":
        return _json(200, {"ok": True, "result": [{"name": t.name, "description": t.full_description(), "needs_human_ok": t.human_ok,
                                                    "input_schema": t.input_schema()} for t in T.TOOLS.values()]})
    if method == "POST" and sub.startswith("/tools/"):
        name = sub[len("/tools/"):]
        tool = T.TOOLS.get(name)
        if tool is None:
            return _json(404, {"ok": False, "error": f"unknown tool {name!r}"})
        if tool.human_ok and str(headers.get(APPROVAL_HEADER, "")).lower() != "true":
            return _json(428, {"ok": False, "error": f"{name} needs the human's OK; after they agree, resend with header "
                                                     f"'{APPROVAL_HEADER}: true'"})
        try:
            args = json.loads(body or b"{}")
        except ValueError:
            return _json(400, {"ok": False, "error": "body must be a JSON object"})
        if not isinstance(args, dict):
            return _json(400, {"ok": False, "error": "body must be a JSON object"})
        try:
            return _json(200, {"ok": True, "result": T.call(node, name, args)})
        except (T.ToolError, NodeError, PlanError, ListError, MoneyError, TripError, SettingsError) as exc:
            return _json(400, {"ok": False, "error": str(exc) or exc.__class__.__name__})
        except (TypeError, ValueError, KeyError) as exc:  # bad argument shapes that slipped past the schema
            log.info("tool %s rejected input: %s", name, exc)
            return _json(400, {"ok": False, "error": "invalid arguments for this tool"})
        except Exception:
            log.exception("tool %s failed", name)
            return _json(500, {"ok": False, "error": "internal error — see the node's log"})
    return _json(404, {"ok": False, "error": "not found"})
