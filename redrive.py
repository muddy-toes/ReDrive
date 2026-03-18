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
# ⚠  KEEP IN SYNC with JS_PRESETS in DRIVER_HTML below.
#    When adding a preset here, add a matching entry there (slider raw values
#    differ from real Hz — see the formula comments above JS_PRESETS).
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
    restim_url:       str   = "ws://localhost:12346"
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


# ── Embedded driver web UI ────────────────────────────────────────────────────

DRIVER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>ReStim Drive</title>
<style>
  *,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
  :root {
    --bg:#0a0a0a; --bg2:#111111; --bg3:#1a1a1a;
    --glass:rgba(20,20,35,0.75);
    --border:rgba(95,163,255,0.20);
    --accent:#5fa3ff; --accent-glow:#5fa3ff44;
    --fg:#f1f1f1; --fg2:#a3a3a3;
    --err:#f43f5e; --ok:#4ade80; --warn:#fbbf24;
    --radius:16px;
  }
  body {
    background:var(--bg);
    color:var(--fg);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px;
    padding:10px;
    height:100dvh; overflow:hidden;
    display:flex; flex-direction:column; box-sizing:border-box; gap:10px;
    max-width:1280px; margin:0 auto;
    background:radial-gradient(circle at 50% 10%,rgba(95,163,255,0.06) 0%,transparent 70%);
  }

  /* ── Page header ── */
  #page-hdr {
    background:var(--glass); backdrop-filter:blur(20px);
    border:1px solid var(--border); border-radius:var(--radius);
    padding:14px 18px; flex-shrink:0;
    box-shadow:0 8px 32px rgba(0,0,0,0.45);
    display:flex; flex-direction:column; gap:10px;
  }
  #hdr-row1 { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  #hdr-row1 h1 {
    font-size:19px; font-weight:700; letter-spacing:-0.04em;
    background:linear-gradient(90deg,#5fa3ff,#c4b5fd);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
    flex:1; margin:0;
  }
  #dot { width:13px; height:13px; border-radius:50%; background:var(--err); box-shadow:0 0 14px var(--err); flex-shrink:0; }
  #status-text { color:var(--fg2); font-size:13px; font-weight:500; }
  #hdr-row2 { display:flex; align-items:center; gap:8px; }
  #driver-name-input {
    flex:1; min-width:0;
    background:rgba(255,255,255,0.06); border:1px solid var(--border);
    border-radius:12px; padding:10px 14px; color:white; font-size:14px;
  }
  #hdr-row3 { display:flex; gap:8px; align-items:center; }
  #stop-btn {
    flex:1; background:#e11d48; color:white; border:none; border-radius:12px;
    font-weight:700; padding:13px 24px; font-size:15px; cursor:pointer; height:44px;
  }
  #stop-btn:active { background:#be123c; }
  #bottle-btn {
    flex:1; background:var(--bg3); border:1px solid var(--border);
    border-radius:12px; color:var(--fg2); font-size:14px; cursor:pointer;
    height:44px; padding:0 16px;
  }
  #bottle-btn.active {
    color:#ffcc14; border-color:#ffcc14;
    box-shadow:0 0 0 2px rgba(255,204,20,0.35),0 0 14px rgba(255,204,20,0.20);
  }

  /* ── Page body ── */
  #page-body { display:flex; gap:10px; flex:1; min-height:0; overflow:hidden; }

  /* ── Rider column ── */
  #rider-col {
    width:80px; flex-shrink:0;
    display:flex; flex-direction:column; gap:8px;
  }
  #rider-cards { flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:8px; }
  #overlay-btn, #cursor-btn {
    width:100%; padding:7px 4px; background:var(--bg3);
    border:1px solid var(--border); border-radius:10px;
    color:var(--fg2); font-size:10px; cursor:pointer; text-align:center;
    line-height:1.3;
  }
  #overlay-btn.active, #cursor-btn.active {
    background:rgba(95,163,255,0.15); border-color:var(--accent); color:var(--accent);
  }
  .rider-card {
    width:100%; aspect-ratio:58/80;
    border-radius:12px; background:var(--bg3);
    background-size:cover; background-position:top center;
    border:2px solid var(--border);
    box-shadow:0 4px 16px rgba(0,0,0,0.5);
    position:relative; flex-shrink:0;
  }
  .rider-card::after {
    content:attr(data-name);
    position:absolute; bottom:0; left:0; right:0;
    background:linear-gradient(transparent,rgba(10,10,15,0.9));
    padding:8px 4px 5px;
    font-size:9px; text-align:center; color:#ddd;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    border-radius:0 0 10px 10px;
  }

  /* ── Tab area ── */
  #tab-area { flex:1; min-width:0; display:flex; flex-direction:column; gap:8px; overflow:hidden; }
  #tab-btns {
    display:flex; background:var(--glass); border-radius:12px;
    padding:5px; border:1px solid var(--border); gap:5px; flex-shrink:0;
  }
  .tab-btn {
    flex:1; padding:11px; border-radius:9px;
    font-weight:600; font-size:14px;
    background:transparent; border:1px solid transparent; color:var(--fg2); cursor:pointer;
  }
  .tab-btn.active {
    background:rgba(95,163,255,0.18); border-color:var(--accent);
    color:var(--accent); box-shadow:0 4px 14px var(--accent-glow);
  }

  /* ── Controls panel ── */
  #controls-panel {
    background:var(--glass); backdrop-filter:blur(20px);
    border:1px solid var(--border); border-radius:18px;
    padding:20px 18px; flex:1; overflow-y:auto;
    display:none; flex-direction:column; gap:20px;
  }
  .section-label {
    font-size:11px; font-weight:700; letter-spacing:0.8px;
    color:var(--accent); text-transform:uppercase; margin-bottom:6px;
  }
  /* Preset row */
  #preset-row { display:flex; gap:6px; flex-wrap:wrap; }
  .preset-btn {
    padding:9px 14px; background:var(--bg3); border:1px solid var(--border);
    border-radius:10px; color:var(--fg2); font-size:12px; font-weight:600;
    cursor:pointer; transition:all .15s;
  }
  .preset-btn:hover, .preset-btn:active { background:#252525; color:var(--fg); }
  /* Pattern grid */
  #pattern-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(80px,1fr)); gap:8px; }
  .pat-btn {
    padding:14px 8px; background:var(--bg3); border:1px solid var(--border);
    border-radius:12px; font-size:13px; font-weight:500;
    color:var(--fg2); cursor:pointer; transition:all .15s; text-align:center;
  }
  .pat-btn:active { transform:scale(0.96); }
  .pat-btn.active { background:var(--accent); color:#000; font-weight:700; box-shadow:0 0 18px var(--accent-glow); border-color:var(--accent); }
  /* Sliders */
  .slider-row { display:flex; flex-direction:column; gap:6px; }
  .slider-header { display:flex; justify-content:space-between; align-items:center; }
  .slider-label { font-size:13px; color:var(--fg2); }
  .slider-val { font-size:13px; color:var(--fg); font-weight:600; }
  input[type=range] {
    -webkit-appearance:none; appearance:none; width:100%;
    height:8px; background:rgba(255,255,255,0.1); border-radius:9999px; outline:none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; width:26px; height:26px;
    background:var(--accent); border-radius:50%;
    box-shadow:0 0 14px var(--accent-glow); cursor:pointer;
  }
  #intensity-slider { height:12px; }
  #intensity-slider::-webkit-slider-thumb { width:34px; height:34px; }
  /* Mode / hold / ramp buttons */
  .mode-btn, .hold-btn, .ramp-btn {
    padding:10px 14px; background:var(--bg3); border:1px solid var(--border);
    border-radius:10px; color:var(--fg2); font-size:13px; font-weight:600;
    cursor:pointer; transition:all .15s;
  }
  .mode-btn.active, .hold-btn.active {
    background:rgba(95,163,255,0.18); border-color:var(--accent); color:var(--accent);
  }
  /* Beta mode */
  #beta-mode-row { display:flex; gap:6px; flex-wrap:wrap; }
  #sweep-controls, #hold-controls, #spiral-controls { display:none; flex-direction:column; gap:8px; }
  #hold-beta-row { display:flex; gap:6px; }
  #spiral-btn-row { display:flex; gap:6px; }
  #spiral-amp-wrap { position:relative; height:28px; border-radius:6px; overflow:hidden; }
  #spiral-amp-track { position:absolute; inset:0; background:linear-gradient(to right,var(--accent),#c4b5fd); border-radius:6px; }
  #spiral-amp-bar { position:absolute; right:0; top:0; bottom:0; background:rgba(0,0,0,0.55); border-radius:0 6px 6px 0; }
  /* Alpha row */
  #alpha-row { display:flex; align-items:center; gap:8px; }
  #alpha-toggle {
    padding:8px 14px; background:var(--bg3); border:1px solid var(--border);
    border-radius:10px; color:var(--fg2); font-size:13px; cursor:pointer;
  }
  #alpha-toggle.active { background:rgba(95,163,255,0.18); border-color:var(--accent); color:var(--accent); }
  /* Viz row */
  #viz-row { display:flex; gap:10px; align-items:flex-start; }
  #waveform { flex:1; height:90px; border-radius:12px; background:#0f0f0f; }
  #tri-canvas { width:110px; height:96px; border-radius:12px; background:#0f0f0f; flex-shrink:0; }
  /* Beta position */
  #beta-pos { background:rgba(15,15,25,0.7); padding:12px; border-radius:12px; }
  #beta-track { height:8px; background:#222; border-radius:9999px; position:relative; margin:10px 0; }
  #beta-dot {
    width:18px; height:18px; background:var(--warn); border-radius:50%;
    position:absolute; top:-5px; box-shadow:0 0 14px var(--warn);
    transition:left .4s cubic-bezier(0.23,1,0.32,1);
  }
  #beta-labels { display:flex; justify-content:space-between; font-size:10px; color:var(--fg2); }
  /* Ramp */
  #ramp-btn-row { display:flex; gap:6px; }
  #ramp-go {
    flex:1; padding:10px; background:#16a34a; color:white; border:none;
    border-radius:10px; font-weight:700; font-size:13px; cursor:pointer;
  }
  #ramp-go:active { background:#15803d; }
  #ramp-stop-b {
    flex:1; padding:10px; background:#dc2626; color:white; border:none;
    border-radius:10px; font-weight:700; font-size:13px; cursor:pointer;
  }
  #ramp-stop-b:active { background:#b91c1c; }
  #ramp-progress-wrap { display:none; flex-direction:column; gap:4px; }
  #ramp-track { height:8px; background:#222; border-radius:9999px; overflow:hidden; }
  #ramp-bar { height:100%; width:0%; background:var(--accent); border-radius:9999px; transition:width .5s linear; }
  #ramp-pct { font-size:11px; color:var(--fg2); text-align:right; }
  /* Live indicator */
  #live { display:none; font-size:11px; color:var(--ok); font-weight:700; letter-spacing:.05em; }

  /* ── Touch panel ── */
  #touch-panel { display:none; flex:1; flex-direction:column; gap:8px; min-height:0; overflow:hidden; }
  #tc-body { display:flex; gap:8px; flex:1; min-height:0; align-items:flex-start; }
  #tc-main {
    flex:0 0 auto; width:min(100%,340px); aspect-ratio:9/16;
    min-height:0; position:relative; border-radius:18px;
    background:#0f0f0f; align-self:flex-start;
    box-shadow:0 12px 40px rgba(0,0,0,0.6);
  }
  #tc-main canvas { display:block; width:100%; height:100%; border-radius:18px; cursor:none; touch-action:none; }
  @keyframes tc-loop-pulse {
    0%,100% { box-shadow:0 0 0 0 rgba(95,163,255,0.5); }
    50%      { box-shadow:0 0 0 8px rgba(95,163,255,0); }
  }
  #tc-main.looping { animation:tc-loop-pulse 1.1s ease-in-out infinite; }
  /* Category buttons */
  #cat-panel { display:flex; flex-direction:column; gap:6px; }
  .cat-btn {
    padding:10px 12px; background:var(--bg3); border:1px solid var(--border);
    border-radius:10px; color:var(--fg2); font-size:12px; font-weight:600;
    cursor:pointer; transition:all .15s; text-align:center; min-width:56px;
  }
  .cat-btn.active { background:rgba(95,163,255,0.18); border-color:var(--accent); color:var(--accent); }

  /* ── Small screen ── */
  @media(max-width:600px) {
    body { padding:8px; gap:8px; }
    #page-body { flex-direction:column; }
    #rider-col { width:100%; flex-direction:row; flex-wrap:nowrap; overflow-x:auto; padding-bottom:4px; }
    #rider-cards { flex-direction:row; gap:8px; overflow-y:visible; overflow-x:auto; }
    .rider-card { width:64px; flex-shrink:0; }
    #overlay-btn, #cursor-btn { width:auto; min-width:52px; flex-shrink:0; }
  }
