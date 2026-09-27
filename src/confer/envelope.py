"""Signed, end-to-end encrypted envelopes — the only unit that crosses the wire.

Outer (visible to transports/relays): ``v, id, from, to, ts, ct, sig``.
Inner (encrypted with a NaCl Box between sender and recipient): ``type, body``.

``sig`` is an Ed25519 signature by ``from`` over the canonical JSON of the
outer fields (everything except ``sig``), so a relay can check authenticity
and route by ``to`` without ever seeing message types or contents.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from nacl.exceptions import CryptoError

from .identity import Identity, IdentityError, b64d, b64e, verify

VERSION = "confer/1"
MAX_FUTURE_SECONDS = 300  # tolerated clock skew
# Envelopes may legitimately arrive late (peer offline, sitting in a relay
# mailbox, outbox retries), so the age window is long. Replays are stopped by
# the receiver's seen-id cache, which must outlive this window.
MAX_AGE_SECONDS = 7 * 24 * 3600
SEEN_TTL_SECONDS = MAX_AGE_SECONDS + 24 * 3600
MEDIA_TYPE = "application/vnd.confer.envelope+json"
MAX_CT_CHARS = 16 * 1024 * 1024  # ~12 MB plaintext after base64
_OUTER_FIELDS = ("v", "id", "from", "to", "ts", "ct")


class EnvelopeError(ValueError):
    pass


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Opened:
    id: str
    sender: str
    type: str
    body: dict
    ts: int


def seal(identity: Identity, to: str, type_: str, body: dict, *, now: float | None = None) -> dict:
    inner = canonical({"type": type_, "body": body})
    ct = identity.box(to).encrypt(inner)  # 24-byte nonce is prepended
    env = {
        "v": VERSION,
        "id": uuid.uuid4().hex,
        "from": identity.agent_id,
        "to": to,
        "ts": int(now if now is not None else time.time()),
        "ct": b64e(bytes(ct)),
    }
    env["sig"] = b64e(identity.sign(canonical(env)))
    return env


def check_outer(env: Any, *, now: float | None = None, max_age: int = MAX_AGE_SECONDS) -> None:
    """Structure, freshness and signature — everything a relay can verify."""
    if not isinstance(env, dict):
        raise EnvelopeError("envelope must be an object")
    for key in (*_OUTER_FIELDS, "sig"):
        if key not in env:
            raise EnvelopeError(f"missing field: {key}")
    if env["v"] != VERSION:
        raise EnvelopeError(f"unsupported version: {env['v']!r}")
    if not isinstance(env["ts"], int) or isinstance(env["ts"], bool):
        raise EnvelopeError("ts must be an integer")
    if not all(isinstance(env[k], str) for k in ("id", "from", "to", "ct", "sig")):
        raise EnvelopeError("malformed envelope fields")
    current = now if now is not None else time.time()
    if env["ts"] > current + MAX_FUTURE_SECONDS or env["ts"] < current - max_age:
        raise EnvelopeError("stale or future-dated envelope")
    if len(env["ct"]) > MAX_CT_CHARS:
        raise EnvelopeError("envelope too large")
    outer = {k: env[k] for k in _OUTER_FIELDS}
    try:
        sig = b64d(env["sig"])
    except IdentityError as exc:
        raise EnvelopeError("bad signature encoding") from exc
    if not verify(env["from"], canonical(outer), sig):
        raise EnvelopeError("bad signature")


def open_(identity: Identity, env: Any, *, now: float | None = None) -> Opened:
    check_outer(env, now=now)
    if env["to"] != identity.agent_id:
        raise EnvelopeError("envelope is not addressed to this node")
    try:
        plain = identity.box(env["from"]).decrypt(b64d(env["ct"]))
        inner = json.loads(plain)
    except (CryptoError, IdentityError, ValueError) as exc:
        raise EnvelopeError("cannot decrypt envelope") from exc
    if not isinstance(inner, dict) or not isinstance(inner.get("type"), str) or not isinstance(inner.get("body"), dict):
        raise EnvelopeError("malformed envelope payload")
    return Opened(id=env["id"], sender=env["from"], type=inner["type"], body=inner["body"], ts=env["ts"])
