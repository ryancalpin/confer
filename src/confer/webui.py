"""Mobile-first web inbox for Confer.

Served at GET /ui/<ui_token>  — secret-token URL.
Actions at  POST /ui/<ui_token>/act  (application/x-www-form-urlencoded).

The page is entirely self-contained (no external assets) so it loads on a
phone over a slow Tailscale connection with no dependency on the open internet.
"""

from __future__ import annotations

import hmac
import html
import secrets
import urllib.parse
from typing import TYPE_CHECKING

from .identity import fingerprint

if TYPE_CHECKING:
    from .node import Node

# --------------------------------------------------------------------------- helpers


def _e(text: object) -> str:
    """HTML-escape any value to a safe string."""
    return html.escape(str(text) if text is not None else "")


# --------------------------------------------------------------------------- page


_CSS = """
:root {
  --bg: #f5f5f5;
  --card: #ffffff;
  --border: #ddd;
  --text: #222;
  --muted: #666;
  --accent: #2563eb;
  --accent-fg: #fff;
  --danger: #dc2626;
  --danger-fg: #fff;
  --ok: #16a34a;
  --ok-fg: #fff;
  --highlight: #fef9c3;
  --highlight-border: #ca8a04;
  --radius: 8px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1a1a1a;
    --card: #2a2a2a;
    --border: #444;
    --text: #e8e8e8;
    --muted: #aaa;
    --accent: #3b82f6;
    --accent-fg: #fff;
    --danger: #ef4444;
    --ok: #22c55e;
    --highlight: #422006;
    --highlight-border: #d97706;
  }
}
:root[data-theme="dark"] {
  --bg: #1a1a1a;
  --card: #2a2a2a;
  --border: #444;
  --text: #e8e8e8;
  --muted: #aaa;
  --accent: #3b82f6;
  --accent-fg: #fff;
  --danger: #ef4444;
  --ok: #22c55e;
  --highlight: #422006;
  --highlight-border: #d97706;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
  padding: 0 16px 32px;
  max-width: 640px;
  margin: 0 auto;
}
h1 { font-size: 1.35rem; margin: 20px 0 4px; }
h2 { font-size: 1.1rem; margin: 24px 0 10px; color: var(--muted); letter-spacing: .02em; text-transform: uppercase; font-size: .8rem; }
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
  margin-bottom: 10px;
}
.card.actionable {
  border-color: var(--highlight-border);
  background: var(--highlight);
}
.card-title { font-weight: 600; font-size: 1rem; }
.card-meta { font-size: .85rem; color: var(--muted); margin-top: 2px; }
button, input[type=submit] {
  display: inline-block;
  padding: 10px 18px;
  min-height: 44px;
  border: none;
  border-radius: var(--radius);
  font-size: .95rem;
  font-weight: 500;
  cursor: pointer;
  background: var(--accent);
  color: var(--accent-fg);
  margin-top: 8px;
  margin-right: 8px;
}
button.danger, input[type=submit].danger { background: var(--danger); color: var(--danger-fg); }
button.ok, input[type=submit].ok { background: var(--ok); color: var(--ok-fg); }
button.secondary { background: var(--border); color: var(--text); }
label { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; cursor: pointer; }
label input[type=checkbox] { width: 20px; height: 20px; margin-top: 2px; flex-shrink: 0; }
.slot-label { font-size: .95rem; }
.error-banner {
  background: #fee2e2;
  border: 1px solid var(--danger);
  border-radius: var(--radius);
  padding: 12px 16px;
  color: var(--danger);
  margin-bottom: 16px;
  font-size: .95rem;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .error-banner { background: #450a0a; }
}
:root[data-theme="dark"] .error-banner { background: #450a0a; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 99px;
  font-size: .75rem;
  font-weight: 600;
  margin-left: 6px;
  vertical-align: middle;
}
.badge-proposed { background: #dbeafe; color: #1e40af; }
.badge-confirmed { background: #dcfce7; color: #15803d; }
.badge-cancelled { background: #fee2e2; color: #991b1b; }
.badge-needs_reschedule { background: #fef9c3; color: #854d0e; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .badge-proposed { background: #1e3a5f; color: #93c5fd; }
  :root:not([data-theme="light"]) .badge-confirmed { background: #14532d; color: #86efac; }
  :root:not([data-theme="light"]) .badge-cancelled { background: #450a0a; color: #fca5a5; }
  :root:not([data-theme="light"]) .badge-needs_reschedule { background: #422006; color: #fcd34d; }
}
:root[data-theme="dark"] .badge-proposed { background: #1e3a5f; color: #93c5fd; }
:root[data-theme="dark"] .badge-confirmed { background: #14532d; color: #86efac; }
:root[data-theme="dark"] .badge-cancelled { background: #450a0a; color: #fca5a5; }
:root[data-theme="dark"] .badge-needs_reschedule { background: #422006; color: #fcd34d; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th { text-align: left; padding: 6px 8px; background: var(--border); }
td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
td:last-child { font-family: monospace; font-size: .78rem; word-break: break-all; }
.empty-note { color: var(--muted); font-style: italic; font-size: .9rem; margin: 4px 0; }
"""


