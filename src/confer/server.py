"""HTTP server: A2A endpoint, Agent Card, optional relay, calendar feed.

Routes
  GET  /.well-known/agent-card.json   A2A v1.0 Agent Card (also /.well-known/agent.json)
  POST /a2a                           A2A JSON-RPC (SendMessage / message/send)
  GET  /calendar/<feed_token>.ics     confirmed plans, for phone calendar subscription
  GET  /healthz
  POST /relay/v1/{send,fetch,ack}     only with relay mode enabled

Stdlib only (ThreadingHTTPServer). Put it behind TLS — Tailscale Serve/Funnel,
Caddy, or any reverse proxy. Envelopes are end-to-end encrypted and signed, so
TLS protects metadata; it is not what keeps message contents private.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .envelope import MAX_AGE_SECONDS, MAX_CT_CHARS, MEDIA_TYPE, EnvelopeError, canonical, check_outer
from .identity import b64d, verify
from .node import Node, Rejected
from .transport import A2A_PATH, A2A_VERSION, REJECTED, a2a_reply, extract_envelope, text_reply

log = logging.getLogger("confer.server")

MAX_BODY = MAX_CT_CHARS + 64 * 1024
RELAY_CAP_PER_RECIPIENT = 1000
RELAY_REQ_MAX_SKEW = 300


class RateLimiter:
    """Sliding-window limit per key (client IP or sender id)."""

    MAX_KEYS = 10000

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > self.MAX_KEYS:  # bound memory under address churn
                idle = [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]
                for k in idle:
                    del self._hits[k]
                if len(self._hits) > self.MAX_KEYS:  # still full: drop the least recently active half
                    for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[: len(self._hits) // 2]:
                        del self._hits[k]
            return True


def agent_card(node: Node, public_url: str, relay: bool) -> dict:
    url = public_url.rstrip("/") + A2A_PATH
    who = node.whoami()
    return {
        "name": f"{who['name']} (Confer)",
        "description": "Personal agent endpoint on the Confer network: trusted-contact scheduling, notes and E2E-encrypted files.",
        "url": url,
        "version": "0.1.0",
        "provider": {"organization": "Confer (open source)", "url": "https://github.com/"},
        "supportedInterfaces": [{"url": url, "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [{
                "uri": "urn:confer:protocol:v1",
                "description": "Signed + encrypted Confer envelopes carried as data parts",
                "required": True,
                "params": {"agent_id": who["agent_id"], "media_type": MEDIA_TYPE, "relay": relay},
            }],
        },
        "defaultInputModes": [MEDIA_TYPE],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": "confer.plans", "name": "Plan together", "description": "Propose, negotiate and confirm plans between trusted contacts' agents", "tags": ["scheduling", "calendar"]},
            {"id": "confer.files", "name": "Share files", "description": "End-to-end encrypted file transfer between trusted agents", "tags": ["files"]},
            {"id": "confer.notes", "name": "Agent notes", "description": "Short messages between trusted agents", "tags": ["messaging"]},
        ],
    }


class ConferServer:
    def __init__(self, node: Node, host: str = "127.0.0.1", port: int = 0, *, public_url: str = "", relay: bool = False, tick_seconds: float = 30.0):
        self.node = node
        self.relay_enabled = relay
        self.tick_seconds = tick_seconds
        self.ip_limiter = RateLimiter(120, 60)
        self.relay_limiter = RateLimiter(300, 60)
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.confer = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.public_url = (public_url or node.config.get("endpoint") or f"http://{host}:{self.port}").rstrip("/")
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        node.kick = self._wake.set  # outbound work happens on the worker thread

    def start(self) -> "ConferServer":
        for target in (self.httpd.serve_forever, self._worker):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.node.kick = self.node.flush

    def _worker(self) -> None:
        last_tick = 0.0
        while not self._stop.is_set():
            woke = self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                if woke:
                    self.node.flush()
                if time.monotonic() - last_tick >= self.tick_seconds:
                    last_tick = time.monotonic()
                    self.node.tick()
                    if self.relay_enabled:
                        self.node.store.mailbox_expire(time.time() - MAX_AGE_SECONDS)
            except Exception:
                log.exception("background work failed")

    # ------------------------------------------------------------- handlers
    def handle_rpc(self, req: Any) -> dict:
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "invalid JSON-RPC request")
        rid, method = req.get("id"), req.get("method")
        if method not in ("SendMessage", "message/send"):
            return _rpc_error(rid, -32601, f"method not supported: {method}")
        env = extract_envelope(req.get("params"))
        if env is None:
            return {"jsonrpc": "2.0", "id": rid, "result": text_reply(
                "This is a Confer node. It only accepts signed Confer envelopes from paired contacts "
                f"(data part, mediaType {MEDIA_TYPE}).")}
        try:
            result = self.node.receive(env)
        except (Rejected, EnvelopeError) as exc:
            return _rpc_error(rid, REJECTED, f"rejected: {exc}")
        except Exception:
            log.exception("error handling envelope")
            return _rpc_error(rid, -32603, "internal error, retry later")
        return {"jsonrpc": "2.0", "id": rid, "result": a2a_reply({"confer": result})}

    def relay_send(self, body: dict) -> dict:
        env = body.get("envelope")
        try:
            check_outer(env)
        except EnvelopeError as exc:
            return {"ok": False, "error": str(exc), "permanent": True}
        if not self.relay_limiter.allow("send:" + env["from"]):
            return {"ok": False, "error": "rate limited"}
        if not self.node.store.mailbox_put(env, time.time(), RELAY_CAP_PER_RECIPIENT):
            return {"ok": False, "error": "recipient mailbox full"}
        return {"ok": True}

    def _relay_auth(self, body: dict, op: str) -> str | None:
        try:
            sig = b64d(body.get("sig", ""))
            fields = {k: v for k, v in body.items() if k != "sig"}
            if fields.get("op") != op or abs(time.time() - int(fields.get("ts", 0))) > RELAY_REQ_MAX_SKEW:
                return None
            agent_id = str(fields.get("agent_id", ""))
            return agent_id if verify(agent_id, canonical(fields), sig) else None
        except Exception:
            return None

    def relay_fetch(self, body: dict) -> dict:
        agent_id = self._relay_auth(body, "fetch")
        if not agent_id:
            return {"ok": False, "error": "bad signature"}
        return {"ok": True, "envelopes": self.node.store.mailbox_get(agent_id)}

    def relay_ack(self, body: dict) -> dict:
        agent_id = self._relay_auth(body, "ack")
        if not agent_id:
            return {"ok": False, "error": "bad signature"}
        ids = [str(i) for i in (body.get("ids") or [])][:500]
        return {"ok": True, "deleted": self.node.store.mailbox_ack(agent_id, ids)}


def _rpc_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class _Handler(BaseHTTPRequestHandler):
    server_version = "Confer/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> ConferServer:
        return self.server.confer  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s " + fmt, self.client_address[0], *args)

    def _send(self, code: int, payload: Any, ctype: str = "application/json") -> None:
        raw = payload if isinstance(payload, bytes) else (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _client(self) -> str:
        return self.client_address[0]

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            return self._send(200, agent_card(self.app.node, self.app.public_url, self.app.relay_enabled))
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path.startswith("/calendar/") and path.endswith(".ics"):
            token = path[len("/calendar/") : -len(".ics")]
            expected = str(self.app.node.config.get("feed_token", ""))
            if expected and hmac.compare_digest(token, expected):
                return self._send(200, self.app.node.calendar_ics(), "text/calendar; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.app.ip_limiter.allow(self._client()):
            return self._send(429, {"error": "rate limited"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(400, {"error": "bad length"})
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "body too large or empty"})
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, _rpc_error(None, -32700, "parse error"))
        path = self.path.split("?", 1)[0]
        if path == A2A_PATH:
            return self._send(200, self.app.handle_rpc(body))
        if self.app.relay_enabled and isinstance(body, dict):
            route = {"/relay/v1/send": self.app.relay_send, "/relay/v1/fetch": self.app.relay_fetch, "/relay/v1/ack": self.app.relay_ack}.get(path)
            if route:
                return self._send(200, route(body))
        self._send(404, {"error": "not found"})
