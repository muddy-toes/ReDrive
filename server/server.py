"""server.py — ReDrive relay server.

Runs on a DigitalOcean droplet (or any Linux VPS).  Multiple riders connect
to one driver's room; the pattern engine lives here, T-code is broadcast to
all connected rider WebSockets.

Usage:
    python server/server.py [--port 8765]

Requires: pip install aiohttp
"""

import asyncio
import json
import queue
import random
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

# ── Mock tkinter so redrive.py can be imported headlessly ────────────────────
sys.modules.setdefault("tkinter",     MagicMock())
sys.modules.setdefault("tkinter.ttk", MagicMock())

# ── Import engine + HTML strings from parent package ────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from redrive import DriveEngine, DriveConfig, DRIVER_HTML, TOUCH_HTML, PRESETS

import aiohttp
from aiohttp import web

# ── Room code alphabet — no ambiguous chars (0/O, 1/I/L) ───────────────────
_ROOM_CHARS   = "BCDFGHJKMNPQRSTVWXYZ23456789"
_CODE_LEN     = 10
_ROOM_EXPIRY        = 86_400   # 24 h in seconds
_DRIVER_GRACE       = 300      # 5 min grace after driver goes quiet
_CLEANUP_INTERVAL   = 30       # sweep every 30 s (catches grace expiry quickly)

# ── Global room registry ─────────────────────────────────────────────────────
_rooms: dict[str, "Room"] = {}


def _new_code() -> str:
    for _ in range(20):
        code = "".join(random.choices(_ROOM_CHARS, k=_CODE_LEN))
        if code not in _rooms:
            return code
    raise RuntimeError("Failed to generate unique room code")


class Room:
    """One driver session with N riders."""

    def __init__(self, code: str, main_loop: asyncio.AbstractEventLoop,
                 waiting: bool = False):
        self.code         = code
        self.driver_key      = "" if waiting else secrets.token_urlsafe(20)
        self.created_at      = time.monotonic()
        self.driver_last_seen = time.monotonic()   # updated on every valid driver request
        self.bottle_until: float = 0.0
        self.rider_wss:  set[web.WebSocketResponse] = set()
        self._main_loop  = main_loop
        self._log_q      = queue.Queue()
        # Waiting room support
        self.waiting: bool = waiting
        self.waiting_expires: float = time.time() + 1800 if waiting else 0.0
        # Public session list
        self.public: bool = True
        # Custom anatomy uploads
        self.custom_anatomies: list = []
        if not waiting:
            cfg          = DriveConfig()   # defaults — no ReStim URL needed
            self.engine  = DriveEngine(cfg, {}, self._log_q,
                                       send_hook=self._hook)
            self.engine.start()
        else:
            self.engine  = None

    # Called from the engine's background thread — schedule on main loop
    def _hook(self, cmd: str):
        asyncio.run_coroutine_threadsafe(self._broadcast(cmd), self._main_loop)

    async def _broadcast(self, cmd: str):
        dead = set()
        for ws in list(self.rider_wss):
            try:
                await ws.send_str(cmd)
            except Exception:
                dead.add(ws)
        self.rider_wss -= dead

    def touch_driver(self):
        self.driver_last_seen = time.monotonic()

    def expired(self) -> bool:
        now = time.monotonic()
        if now - self.created_at > _ROOM_EXPIRY:
            return True
        # Grace period: expire if driver has been gone longer than _DRIVER_GRACE
        if now - self.driver_last_seen > _DRIVER_GRACE:
            return True
        return False

    def stop(self):
        if self.engine is not None:
            self.engine.stop()

    @property
    def rider_count(self) -> int:
        return len(self.rider_wss)


# ── Rider info page ──────────────────────────────────────────────────────────

_RIDER_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReDrive &middot; Rider</title>
<style>
  :root {{ --bg:#111; --bg2:#1a1a1a; --bg3:#222; --border:#2a2a2a;
           --fg:#fff; --fg2:#999; --accent:#5fa3ff; --ok:#4caf50; --err:#f44336; --warn:#ff9800; }}
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:var(--bg); color:var(--fg); font:15px/1.6 system-ui,sans-serif;
          max-width:480px; margin:0 auto; padding:1.5rem; }}
  h1 {{ color:var(--accent); font-size:1.6rem; margin-bottom:.2rem }}
  .sub {{ color:var(--fg2); font-size:.9rem; margin-bottom:1.8rem }}
  .card {{ background:var(--bg2); border:1px solid var(--border); border-radius:8px;
           padding:1.2rem 1.4rem; margin-bottom:1rem }}
  .card h2 {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.08em;
               color:var(--fg2); margin-bottom:.9rem; font-weight:600 }}
  code {{ background:var(--bg3); padding:.15em .45em; border-radius:3px;
          font-family:monospace; font-size:.92em; color:var(--accent) }}
  .step {{ display:flex; gap:.8rem; align-items:baseline; margin-bottom:.6rem }}
  .step-n {{ color:var(--accent); font-weight:700; font-size:.85rem; flex-shrink:0 }}
  .step p {{ color:var(--fg2); font-size:.9rem }}
  /* live status */
  #status-dot {{ display:inline-block; width:9px; height:9px; border-radius:50%;
                  background:var(--err); margin-right:6px; vertical-align:middle }}
  #status-txt {{ color:var(--fg2); font-size:.85rem; vertical-align:middle }}
  .stat-row {{ display:flex; justify-content:space-between; align-items:center;
               padding:.55rem 0; border-bottom:1px solid var(--border) }}
  .stat-row:last-child {{ border-bottom:none }}
  .stat-label {{ color:var(--fg2); font-size:.85rem }}
  .stat-value {{ color:var(--fg); font-size:.95rem; font-weight:600 }}
  #vol-bar-wrap {{ background:var(--bg3); border-radius:4px; height:8px;
                   flex:1; margin:0 .8rem; overflow:hidden }}
  #vol-bar {{ height:100%; border-radius:4px; background:var(--accent);
               width:0%; transition:width .4s }}
  #ramp-row {{ display:none }}
  #ramp-bar-wrap {{ background:var(--bg3); border-radius:4px; height:6px;
                    flex:1; margin:0 .8rem; overflow:hidden }}
  #ramp-bar {{ height:100%; border-radius:4px; background:var(--warn); width:0% }}
</style>
</head>
<body>
<h1>ReDrive</h1>
<p class="sub">Room <strong style="color:var(--accent);letter-spacing:.1em">{code}</strong></p>

<div class="card">
  <h2>How to connect</h2>
  <div class="step"><span class="step-n">1</span>
    <p>Make sure <strong>ReStim</strong> is open on your device with WebSocket enabled.</p></div>
  <div class="step"><span class="step-n">2</span>
    <p>Run the rider app or: <code>python rider_client.py {code}</code></p></div>
  <div class="step"><span class="step-n">3</span>
    <p>Keep this page open to see live output. Your device's own power limits always apply.</p></div>
</div>

<div class="card">
  <h2>Live output &nbsp;<span id="status-dot"></span><span id="status-txt">connecting…</span></h2>
  <div class="stat-row">
    <span class="stat-label">Pattern</span>
    <span class="stat-value" id="s-pattern">—</span>
  </div>
  <div class="stat-row">
    <span class="stat-label">Intensity</span>
    <span class="stat-value" id="s-intensity">—</span>
  </div>
  <div class="stat-row">
    <span class="stat-label">Output</span>
    <div id="vol-bar-wrap"><div id="vol-bar"></div></div>
    <span class="stat-value" id="s-vol">—</span>
  </div>
  <div class="stat-row" id="ramp-row">
    <span class="stat-label">Ramp</span>
    <div id="ramp-bar-wrap"><div id="ramp-bar"></div></div>
    <span class="stat-value" id="s-ramp">—</span>
  </div>
</div>