def _badge(status: str) -> str:
    return f'<span class="badge badge-{_e(status)}">{_e(status)}</span>'


def render_page(node: "Node", error: str = "") -> bytes:
    """Build and return the full HTML page as UTF-8 bytes."""
    from .identity import fingerprint as fp

    inbox_items = node.inbox()
    all_plans = node.plans()
    contacts = node.store.contacts()

    # Separate actionable items
    actionable = [i for i in inbox_items if i.get("actionable") and i.get("status") != "done"]
    other_items = [i for i in inbox_items if not i.get("actionable") and i.get("status") != "done"]

    # Plans waiting for participant response
    invite_plans = [
        p for p in all_plans
        if p.get("role") == "participant"
        and p.get("my_status") == "invited"
        and p.get("status") == "proposed"
    ]

    token = node.config.get("ui_token", "")
    pending_intros = [i for i in node.intros() if i["status"] == "offered" and i["expires_at"] > node.now()]

    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Confer — {_e(node.name)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Confer &mdash; {_e(node.name)}</h1>
""")

    if error:
        parts.append(f'<div class="error-banner">{_e(error)}</div>\n')

    # ----- Actionable inbox items
    if actionable:
        parts.append('<h2>Needs attention</h2>\n')
        for item in actionable:
            parts.append(f"""<div class="card actionable">
<div class="card-title">{_e(item.get("summary", ""))}</div>
<form method="post" action="/ui/{_e(token)}/act">
  <input type="hidden" name="t" value="{_e(token)}">
  <input type="hidden" name="action" value="dismiss">
  <input type="hidden" name="item_id" value="{_e(item.get('id', ''))}">
  <button type="submit" class="secondary">Dismiss</button>
</form>
</div>
""")

    # ----- Introductions waiting for a yes/no
    if pending_intros:
        parts.append('<h2>Introductions</h2>\n')
        for intro in pending_intros:
            via = node.store.contact(intro["introducer"])
            parts.append(f"""<div class="card actionable">
<div class="card-title">Meet {_e(intro["peer_name"])}?</div>
<div class="card-meta">Introduced by {_e(via.name if via else "a contact")} · fingerprint {_e(fingerprint(intro["peer_id"]))}</div>
""")
            if intro.get("note"):
                parts.append(f'<div class="card-meta">{_e(intro["note"])}</div>\n')
            parts.append(f"""<form method="post" action="/ui/{_e(token)}/act">
  <input type="hidden" name="t" value="{_e(token)}">
  <input type="hidden" name="action" value="intro">
  <input type="hidden" name="intro_id" value="{_e(intro["intro_id"])}">
  <input type="submit" name="decision" value="accept" class="ok">
  <input type="submit" name="decision" value="decline" class="danger">
</form>
</div>
""")

    # ----- Pending plan invites
    if invite_plans:
        parts.append('<h2>Plan invitations</h2>\n')
        for plan in invite_plans:
            suggested = set(plan.get("suggested", []))
            slots = plan.get("slots", [])
            plan_id = _e(plan.get("id", ""))
            parts.append(f"""<div class="card actionable">
<div class="card-title">{_e(plan.get("title", ""))}</div>
<div class="card-meta">From {_e(plan.get("organizer_name", ""))}</div>
""")
            if plan.get("location"):
                parts.append(f'<div class="card-meta">📍 {_e(plan["location"])}</div>\n')
            if plan.get("notes"):
                parts.append(f'<div class="card-meta">{_e(plan["notes"])}</div>\n')

            parts.append(f"""<form method="post" action="/ui/{_e(token)}/act">
  <input type="hidden" name="t" value="{_e(token)}">
  <input type="hidden" name="action" value="respond">
  <input type="hidden" name="plan_id" value="{plan_id}">
""")
            for i, slot_dict in enumerate(slots):
                checked = " checked" if i in suggested else ""
                label_text = _e(node._fmt_slot(slot_dict))
                parts.append(f"""  <label>
    <input type="checkbox" name="slot" value="{i}"{checked}>
    <span class="slot-label">{label_text}</span>
  </label>
""")
            parts.append(f"""  <input type="submit" name="decision" value="accept" class="ok">
  <input type="submit" name="decision" value="decline" class="danger">
</form>
</div>
""")

    # ----- Other inbox items
    if other_items:
        parts.append('<h2>Inbox</h2>\n')
        for item in other_items:
            parts.append(f"""<div class="card">
<div class="card-title">{_e(item.get("summary", ""))}</div>
<form method="post" action="/ui/{_e(token)}/act">
  <input type="hidden" name="t" value="{_e(token)}">
  <input type="hidden" name="action" value="dismiss">
  <input type="hidden" name="item_id" value="{_e(item.get('id', ''))}">
  <button type="submit" class="secondary">Dismiss</button>
</form>
</div>
""")

    if not actionable and not invite_plans and not other_items:
        parts.append('<div class="card"><span class="empty-note">Inbox is empty.</span></div>\n')

    # ----- Plans list
    parts.append('<h2>Plans</h2>\n')
    if all_plans:
        for plan in all_plans:
            status = plan.get("status", "")
            parts.append(f"""<div class="card">
<div class="card-title">{_e(plan.get("title", ""))}{_badge(status)}</div>
<div class="card-meta">{_e(plan.get("organizer_name", ""))}</div>
</div>
""")
    else:
        parts.append('<div class="card"><span class="empty-note">No plans yet.</span></div>\n')

    # ----- Contacts list
    parts.append('<h2>Contacts</h2>\n')
    if contacts:
        parts.append('<div class="card"><table><thead><tr><th>Name</th><th>Grants</th><th>Fingerprint</th></tr></thead><tbody>\n')
        for c in contacts:
            grants_text = ", ".join(c.grants) if c.grants else "—"
            fprint = fp(c.agent_id)
            parts.append(f'<tr><td>{_e(c.name)}</td><td>{_e(grants_text)}</td><td>{_e(fprint)}</td></tr>\n')
        parts.append('</tbody></table></div>\n')
    else:
        parts.append('<div class="card"><span class="empty-note">No contacts yet.</span></div>\n')

    parts.append('</body>\n</html>\n')

    return "".join(parts).encode("utf-8")


# Security headers to send with every UI response
UI_SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
]


def handle_get(node: "Node", token: str, request_token: str) -> tuple[int, bytes]:
    """Return (status_code, body_bytes). Token comparison is constant-time."""
    if not token or not hmac.compare_digest(token, request_token):
        return 404, b'{"error":"not found"}'
    body = render_page(node)
    return 200, body


def handle_post(node: "Node", token: str, request_token: str, body_bytes: bytes) -> tuple[int, str]:
    """Parse form body, dispatch action, return (redirect_path, or error page path).

    Returns (303_redirect_to, "") or (200_error, error_html_bytes).
    Actually returns (status, redirect_location_or_empty, optional_error_body).
    """
    if not token or not hmac.compare_digest(token, request_token):
        return 404, "", b'{"error":"not found"}'

    try:
        form = urllib.parse.parse_qs(body_bytes.decode("utf-8", errors="replace"), keep_blank_values=True)
    except Exception:
        form = {}

    # Defense-in-depth: verify hidden t field equals the token
    t_values = form.get("t", [])
    if not t_values or not hmac.compare_digest(t_values[0], token):
        return 403, "", b'{"error":"forbidden"}'

    action = (form.get("action") or [""])[0]
    error = ""

    if action == "dismiss":
        item_id_str = (form.get("item_id") or [""])[0]
        try:
            item_id = int(item_id_str)
            node.dismiss(item_id)
        except (ValueError, Exception) as exc:
            error = str(exc)
    elif action == "respond":
        plan_id = (form.get("plan_id") or [""])[0]
        decision = (form.get("decision") or [""])[0]
        slot_strs = form.get("slot", [])
        try:
            slots_list: list[int] | None = [int(s) for s in slot_strs] if decision == "accept" else None
            from .node import NodeError
            node.respond(plan_id, decision, slots=slots_list)
        except Exception as exc:
            error = str(exc)
    elif action == "intro":
        intro_id = (form.get("intro_id") or [""])[0]
        decision = (form.get("decision") or [""])[0]
        try:
            if decision == "accept":
                node.accept_intro(intro_id)
            elif decision == "decline":
                node.decline_intro(intro_id)
            else:
                error = "choose accept or decline"
        except Exception as exc:
            error = str(exc)
    else:
        error = f"unknown action {action!r}"

    if error:
        body = render_page(node, error=error)
        return 200, "", body

    return 303, f"/ui/{token}", b""
