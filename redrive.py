"""redrive.py — ReDrive · ReStim pattern engine with remote "driver" control.

Rider:   python redrive.py
Driver:  open  http://<rider-ip>:<ctrl_port>  in any browser (desktop or phone)

The RIDER always controls maximum power on their own device.
The driver controls pattern selection, relative intensity (0–100% of rider's max),
beta position, and alpha oscillation.

Install: pip install aiohttp
"""

import asyncio
import json
import math
import queue
import random as _rng
import threading
from dataclasses import dataclass, asdict, field, fields as dc_fields
from pathlib import Path
from typing import Optional, Callable

try:
    import tkinter as tk
    from tkinter import ttk
    _HAS_TK = True
except ImportError:
    _HAS_TK = False

import aiohttp
from aiohttp import web
from template_env import get_jinja_env

_jinja_env = get_jinja_env


# ── OGB-inspired dark theme palette ──────────────────────────────────────────

BG     = "#111111"
BG2    = "#1a1a1a"
BG3    = "#222222"
BORDER = "#2a2a2a"
FG     = "#ffffff"
FG2    = "#999999"
ACCENT = "#5fa3ff"
SUCCESS= "#4caf50"
ERROR  = "#f44336"
WARN   = "#ff9800"


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "redrive_config.json"

PATTERNS = ["Hold", "Sine", "Ramp ↑", "Ramp ↓", "Pulse", "Burst", "Random", "Edge"]

# ── Saved presets ─────────────────────────────────────────────────────────────
# Each preset is a full snapshot of driver state applied atomically on load.
# beta_sweep.skew: -1..1 (negative = dwell A, positive = dwell B)
#
PRESETS: dict[str, dict] = {
    "Milking": {
        # Pattern: steady hold (no waveform oscillation) with slow creep feel
        "pattern":       "Hold",
        "intensity":     1.0,          # 100% of rider max
        "hz":            0.05,         # pattern speed (irrelevant for Hold, but saved)
        "depth":         0.12,         # 12% depth
        "alpha":         False,        # alpha oscillation off
        # Beta sweep: envelope ramps hz 0.34→5 Hz quickly, holds 10s, ramps back down 5s, loops
        "beta_mode":     "sweep",
        "beta_sweep":    {"centre": 7700, "width": 2450, "skew": 0.17},
        "sweep_hz_envelope": {
            "base_hz":   0.34,   # slow creep speed
            "peak_hz":   5.0,    # max frenzy
            "ramp_up":   2.0,    # seconds to reach peak
            "hold":      10.0,   # seconds at peak
            "ramp_down": 5.0,    # seconds back to base
        },
        # Ramp config (pre-loaded but NOT auto-started — driver clicks Start Ramp)
        "ramp_target":   1.0,
        "ramp_duration": 60.0,
    },
}


@dataclass
class DriveConfig:
    restim_url:       str   = "ws://localhost:12346/tcode"
    ctrl_port:        int   = 8765          # HTTP port for driver browser UI
    # T-code axes (must match ReStim Preferences → Funscript/T-Code)
    axis_volume:      str   = "L0"
    axis_beta:        str   = "L1"
    axis_alpha:       str   = "L2"
    # Output floor: min T-code value sent when intensity > 0
    tcode_floor:      int   = 0
    # Beta positions  (0 = Left ←── 5000 = Centre ──→ 9999 = Right)
    beta_off:         int   = 9999
    beta_light:       int   = 8099
    beta_active:      int   = 5000
    beta_thresh:      float = 0.35
    # Alpha oscillation
    alpha_min_hz:     float = 0.3
    alpha_max_hz:     float = 1.5
    alpha_min_amp:    float = 0.20
    alpha_max_amp:    float = 0.45
    # Loop tick
    send_interval_ms: int   = 50

    def save(self):
        try:
            CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))
        except Exception:
            pass

    @classmethod
    def load(cls) -> "DriveConfig":
        if CONFIG_FILE.exists():
            try:
                d = json.loads(CONFIG_FILE.read_text())
                valid = {f.name for f in dc_fields(cls)}
                return cls(**{k: v for k, v in d.items() if k in valid})
            except Exception:
                pass
        cfg = cls()
        cfg.save()
        return cfg


# ── T-code helpers ────────────────────────────────────────────────────────────

def _tv(v: float) -> str:
    return str(int(max(0.0, min(1.0, v)) * 9999)).zfill(4)


def _tv_floor(v: float, floor_val: int) -> str:
    if v <= 0.0:
        return "0000"
    return str(max(floor_val, min(9999, int(v * 9999)))).zfill(4)


# ── Pattern engine ────────────────────────────────────────────────────────────