</style>
</head>
<body>

<!-- ── Always-visible header ── -->
<div id="page-hdr">
  <div id="hdr-row1">
    <h1>RESTIM DRIVE</h1>
    <div id="dot"></div>
    <span id="status-text">Connecting…</span>
  </div>
  <div id="hdr-row2">
    <input id="driver-name-input" type="text" placeholder="Your driver name…"
           oninput="setDriverName(this.value)">
    <button onclick="copyRoomCode(this)"
      style="padding:5px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--fg2);border-radius:5px;font-size:12px;cursor:pointer;white-space:nowrap">&#128279; Room</button>
    <button onclick="copyRiderLink(this)"
      style="padding:5px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--fg2);border-radius:5px;font-size:12px;cursor:pointer;white-space:nowrap">&#9654; Rider Link</button>
  </div>
  <div id="hdr-row3">
    <!-- Poppers mode radios above the Poppers button -->
    <div style="flex:1;display:flex;flex-direction:column;gap:4px">
      <div id="poppers-mode-row" style="display:flex;gap:10px;align-items:center;justify-content:flex-end">
        <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:11px;color:#999">
          <input type="radio" name="poppers-mode" value="normal" checked onchange="_poppersMode=this.value" style="accent-color:var(--accent);cursor:pointer">
          <span id="pm-lbl-normal" style="color:#fff">Normal</span>
        </label>
        <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:11px;color:#999">
          <input type="radio" name="poppers-mode" value="deep_huff" onchange="_poppersMode=this.value" style="accent-color:var(--accent);cursor:pointer">
          <span id="pm-lbl-deep_huff">Deep Huff</span>
        </label>
        <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:11px;color:#999">
          <input type="radio" name="poppers-mode" value="double_hit" onchange="_poppersMode=this.value" style="accent-color:var(--accent);cursor:pointer">
          <span id="pm-lbl-double_hit">Double Hit</span>
        </label>
      </div>
      <div style="display:flex;gap:6px">
        <button id="stop-btn" onclick="sendStop()">&#11035; STOP</button>
        <button id="bottle-btn" onclick="sendBottle()"
          style="flex:1;height:44px;background:var(--bg3);color:var(--fg2);border:1px solid var(--border);border-radius:6px;font-size:14px;font-weight:bold;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:5px"><img src="/bottle.png" style="width:18px;height:18px;object-fit:contain">Poppers</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Page body ── -->
