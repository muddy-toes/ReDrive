"""server.py -- ReDrive unified server.

Relay mode (default):
    python server.py [--port 8765]
    Multiple drivers create rooms; riders connect via room codes.

Local LAN mode:
    python server.py --local [--port 8765]
    Single room, auto-created at startup, engine connects to local ReStim.
    Prints pre-authenticated driver and rider URLs on startup.
"""

import argparse
import asyncio
import json
import queue
import random
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional

from engine import DriveEngine, DriveConfig, PRESETS

import aiohttp
import jinja2
import aiohttp_jinja2
from aiohttp import web

# -- Room code alphabet -- no ambiguous chars (0/O, 1/I/L)
_ROOM_CHARS   = "BCDFGHJKMNPQRSTVWXYZ23456789"
_CODE_LEN     = 10
_ROOM_EXPIRY        = 86_400   # 24 h in seconds
_DRIVER_GRACE       = 300      # 5 min grace after driver goes quiet
_CLEANUP_INTERVAL   = 30       # sweep every 30 s

# -- Global room registry
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
                 waiting: bool = False, local_restim: bool = False):
        self.code         = code
        self.driver_key      = "" if waiting else secrets.token_urlsafe(20)
        self.created_at      = time.monotonic()
        self.driver_last_seen = time.monotonic()
        self.bottle_until: float = 0.0
        self.bottle_mode:  str   = "normal"
        self.pending_likes: list = []
        self.rider_wss:  set[web.WebSocketResponse] = set()
        self._main_loop  = main_loop
        self._log_q      = queue.Queue()
        self.local_restim = local_restim
        # Waiting room support
        self.waiting: bool = waiting
        self.waiting_expires: float = time.time() + 1800 if waiting else 0.0
        # Public session list
        self.public: bool = True
        # Custom anatomy uploads
        self.custom_anatomies: list = []
        # Participant tracking
        self.driver_name: str = ""
        self.participants: dict = {}
        self._rider_counter: int = 0
        # Driver WebSocket connections
        self.driver_wss: set[web.WebSocketResponse] = set()
        self._push_task = None
        if not waiting:
            if local_restim:
                cfg = DriveConfig.load()
                hook = None  # direct ReStim connection
            else:
                cfg = DriveConfig()
                hook = self._hook
            self.engine  = DriveEngine(cfg, {}, self._log_q,
                                       send_hook=hook)
            self.engine.start()
            self._start_push_loop()
        else:
            self.engine  = None

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

    def _pick_anatomy(self, idx: int) -> str:
        """Pick an anatomy for a new rider based on their index."""
        if self.custom_anatomies:
            return self.custom_anatomies[0]
        anatomy_dir = Path(__file__).parent / "touch_assets" / "anatomy"
        files = sorted(
            f.name for f in anatomy_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
            and not f.name.startswith("_")
        ) if anatomy_dir.is_dir() else []
        if not files:
            return ""
        return files[(idx - 1) % len(files)]

    async def _broadcast_participants(self):
        """Send participants_update to all connected WebSockets (driver + riders)."""
        parts = list(self.participants.values())
        msg = json.dumps({
            "type": "participants_update",
            "participants": parts,
            "driver_name": self.driver_name,
        })
        dead = set()
        all_wss = list(self.rider_wss) + list(self.driver_wss)
        for ws in all_wss:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self.rider_wss -= dead
        self.driver_wss -= dead

    def touch_driver(self):
        self.driver_last_seen = time.monotonic()

    def expired(self) -> bool:
        now = time.monotonic()
        if now - self.created_at > _ROOM_EXPIRY:
            return True
        if now - self.driver_last_seen > _DRIVER_GRACE:
            return True
        return False

    async def _build_driver_state(self) -> dict:
        """Build the full driver state dict."""
        if self.engine is None:
            return {}
        resp = await self.engine._handle_state(None)
        d = json.loads(resp.text)
        d["rider_count"]      = self.rider_count
        d["bottle_active"]    = time.monotonic() < self.bottle_until
        d["bottle_remaining"] = max(0.0, round(self.bottle_until - time.monotonic(), 1))
        d["bottle_mode"]      = self.bottle_mode
        d["likes"]            = self.pending_likes[:]
        self.pending_likes.clear()
        return d

    def _build_rider_state(self) -> dict:
        """Build the rider state dict."""
        now = time.monotonic()
        intensity = 0.0
        if self.engine:
            intensity = self.engine._shared.get("__live__l0", self.engine._pattern.intensity)
        bottle_active = now < self.bottle_until
        return {
            "intensity":        round(intensity, 4),
            "bottle_active":    bottle_active,
            "bottle_remaining": max(0.0, round(self.bottle_until - now, 1)),
            "bottle_mode":      self.bottle_mode,
            "driver_name":      self.driver_name,
            "driver_connected": len(self.driver_wss) > 0,
        }

    async def _state_push_loop(self):
        """Push state to connected WebSockets at regular intervals."""
        tick = 0
        try:
            while True:
                await asyncio.sleep(0.2)  # 5 Hz
                tick += 1

                # Push to driver WS connections every tick (5 Hz)
                if self.driver_wss and self.engine is not None:
                    try:
                        state = await self._build_driver_state()
                        msg = json.dumps({"type": "state", "data": state})
                        dead = set()
                        for ws in list(self.driver_wss):
                            try:
                                await ws.send_str(msg)
                            except Exception:
                                dead.add(ws)
                        self.driver_wss -= dead
                    except Exception:
                        pass

                # Push to rider WS connections every 3rd tick (~1.7 Hz)
                if tick % 3 == 0 and self.rider_wss:
                    try:
                        rstate = self._build_rider_state()
                        rmsg = json.dumps({"type": "rider_state", **rstate})
                        dead = set()
                        for ws in list(self.rider_wss):
                            try:
                                await ws.send_str(rmsg)
                            except Exception:
                                dead.add(ws)
                        self.rider_wss -= dead
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    def _start_push_loop(self):
        """Start the state push loop as an asyncio task."""
        self._push_task = asyncio.ensure_future(
            self._state_push_loop(), loop=self._main_loop
        )

    async def _broadcast_driver_status(self, connected: bool):
        """Broadcast driver_status to all rider WS connections."""
        msg = json.dumps({
            "type": "driver_status",
            "connected": connected,
            "name": self.driver_name if connected else "",
        })
        dead = set()
        for ws in list(self.rider_wss):
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self.rider_wss -= dead

    async def _broadcast_bottle_status(self, mode: str, duration: int):
        """Push bottle_status to all rider WS connections."""
        msg = json.dumps({
            "type": "bottle_status",
            "active": True,
            "remaining": duration,
            "mode": mode,
        })
        dead = set()
        for ws in list(self.rider_wss):
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self.rider_wss -= dead

    def stop(self):
        if hasattr(self, "_push_task") and self._push_task is not None:
            self._push_task.cancel()
            self._push_task = None
        if self.engine is not None:
            self.engine.stop()

    @property
    def rider_count(self) -> int:
        return len(self.rider_wss)