class PatternEngine:
    """Stateful pattern generator — call tick(dt) each frame → float 0..1.

    intensity is a relative value (0..1 of whatever the rider's device max is).
    The rider always controls absolute power limits on their own hardware.
    """

    def __init__(self):
        self.pattern:      str   = "Hold"
        self.intensity:    float = 0.0
        self.hz:           float = 0.5
        self.depth:        float = 1.0   # 1.0 = full swing to 0, 0.0 = flat (= Hold)
        self._phase:       float = 0.0
        self._rng_prev:    float = 0.0
        self._rng_next:    float = 0.0
        self._rng_t:       float = 0.0
        self._edge_phase:  int   = 0     # 0=ramp, 1=hold, 2=drop, 3=rest
        self._edge_t:      float = 0.0

    def tick(self, dt: float) -> float:
        """Advance by dt seconds, return current output 0..1.

        depth controls swing range:
          1.0 = full sweep from 0 to intensity  (default)
          0.5 = sweeps from intensity*0.5 to intensity
          0.0 = flat at intensity  (same as Hold)
        """
        if self.intensity <= 0.0:
            self._phase = 0.0
            return 0.0

        hz    = max(0.01, self.hz)
        p     = self._phase
        i     = self.intensity
        d     = self.depth           # 0..1 — how far the pattern dips
        floor = 1.0 - d              # minimum as fraction of intensity
        pat   = self.pattern

        if pat == "Hold":
            val = i

        elif pat == "Sine":
            # wave 0..1, scaled by depth so output stays in [i*floor, i]
            wave = 0.5 + 0.5 * math.sin(2 * math.pi * p)
            val  = i * (floor + d * wave)

        elif pat == "Ramp ↑":
            val = i * (floor + d * (p % 1.0))

        elif pat == "Ramp ↓":
            val = i * (floor + d * (1.0 - p % 1.0))

        elif pat == "Pulse":
            # triangle wave 0→1→0
            t    = p % 1.0
            wave = 1.0 - abs(2.0 * t - 1.0)
            val  = i * (floor + d * wave)

        elif pat == "Burst":
            # square wave: high = intensity, low = intensity * floor
            val = i if (p % 1.0) < 0.5 else i * floor

        elif pat == "Random":
            # smooth interpolated random
            self._rng_t += dt * hz
            if self._rng_t >= 1.0:
                self._rng_t -= 1.0
                self._rng_prev = self._rng_next
                self._rng_next = _rng.random()
            wave = self._rng_prev + (self._rng_next - self._rng_prev) * self._rng_t
            val  = i * (floor + d * wave)

        elif pat == "Edge":
            # slow ramp → hold near peak → quick drop → short rest → repeat
            # depth applies to the rest phase only — the ramp/hold always reaches full i
            period = 1.0 / hz
            phases = [period * 0.45, period * 0.30, period * 0.10, period * 0.15]
            ep     = self._edge_phase
            if ep == 0:
                val = i * min(1.0, self._edge_t / phases[0]) * 0.92
            elif ep == 1:
                val = i * 0.92
            elif ep == 2:
                val = i * 0.92 * max(0.0, 1.0 - self._edge_t / phases[2])
            else:
                val = i * floor   # rest phase dips to floor, not necessarily 0
            self._edge_t += dt
            if self._edge_t >= phases[ep]:
                self._edge_t = 0.0
                self._edge_phase = (ep + 1) % 4

        else:
            val = i

        self._phase = (p + hz * dt) % 1.0
        return max(0.0, min(1.0, val))

    def set_command(self, cmd: dict):
        if "pattern" in cmd:
            name = cmd["pattern"]
            if name in PATTERNS and name != self.pattern:
                self._phase      = 0.0
                self._edge_phase = 0
                self._edge_t     = 0.0
                self.pattern     = name
        if "intensity" in cmd:
            self.intensity = max(0.0, min(1.0, float(cmd["intensity"])))
        if "hz" in cmd:
            self.hz = max(0.01, min(10.0, float(cmd["hz"])))
        if "depth" in cmd:
            self.depth = max(0.0, min(1.0, float(cmd["depth"])))

    def stop(self):
        self.intensity   = 0.0
        self._phase      = 0.0
        self._edge_phase = 0
        self._edge_t     = 0.0





# ── Bridge + pattern engine (asyncio thread) ──────────────────────────────────