<div id="page-body">

  <!-- Left: rider column -->
  <div id="rider-col">
    <div id="rider-cards"></div>
    <button id="overlay-btn" onclick="toggleOverlay(this)" title="Overlay guide: ON">GUIDE<br>ON</button>
    <button id="cursor-btn" onclick="toggleCursor(this)" title="Cursor: dot">DOT</button>
  </div>

  <!-- Right: tab area -->
  <div id="tab-area">
    <div id="tab-btns">
      <button class="tab-btn active" data-tab="controls" onclick="setTab('controls')">Controls</button>
      <button class="tab-btn" data-tab="touch" onclick="setTab('touch')">Touch</button>
    </div>

    <!-- Controls tab -->
    <div id="controls-panel" style="display:flex">
      <div class="section-label" style="margin-top:4px">Live</div>
      <div id="viz-row">
        <canvas id="waveform" height="72"></canvas>
        <canvas id="tri-canvas" width="110" height="90"></canvas>
      </div>
      <div id="beta-pos">
        <div id="beta-track"><div id="beta-dot" style="left:50%"></div></div>
        <div id="beta-labels"><span>&#9668; L</span><span>Centre</span><span>R &#9658;</span></div>
      </div>
      <div id="live"></div>

      <div class="section-label">Presets</div>
      <div id="preset-row"></div>

      <div class="section-label">Pattern</div>
      <div id="pattern-grid"></div>

      <div class="slider-row">
        <div class="slider-header">
          <span class="slider-label">Intensity  <small style="color:var(--fg2)">(% of rider's max)</small></span>
          <span class="slider-val" id="int-val">0%</span>
        </div>
        <input type="range" id="intensity-slider" min="0" max="100" value="0"
               oninput="onIntensity(this.value)">
      </div>

      <div class="slider-row">
        <div class="slider-header">
          <span class="slider-label">Speed (Hz)</span>
          <span class="slider-val" id="hz-val">0.50 Hz</span>
        </div>
        <input type="range" id="hz-slider" min="1" max="100" value="10"
               oninput="onHz(this.value)">
      </div>

      <div class="slider-row">
        <div class="slider-header">
          <span class="slider-label">Depth  <small style="color:var(--fg2)">how far pattern dips  (0% = flat, 100% = full swing)</small></span>
          <span class="slider-val" id="depth-val">100%</span>
        </div>
        <input type="range" id="depth-slider" min="0" max="100" value="100"
               oninput="onDepth(this.value)">
      </div>

      <div class="section-label">Ramp</div>
      <div class="slider-row">
        <div class="slider-header">
          <span class="slider-label">Ramp to</span>
          <span class="slider-val" id="ramp-target-val">80%</span>
        </div>
        <input type="range" id="ramp-target" min="0" max="100" value="80"
               oninput="document.getElementById('ramp-target-val').textContent=this.value+'%'">
      </div>
      <div class="slider-row">
        <div class="slider-header">
          <span class="slider-label">Over</span>
          <span class="slider-val" id="ramp-dur-val">60s</span>
        </div>
        <input type="range" id="ramp-duration" min="5" max="600" value="60"
               oninput="onRampDur(this.value)">
      </div>
      <div id="ramp-btn-row">
        <button class="ramp-btn" id="ramp-go" onclick="startRamp()">&#9654;  Start Ramp</button>
        <button class="ramp-btn" id="ramp-stop-b" onclick="stopRamp()">&#9632;  Stop Ramp</button>
      </div>
      <div id="ramp-progress-wrap">
        <div id="ramp-track"><div id="ramp-bar"></div></div>
        <span id="ramp-pct">0% &#8594; 80%</span>
      </div>

      <div class="section-label">Beta  &#183;  sweep between electrodes</div>
      <div id="beta-mode-row">
        <button class="mode-btn" data-mode="auto" onclick="setBetaMode(this)">Auto</button>
        <button class="mode-btn active" data-mode="sweep" onclick="setBetaMode(this)">Sweep &#8596;</button>
        <button class="mode-btn" data-mode="spiral" onclick="setBetaMode(this)">Spiral &#9678;</button>
        <button class="mode-btn" data-mode="hold" onclick="setBetaMode(this)">Hold</button>
      </div>

      <div id="sweep-controls">
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Sweep speed</span>
            <span class="slider-val" id="sweep-hz-val">0.15 Hz</span>
          </div>
          <input type="range" id="sweep-hz" min="1" max="200" value="15"
                 oninput="onSweepHz(this.value)">
        </div>
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Centre position</span>
            <span class="slider-val" id="sweep-ctr-val">Centre</span>
          </div>
          <input type="range" id="sweep-centre" min="0" max="9999" value="5000"
                 oninput="onSweepCentre(this.value)">
        </div>
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Sweep width</span>
            <span class="slider-val" id="sweep-width-val">80%</span>
          </div>
          <input type="range" id="sweep-width" min="0" max="4999" value="4000"
                 oninput="onSweepWidth(this.value)">
        </div>
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Skew  <small style="color:var(--fg2)">&#8592; A dwell &#183; even &#183; B dwell &#8594;</small></span>
            <span class="slider-val" id="sweep-skew-val">even</span>
          </div>
          <input type="range" id="sweep-skew" min="-100" max="100" value="0"
                 oninput="onSweepSkew(this.value)">
        </div>
      </div>

      <div id="hold-controls" style="display:none">
        <div id="hold-beta-row">
          <button class="hold-btn active" data-beta="8099" onclick="setHoldBeta(this)">&#9668; A</button>
          <button class="hold-btn" data-beta="5000" onclick="setHoldBeta(this)">Centre</button>
          <button class="hold-btn" data-beta="1900" onclick="setHoldBeta(this)">B &#9658;</button>
        </div>
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Fine position</span>
            <span class="slider-val" id="hold-pos-val">Centre</span>
          </div>
          <input type="range" id="hold-pos" min="0" max="9999" value="5000"
                 oninput="onHoldPos(this.value)">
        </div>
      </div>

      <div id="spiral-controls" style="display:none">
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Spiral speed</span>
            <span class="slider-val" id="spiral-hz-val">0.15 Hz</span>
          </div>
          <input type="range" id="spiral-hz" min="1" max="200" value="15"
                 oninput="onSpiralHz(this.value)">
        </div>
        <div class="slider-row">
          <div class="slider-header">
            <span class="slider-label">Tighten rate  <small style="color:var(--fg2)">how fast spiral shrinks</small></span>
            <span class="slider-val" id="spiral-rate-val">3%/s</span>
          </div>
          <input type="range" id="spiral-rate" min="1" max="50" value="3"
                 oninput="onSpiralRate(this.value)">
        </div>
        <div id="spiral-btn-row">
          <button class="hold-btn" id="spiral-tighten-btn" onclick="toggleSpiralTighten()">Tighten: OFF</button>
          <button class="hold-btn" onclick="resetSpiral()">Reset &#8634;</button>
        </div>
        <div id="spiral-amp-wrap">
          <span style="font-size:11px;color:var(--fg2);min-width:60px">Amplitude</span>
          <div id="spiral-amp-track"><div id="spiral-amp-bar"></div></div>
          <span id="spiral-amp-pct" style="font-size:11px;color:var(--accent);min-width:35px;text-align:right">100%</span>
        </div>
      </div>

      <div id="alpha-row">
        <button id="alpha-toggle" class="active" onclick="toggleAlpha()">
          &#945;  Alpha oscillation: ON
        </button>
      </div>
    </div><!-- end #controls-panel -->

    <!-- Touch tab -->
    <div id="touch-panel">
      <div id="tc-body">
        <div id="tc-main">
          <canvas id="touch-canvas"></canvas>
        </div>
        <div id="cat-panel">
          <button class="cat-btn" data-cat="hunk" onclick="setCategory('hunk')">HUNK</button>
          <button class="cat-btn" data-cat="toon" onclick="setCategory('toon')">TOON</button>
          <button class="cat-btn" data-cat="furry" onclick="setCategory('furry')">FURRY</button>
        </div>
      </div>
      <!-- Base Power gradient slider -->
      <div style="flex-shrink:0;padding:2px 0 0">
        <div style="font-size:10px;color:var(--fg2);margin-bottom:4px;text-align:center;letter-spacing:.05em">BASE POWER</div>
        <div style="position:relative;height:34px;border-radius:6px;overflow:hidden;touch-action:none">
          <div style="position:absolute;inset:0;background:linear-gradient(to right,#44cc70,#ffcc14,#ff8800,#ff4444)"></div>
          <input type="range" id="tc-power-slider" min="0" max="100" value="50"
                 oninput="_tcPowerSlider=this.value/100;_tcUpdatePowerThumb();tcDraw()"
                 style="position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer;margin:0;padding:0;-webkit-appearance:none;appearance:none;touch-action:none">
          <div id="tc-power-thumb" style="position:absolute;top:4px;bottom:4px;width:5px;left:50%;transform:translateX(-50%);background:rgba(255,255,255,0.95);border-radius:3px;pointer-events:none;box-shadow:0 0 5px rgba(0,0,0,0.8)"></div>
        </div>
      </div>
      <div id="tc-status" style="color:var(--fg2);font-size:11px;font-family:monospace;min-height:14px;flex-shrink:0">Tap or drag &middot; Y = position &middot; X = intensity</div>
    </div><!-- end #touch-panel -->

  </div><!-- end #tab-area -->

</div><!-- end #page-body -->

<script>
const PATTERNS = ["Hold","Sine","Ramp \u2191","Ramp \u2193","Pulse","Burst","Random","Edge"];
let state = { pattern:"Hold", intensity:0, hz:0.5, depth:1.0,
              betaMode:"sweep", beta:5000, alpha:true };
let spiralTighten = false;

// ── Presets ───────────────────────────────────────────────────────────────────
// ⚠  KEEP IN SYNC with Python PRESETS dict above.
// Slider raw values pre-computed from real Hz:
//   hz-slider (1-100):   hz = round((v/100)^2 * 795 + 5) / 100
//   sweep-hz (1-200):    hz = round((v/200)^2 * 498 + 2) / 100
const JS_PRESETS = {
  "Milking": {
    pattern:      "Hold",
    intensity:    100,   // slider 0-100 → 100%
    hzSlider:     1,     // slider 1  → 0.05 Hz pattern speed
    depth:        12,    // 12%
    alpha:        false,
    betaMode:     "sweep",
    sweepHzSlider: 51,   // slider 51 → 0.34 Hz  (round((51/200)^2*498+2)/100 = 0.34)
    sweepCentre:  7700,  // betaLabel → "54 →"
    sweepWidth:   2450,  // 49%
    sweepSkew:    17,    // B +17%
    rampTarget:   100,
    rampDuration: 60,
  },
};

// Build preset row
const presetRow = document.getElementById("preset-row");
Object.keys(JS_PRESETS).forEach(name => {
  const b = document.createElement("button");
  b.className = "preset-btn";
  b.textContent = "\u2605 " + name;
  b.onclick = () => loadPreset(name);
  presetRow.appendChild(b);
});

async function loadPreset(name) {
  const p = JS_PRESETS[name];
  if (!p) return;
  // 1. Tell server to apply preset atomically
  await sendCmd({ load_preset: name });

  // 2. Sync all driver UI controls (no extra commands — server already handled it)
  // Pattern
  state.pattern = p.pattern;
  document.querySelectorAll(".pat-btn").forEach(b =>
    b.classList.toggle("active", b.textContent === p.pattern));

  // Intensity
  document.getElementById("intensity-slider").value = p.intensity;
  document.getElementById("int-val").textContent = p.intensity + "%";
  state.intensity = p.intensity / 100;

  // Speed Hz
  document.getElementById("hz-slider").value = p.hzSlider;
  const hzVal = Math.round(Math.pow(p.hzSlider/100, 2) * 795 + 5) / 100;
  document.getElementById("hz-val").textContent = hzVal.toFixed(2) + " Hz";
  state.hz = hzVal;

  // Depth
  document.getElementById("depth-slider").value = p.depth;
  document.getElementById("depth-val").textContent = p.depth + "%";
  state.depth = p.depth / 100;

  // Alpha
  state.alpha = p.alpha;
  const abtn = document.getElementById("alpha-toggle");
  abtn.classList.toggle("active", p.alpha);
  abtn.textContent = "\u03b1  Alpha oscillation: " + (p.alpha ? "ON" : "OFF");

  // Beta mode
  state.betaMode = p.betaMode;
  document.querySelectorAll(".mode-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === p.betaMode));
  document.getElementById("sweep-controls").style.display =
    p.betaMode === "sweep" ? "block" : "none";
  document.getElementById("hold-controls").style.display =
    p.betaMode === "hold"  ? "block" : "none";

  // Sweep Hz
  document.getElementById("sweep-hz").value = p.sweepHzSlider;
  const swHz = Math.round(Math.pow(p.sweepHzSlider/200, 2) * 498 + 2) / 100;
  document.getElementById("sweep-hz-val").textContent = swHz.toFixed(2) + " Hz";

  // Sweep centre
  document.getElementById("sweep-centre").value = p.sweepCentre;
  document.getElementById("sweep-ctr-val").textContent = betaLabel(p.sweepCentre);

  // Sweep width
  document.getElementById("sweep-width").value = p.sweepWidth;
  document.getElementById("sweep-width-val").textContent =
    Math.round(p.sweepWidth / 49.99) + "%";

  // Sweep skew
  document.getElementById("sweep-skew").value = p.sweepSkew;
  document.getElementById("sweep-skew-val").textContent =
    p.sweepSkew === 0 ? "even"
      : p.sweepSkew < 0 ? "A +" + (-p.sweepSkew) + "%"
                        : "B +" + p.sweepSkew + "%";

  // Ramp sliders (pre-fill without starting)
  document.getElementById("ramp-target").value = p.rampTarget;
  document.getElementById("ramp-target-val").textContent = p.rampTarget + "%";
  document.getElementById("ramp-duration").value = p.rampDuration;
  onRampDur(p.rampDuration);
  document.getElementById("ramp-progress-wrap").style.display = "none";
}

// Build pattern buttons
const grid = document.getElementById("pattern-grid");
PATTERNS.forEach(p => {
  const b = document.createElement("button");
  b.className = "pat-btn" + (p === state.pattern ? " active" : "");
  b.textContent = p;
  b.onclick = () => setPattern(p);
  grid.appendChild(b);
});

function setPattern(p) {
  state.pattern = p;
  document.querySelectorAll(".pat-btn").forEach(b =>
    b.classList.toggle("active", b.textContent === p));
  sendCmd({ pattern: p });
}

function onIntensity(v) {
  state.intensity = v / 100;
  document.getElementById("int-val").textContent = v + "%";
  sendCmd({ intensity: state.intensity });
  document.getElementById("ramp-progress-wrap").style.display = "none";
}

function onHz(v) {
  // map 1–100 → 0.05–8 Hz (log curve)
  const hz = Math.round(Math.pow(v / 100, 2) * 795 + 5) / 100;
  state.hz = hz;
  document.getElementById("hz-val").textContent = hz.toFixed(2) + " Hz";
  sendCmd({ hz: hz });
}

function onDepth(v) {
  state.depth = v / 100;
  document.getElementById("depth-val").textContent = v + "%";
  sendCmd({ depth: state.depth });
}

// ── Ramp ─────────────────────────────────────────────────────────────────────
function onRampDur(v) {
  v = parseInt(v);
  document.getElementById("ramp-dur-val").textContent =
    v >= 60 ? (v/60).toFixed(1)+"m" : v+"s";
}

function startRamp() {
  const target   = parseInt(document.getElementById("ramp-target").value) / 100;
  const duration = parseInt(document.getElementById("ramp-duration").value);
  sendCmd({ ramp: { target, duration } });
  document.getElementById("ramp-progress-wrap").style.display = "flex";
}

function stopRamp() {
  sendCmd({ ramp_stop: true });
  document.getElementById("ramp-progress-wrap").style.display = "none";
}

// ── Beta sweep controls ───────────────────────────────────────────────────────
function setBetaMode(btn) {
  document.querySelectorAll(".mode-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const mode = btn.dataset.mode;
  state.betaMode = mode;
  document.getElementById("sweep-controls").style.display   = mode === "sweep"  ? "block" : "none";
  document.getElementById("spiral-controls").style.display  = mode === "spiral" ? "block" : "none";
  document.getElementById("hold-controls").style.display    = mode === "hold"   ? "block" : "none";
  sendCmd({ beta_mode: mode });
}

function betaLabel(v) {
  if (v < 1500) return "\u2190 A";
  if (v > 8500) return "B \u2192";
  if (v > 4500 && v < 5500) return "Centre";
  return v < 5000 ? "\u2190 " + Math.round((5000-v)/50) : Math.round((v-5000)/50) + " \u2192";
}

function onSweepHz(v) {
  const hz = Math.round(Math.pow(v/200, 2) * 498 + 2) / 100;
  document.getElementById("sweep-hz-val").textContent = hz.toFixed(2)+" Hz";
  sendCmd({ beta_sweep: { hz } });
}

function onSweepCentre(v) {
  v = parseInt(v);
  document.getElementById("sweep-ctr-val").textContent = betaLabel(v);
  sendCmd({ beta_sweep: { centre: v } });
}

function onSweepWidth(v) {
  v = parseInt(v);
  document.getElementById("sweep-width-val").textContent = Math.round(v/49.99)+"%";
  sendCmd({ beta_sweep: { width: v } });
}

function onSweepSkew(v) {
  v = parseInt(v);
  const lbl = v === 0 ? "even" : (v < 0 ? "A +" + (-v) + "%" : "B +" + v + "%");
  document.getElementById("sweep-skew-val").textContent = lbl;
  sendCmd({ beta_sweep: { skew: v / 100 } });
}

// ── Spiral controls ───────────────────────────────────────────────────────────
function onSpiralHz(v) {
  const hz = Math.round(Math.pow(v/200, 2) * 498 + 2) / 100;
  document.getElementById("spiral-hz-val").textContent = hz.toFixed(2) + " Hz";
  sendCmd({ spiral: { hz } });
}

function onSpiralRate(v) {
  v = parseInt(v);
  document.getElementById("spiral-rate-val").textContent = v + "%/s";
  sendCmd({ spiral: { tighten_rate: v / 100 } });
}

function toggleSpiralTighten() {
  spiralTighten = !spiralTighten;
  const btn = document.getElementById("spiral-tighten-btn");
  btn.classList.toggle("active", spiralTighten);
  btn.textContent = "Tighten: " + (spiralTighten ? "ON" : "OFF");
  sendCmd({ spiral: { tighten: spiralTighten } });
}

function resetSpiral() {
  sendCmd({ spiral: { reset: true } });
  document.getElementById("spiral-amp-bar").style.width = "100%";
  document.getElementById("spiral-amp-pct").textContent = "100%";
}

function setHoldBeta(btn) {
  document.querySelectorAll(".hold-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const val = parseInt(btn.dataset.beta);
  document.getElementById("hold-pos").value = val;
  document.getElementById("hold-pos-val").textContent = betaLabel(val);
  sendCmd({ beta: val });
}

function onHoldPos(v) {
  v = parseInt(v);
  document.getElementById("hold-pos-val").textContent = betaLabel(v);
  document.querySelectorAll(".hold-btn").forEach(b => b.classList.remove("active"));
  sendCmd({ beta: v });
}

function toggleAlpha() {
  state.alpha = !state.alpha;
  const btn = document.getElementById("alpha-toggle");
  btn.classList.toggle("active", state.alpha);
  btn.textContent = "\u03b1  Alpha oscillation: " + (state.alpha ? "ON" : "OFF");
  sendCmd({ alpha: state.alpha });
}

function sendStop() {
  state.intensity = 0;
  document.getElementById("intensity-slider").value = 0;
  document.getElementById("int-val").textContent = "0%";
  sendCmd({ stop: true });
}

let _poppersMode = 'normal';
function _poppersDuration() {
  if (_poppersMode === 'normal')     return 10;
  if (_poppersMode === 'deep_huff')  return 20;
  if (_poppersMode === 'double_hit') return 35;
  return 10;
}
// Keep radio labels styled: selected = white, others = #999
document.querySelectorAll('input[name="poppers-mode"]').forEach(r => {
  r.addEventListener('change', () => {
    _poppersMode = r.value;
    document.querySelectorAll('input[name="poppers-mode"]').forEach(r2 => {
      const lbl = r2.parentElement.querySelector('span');
      if (lbl) lbl.style.color = r2.checked ? '#fff' : '#999';
    });
  });
});

let _bottleTimer = null;
function sendBottle() {
  const dur = _poppersDuration();
  sendCmd({bottle: {mode: _poppersMode, duration: dur}});
  const btn = document.getElementById('bottle-btn');
  btn.classList.add('active');
  if (_bottleTimer) clearTimeout(_bottleTimer);
  _bottleTimer = setTimeout(() => btn.classList.remove('active'), dur * 1000);
}

async function sendCmd(cmd) {
  try {
    const r = await fetch("/command", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(cmd)
    });
    if (!r.ok) throw new Error(r.status);
    setConnected(true);
  } catch { setConnected(false); }
}

function setConnected(ok) {
  document.getElementById("dot").style.background = ok ? "var(--ok)" : "var(--err)";
  document.getElementById("status-text").textContent =
    ok ? "Connected to rider" : "Connection lost \u2014 retrying\u2026";
}

// ── Visualization ─────────────────────────────────────────────────────────────
const HIST = 40;
let volHist   = new Array(HIST).fill(0);
let alphaHist = new Array(HIST).fill(0);

function drawWaveform(vol, alpha) {
  volHist.push(vol);   if (volHist.length   > HIST) volHist.shift();
  alphaHist.push(alpha); if (alphaHist.length > HIST) alphaHist.shift();
  const cvs = document.getElementById("waveform");
  const W = cvs.parentElement ? cvs.parentElement.clientWidth - 126 : 180;
  if (W < 20) return;
  cvs.width = W;
  const H = cvs.height;
  const ctx = cvs.getContext("2d");
  ctx.fillStyle = "#1a1a1a"; ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = "#2a2a2a"; ctx.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach(y => {
    ctx.beginPath(); ctx.moveTo(0, H*y); ctx.lineTo(W, H*y); ctx.stroke();
  });
  function drawLine(hist, color, fill, lw) {
    if (fill) {
      ctx.fillStyle = fill; ctx.beginPath(); ctx.moveTo(0, H);
      hist.forEach((v, i) => ctx.lineTo((i/(HIST-1))*W, H - v*(H-4)));
      ctx.lineTo(W, H); ctx.closePath(); ctx.fill();
    }
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
    hist.forEach((v, i) => {
      const x=(i/(HIST-1))*W, y=H-v*(H-4);
      i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
    }); ctx.stroke();
  }
  drawLine(alphaHist, "#4caf50", "rgba(76,175,80,0.12)", 1);
  drawLine(volHist,   "#5fa3ff", "rgba(95,163,255,0.15)", 2);
  ctx.fillStyle = "#5fa3ff"; ctx.beginPath();
  ctx.arc(W-3, H - volHist[HIST-1]*(H-4), 3, 0, Math.PI*2); ctx.fill();
  ctx.fillStyle="#3a5a7a"; ctx.font="9px Arial"; ctx.textAlign="left";
  ctx.fillText("Vol",2,10); ctx.fillStyle="#2a4a2a"; ctx.fillText("\u03b1",2,H-3);
}

function drawTriangle(vol, beta, alpha) {
  const cvs = document.getElementById("tri-canvas");
  const W = cvs.width, H = cvs.height;
  const ctx = cvs.getContext("2d");
  ctx.fillStyle = "#1a1a1a"; ctx.fillRect(0, 0, W, H);
  const pad = 16;
  const vx = [W/2, pad, W-pad], vy = [pad, H-pad-10, H-pad-10];
  // Interior fill when active
  if (vol > 0.02) {
    const g = ctx.createRadialGradient(W/2,H*0.62,0, W/2,H*0.62, W*0.5);
    g.addColorStop(0, `rgba(95,163,255,${vol*0.2})`);
    g.addColorStop(1, "rgba(95,163,255,0)");
    ctx.fillStyle = g; ctx.beginPath();
    ctx.moveTo(vx[0],vy[0]); ctx.lineTo(vx[1],vy[1]); ctx.lineTo(vx[2],vy[2]);
    ctx.closePath(); ctx.fill();
  }
  // Triangle outline
  ctx.strokeStyle = vol > 0.02 ? "#3a4a5a" : "#282828"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(vx[0],vy[0]); ctx.lineTo(vx[1],vy[1]);
  ctx.lineTo(vx[2],vy[2]); ctx.closePath(); ctx.stroke();
  // Labels
  ctx.font="9px Arial"; ctx.textAlign="center"; ctx.fillStyle="#444";
  ctx.fillText("Vol", vx[0], vy[0]-4);
  ctx.fillText("L",   vx[1], vy[1]+11);
  ctx.fillText("R",   vx[2], vy[2]+11);
  // Dot position: beta=horizontal on base, vol lifts toward apex
  const bf    = beta / 9999;
  const baseX = vx[1] + bf * (vx[2] - vx[1]);
  const dotX  = baseX  + vol * (vx[0] - baseX);
  const dotY  = vy[1]  + vol * (vy[0] - vy[1]);
  // Alpha halo
  if (alpha > 0.02) {
    const h = ctx.createRadialGradient(dotX,dotY,0, dotX,dotY, 13*alpha+3);
    h.addColorStop(0, `rgba(76,175,80,${alpha*0.65})`);
    h.addColorStop(1, "rgba(76,175,80,0)");
    ctx.fillStyle = h; ctx.beginPath();
    ctx.arc(dotX, dotY, 13*alpha+3, 0, Math.PI*2); ctx.fill();
  }
  // Main dot
  const r = Math.max(3.5, 4 + vol*5);
  ctx.fillStyle = vol > 0.02 ? "#5fa3ff" : "#2a2a2a";
  ctx.beginPath(); ctx.arc(dotX, dotY, r, 0, Math.PI*2); ctx.fill();
  if (vol > 0.4) {
    ctx.fillStyle = `rgba(255,255,255,${(vol-0.4)*0.75})`;
    ctx.beginPath(); ctx.arc(dotX, dotY, r*0.4, 0, Math.PI*2); ctx.fill();
  }
}

// ── Poll state ────────────────────────────────────────────────────────────────
async function pollState() {
  try {
    const r = await fetch("/state");
    const d = await r.json();
    setConnected(true);
    drawWaveform(d.vol, d.alpha);
    drawTriangle(d.vol, d.beta, d.alpha);
    // Beta position dot
    document.getElementById("beta-dot").style.left = ((d.beta/9999)*100)+"%";
    // Ramp progress
    if (d.ramp_active) {
      document.getElementById("intensity-slider").value = Math.round(d.intensity*100);
      document.getElementById("int-val").textContent = Math.round(d.intensity*100)+"%";
      document.getElementById("ramp-progress-wrap").style.display = "flex";
      document.getElementById("ramp-bar").style.width = (d.ramp_progress*100)+"%";
      document.getElementById("ramp-pct").textContent =
        Math.round(d.ramp_progress*100)+"% \u2192 "+Math.round(d.ramp_target*100)+"%";
    } else {
      if (document.getElementById("ramp-progress-wrap").style.display === "flex")
        document.getElementById("ramp-progress-wrap").style.display = "none";
    }
    // Sync beta mode buttons if server state differs
    if (d.beta_mode && d.beta_mode !== state.betaMode) {
      state.betaMode = d.beta_mode;
      document.querySelectorAll(".mode-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.mode === d.beta_mode));
      document.getElementById("sweep-controls").style.display   =
        d.beta_mode === "sweep"  ? "block" : "none";
      document.getElementById("spiral-controls").style.display  =
        d.beta_mode === "spiral" ? "block" : "none";
      document.getElementById("hold-controls").style.display    =
        d.beta_mode === "hold"   ? "block" : "none";
    }
    // Spiral amplitude bar
    if (d.beta_mode === "spiral" && d.spiral_amp !== undefined) {
      const pct = Math.round(d.spiral_amp * 100);
      document.getElementById("spiral-amp-bar").style.width = pct + "%";
      document.getElementById("spiral-amp-pct").textContent = pct + "%";
    }
    document.getElementById("live").textContent =
      `Vol ${Math.round(d.vol*100)}%  \u03b2 ${d.beta} (${betaLabel(d.beta)})  \u03b1 ${Math.round(d.alpha*100)}%  ${d.pattern}`;
    if (d.likes && d.likes.length) {
      d.likes.forEach(like => triggerLikeAnimation(like));
    }
  } catch { setConnected(false); }
}

setInterval(pollState, 350);
pollState();

// ── Room code / rider link ─────────────────────────────────────────────────
const _m = window.location.pathname.match(/\/room\/([^/]+)/);
const _ROOM_CODE = _m ? _m[1] : null;

function copyRoomCode(btn) {
  if (!_ROOM_CODE) return;
  navigator.clipboard.writeText(_ROOM_CODE)
    .then(() => { const t = btn.textContent; btn.textContent = '\u2713 Copied!'; setTimeout(() => btn.textContent = t, 1500); })
    .catch(() => {});
}

function copyRiderLink(btn) {
  if (!_ROOM_CODE) return;
  const url = location.origin + '/room/' + _ROOM_CODE + '/rider';
  navigator.clipboard.writeText(url)
    .then(() => { const t = btn.textContent; btn.textContent = '\u2713 Copied!'; setTimeout(() => btn.textContent = t, 1500); })
    .catch(() => {});
}

// ── Driver name ────────────────────────────────────────────────────────────
let _driverNameTimer = null;
function setDriverName(val) {
  clearTimeout(_driverNameTimer);
  _driverNameTimer = setTimeout(() => {
    sendCmd({set_driver_name: val.trim()});
    localStorage.setItem('reDriveDriverName', val.trim());
  }, 600);
}
(function initDriverName() {
  const saved = localStorage.getItem('reDriveDriverName') || '';
  if (saved) {
    const inp = document.getElementById('driver-name-input');
    if (inp) inp.value = saved;
    // Send on load so server knows the name
    if (saved) sendCmd({set_driver_name: saved});
  }
})();

// ── Participant avatars ─────────────────────────────────────────────────────
function renderParticipants(data) {
  const col = document.getElementById('rider-cards');
  if (!col) return;
  const parts = (data.participants || []).slice().sort((a, b) => {
    const aHas = a.anatomy && a.anatomy.includes('_uploads') ? 0 : 1;
    const bHas = b.anatomy && b.anatomy.includes('_uploads') ? 0 : 1;
    return aHas - bHas;
  });
  col.innerHTML = parts.map(p => {
    const url = p.anatomy
      ? '/touch_assets/anatomy/' + p.anatomy.split('/').map(encodeURIComponent).join('/')
      : '';
    const bg = url
      ? 'background-image:url(\'' + url + '\');background-size:cover;background-position:top center'
      : 'background:#222';
    return '<div class="rider-card" data-idx="' + p.idx + '" style="' + bg + '">' +
      '<div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.65);' +
      'font-size:8px;color:#ccc;text-align:center;padding:2px;border-radius:0 0 5px 5px;' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + p.name + '</div>' +
      '</div>';
  }).join('');
}

// ── Like animation ──────────────────────────────────────────────────────────
if (!document.getElementById('like-style')) {
  const s = document.createElement('style');
  s.id = 'like-style';
  s.textContent = '@keyframes likeFloat {' +
    '0%   { transform: translateY(0) scale(1);       opacity: 1; }' +
    '60%  { transform: translateY(-80px) scale(1.3); opacity: 1; }' +
    '100% { transform: translateY(-160px) scale(0.8); opacity: 0; }' +
    '}';
  document.head.appendChild(s);
}

function triggerLikeAnimation(like) {
  const col = document.getElementById('rider-cards');
  if (!col) return;
  const cards = col.querySelectorAll('.rider-card');
  let origin = col;
  cards.forEach(c => { if (parseInt(c.dataset.idx) === like.rider_idx) origin = c; });
  const rect = origin.getBoundingClientRect();
  const el = document.createElement('div');
  el.textContent = like.emoji;
  el.style.cssText =
    'position:fixed;' +
    'left:' + (rect.left + rect.width / 2) + 'px;' +
    'top:' + rect.top + 'px;' +
    'font-size:28px;' +
    'pointer-events:none;' +
    'z-index:9999;' +
    'animation:likeFloat 1.8s ease-out forwards;';
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1900);
}

(function initParticipantsPoll() {
  if (!_ROOM_CODE) return;
  async function fetchParticipants() {
    try {
      const d = await (await fetch('/room/' + _ROOM_CODE + '/participants')).json();
      renderParticipants(d);
    } catch(_) {}
  }
  fetchParticipants();
  setInterval(fetchParticipants, 5000);
})();

// ── Tab switching (controls / touch) ─────────────────────────────────────────
let _driverMode = 'controls';
function setTab(tab) {
  _driverMode = tab;
  document.getElementById('controls-panel').style.display = tab === 'controls' ? 'flex' : 'none';
  const tp = document.getElementById('touch-panel');
  tp.style.display = tab === 'touch' ? 'flex' : 'none';
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  if (tab === 'touch') {
    initTouchPanel();
    const _until = Date.now() + 3000;
    (function sync() {
      const w = document.getElementById('tc-main');
      const c = document.getElementById('touch-canvas');
      if (w && w.offsetHeight > 10) { if (!c || c.width !== w.offsetWidth || c.height !== w.offsetHeight) tcDraw(); }
      if (Date.now() < _until) requestAnimationFrame(sync);
    })();
  }
}

// ── Embedded touch panel ─────────────────────────────────────────────────────
const TC_TOOLS = {
  feather: { min:0.08, max:0.55, color:'#88aaff', cursorW:0.88, multiplier:0.35, power:1.5 },
  hand:    { min:0.25, max:0.80, color:'#ffffff', cursorW:0.55, multiplier:0.75, power:1.0 },
  stroker: { min:0.55, max:1.00, color:'#ff8800', cursorW:0.35, multiplier:1.00, power:0.8 },
};
const TC_ELEC_BETA  = { '1':0, '2':2500, '3':7500, '4':9999 };
const TC_ANAT_YF   = { tip:0.0, balls:0.5, anus:1.0 };
const TC_ELEC_COLOR= { '1':'#ff4444', '2':'#4488ff', '3':'#ffcc14', '4':'#44cc70' };

let tcTool        = 'feather';
let tcPointerDown = false;
let tcLastX       = 0.5, tcLastY = 0.5;
let tcTrail       = [];
let tcPanelInited = false;
let tcAnatVariants= [];
let tcCurrentAnat = localStorage.getItem('anatId') || 'default';
let tcCustomImg   = null;
let tcServerInt   = 0.5;
let _tcGesturePath= [], _tcLooping=false, _tcLoopStart=0, _tcLoopDur=0, _tcGestureStart=0;
let _tcPowerSlider = 0.5; // 0=min 1=max, default middle
let _tcParticipants = []; // latest participants list from WS

function tcElecAt() {
  const valid = ['1','2','3','4'];
  const def = { tip:'2', balls:'3', anus:'1' };
  try {
    const stored = JSON.parse(localStorage.getItem('elecAt') || 'null');
    if (!stored || typeof stored !== 'object') return def;
    // Remap any non-numeric values (e.g. 'Red','Blue') back to defaults
    const remapped = {};
    const defVals = Object.values(def);
    let di = 0;
    for (const [k, v] of Object.entries(stored)) {
      remapped[k] = valid.includes(String(v)) ? String(v) : (defVals[di++] || '1');
    }
    return remapped;
  } catch(e) { return def; }
}

function tcRgba(hex, a) {
  const n = parseInt(hex.replace('#',''), 16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;
}

function tcBuildGrad(ctx, W, H) {
  const base = { '1':'255,68,68', '2':'68,136,255', '3':'255,204,20', '4':'68,204,112' };
  const ea = tcElecAt();
  const stops = Object.entries(ea)
    .map(([anat,elec]) => ({ y:TC_ANAT_YF[anat] ?? null, c:base[elec] || '180,180,180' }))
    .filter(s => s.y !== null && s.y !== undefined)
    .sort((a,b) => a.y - b.y);
  const g = ctx.createLinearGradient(0,0,0,H);
  stops.forEach((s,i) => {
    const op = [0.82,0.60,0.44,0.76][i] || 0.60;
    g.addColorStop(s.y, `rgba(${s.c},${op})`);
  });
  return g;
}

function tcDrawDetailed(ctx, W, H, thumb) {
  const cx=W/2, GLY=0.07, SHT=0.15, SHB=0.44, SCY=0.50, PERY=0.72, ANY=0.88;
  const shr=W*0.130, gr=W*0.195, gtv=H*0.055;
  const slx=W*0.195, sla=W*0.205, slb=H*0.115;
  const ar=Math.min(W*0.095,H*0.046), pr=W*0.062, lw=thumb?0.8:1.5;
  ctx.clearRect(0,0,W,H); ctx.fillStyle='#1a1a1a'; ctx.fillRect(0,0,W,H);
  const grad=tcBuildGrad(ctx,W,H);
  const fill=()=>{ ctx.fillStyle=grad; ctx.fill(); ctx.strokeStyle='#2c3558'; ctx.lineWidth=lw; ctx.stroke(); };
  ctx.beginPath();
  ctx.moveTo(cx-pr,H*PERY);
  ctx.bezierCurveTo(cx-pr*0.7,H*(PERY+ANY)/2,cx-ar*0.85,H*ANY-ar*0.7,cx-ar*0.85,H*ANY);
  ctx.lineTo(cx+ar*0.85,H*ANY);
  ctx.bezierCurveTo(cx+ar*0.85,H*ANY-ar*0.7,cx+pr*0.7,H*(PERY+ANY)/2,cx+pr,H*PERY);
  ctx.closePath(); fill();
  ctx.beginPath(); ctx.ellipse(cx-slx,H*SCY+slb*0.18,sla*0.82,slb*0.86,0.08,0,Math.PI*2); fill();
  ctx.beginPath(); ctx.ellipse(cx+slx,H*SCY+slb*0.18,sla*0.82,slb*0.86,-0.08,0,Math.PI*2); fill();
  ctx.beginPath(); ctx.moveTo(cx,H*SCY-slb*0.12);
  ctx.bezierCurveTo(cx+slb*0.04,H*SCY,cx-slb*0.04,H*(SCY+0.07),cx,H*(SCY+0.10));
  ctx.strokeStyle='rgba(28,38,88,0.50)'; ctx.lineWidth=thumb?1:2; ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(cx-shr*1.06,H*SHB); ctx.lineTo(cx-shr,H*SHT);
  ctx.lineTo(cx+shr,H*SHT); ctx.lineTo(cx+shr*1.06,H*SHB);
  ctx.closePath(); fill();
  ctx.beginPath(); ctx.ellipse(cx,H*GLY,gr,gtv,0,0,Math.PI*2); fill();
  ctx.beginPath();
  ctx.moveTo(cx-gr*0.87,H*SHT+1);
  ctx.bezierCurveTo(cx-gr*0.20,H*SHT+H*0.013,cx+gr*0.20,H*SHT+H*0.013,cx+gr*0.87,H*SHT+1);
  ctx.strokeStyle='rgba(28,38,88,0.60)'; ctx.lineWidth=thumb?1:2.5; ctx.stroke();
  ctx.beginPath(); ctx.arc(cx,H*ANY,ar,0,Math.PI*2);
  ctx.fillStyle='rgba(45,75,225,0.68)'; ctx.fill();
  ctx.strokeStyle='#223298'; ctx.lineWidth=lw; ctx.stroke();
  ctx.beginPath(); ctx.arc(cx,H*ANY,ar*0.50,0,Math.PI*2);
  ctx.strokeStyle='rgba(90,130,255,0.32)'; ctx.lineWidth=1; ctx.stroke();
  if (!thumb) {
    const tg=ctx.createRadialGradient(cx,0,0,cx,0,H*0.42);
    tg.addColorStop(0,'rgba(255,195,20,0.16)'); tg.addColorStop(1,'transparent');
    ctx.fillStyle=tg; ctx.fillRect(0,0,W,H);
    const bg=ctx.createRadialGradient(cx,H,0,cx,H,H*0.42);
    bg.addColorStop(0,'rgba(50,70,240,0.16)'); bg.addColorStop(1,'transparent');
    ctx.fillStyle=bg; ctx.fillRect(0,0,W,H);
  }
}

function tcDrawSimple(ctx, W, H, thumb) {
  const cx=W/2;
  ctx.clearRect(0,0,W,H); ctx.fillStyle='#1a1a1a'; ctx.fillRect(0,0,W,H);
  const grad=tcBuildGrad(ctx,W,H);
  ctx.beginPath();
  ctx.moveTo(cx,H*0.02);
  ctx.bezierCurveTo(cx+W*0.17,H*0.05,cx+W*0.22,H*0.20,cx+W*0.31,H*0.48);
  ctx.bezierCurveTo(cx+W*0.33,H*0.55,cx+W*0.20,H*0.66,cx+W*0.11,H*0.80);
  ctx.bezierCurveTo(cx+W*0.05,H*0.91,cx+W*0.03,H*0.96,cx,H*0.97);
  ctx.bezierCurveTo(cx-W*0.03,H*0.96,cx-W*0.05,H*0.91,cx-W*0.11,H*0.80);
  ctx.bezierCurveTo(cx-W*0.20,H*0.66,cx-W*0.33,H*0.55,cx-W*0.31,H*0.48);
  ctx.bezierCurveTo(cx-W*0.22,H*0.20,cx-W*0.17,H*0.05,cx,H*0.02);
  ctx.closePath();
  ctx.fillStyle=grad; ctx.fill();
  ctx.strokeStyle='#3a4a90'; ctx.lineWidth=thumb?1:2; ctx.stroke();
  if (!thumb) {
    ctx.strokeStyle='rgba(255,255,255,0.10)'; ctx.lineWidth=1; ctx.setLineDash([3,4]);
    for (const yf of [0.44,0.56]) {
      ctx.beginPath(); ctx.moveTo(W*0.12,H*yf); ctx.lineTo(W*0.88,H*yf); ctx.stroke();
    }
    ctx.setLineDash([]);
    const tg=ctx.createRadialGradient(cx,0,0,cx,0,H*0.50);
    tg.addColorStop(0,'rgba(255,195,20,0.14)'); tg.addColorStop(1,'transparent');
    ctx.fillStyle=tg; ctx.fillRect(0,0,W,H);
    const bg=ctx.createRadialGradient(cx,H,0,cx,H,H*0.50);
    bg.addColorStop(0,'rgba(50,70,240,0.14)'); bg.addColorStop(1,'transparent');
    ctx.fillStyle=bg; ctx.fillRect(0,0,W,H);
  }
}

function tcBetaFromY(y) {
  const ea = tcElecAt();
  const pts = Object.entries(ea)
    .map(([anat,elec]) => ({ y:TC_ANAT_YF[anat] ?? null, beta:TC_ELEC_BETA[elec] ?? 0 }))
    .filter(p => p.y !== null)
    .sort((a,b) => a.y - b.y);
  if (y <= pts[0].y) return pts[0].beta;
  if (y >= pts[pts.length-1].y) return pts[pts.length-1].beta;
  for (let i=0; i<pts.length-1; i++) {
    if (y>=pts[i].y && y<=pts[i+1].y) {
      const f=(y-pts[i].y)/(pts[i+1].y-pts[i].y);
      return Math.round(pts[i].beta+f*(pts[i+1].beta-pts[i].beta));
    }
  }
  return 5000;
}

function tcIntFromX(x) {
  // Sliding 25% window: lo = slider*0.75, hi = lo+0.25
  const lo = _tcPowerSlider * 0.75;
  return lo + 0.25 * Math.max(0, Math.min(1, x));
}

function _tcPowerColor(power, alpha) {
  alpha = (alpha === undefined) ? 1 : alpha;
  const p = Math.max(0, Math.min(1, power));
  const stops = [
    [0,    [68,  204, 112]],
    [0.33, [255, 204, 20 ]],
    [0.67, [255, 136, 0  ]],
    [1.0,  [255, 68,  68 ]],
  ];
  let c = stops[stops.length-1][1];
  for (let i = 0; i < stops.length-1; i++) {
    if (p <= stops[i+1][0]) {
      const f = (p - stops[i][0]) / (stops[i+1][0] - stops[i][0]);
      c = stops[i][1].map((v,j) => Math.round(v + f*(stops[i+1][1][j]-v)));
      break;
    }
  }
  return `rgba(${c[0]},${c[1]},${c[2]},${alpha})`;
}

function _tcUpdatePowerThumb() {
  const thumb = document.getElementById('tc-power-thumb');
  if (thumb) thumb.style.left = (_tcPowerSlider * 100) + '%';
}

function tcDraw() {
  const canvas=document.getElementById('touch-canvas');
  if (!canvas) return;
  const wrap=document.getElementById('tc-main');
  const W=wrap.offsetWidth, H=wrap.offsetHeight;
  if (W<10||H<10) return;
  canvas.width=W; canvas.height=H;
  const ctx=canvas.getContext('2d');
  if (tcCustomImg) {
    ctx.fillStyle='#1a1a1a'; ctx.fillRect(0,0,W,H);
    ctx.drawImage(tcCustomImg,0,0,W,H);
  } else {
    const v=tcAnatVariants.find(a=>a.id===tcCurrentAnat);
    const fn=(v&&v.drawFn)||tcDrawDetailed;
    fn(ctx,W,H,false);
  }
  // Overlay guide — drawn between base image and cursor effects
  if (_tcOverlayOn && tcOverlayImg) {
    ctx.globalAlpha = 0.28;
    ctx.drawImage(tcOverlayImg, 0, 0, W, H);
    ctx.globalAlpha = 1.0;
  }
  // Power window tint — subtle gradient showing current lo→hi range
  const _lo=_tcPowerSlider*0.75, _hi=_lo+0.25;
  const _tint=ctx.createLinearGradient(0,0,W,0);
  _tint.addColorStop(0,_tcPowerColor(_lo,0.06));
  _tint.addColorStop(1,_tcPowerColor(_hi,0.06));
  ctx.fillStyle=_tint; ctx.fillRect(0,0,W,H);
  // Trail — power-colored fading dots
  const now=Date.now(), FADE=1800;
  for (const p of tcTrail) {
    const age=now-p.t;
    if (age>FADE) continue;
    const f=1-age/FADE, r=3+f*7;
    ctx.beginPath(); ctx.arc(p.x*W,p.y*H,r,0,Math.PI*2);
    ctx.fillStyle=_tcPowerColor(p.p!=null?p.p:tcIntFromX(p.x), f*f*0.55); ctx.fill();
  }
  if (tcTrail.length>0) {
    const head=tcTrail[tcTrail.length-1];
    const hp=head.p!=null?head.p:tcIntFromX(head.x);
    ctx.beginPath(); ctx.arc(head.x*W,head.y*H,5,0,Math.PI*2);
    ctx.fillStyle=_tcPowerColor(hp,0.90); ctx.fill();
  }
  // Cursor — power-aware size, color, softness
  if (tcPointerDown || _tcLooping) {
    const power=tcIntFromX(tcLastX);
    const curX=tcLastX*W, curY=tcLastY*H;
    const S=Math.min(W,H);
    // Size: scales from ~6% to ~16% of shortest canvas dimension
    const dotR=S*(0.06+power*0.10);
    // Glow softness: large soft at low power, tight hard at high power
    const glowR=dotR*(2.8-power*1.5);
    const glow=ctx.createRadialGradient(curX,curY,0,curX,curY,glowR);
    glow.addColorStop(0,_tcPowerColor(power,0.35));
    glow.addColorStop(0.55,_tcPowerColor(power,0.12));
    glow.addColorStop(1,_tcPowerColor(power,0));
    ctx.fillStyle=glow; ctx.beginPath(); ctx.arc(curX,curY,glowR,0,Math.PI*2); ctx.fill();
    if (_tcCursorMode==='grid') {
      // Crosshair: full-width H line + full-height V line, thin stroke that thickens with power
      ctx.strokeStyle=_tcPowerColor(power,0.55+power*0.40);
      ctx.lineWidth=1.5+power*4; ctx.lineCap='butt';
      ctx.beginPath(); ctx.moveTo(0,curY); ctx.lineTo(W,curY); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(curX,0); ctx.lineTo(curX,H); ctx.stroke();
    } else {
      // Core dot
      ctx.beginPath(); ctx.arc(curX,curY,dotR,0,Math.PI*2);
      ctx.fillStyle=_tcPowerColor(power,0.55+power*0.40); ctx.fill();
      // Ring — thicker/harder at high power
      ctx.beginPath(); ctx.arc(curX,curY,dotR,0,Math.PI*2);
      ctx.strokeStyle=_tcPowerColor(power,0.85);
      ctx.lineWidth=1+power*2.5; ctx.stroke();
    }
    // % label (same for both modes) — floats above cursor point
    const pct=Math.round(power*100)+'%';
    ctx.fillStyle=_tcPowerColor(power,0.95);
    ctx.font=`bold ${11+Math.round(power*5)}px Arial`;
    ctx.textAlign='center'; ctx.textBaseline='bottom';
    ctx.fillText(pct,curX,curY-dotR-4);
    ctx.textBaseline='alphabetic';
  }
}

function tcGetPos(e, canvas) {
  const rect=canvas.getBoundingClientRect(), src=e.touches?e.touches[0]:e;
  return {
    x:Math.max(0,Math.min(1,(src.clientX-rect.left)/rect.width)),
    y:Math.max(0,Math.min(1,(src.clientY-rect.top)/rect.height)),
  };
}

function tcOnDown(e) {
  e.preventDefault();
  tcPointerDown=true;
  _tcGesturePath=[]; _tcGestureStart=performance.now(); tcSetLooping(false);
  const canvas=document.getElementById('touch-canvas');
  const pos=tcGetPos(e,canvas);
  tcLastX=pos.x; tcLastY=pos.y;
  tcTrail=[{x:pos.x,y:pos.y,p:tcIntFromX(pos.x),t:Date.now()}];
  _tcGesturePath.push({t:0, x:pos.x, y:pos.y});
  sendCmd({beta_mode:'hold',beta:tcBetaFromY(pos.y),intensity:tcIntFromX(pos.x)});
  tcDraw();
}
function tcOnMove(e) {
  if (!tcPointerDown) return; e.preventDefault();
  const canvas=document.getElementById('touch-canvas');
  const pos=tcGetPos(e,canvas);
  tcLastX=pos.x; tcLastY=pos.y;
  tcTrail.push({x:pos.x,y:pos.y,p:tcIntFromX(pos.x),t:Date.now()});
  if (tcTrail.length>60) tcTrail.shift();
  _tcGesturePath.push({t:performance.now()-_tcGestureStart, x:pos.x, y:pos.y});
  sendCmd({beta:tcBetaFromY(pos.y),intensity:tcIntFromX(pos.x)});
  tcDraw();
}
function tcOnUp() {
  if (!tcPointerDown) return;
  tcPointerDown=false;
  const dur=(performance.now()-_tcGestureStart)/1000;
  if (dur>=0.5 && _tcGesturePath.length>=6) {
    _tcLoopStart=performance.now(); _tcLoopDur=dur*1000;
    tcSetLooping(true);
  }
  tcDraw();
}
function tcSetLooping(on) {
  _tcLooping=on;
  const m=document.getElementById('tc-main');
  if (m) m.classList.toggle('looping',on);
}
function _tcPathAt(t) {
  const path=_tcGesturePath;
  if (!path.length) return null;
  if (t<=path[0].t) return path[0];
  if (t>=path[path.length-1].t) return path[path.length-1];
  for (let i=0;i<path.length-1;i++) {
    if (t>=path[i].t&&t<=path[i+1].t) {
      const f=(t-path[i].t)/(path[i+1].t-path[i].t);
      return {x:path[i].x+f*(path[i+1].x-path[i].x), y:path[i].y+f*(path[i+1].y-path[i].y)};
    }
  }
  return path[path.length-1];
}
function tcStop() { sendCmd({stop:true}); tcSetLooping(false); }

function tcSelectTool(btn) {
  document.querySelectorAll('.tc-tool-btn').forEach(b=>{
    b.style.background='var(--bg3)'; b.style.borderColor='var(--border)'; b.style.color='var(--fg2)';
  });
  tcTool=btn.dataset.tool;
  const t=TC_TOOLS[tcTool];
  btn.style.background=tcRgba(t.color,0.08); btn.style.borderColor=t.color; btn.style.color=t.color;
  tcDraw();
}

function _tcPickerAddItem(v, isCustom) {
  const el=document.getElementById('tc-picker'); if (!el) return;
  const active = v.id===tcCurrentAnat;
  const wrap=document.createElement('div');
  wrap.dataset.anatId = v.id;
  wrap.style.cssText='width:48px;height:64px;border-radius:6px;cursor:pointer;' +
    'border:2px solid '+(active?'var(--accent)':'var(--border)')+';' +
    'flex-shrink:0;overflow:hidden;background:var(--bg3);position:relative;touch-action:manipulation';
  if (isCustom) {
    // Gold star badge for custom/rider images
    const badge=document.createElement('div');
    badge.style.cssText='position:absolute;top:2px;right:2px;font-size:9px;z-index:2;line-height:1';
    badge.textContent='★'; wrap.appendChild(badge);
  }
  if (v.type==='canvas') {
    const tc=document.createElement('canvas'); tc.width=48; tc.height=64;
    v.drawFn(tc.getContext('2d'),48,64,true);
    wrap.appendChild(tc);
  } else {
    const img=document.createElement('img'); img.src=v.src;
    img.style='width:100%;height:100%;display:block;object-fit:cover;object-position:top'; wrap.appendChild(img);
  }
  const lbl=document.createElement('div');
  lbl.style.cssText='position:absolute;bottom:0;left:0;right:0;font-size:8px;text-align:center;'+
    'background:rgba(0,0,0,0.60);padding:2px 0;color:var(--fg2);pointer-events:none;' +
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
  lbl.textContent=v.label; wrap.appendChild(lbl);
  wrap.addEventListener('click',()=>{
    tcCurrentAnat=v.id; localStorage.setItem('anatId',v.id);
    if (v.type==='canvas') {
      tcCustomImg=null; tcDraw();
    } else {
      const im2=new Image();
      im2.onload=()=>{tcCustomImg=im2; tcDraw();};
      im2.onerror=()=>{tcCustomImg=null; tcDraw();};
      im2.src=v.src;
    }
    document.querySelectorAll('#tc-picker [data-anat-id]').forEach(w=>{
      w.style.borderColor=(w.dataset.anatId===v.id)?'var(--accent)':'var(--border)';
    });
  });
  el.appendChild(wrap);
}

function tcBuildPicker() {
  const el=document.getElementById('tc-picker'); if (!el) return;
  el.innerHTML='';
  tcAnatVariants=[];

  // Try room anatomy API first (includes custom rider uploads)
  const m=window.location.pathname.match(/\/room\/([^/]+)/);
  const roomCode=m?m[1]:null;
  const apiUrl=roomCode?'/room/'+roomCode+'/anatomies':null;

  const finish=(customFiles, builtinFiles)=>{
    // 1. Custom/rider uploads — top of picker, gold star badge
    const customItems=[];
    for (const f of (customFiles||[])) {
      const id=f, label=f.split('/').pop().replace(/\.[^.]+$/,'');
      const src='/touch_assets/anatomy/'+f.split('/').map(encodeURIComponent).join('/');
      customItems.push({id,label,type:'png',src});
    }
    // 1. Custom / rider uploads — ★ badge, top of list
    for (const v of customItems) {
      tcAnatVariants.push(v);
      _tcPickerAddItem(v, true);
    }
    // 2. Standard server PNGs (hunk1.png, hunk2.png, etc.) — room's built-in images
    for (const f of (builtinFiles||[])) {
      const id=f, label=f.replace(/\.[^.]+$/,'');
      const src='/touch_assets/anatomy/'+encodeURIComponent(f);
      const v={id,label,type:'png',src};
      tcAnatVariants.push(v); _tcPickerAddItem(v,false);
    }
    // 3. Canvas fallbacks — always available, no server needed
    const builtins=[
      {id:'default',label:'Default',type:'canvas',drawFn:tcDrawDetailed},
      {id:'simple', label:'Simple', type:'canvas',drawFn:tcDrawSimple},
    ];
    for (const v of builtins) { tcAnatVariants.push(v); _tcPickerAddItem(v,false); }
    // Auto-select first custom if no saved preference or saved is default
    if (customItems.length && (tcCurrentAnat==='default'||tcCurrentAnat==='simple'||!tcCurrentAnat)) {
      const first=customItems[0];
      tcCurrentAnat=first.id; localStorage.setItem('anatId',first.id);
      const im=new Image();
      im.onload=()=>{tcCustomImg=im; tcDraw();};
      im.src=first.src;
      document.querySelectorAll('#tc-picker [data-anat-id]').forEach(w=>{
        w.style.borderColor=(w.dataset.anatId===first.id)?'var(--accent)':'var(--border)';
      });
    }
    // Apply rider names to picker labels
    _tcRefreshPickerNames();
  };

  if (apiUrl) {
    fetch(apiUrl).then(r=>r.ok?r.json():null).then(data=>{
      // API returns {custom: [...], standard: [...]}
      // standard = files in touch_assets/anatomy/ (hunk1.png etc.)
      finish(data?data.custom:[], data?data.standard:[]);
    }).catch(()=>finish([],[]));
  } else {
    fetch('/touch_assets/list?type=anatomy').then(r=>r.ok?r.json():null).then(files=>{
      finish([], files||[]);
    }).catch(()=>finish([],[]));
  }
}

function _tcRefreshPickerNames() {
  // Label custom picker items with rider names where anatomy filename matches
  if (!_tcParticipants.length) return;
  const el = document.getElementById('tc-picker'); if (!el) return;
  el.querySelectorAll('[data-anat-id]').forEach(wrap => {
    const aid = wrap.dataset.anatId;
    const p = _tcParticipants.find(x => x.anatomy && (x.anatomy === aid || x.anatomy.endsWith('/'+aid) || aid.endsWith('/'+x.anatomy)));
    if (!p) return;
    const lbl = wrap.querySelector('div:last-child');
    if (lbl) lbl.textContent = p.name || lbl.textContent;
    // Add a small rider icon to distinguish
    let badge = wrap.querySelector('.rider-badge');
    if (!badge) {
      badge = document.createElement('div');
      badge.className = 'rider-badge';
      badge.style.cssText = 'position:absolute;top:2px;left:2px;font-size:8px;z-index:3;line-height:1;background:rgba(0,0,0,0.6);border-radius:2px;padding:1px 2px;color:#5fa3ff';
      badge.textContent = '👤';
      wrap.appendChild(badge);
    }
  });
}

function initTouchPanel() {
  if (tcPanelInited) { return; }
  tcPanelInited = true;
  const canvas=document.getElementById('touch-canvas');
  const wrap=document.getElementById('tc-main');
  canvas.addEventListener('mousedown',  tcOnDown, {passive:false});
  canvas.addEventListener('touchstart', tcOnDown, {passive:false});
  canvas.addEventListener('mousemove',  tcOnMove, {passive:false});
  canvas.addEventListener('touchmove',  tcOnMove, {passive:false});
  document.addEventListener('mouseup',     tcOnUp);
  document.addEventListener('touchend',    tcOnUp);
  document.addEventListener('touchcancel', tcOnUp);
  const tcRO=new ResizeObserver(entries=>{
    for (const e of entries) {
      if (e.contentRect.width>10&&e.contentRect.height>10) requestAnimationFrame(tcDraw);
    }
  });
  tcRO.observe(wrap);
  // (tcBuildPicker not called — category buttons handle image selection)
  // Apply saved anatomy if it's a PNG
  if (tcCurrentAnat!=='default'&&tcCurrentAnat!=='simple') {
    const img=new Image();
    img.onload=()=>{tcCustomImg=img; tcDraw();};
    img.onerror=()=>{tcCustomImg=null; tcDraw();};
    img.src='/touch_assets/anatomy/'+encodeURIComponent(tcCurrentAnat);
  }
  // Trail fade + gesture loop replay
  (function tcTrailTick() {
    if (_driverMode==='touch') {
      const now=Date.now();
      if (_tcLooping && _tcGesturePath.length>1 && _tcLoopDur>0) {
        const elapsed=(performance.now()-_tcLoopStart)%_tcLoopDur;
        const pos=_tcPathAt(elapsed);
        if (pos) {
          tcLastX=pos.x; tcLastY=pos.y;
          tcTrail.push({x:pos.x,y:pos.y,t:now});
          if (tcTrail.length>80) tcTrail.shift();
        }
        tcDraw();
      } else {
        tcTrail=tcTrail.filter(p=>now-p.t<1800);
        if (tcTrail.length||tcPointerDown) tcDraw();
      }
    }
    requestAnimationFrame(tcTrailTick);
  })();
  // Poll server intensity for tool scaling
  setInterval(async()=>{
    try {
      const d=await(await fetch('/state')).json();
      if (d.intensity!=null) tcServerInt=d.intensity;
    } catch(_) {}
  }, 1500);
}

// ── Cursor mode ───────────────────────────────────────────────────────────────
let _tcCursorMode = localStorage.getItem('reDriveCursor') || 'dot'; // 'dot' | 'grid'

function toggleCursor(btn) {
  _tcCursorMode = _tcCursorMode === 'dot' ? 'grid' : 'dot';
  localStorage.setItem('reDriveCursor', _tcCursorMode);
  btn.textContent = _tcCursorMode === 'dot' ? 'DOT' : 'GRID';
  btn.title = 'Cursor: ' + _tcCursorMode;
  btn.classList.toggle('active', _tcCursorMode === 'grid');
  if (_driverMode === 'touch') tcDraw();
}

(function initCursorBtn() {
  const btn = document.getElementById('cursor-btn');
  if (!btn) return;
  btn.textContent = _tcCursorMode === 'dot' ? 'DOT' : 'GRID';
  btn.title = 'Cursor: ' + _tcCursorMode;
  btn.classList.toggle('active', _tcCursorMode === 'grid');
})();

// ── Overlay guide ─────────────────────────────────────────────────────────────
let tcOverlayImg = null;
let _tcOverlayOn = localStorage.getItem('reDriveOverlay') !== 'false'; // default true

(function loadOverlayImg() {
  const img = new Image();
  img.onload = () => {
    tcOverlayImg = img;
    if (_tcOverlayOn && _driverMode === 'touch') tcDraw();
  };
  img.src = '/touch_assets/anatomy/anatomyexampleOVERLAY.png';
})();

function toggleOverlay(btn) {
  _tcOverlayOn = !_tcOverlayOn;
  localStorage.setItem('reDriveOverlay', String(_tcOverlayOn));
  btn.innerHTML = 'GUIDE<br>' + (_tcOverlayOn ? 'ON' : 'OFF');
  btn.title = _tcOverlayOn ? 'Overlay guide: ON' : 'Overlay guide: OFF';
  btn.classList.toggle('active', _tcOverlayOn);
  if (_driverMode === 'touch') tcDraw();
}

// Init overlay button state from localStorage
(function initOverlayBtn() {
  const btn = document.getElementById('overlay-btn');
  if (!btn) return;
  btn.classList.toggle('active', _tcOverlayOn);
  btn.innerHTML = 'GUIDE<br>' + (_tcOverlayOn ? 'ON' : 'OFF');
  btn.title = _tcOverlayOn ? 'Overlay guide: ON' : 'Overlay guide: OFF';
})();

// ── Category cycling ──────────────────────────────────────────────────────────
let _tcCat = null;
let _tcCatImages = [];
let _tcCatIdx = 0;
let _tcCatTimer = null;
let _tcStandardImages = [];

(async function loadStdImages() {
  try {
    const d = await (await fetch('/touch_assets/list?type=anatomy')).json();
    _tcStandardImages = (d || []).filter(f => /^(hunk|toon|furry)/i.test(f));
    // Restore saved category selection
    const savedCat = localStorage.getItem('reDriveCat');
    if (savedCat) setCategory(savedCat);
  } catch(_) {}
})();

function setCategory(cat) {
  _tcCat = cat;
  localStorage.setItem('reDriveCat', cat);
  clearInterval(_tcCatTimer);
  const imgs = _tcStandardImages.filter(f => f.toLowerCase().startsWith(cat));
  if (!imgs.length) return;
  _tcCatImages = imgs;
  _tcCatIdx = 0;
  _applyImage();
  _tcCatTimer = setInterval(() => {
    _tcCatIdx = (_tcCatIdx + 1) % _tcCatImages.length;
    _applyImage();
  }, 600000);
  document.querySelectorAll('.cat-btn').forEach(b => b.classList.toggle('active', b.dataset.cat === cat));
}

function _applyImage() {
  // Prefer rider's custom upload
  const custom = _tcParticipants.find(p => p.anatomy && p.anatomy.includes('_uploads'));
  const src = custom
    ? '/touch_assets/anatomy/' + custom.anatomy.split('/').map(encodeURIComponent).join('/')
    : (_tcCatImages.length ? '/touch_assets/anatomy/' + encodeURIComponent(_tcCatImages[_tcCatIdx]) : null);
  if (!src) return;
  const img = new Image();
  img.onload = () => { tcCustomImg = img; tcDraw(); };
  img.src = src;
}

// ── Driver WebSocket — receive participants_update ────────────────────────────
(function connectDriverWS() {
  if (typeof DRIVER_KEY === 'undefined' || typeof ROOM_CODE === 'undefined') return;
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = wsProto + '//' + location.host + '/room/' + ROOM_CODE + '/driver-ws?key=' + encodeURIComponent(DRIVER_KEY);
  let _driverWs = null;
  function connect() {
    try {
      const ws = new WebSocket(wsUrl);
      _driverWs = ws;
      ws.onmessage = ev => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'participants_update') {
            _tcParticipants = msg.participants || [];
            renderParticipants({participants: _tcParticipants});
            if (_driverMode === 'touch') { _tcRefreshPickerNames(); _applyImage(); }
          }
        } catch(_) {}
      };
      ws.onclose = () => { _driverWs = null; setTimeout(connect, 5000); };
      ws.onerror = () => { try { ws.close(); } catch(_) {} };
    } catch(_) { setTimeout(connect, 5000); }
  }
  connect();
})();
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


# ── Touch driver page ─────────────────────────────────────────────────────────

TOUCH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ReDrive &middot; Rider</title>
<style>
  :root {
    --bg:#0a0a0a; --bg2:#111111; --bg3:#1a1a1a;
    --glass:rgba(20,20,35,0.75);
    --border:rgba(95,163,255,0.20);
    --accent:#5fa3ff; --accent-glow:#5fa3ff44;
    --success:#4ade80; --warn:#fbbf24;
    --text:#f1f1f1; --text2:#a3a3a3;
    --err:#f43f5e; --ok:#4ade80;
    --radius:18px;
  }
  *,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
  html { min-height:100%; }
  body {
    background:var(--bg);
    color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:15px;
    padding:12px;
    padding-top:calc(12px + env(safe-area-inset-top));
    padding-bottom:calc(12px + env(safe-area-inset-bottom));
    max-width:520px; margin:0 auto;
    min-height:100dvh; overflow-y:auto;
    display:flex; flex-direction:column; gap:16px;
    background:radial-gradient(circle at 50% 10%,rgba(95,163,255,0.06) 0%,transparent 70%);
    user-select:none; -webkit-user-select:none;
  }
  /* Header */
  #header {
    background:var(--glass); backdrop-filter:blur(20px);
    border:1px solid var(--border); border-radius:var(--radius);
    padding:16px 20px; display:flex; align-items:center; gap:14px;
    box-shadow:0 8px 32px rgba(0,0,0,0.45); flex-shrink:0;
  }
  #conn { display:flex; align-items:center; gap:8px; }
  #cdot { width:13px; height:13px; border-radius:50%; background:var(--err); box-shadow:0 0 14px var(--err); flex-shrink:0; }
  #ctxt { color:var(--text2); font-size:14px; font-weight:500; }
  #rider-name-input {
    flex:1; min-width:0;
    background:rgba(255,255,255,0.06); border:1px solid var(--border);
    border-radius:12px; padding:12px 16px; color:white; font-size:15px;
  }
  #stop-btn {
    background:#e11d48; color:white; border:none; border-radius:12px;
    font-weight:700; padding:13px 24px; font-size:15px; cursor:pointer; flex-shrink:0;
  }
  #stop-btn:active { background:#be123c; }
  /* Driven-by banner */
  #driven-by {
    display:none; text-align:center; font-size:13px; color:var(--text2);
    background:var(--glass); border:1px solid var(--border);
    border-radius:var(--radius); padding:10px; flex-shrink:0;
  }
  #driven-by strong { color:var(--accent); }
  /* Riders panel */
  #riders-panel { display:none; gap:12px; justify-content:center; flex-wrap:wrap; padding:4px 0; flex-shrink:0; }
  .rider-card { display:flex; flex-direction:column; align-items:center; gap:6px; }
  .rider-avatar {
    width:68px; height:92px; border-radius:14px;
    border:2px solid var(--border); background:var(--bg3);
    background-size:cover; background-position:top center;
    box-shadow:0 6px 20px rgba(0,0,0,0.5);
  }
  .rider-name { font-size:10.5px; color:var(--text2); max-width:68px; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  /* Power bar */
  #power-section {
    background:var(--glass); border:1px solid var(--border);
    border-radius:var(--radius); padding:18px 20px; flex-shrink:0;
  }
  #power-label {
    font-size:12px; font-weight:700; letter-spacing:0.8px;
    color:var(--accent); text-transform:uppercase; margin-bottom:10px;
  }
  #power-bar-wrap {
    position:relative; height:48px; border-radius:14px;
    background:var(--bg3); border:1px solid var(--border);
    overflow:hidden; box-shadow:inset 0 2px 8px rgba(0,0,0,0.6);
  }
  #power-bar {
    position:absolute; inset:0; width:0%;
    background:linear-gradient(to right,#4ade80,#fbbf24,#f97316,#e11d48);
    transition:width 0.35s ease;
  }
  #power-pct {
    position:absolute; inset:0;
    display:flex; align-items:center; justify-content:center;
    font-size:19px; font-weight:700; color:white;
    text-shadow:0 2px 8px rgba(0,0,0,0.8);
  }
  @keyframes power-pulse { 0%,100%{opacity:1} 50%{opacity:0.75} }
  #power-bar.live { animation:power-pulse 1.4s ease-in-out infinite; }
  /* Emote grid */
  #emote-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; flex-shrink:0; }
  .emote-btn {
    aspect-ratio:1; background:var(--bg3); border:1px solid var(--border);
    border-radius:16px; font-size:28px;
    display:flex; align-items:center; justify-content:center;
    cursor:pointer; transition:all 0.18s ease;
    box-shadow:0 4px 12px rgba(0,0,0,0.4);
  }
  .emote-btn:active { transform:scale(0.88); background:var(--accent); border-color:var(--accent); }
  /* Footer */
  #footer {
    display:flex; align-items:center; justify-content:space-between;
    margin-top:auto; padding-top:8px; flex-shrink:0;
  }
  #room-code-btn {
    background:var(--bg3); border:1px solid var(--border);
    border-radius:12px; padding:10px 18px;
    color:var(--accent); font-family:monospace; font-size:14px;
    letter-spacing:1px; cursor:pointer;
  }
  /* Bottle overlay */
  #bottle-overlay {
    display:none; position:fixed; inset:0;
    background:rgba(10,10,15,0.96); z-index:9999;
    flex-direction:column; align-items:center; justify-content:center;
    gap:18px; padding:20px; text-align:center;
  }
  #bottle-overlay img { max-width:65vmin; max-height:38vmin; object-fit:contain; border-radius:16px; }
