"""Wire transport: A2A v1.0 JSON-RPC framing plus the relay mailbox calls.

A Confer envelope travels as a single A2A *data part* whose ``mediaType`` is
``application/vnd.confer.envelope+json``. Any A2A v1.0 client can reach a
Confer node; any Confer node is an ordinary A2A agent on the wire.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from .envelope import MEDIA_TYPE, canonical
from .identity import Identity, b64e

A2A_PATH = "/a2a"
A2A_VERSION = "1.0"
REJECTED = -32001  # JSON-RPC error code: envelope permanently refused by the receiver
USER_AGENT = "confer/0.4"


class DeliveryError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def a2a_request(env: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": uuid.uuid4().hex,
                "parts": [{"data": {"confer": env}, "mediaType": MEDIA_TYPE}],
            }
        },
    }


def extract_envelope(params: Any) -> dict | None:
    """Find the Confer envelope in A2A ``SendMessage`` params (v1.0 or v0.3 parts)."""
    msg = params.get("message") if isinstance(params, dict) else None
    parts = msg.get("parts") if isinstance(msg, dict) else None
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        media = part.get("mediaType") or part.get("mimeType") or (part.get("metadata") or {}).get("mediaType")
        if isinstance(data, dict) and isinstance(data.get("confer"), dict) and (media in (None, MEDIA_TYPE) or part.get("kind") == "data"):
            return data["confer"]
    return None


def a2a_reply(data: dict) -> dict:
    return {
        "message": {
            "role": "ROLE_AGENT",
            "messageId": uuid.uuid4().hex,
            "parts": [{"data": data, "mediaType": "application/json"}],
        }
    }


def text_reply(text: str) -> dict:
    return {"message": {"role": "ROLE_AGENT", "messageId": uuid.uuid4().hex, "parts": [{"text": text, "mediaType": "text/plain"}]}}


def signed_relay_request(identity: Identity, op: str, **fields: Any) -> dict:
    body = {"op": op, "agent_id": identity.agent_id, "ts": int(time.time()), "nonce": uuid.uuid4().hex, **fields}
    body["sig"] = b64e(identity.sign(canonical(body)))
    return body


class HttpTransport:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def _post(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "A2A-Version": A2A_VERSION, "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (peer endpoints)
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            # 4xx other than 408/429 will not get better by retrying
            permanent = 400 <= exc.code < 500 and exc.code not in (408, 429)
            raise DeliveryError(f"HTTP {exc.code} from {url}: {detail}", permanent=permanent) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
            raise DeliveryError(f"cannot reach {url}: {exc}") from exc

    def send(self, endpoint: str, env: dict) -> dict:
        resp = self._post(endpoint.rstrip("/") + A2A_PATH, a2a_request(env))
        if "error" in resp:
            err = resp["error"] or {}
            raise DeliveryError(f"peer refused: {err.get('message')}", permanent=err.get("code") == REJECTED)
        return resp.get("result") or {}

    def relay_send(self, relay: str, env: dict) -> None:
        resp = self._post(relay.rstrip("/") + "/relay/v1/send", {"envelope": env})
        if not resp.get("ok"):
            raise DeliveryError(f"relay refused: {resp.get('error')}", permanent=bool(resp.get("permanent")))

    def relay_fetch(self, relay: str, identity: Identity) -> list[dict]:
        resp = self._post(relay.rstrip("/") + "/relay/v1/fetch", signed_relay_request(identity, "fetch"))
        if not resp.get("ok"):
            raise DeliveryError(f"relay fetch failed: {resp.get('error')}")
        return [e for e in resp.get("envelopes", []) if isinstance(e, dict)]

    def relay_ack(self, relay: str, identity: Identity, env_ids: list[str]) -> None:
        if env_ids:
            self._post(relay.rstrip("/") + "/relay/v1/ack", signed_relay_request(identity, "ack", ids=env_ids))
