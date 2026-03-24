"""engine.py — ReDrive core: pattern generation + ReStim connection.

Pure engine logic with no tkinter dependencies.
Import this from server.py to drive the ReStim hardware.
"""

import asyncio
import json
import math
import queue
import random as _rng
import threading
import time
from dataclasses import dataclass, asdict, field, fields as dc_fields
from pathlib import Path
from typing import Optional, Callable

import aiohttp


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
    # ReStim defaults: V0=volume, L0=alpha position, L1=beta position
    axis_volume:      str   = "V0"
    axis_beta:        str   = "L1"
    axis_alpha:       str   = "L0"
    # Output floor: min T-code value sent when intensity > 0
    tcode_floor:      int   = 0
    # Beta positions  (9999 = L+ ←── 5000 = Centre ──→ 0 = R+)
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
    # Touch panel images (admin-configurable)
    # Each entry: {"name": "Display Name", "filename": "image.png"}
    # Files live in touch_assets/anatomy/
    touch_images:     list  = field(default_factory=lambda: [
        {"name": "Hunk 1", "filename": "hunk1.png"},
        {"name": "Hunk 2", "filename": "hunk2.png"},
        {"name": "Hunk 3", "filename": "hunk3.png"},
        {"name": "Furry",  "filename": "furry1.png"},
    ])
    # Overlay image (transparent PNG in touch_assets/anatomy/, drawn over the touch image)
    overlay_image:    str   = "overlay.png"

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
        self._alpha_override: Optional[float] = None  # None = oscillate normally
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
        self._gesture_seq:      list  = []  # [(t_rel, beta, alpha, intensity), ...]
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

    # ── Command processing ────────────────────────────────────────────────────

    async def _handle_command_data(self, cmd: dict):
        """Process an already-parsed command dict (called by server.py relay)."""
        await self._process_command(cmd)

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
                    (float(p["t"]) - t0, int(p["beta"]),
                     float(p.get("alpha", 0.5)), float(p["intensity"]))
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
            # gesture_stop clears the gesture entirely
            if cmd.get("gesture_stop"):
                self._gesture_active = False
                self._gesture_seq    = []
            # beta_mode change pauses gesture (preserved for resume) unless switching TO touch
            elif "beta_mode" in cmd:
                if cmd["beta_mode"] == "touch":
                    # Resume gesture if we have one
                    if self._gesture_seq:
                        self._gesture_active = True
                else:
                    self._gesture_active = False
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
                if mode in ("auto", "sweep", "hold", "spiral", "touch"):
                    self._beta_mode = mode
                    self._beta_sweep_phase = 0.0
                    self._alpha_override = None  # release alpha to oscillator
                    if mode == "spiral":
                        self._spiral_phase = 0.0
                        self._spiral_amp   = 1.0
                    self._log(f"Beta mode: {mode}")
            # alpha_pos after beta_mode so it re-overrides when both present (tcOnDown)
            if "alpha_pos" in cmd:
                self._alpha_override = float(cmd["alpha_pos"])
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

    async def _broadcast_to_riders(self, msg_str: str):
        """Send a string message to all connected rider WebSockets."""
        dead = set()
        for ws in list(self._rider_wss):
            try:
                await ws.send_str(msg_str)
            except Exception:
                dead.add(ws)
        self._rider_wss -= dead

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
                g_beta, g_alpha, g_int = self._gesture_advance(dt)
                g_int = max(0.0, min(1.0, g_int))
                g_alpha = max(0.0, min(1.0, g_alpha))
                self._pattern.intensity = g_int
                self._shared["__live__l0"] = g_int
                self._shared["__live__l1"] = g_beta / 9999.0
                self._shared["__live__l2"] = g_int  # alpha active during gesture
                tv = _tv_floor(g_int, cfg.tcode_floor)
                await self._send(
                    f"{cfg.axis_volume}{tv}I{cfg.send_interval_ms} "
                    f"{cfg.axis_beta}{g_beta:04d}I{cfg.send_interval_ms} "
                    f"{cfg.axis_alpha}{_tv(g_alpha)}I{cfg.send_interval_ms}"
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

    def _gesture_advance(self, dt: float) -> tuple[int, float, float]:
        """Advance gesture playback by dt and return interpolated (beta, alpha, intensity)."""
        seq = self._gesture_seq
        if not seq:
            return self._current_beta, 0.5, self._pattern.intensity
        self._gesture_t += dt
        total = seq[-1][0]
        if total < 0.001:
            return int(seq[0][1]), float(seq[0][2]), float(seq[0][3])
        t = self._gesture_t % total
        for i in range(len(seq) - 1):
            t0, b0, a0, i0 = seq[i]
            t1, b1, a1, i1 = seq[i + 1]
            if t0 <= t < t1:
                frac = (t - t0) / max(0.001, t1 - t0)
                return (int(b0 + frac * (b1 - b0)),
                        float(a0 + frac * (a1 - a0)),
                        float(i0 + frac * (i1 - i0)))
        return int(seq[-1][1]), float(seq[-1][2]), float(seq[-1][3])

    async def _alpha_loop(self):
        """Drives L2 alpha oscillation."""
        while not self._stop_ev.is_set():
            cfg = self._cfg
            dt  = cfg.send_interval_ms / 1000.0

            # Gesture playback handles alpha directly — skip
            if self._gesture_active:
                await asyncio.sleep(dt)
                continue

            # Touch arc override — driver is sending explicit alpha position
            if self._alpha_override is not None:
                pos = max(0.0, min(1.0, self._alpha_override))
                await self._send(f"{cfg.axis_alpha}{_tv(pos)}I{int(dt * 1000)}")
                self._alpha_parked = False
                self._shared["__live__l2"] = self._pattern.intensity
                await asyncio.sleep(dt)
                continue

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

    # ── HTTP handler wrappers (used in LAN mode tests) ─────────────────────

    async def _handle_command(self, req):
        from aiohttp import web
        try:
            cmd = await req.json()
        except Exception:
            return web.Response(status=400)
        await self._process_command(cmd)
        return web.Response(text="ok")

    async def _handle_state(self, _req):
        from aiohttp import web
        d = self._build_state_dict()
        return web.Response(text=json.dumps(d), content_type="application/json")

    async def _handle_rider_state(self, _req):
        from aiohttp import web
        d = self._build_rider_state_dict()
        return web.Response(text=json.dumps(d), content_type="application/json")

    async def _handle_driver_ws(self, req):
        from aiohttp import web
        ws = web.WebSocketResponse(max_msg_size=65536)
        await ws.prepare(req)
        self._driver_wss.add(ws)

        state = self._build_state_dict()
        await ws.send_str(json.dumps({"type": "state", "data": state}))

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
        from aiohttp import web
        ws = web.WebSocketResponse(max_msg_size=1024*1024)
        await ws.prepare(req)
        self._rider_wss.add(ws)

        rstate = self._build_rider_state_dict()
        rstate["type"] = "rider_state"
        await ws.send_str(json.dumps(rstate))

        driver_connected = len(self._driver_wss) > 0
        await ws.send_str(json.dumps({
            "type": "driver_status",
            "connected": driver_connected,
            "name": self._driver_name or ("Anonymous" if driver_connected else ""),
        }))

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    pass
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._rider_wss.discard(ws)
        return ws

    def _build_app(self):
        """Build a standalone aiohttp app for LAN mode testing."""
        from aiohttp import web
        from template_env import get_jinja_env
        _jinja_env = get_jinja_env

        engine = self

        async def _handle_index(_req):
            env = _jinja_env()
            tmpl = env.get_template("driver.html")
            html = tmpl.render(api_prefix="", driver_key="", room_code="")
            return web.Response(text=html, content_type="text/html")

        async def _handle_touch(_req):
            env = _jinja_env()
            tmpl = env.get_template("touch.html")
            html = tmpl.render(api_prefix="", room_code="")
            return web.Response(text=html, content_type="text/html")

        app = web.Application()
        app.router.add_get("/", _handle_index)
        app.router.add_get("/touch", _handle_touch)
        app.router.add_post("/command", engine._handle_command)
        app.router.add_get("/state", engine._handle_state)
        app.router.add_get("/rider-state", engine._handle_rider_state)
        app.router.add_get("/driver-ws", engine._handle_driver_ws)
        app.router.add_get("/rider-ws", engine._handle_rider_ws)
        app.router.add_static("/public", str(Path(__file__).parent / "public"))
        return app

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _run_async(self):
        self._loop    = asyncio.get_event_loop()
        self._stop_ev = asyncio.Event()

        if self._send_hook is None:
            # Local mode: connect to ReStim directly
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
                self._pattern_loop(), self._alpha_loop()
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