<div id="bottle-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.93);z-index:9999;flex-direction:column;align-items:center;justify-content:center;gap:1rem">
  <img src="/bottle.png" style="max-width:80vmin;max-height:72vmin;object-fit:contain;border-radius:8px">
  <div id="bottle-cd" style="color:#fff;font-size:1.1rem;font-family:monospace;opacity:0.7"></div>
</div>

<script>
const STATE_URL = '{prefix}/state';
let errCount = 0;
async function poll() {{
  try {{
    const d = await (await fetch(STATE_URL)).json();
    errCount = 0;
    document.getElementById('status-dot').style.background = 'var(--ok)';
    document.getElementById('status-txt').textContent = 'live';
    document.getElementById('s-pattern').textContent   = d.pattern  ?? '—';
    document.getElementById('s-intensity').textContent = d.intensity != null
      ? Math.round(d.intensity * 100) + '%' : '—';
    const volPct = d.vol != null ? Math.round(d.vol * 100) : 0;
    document.getElementById('vol-bar').style.width = volPct + '%';
    document.getElementById('s-vol').textContent   = volPct + '%';
    const rampRow = document.getElementById('ramp-row');
    if (d.ramp_active) {{
      rampRow.style.display = 'flex';
      const pct = Math.round((d.ramp_progress ?? 0) * 100);
      document.getElementById('ramp-bar').style.width = pct + '%';
      document.getElementById('s-ramp').textContent   =
        pct + '% → ' + Math.round((d.ramp_target ?? 0) * 100) + '%';
    }} else {{
      rampRow.style.display = 'none';
    }}
    const overlay = document.getElementById('bottle-overlay');
    if (d.bottle_active) {{
      overlay.style.display = 'flex';
      document.getElementById('bottle-cd').textContent = d.bottle_remaining + 's';
    }} else {{
      overlay.style.display = 'none';
    }}
  }} catch(e) {{
    errCount++;
    if (errCount > 2) {{
      document.getElementById('status-dot').style.background = 'var(--err)';
      document.getElementById('status-txt').textContent = 'disconnected';
    }}
  }}
}}
poll();
setInterval(poll, 1500);
</script>
<script src='https://storage.ko-fi.com/cdn/scripts/overlay-widget.js'></script>
<script>
  kofiWidgetOverlay.draw('stimstation', {{
    'type': 'floating-chat',
    'floating-chat.donateButton.text': 'Support Us',
    'floating-chat.donateButton.background-color': '#d9534f',
    'floating-chat.donateButton.text-color': '#fff'
  }});
