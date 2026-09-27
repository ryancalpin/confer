"""Agent identity: one Ed25519 keypair per node.

The agent id *is* the public key (base64url, no padding), so every id is
self-certifying: anyone holding an id can verify signatures from it and
encrypt to it without a directory service. Encryption uses the X25519 key
derived from the same Ed25519 key (libsodium's standard conversion).
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey


class IdentityError(ValueError):
    pass


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    if not isinstance(text, str):
        raise IdentityError("expected base64url string")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise IdentityError("invalid base64url") from exc


def verify_key_from_id(agent_id: str) -> VerifyKey:
    raw = b64d(agent_id)
    if len(raw) != 32:
        raise IdentityError("agent id must encode a 32-byte Ed25519 public key")
    return VerifyKey(raw)


def fingerprint(agent_id: str) -> str:
    """Short human-comparable fingerprint, e.g. ``4f1c-99ab-02de-77e0``."""
    digest = hashlib.sha256(b64d(agent_id)).hexdigest()[:16]
    return "-".join(digest[i : i + 4] for i in range(0, 16, 4))


def verify(agent_id: str, message: bytes, signature: bytes) -> bool:
    try:
        verify_key_from_id(agent_id).verify(message, signature)
        return True
    except (BadSignatureError, IdentityError, ValueError):
        return False


class Identity:
    def __init__(self, signing_key: SigningKey):
        self._sk = signing_key
        self.agent_id = b64e(bytes(signing_key.verify_key))
        self._curve_sk: PrivateKey = signing_key.to_curve25519_private_key()

    @classmethod
    def generate(cls) -> "Identity":
        return cls(SigningKey.generate())

    @classmethod
    def load(cls, path: Path) -> "Identity":
        seed = b64d(path.read_text().strip())
        if len(seed) != 32:
            raise IdentityError(f"corrupt key file: {path}")
        return cls(SigningKey(seed))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(b64e(bytes(self._sk)) + "\n")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.agent_id)

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(message).signature

    def box(self, peer_id: str) -> Box:
        """Authenticated-encryption box between this node and ``peer_id``."""
        peer_curve: PublicKey = verify_key_from_id(peer_id).to_curve25519_public_key()
        return Box(self._curve_sk, peer_curve)