</style>
</head>
<body>

<div id="header">
  <div id="conn"><div id="cdot"></div><span id="ctxt">Connecting&#8230;</span></div>
  <input id="rider-name-input" placeholder="Your name" maxlength="30">
  <button id="stop-btn" onclick="doStop()">&#9632; STOP</button>
</div>

<div id="driven-by">Driven by <strong id="driven-by-name"></strong></div>

<div id="riders-panel"></div>

<div id="power-section">
  <div id="power-label">POWER</div>
  <div id="power-bar-wrap">
    <div id="power-bar"></div>
    <div id="power-pct">&#8212;</div>
  </div>
</div>

<div id="emote-grid">
  <button class="emote-btn" onclick="sendLike('😍')">😍</button>
  <button class="emote-btn" onclick="sendLike('⚡')">⚡</button>
  <button class="emote-btn" onclick="sendLike('💦')">💦</button>
  <button class="emote-btn" onclick="sendLike('🔥')">🔥</button>
  <button class="emote-btn" onclick="sendLike('👋')">👋</button>
  <button class="emote-btn" onclick="sendLike('😈')">😈</button>
</div>

<div id="footer">
  <button id="room-code-btn" onclick="copyRoomCode(this)"></button>
  <label id="upload-avatar-btn" title="Upload your anatomy image (sets your avatar)"
    style="padding:4px 10px;background:none;border:1px solid var(--border);border-radius:4px;
           color:var(--fg2);font-size:11px;cursor:pointer">
    &#128247; My Pic
    <input type="file" id="anat-file-input" accept="image/png,image/jpeg,image/webp"
           style="display:none" onchange="onAnatFileSelected(this)">
  </label>
