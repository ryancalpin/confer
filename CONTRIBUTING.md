# Contributing

```bash
uv venv && uv pip install -e '.[dev,mcp]'
.venv/bin/pytest -q
```

- The protocol is specified in `docs/PROTOCOL.md`. Any wire change updates
  the spec in the same PR.
- Keep the core dependency-light: PyNaCl and python-dateutil. Anything else
  is an optional extra.
- Every new message type needs:
  - a permission check (grant or plan role);
  - input validation with size limits;
  - an end-to-end test in `tests/test_network.py`, including the refusal
    case.
- Security issues: see `SECURITY.md`.

Good first areas:
- a CalDAV busy-time provider;
- key rotation (signed `key.rotate` to all contacts);
- push-style relay delivery (long-poll);
- a TypeScript implementation of the protocol.
