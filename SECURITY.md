# Security model

## What Confer protects

| Asset | Protection |
|---|---|
| Message contents (plans, notes, files) | End-to-end authenticated encryption: a NaCl Box between the two nodes' keys. Relays, proxies and TLS terminators can't read it. |
| Message authenticity | An Ed25519 signature on every envelope. The agent id is the public key, so there's no directory to compromise. |
| Your calendar | Never transmitted. Peers learn only which of *their proposed* slots you accepted, and only once you (or your `autoconfirm` grant) answer. |
| Who may reach your agent | Only paired contacts. Anything else is refused before a handler runs. The two exceptions are `pair.request` carrying a valid one-time invite secret, and `pair.intro` matching an introduction your node was offered. |
| What a contact may do | Per-contact grants (`plans`, `autoconfirm`, `files`, `notes`, `intros`, `lists`, `money`, `location`, `trips`) checked on every message. Only a plan's or list's owner can change it. |
| Location | Opt-in per contact (`location` grant, off by default). It is shared only when you choose to, it expires (24 h at most), and receivers keep only the latest update, never a history. |
| REST API | Off until `confer api enable`. Every call except the public `GET /api/v1/openapi.json` needs a 256-bit bearer token, compared in constant time. The body is not read until the token checks out. Tools that act for the human also need a `Confer-Human-Approved: true` header, so integrators have to wire in an approval step. Wrong tokens are rate-limited per IP. Remote callers can't set `notify_cmd`, `notify_webhook` or a local calendar path. Remote calendar URLs must be https and resolve to public addresses, so there is no SSRF into your network. Agents can only send files from `~/.confer/outgoing/`, so a token or agent can't be used to read other files on the machine. MCP over HTTP binds to localhost only. |
| Money | Confer never moves money or holds payment credentials. Expense shares count only after the other person accepts them. Once accepted, the other side can't flip them to disputed. The creator can still withdraw an entry, which only gives up money owed to them. |
| Replays | Each envelope id is remembered for 8 days, and envelopes older than 7 days are refused. |
| Introductions | Only contacts you gave the `intros` grant can introduce people, and every introduction needs your approval. The peer's key is vouched for by the introducer, and its fingerprint is shown to you. |
| Phone app | Served only at a secret-token URL (`ui_token`, 192 bits); the same token is also required as a hidden form field. Security headers: strict CSP with a per-response script nonce, no framing, no caching. All text from other people is HTML-escaped. Uploads are capped at 11 MB, and only for the file-send action. Downloads come only from `<home>/files`, as attachments. If the link leaks, **Settings → Rotate this page's link** kills it at once. From the web you can't set anything that runs a program or reads a local file (`notify_cmd`, a local calendar path); those are CLI-only. |

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