</script>
</body>
</html>
"""


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _inject_prefix(html: str, prefix: str, driver_key: str = "") -> str:
    """Rewrite absolute API paths to be room-scoped and inject driver key."""
    html = (html
            .replace('"/command"',  f'"{prefix}/command"')
            .replace("'/command'",  f"'{prefix}/command'")
            .replace('"/state"',    f'"{prefix}/state"')
            .replace("'/state'",    f"'{prefix}/state'")
            .replace('fetch("/touch"', f'fetch("{prefix}/touch"')
            .replace('href="/touch"', f'href="{prefix}/touch"')
            .replace("'/bottle?duration='", f"'{prefix}/bottle?duration='"))
    if driver_key:
        key_script = (
            f'<script>const DRIVER_KEY="{driver_key}";\n'
            f'const _origFetch=window.fetch;\n'
            f'window.fetch=function(url,opts={{}}){{'
            f'opts.headers={{...opts.headers,"X-Driver-Key":DRIVER_KEY}};'
            f'return _origFetch(url,opts);}};\n'
            # Heartbeat: POST /ping every 60s so grace timer doesn't expire on active driver
            f'setInterval(()=>fetch("ping",{{method:"POST"}}),60000);\n'
            f'</script>\n'
        )
        html = html.replace("</head>", key_script + "</head>", 1)
    return html


_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReDrive &middot; EstimStation</title>
<style>
  :root { --bg:#0e0e10; --bg2:#18181c; --bg3:#222228; --border:#2a2a32;
          --fg:#f0f0f5; --fg2:#8888a0; --accent:#5fa3ff; --accent2:#a855f7;
          --ok:#4caf50; --warn:#ff9800; }
  * { box-sizing:border-box; margin:0; padding:0 }
  body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center;
         min-height:100vh; padding:2rem }
  .site-header { width:100%; max-width:420px; display:flex; align-items:center;
                 justify-content:space-between; margin-bottom:2rem; padding-bottom:1rem;
                 border-bottom:1px solid var(--border) }
  .site-header a { text-decoration:none }
  .brand { font-size:1.1rem; font-weight:700; color:var(--fg); letter-spacing:.04em }
  .brand span { color:var(--accent) }
  h1 { font-size:2.2rem; letter-spacing:.05em;
       background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
       -webkit-background-clip:text; -webkit-text-fill-color:transparent;
       background-clip:text; margin-bottom:.25rem }
  p.sub { color:var(--fg2); margin-bottom:2.5rem; font-size:.95rem }
  .card { background:var(--bg2); border:1px solid var(--border); border-radius:10px;
          padding:2rem 2.5rem; width:100%; max-width:420px; margin-bottom:1.5rem }
  .card h2 { font-size:1.1rem; color:var(--fg2); text-transform:uppercase;
              letter-spacing:.08em; margin-bottom:1.2rem; font-weight:500 }
  button { width:100%; padding:.85rem;
           background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
           color:#fff; border:none; border-radius:6px; font-size:1rem; font-weight:700;
           cursor:pointer; transition:opacity .15s }
  button:hover { opacity:.85 }
  input { width:100%; padding:.75rem 1rem; background:var(--bg3);
          border:1px solid var(--border); border-radius:6px; color:var(--fg);
          font-size:1.1rem; letter-spacing:.15em; text-transform:uppercase;
          text-align:center; margin-bottom:1rem }
  input::placeholder { letter-spacing:normal; text-transform:none; color:var(--fg2) }
  .note { color:var(--fg2); font-size:.83rem; margin-top:1rem; line-height:1.6 }
  code { background:var(--bg3); padding:.1em .4em; border-radius:3px;
         font-family:monospace; font-size:.9em; color:var(--accent) }
  .faq { width:100%; max-width:420px; margin-bottom:2rem }
  .faq-section-label { font-size:.75rem; color:var(--accent); text-transform:uppercase;
                       letter-spacing:.12em; margin:1.4rem 0 .5rem; font-weight:700 }
  details { border-bottom:1px solid var(--border); padding:.6rem 0 }
  details:first-of-type { border-top:1px solid var(--border) }
  summary { cursor:pointer; font-size:.95rem; color:var(--fg); list-style:none;
            display:flex; justify-content:space-between; align-items:center;
            user-select:none; padding:.2rem 0 }
  summary::-webkit-details-marker { display:none }
  summary::after { content:'+'; color:var(--fg2); font-size:1.1rem; flex-shrink:0; margin-left:1rem }
  details[open] summary::after { content:'\u2212' }
  details p { color:var(--fg2); font-size:.9rem; line-height:1.7;
              padding:.6rem 0 .2rem; margin:0 }
  details p a { color:var(--accent); text-decoration:none }
  .warn { background:#1a1200; border:1px solid #3a2800; border-radius:8px;
          padding:.9rem 1.1rem; margin-bottom:1.5rem; width:100%; max-width:420px;
          color:#ffcc55; font-size:.88rem; line-height:1.6 }
  .warn strong { display:block; margin-bottom:.3rem; font-size:.95rem }
  .site-footer { width:100%; max-width:420px; text-align:center; margin-top:2rem;
                 padding-top:1.2rem; border-top:1px solid var(--border);
                 color:var(--fg2); font-size:.8rem; line-height:1.9 }
  .site-footer a { color:var(--accent); text-decoration:none }
  .site-footer a:hover { text-decoration:underline }
</style>
</head>
<body>

<header class="site-header">
  <a href="https://www.estimstation.com/" class="brand">
    <span>Estim</span>Station
  </a>
  <span style="color:var(--fg2);font-size:.82rem">ReDrive &middot; Remote Estim</span>
</header>

<h1>ReDrive</h1>
<p class="sub">Remote pattern engine for ReStim &mdash; by EstimStation</p>

<div class="warn">
  <strong>&#9888; Early alpha software</strong>
  This is experimental. Expect rough edges, disconnections, and missing features.
  Always keep your hand on your ReStim device's power dial &mdash; the driver controls
  pattern shape, but <em>you</em> control your maximum intensity.
</div>

<div class="faq">

  <div class="faq-section-label">For Riders</div>

  <details>
    <summary>What do I need to use ReDrive as a rider?</summary>
    <p>A PC running ReStim with its WebSocket server enabled, and Python 3.9+.
    Download the ReDrive Rider app or run <code>rider_client.py</code> directly.</p>
  </details>

  <details>
    <summary>How do I set up ReStim to work with ReDrive?</summary>
    <p>In ReStim, enable the WebSocket server (default port 12346). ReDrive connects
    to <code>ws://localhost:12346</code>. See the
    <a href="https://github.com/siotour/restim" target="_blank" rel="noopener">ReStim GitHub</a>
    for setup instructions.</p>
  </details>

  <details>
    <summary>My driver sent me a room code &mdash; what do I do?</summary>
    <p>Run <code>python rider_client.py ROOMCODE</code> or use the ReDrive Rider app,
    enter the code, and click Connect. Make sure ReStim is already running first.</p>
  </details>

  <details>
    <summary>How do I get started as a rider?</summary>
    <p>Download <strong>ReDrive Rider</strong> for
    <a href="/download/windows">Windows</a> or <a href="/download/mac">macOS</a>,
    install it, enter the room code your driver shares with you, and click Connect.
    Make sure ReStim is already running before you connect.</p>
  </details>

  <div class="faq-section-label">For Drivers</div>

  <details>
    <summary>What device do I need to drive?</summary>
    <p>Any phone, tablet, or computer with a modern browser. No app needed &mdash;
    just open your room URL and you're in control.</p>
  </details>

  <details>
    <summary>Can I control multiple riders at once?</summary>
    <p>Yes &mdash; share your room code with as many riders as you like.
    All connected riders receive the same signal simultaneously.</p>
  </details>

  <details>
    <summary>How long does a room last?</summary>
    <p>Rooms expire after 24 hours of inactivity. Active rooms with a connected
    driver are kept alive automatically.</p>
  </details>

  <details>
    <summary>How do I get started as a driver?</summary>
    <p>Click <strong>Create New Room</strong> below. You'll get a room code &mdash;
    share it with your rider(s). Open the touch canvas on your phone and you're in
    control. Riders need to be connected before patterns reach them.</p>
  </details>

  <div class="faq-section-label">General</div>

  <details>
    <summary>What does ReDrive do?</summary>
    <p>ReDrive lets one person (the <strong>driver</strong>) control the estim patterns
    of one or more people (the <strong>riders</strong>) in real time over the internet.
    The driver uses a touch canvas on their phone or browser. Riders run a small app
    on their PC that bridges the signal to their local ReStim device.</p>
  </details>

  <details>
    <summary>Is my session private?</summary>
    <p>Rooms are identified by a 10-character code that you share yourself. Nobody else
    can access your room without the code. Rooms expire after 24 hours. No session data
    is logged or stored.</p>
  </details>

  <details>
    <summary>What if something goes wrong?</summary>
    <p>The rider can disconnect or close ReDrive Rider at any time &mdash; it immediately
    stops forwarding signals. Your ReStim device's own power controls always take
    priority. If in doubt, turn the dial down.</p>
  </details>
</div>

<div class="card">
  <h2>Driver &mdash; create a room</h2>
  <form action="/create" method="post">
    <button type="submit">Create New Room</button>
  </form>
  <p class="note">You'll get a 10-character room code to share with your rider(s).</p>
</div>

<div class="card">
  <h2>Rider &mdash; looking for a driver?</h2>
  <form action="/waiting" method="post">
    <button type="submit" style="background:linear-gradient(135deg,var(--accent2) 0%,var(--accent) 100%)">Request a Driver &rarr;</button>
  </form>
  <p class="note">Creates a waiting room. Share the invite link with your driver &mdash; they'll be redirected straight into control.</p>
  <div id="waiting-list" style="margin-top:1rem"></div>
</div>

<div class="card">
  <h2>Rider &mdash; join a room</h2>
  <input id="code-in" placeholder="Enter room code" maxlength="10"
         oninput="this.value=this.value.toUpperCase().replace(/[^BCDFGHJKMNPQRSTVWXYZ23456789]/g,'')">
  <button onclick="joinRider()">Connect as Rider</button>
  <p class="note">
    Download <a href="/download/windows" style="color:var(--accent)">ReDrive Rider for Windows</a>
    or <a href="/download/mac" style="color:var(--accent)">macOS</a> &mdash; or run
    <code>python rider_client.py &lt;ROOMCODE&gt;</code> directly if you have Python.
  </p>
</div>

<div class="card" id="live-sessions-card">
  <h2>Join a live session</h2>
  <div id="live-sessions-list"><span style="color:var(--fg2);font-size:.9rem">Loading&#8230;</span></div>
</div>

<footer class="site-footer">
  &copy; EstimStation &middot;
  <a href="https://www.estimstation.com">estimstation.com</a> &middot;
  ReDrive is open source &middot;
  <a href="/anatomy-maker" style="color:#5fa3ff">&#x1F5BC; Make your own anatomy overlay &rarr;</a>
</footer>

<script>
function joinRider(){
  const c = document.getElementById('code-in').value.trim();
  if(c.length === 10) window.location = '/room/' + c + '/join';
  else alert('Enter a 10-character room code');
}

// ── Waiting room list (driver side) ──────────────────────────────────────────
async function refreshWaiting() {
  try {
    const resp = await fetch('/api/waiting');
    const rooms = await resp.json();
    const el = document.getElementById('waiting-list');
    if (!rooms.length) { el.innerHTML = ''; return; }
    let html = '<div style="font-size:.78rem;color:var(--fg2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.5rem">Pending waiting rooms</div>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:.9rem">';
    for (const r of rooms) {
      const mins = Math.floor(r.expires_in / 60), secs = r.expires_in % 60;
      const timeStr = mins + 'm ' + String(secs).padStart(2,'0') + 's remaining';
      html += `<tr style="border-top:1px solid var(--border)">
        <td style="padding:.5rem .3rem;font-family:monospace;color:var(--accent)">${r.code}</td>
        <td style="padding:.5rem .3rem;color:var(--fg2);font-size:.82rem">${timeStr}</td>
        <td style="padding:.5rem .3rem;text-align:right">
          <a href="/waiting/${r.code}/claim"
             style="color:var(--accent);text-decoration:none;font-size:.82rem;
                    border:1px solid var(--accent);padding:2px 8px;border-radius:4px">
            Claim as Driver &rarr;</a>
        </td>
      </tr>`;
    }
    html += '</table>';
    el.innerHTML = html;
  } catch(_) {}
}
refreshWaiting();
setInterval(refreshWaiting, 10000);

// ── Public live sessions ──────────────────────────────────────────────────────
async function refreshPublicRooms() {
  try {
    const resp = await fetch('/api/rooms');
    const rooms = await resp.json();
    const el = document.getElementById('live-sessions-list');
    if (!rooms.length) {
      el.innerHTML = '<span style="color:var(--fg2);font-size:.9rem">No public sessions running right now</span>';
      return;
    }
    let html = '<table style="width:100%;border-collapse:collapse;font-size:.9rem">';
    html += '<thead><tr style="border-bottom:1px solid var(--border)">' +
      '<th style="text-align:left;padding:.4rem .3rem;color:var(--fg2);font-size:.78rem;font-weight:500">Room</th>' +
      '<th style="text-align:left;padding:.4rem .3rem;color:var(--fg2);font-size:.78rem;font-weight:500">Riders</th>' +
      '<th style="text-align:left;padding:.4rem .3rem;color:var(--fg2);font-size:.78rem;font-weight:500">Running</th>' +
      '<th></th></tr></thead><tbody>';
    for (const r of rooms) {
      html += `<tr style="border-top:1px solid var(--border)">
        <td style="padding:.5rem .3rem;font-family:monospace;color:var(--accent)">${r.code}</td>
        <td style="padding:.5rem .3rem;color:var(--fg2)">${r.riders}</td>
        <td style="padding:.5rem .3rem;color:var(--fg2)">${r.age_minutes}m</td>
        <td style="padding:.5rem .3rem;text-align:right">
          <a href="/room/${r.code}/touch"
             style="color:#000;background:var(--accent);text-decoration:none;
                    font-size:.82rem;font-weight:700;padding:3px 10px;border-radius:4px">
            Join as Rider</a>
        </td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch(_) {
    document.getElementById('live-sessions-list').innerHTML =
      '<span style="color:var(--fg2);font-size:.9rem">Could not load session list</span>';
  }
}
refreshPublicRooms();
setInterval(refreshPublicRooms, 30000);
</script>
<script src='https://storage.ko-fi.com/cdn/scripts/overlay-widget.js'></script>
<script>
  kofiWidgetOverlay.draw('stimstation', {
    'type': 'floating-chat',
    'floating-chat.donateButton.text': 'Support Us',
    'floating-chat.donateButton.background-color': '#d9534f',
    'floating-chat.donateButton.text-color': '#fff'
  });
</script>
</body>
</html>
"""


