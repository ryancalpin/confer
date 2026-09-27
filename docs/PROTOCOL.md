# Confer Protocol v1 (`confer/1`)

Status: draft, implemented by the reference implementation in `src/confer/`.
Key words MUST/SHOULD/MAY are as in RFC 2119.

## 1. Identities

- A node's identity is an Ed25519 keypair. The **agent id** is the 32-byte
  public key, base64url-encoded without padding (43 characters).
- A **fingerprint** is the first 64 bits of `SHA-256(public key)`, shown as
  four groups of four hex digits (`4f1c-99ab-02de-77e0`). Humans compare
  fingerprints out of band to rule out a man-in-the-middle at pairing time.
- Encryption uses the X25519 keys derived from the Ed25519 keys
  (`crypto_sign_ed25519_pk_to_curve25519`).

## 2. Envelopes

Every protocol message is one envelope:

```json
{ "v": "confer/1", "id": "<32 hex>", "from": "<agent id>", "to": "<agent id>",
  "ts": 1790000000, "ct": "<base64url>", "sig": "<base64url>" }
```

- `ct` = `crypto_box(nonce(24) || ciphertext)` of the canonical JSON of
  `{"type": <string>, "body": <object>}`, from the sender's X25519 secret key
  to the recipient's X25519 public key.
- `sig` = Ed25519 signature by `from` over the canonical JSON of the outer
  object *without* `sig`.
- **Canonical JSON** means UTF-8 with sorted keys, no insignificant
  whitespace, and non-ASCII characters left unescaped.
- Receivers MUST reject an envelope if any of these hold:
  - the signature is invalid;
  - `to` isn't their own id;
  - `ts` is more than 300 s in the future or more than 7 days in the past;
  - `ct` is longer than 16 MiB;
  - decryption fails.
- Receivers MUST keep every processed `id` for at least 8 days and
  acknowledge duplicates without processing them again. This is what makes
  retries and relays safe.

## 3. Transport: A2A v1.0

An envelope travels as an A2A `SendMessage` (legacy: `message/send`)
JSON-RPC request to the node's A2A endpoint (`<base>/a2a`):

```json
{"jsonrpc":"2.0","id":"…","method":"SendMessage","params":{"message":{
  "role":"ROLE_USER","messageId":"…",
  "parts":[{"data":{"confer":<envelope>},"mediaType":"application/vnd.confer.envelope+json"}]}}}
```

- **Success.** The node replies with an A2A `Message` whose data part is
  `{"confer": {"ok": true}}` (plus `"duplicate": true` for a replay).
- **Permanent refusal.** An unknown sender, a missing grant or a malformed
  message is JSON-RPC error `-32001`. Senders MUST NOT retry it.
- **Transient failure.** `-32603`, HTTP 5xx or a network error. Senders
  SHOULD retry with exponential backoff until the envelope is about to
  exceed the age window.
- **Discovery.** Nodes publish an A2A Agent Card at
  `/.well-known/agent-card.json` with an extension
  `urn:confer:protocol:v1` whose `params.agent_id` is the node's id.
- **Plain text.** An A2A request with no Confer data part gets a plain-text
  explanation and nothing else.

### 3.1 Relays

A relay is untrusted store-and-forward for nodes that can't accept inbound
connections.

| Route | Body | Relay checks |
|---|---|---|
| `POST /relay/v1/send` | `{"envelope": E}` | outer signature + freshness of E, per-sender rate limit, per-recipient cap. Stores E under `E.to`. |
| `POST /relay/v1/fetch` | `{"op":"fetch","agent_id","ts","nonce","sig"}` | `sig` by `agent_id` over the canonical body without `sig`, and `ts` within ±300 s. Returns up to N envelopes. |
| `POST /relay/v1/ack` | `{"op":"ack","agent_id","ts","nonce","ids":[…],"sig"}` | same signature check; deletes those ids from that agent's mailbox. |

Relays drop envelopes older than 7 days. A relay can see metadata (who
messages whom, when, and roughly how much). It can't read or forge content.

## 4. Contacts and grants

A contact is `(agent id, local name, endpoint, relay, grants, status)`.
Grants say what *that contact* may do with *my* node:

