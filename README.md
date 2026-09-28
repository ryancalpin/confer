# Confer

**Open agent-to-agent coordination for personal AI assistants.**

Your agent talks to your people's agents. You say *"set up dinner with Sam
next week"*; your agent contacts Sam's agent, both check their own calendars
privately, agree on a time, and each of you gets one message:
*"✅ Dinner is set for Tue 18:00."* It works the same for a weekly game night
with five friends or a book club with forty people.

Beyond planning, your agents can:
- keep **shared lists** ("who's bringing what");
- **split the bill** and settle up;
- share an **ETA or location** that expires on its own;
- exchange **files and notes** end-to-end encrypted;
- **introduce** people to each other.

Confer is an open-source, self-hostable take on the idea behind Instinct's
Instinct-to-Instinct Trusted Person network. It isn't affiliated with Instinct
and doesn't interoperate with it. Confer uses **open standards**
([A2A v1.0](https://a2a-protocol.org) on the wire, [MCP](https://modelcontextprotocol.io)
for your agent, iCalendar for calendars), so it works with any assistant:
Claude, Hermes, Codex, OpenClaw, or your own.

| | Closed agent networks | Confer |
|---|---|---|
| Who runs the network | the vendor | you (a node per person; optional relays anyone can run) |
| Who sees your calendar | the vendor's cloud | nobody. Your node only answers "which of *these* times work?" |
| Message privacy | vendor policy | end-to-end encrypted + signed (NaCl), even through relays |
| Which assistant | theirs | any agent that speaks MCP, or the CLI |
| Protocol | proprietary | documented ([docs/PROTOCOL.md](docs/PROTOCOL.md)), carried over A2A |

## What it does

- **Trusted people only.** Pair once with a one-time invite token. Strangers
  can't message your agent at all.
- **Per-person permissions (grants).** Grants cover what each contact's agent
  may do with yours: `plans` (propose plans), `autoconfirm` (plans that fit
  your calendar are accepted without asking you), `files`, and `notes`.
- **Private scheduling.** The organizer's agent offers a few candidate times
  from its own free time. Your agent checks them against your calendar
  locally. You, or your `autoconfirm` rule, say which ones work. The
  organizer's agent picks the earliest time that works for everyone and
  confirms it to all.
- **Groups, quorum and recurring plans.** A plan can need everyone or just N
  people (`--quorum 6`). It can have a response deadline and an RRULE
  (`FREQ=WEEKLY;BYDAY=TH`). Counter-proposals and revisions are supported.
- **End-to-end encrypted files and notes** between agents (≤10 MB).
- **Works offline and behind NAT.** A durable outbox retries messages for up
  to 7 days. A relay stores and forwards encrypted envelopes, and it can't
  read or forge them.
- **Your calendar in, your calendar out.** Busy time comes from any `.ics`
  file or secret iCal URL (Google, iCloud, Outlook). Confirmed plans are
  written to `calendar.ics` and served as a subscribable feed.
- **Human in the loop.** You get an inbox, plus webhook or command
  notifications (e.g. Telegram) for anything that needs you.
- **A phone inbox.** `confer serve` prints a private URL for a
  mobile-first page. From it you can approve plans and introductions, pick
  times, and dismiss items. It has no app, no JS framework and no external
  assets.

### Things closed networks don't do

- **Vouched introductions.** `confer introduce Sam Priya` offers each of
  them the other's verified key, and each approves. Their agents then
  connect directly, with no token to pass around. Trust grows the way it
  does between people.
- **Preference-weighted scheduling.** You can mark the times you'd
  *prefer* (`--prefer 2`), not just the ones that work. The group lands on
  the time most people like, not just the earliest one.
- **Tentative holds.** Times you've offered or accepted on an unresolved
  plan count as busy for other proposals. Two negotiations can't both claim
  your Friday.
- **Nudges.** Organizers' agents remind people who haven't answered, once,
  in the last quarter before the deadline.
- **Agent Q&A.** `confer note Sam "Thai or Italian?" --ask` returns a
  message id, and the answer comes back threaded (`reply_to`). Your agent
  can ask Sam's agent things without interrupting either of you.
- **Key rotation.** `confer rotate-key --yes` moves you to a new key. The old
  key signs the handoff and the new key proves it is really held, so every
  contact follows automatically.

### Beyond planning

- **Trips.** `confer trip new "Tahoe" --with Sam,Priya --start 2026-12-18 --end 2026-12-21`
  sets up the trip, and everyone can then fill it in:
  - **Itinerary:** flights, lodging and activities, with confirmation codes
    (`confer trip add`).
  - **Your own travel:** arrival, departure, and whether you need a pickup
    (`confer trip arrive --pickup`).
  - **Carpools and rooms:** capacity-checked (`ride`, `join-ride`, `room`,
    `join-room`).
  - **Tasks and polls:** assign tasks (`task --for Sam`) and vote on
    decisions (`poll`, `vote`).
  - **Budget:** the trip gets a shared packing list, and expenses tagged
    with it add up in `confer trip budget`.
  - **Calendar:** trip days count as busy time, and the itinerary shows up
    in your calendar feed.

- **Shared lists.**
  `confer list new "BBQ" --with Sam,Priya --item burgers --item buns`.
  Everyone can add items, check them off, or claim one ("I'll bring it").
  The owner's agent keeps the master copy and syncs everyone.
- **Split the bill.** `confer money split "Dinner" 96.50 --with Sam,Priya`
  asks each person's agent for their share. They accept or dispute, and
  pay you back through your `pay_link` (Venmo, PayPal and so on).
  `confer money balances` shows who owes whom. Confer keeps the agreed
  record and never moves money.
- **ETA and location.**
  `confer share --plan <id> --text "leaving now" --eta 15` reaches everyone
  in the plan, and the phone app adds a "share my location" button.
  - **Opt-in:** each contact must allow it (the `location` grant).
  - **Short-lived:** 2 h by default, 24 h at most.
  - **No history:** receivers keep only the latest update.
- **Files and notes.** `confer send-file Sam menu.pdf` (≤10 MB) and
  `confer note Sam "Thai or Italian?" --ask`. Both are end-to-end
  encrypted, and answers come back threaded.

## How you manage it

There are three ways to manage it, all over the same node:

| | Best for |
|---|---|
| **Phone app.** `confer serve` prints a private link; open it on your phone over Tailscale and use Share → Add to Home Screen. There are seven tabs: Inbox · Plans · Trips · Lists · Money · People · Settings. Treat the link like a password; Settings can rotate it. | Day to day: approve plans, money requests and introductions; organize trips; manage lists; see balances; share your ETA; edit each person's permissions and all settings. |
| **iOS app** ([`ios/`](ios/)). Native SwiftUI, connected to your node's REST API over your tailnet. | The same things as the phone app, natively: tabs for Inbox, Plans, Trips, Lists and Money, plus People and Settings from the Inbox. Local notifications tell you when something needs you. |
| **Your AI assistant** (MCP, 39 tools) | Just ask: "plan dinner with Sam Friday", "add charcoal to the BBQ list", "what do I owe Priya?". Money, location and approvals always wait for your OK. |
| **CLI** (`confer ...`, `--json` for scripts) | Setup, automation and power use. |

**Permissions are per person.** Each contact gets grants you choose:
`plans`, `autoconfirm`, `files`, `notes`, `intros`, `lists`, `money`, `trips`
and `location`. Everything except `autoconfirm` and `location` is on by default.
Change them in the phone app or with `confer grant Sam +location,-money`.

**Settings** live in `~/.confer/config.json`. Edit them in the phone app's
Settings tab or with `confer config <key> <value>`:
- `name`, `endpoint`, `relay`, `tz`;
- working hours (`hours_start`, `hours_end`) and `buffer_minutes`;
- `calendar`: an .ics URL;
- `tentative_holds`, `trips_block_calendar`, `currency`, `pay_link`,
  `nudge_after_hours`.

For safety, some settings can only be set from the CLI:
- `notify_cmd`, because it runs a program;
- `notify_webhook`, because it receives every inbox event;
- a local `.ics` or `.json` calendar path.

Remote calendar URLs must be `https://` or `webcal://` and point to a public
address.

## Quick start

```bash
pip install "confer[ics,mcp] @ git+https://github.com/ryancalpin/confer"   # or: uv tool install "confer[ics,mcp] @ git+https://github.com/ryancalpin/confer"

confer init "Alex" --tz America/Chicago
confer config endpoint https://alex.example.ts.net   # where peers reach you (or: confer config relay <url>)
confer config calendar "https://calendar.google.com/calendar/ical/.../basic.ics"
confer serve --port 3067 &                           # put TLS in front (Tailscale Serve, Caddy, ...)

confer invite "Sam"            # prints a one-time token — text it to Sam
# Sam runs:  confer accept confer1:eyJ... --name Alex
```

Then:

```bash
confer plan new "Dinner" --with Sam --duration 90 --from 2026-10-01 --to 2026-10-07 --between 18:00-21:00 --location "Luigi's"
confer inbox                  # Sam's side: "Alex wants to plan 'Dinner'... you're free for 1, 3"
confer plan respond <id> accept          # Sam: accept the options their calendar shows free
confer plan list                          # both: [confirmed]
```

More: `confer grant Sam +autoconfirm`, `confer note Sam "Thai or Italian?"`,
`confer send-file Sam menu.pdf`, `confer plan revise <id> --from ...`,
`confer plan cancel <id>`, `confer --json ...` for scripts.

## Use it from any agent or harness

One tool catalog (39 tools) is exposed four ways:
- **MCP:** `confer mcp`, over stdio or `--http`.
- **REST API:** `confer api enable`, then `POST /api/v1/tools/<name>`, with an OpenAPI spec.
- **Function-calling schemas:** `confer tools export --format openai|anthropic|gemini`.
- **Python client:** `confer.client.ConferClient`.

**[integrations/](integrations/)** has copy-paste setup for:
- **MCP clients:** Claude Code, Claude Desktop, Codex CLI, Gemini CLI,
  Cursor, VS Code, Goose, Hermes.
- **Skill-based agents** such as OpenClaw.
- **Agent frameworks:** the Anthropic and OpenAI APIs and Agents SDKs,
  LangChain, Gemini.
- **Automation tools:** n8n, Zapier, Home Assistant.
- **Other A2A agents.**

```json
{ "mcpServers": { "confer": { "command": "confer", "args": ["mcp"] } } }
```

Your agent gets 39 tools. They cover plans (`confer_propose_plan`,
`confer_respond`), lists (`confer_create_list`, `confer_list_edit`), money
(`confer_split_expense`, `confer_balances`), status (`confer_share_status`),
notes, files, introductions, and the inbox. Your agent is the brain: it understands "dinner with my wife on
Friday", books the restaurant, and texts you. Confer is the trusted pipe to
other people's agents. See [integrations/](integrations/) for Claude Code, Hermes and more
config.

## How it works

```
 Alex's agent ──MCP──▶ Alex's node ══ A2A + signed, encrypted envelopes ══▶ Sam's node ◀──MCP── Sam's agent
                          │                    (direct or via relay)             │
                     alex.ics (busy)                                         sam.ics (busy)
```

1. **Identity.** Each node has an Ed25519 key, and its agent id *is* the
   public key. Pairing binds names to keys, so you can compare fingerprints
   out of band.
2. **Envelopes.** Every message is encrypted to the recipient (NaCl Box) and
   signed by the sender. Relays and proxies only see who is talking to whom.
3. **Plans.** The flow is hub-and-spoke: only the organizer's node can
   propose, revise, confirm or cancel. Participants don't need to know each
   other, and they never see each other's answers.

Full spec: [docs/PROTOCOL.md](docs/PROTOCOL.md). Threat model:
[SECURITY.md](SECURITY.md).

## Running a node

- `confer serve --host 127.0.0.1 --port 3067` puts a stdlib HTTP server on
  localhost. Expose it with TLS: `tailscale serve`/`funnel`, Caddy, nginx or
  Cloudflare Tunnel.
- If you can't expose anything, use a relay (`confer config relay https://relay.example`).
  Your node then just polls the relay; run `confer serve` anyway for the
  worker loop. To host a relay yourself for friends, run `confer relay --port 3068`.
- Docker: `docker build -t confer . && docker run -v confer:/data -p 3067:3067 confer`
  (run `docker run -it -v confer:/data confer init "Alex"` once first).
- `examples/confer.service` is a systemd user unit.
  `examples/notify-telegram.sh` pushes inbox events to your phone.
- State lives in `~/.confer/` (or `$CONFER_HOME`): `identity.key` (0600),
  `state.db`, `calendar.ics`, and received `files/`. Back up
  `identity.key`, because it *is* your identity.

## Status

v0.4: the protocol and reference implementation are complete and tested (the
end-to-end tests run real nodes over HTTP, with relays, retries, forged
messages, introductions and key rotation). CI runs on Python 3.11–3.13. Not
yet done: multi-device, CalDAV write-back, push instead of relay polling, and
SDKs in other languages. Contributions
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0
