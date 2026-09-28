# Changelog

## 0.3.0
- **Shared lists** (`confer list ...`, `list.share` / `list.op` / `list.close`, grant `lists`): groceries, packing, "who's bringing what", with claims.
- **Expense splitting** (`confer money ...`, `money.expense` / `money.settle` / `money.ack` / `money.cancel`, grant `money`): equal or custom splits, accept/dispute, settle-up, per-person balances, optional `pay_link`. Records only; no money moves.
- **Status / ETA / location** (`confer share`, `presence.update` / `presence.clear`, grant `location`, off by default): expiring and latest-only, sent to contacts or to everyone in a plan.
- MCP: 28 tools. New config: `currency`, `pay_link`.

## 0.2.0
- **Vouched introductions** (`confer introduce A B`, `intro.offer` / `pair.intro`, new `intros` grant).
- **Key rotation** (`confer rotate-key --yes`, `key.rotate` with a proof from the new key, 30-day grace for the old key; a running server picks up a key rotated by another process).
- **Preference-weighted scheduling** (`prefer` in `plan.respond`, `--prefer`).
- **Tentative holds**: times on unresolved plans count as busy (`tentative_holds` config, on by default).
- **Reminders**: `plan.nudge`, once per revision, before the deadline (`confer remind`, automatic in `serve`).
- **Threaded notes**: `msg_id`, `reply_to`, `expects_reply`; `confer note --ask/--reply-to`; MCP `confer_replies`.
- **Phone inbox**: private mobile web page to approve plans and introductions.
- Outbox delivers each recipient's messages in order across retries.
- MCP: 19 tools (adds replies, introductions). CI (Python 3.11–3.13), Dockerfile.

## 0.1.0
- First release: pairing, grants, E2E-encrypted envelopes over A2A v1.0, private multi-party scheduling (quorum, recurrence, counters), files, notes, relay, calendar feed, CLI, MCP server.