| grant | allows |
|---|---|
| `plans` | send `plan.propose` |
| `autoconfirm` | my node answers proposals from my calendar without asking me |
| `files` | send `file.send` |
| `notes` | send `note` |
| `intros` | send `intro.offer` (each introduction still needs my owner's approval) |

Envelopes from non-contacts are refused, except `pair.request`. A pending
contact (you accepted their invite but they haven't confirmed yet) may only
send `pair.accept` or `pair.intro`. `plan.respond`, `plan.final` and `plan.cancel` need no
grant, because they only act on plans that already involve both parties.

## 5. Pairing

1. **Invite.** The inviter creates
   `confer1:` + base64url(JSON `{id, name, endpoint, relay, secret}`), where
   `secret` is 24 random bytes. It stores
   `(SHA-256(secret), local name, grants, expiry)`.
2. **Request.** The invitee sends
   `pair.request {secret, name, endpoint, relay}` to the inviter. The invitee
   already knows the inviter's key from the token, so this first message is
   encrypted and signed like any other.
3. **Accept.** The inviter atomically consumes the invite. It must be unused
   and unexpired, and it can be used once. The inviter then stores the
   contact with the invite's grants and replies
   `pair.accept {name, endpoint, relay}`.
4. **Activate.** The invitee marks the contact active.

Anyone holding the token before the intended person can pair instead of
them. Send tokens over a private channel and compare fingerprints. Both
sides get an inbox entry showing the peer's fingerprint.

## 6. Plans

Plans are hub-and-spoke. The organizer's node is the only authority for a
plan.

### 6.1 Wire plan (what participants see)

```json
{ "id": "<≤64 alnum>", "rev": 1, "title": "Dinner",
  "organizer": "<agent id>", "organizer_name": "Alex",
  "participants": { "<agent id>": {"name": "Sam"} },
  "slots": [ {"start": "2031-03-04T00:00:00Z", "end": "2031-03-04T01:30:00Z"} ],
  "rrule": "FREQ=WEEKLY;BYDAY=TH" | null, "tz": "America/Chicago",
  "location": "", "notes": "",
  "quorum": "all" | <int ≥ 1>, "deadline": <unix ts> | null,
  "status": "proposed" | "confirmed" | "needs_reschedule" | "cancelled",
  "chosen": <slot index> | null }
```

Limits: 20 slots, 200 participants, 14-day slot length, 200-character RRULE.
Other participants' answers are never sent.

**Recurrence.** `rrule` is an RFC 5545 RRULE value. `FREQ` MUST be DAILY,
WEEKLY, MONTHLY or YEARLY; sub-daily rules are refused to keep availability
checks cheap. `DTSTART` MUST NOT appear, and `UNTIL` MUST be UTC. The rule
is expanded in the organizer's IANA timezone `tz`, starting at the slot's
start converted to that zone, so a weekly 18:00 stays 18:00 local across DST.

### 6.2 Messages

| type | from → to | body |
|---|---|---|
| `plan.propose` | organizer → each participant | `{plan}`. A higher `rev` supersedes the earlier one; any other `rev` is ignored. |
| `plan.respond` | participant → organizer | `{plan_id, rev, decision: accept\|decline\|counter, ok_slots:[idx], prefer:[idx], note, counter:[{start,end}]}` |
| `plan.final` | organizer → each participant | `{plan}` with `status: confirmed` and `chosen` |
| `plan.cancel` | organizer → each participant | `{plan_id, reason}` |
| `plan.nudge` | organizer → a participant who hasn't answered | `{plan_id, rev, deadline}` |

Receivers MUST check that `plan.organizer == envelope.from` for
propose/final/cancel, and that the sender is a listed participant for
respond. A `plan.respond` for a revision other than the current one is
ignored.

### 6.3 Participant behaviour

On `plan.propose` the node computes which slots are free: inside the owner's
hours, with no overlap with busy time plus a buffer, checked for the first 4
occurrences if the plan recurs. Then:

- **`autoconfirm` granted and at least one slot is free:** reply
  `accept` with those slots, then notify the owner.
- **Otherwise:** create an actionable inbox item and wait for the owner to
  accept (a subset of options), decline, or counter (with suggested times).

A participant may decline a confirmed plan at any time. The organizer is
told, and the plan stays confirmed.

**Tentative holds.** Nodes SHOULD treat times on unresolved plans as busy
when answering other proposals. For the organizer, that means every
candidate slot. For a participant, it means the slots they accepted. This
stops two negotiations running at once from landing on the same evening. A
plan's own holds are ignored when re-evaluating that plan.

**Reminders.** The organizer MAY send one `plan.nudge` per revision to each
participant who hasn't answered. Due times:
- with a deadline: in its last quarter, and at least one hour before it;
- without a deadline: after a configurable delay (default 24 h).

The receiver ignores a nudge if it has already answered.

### 6.4 Organizer tally

Let `need` be the number of participants for `all`, or `min(N, participants)`.
For each slot, count the participants who accepted it.

- **Confirm:** for quorum `all`, once every participant has answered and some
  slot's count equals `need`. For numeric quorum, as soon as some slot's count
  reaches `need`. Among eligible slots, pick the one the most participants
  listed in `prefer` (a subset of their `ok_slots`), breaking ties by
  earliest start. Broadcast
  `plan.final` to *all* participants (people who didn't pick that slot get an
  invitation to join).
- **Needs reschedule:** when no slot can still reach `need` even if every
  pending participant accepted it, or when the deadline has passed. The
  organizer revises (new slots, `rev+1`, all answers reset) or cancels.

## 7. Files and notes

- `file.send {name, mime, sha256, data (base64, ≤10 MiB decoded), note}`.
  Receivers MUST verify `sha256`. They MUST sanitize `name` to a basename.
  They SHOULD store the file under a per-contact directory.
- `note {msg_id, text (≤4000 chars), plan_id?, reply_to?, expects_reply?}`:
  a short message for the other person or their agent.
  - `expects_reply` marks a question.
  - `reply_to` carries the `msg_id` being answered, which lets agents run
    simple ask/answer exchanges.

## 7a. Introductions (vouched pairing)

A node that is a contact of both A and B can introduce them, so they never
have to exchange invite tokens:

1. **Offer.** The introducer I sends
   `intro.offer {intro_id, note, peer: {id, name, endpoint, relay}}` to A
   (about B) and to B (about A). `intro_id` is 8–64 alphanumeric characters.
   A receiver MUST hold the `intros` grant for I. It stores the offer (valid
   14 days) and asks its owner to approve.
2. **Approve.** When an owner approves, their node stores the peer as a
   `pending` contact and sends `pair.intro {intro_id, name, endpoint, relay}`
   directly to the peer.
3. **Handle `pair.intro`.** It may come from a non-contact or a pending
   contact. The receiver MUST find an offer with that `intro_id` whose
   `peer.id` equals the envelope sender, and then:
   - **its owner hasn't decided yet:** record that the peer is ready and
     acknowledge;
   - **its owner declined:** refuse with `-32001`;
   - **its owner approved:** activate the contact and reply `pair.accept`.
4. **Connect.** If an owner approves after the peer is already ready, the
   contact becomes active at once.

Trust note: you are trusting I's claim that `peer.id` is really B.
Fingerprints appear in both inboxes so they can be checked.

## 7b. Key rotation

1. **Announce.** The node creates a new keypair N and sends every active
   contact `key.rotate {new_id: N, ts, proof}`. The envelope is signed with
   the **old** key. `proof` is N's Ed25519 signature over
   `canonical({"old": old_id, "new": N, "ts": ts})`, which proves the new key
   is really held.
2. **Receive.** The receiver checks `proof` and that `ts` is within the
   envelope age window. It MUST refuse if N already belongs to another
   contact. It then re-points every local record from the old id to N.
3. **Grace period.** The rotating node keeps the old key for 30 days. It
   opens envelopes still addressed to it and keeps polling relays under it.

Senders MUST deliver each recipient's messages in order, never letting a
retry of an earlier message be overtaken by a later one. That guarantees
`key.rotate` arrives before anything signed with the new key.

## 8. Versioning

A future incompatible change bumps `v` (`confer/2`). A node MUST reject
versions it doesn't understand. New message `type`s MAY be added in a minor
revision. Receivers refuse unknown types with `-32001`, so senders can
detect support.