</div>

<div id="bottle-overlay">
  <img src="/bottle.png" style="max-width:55vmin;max-height:40vmin;object-fit:contain;border-radius:8px">
  <div id="bottle-overlay-heading" style="color:#fff;font-size:1.7rem;font-weight:bold;text-align:center"></div>
  <div id="bottle-overlay-sub"     style="color:#ffcc14;font-size:1.1rem;text-align:center"></div>
  <div id="bottle-overlay-dots"    style="display:flex;justify-content:center;flex-wrap:wrap;gap:4px"></div>
  <div id="bottle-overlay-cd"      style="color:#fff;font-size:1.2rem;font-family:monospace;opacity:0.7"></div>
</div>

<script>
// ROOM_CODE injected by server
const _ROOM_CODE = (typeof ROOM_CODE !== 'undefined') ? ROOM_CODE : null;
const _BASE = _ROOM_CODE ? '/room/' + _ROOM_CODE : '';

// ── Connection status ────────────────────────────────────────────────────────
function setConn(ok) {
  document.getElementById('cdot').style.background = ok ? 'var(--ok)' : 'var(--err)';
  document.getElementById('ctxt').textContent = ok ? 'Connected' : 'Connection lost \u2014 retrying\u2026';
}

// ── Power bar ────────────────────────────────────────────────────────────────
function updatePower(v) {
  v = Math.max(0, Math.min(1, v || 0));
  const bar = document.getElementById('power-bar');
  const pct = document.getElementById('power-pct');
  bar.style.width = Math.round(v * 100) + '%';
  pct.textContent = v > 0.01 ? Math.round(v * 100) + '%' : '\u2014';
  v > 0.01 ? bar.classList.add('live') : bar.classList.remove('live');
}

