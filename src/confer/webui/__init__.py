"""Phone-first management UI for a Confer node.

Routes (all under a secret-token URL; the token lives in ``config.json`` as
``ui_token`` and can be rotated from the Settings tab):

  GET  /ui/<token>?tab=<tab>        one server-rendered page per tab:
                                    inbox · plans · lists · money · people · settings
  GET  /ui/<token>/file?path=<rel>  download a received file (only under <home>/files)
  POST /ui/<token>/act              form actions (``action=...``), answered with a
                                    303 back to the tab, or the page with an error banner

Security model: the token is the only credential, so every POST also carries it
in a hidden ``t`` field; all dynamic text is HTML-escaped; a strict CSP with a
per-response script nonce; no inline event handlers. Nothing reachable from the
web can make the node run a command or read a local file outside
``<home>/files`` (``notify_cmd`` and local calendar paths are CLI-only).

The page is self-contained (no external assets), so it loads on a phone over a
slow Tailscale connection with no dependency on the open internet.
"""

from __future__ import annotations

import email.parser
import email.policy
import hmac
import json
import logging
import mimetypes
import secrets
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from ..node import NodeError
from .actions import ACTIONS, NOTICES, Form, UIError
from .pages import TABS, Ctx, render

if TYPE_CHECKING:
    from ..node import Node

log = logging.getLogger("confer.webui")

MAX_FORM_BYTES = 64 * 1024  # urlencoded forms
MAX_UPLOAD_BYTES = 11 * 1024 * 1024  # multipart, file-upload action only (files are capped at 10 MB)
UPLOAD_ACTIONS = {"send_file"}

Response = tuple[int, list[tuple[str, str]], bytes]

# Sent with every UI response. The page itself also gets a script-src nonce (see security_headers).
UI_SECURITY_HEADERS = [
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
]
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'")


def security_headers(nonce: str = "") -> list[tuple[str, str]]:
    csp = _CSP + (f"; script-src 'nonce-{nonce}'" if nonce else "")
    return [("Content-Security-Policy", csp), *UI_SECURITY_HEADERS]


def token_ok(node: "Node", request_token: str) -> bool:
    """Constant-time check of the token in the URL against the node's ui_token."""
    token = str(node.config.get("ui_token", ""))
    return bool(token) and hmac.compare_digest(token.encode(), request_token.encode())


def _json(code: int, message: str) -> Response:
    return code, [("Content-Type", "application/json"), *security_headers()], json.dumps({"error": message}).encode()


def _page(node: "Node", tab: str, public_url: str, *, error: str = "", notice: str = "", extra: dict | None = None) -> Response:
    nonce = secrets.token_urlsafe(16)
    ctx = Ctx(node=node, token=str(node.config.get("ui_token", "")), tab=tab if tab in TABS else "inbox",
              public_url=public_url, nonce=nonce, error=error, notice=notice, extra=extra or {})
    return 200, [("Content-Type", "text/html; charset=utf-8"), *security_headers(nonce)], render(ctx)


# --------------------------------------------------------------------------- GET


def handle_get(node: "Node", request_token: str, sub: str = "", query: str = "", *, public_url: str = "") -> Response:
    """``sub`` is the path after the token ("" for the page, "file" for downloads)."""
    if not token_ok(node, request_token):
        return _json(404, "not found")
    params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=20)
    if sub == "file":
        return _download(node, (params.get("path") or [""])[0])
    if sub:
        return _json(404, "not found")
    tab = (params.get("tab") or ["inbox"])[0]
    ok = (params.get("ok") or [""])[0]
    return _page(node, tab, public_url, notice=NOTICES.get(ok, ""))


_UNSAFE_TYPES = ("html", "xml", "svg", "javascript", "ecmascript")


def _download(node: "Node", rel: str) -> Response:
    """Serve a received file — only if it resolves inside ``<home>/files``."""
    if not rel or "\x00" in rel:
        return _json(404, "not found")
    try:
        base = (node.home / "files").resolve()
        target = (base / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return _json(404, "not found")
    if target == base or not target.is_relative_to(base) or not target.is_file():
        return _json(404, "not found")
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if any(bad in ctype for bad in _UNSAFE_TYPES):
        ctype = "application/octet-stream"  # never let a received file render as a page
    ascii_name = "".join(ch if ch.isascii() and ch.isprintable() and ch not in '"\\' else "_" for ch in target.name)
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(target.name, safe='')}"
    headers = [("Content-Type", ctype), ("Content-Disposition", disposition),
               ("Content-Security-Policy", "default-src 'none'; sandbox"), *UI_SECURITY_HEADERS]
    return 200, headers, target.read_bytes()


# --------------------------------------------------------------------------- POST


def body_limit(content_type: str) -> int:
    """Largest POST body accepted for this Content-Type (checked before reading it)."""
    return MAX_UPLOAD_BYTES if content_type.lower().startswith("multipart/form-data") else MAX_FORM_BYTES


def _parse(content_type: str, body: bytes) -> Form:
    if content_type.lower().startswith("multipart/form-data"):
        return _parse_multipart(content_type, body)
    fields = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=1000)
    return Form(fields)


def _parse_multipart(content_type: str, body: bytes) -> Form:
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", errors="replace")
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if not msg.is_multipart():
        raise UIError("bad upload")
    fields: dict[str, list[str]] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[str(name)] = (Path(str(filename)).name, data)
        else:
            fields.setdefault(str(name), []).append(data.decode("utf-8", errors="replace"))
    return Form(fields, files)


def handle_post(node: "Node", request_token: str, body: bytes, content_type: str = "", *, public_url: str = "") -> Response:
    if not token_ok(node, request_token):
        return _json(404, "not found")
    try:
        form = _parse(content_type, body)
    except Exception:
        log.info("unparseable UI form", exc_info=True)
        return _json(400, "bad form")
    t = form.get("t")
    if not t or not token_ok(node, t):  # defense in depth: the token must be in the body too
        return _json(403, "forbidden")
    name = form.get("action")
    if form.files and name not in UPLOAD_ACTIONS:
        return _json(400, "uploads are only accepted for sending a file")
    handler, default_tab = ACTIONS.get(name, (None, "inbox"))
    tab = form.get("tab")
    tab = tab if tab in TABS else default_tab
    if handler is None:
        return _page(node, tab, public_url, error=f"unknown action {name!r}")
    try:
        extra = handler(node, form)
    except (NodeError, UIError) as exc:
        return _page(node, tab, public_url, error=str(exc) or "that didn't work")
    except ValueError as exc:  # e.g. a plan/list validation error surfacing from below
        return _page(node, tab, public_url, error=str(exc) or "invalid input")
    except Exception:
        log.exception("UI action %s failed", name)
        return _page(node, tab, public_url, error="Something went wrong on the node — check its log.")
    if extra:
        return _page(node, tab, public_url, extra=extra)
    # re-read the token: rotate_ui changes it, and the old URL must not be used again
    location = f"/ui/{node.config.get('ui_token', '')}?tab={tab}"
    if name in NOTICES:
        location += f"&ok={name}"
    return 303, [("Location", location), *security_headers()], b""