class DriveEngine:
    def __init__(self, cfg: DriveConfig, shared: dict, log_q: queue.Queue,
                 send_hook: Optional[Callable[[str], None]] = None):
        self._cfg          = cfg
        self._shared       = shared
        self._log_q        = log_q
        self._send_hook    = send_hook   # if set: called instead of direct ReStim WS
        self._ws           = None
        self._session      = None
        self._pattern      = PatternEngine()
        self._current_beta = cfg.beta_off
        self._alpha_phase  = 0.0
        self._alpha_parked = True
        self._alpha_on     = True
        self._beta_override: Optional[int] = None   # None = auto
        self._stop_ev: Optional[asyncio.Event] = None
        self._loop:    Optional[asyncio.AbstractEventLoop] = None
        self._next_connect_at: float = 0.0          # reconnect cooldown
        # Ramp state
        self._ramp_active:   bool  = False
        self._ramp_target:   float = 0.0
        self._ramp_start:    float = 0.0
        self._ramp_duration: float = 60.0
        self._ramp_elapsed:  float = 0.0
        # Beta sweep state
        self._beta_mode:          str   = "sweep" # "auto" | "sweep" | "hold" | "spiral"
        self._beta_sweep_hz:      float = 0.15    # full back-and-forth cycles/second
        self._beta_sweep_centre:  int   = 5000    # 0-9999
        self._beta_sweep_width:   int   = 4000    # each side — total swing = 2×width
        self._beta_sweep_phase:   float = 0.0
        self._beta_sweep_skew:    float = 0.0     # -1..1: bias toward A (<0) or B (>0) end
        # Sweep Hz envelope — ramps hz through a cycle (None = off)
        self._sweep_hz_env: dict | None = None  # {base,peak,up,hold,down,t,total}
        # Spiral state — coordinated beta (sine) + alpha (cosine) quadrature sweep
        self._spiral_phase:       float = 0.0
        self._spiral_hz:          float = 0.15
        self._spiral_amp:         float = 1.0     # current amplitude 0..1 (1=full width)
        self._spiral_tighten:     bool  = False   # gradually reduce amplitude over time
        self._spiral_tighten_rate:float = 0.03    # fraction of amp lost per second
        # Gesture loop playback
        self._gesture_active:   bool  = False
        self._gesture_seq:      list  = []  # [(t_rel, beta, intensity), ...]
        self._gesture_t:        float = 0.0
        # LAN mode: driver name + bottle (popper) state
        self._driver_name:     str   = ""
        self._bottle_until:    float = 0.0
        self._bottle_mode:     str   = "normal"
        # LAN mode WebSocket tracking
        self._driver_wss: set = set()
        self._rider_wss:  set = set()

    def _log(self, msg: str):
        self._log_q.put_nowait(msg)

    # ── ReStim connection ────────────────────────────────────────────────────

    async def _connect(self) -> bool:
        # Close existing resources before creating new ones
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self._cfg.restim_url)
            self._log(f"Connected → {self._cfg.restim_url}")
            return True
        except Exception as e:
            self._log(f"Connect failed: {e}")
            return False

    async def _send(self, cmd: str):
        if self._send_hook is not None:
            self._send_hook(cmd)
            return
        if self._ws is None or self._ws.closed:
            now = self._loop.time()
            if now < self._next_connect_at:
                return                          # still in cooldown — drop silently
            await self._connect()
            self._next_connect_at = self._loop.time() + 5.0  # retry at most every 5s
            if self._ws is None:
                return
        try:
            await self._ws.send_str(cmd)
            # Fan out T-code to rider WebSockets
            if self._rider_wss:
                dead = set()
                for rws in list(self._rider_wss):
                    try:
                        await rws.send_str(cmd)
                    except Exception:
                        dead.add(rws)
                self._rider_wss -= dead
        except Exception as e:
            self._log(f"Send error: {e}")
            try:
                if self._ws is not None and not self._ws.closed:
                    await self._ws.close()
            except Exception:
                pass
            self._ws = None
            try:
                if self._session is not None and not self._session.closed:
                    await self._session.close()
            except Exception:
                pass
            self._session = None

    # ── HTTP server (driver browser UI) ─────────────────────────────────────

    async def _handle_index(self, _req):
        env = _jinja_env()
        tmpl = env.get_template("driver.html")
        html = tmpl.render(api_prefix="", driver_key="", room_code="")
        return web.Response(text=html, content_type="text/html")

    async def _handle_touch(self, _req):
        env = _jinja_env()
        tmpl = env.get_template("touch.html")
        html = tmpl.render(api_prefix="", room_code="")
        return web.Response(text=html, content_type="text/html")

    async def _handle_assets_list(self, req):
        """Return JSON list of PNG/JPG files in touch_assets/{type}/ subfolder."""
        type_ = req.rel_url.query.get("type", "anatomy")
        if "/" in type_ or "\\" in type_ or ".." in type_:
            raise web.HTTPForbidden()
        folder = Path(__file__).parent / "touch_assets" / type_
        folder.mkdir(parents=True, exist_ok=True)
        files = sorted(
            f.name for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        )
        return web.Response(text=json.dumps(files), content_type="application/json")

    async def _handle_assets_file(self, req):
        """Serve a file from touch_assets/{type}/{name}."""
        type_ = req.match_info["type"]
        name  = req.match_info["name"]
        if "/" in type_ or "\\" in type_ or ".." in type_ or "/" in name or ".." in name:
            raise web.HTTPForbidden()
        path = Path(__file__).parent / "touch_assets" / type_ / name
        if not path.is_file():
            raise web.HTTPNotFound()
        ct = {".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower(), "application/octet-stream")
        return web.Response(body=path.read_bytes(), content_type=ct)


    async def _handle_command_data(self, cmd: dict):
        """Process an already-parsed command dict (called by server.py relay)."""
        return await self._process_command(cmd)

    async def _handle_command(self, req):
        try:
            cmd = await req.json()
        except Exception:
            return web.Response(status=400)
        return await self._process_command(cmd)

    async def _process_command(self, cmd: dict):

        if cmd.get("stop"):
            self._pattern.stop()
            self._ramp_active    = False
            self._gesture_active = False
            self._gesture_seq    = []
        elif "gesture_record" in cmd:
            pts = cmd["gesture_record"]
            if len(pts) >= 4:
                t0 = float(pts[0]["t"])
                self._gesture_seq = [
                    (float(p["t"]) - t0, int(p["beta"]), float(p["intensity"]))
                    for p in pts
                ]
                self._gesture_t      = 0.0
                self._gesture_active = True
                total = self._gesture_seq[-1][0]
                self._log(f"Gesture loop: {len(pts)} pts · {total:.1f}s")
        elif "load_preset" in cmd:
            name = cmd["load_preset"]
            if name in PRESETS:
                p = PRESETS[name]
                self._log(f"Preset loaded: {name}")
                # Cancel any running ramp / gesture
                self._ramp_active    = False
                self._gesture_active = False
                self._gesture_seq    = []
                # Pattern + intensity + speed + depth
                self._pattern.set_command({
                    "pattern":   p.get("pattern",   "Hold"),
                    "intensity": p.get("intensity", 1.0),
                    "hz":        p.get("hz",        0.5),
                    "depth":     p.get("depth",     1.0),
                })
                # Alpha
                self._alpha_on = p.get("alpha", True)
                # Beta mode
                mode = p.get("beta_mode", "sweep")
                if mode in ("auto", "sweep", "hold", "spiral"):
                    self._beta_mode = mode
                    self._beta_sweep_phase = 0.0
                    if mode == "spiral":
                        self._spiral_phase = 0.0
                        self._spiral_amp   = 1.0
                # Sweep parameters
                bs = p.get("beta_sweep", {})
                if "hz"     in bs:
                    self._beta_sweep_hz     = max(0.01, min(5.0,  float(bs["hz"])))
                if "centre" in bs:
                    self._beta_sweep_centre = max(0,    min(9999, int(bs["centre"])))
                if "width"  in bs:
                    self._beta_sweep_width  = max(0,    min(4999, int(bs["width"])))
                if "skew"   in bs:
                    self._beta_sweep_skew   = max(-1.0, min(1.0,  float(bs["skew"])))
                # Ramp config (pre-fill sliders, don't auto-start)
                if "ramp_target"   in p:
                    self._ramp_target   = max(0.0, min(1.0, float(p["ramp_target"])))
                if "ramp_duration" in p:
                    self._ramp_duration = max(1.0, float(p["ramp_duration"]))
                # Sweep Hz envelope
                if "sweep_hz_envelope" in p:
                    e = p["sweep_hz_envelope"]
                    up   = max(0.1, float(e.get("ramp_up",   2.0)))
                    hold = max(0.1, float(e.get("hold",     10.0)))
                    down = max(0.1, float(e.get("ramp_down",  5.0)))
                    self._sweep_hz_env = {
                        "base":  max(0.01, float(e.get("base_hz",  0.34))),
                        "peak":  max(0.01, float(e.get("peak_hz",  5.0))),
                        "up": up, "hold": hold, "down": down,
                        "t": 0.0, "total": up + hold + down,
                    }
                else:
                    self._sweep_hz_env = None
        elif "ramp" in cmd:
            r = cmd["ramp"]
            self._ramp_target   = max(0.0, min(1.0, float(r.get("target",   1.0))))
            self._ramp_duration = max(1.0,           float(r.get("duration", 60.0)))
            self._ramp_start    = self._pattern.intensity
            self._ramp_elapsed  = 0.0
            self._ramp_active   = True
            self._log(
                f"Ramp: {int(self._ramp_start*100)}%"
                f" → {int(self._ramp_target*100)}%"
                f" over {self._ramp_duration:.0f}s"
            )
        elif cmd.get("ramp_stop"):
            self._ramp_active = False
            self._log("Ramp stopped")
        elif "set_driver_name" in cmd:
            self._driver_name = str(cmd["set_driver_name"])[:40]
            self._shared["__driver_name__"] = self._driver_name
        elif "bottle" in cmd:
            import time
            b = cmd["bottle"]
            self._bottle_mode = b.get("mode", "normal")
            dur = float(b.get("duration", 10))
            self._bottle_until = time.monotonic() + dur
            self._shared["__bottle_until__"] = self._bottle_until
            self._shared["__bottle_mode__"] = self._bottle_mode
            # Immediate push to rider WebSockets
            if self._rider_wss:
                await self._broadcast_to_riders(json.dumps({
                    "type": "bottle_status",
                    "active": True,
                    "remaining": dur,
                    "mode": self._bottle_mode,
                }))
        else:
            # gesture_stop or any explicit beta_mode change cancels loop
            if cmd.get("gesture_stop") or "beta_mode" in cmd:
                self._gesture_active = False
                self._gesture_seq    = []
            # Manual intensity cancels any active ramp
            if "intensity" in cmd and self._ramp_active:
                self._ramp_active = False
            self._pattern.set_command(cmd)
            if "beta" in cmd:
                self._beta_override = int(cmd["beta"])
            if "alpha" in cmd:
                self._alpha_on = bool(cmd["alpha"])
            if "beta_mode" in cmd:
                mode = cmd["beta_mode"]
                if mode in ("auto", "sweep", "hold", "spiral"):
                    self._beta_mode = mode
                    self._beta_sweep_phase = 0.0
                    if mode == "spiral":
                        self._spiral_phase = 0.0
                        self._spiral_amp   = 1.0
                    self._log(f"Beta mode: {mode}")
            if "beta_sweep" in cmd:
                s = cmd["beta_sweep"]
                if "hz" in s:
                    self._beta_sweep_hz = max(0.01, min(5.0, float(s["hz"])))
                if "centre" in s:
                    self._beta_sweep_centre = max(0, min(9999, int(s["centre"])))
                if "width" in s:
                    self._beta_sweep_width = max(0, min(4999, int(s["width"])))
                if "skew" in s:
                    self._beta_sweep_skew = max(-1.0, min(1.0, float(s["skew"])))
            if "spiral" in cmd:
                s = cmd["spiral"]
                if "hz" in s:
                    self._spiral_hz = max(0.01, min(2.0, float(s["hz"])))
                if "tighten" in s:
                    self._spiral_tighten = bool(s["tighten"])
                if "tighten_rate" in s:
                    self._spiral_tighten_rate = max(0.005, min(0.5, float(s["tighten_rate"])))
                if s.get("reset"):
                    self._spiral_amp   = 1.0
                    self._spiral_phase = 0.0
                    self._log("Spiral reset")

        # Mirror to shared dict for the GUI poll loop
        self._shared["__cmd_pattern__"]   = self._pattern.pattern
        self._shared["__cmd_intensity__"] = self._pattern.intensity
        self._shared["__cmd_hz__"]        = self._pattern.hz
        self._shared["__cmd_depth__"]     = self._pattern.depth
        return web.Response(text="ok")

    def _build_state_dict(self):
        """Build the driver state dict (used by HTTP and WS endpoints)."""
        return {
            "vol":           self._shared.get("__live__l0", 0.0),
            "beta":          int(self._shared.get("__live__l1",
                                 self._cfg.beta_off / 9999.0) * 9999),
            "alpha":         self._shared.get("__live__l2", 0.0),
            "pattern":       self._pattern.pattern,
            "intensity":     self._pattern.intensity,
            "hz":            self._pattern.hz,
            "depth":         self._pattern.depth,
            "ramp_active":   self._ramp_active,
            "ramp_progress": self._shared.get("__ramp_progress__", 0.0),
            "ramp_target":    self._ramp_target,
            "ramp_duration":  self._ramp_duration,
            "beta_mode":      self._beta_mode,
            "sweep_hz":       self._beta_sweep_hz,
            "sweep_centre":   self._beta_sweep_centre,
            "sweep_width":    self._beta_sweep_width,
            "sweep_skew":     int(self._beta_sweep_skew * 100),
            "alpha_on":       self._alpha_on,
            "spiral_amp":      self._spiral_amp,
            "spiral_tighten":  self._spiral_tighten,
            "gesture_active":  self._gesture_active,
            "gesture_dur":     self._gesture_seq[-1][0] if self._gesture_seq else 0.0,
            "presets":         list(PRESETS.keys()),
        }

    def _build_rider_state_dict(self):
        """Build rider state dict (used by HTTP and WS endpoints)."""
        import time
        now = time.monotonic()
        active = now < self._bottle_until
        remaining = max(0, self._bottle_until - now) if active else 0
        return {
            "intensity": self._pattern.intensity,
            "bottle_active": active,
            "bottle_remaining": round(remaining, 1),
            "bottle_mode": self._bottle_mode,
            "driver_name": self._driver_name,
        }

    async def _handle_state(self, _req):
        d = self._build_state_dict()
        return web.Response(text=json.dumps(d), content_type="application/json")

    async def _handle_rider_state(self, _req):
        d = self._build_rider_state_dict()
        return web.Response(text=json.dumps(d), content_type="application/json")

    # ── WebSocket handlers (LAN mode) ────────────────────────────────────────

    async def _handle_driver_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=65536)
        await ws.prepare(req)
        self._driver_wss.add(ws)

        # Send initial state
        state = self._build_state_dict()
        await ws.send_str(json.dumps({"type": "state", "data": state}))

        # Broadcast driver_status to riders
        await self._broadcast_to_riders(json.dumps({
            "type": "driver_status",
            "connected": True,
            "name": self._driver_name or "Anonymous",
        }))

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "command":
                            cmd = data.get("data", {})
                            await self._process_command(cmd)
                            await ws.send_str(json.dumps({"type": "command_ack", "ok": True}))
                        elif data.get("type") == "ping":
                            await ws.send_str(json.dumps({"type": "pong"}))
                    except Exception as e:
                        await ws.send_str(json.dumps({
                            "type": "command_ack", "ok": False, "error": str(e),
                        }))
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._driver_wss.discard(ws)
            if not self._driver_wss:
                await self._broadcast_to_riders(json.dumps({
                    "type": "driver_status",
                    "connected": False,
                    "name": "",
                }))
        return ws

    async def _handle_rider_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=65536)
        await ws.prepare(req)
        self._rider_wss.add(ws)

        # Send initial rider state
        rstate = self._build_rider_state_dict()
        rstate["type"] = "rider_state"
        await ws.send_str(json.dumps(rstate))

        # Send current driver status
        driver_connected = len(self._driver_wss) > 0
        await ws.send_str(json.dumps({
            "type": "driver_status",
            "connected": driver_connected,
            "name": self._driver_name or ("Anonymous" if driver_connected else ""),
        }))

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    pass  # rider messages (future: set_name, like, etc.)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._rider_wss.discard(ws)
        return ws

    async def _broadcast_to_riders(self, msg_str: str):
        """Send a string message to all connected rider WebSockets."""
        dead = set()
        for ws in list(self._rider_wss):
            try:
                await ws.send_str(msg_str)
            except Exception:
                dead.add(ws)
        self._rider_wss -= dead

    # ── App builder + HTTP server ─────────────────────────────────────────

    def _build_app(self):
        """Build and return the aiohttp Application with all routes."""
        app = web.Application()
        app.router.add_get("/",                              self._handle_index)
        app.router.add_get("/touch",                         self._handle_touch)
        app.router.add_post("/command",                      self._handle_command)
        app.router.add_get("/state",                         self._handle_state)
        app.router.add_get("/rider-state",                   self._handle_rider_state)
        app.router.add_get("/driver-ws",                     self._handle_driver_ws)
        app.router.add_get("/rider-ws",                      self._handle_rider_ws)
        app.router.add_static("/public",                     str(Path(__file__).parent / "public"))
        app.router.add_get("/touch_assets/list",             self._handle_assets_list)
        app.router.add_get("/touch_assets/{type}/{name}",    self._handle_assets_file)
        return app

    async def _start_http(self):
        app = self._build_app()
        # Ensure asset directories exist at startup
        for sub in ("anatomy", "tools"):
            (Path(__file__).parent / "touch_assets" / sub).mkdir(parents=True, exist_ok=True)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._cfg.ctrl_port)
        await site.start()
        self._log(
            f"Driver UI → http://localhost:{self._cfg.ctrl_port}"
            f"  |  share your LAN IP for remote access"
        )

    # ── Output loops ─────────────────────────────────────────────────────────

    async def _pattern_loop(self):
        """Drives L0 volume and L1 beta from the pattern engine."""
        last = self._loop.time()
        while not self._stop_ev.is_set():
            cfg = self._cfg
            now = self._loop.time()
            dt  = now - last
            last = now

            # ── Gesture loop (takes over entire output when active) ────────────
            if self._gesture_active and self._gesture_seq:
                g_beta, g_int = self._gesture_advance(dt)
                g_int = max(0.0, min(1.0, g_int))
                self._pattern.intensity = g_int
                self._shared["__live__l0"] = g_int
                self._shared["__live__l1"] = g_beta / 9999.0
                tv = _tv_floor(g_int, cfg.tcode_floor)
                await self._send(
                    f"{cfg.axis_volume}{tv}I{cfg.send_interval_ms} "
                    f"{cfg.axis_beta}{g_beta:04d}I{cfg.send_interval_ms}"
                )
                self._current_beta = g_beta
                await asyncio.sleep(cfg.send_interval_ms / 1000.0)
                continue

            # Apply ramp — updates pattern intensity before tick
            if self._ramp_active:
                self._ramp_elapsed += dt
                progress = min(1.0, self._ramp_elapsed / max(0.01, self._ramp_duration))
                self._pattern.intensity = (
                    self._ramp_start
                    + (self._ramp_target - self._ramp_start) * progress
                )
                self._shared["__ramp_progress__"] = progress
                if progress >= 1.0:
                    self._ramp_active = False
                    self._log(f"Ramp complete → {int(self._ramp_target * 100)}%")

            intensity = self._pattern.tick(dt)
            self._shared["__live__l0"] = intensity

            # L0 volume
            tv    = _tv_floor(intensity, cfg.tcode_floor)
            parts = [f"{cfg.axis_volume}{tv}I{cfg.send_interval_ms}"]

            # L1 beta
            if intensity <= 0.0:
                # Park at neutral when silent
                desired = cfg.beta_off
                if desired != self._current_beta:
                    parts.append(f"{cfg.axis_beta}{desired:04d}I500")
                    self._current_beta = desired

            elif self._beta_mode == "sweep":
                # Sweep Hz envelope: ramp up → hold → ramp down → repeat
                if self._sweep_hz_env is not None:
                    e = self._sweep_hz_env
                    e['t'] = (e['t'] + dt) % e['total']
                    t = e['t']
                    if t < e['up']:
                        self._beta_sweep_hz = e['base'] + (e['peak'] - e['base']) * (t / e['up'])
                    elif t < e['up'] + e['hold']:
                        self._beta_sweep_hz = e['peak']
                    else:
                        td = t - e['up'] - e['hold']
                        self._beta_sweep_hz = e['peak'] - (e['peak'] - e['base']) * (td / e['down'])
                # Continuous sweep between centre ± width
                # Skew > 0 → spends more time near B end; skew < 0 → near A end
                # Uses adjusted-sine: sin(θ + k·sin(θ)) which biases dwell time asymmetrically
                theta = 2.0 * math.pi * self._beta_sweep_phase
                sin_t = math.sin(theta)
                raw_wave = (math.sin(theta + self._beta_sweep_skew * sin_t)
                            if abs(self._beta_sweep_skew) > 0.001 else sin_t)
                raw = self._beta_sweep_centre + self._beta_sweep_width * raw_wave
                desired = max(0, min(9999, int(raw)))
                self._beta_sweep_phase = (
                    self._beta_sweep_phase + self._beta_sweep_hz * dt) % 1.0
                # Always send — sweep is always changing
                parts.append(
                    f"{cfg.axis_beta}{desired:04d}I{cfg.send_interval_ms}")
                self._current_beta = desired

            elif self._beta_mode == "spiral":
                # Beta sweeps as sine; alpha loop reads same phase as cosine (quadrature)
                # Tighten mode gradually reduces amplitude → reset → repeat
                effective_hz = self._spiral_hz * (
                    1.0 + (1.0 - self._spiral_amp) * 2.0)
                theta   = 2.0 * math.pi * self._spiral_phase
                raw     = (self._beta_sweep_centre
                           + self._beta_sweep_width
                           * self._spiral_amp * math.sin(theta))
                desired = max(0, min(9999, int(raw)))
                self._spiral_phase = (
                    self._spiral_phase + effective_hz * dt) % 1.0
                if self._spiral_tighten:
                    self._spiral_amp = max(
                        0.15, self._spiral_amp - self._spiral_tighten_rate * dt)
                    if self._spiral_amp <= 0.15:
                        self._spiral_amp   = 1.0
                        self._spiral_phase = 0.0
                        self._log("Spiral reset")
                parts.append(
                    f"{cfg.axis_beta}{desired:04d}I{cfg.send_interval_ms}")
                self._current_beta = desired

            elif self._beta_mode == "hold":
                desired = (self._beta_override
                           if self._beta_override is not None
                           else cfg.beta_active)
                if desired != self._current_beta:
                    parts.append(f"{cfg.axis_beta}{desired:04d}I200")
                    self._current_beta = desired

            else:  # auto — intensity-driven 3-position
                desired = (cfg.beta_active
                           if intensity >= cfg.beta_thresh
                           else cfg.beta_light)
                if desired != self._current_beta:
                    parts.append(f"{cfg.axis_beta}{desired:04d}I200")
                    self._current_beta = desired

            self._shared["__live__l1"] = self._current_beta / 9999.0

            if parts:
                await self._send(" ".join(parts))

            await asyncio.sleep(cfg.send_interval_ms / 1000.0)

    def _gesture_advance(self, dt: float) -> tuple[int, float]:
        """Advance gesture playback by dt and return interpolated (beta, intensity)."""
        seq = self._gesture_seq
        if not seq:
            return self._current_beta, self._pattern.intensity
        self._gesture_t += dt
        total = seq[-1][0]
        if total < 0.001:
            return int(seq[0][1]), float(seq[0][2])
        t = self._gesture_t % total
        for i in range(len(seq) - 1):
            t0, b0, i0 = seq[i]
            t1, b1, i1 = seq[i + 1]
            if t0 <= t < t1:
                frac = (t - t0) / max(0.001, t1 - t0)
                return int(b0 + frac * (b1 - b0)), float(i0 + frac * (i1 - i0))
        return int(seq[-1][1]), float(seq[-1][2])

    async def _alpha_loop(self):
        """Drives L2 alpha oscillation."""
        while not self._stop_ev.is_set():
            cfg = self._cfg
            dt  = cfg.send_interval_ms / 1000.0
            eff = self._pattern.intensity if self._alpha_on else 0.0

            if eff < 0.01:
                if not self._alpha_parked:
                    await self._send(f"{cfg.axis_alpha}{_tv(0.5)}I500")
                    self._alpha_parked = True
                self._alpha_phase = 0.0
                self._shared["__live__l2"] = 0.0
            else:
                self._alpha_parked = False
                amp = cfg.alpha_min_amp + (cfg.alpha_max_amp - cfg.alpha_min_amp) * eff
                if self._beta_mode == "spiral":
                    # Quadrature: cosine of shared spiral_phase → 90° offset from beta sine
                    theta = 2.0 * math.pi * self._spiral_phase
                    pos   = 0.5 + amp * self._spiral_amp * math.cos(theta)
                else:
                    hz  = cfg.alpha_min_hz + (cfg.alpha_max_hz - cfg.alpha_min_hz) * eff
                    pos = 0.5 + amp * math.sin(2 * math.pi * self._alpha_phase)
                    self._alpha_phase = (self._alpha_phase + hz * dt) % 1.0
                await self._send(f"{cfg.axis_alpha}{_tv(pos)}I{int(dt * 1000)}")
                self._shared["__live__l2"] = eff

            await asyncio.sleep(dt)

    # ── State push loop (WS broadcast) ──────────────────────────────────────

    async def _state_push_loop(self):
        """Push state to driver WS at 5Hz and rider state at ~2Hz."""
        import time
        rider_tick = 0
        while not self._stop_ev.is_set():
            await asyncio.sleep(0.2)
            # Driver state push
            if self._driver_wss:
                state = self._build_state_dict()
                msg = json.dumps({"type": "state", "data": state})
                dead = set()
                for ws in list(self._driver_wss):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        dead.add(ws)
                self._driver_wss -= dead

            # Rider state push (every ~600ms)
            rider_tick += 1
            if rider_tick >= 3 and self._rider_wss:
                rider_tick = 0
                now = time.monotonic()
                active = now < self._bottle_until
                rmsg = json.dumps({
                    "type": "rider_state",
                    "intensity": self._pattern.intensity,
                    "bottle_active": active,
                    "bottle_remaining": round(max(0, self._bottle_until - now), 1) if active else 0,
                    "bottle_mode": self._bottle_mode,
                    "driver_name": self._driver_name,
                    "driver_connected": len(self._driver_wss) > 0,
                })
                dead = set()
                for ws in list(self._rider_wss):
                    try:
                        await ws.send_str(rmsg)
                    except Exception:
                        dead.add(ws)
                self._rider_wss -= dead

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _run_async(self):
        self._loop    = asyncio.get_event_loop()
        self._stop_ev = asyncio.Event()

        if self._send_hook is None:
            # Local mode: start HTTP server and connect to ReStim directly
            await self._start_http()
            if not await self._connect():
                self._log("Could not connect to ReStim — check URL and try Start again")
            # Park all axes on start
            cfg = self._cfg
            await self._send(
                f"{cfg.axis_beta}{cfg.beta_off:04d}I0 "
                f"{cfg.axis_volume}0000I0 "
                f"{cfg.axis_alpha}{_tv(0.5)}I0"
            )

        try:
            await asyncio.gather(
                self._pattern_loop(), self._alpha_loop(), self._state_push_loop()
            )
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()
            self._log("Engine stopped.")

    def start(self):
        threading.Thread(
            target=lambda: asyncio.run(self._run_async()), daemon=True
        ).start()

    def stop(self):
        if self._stop_ev and self._loop:
            self._loop.call_soon_threadsafe(self._stop_ev.set)