# ── Request handlers ─────────────────────────────────────────────────────────

async def handle_index(_req):
    return web.Response(text=_LANDING_HTML, content_type="text/html")


def _check_driver_key(req, room) -> bool:
    """Return True if the request carries the correct driver key."""
    key = (req.rel_url.query.get("key")
           or req.headers.get("X-Driver-Key", ""))
    return secrets.compare_digest(key, room.driver_key)


async def handle_create(req):
    code = _new_code()
    loop = asyncio.get_event_loop()
    room = Room(code, loop)
    _rooms[code] = room
    print(f"[room] created {code}  (total: {len(_rooms)})")
    raise web.HTTPFound(f"/room/{code}?key={room.driver_key}")


async def handle_room_driver(req):
    code = req.match_info["code"]
    if code not in _rooms:
        raise web.HTTPNotFound(text="Room not found or expired")
    room = _rooms[code]
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid or missing driver key. Use the link you were given when creating this room.")
    room.touch_driver()
    prefix = f"/room/{code}"
    html   = _inject_prefix(DRIVER_HTML, prefix, driver_key=room.driver_key)
    # Inject room code sharing panel + copy buttons near top of body
    banner = f"""
<div id="room-banner" style="
  position:fixed;top:0;left:0;right:0;z-index:9999;
  background:#1a1a1a;border-bottom:1px solid #2a2a2a;
  padding:6px 12px;font-size:13px">
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <span style="color:#999;font-size:11px;white-space:nowrap">Room:</span>
    <code id="rc" style="color:#5fa3ff;letter-spacing:.12em;font-size:15px;font-weight:700">{code}</code>
    <div style="display:flex;gap:4px;flex-shrink:0">
      <button id="btn-code"  onclick="rdCopy('code')"  title="Copy room code only"
              style="padding:3px 8px;background:#222;border:1px solid #444;color:#ccc;border-radius:4px;cursor:pointer;font-size:11px">&#128203; Code only</button>
      <button id="btn-rider" onclick="rdCopy('rider')" title="Copy rider join link"
              style="padding:3px 8px;background:#222;border:1px solid #444;color:#ccc;border-radius:4px;cursor:pointer;font-size:11px">&#128279; Rider link</button>
      <button id="btn-touch" onclick="rdCopy('touch')" title="Copy touch link"
              style="padding:3px 8px;background:#222;border:1px solid #444;color:#ccc;border-radius:4px;cursor:pointer;font-size:11px">&#127918; Touch link</button>
      <button id="btn-privacy" onclick="rdTogglePrivacy()" title="Toggle public/private session"
              style="padding:3px 8px;background:#222;border:1px solid #444;color:#ccc;border-radius:4px;cursor:pointer;font-size:11px">&#127760; Public</button>
    </div>
    <span id="rider-ct" style="color:#666;margin-left:auto;font-size:12px;white-space:nowrap">0 riders</span>
  </div>
</div>
<div style="height:44px"></div>
<script>
const _RC="{code}";
const _BASE=location.origin+"{prefix}";
const _RIDER_URL=_BASE;
const _TOUCH_URL=_BASE+"/touch";
let _isPublic = true;
function rdFlash(btnId){{
  const btn=document.getElementById(btnId);
  if(!btn) return;
  const orig=btn.textContent;
  btn.textContent='Copied!';
  btn.style.color='#4caf50';
  btn.style.borderColor='#4caf50';
  clearTimeout(btn._ft);
  btn._ft=setTimeout(()=>{{btn.textContent=orig;btn.style.color='';btn.style.borderColor='';}} ,1500);
}}
function rdCopy(type){{
  let text, btnId;
  if(type==='code')  {{ text=_RC;         btnId='btn-code';  }}
  else if(type==='rider') {{ text=_RIDER_URL; btnId='btn-rider'; }}
  else               {{ text=_TOUCH_URL;  btnId='btn-touch'; }}
  navigator.clipboard.writeText(text).then(()=>rdFlash(btnId));
}}
async function rdTogglePrivacy(){{
  try{{
    const r=await fetch('{prefix}/privacy',{{method:'POST'}});
    if(!r.ok) return;
    const d=await r.json();
    _isPublic=d.public;
    const btn=document.getElementById('btn-privacy');
    btn.textContent=_isPublic?'&#127760; Public':'&#128274; Private';
    btn.style.color=_isPublic?'#4caf50':'#ff9800';
    btn.style.borderColor=_isPublic?'#4caf50':'#ff9800';
  }}catch(_){{}}
}}
setInterval(async()=>{{
  try{{const d=await(await fetch('{prefix}/state')).json();
  document.getElementById('rider-ct').textContent=d.rider_count+' rider'+(d.rider_count===1?'':'s');}}catch{{}}
}},3000);
</script>
"""
    html = html.replace("<body>", "<body>" + banner, 1)
    return web.Response(text=html, content_type="text/html")


async def handle_room_touch(req):
    code = req.match_info["code"]
    if code not in _rooms:
        raise web.HTTPNotFound(text="Room not found or expired")
    prefix = f"/room/{code}"
    html   = _inject_prefix(TOUCH_HTML, prefix)
    # Inject ROOM_CODE constant before any other scripts (into <head>)
    head_script = f'<script>const ROOM_CODE="{code}";</script>\n'
    html = html.replace("<head>", "<head>" + head_script, 1)
    # Show room code button in touch page
    code_script = (
        f'<script>'
        f'(function(){{'
        f'var b=document.getElementById("room-code-btn");'
        f'if(b){{b.textContent="{code}";b.dataset.code="{code}";b.style.display="";}}'
        f'}})();</script>\n'
    )
    html = html.replace("</body>", code_script + "</body>", 1)
    return web.Response(text=html, content_type="text/html")


async def handle_room_join(req):
    code = req.match_info["code"]
    if code not in _rooms:
        raise web.HTTPNotFound(text="Room not found or expired")
    prefix = f"/room/{code}"
    html = _RIDER_PAGE_HTML.format(code=code, prefix=prefix)
    return web.Response(text=html, content_type="text/html")


