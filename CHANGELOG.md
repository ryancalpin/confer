# Changelog

## 0.4.0
- **Trips** (`confer trip ...`, `trip.share` / `trip.op` / `trip.close`, grant `trips`):
  - itinerary: flights, lodging, activities, with confirmation codes and links;
  - each person's arrival and departure, including "needs pickup";
  - carpools and rooms, both capacity-checked;
  - tasks with an assignee and due date;
  - polls;
  - a linked packing list and trip budget.
  - Trip days count as busy time, and the trip and its itinerary appear in the calendar feed.
- **Integrations**: a single tool catalog (39 tools) drives:
  - MCP over stdio or streamable HTTP;
  - a REST API (`confer api enable`) with OpenAPI 3.1 and a `Confer-Human-Approved` header for sensitive tools;
  - function-calling exports (`confer tools export --format openai|anthropic|gemini|mcp|openapi`);
  - the Python `ConferClient`;
  - setup guides for many harnesses in `integrations/`.
- **Settings validation** shared by every remote surface (`confer.settings`).
- **Security review fixes:**
  - Agents and API callers can only send files from `~/.confer/outgoing/`
    (`confer_outgoing_files` lists them). This closed a hole where a stolen
    API token could read arbitrary files.
  - `notify_webhook` is CLI-only.
  - Remote calendar URLs must be https and resolve to public addresses (no SSRF).
  - Wrong API tokens are rate-limited.
  - MCP tools carry `destructiveHint` / `readOnlyHint` annotations.
- **Correctness fixes:**
  - The hours cross-check now runs on cleaned values.
  - Leaving a trip drops your votes and task assignments.
  - `confer grant` accepts several arguments.
  - `trips_block_calendar` can be set with `confer config`.
- **Docs:**
  - Every harness snippet was checked against official docs.
  - OpenClaw has MCP and skill setup.
  - The protocol spec now has trips (§7f) and lists the messages that need no grant (§7g).

## 0.3.0
- **Shared lists** (`confer list ...`, `list.share` / `list.op` / `list.close`, grant `lists`): groceries, packing, "who's bringing what", with claims.
- **Expense splitting** (`confer money ...`, `money.expense` / `money.settle` / `money.ack` / `money.cancel`, grant `money`): equal or custom splits, accept/dispute, settle-up, per-person balances, optional `pay_link`. Records only; no money moves.
- **Status / ETA / location** (`confer share`, `presence.update` / `presence.clear`, grant `location`, off by default): expiring and latest-only, sent to contacts or to everyone in a plan.
- **Phone app**: the web inbox is now a full management UI with six tabs (Inbox · Plans · Lists · Money · People · Settings). It covers creating plans, lists and expenses; invites (copy or share); per-person permission checkboxes; settings; status and location sharing; sending notes and files; downloading received files; and rotating the link and the identity key.
- MCP: 28 tools. New config: `currency`, `pay_link`. `pay_link` and `notify_webhook` may now contain query strings.
- Security fixes:
  - Accepted money entries are final.
  - Per-contact caps on shared lists and pending money requests.
  - Pay links must be https.
  - Leaving a list waits for the owner to confirm.
  - Key rotation rewrites only id fields, never free text.
  - Expired location shares are cleared from the inbox.

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
