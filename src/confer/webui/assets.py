"""Static assets for the phone UI: stylesheet, the one small script, tab icons.

Everything is inlined into each page (no external requests), so the UI works
over a slow Tailscale link with no dependency on the open internet. The script
runs only with the per-response CSP nonce and attaches its handlers with
``addEventListener`` — there are no inline event-handler attributes.
"""

from __future__ import annotations

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f2f2f7; --card: #ffffff; --border: #e0e0e6; --text: #1c1c1e; --muted: #6b6b73;
  --accent: #2563eb; --accent-fg: #ffffff; --accent-soft: #dbeafe;
  --danger: #dc2626; --danger-soft: #fee2e2; --ok: #15803d; --ok-soft: #dcfce7;
  --warn: #a16207; --warn-soft: #fef9c3; --field: #f7f7fa;
  --radius: 14px; --tabbar: 64px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #000000; --card: #1c1c1e; --border: #2c2c30; --text: #f2f2f7; --muted: #9a9aa2;
    --accent: #3b82f6; --accent-soft: #172554; --danger: #f87171; --danger-soft: #450a0a;
    --ok: #4ade80; --ok-soft: #052e16; --warn: #facc15; --warn-soft: #422006; --field: #2c2c2e;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  padding-bottom: calc(var(--tabbar) + env(safe-area-inset-bottom) + 24px);
  overflow-wrap: anywhere;
}
a { color: var(--accent); }
.appbar {
  position: sticky; top: 0; z-index: 5; background: var(--bg);
  padding: calc(env(safe-area-inset-top) + 10px) 16px 8px; border-bottom: 1px solid var(--border);
}
.appbar h1 { font-size: 1.6rem; margin: 0; letter-spacing: -.01em; }
.appbar .who { color: var(--muted); font-size: .85rem; }
main { padding: 8px 16px 0; max-width: 640px; margin: 0 auto; }
h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 22px 4px 8px; font-weight: 600; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 16px; margin-bottom: 10px; }
.card.hot { border-color: var(--warn); box-shadow: inset 4px 0 0 var(--warn); }
.card-title { font-weight: 600; }
.meta { color: var(--muted); font-size: .88rem; margin-top: 2px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.row > form { margin: 0; }
.spacer { flex: 1; }
.empty { color: var(--muted); font-style: italic; }
.banner { border-radius: var(--radius); padding: 12px 16px; margin: 12px 0; font-size: .95rem; }
.banner.error { background: var(--danger-soft); color: var(--danger); border: 1px solid var(--danger); }
.banner.notice { background: var(--ok-soft); color: var(--ok); border: 1px solid var(--ok); }
button, .btn {
  -webkit-appearance: none; appearance: none; display: inline-flex; align-items: center; justify-content: center;
  min-height: 44px; min-width: 44px; padding: 10px 16px; margin-top: 8px; border: 0; border-radius: 12px;
  background: var(--accent); color: var(--accent-fg); font: inherit; font-weight: 600; cursor: pointer; text-decoration: none;
}
button.secondary, .btn.secondary { background: var(--field); color: var(--text); border: 1px solid var(--border); }
button.danger { background: var(--danger); color: #fff; }
button.ok { background: var(--ok); color: #fff; }
@media (prefers-color-scheme: dark) { button.ok, button.danger { color: #000; } }
button.small { min-height: 44px; padding: 6px 12px; font-size: .9rem; font-weight: 500; margin-top: 0; }
button.wide { width: 100%; }
button[hidden] { display: none; }
label.field { display: block; margin-top: 12px; font-size: .9rem; color: var(--muted); }
label.field > span { display: block; margin-bottom: 4px; }
input[type=text], input[type=url], input[type=number], input[type=date], input[type=time], input[type=file],
select, textarea {
  width: 100%; min-height: 44px; padding: 10px 12px; font: inherit; font-size: 16px; color: var(--text);
  background: var(--field); border: 1px solid var(--border); border-radius: 10px;
}
textarea { min-height: 88px; resize: vertical; }
textarea.token { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8rem; min-height: 110px; }
.pair { display: flex; gap: 8px; }
.pair > * { flex: 1; min-width: 0; }
label.check { display: flex; gap: 12px; align-items: flex-start; padding: 10px 0; min-height: 44px; cursor: pointer; }
fieldset > label.check + label.check, .optrow + .optrow { border-top: 1px solid var(--border); }
label.check input { width: 22px; height: 22px; margin: 1px 0 0; flex-shrink: 0; accent-color: var(--accent); }
label.check small { display: block; color: var(--muted); }
fieldset { border: 0; padding: 0; margin: 12px 0 0; min-width: 0; }
legend { font-size: .9rem; color: var(--muted); padding: 0; margin-bottom: 2px; }
details.card > summary { list-style: none; cursor: pointer; font-weight: 600; min-height: 24px; display: flex; align-items: center; }
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::after { content: "+"; margin-left: auto; color: var(--muted); font-size: 1.3rem; line-height: 1; }
details.card[open] > summary::after { content: "\\2212"; }
.pill { display: inline-block; padding: 1px 9px; border-radius: 99px; font-size: .75rem; font-weight: 600; vertical-align: middle; margin-left: 6px; background: var(--field); color: var(--muted); }
.pill.proposed { background: var(--accent-soft); color: var(--accent); }
.pill.confirmed, .pill.accepted { background: var(--ok-soft); color: var(--ok); }
.pill.cancelled, .pill.disputed, .pill.declined { background: var(--danger-soft); color: var(--danger); }
.pill.needs_reschedule, .pill.pending, .pill.invited { background: var(--warn-soft); color: var(--warn); }
ul.items { list-style: none; margin: 8px 0 0; padding: 0; }
ul.items li { display: flex; gap: 8px; align-items: center; padding: 6px 0; border-top: 1px solid var(--border); }
ul.items li:first-child { border-top: 0; }
ul.items li .text { flex: 1; min-width: 0; }
ul.items li.done .text { text-decoration: line-through; color: var(--muted); }
ul.items form { margin: 0; }
.tick { width: 44px; height: 44px; margin: 0; padding: 0; border-radius: 50%; background: var(--field); color: var(--text); border: 2px solid var(--border); font-size: 1.1rem; }
li.done .tick { background: var(--ok); border-color: var(--ok); color: #fff; }
.amount { font-variant-numeric: tabular-nums; font-weight: 600; }
.pos { color: var(--ok); } .neg { color: var(--danger); }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85rem; }
nav.tabs {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 10; display: flex;
  background: var(--card); border-top: 1px solid var(--border);
  padding-bottom: env(safe-area-inset-bottom);
}
nav.tabs a {
  flex: 1; min-width: 0; height: var(--tabbar); display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; text-decoration: none; color: var(--muted); font-size: .68rem; position: relative;
}
nav.tabs a[aria-current=page] { color: var(--accent); font-weight: 600; }
nav.tabs svg { width: 26px; height: 26px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
nav.tabs .count {
  position: absolute; top: 6px; left: calc(50% + 6px); min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
  background: var(--danger); color: #fff; font-size: .7rem; font-weight: 700; line-height: 18px; text-align: center;
}
"""

# One small script, loaded with the CSP nonce. Only three behaviours:
#   [data-copy=ID]   copy the text of element ID to the clipboard
#   [data-share=ID]  open the phone's share sheet with that text (if supported)
#   [data-geo]       on tap, read the phone's location into the form, then submit
JS = """
(function () {
  function say(el, msg) { var s = el.parentNode.querySelector('.js-status'); if (s) { s.textContent = msg; } }
  document.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      var src = document.getElementById(b.getAttribute('data-copy'));
      if (!src) { return; }
      var text = src.value || src.textContent;
      function fallback() { src.focus(); src.select(); try { document.execCommand('copy'); say(b, 'Copied'); } catch (e) { say(b, 'Select and copy it'); } }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { say(b, 'Copied'); }, fallback);
      } else { fallback(); }
    });
  });
  document.querySelectorAll('[data-share]').forEach(function (b) {
    if (!navigator.share) { return; }
    b.hidden = false;
    b.addEventListener('click', function () {
      var src = document.getElementById(b.getAttribute('data-share'));
      if (src) { navigator.share({ text: src.value || src.textContent }).catch(function () {}); }
    });
  });
  document.querySelectorAll('[data-geo]').forEach(function (b) {
    if (!navigator.geolocation) { b.hidden = true; return; }
    b.addEventListener('click', function () {
      var f = b.form;
      say(b, 'Getting your location…');
      navigator.geolocation.getCurrentPosition(function (pos) {
        f.elements.lat.value = pos.coords.latitude.toFixed(5);
        f.elements.lon.value = pos.coords.longitude.toFixed(5);
        f.elements.accuracy.value = Math.round(pos.coords.accuracy || 0);
        f.submit();
      }, function (err) {
        say(b, 'Location unavailable: ' + (err && err.message ? err.message : 'permission denied'));
      }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
    });
  });
})();
"""

# Stroke icons (24x24) for the tab bar — inline SVG, no image requests.
ICONS = {
    "inbox": '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 3h6l1-3h5"/>',
    "plans": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    "lists": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/>',
    "money": '<rect x="2" y="6" width="20" height="13" rx="2"/><circle cx="12" cy="12.5" r="2.5"/><path d="M6 9.5v6M18 9.5v6"/>',
    "people": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/>'
              '<path d="M16 4.8a3.3 3.3 0 010 6.4M18 14.8c1.8.8 3 2.5 3.5 5.2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M4.2 5.8l2.1 2.1M17.7 16.1l2.1 2.1'
                'M2.5 12h3M18.5 12h3M4.2 18.2l2.1-2.1M17.7 7.9l2.1-2.1"/>',
}
