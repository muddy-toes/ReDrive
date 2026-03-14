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
import sys
import time
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
_ROOM_EXPIRY  = 86_400   # 24 h in seconds
_CLEANUP_INTERVAL = 600  # sweep expired rooms every 10 min

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

    def __init__(self, code: str, main_loop: asyncio.AbstractEventLoop):
        self.code        = code
        self.created_at  = time.monotonic()
        self.rider_wss:  set[web.WebSocketResponse] = set()
        self._main_loop  = main_loop
        self._log_q      = queue.Queue()
        cfg              = DriveConfig()   # defaults — no ReStim URL needed
        self.engine      = DriveEngine(cfg, {}, self._log_q,
                                       send_hook=self._hook)
        self.engine.start()

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

    def expired(self) -> bool:
        return time.monotonic() - self.created_at > _ROOM_EXPIRY

    def stop(self):
        self.engine.stop()

    @property
    def rider_count(self) -> int:
        return len(self.rider_wss)


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _inject_prefix(html: str, prefix: str) -> str:
    """Rewrite absolute API paths to be room-scoped."""
    return (html
            .replace('"/command"',  f'"{prefix}/command"')
            .replace("'/command'",  f"'{prefix}/command'")
            .replace('"/state"',    f'"{prefix}/state"')
            .replace("'/state'",    f"'{prefix}/state'")
            .replace('fetch("/touch"', f'fetch("{prefix}/touch"'))


_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReDrive</title>
<style>
  :root { --bg:#111; --bg2:#1a1a1a; --bg3:#222; --border:#2a2a2a;
          --fg:#fff; --fg2:#999; --accent:#5fa3ff; }
  * { box-sizing:border-box; margin:0; padding:0 }
  body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; min-height:100vh; padding:2rem }
  h1 { font-size:2.2rem; letter-spacing:.05em; color:var(--accent); margin-bottom:.25rem }
  p.sub { color:var(--fg2); margin-bottom:2.5rem; font-size:.95rem }
  .card { background:var(--bg2); border:1px solid var(--border); border-radius:10px;
          padding:2rem 2.5rem; width:100%; max-width:420px; margin-bottom:1.5rem }
  .card h2 { font-size:1.1rem; color:var(--fg2); text-transform:uppercase;
              letter-spacing:.08em; margin-bottom:1.2rem; font-weight:500 }
  button { width:100%; padding:.85rem; background:var(--accent); color:#000;
           border:none; border-radius:6px; font-size:1rem; font-weight:700;
           cursor:pointer; transition:opacity .15s }
  button:hover { opacity:.85 }
  input { width:100%; padding:.75rem 1rem; background:var(--bg3);
          border:1px solid var(--border); border-radius:6px; color:var(--fg);
          font-size:1.1rem; letter-spacing:.15em; text-transform:uppercase;
          text-align:center; margin-bottom:1rem }
  input::placeholder { letter-spacing:normal; text-transform:none; color:var(--fg2) }
  .note { color:var(--fg2); font-size:.83rem; margin-top:1rem; line-height:1.6 }
  code { background:var(--bg3); padding:.1em .4em; border-radius:3px;
         font-family:monospace; font-size:.9em }
</style>
</head>
<body>
<h1>ReDrive</h1>
<p class="sub">Remote pattern engine for ReStim</p>

<div class="card">
  <h2>Driver — create a room</h2>
  <form action="/create" method="post">
    <button type="submit">Create New Room</button>
  </form>
  <p class="note">You'll get a 10-character room code to share with your rider(s).</p>
</div>

<div class="card">
  <h2>Rider — join a room</h2>
  <input id="code-in" placeholder="Enter room code" maxlength="10"
         oninput="this.value=this.value.toUpperCase().replace(/[^BCDFGHJKMNPQRSTVWXYZ23456789]/g,'')">
  <button onclick="joinRider()">Connect as Rider</button>
  <p class="note">
    Run <code>python rider_client.py &lt;ROOMCODE&gt;</code> on the machine connected
    to your ReStim device, then open <code>/room/&lt;ROOMCODE&gt;/touch</code> on your phone.
  </p>