# -- Request handlers

async def handle_index(req):
    return aiohttp_jinja2.render_template("landing.html", req, {})


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
    return aiohttp_jinja2.render_template("driver.html", req, {
        "api_prefix": prefix,
        "driver_key": room.driver_key,
        "room_code": code,
    })


async def handle_room_touch(req):
    code = req.match_info["code"]
    if code not in _rooms:
        raise web.HTTPNotFound(text="Room not found or expired")
    prefix = f"/room/{code}"
    return aiohttp_jinja2.render_template("touch.html", req, {
        "api_prefix": prefix,
        "room_code": code,
    })


async def handle_room_join(req):
    code = req.match_info["code"]
    if code not in _rooms:
        raise web.HTTPNotFound(text="Room not found or expired")
    prefix = f"/room/{code}"
    return aiohttp_jinja2.render_template("rider_join.html", req, {
        "code": code,
        "prefix": prefix,
    })


async def handle_room_command(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.touch_driver()
    try:
        body = await req.read()
        cmd = json.loads(body)
    except Exception:
        return web.Response(status=400)
    result = await _process_driver_command(room, cmd)
    if isinstance(result, web.Response):
        return result
    return web.Response(text="{}", content_type="application/json")


async def handle_rider_state(req):
    """Public (no auth) state endpoint for riders."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    d = room._build_rider_state()
    return web.Response(
        text=json.dumps(d),
        content_type="application/json"
    )


async def handle_room_state(req):
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    if not _check_driver_key(req, room):
        raise web.HTTPForbidden(text="Invalid driver key")
    room.touch_driver()
    d = await room._build_driver_state()
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
    """Heartbeat -- keeps the driver grace timer alive while the page is open."""
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


async def handle_driver_ws(req):
    """Driver connects here via WebSocket for bidirectional command/state."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    key = req.rel_url.query.get("key", "")
    if not secrets.compare_digest(key, room.driver_key):
        ws = web.WebSocketResponse(max_msg_size=65536)
        await ws.prepare(req)
        await ws.close(code=4403, message=b"Invalid driver key")
        return ws

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=65536)
    await ws.prepare(req)
    room.driver_wss.add(ws)
    room.touch_driver()

    # Send full state immediately on connect
    try:
        state = await room._build_driver_state()
        await ws.send_str(json.dumps({"type": "state", "data": state}))
    except Exception:
        pass

    # Notify riders that driver is connected
    await room._broadcast_driver_status(True)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "ping":
                    room.touch_driver()
                    await ws.send_str(json.dumps({"type": "pong"}))

                elif msg_type == "command":
                    room.touch_driver()
                    cmd = data.get("data", {})
                    ok = True
                    try:
                        await _process_driver_command(room, cmd)
                    except Exception:
                        ok = False
                    await ws.send_str(json.dumps({"type": "command_ack", "ok": ok}))

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        room.driver_wss.discard(ws)
        if not room.driver_wss:
            await room._broadcast_driver_status(False)

    return ws