// ── STOP ─────────────────────────────────────────────────────────────────────
function doStop() {
  fetch(_BASE + '/command', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({stop:true})});
}

// ── Room code copy ────────────────────────────────────────────────────────────
if (_ROOM_CODE) {
  const btn = document.getElementById('room-code-btn');
  btn.textContent = _ROOM_CODE;
}
function copyRoomCode(btn) {
  if (!_ROOM_CODE) return;
  const url = location.origin + '/room/' + _ROOM_CODE + '/rider';
  navigator.clipboard.writeText(url)
    .then(() => { const t = btn.textContent; btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = t, 1500); })
    .catch(() => {});
}

// ── State poll (intensity + bottle) ─────────────────────────────────────────
let _bottleOverlayActive = false;
let _bottleOverlayMode   = 'normal';
let _bottleOverlayIv     = null;
let _bottlePhaseTimer    = null;

setInterval(async () => {
  try {
    const d = await (await fetch(_BASE + '/rider-state')).json();
    setConn(true);
    updatePower(d.intensity ?? 0);
    if (d.bottle_active) showBottleOverlay(d.bottle_mode || 'normal', d.bottle_remaining || 0);
    else if (_bottleOverlayActive) hideBottleOverlay();
  } catch(_) { setConn(false); }
}, 1200);