</div>

<script>
function joinRider(){
  const c = document.getElementById('code-in').value.trim();
  if(c.length === 10) window.location = '/room/' + c + '/touch';
  else alert('Enter a 10-character room code');
}
</script>
</body>
</html>
"""


# ── Request handlers ─────────────────────────────────────────────────────────

async def handle_index(_req):
    return web.Response(text=_LANDING_HTML, content_type="text/html")


async def handle_create(req):
    code = _new_code()
    loop = asyncio.get_event_loop()
    _rooms[code] = Room(code, loop)
    print(f"[room] created {code}  (total: {len(_rooms)})")
    raise web.HTTPFound(f"/room/{code}")


async def handle_room_driver(req):
    code = req.match_info["code"]
    if code not in _rooms:
        raise web.HTTPNotFound(text="Room not found or expired")
    prefix = f"/room/{code}"
    html   = _inject_prefix(DRIVER_HTML, prefix)
    # Inject room code banner + copy button near top of body
    banner = f"""
<div id="room-banner" style="
  position:fixed;top:0;left:0;right:0;z-index:9999;
  background:#1a1a1a;border-bottom:1px solid #2a2a2a;
  padding:6px 16px;display:flex;align-items:center;gap:12px;font-size:13px">
  <span style="color:#999">Room&nbsp;</span>
  <code id="rc" style="color:#5fa3ff;letter-spacing:.12em;font-size:15px;font-weight:700">{code}</code>
  <button onclick="navigator.clipboard.writeText('{code}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)"
          style="padding:3px 10px;background:#222;border:1px solid #444;color:#ccc;
                 border-radius:4px;cursor:pointer;font-size:12px">Copy</button>
  <span id="rider-ct" style="color:#666;margin-left:auto">0 riders</span>
</div>
<div style="height:36px"></div>
<script>
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
    return web.Response(text=html, content_type="text/html")


async def handle_room_command(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    # Delegate to the room's engine command handler — reconstruct a fake request
    # by monkey-patching the match_info so the engine handler works normally.
    # Simpler: just replicate the logic by re-routing to engine._handle_command.
    return await room.engine._handle_command(req)


async def handle_room_state(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    state = await room.engine._handle_state(req)
    # Inject rider count into the state JSON
    d = json.loads(state.text)
    d["rider_count"] = room.rider_count
    return web.Response(text=json.dumps(d), content_type="application/json")


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
    name  = req.match_info["name"]
    if "/" in type_ or ".." in type_ or "/" in name or ".." in name:
        raise web.HTTPForbidden()
    path = Path(__file__).parent.parent / "touch_assets" / type_ / name
    if not path.is_file():
        raise web.HTTPNotFound()
    ct = {".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
              path.suffix.lower(), "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=ct)


# ── Room expiry cleanup ──────────────────────────────────────────────────────

async def _cleanup_loop():
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        expired = [c for c, r in list(_rooms.items()) if r.expired()]
        for code in expired:
            _rooms.pop(code).stop()
            print(f"[room] expired {code}  (total: {len(_rooms)})")


# ── App factory ──────────────────────────────────────────────────────────────

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/",                           handle_index)
    app.router.add_post("/create",                    handle_create)
    app.router.add_get("/room/{code}",                handle_room_driver)
    app.router.add_get("/room/{code}/touch",          handle_room_touch)
    app.router.add_post("/room/{code}/command",       handle_room_command)
    app.router.add_get("/room/{code}/state",          handle_room_state)
    app.router.add_get("/room/{code}/rider",          handle_rider_ws)
    app.router.add_get("/touch_assets/list",          handle_assets_list)
    app.router.add_get("/touch_assets/{type}/{name}", handle_assets_file)
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