# ── Custom widgets ────────────────────────────────────────────────────────────

class IntensityBar(tk.Canvas):
    W, H = 90, 11

    def __init__(self, parent, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=BG3, highlightthickness=1,
                         highlightbackground=BORDER, **kw)
        self._rect = self.create_rectangle(0, 0, 0, self.H, fill=SUCCESS, outline="")

    def set(self, v: float):
        w = int(max(0.0, min(1.0, v)) * self.W)
        self.coords(self._rect, 0, 0, w, self.H)
        if v < 0.5:
            r, g = int(v * 2 * 220), 187
        else:
            r, g = 220, int((1.0 - (v - 0.5) * 2) * 187)
        self.itemconfig(self._rect, fill=f"#{r:02x}{g:02x}10")


# ── Main GUI ──────────────────────────────────────────────────────────────────

class DriveGUI:
    def __init__(self):
        self.cfg       = DriveConfig.load()
        self._shared:  dict         = {}
        self._log_q:   queue.Queue  = queue.Queue()
        self._engine:  Optional[DriveEngine] = None
        self._running: bool         = False

        self.root = tk.Tk()
        self.root.title("ReStim Drive")
        self.root.minsize(520, 500)

        self._apply_theme()
        self._build_ui()
        self.root.after(150, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Dark theme ────────────────────────────────────────────────────────────

    def _apply_theme(self):
        self.root.configure(bg=BG)
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass

        st.configure("TFrame",            background=BG)
        st.configure("TLabelframe",       background=BG, foreground=FG2,
                     bordercolor=BORDER, relief="flat")
        st.configure("TLabelframe.Label", background=BG, foreground=FG2,
                     font=("Arial", 9))
        st.configure("TLabel",            background=BG, foreground=FG,
                     font=("Arial", 9))
        st.configure("TNotebook",         background=BG, bordercolor=BORDER,
                     tabmargins=[0, 0, 0, 0])
        st.configure("TNotebook.Tab",     background=BG3, foreground=FG2,
                     padding=[10, 4], font=("Arial", 9))
        st.map("TNotebook.Tab",
               background=[("selected", BG2), ("active", BG3)],
               foreground=[("selected", FG),  ("active", FG)])
        st.configure("TButton",           background=BG3, foreground=FG,
                     bordercolor=BORDER, focuscolor="none",
                     relief="flat", font=("Arial", 9), padding=[4, 2])
        st.map("TButton",
               background=[("active", "#333333"), ("pressed", "#2a2a2a")],
               foreground=[("disabled", FG2)])
        st.configure("Accent.TButton",    background=ACCENT, foreground="#000000",
                     bordercolor=ACCENT, focuscolor="none",
                     relief="flat", font=("Arial", 9, "bold"), padding=[6, 3])
        st.map("Accent.TButton",
               background=[("active", "#4d91ee"), ("pressed", "#3d81de")])
        st.configure("TEntry",
                     fieldbackground=BG3, foreground=FG, bordercolor=BORDER,
                     insertcolor=FG, selectbackground=ACCENT,
                     selectforeground="#000000")
        st.configure("TSpinbox",
                     fieldbackground=BG3, foreground=FG, bordercolor=BORDER,
                     insertcolor=FG, arrowcolor=FG2, background=BG3)
        st.configure("TCheckbutton",      background=BG, foreground=FG,
                     focuscolor="none", font=("Arial", 9))
        st.map("TCheckbutton",
               background=[("active", BG)],
               indicatorcolor=[("selected", ACCENT), ("!selected", BG3)])
        st.configure("TScale",            background=BG, troughcolor=BG3,
                     sliderlength=12, sliderrelief="flat", bordercolor=BORDER)
        st.map("TScale", background=[("active", ACCENT)])
        st.configure("TScrollbar",        background=BG3, troughcolor=BG,
                     bordercolor=BORDER, arrowcolor=FG2, relief="flat")
        st.map("TScrollbar", background=[("active", "#444444")])
        st.configure("TSeparator",        background=BORDER)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Connection bar ─────────────────────────────────────────────────
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=8, pady=(8, 0))

        self._dot_canvas = tk.Canvas(top, width=14, height=14,
                                     bg=BG, highlightthickness=0)
        self._dot_canvas.pack(side=tk.LEFT, padx=(0, 4))
        self._dot = self._dot_canvas.create_oval(2, 2, 12, 12, fill=ERROR, outline="")

        self._status_lbl = ttk.Label(top, text="Stopped", width=12)
        self._status_lbl.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top, text="ReStim:").pack(side=tk.LEFT)
        self._url_var = tk.StringVar(value=self.cfg.restim_url)
        ttk.Entry(top, textvariable=self._url_var, width=22).pack(side=tk.LEFT, padx=4)

        self._start_btn = ttk.Button(top, text="Start", style="Accent.TButton",
                                     command=self._toggle, width=8)
        self._start_btn.pack(side=tk.RIGHT)

        # ── Driver URL ─────────────────────────────────────────────────────
        info = ttk.Frame(self.root)
        info.pack(fill=tk.X, padx=8, pady=(4, 0))
        ttk.Label(info, text="Driver URL:", font=("Arial", 8),
                  foreground=FG2).pack(side=tk.LEFT)
        self._ctrl_url_lbl = ttk.Label(
            info,
            text=f"http://localhost:{self.cfg.ctrl_port}  (start engine first)",
            font=("Arial", 8), foreground=ACCENT)
        self._ctrl_url_lbl.pack(side=tk.LEFT, padx=4)
        self._copy_url_btn = ttk.Button(
            info, text="Copy", width=7,
            command=lambda: (
                self.root.clipboard_clear(),
                self.root.clipboard_append(f"http://localhost:{self.cfg.ctrl_port}"),
                self._copy_url_btn.configure(text="Copied"),
                self.root.after(1500, lambda: self._copy_url_btn.configure(text="Copy")),
            ))
        self._copy_url_btn.pack(side=tk.LEFT, padx=2)

        self._driver_status_lbl = ttk.Label(
            info, text="", font=("Arial", 8), foreground=FG2)
        self._driver_status_lbl.pack(side=tk.RIGHT, padx=4)

        self._poppers_lbl = ttk.Label(
            self.root, text="", font=("Arial", 10, "bold"),
            foreground=WARN)
        self._poppers_lbl.pack(fill=tk.X, padx=8)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(
            fill=tk.X, padx=8, pady=8)

        # ── Live output bar ────────────────────────────────────────────────
        live = ttk.Frame(self.root)
        live.pack(fill=tk.X, padx=8)

        ttk.Label(live, text="Live →", font=("Arial", 7),
                  foreground=FG2).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(live, text="Vol", font=("Arial", 7),
                  foreground=FG2).pack(side=tk.LEFT)
        self._vol_bar = IntensityBar(live)
        self._vol_bar.pack(side=tk.LEFT, padx=(2, 2))
        self._vol_lbl = ttk.Label(live, text=" 0%", width=5, font=("Consolas", 7))
        self._vol_lbl.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(live, text="β", font=("Arial", 7),
                  foreground=FG2).pack(side=tk.LEFT)
        self._beta_lbl = ttk.Label(live, text="9999", width=5, font=("Consolas", 7))
        self._beta_lbl.pack(side=tk.LEFT, padx=(2, 10))

        ttk.Label(live, text="α", font=("Arial", 7),
                  foreground=FG2).pack(side=tk.LEFT)
        self._alpha_bar = IntensityBar(live)
        self._alpha_bar.pack(side=tk.LEFT, padx=(2, 2))
        self._alpha_lbl = ttk.Label(live, text=" 0%", width=5, font=("Consolas", 7))
        self._alpha_lbl.pack(side=tk.LEFT)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(
            fill=tk.X, padx=8, pady=8)

        # ── Local override panel ───────────────────────────────────────────
        ctrl = ttk.LabelFrame(self.root, text="Local override", padding=8)
        ctrl.pack(fill=tk.X, padx=8)

        # Pattern buttons
        pat_row = ttk.Frame(ctrl)
        pat_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(pat_row, text="Pattern", font=("Arial", 8),
                  foreground=FG2).pack(side=tk.LEFT, padx=(0, 8))
        self._pat_btns: dict[str, ttk.Button] = {}
        for p in PATTERNS:
            b = ttk.Button(pat_row, text=p, width=7,
                           command=lambda pat=p: self._set_pattern(pat))
            b.pack(side=tk.LEFT, padx=2)
            self._pat_btns[p] = b
        self._pat_btns["Hold"].configure(style="Accent.TButton")

        # Intensity + Hz sliders
        sliders = ttk.Frame(ctrl)
        sliders.pack(fill=tk.X, pady=(4, 0))

        ttk.Label(sliders, text="Intensity", width=12).grid(
            row=0, column=0, sticky="w", pady=2)
        self._int_var = tk.DoubleVar(value=0.0)
        self._int_lbl = ttk.Label(sliders, text=" 0%", width=6)
        self._int_lbl.grid(row=0, column=2, sticky="w")
        ttk.Scale(sliders, from_=0, to=1, variable=self._int_var,
                  command=self._on_intensity, length=200).grid(
            row=0, column=1, padx=4, sticky="w")
        ttk.Label(sliders, text="Set max power limits in\nReStim Preferences",
                  font=("Arial", 7), foreground=FG2, justify="left").grid(
            row=0, column=3, rowspan=2, sticky="nw", padx=(8, 0))

        ttk.Label(sliders, text="Speed (Hz)", width=12).grid(
            row=1, column=0, sticky="w", pady=2)
        self._hz_var = tk.DoubleVar(value=0.5)
        self._hz_lbl = ttk.Label(sliders, text="0.50 Hz", width=8)
        self._hz_lbl.grid(row=1, column=2, sticky="w")
        ttk.Scale(sliders, from_=0.05, to=8.0, variable=self._hz_var,
                  command=self._on_hz, length=200).grid(
            row=1, column=1, padx=4, sticky="w")

        ttk.Label(sliders, text="Depth", width=12).grid(
            row=2, column=0, sticky="w", pady=2)
        self._depth_var = tk.DoubleVar(value=1.0)
        self._depth_lbl = ttk.Label(sliders, text="100%", width=8)
        self._depth_lbl.grid(row=2, column=2, sticky="w")
        ttk.Scale(sliders, from_=0.0, to=1.0, variable=self._depth_var,
                  command=self._on_depth, length=200).grid(
            row=2, column=1, padx=4, sticky="w")
        ttk.Label(sliders, text="← flat · · · full swing →",
                  font=("Arial", 7), foreground=FG2).grid(
            row=2, column=3, sticky="w", padx=4)

        # ── Current pattern readout ────────────────────────────────────────
        self._state_lbl = ttk.Label(
            self.root,
            text="Pattern: Hold   Intensity: 0%   Hz: 0.50   Depth: 100%",
            font=("Consolas", 8), foreground=FG2)
        self._state_lbl.pack(anchor="w", padx=10, pady=(6, 0))

        # ── Log ────────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(self.root, text="Log", padding=(4, 2))
        lf.pack(fill=tk.X, padx=8, pady=(8, 8))
        self._log_text = tk.Text(
            lf, height=4, state=tk.DISABLED,
            font=("Consolas", 8), bg=BG2, fg=FG2,
            insertbackground=FG, relief=tk.FLAT,
            wrap=tk.WORD, highlightthickness=0)
        self._log_text.pack(fill=tk.X)

    # ── Local control handlers ────────────────────────────────────────────────

    def _set_pattern(self, p: str):
        for name, btn in self._pat_btns.items():
            btn.configure(style="Accent.TButton" if name == p else "TButton")
        if self._engine:
            self._engine._pattern.set_command({"pattern": p})
            self._shared["__cmd_pattern__"] = p

    def _on_intensity(self, v):
        fv = float(v)
        self._int_lbl.config(text=f"{int(fv * 100):2d}%")
        if self._engine:
            self._engine._pattern.set_command({"intensity": fv})
            self._shared["__cmd_intensity__"] = fv

    def _on_hz(self, v):
        fv = float(v)
        self._hz_lbl.config(text=f"{fv:.2f} Hz")
        if self._engine:
            self._engine._pattern.set_command({"hz": fv})
            self._shared["__cmd_hz__"] = fv

    def _on_depth(self, v):
        fv = float(v)
        self._depth_lbl.config(text=f"{int(fv * 100)}%")
        if self._engine:
            self._engine._pattern.set_command({"depth": fv})
            self._shared["__cmd_depth__"] = fv

    # ── Log ───────────────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        t = self._log_text
        t.configure(state=tk.NORMAL)
        t.insert(tk.END, msg + "\n")
        lines = int(t.index(tk.END).split(".")[0])
        if lines > 202:
            t.delete("1.0", f"{lines - 200}.0")
        t.see(tk.END)
        t.configure(state=tk.DISABLED)

    # ── Bridge control ────────────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            if self._engine:
                self._engine.stop()
            self._running = False
            self._start_btn.config(text="Start")
            self._dot_canvas.itemconfig(self._dot, fill=ERROR)
            self._status_lbl.config(text="Stopped")
        else:
            self.cfg.restim_url = self._url_var.get().strip()
            self._shared.clear()
            self._engine = DriveEngine(self.cfg, self._shared, self._log_q)
            self._engine.start()
            self._running = True
            self._start_btn.config(text="Stop")
            self._dot_canvas.itemconfig(self._dot, fill=WARN)
            self._status_lbl.config(text="Connecting…")

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                self._append_log(msg)
                low = msg.lower()
                if "connected →" in low:
                    self._dot_canvas.itemconfig(self._dot, fill=SUCCESS)
                    self._status_lbl.config(text="Connected")
                elif "failed" in low or "error" in low:
                    self._dot_canvas.itemconfig(self._dot, fill=ERROR)
                    self._status_lbl.config(text="Error")
        except queue.Empty:
            pass

        # Live output bars
        l0 = self._shared.get("__live__l0", 0.0)
        l1 = self._shared.get("__live__l1", self.cfg.beta_off / 9999.0)
        l2 = self._shared.get("__live__l2", 0.0)
        self._vol_bar.set(l0)
        self._vol_lbl.config(text=f"{int(l0 * 100):2d}%")
        self._beta_lbl.config(text=str(int(l1 * 9999)))
        self._alpha_bar.set(l2)
        self._alpha_lbl.config(text=f"{int(l2 * 100):2d}%")

        # State readout
        pat   = self._shared.get("__cmd_pattern__", "Hold")
        it    = int(self._shared.get("__cmd_intensity__", 0.0) * 100)
        hz    = self._shared.get("__cmd_hz__", 0.5)
        depth = int(self._shared.get("__cmd_depth__", 1.0) * 100)
        self._state_lbl.config(
            text=f"Pattern: {pat:<8}  Intensity: {it:3d}%  Hz: {hz:.2f}  Depth: {depth}%")

        # Driver name display
        name = self._shared.get("__driver_name__", "")
        if name:
            self._driver_status_lbl.config(text=f"Driver: {name}")
        elif self._running:
            self._driver_status_lbl.config(text="Driver: Anonymous")
        else:
            self._driver_status_lbl.config(text="")

        # Poppers countdown display
        import time as _time
        bottle_until = self._shared.get("__bottle_until__", 0)
        now = _time.monotonic()
        if now < bottle_until:
            remaining = int(bottle_until - now) + 1
            mode = self._shared.get("__bottle_mode__", "normal")
            mode_label = mode.replace("_", " ").title()
            self._poppers_lbl.config(text=f"  POPPERS ({mode_label}) - {remaining}s")
        else:
            self._poppers_lbl.config(text="")

        self.root.after(150, self._poll)

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._running and self._engine:
            self._engine.stop()
        self.cfg.save()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DriveGUI().run()