// ── Bottle overlay ───────────────────────────────────────────────────────────
function showDeepHuffDots(containerEl) {
  containerEl.innerHTML = '';
  const dots = [];
  for (let i = 0; i < 10; i++) {
    const d = document.createElement('span');
    d.textContent = '\u25cf';
    d.style.cssText = 'font-size:20px;margin:0 4px;transition:opacity 0.5s;color:#ffcc14';
    containerEl.appendChild(d); dots.push(d);
  }
  let idx = 0;
  const iv = setInterval(() => {
    if (idx < dots.length) { dots[idx].style.opacity = '0'; idx++; }
    else clearInterval(iv);
  }, 2000);
  return iv;
}
function _clearBottleTimers() {
  if (_bottleOverlayIv)  { clearInterval(_bottleOverlayIv);  _bottleOverlayIv  = null; }
  if (_bottlePhaseTimer) { clearTimeout(_bottlePhaseTimer);  _bottlePhaseTimer = null; }
}
function showBottleOverlay(mode, remaining) {
  const ov      = document.getElementById('bottle-overlay');
  const heading = document.getElementById('bottle-overlay-heading');
  const sub     = document.getElementById('bottle-overlay-sub');
  const dots    = document.getElementById('bottle-overlay-dots');
  const cd      = document.getElementById('bottle-overlay-cd');
  if (!ov) return;
  if (_bottleOverlayActive && _bottleOverlayMode === mode) { cd.textContent = Math.ceil(remaining) + 's'; return; }
  _clearBottleTimers();
  _bottleOverlayActive = true; _bottleOverlayMode = mode;
  dots.innerHTML = ''; ov.style.display = 'flex';
  if (mode === 'normal') {
    heading.textContent = 'Take a huff!'; sub.textContent = ''; cd.textContent = Math.ceil(remaining) + 's';
  } else if (mode === 'deep_huff') {
    heading.textContent = 'DEEP HUFF'; sub.textContent = 'HOLD IT\u2026'; cd.textContent = '';
    _bottleOverlayIv = showDeepHuffDots(dots);
  } else if (mode === 'double_hit') {
    heading.textContent = 'HIT #1 \ud83e\uddf4'; sub.textContent = ''; cd.textContent = '';
    _bottlePhaseTimer = setTimeout(() => {
      ov.style.display = 'none';
      _bottlePhaseTimer = setTimeout(() => {
        ov.style.display = 'flex';
        heading.textContent = 'HIT #2 \ud83e\uddf4'; sub.textContent = ''; cd.textContent = '';
      }, 15000);
    }, 10000);
  }
}
function hideBottleOverlay() {
  _bottleOverlayActive = false; _clearBottleTimers();
  const ov = document.getElementById('bottle-overlay');
  if (ov) ov.style.display = 'none';
}