async def handle_room_command(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.touch_driver()
    return await room.engine._handle_command(req)


async def handle_room_state(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.touch_driver()
    state = await room.engine._handle_state(req)
    d = json.loads(state.text)
    d["rider_count"]      = room.rider_count
    d["bottle_active"]    = time.monotonic() < room.bottle_until
    d["bottle_remaining"] = max(0.0, round(room.bottle_until - time.monotonic(), 1))
    return web.Response(text=json.dumps(d), content_type="application/json")


async def handle_room_bottle(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.touch_driver()
    try:
        duration = max(5, min(15, int(req.rel_url.query.get("duration", "10"))))
    except ValueError:
        duration = 10
    room.bottle_until = time.monotonic() + duration
    return web.Response(text="{}", content_type="application/json")


async def handle_driver_ping(req):
    """Heartbeat — keeps the driver grace timer alive while the page is open."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.touch_driver()
    grace_left = max(0, _DRIVER_GRACE - (time.monotonic() - room.driver_last_seen))
    return web.Response(text=json.dumps({"ok": True, "grace_left": int(grace_left)}),
                        content_type="application/json")


async def handle_rider_ws(req):
    """Rider connects here via WebSocket and receives T-code strings."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(req)
    room.rider_wss.add(ws)
    print(f"[rider] connected to {code}  (riders: {room.rider_count})")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                pass  # riders are receive-only (status msgs could go here)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        room.rider_wss.discard(ws)
        print(f"[rider] disconnected from {code}  (riders: {room.rider_count})")

    return ws


# ── Touch assets (shared across all rooms) ──────────────────────────────────

async def handle_assets_list(req):
    type_ = req.rel_url.query.get("type", "anatomy")
    folder = Path(__file__).parent.parent / "touch_assets" / type_
    folder.mkdir(parents=True, exist_ok=True)
    files = sorted(f.name for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
    return web.Response(text=json.dumps(files), content_type="application/json")


async def handle_assets_file(req):
    type_ = req.match_info["type"]
    name  = req.match_info.get("name", "")
    subdir = req.match_info.get("subdir", "")
    if "/" in type_ or ".." in type_ or ".." in name or ".." in subdir:
        raise web.HTTPForbidden()
    if subdir:
        # Allow exactly one level of subdirectory (e.g. _uploads)
        if "/" in subdir:
            raise web.HTTPForbidden()
        path = Path(__file__).parent.parent / "touch_assets" / type_ / subdir / name
    else:
        if "/" in name:
            raise web.HTTPForbidden()
        path = Path(__file__).parent.parent / "touch_assets" / type_ / name
    if not path.is_file():
        raise web.HTTPNotFound()
    ct = {".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
              path.suffix.lower(), "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=ct)


async def handle_version(_req):
    # Prefer server/version.json (rider build info), fall back to root version.json
    path = Path(__file__).parent / "version.json"
    if not path.is_file():
        path = Path(__file__).parent.parent / "version.json"
    if not path.is_file():
        return web.Response(text='{"version":"0.1.0"}', content_type="application/json")
    return web.Response(body=path.read_bytes(), content_type="application/json",
                        headers={"Access-Control-Allow-Origin": "*"})


async def handle_rider_download(req):
    """Placeholder download endpoints for the rider app installer."""
    platform = req.match_info["platform"]   # "windows" or "mac"
    if platform == "windows":
        raise web.HTTPNotFound(text="Windows build coming soon")
    elif platform == "mac":
        raise web.HTTPNotFound(text="Mac build coming soon")
    raise web.HTTPNotFound()


async def handle_download(req):
    platform = req.match_info["platform"]   # "windows" or "mac"
    ext = {"windows": ".exe", "mac": ".dmg"}.get(platform)
    if not ext:
        raise web.HTTPNotFound()
    fname = f"ReDriveRider-Setup{ext}" if platform == "windows" else "ReDriveRider.dmg"
    path = Path(__file__).parent / "dist" / fname
    if not path.is_file():
        raise web.HTTPNotFound(text=f"{fname} not yet available — check back soon.")
    return web.Response(
        body=path.read_bytes(),
        content_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def handle_bottle_png(_req):
    path = Path(__file__).parent.parent / "bottle.png"
    if not path.is_file():
        raise web.HTTPNotFound(text="bottle.png not found")
    return web.Response(body=path.read_bytes(), content_type="image/png")


# ── Waiting room handlers ─────────────────────────────────────────────────────

_WAITING_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReDrive &middot; Waiting for Driver</title>
<style>
  :root {{ --bg:#0e0e10; --bg2:#18181c; --bg3:#222228; --border:#2a2a32;
          --fg:#f0f0f5; --fg2:#8888a0; --accent:#5fa3ff; --accent2:#a855f7;
          --ok:#4caf50; --warn:#ff9800; }}
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center;
         min-height:100vh; padding:2rem }}
  .card {{ background:var(--bg2); border:1px solid var(--border); border-radius:10px;
          padding:2rem 2.5rem; width:100%; max-width:420px; margin-bottom:1.5rem;
          text-align:center }}
  h1 {{ font-size:1.8rem; margin-bottom:.5rem;
        background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
        -webkit-background-clip:text; -webkit-text-fill-color:transparent;
        background-clip:text }}
  .sub {{ color:var(--fg2); font-size:.9rem; margin-bottom:1.5rem }}
  .code-display {{ font-size:2.6rem; font-weight:900; letter-spacing:.18em;
                   color:var(--accent); font-family:monospace; margin:1rem 0 }}
  @keyframes pulse {{
    0%,100% {{ opacity:1; transform:scale(1) }}
    50%      {{ opacity:.6; transform:scale(0.97) }}
  }}
  .waiting-label {{ animation:pulse 2s ease-in-out infinite; color:var(--fg2);
                    font-size:1rem; margin-bottom:1rem }}
  .btn {{ display:inline-block; padding:.7rem 1.5rem;
          background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
          color:#fff; border:none; border-radius:6px; font-size:.95rem; font-weight:700;
          cursor:pointer; transition:opacity .15s; text-decoration:none; margin:.3rem }}
  .btn:hover {{ opacity:.85 }}
  .btn-outline {{ background:none; border:1px solid var(--border); color:var(--fg2) }}
  .btn-outline:hover {{ border-color:var(--accent); color:var(--accent) }}
  .invite-link {{ background:var(--bg3); border:1px solid var(--border); border-radius:6px;
                  padding:.6rem 1rem; font-size:.82rem; color:var(--accent); font-family:monospace;
                  word-break:break-all; margin:.8rem 0 }}
  #countdown {{ font-size:.85rem; color:var(--fg2); margin-top:.8rem }}
  .site-footer {{ color:var(--fg2); font-size:.8rem; margin-top:2rem }}
  .site-footer a {{ color:var(--accent); text-decoration:none }}
</style>
</head>
<body>
<div class="card">
  <h1>ReDrive</h1>
  <p class="sub">Share the driver invite link below</p>
  <div class="waiting-label">&#9679; Waiting for a driver&#8230;</div>
  <div class="code-display">{code}</div>
  <div class="invite-link" id="invite-link">{invite_url}</div>
  <div style="display:flex;gap:.5rem;justify-content:center;flex-wrap:wrap;margin-top:.5rem">
    <button class="btn" onclick="copyInvite()">&#128203; Copy driver invite link</button>
    <a class="btn btn-outline" href="/">&#8592; Back</a>
  </div>
  <div id="countdown">Expires in <span id="cd-timer">30:00</span></div>
</div>

<footer class="site-footer">
  <a href="https://www.estimstation.com">estimstation.com</a> &middot; ReDrive
</footer>

<script>
const CODE = "{code}";
const STATUS_URL = "/waiting/" + CODE + "/status";
const INVITE_URL = "{invite_url}";
const EXPIRES_AT = Date.now() + {ms_remaining};

function copyInvite() {{
  navigator.clipboard.writeText(INVITE_URL).then(() => {{
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = "Copied!";
    btn.style.background = "var(--ok)";
    setTimeout(() => {{ btn.textContent = orig; btn.style.background = ""; }}, 1500);
  }});
}}

// Countdown timer
function updateCountdown() {{
  const ms = EXPIRES_AT - Date.now();
  if (ms <= 0) {{
    document.getElementById('cd-timer').textContent = "Expired";
    return;
  }}
  const mins = Math.floor(ms / 60000);
  const secs = Math.floor((ms % 60000) / 1000);
  document.getElementById('cd-timer').textContent =
    mins + ":" + String(secs).padStart(2, "0");
}}
setInterval(updateCountdown, 1000);
updateCountdown();

// Poll for driver claim
async function pollStatus() {{
  try {{
    const d = await (await fetch(STATUS_URL)).json();
    if (d.claimed && d.touch_url) {{
      window.location = d.touch_url;
      return;
    }}
  }} catch(_) {{}}
  setTimeout(pollStatus, 3000);
}}
pollStatus();
</script>
</body>
</html>
"""


async def handle_create_waiting(req):
    """Rider creates a waiting room — no driver key yet."""
    code = _new_code()
    loop = asyncio.get_event_loop()
    room = Room(code, loop, waiting=True)
    _rooms[code] = room
    print(f"[room] waiting created {code}  (total: {len(_rooms)})")
    raise web.HTTPFound(f"/waiting/{code}")


async def handle_waiting_page(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None or not room.waiting:
        raise web.HTTPNotFound(text="Waiting room not found or expired")
    if time.time() > room.waiting_expires:
        raise web.HTTPNotFound(text="Waiting room has expired")
    ms_remaining = max(0, int((room.waiting_expires - time.time()) * 1000))
    base = req.url.origin()
    invite_url = f"{base}/waiting/{code}/claim"
    html = _WAITING_HTML.format(
        code=code,
        invite_url=invite_url,
        ms_remaining=ms_remaining,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_waiting_status(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        return web.Response(text=json.dumps({"claimed": True, "touch_url": None}),
                            content_type="application/json")
    if room.waiting:
        if time.time() > room.waiting_expires:
            return web.Response(text=json.dumps({"claimed": False, "touch_url": None,
                                                  "expired": True}),
                                content_type="application/json")
        return web.Response(text=json.dumps({"claimed": False, "touch_url": None}),
                            content_type="application/json")
    # Room exists and is no longer a waiting room — driver has claimed it
    return web.Response(
        text=json.dumps({"claimed": True, "touch_url": f"/room/{code}/touch"}),
        content_type="application/json")


async def handle_waiting_claim(req):
    """Driver visits this URL to claim a waiting room."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Waiting room not found or already claimed")
    if not room.waiting:
        raise web.HTTPNotFound(text="Waiting room already claimed")
    if time.time() > room.waiting_expires:
        raise web.HTTPNotFound(text="Waiting room has expired")

    # Promote waiting room to a real room
    room.driver_key = secrets.token_urlsafe(20)
    room.waiting = False
    room.waiting_expires = 0.0
    room.driver_last_seen = time.monotonic()
    cfg = DriveConfig()
    room._log_q = queue.Queue()
    room.engine = DriveEngine(cfg, {}, room._log_q, send_hook=room._hook)
    room.engine.start()

    # Broadcast to any connected WebSocket riders
    msg = json.dumps({"type": "driver_joined"})
    dead = set()
    for ws in list(room.rider_wss):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    room.rider_wss -= dead

    print(f"[room] waiting claimed {code}  (total: {len(_rooms)})")
    raise web.HTTPFound(f"/room/{code}?key={room.driver_key}")


# ── Anatomy overlay maker ─────────────────────────────────────────────────────

_ANATOMY_MAKER_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReDrive &middot; Anatomy Overlay Maker</title>
<style>
  :root { --bg:#111; --bg2:#1a1a1a; --bg3:#222; --border:#2a2a2a;
          --fg:#f0f0f5; --fg2:#999; --accent:#5fa3ff; --ok:#4caf50; }
  * { box-sizing:border-box; margin:0; padding:0 }
  body { background:var(--bg); color:var(--fg);
         font:15px/1.6 system-ui,Helvetica,sans-serif;
         max-width:340px; margin:0 auto; padding:1.4rem 1rem 3rem }
  h1 { color:var(--accent); font-size:1.3rem; margin-bottom:.15rem }
  .sub { color:var(--fg2); font-size:.85rem; margin-bottom:1.6rem }
  .step-label { color:var(--accent); font-weight:700; font-size:.82rem;
                text-transform:uppercase; letter-spacing:.08em; margin-bottom:.5rem }
  .section { margin-bottom:1.4rem }
  label { color:var(--fg2); font-size:.88rem; display:block; margin-bottom:.25rem }
  input[type=file] { display:none }
  .file-btn { display:inline-block; padding:.6rem 1.2rem;
              background:var(--bg2); border:1px solid var(--border);
              border-radius:6px; cursor:pointer; font-size:.95rem;
              color:var(--fg); transition:border-color .15s }
  .file-btn:hover { border-color:var(--accent) }
  canvas { display:block; border:1px solid var(--border); border-radius:6px;
           touch-action:none; cursor:grab }
  canvas:active { cursor:grabbing }
  .tip { color:var(--fg2); font-size:.78rem; margin-top:.4rem; text-align:center }
  .slider-row { display:flex; align-items:center; gap:.6rem; margin-bottom:.5rem }
  .slider-row label { min-width:52px; color:var(--fg2); font-size:.85rem; margin:0 }
  input[type=range] { flex:1; accent-color:var(--accent) }
  .slider-val { min-width:44px; text-align:right; color:var(--fg);
                font-size:.85rem; font-family:monospace }
  .dl-btn { width:100%; padding:.85rem; background:var(--ok);
            color:#fff; border:none; border-radius:6px; font-size:1rem;
            font-weight:700; cursor:pointer; transition:opacity .15s }
  .dl-btn:hover { opacity:.85 }
  .dl-btn:disabled { opacity:.4; cursor:default }
  hr { border:none; border-top:1px solid var(--border); margin:1.4rem 0 }
  .how-to { color:var(--fg2); font-size:.85rem; line-height:1.8 }
  .how-to ul { padding-left:1.2rem }
  .how-to li { margin-bottom:.15rem }
  .back { color:var(--accent); text-decoration:none; font-size:.85rem }
  .back:hover { text-decoration:underline }
</style>
</head>
<body>

<p style="margin-bottom:.8rem"><a href="/" class="back">&larr; Back to ReDrive</a></p>
<h1>ReDrive &middot; Anatomy Overlay Maker</h1>
<p class="sub">Position your photo behind the outline, then export a 400&times;1000&nbsp;px overlay.</p>

<!-- Step 1 -->
<div class="section">
  <div class="step-label">Step 1 &mdash; Upload your photo</div>
  <label class="file-btn" for="photo-input">&#128247; Choose photo</label>
  <input type="file" id="photo-input" accept="image/*">
</div>

<!-- Step 2 -->
<div class="section">
  <div class="step-label">Step 2 &mdash; Align to the outline</div>
  <canvas id="preview" width="280" height="700"></canvas>
  <p class="tip">Drag to move &middot; scroll / pinch to zoom</p>

  <div style="margin-top:.9rem">
    <div class="slider-row">
      <label for="sl-scale">Scale</label>
      <input type="range" id="sl-scale" min="10" max="300" value="100">
      <span class="slider-val" id="lbl-scale">100%</span>
    </div>
    <div class="slider-row">
      <label for="sl-rotate">Rotate</label>
      <input type="range" id="sl-rotate" min="-180" max="180" value="0">
      <span class="slider-val" id="lbl-rotate">0&deg;</span>
    </div>
  </div>
</div>

<!-- Step 3 -->
<div class="section">
  <div class="step-label">Step 3 &mdash; Save</div>
  <button class="dl-btn" id="dl-btn" disabled onclick="downloadOverlay()">
    &#128190; Download overlay.png
  </button>
</div>

<hr>
<div class="how-to">
  <strong style="color:var(--fg);font-size:.9rem">How to use your overlay</strong>
  <ul style="margin-top:.5rem">
    <li>Open the <strong>ReDrive Rider</strong> app</li>
    <li>In the &ldquo;My overlay&rdquo; section, click <strong>Set&hellip;</strong></li>
    <li>Select the <em>overlay.png</em> you just downloaded</li>
    <li>Connect to a session &mdash; your overlay will upload automatically</li>
  </ul>
</div>

<script>
(function () {
  const PREVIEW_W = 280, PREVIEW_H = 700;
  const EXPORT_W  = 400, EXPORT_H  = 1000;

  const canvas    = document.getElementById('preview');
  const ctx       = canvas.getContext('2d');
  const slScale   = document.getElementById('sl-scale');
  const slRotate  = document.getElementById('sl-rotate');
  const lblScale  = document.getElementById('lbl-scale');
  const lblRotate = document.getElementById('lbl-rotate');
  const dlBtn     = document.getElementById('dl-btn');

  let userImg   = null;
  let overlayImg = null;
  let panX = PREVIEW_W / 2;   // 140
  let panY = PREVIEW_H / 2;   // 350
  let zoom = 1.0;
  let rotRad = 0;

  // ── Load overlay image ───────────────────────────────────────────────────
  (function loadOverlay() {
    const img = new Image();
    img.onload = () => { overlayImg = img; redraw(); };
    img.onerror = () => { overlayImg = null; redraw(); };
    img.src = '/touch_assets/anatomy/anatomyexampleOVERLAY.png';
  })();

  // ── Fallback outline ─────────────────────────────────────────────────────
  function drawFallbackOutline(c, W, H) {
    c.save();
    c.globalAlpha = 0.55;
    c.strokeStyle = '#aaaacc';
    c.lineWidth = 2;
    // Glans
    c.beginPath();
    c.ellipse(W/2, H*0.08, W*0.22, H*0.06, 0, 0, Math.PI*2);
    c.stroke();
    // Shaft
    c.beginPath();
    c.moveTo(W/2 - W*0.12, H*0.13);
    c.lineTo(W/2 - W*0.10, H*0.42);
    c.moveTo(W/2 + W*0.12, H*0.13);
    c.lineTo(W/2 + W*0.10, H*0.42);
    c.stroke();
    // Left testicle
    c.beginPath();
    c.ellipse(W/2 - W*0.22, H*0.50, W*0.18, H*0.10, -0.2, 0, Math.PI*2);
    c.stroke();
    // Right testicle
    c.beginPath();
    c.ellipse(W/2 + W*0.22, H*0.50, W*0.18, H*0.10, 0.2, 0, Math.PI*2);
    c.stroke();
    // Perineum/anus region
    c.beginPath();
    c.ellipse(W/2, H*0.80, W*0.08, H*0.04, 0, 0, Math.PI*2);
    c.stroke();
    c.restore();
  }

  // ── Redraw ───────────────────────────────────────────────────────────────
  function redraw() {
    ctx.clearRect(0, 0, PREVIEW_W, PREVIEW_H);

    // Layer 1: user photo
    if (userImg) {
      ctx.save();
      ctx.translate(panX, panY);
      ctx.rotate(rotRad);
      ctx.scale(zoom, zoom);
      ctx.drawImage(userImg, -userImg.naturalWidth / 2, -userImg.naturalHeight / 2);
      ctx.restore();
    }

    // Layer 2: anatomy outline at 60% opacity
    if (overlayImg && overlayImg.complete && overlayImg.naturalWidth > 0) {
      ctx.save();
      ctx.globalAlpha = 0.6;
      ctx.drawImage(overlayImg, 0, 0, PREVIEW_W, PREVIEW_H);
      ctx.restore();
    } else {
      drawFallbackOutline(ctx, PREVIEW_W, PREVIEW_H);
    }
  }

  // ── File input ───────────────────────────────────────────────────────────
  document.getElementById('photo-input').addEventListener('change', function () {
    const file = this.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = e => {
      const img = new Image();
      img.onload = () => {
        userImg = img;
        // Auto-fit: scale so the image fills the canvas height
        const fitScale = PREVIEW_H / img.naturalHeight;
        zoom = fitScale;
        slScale.value = Math.round(zoom * 100);
        lblScale.textContent = slScale.value + '%';
        panX = PREVIEW_W / 2;
        panY = PREVIEW_H / 2;
        rotRad = 0;
        slRotate.value = 0;
        lblRotate.textContent = '0\u00b0';
        dlBtn.disabled = false;
        redraw();
      };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  });

  // ── Sliders ──────────────────────────────────────────────────────────────
  slScale.addEventListener('input', function () {
    zoom = parseInt(this.value) / 100;
    lblScale.textContent = this.value + '%';
    redraw();
  });

  slRotate.addEventListener('input', function () {
    rotRad = parseInt(this.value) * Math.PI / 180;
    lblRotate.textContent = this.value + '\u00b0';
    redraw();
  });

  // ── Mouse drag ───────────────────────────────────────────────────────────
  let dragging = false, dragStartX = 0, dragStartY = 0, panStartX = 0, panStartY = 0;

  canvas.addEventListener('mousedown', e => {
    dragging = true;
    dragStartX = e.clientX; dragStartY = e.clientY;
    panStartX = panX; panStartY = panY;
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    panX = panStartX + (e.clientX - dragStartX);
    panY = panStartY + (e.clientY - dragStartY);
    redraw();
  });
  window.addEventListener('mouseup', () => { dragging = false; });

  // ── Mouse wheel zoom ─────────────────────────────────────────────────────
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    zoom *= Math.pow(1.001, e.deltaY);
    zoom = Math.max(0.1, Math.min(5.0, zoom));
    slScale.value = Math.round(zoom * 100);
    lblScale.textContent = slScale.value + '%';
    redraw();
  }, { passive: false });

  // ── Touch (pan + pinch) ──────────────────────────────────────────────────
  let lastTouches = [];

  canvas.addEventListener('touchstart', e => {
    e.preventDefault();
    lastTouches = Array.from(e.touches);
  }, { passive: false });

  canvas.addEventListener('touchmove', e => {
    e.preventDefault();
    const touches = Array.from(e.touches);

    if (touches.length === 1 && lastTouches.length >= 1) {
      // Pan
      const dx = touches[0].clientX - lastTouches[0].clientX;
      const dy = touches[0].clientY - lastTouches[0].clientY;
      panX += dx; panY += dy;
      redraw();
    } else if (touches.length === 2 && lastTouches.length >= 2) {
      // Pinch-zoom
      const prevDist = Math.hypot(
        lastTouches[0].clientX - lastTouches[1].clientX,
        lastTouches[0].clientY - lastTouches[1].clientY);
      const newDist = Math.hypot(
        touches[0].clientX - touches[1].clientX,
        touches[0].clientY - touches[1].clientY);
      if (prevDist > 0) {
        zoom *= newDist / prevDist;
        zoom = Math.max(0.1, Math.min(5.0, zoom));
        slScale.value = Math.round(zoom * 100);
        lblScale.textContent = slScale.value + '%';
        redraw();
      }
    }

    lastTouches = touches;
  }, { passive: false });

  canvas.addEventListener('touchend', e => {
    lastTouches = Array.from(e.touches);
  }, { passive: false });

  // ── Export ───────────────────────────────────────────────────────────────
  window.downloadOverlay = function () {
    const scale = EXPORT_W / PREVIEW_W; // 1.4286
    const off = document.createElement('canvas');
    off.width = EXPORT_W; off.height = EXPORT_H;
    const octx = off.getContext('2d');

    // Draw photo
    if (userImg) {
      octx.save();
      octx.translate(panX * scale, panY * scale);
      octx.rotate(rotRad);
      octx.scale(zoom * scale, zoom * scale);
      octx.drawImage(userImg, -userImg.naturalWidth / 2, -userImg.naturalHeight / 2);
      octx.restore();
    }

    // Draw overlay at full opacity
    octx.globalAlpha = 1.0;
    if (overlayImg && overlayImg.complete && overlayImg.naturalWidth > 0) {
      octx.drawImage(overlayImg, 0, 0, EXPORT_W, EXPORT_H);
    } else {
      drawFallbackOutline(octx, EXPORT_W, EXPORT_H);
    }

    off.toBlob(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'redrive-overlay.png';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
    }, 'image/png');
  };

  // Initial draw (shows outline only until photo loaded)
  redraw();
})();
</script>
</body>
</html>
"""


async def handle_anatomy_maker(_req):
    return web.Response(text=_ANATOMY_MAKER_HTML, content_type="text/html")


# ── Public session list + privacy ─────────────────────────────────────────────

async def handle_api_rooms(req):
    """Return JSON list of public, non-waiting, active rooms."""
    now = time.monotonic()
    result = []
    for code, room in _rooms.items():
        if room.waiting:
            continue
        if not room.public:
            continue
        age_min = int((now - room.created_at) / 60)
        result.append({
            "code": code,
            "riders": room.rider_count,
            "age_minutes": age_min,
        })
    return web.Response(text=json.dumps(result), content_type="application/json")


async def handle_api_waiting(req):
    """Return JSON list of active waiting rooms (for driver claiming)."""
    now_wall = time.time()
    result = []
    for code, room in _rooms.items():
        if not room.waiting:
            continue
        if now_wall > room.waiting_expires:
            continue
        expires_in = max(0, int(room.waiting_expires - now_wall))
        result.append({"code": code, "expires_in": expires_in})
    return web.Response(text=json.dumps(result), content_type="application/json")


async def handle_room_privacy(req):
    """Toggle room.public. Requires X-Driver-Key header."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.public = not room.public
    return web.Response(text=json.dumps({"public": room.public}),
                        content_type="application/json")


# ── Anatomy upload ────────────────────────────────────────────────────────────

_MAX_ANATOMY_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_ANATOMY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


async def handle_anatomy_upload(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")

    try:
        reader = await req.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            raise web.HTTPBadRequest(text="Missing file field")

        # Read with size cap
        chunks = []
        total = 0
        while True:
            chunk = await field.read_chunk(8192)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_ANATOMY_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=_MAX_ANATOMY_BYTES, actual_size=total)
            chunks.append(chunk)
        data = b"".join(chunks)
    except web.HTTPException:
        raise
    except Exception as e:
        raise web.HTTPBadRequest(text=f"Upload error: {e}")

    # Detect extension from content-type or filename
    ct = field.headers.get("Content-Type", "")
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}
    suffix = ext_map.get(ct, "")
    if not suffix:
        fname = field.filename or ""
        suffix = Path(fname).suffix.lower()
    if suffix not in _ALLOWED_ANATOMY_SUFFIXES:
        raise web.HTTPUnsupportedMediaType(text="File must be PNG, JPG, or WEBP")

    # Save file
    uploads_dir = (Path(__file__).parent.parent
                   / "touch_assets" / "anatomy" / "_uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    short_id = uuid.uuid4().hex[:8]
    filename = f"{code}_{short_id}{suffix}"
    (uploads_dir / filename).write_bytes(data)

    rel_name = f"_uploads/{filename}"
    room.custom_anatomies.append(rel_name)

    # Broadcast to room WebSocket connections
    msg = json.dumps({"type": "anatomy_added", "name": rel_name})
    dead = set()
    for ws in list(room.rider_wss):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    room.rider_wss -= dead

    return web.Response(text=json.dumps({"ok": True, "name": rel_name}),
                        content_type="application/json")


async def handle_room_anatomies(req):
    """Return custom anatomies for this room + standard anatomy list."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")

    anatomy_dir = Path(__file__).parent.parent / "touch_assets" / "anatomy"
    anatomy_dir.mkdir(parents=True, exist_ok=True)
    standard = sorted(
        f.name for f in anatomy_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _ALLOWED_ANATOMY_SUFFIXES
    )
    return web.Response(
        text=json.dumps({"custom": room.custom_anatomies, "standard": standard}),
        content_type="application/json"
    )


# ── Room expiry cleanup ──────────────────────────────────────────────────────

def _delete_room_uploads(code: str):
    """Delete anatomy upload files belonging to a room."""
    uploads_dir = (Path(__file__).parent.parent
                   / "touch_assets" / "anatomy" / "_uploads")
    if not uploads_dir.is_dir():
        return
    prefix = f"{code}_"
    for f in list(uploads_dir.iterdir()):
        if f.is_file() and f.name.startswith(prefix):
            try:
                f.unlink()
            except Exception:
                pass


async def _cleanup_loop():
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        now_mono = time.monotonic()
        now_wall = time.time()
        for code, room in list(_rooms.items()):
            # Expired waiting rooms (wall-clock based)
            if room.waiting and now_wall > room.waiting_expires:
                _rooms.pop(code)
                _delete_room_uploads(code)
                print(f"[room] waiting expired  {code}  (total: {len(_rooms)})")
                continue
            # Skip waiting rooms from normal expiry checks
            if room.waiting:
                continue
            if now_mono - room.created_at > _ROOM_EXPIRY:
                _rooms.pop(code).stop()
                _delete_room_uploads(code)
                print(f"[room] expired (24h)  {code}  (total: {len(_rooms)})")
            elif now_mono - room.driver_last_seen > _DRIVER_GRACE:
                _rooms.pop(code).stop()
                _delete_room_uploads(code)
                print(f"[room] expired (driver gone 5m)  {code}  (total: {len(_rooms)})")


# ── App factory ──────────────────────────────────────────────────────────────

def build_app() -> web.Application:
    app = web.Application(client_max_size=6 * 1024 * 1024)  # allow up to ~6 MB uploads
    app.router.add_get("/",                                    handle_index)
    app.router.add_get("/anatomy-maker",                       handle_anatomy_maker)
    app.router.add_post("/create",                             handle_create)
    # Waiting room routes
    app.router.add_post("/waiting",                            handle_create_waiting)
    app.router.add_get("/waiting/{code}",                      handle_waiting_page)
    app.router.add_get("/waiting/{code}/status",               handle_waiting_status)
    app.router.add_get("/waiting/{code}/claim",                handle_waiting_claim)
    # Public session list
    app.router.add_get("/api/rooms",                           handle_api_rooms)
    app.router.add_get("/api/waiting",                         handle_api_waiting)
    # Room routes
    app.router.add_get("/room/{code}",                         handle_room_driver)
    app.router.add_get("/room/{code}/touch",                   handle_room_touch)
    app.router.add_get("/room/{code}/join",                    handle_room_join)
    app.router.add_post("/room/{code}/command",                handle_room_command)
    app.router.add_get("/room/{code}/state",                   handle_room_state)
    app.router.add_post("/room/{code}/bottle",                 handle_room_bottle)
    app.router.add_get("/room/{code}/rider",                   handle_rider_ws)
    app.router.add_post("/room/{code}/ping",                   handle_driver_ping)
    app.router.add_post("/room/{code}/privacy",                handle_room_privacy)
    # Anatomy upload
    app.router.add_post("/room/{code}/upload_anatomy",         handle_anatomy_upload)
    app.router.add_get("/room/{code}/anatomies",               handle_room_anatomies)
    # Static assets
    app.router.add_get("/bottle.png",                          handle_bottle_png)
    app.router.add_get("/touch_assets/list",                   handle_assets_list)
    app.router.add_get("/touch_assets/{type}/{subdir}/{name}", handle_assets_file)
    app.router.add_get("/touch_assets/{type}/{name}",          handle_assets_file)
    app.router.add_get("/version.json",                        handle_version)
    app.router.add_get("/download/rider/{platform}",           handle_rider_download)
    app.router.add_get("/download/{platform}",                 handle_download)

    async def _start_cleanup(_app):
        asyncio.ensure_future(_cleanup_loop())

    app.on_startup.append(_start_cleanup)
    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ReDrive relay server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"ReDrive relay → http://{args.host}:{args.port}", flush=True)
    try:
        web.run_app(build_app(), host=args.host, port=args.port,
                    access_log=None)
    except Exception as e:
        print(f"FATAL: {e}", flush=True)
        import traceback; traceback.print_exc()
