# Confer

**Open agent-to-agent coordination for personal AI assistants.**

Your agent talks to your people's agents. You say *"set up dinner with Sam
next week"*; your agent contacts Sam's agent, both check their own calendars
privately, agree on a time, and each of you gets one message:
*"✅ Dinner is set for Tue 18:00."* It works the same for a weekly game night
with five friends or a book club with forty people.

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

## Quick start

```bash
pip install "confer[ics,mcp]"          # or: uv tool install "confer[ics,mcp]"

confer init "Ryan" --tz America/Chicago
confer config endpoint https://ryan.example.ts.net   # where peers reach you (or: confer config relay <url>)
confer config calendar "https://calendar.google.com/calendar/ical/.../basic.ics"
confer serve --port 3067 &                           # put TLS in front (Tailscale Serve, Caddy, ...)

confer invite "Sam"            # prints a one-time token — text it to Sam
# Sam runs:  confer accept confer1:eyJ... --name Ryan
```

Then:

```bash
confer plan new "Dinner" --with Sam --duration 90 --from 2026-10-01 --to 2026-10-07 --between 18:00-21:00 --location "Luigi's"
confer inbox                  # Sam's side: "Ryan wants to plan 'Dinner'... you're free for 1, 3"
confer plan respond <id> accept          # Sam: accept the options their calendar shows free
confer plan list                          # both: [confirmed]
```

More: `confer grant Sam +autoconfirm`, `confer note Sam "Thai or Italian?"`,
`confer send-file Sam menu.pdf`, `confer plan revise <id> --from ...`,
`confer plan cancel <id>`, `confer --json ...` for scripts.

## Use it from your AI assistant (MCP)

```json
{ "mcpServers": { "confer": { "command": "confer", "args": ["mcp"] } } }
```

Your agent gets 15 tools: `confer_propose_plan`, `confer_inbox`,
`confer_respond`, `confer_send_note`, `confer_send_file`, `confer_invite`,
and others. Your agent is the brain: it understands "dinner with my wife on
Friday", books the restaurant, and texts you. Confer is the trusted pipe to
other people's agents. See [examples/](examples/) for Claude Code and Hermes
config.

## How it works

```
 Ryan's agent ──MCP──▶ Ryan's node ══ A2A + signed, encrypted envelopes ══▶ Sam's node ◀──MCP── Sam's agent
                          │                    (direct or via relay)             │
                     ryan.ics (busy)                                         sam.ics (busy)
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
- `examples/confer.service` is a systemd user unit.
  `examples/notify-telegram.sh` pushes inbox events to your phone.
- State lives in `~/.confer/` (or `$CONFER_HOME`): `identity.key` (0600),
  `state.db`, `calendar.ics`, and received `files/`. Back up
  `identity.key`, because it *is* your identity.

## Status

v0.1: the protocol and reference implementation are complete and tested (the
end-to-end tests run real nodes over HTTP, with relays, retries and forged
messages). Not yet done: key rotation and multi-device, CalDAV write-back,
push instead of relay polling, and SDKs in other languages. Contributions
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0