// ── Rider name ────────────────────────────────────────────────────────────────
let _riderWs = null, _riderNameTimer = null;
(function initRiderName() {
  const inp = document.getElementById('rider-name-input');
  if (!inp) return;
  const saved = localStorage.getItem('reDriveRiderName') || '';
  if (saved) inp.value = saved;
  inp.addEventListener('input', () => {
    const val = inp.value;
    localStorage.setItem('reDriveRiderName', val);
    clearTimeout(_riderNameTimer);
    _riderNameTimer = setTimeout(() => {
      if (_riderWs && _riderWs.readyState === WebSocket.OPEN)
        _riderWs.send(JSON.stringify({type:'set_name', name:val.trim()}));
    }, 600);
  });
})();

// ── Riders panel ──────────────────────────────────────────────────────────────
function renderRidersPanel(data) {
  const panel = document.getElementById('riders-panel');
  if (!panel) return;
  const parts = (data.participants || []);
  if (!parts.length) { panel.style.display = 'none'; return; }
  panel.style.display = 'flex';
  panel.innerHTML = parts.map(p => {
    const url = p.anatomy
      ? '/touch_assets/anatomy/' + p.anatomy.split('/').map(encodeURIComponent).join('/')
      : '';
    const bg = url
      ? `background-image:url('${url}');background-size:cover;background-position:top center`
      : 'background:#222';
    return `<div class="rider-card">
      <div class="rider-avatar" style="${bg};position:relative">
        <div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.65);
          font-size:8px;color:#ccc;text-align:center;padding:2px;
          border-radius:0 0 5px 5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${(p.name||'Rider').replace(/</g,'&lt;')}
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Emotes ────────────────────────────────────────────────────────────────────
function sendLike(emoji) {
  if (_riderWs && _riderWs.readyState === WebSocket.OPEN)
    _riderWs.send(JSON.stringify({type:'like', emoji}));
}

// ── Room WebSocket (participants, driver name) ────────────────────────────────
(function connectRoomWS() {
  if (!_ROOM_CODE) return;
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = wsProto + '//' + location.host + '/room/' + _ROOM_CODE + '/rider';
  function connect() {
    try {
      const ws = new WebSocket(wsUrl);
      _riderWs = ws;
      ws.onopen = () => {
        const name = localStorage.getItem('reDriveRiderName') || '';
        if (name) ws.send(JSON.stringify({type:'set_name', name}));
      };
      ws.onmessage = ev => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'participants_update') {
            const dbDiv  = document.getElementById('driven-by');
            const dbName = document.getElementById('driven-by-name');
            if (dbDiv && dbName) {
              if (msg.driver_name) { dbName.textContent = msg.driver_name; dbDiv.style.display = 'block'; }
              else dbDiv.style.display = 'none';
            }
            renderRidersPanel(msg);
          }
        } catch(_) {}
      };
      ws.onclose = () => { _riderWs = null; setTimeout(connect, 5000); };
      ws.onerror = () => { try { ws.close(); } catch(_) {} };
    } catch(_) { setTimeout(connect, 5000); }
  }
  connect();
})();

// ── Anatomy upload (rider avatar) ─────────────────────────────────────────────
async function onAnatFileSelected(input) {
  if (!input.files || !input.files[0] || !_ROOM_CODE) return;
  const file = input.files[0]; input.value = '';
  const btn = document.getElementById('upload-avatar-btn');
  const orig = btn ? btn.childNodes[0].textContent : '';
  if (btn) btn.childNodes[0].textContent = '⏳ Uploading…';
  try {
    const fd = new FormData(); fd.append('file', file);
    const r = await fetch(_BASE + '/upload_anatomy', {method:'POST', body:fd});
    if (r.ok) {
      // Save for future auto-upload
      const reader = new FileReader();
      reader.onload = e => {
        localStorage.setItem('reDriveAnatomyB64', e.target.result);
        localStorage.setItem('reDriveAnatomyName', file.name);
      };
      reader.readAsDataURL(file);
      if (btn) { btn.childNodes[0].textContent = '✓ Uploaded!'; setTimeout(()=>{ btn.childNodes[0].textContent = orig; }, 2000); }
    } else {
      if (btn) { btn.childNodes[0].textContent = '✗ Failed'; setTimeout(()=>{ btn.childNodes[0].textContent = orig; }, 2000); }
    }
  } catch(_) {
    if (btn) { btn.childNodes[0].textContent = '✗ Error'; setTimeout(()=>{ btn.childNodes[0].textContent = orig; }, 2000); }
  }
}

// Auto-upload saved anatomy when joining a room
(async function autoUploadAnatomy() {
  if (!_ROOM_CODE) return;
  const b64  = localStorage.getItem('reDriveAnatomyB64');
  const name = localStorage.getItem('reDriveAnatomyName') || 'my_pic.png';
  if (!b64) return;
  try {
    // Only upload if room has no custom anatomy yet
    const res = await fetch(_BASE + '/anatomies');
    if (!res.ok) return;
    const data = await res.json();
    if (data.custom && data.custom.length > 0) return;
    const blob = await fetch(b64).then(r => r.blob());
    const fd = new FormData(); fd.append('file', blob, name);
    await fetch(_BASE + '/upload_anatomy', {method:'POST', body:fd});
  } catch(_) {}
})();
</script>
</body>
</html>
"""

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

    def _log(self, msg: str):
        self._log_q.put_nowait(msg)

    # ── ReStim connection ────────────────────────────────────────────────────

    async def _connect(self) -> bool:
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self._cfg.restim_url, heartbeat=30)
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
        except Exception as e:
            self._log(f"Send error: {e}")
            self._ws = None

    # ── HTTP server (driver browser UI) ─────────────────────────────────────

    async def _handle_index(self, _req):
        return web.Response(text=DRIVER_HTML, content_type="text/html")

    async def _handle_touch(self, _req):
        return web.Response(text=TOUCH_HTML, content_type="text/html")

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

    async def _handle_state(self, _req):
        d = {
            "vol":           self._shared.get("__live__l0", 0.0),
            "beta":          int(self._shared.get("__live__l1",
                                 self._cfg.beta_off / 9999.0) * 9999),
            "alpha":         self._shared.get("__live__l2", 0.0),
            "pattern":       self._pattern.pattern,
            "intensity":     self._pattern.intensity,
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
        return web.Response(text=json.dumps(d), content_type="application/json")

    async def _start_http(self):
        app = web.Application()
        app.router.add_get("/",                              self._handle_index)
        app.router.add_get("/touch",                         self._handle_touch)
        app.router.add_post("/command",                      self._handle_command)
        app.router.add_get("/state",                         self._handle_state)
        app.router.add_get("/touch_assets/list",             self._handle_assets_list)
        app.router.add_get("/touch_assets/{type}/{name}",    self._handle_assets_file)
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
            await asyncio.gather(self._pattern_loop(), self._alpha_loop())
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