async def _process_driver_command(room, cmd: dict):
    """Process a command dict from either WS or HTTP, applying it to the room/engine."""
    if "set_driver_name" in cmd:
        room.driver_name = str(cmd["set_driver_name"])[:30]
        await room._broadcast_participants()
        return
    if "bottle" in cmd:
        b = cmd["bottle"]
        if isinstance(b, dict):
            mode = str(b.get("mode", "normal"))
            duration = max(5, min(60, int(b.get("duration", 10))))
        else:
            mode = "normal"
            duration = 10
        room.bottle_mode  = mode
        room.bottle_until = time.monotonic() + duration
        await room._broadcast_bottle_status(mode, duration)
        return
    if room.engine is not None:
        return await room.engine._handle_command_data(cmd)


async def handle_rider_ws(req):
    """Rider connects here via WebSocket and receives T-code strings."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024*1024)
    await ws.prepare(req)
    room.rider_wss.add(ws)

    # Assign participant slot
    room._rider_counter += 1
    idx = room._rider_counter
    ws_id = id(ws)
    anatomy = room._pick_anatomy(idx)
    room.participants[ws_id] = {
        "name": f"Rider {idx}",
        "anatomy": anatomy,
        "role": "rider",
        "idx": idx,
    }
    print(f"[rider] connected to {code}  (riders: {room.rider_count})")
    await room._broadcast_participants()

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "set_name":
                        name = str(data.get("name", ""))[:30]
                        if ws_id in room.participants:
                            room.participants[ws_id]["name"] = name
                        await room._broadcast_participants()
                    elif data.get("type") == "set_avatar":
                        avatar_data = str(data.get("data", ""))
                        # Limit to 512KB of base64 data
                        if len(avatar_data) <= 512 * 1024 and avatar_data.startswith("data:image/"):
                            if ws_id in room.participants:
                                room.participants[ws_id]["avatar"] = avatar_data
                            await room._broadcast_participants()
                    elif data.get("type") == "like":
                        emoji = str(data.get("emoji", ""))[:4]
                        rider_info = room.participants.get(ws_id, {})
                        room.pending_likes.append({
                            "emoji": emoji,
                            "rider_name": rider_info.get("name", "Rider"),
                            "rider_idx": rider_info.get("idx", 0),
                            "anatomy": rider_info.get("anatomy", ""),
                        })
                except Exception:
                    pass
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        room.rider_wss.discard(ws)
        room.participants.pop(ws_id, None)
        print(f"[rider] disconnected from {code}  (riders: {room.rider_count})")
        await room._broadcast_participants()

    return ws


# -- Touch config (images + overlay from server config)

async def handle_touch_config(req):
    cfg = DriveConfig.load()
    return web.Response(
        text=json.dumps({
            "images": cfg.touch_images,
            "overlay": cfg.overlay_image,
        }),
        content_type="application/json")


# -- Touch assets (shared across all rooms)

async def handle_assets_list(req):
    type_ = req.rel_url.query.get("type", "anatomy")
    folder = Path(__file__).parent / "touch_assets" / type_
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
        if "/" in subdir:
            raise web.HTTPForbidden()
        path = Path(__file__).parent / "touch_assets" / type_ / subdir / name
    else:
        if "/" in name:
            raise web.HTTPForbidden()
        path = Path(__file__).parent / "touch_assets" / type_ / name
    if not path.is_file():
        raise web.HTTPNotFound()
    ct = {".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
              path.suffix.lower(), "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=ct)


async def handle_version(_req):
    path = Path(__file__).parent / "deploy" / "version.json"
    if not path.is_file():
        path = Path(__file__).parent / "version.json"
    if not path.is_file():
        return web.Response(text='{"version":"0.1.0"}', content_type="application/json")
    return web.Response(body=path.read_bytes(), content_type="application/json",
                        headers={"Access-Control-Allow-Origin": "*"})


async def handle_rider_download(req):
    """Placeholder download endpoints for the rider app installer."""
    platform = req.match_info["platform"]
    if platform == "windows":
        raise web.HTTPNotFound(text="Windows build coming soon")
    elif platform == "mac":
        raise web.HTTPNotFound(text="Mac build coming soon")
    raise web.HTTPNotFound()


async def handle_download(req):
    platform = req.match_info["platform"]
    ext = {"windows": ".exe", "mac": ".dmg"}.get(platform)
    if not ext:
        raise web.HTTPNotFound()
    fname = f"ReDriveRider-Setup{ext}" if platform == "windows" else "ReDriveRider.dmg"
    path = Path(__file__).parent / "deploy" / "dist" / fname
    if not path.is_file():
        raise web.HTTPNotFound(text=f"{fname} not yet available -- check back soon.")
    return web.Response(
        body=path.read_bytes(),
        content_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def handle_bottle_png(_req):
    path = Path(__file__).parent / "bottle.png"
    if not path.is_file():
        raise web.HTTPNotFound(text="bottle.png not found")
    return web.Response(body=path.read_bytes(), content_type="image/png")


# -- Waiting room handlers

async def handle_create_waiting(req):
    """Rider creates a waiting room -- no driver key yet."""
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
    return aiohttp_jinja2.render_template("waiting.html", req, {
        "code": code,
        "invite_url": invite_url,
        "ms_remaining": ms_remaining,
    })


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
    return web.Response(
        text=json.dumps({"claimed": True, "touch_url": f"/room/{code}/rider"}),
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
    room._start_push_loop()

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


# -- Anatomy overlay maker

async def handle_anatomy_maker(req):
    return aiohttp_jinja2.render_template("anatomy_maker.html", req, {})


# -- Public session list + privacy

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


# -- Anatomy upload

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

    ct = field.headers.get("Content-Type", "")
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}
    suffix = ext_map.get(ct, "")
    if not suffix:
        fname = field.filename or ""
        suffix = Path(fname).suffix.lower()
    if suffix not in _ALLOWED_ANATOMY_SUFFIXES:
        raise web.HTTPUnsupportedMediaType(text="File must be PNG, JPG, or WEBP")

    uploads_dir = (Path(__file__).parent
                   / "touch_assets" / "anatomy" / "_uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    short_id = uuid.uuid4().hex[:8]
    filename = f"{code}_{short_id}{suffix}"
    (uploads_dir / filename).write_bytes(data)

    rel_name = f"_uploads/{filename}"
    room.custom_anatomies.append(rel_name)

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


async def handle_room_participants(req):
    """Return current participant list and driver name for a room."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")
    parts = list(room.participants.values())
    return web.Response(
        text=json.dumps({"driver_name": room.driver_name, "participants": parts}),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def handle_room_anatomies(req):
    """Return custom anatomies for this room + standard anatomy list."""
    code = req.match_info["code"]
    room = _rooms.get(code)
    if room is None:
        raise web.HTTPNotFound(text="Room not found or expired")

    anatomy_dir = Path(__file__).parent / "touch_assets" / "anatomy"
    anatomy_dir.mkdir(parents=True, exist_ok=True)
    standard = sorted(
        f.name for f in anatomy_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _ALLOWED_ANATOMY_SUFFIXES
    )
    return web.Response(
        text=json.dumps({"custom": room.custom_anatomies, "standard": standard}),
        content_type="application/json"
    )


# -- Room expiry cleanup

def _delete_room_uploads(code: str):
    """Delete anatomy upload files belonging to a room."""
    uploads_dir = (Path(__file__).parent
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
            if room.waiting and now_wall > room.waiting_expires:
                _rooms.pop(code)
                _delete_room_uploads(code)
                print(f"[room] waiting expired  {code}  (total: {len(_rooms)})")
                continue
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


# -- App factory

def build_app(local_room: Optional["Room"] = None) -> web.Application:
    """Build the aiohttp Application.

    If local_room is provided, / redirects to the driver page and /touch
    redirects to the rider page for that room (LAN mode).  Otherwise /
    serves the relay landing page.
    """
    app = web.Application(client_max_size=6 * 1024 * 1024)
    _template_dir = str(Path(__file__).parent / "templates")
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(_template_dir),
                         autoescape=True)

    if local_room:
        async def local_index(req):
            raise web.HTTPFound(f"/room/{local_room.code}?key={local_room.driver_key}")
        async def local_touch(req):
            raise web.HTTPFound(f"/room/{local_room.code}/rider")
        app.router.add_get("/", local_index)
        app.router.add_get("/touch", local_touch)
    else:
        app.router.add_get("/", handle_index)

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
    app.router.add_get("/room/{code}/rider",                   handle_room_touch)
    app.router.add_get("/room/{code}/touch",                   handle_room_touch)
    app.router.add_get("/room/{code}/join",                    handle_room_join)
    app.router.add_post("/room/{code}/command",                handle_room_command)
    app.router.add_get("/room/{code}/state",                   handle_room_state)
    app.router.add_get("/room/{code}/rider-state",             handle_rider_state)
    app.router.add_post("/room/{code}/bottle",                 handle_room_bottle)
    app.router.add_get("/room/{code}/rider-ws",                handle_rider_ws)
    app.router.add_get("/room/{code}/driver-ws",               handle_driver_ws)
    app.router.add_post("/room/{code}/ping",                   handle_driver_ping)
    app.router.add_post("/room/{code}/privacy",                handle_room_privacy)
    # Anatomy upload
    app.router.add_post("/room/{code}/upload_anatomy",         handle_anatomy_upload)
    app.router.add_get("/room/{code}/anatomies",               handle_room_anatomies)
    # Participants
    app.router.add_get("/room/{code}/participants",            handle_room_participants)
    # Static assets
    _public_dir = str(Path(__file__).parent / "public")
    app.router.add_static("/public", _public_dir)
    app.router.add_get("/bottle.png",                          handle_bottle_png)
    app.router.add_get("/touch_config",                        handle_touch_config)
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


# -- Entry points

def run_relay(host: str, port: int):
    """Start the multi-room relay server."""
    app = build_app()
    print(f"ReDrive relay -> http://{host}:{port}", flush=True)
    web.run_app(app, host=host, port=port, access_log=None)


def run_local(host: str, port: int):
    """Start a single-room LAN server with direct ReStim connection."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    code = _new_code()
    room = Room(code, loop, local_restim=True)
    _rooms[code] = room

    app = build_app(local_room=room)

    print(f"ReDrive LAN mode", flush=True)
    print(f"  Driver: http://localhost:{port}/room/{code}?key={room.driver_key}")
    print(f"  Rider:  http://localhost:{port}/room/{code}/rider")
    print(f"  ReStim: {room.engine._cfg.restim_url}")
    print(flush=True)

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        room.stop()
        loop.run_until_complete(runner.cleanup())


def main():
    parser = argparse.ArgumentParser(description="ReDrive server")
    parser.add_argument("--local", action="store_true",
                        help="LAN mode: single room, no auth, connects to local ReStim")
    parser.add_argument("--port", type=int, default=8765,
                        help="HTTP port (default: 8765)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    args = parser.parse_args()

    if args.local:
        run_local(args.host, args.port)
    else:
        run_relay(args.host, args.port)


if __name__ == "__main__":
    main()
