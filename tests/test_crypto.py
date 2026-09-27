import time

import pytest

from confer.envelope import EnvelopeError, check_outer, open_, seal
from confer.identity import Identity, b64e, fingerprint, verify


def test_roundtrip_is_encrypted_and_signed():
    a, b = Identity.generate(), Identity.generate()
    env = seal(a, b.agent_id, "note", {"text": "secret dinner plans"})
    assert "secret" not in str(env) and "note" not in env["ct"]
    opened = open_(b, env)
    assert (opened.sender, opened.type, opened.body) == (a.agent_id, "note", {"text": "secret dinner plans"})


def test_wrong_recipient_cannot_open():
    a, b, c = Identity.generate(), Identity.generate(), Identity.generate()
    env = seal(a, b.agent_id, "note", {})
    with pytest.raises(EnvelopeError, match="not addressed"):
        open_(c, env)
    env2 = dict(env, to=c.agent_id)  # re-addressing breaks the signature
    with pytest.raises(EnvelopeError, match="signature"):
        open_(c, env2)


@pytest.mark.parametrize("field,value", [("ct", "AAAA"), ("from", Identity.generate().agent_id), ("ts", 1), ("id", "x")])
def test_tampering_is_detected(field, value):
    a, b = Identity.generate(), Identity.generate()
    env = seal(a, b.agent_id, "note", {})
    env[field] = value
    with pytest.raises(EnvelopeError):
        open_(b, env)


def test_freshness_window():
    a, b = Identity.generate(), Identity.generate()
    with pytest.raises(EnvelopeError, match="stale"):
        check_outer(seal(a, b.agent_id, "note", {}, now=time.time() + 3600))
    with pytest.raises(EnvelopeError, match="stale"):
        check_outer(seal(a, b.agent_id, "note", {}, now=time.time() - 8 * 86400))
    check_outer(seal(a, b.agent_id, "note", {}, now=time.time() - 3 * 86400))  # relayed late: fine


def test_malformed_inputs():
    b = Identity.generate()
    for bad in (None, [], {"v": "confer/1"}, {"v": "x", "id": "", "from": "", "to": "", "ts": 0, "ct": "", "sig": ""}):
        with pytest.raises(EnvelopeError):
            open_(b, bad)


def test_identity_persistence_and_fingerprint(tmp_path):
    ident = Identity.generate()
    ident.save(tmp_path / "k")
    assert (tmp_path / "k").stat().st_mode & 0o777 == 0o600
    again = Identity.load(tmp_path / "k")
    assert again.agent_id == ident.agent_id
    assert len(fingerprint(ident.agent_id)) == 19
    sig = ident.sign(b"hi")
    assert verify(ident.agent_id, b"hi", sig) and not verify(ident.agent_id, b"ho", sig)
    assert not verify(b64e(b"short"), b"hi", sig)


def test_envelope_id_is_bounded():
    from confer.envelope import canonical

    a, b = Identity.generate(), Identity.generate()
    env = seal(a, b.agent_id, "note", {})
    env["id"] = "a" * 100_000
    env["sig"] = b64e(a.sign(canonical({k: env[k] for k in ("v", "id", "from", "to", "ts", "ct")})))
    with pytest.raises(EnvelopeError, match="malformed"):
        check_outer(env)
