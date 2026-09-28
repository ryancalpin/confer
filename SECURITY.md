# Security model

## What Confer protects

| Asset | Protection |
|---|---|
| Message contents (plans, notes, files) | End-to-end authenticated encryption: a NaCl Box between the two nodes' keys. Relays, proxies and TLS terminators can't read it. |
| Message authenticity | An Ed25519 signature on every envelope. The agent id is the public key, so there's no directory to compromise. |
| Your calendar | Never transmitted. Peers learn only which of *their proposed* slots you accepted, and only once you (or your `autoconfirm` grant) answer. |
| Who may reach your agent | Only paired contacts. Everything else is refused before any handler runs. |
| What a contact may do | Per-contact grants (`plans`, `autoconfirm`, `files`, `notes`, `intros`, `lists`, `money`, `location`) checked on every message. Only a plan's or list's owner can change it. |
| Location | Opt-in per contact (`location` grant, off by default). It is shared only when you choose to, it expires (24 h at most), and receivers keep only the latest update, never a history. |
| Money | Confer never moves money or holds payment credentials. Expense shares count only after the other person accepts them. |
| Replays | Each envelope id is remembered for 8 days, and envelopes older than 7 days are refused. |
| Introductions | Only contacts you gave the `intros` grant can introduce people, and every introduction needs your approval. The peer's key is vouched for by the introducer, and its fingerprint is shown to you. |
| Phone inbox | Served only at a secret-token URL (`ui_token`, 192 bits). A hidden form token is required too. Strict CSP, no framing, no caching, and all text is HTML-escaped. |

## What it does not protect

- **Metadata.** A relay or network observer sees which agent ids talk,
  when, and message sizes. Run the node behind TLS (e.g. Tailscale) to hide
  this from the network. The relay you use still sees it.
- **Invite interception.** Whoever redeems an invite token first becomes the
  contact. Send tokens privately, and compare the fingerprint shown after
  pairing.
- **A compromised device or key.** `identity.key` is your identity.
  - **To retire a key you still control** (routine hygiene, or a lost backup
    copy), run `confer rotate-key --yes`.
  - **If an attacker already holds the key,** they can rotate too. In that
    case, set up a new node and re-pair out of band.
- **Malicious contacts within their grants.** A contact with `plans` can
  send you proposals, and one with `files` can send you files up to 10 MB.
  Grant only what you'd accept from that person, and remove contacts with
  `confer remove`.
- **Your AI assistant.** If you connect an LLM agent via MCP, it can act on
  your behalf. Incoming notes and plan titles are untrusted text written by
  other people. Treat them as data, not instructions. The MCP server's
  instructions tell agents not to accept, counter or cancel plans without
  your OK.

## Operational advice

- Keep `confer serve` on `127.0.0.1` and expose it through a TLS reverse
  proxy.
- The calendar feed URL contains a secret token. Treat it like a password.
- `notify_cmd` runs a program you configure, with event JSON on stdin and
  no shell. Only point it at scripts you trust.

## Reporting a vulnerability

Please open a private security advisory on the repository, or email the
maintainers. Do not file public issues for vulnerabilities.
