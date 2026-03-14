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
        # Beta sweep: slow, centred toward B (shaft/tip), moderate width, B-biased dwell
        "beta_mode":     "sweep",
        "beta_sweep":    {"hz": 0.34, "centre": 7700, "width": 2450, "skew": 0.17},
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
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:#111; --bg2:#1a1a1a; --bg3:#222;
    --border:#2a2a2a; --fg:#fff; --fg2:#999;
    --accent:#5fa3ff; --ok:#4caf50; --err:#f44336; --warn:#ff9800;
  }
  body {
    background:var(--bg); color:var(--fg);
    font-family:Arial,sans-serif; font-size:14px;
    padding:12px; max-width:480px; margin:0 auto;
  }
  h1 { font-size:15px; color:var(--fg2); margin-bottom:12px; letter-spacing:.05em; }

  /* Status */
  #status-bar { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
  #dot { width:10px; height:10px; border-radius:50%; background:var(--err); flex-shrink:0; }
  #status-text { color:var(--fg2); font-size:12px; flex:1; }

  /* Safety note */
  .safety {
    background:var(--bg2); border:1px solid var(--border); border-radius:5px;
    padding:8px 10px; margin-bottom:12px;
    color:var(--fg2); font-size:11px; line-height:1.5;
  }
  .safety strong { color:var(--warn); }

  /* STOP */
  #stop-btn {
    width:100%; padding:16px; background:var(--err); color:#fff;
    border:none; border-radius:6px; font-size:17px; font-weight:bold;
    cursor:pointer; margin-bottom:14px; letter-spacing:.08em;
  }
  #stop-btn:active { background:#c62828; }

  /* Section labels */
  .section-label {
    color:var(--fg2); font-size:11px; letter-spacing:.06em;
    text-transform:uppercase; margin-bottom:6px;
  }

  /* Preset row */
  #preset-row { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:4px; }
  .preset-btn {
    padding:7px 14px; background:#162012; color:#7dcc60;
    border:1px solid #426038; border-radius:5px;
    font-size:12px; font-weight:bold; cursor:pointer; letter-spacing:.04em;
  }
  .preset-btn:active { background:#223018; }
  .preset-btn:hover  { border-color:#6ab050; color:#a0e880; }

  /* Pattern grid */
  #pattern-grid {
    display:grid; grid-template-columns:repeat(4,1fr); gap:6px; margin-bottom:16px;
  }
  .pat-btn {
    padding:10px 4px; background:var(--bg3); color:var(--fg2);
    border:1px solid var(--border); border-radius:5px;
    font-size:12px; cursor:pointer; text-align:center; transition:all .1s;
  }
  .pat-btn:active { background:#333; }
  .pat-btn.active {
    background:var(--accent); border-color:var(--accent);
    color:#000; font-weight:bold;
  }

  /* Sliders */
  .slider-row { margin-bottom:14px; }
  .slider-header { display:flex; justify-content:space-between; margin-bottom:5px; }
  .slider-label { font-size:12px; }
  .slider-val { font-size:12px; color:var(--accent); font-weight:bold; min-width:50px; text-align:right; }
  input[type=range] {
    -webkit-appearance:none; width:100%; height:6px;
    border-radius:3px; background:var(--bg3); outline:none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; width:22px; height:22px;
    border-radius:50%; background:var(--accent); cursor:pointer;
  }
  #intensity-slider { height:10px; }
  #intensity-slider::-webkit-slider-thumb { width:30px; height:30px; }

  /* Beta controls */
  #beta-mode-row { display:grid; grid-template-columns:repeat(4,1fr); gap:6px; margin-bottom:10px; }
  .mode-btn { padding:9px; background:var(--bg3); color:var(--fg2);
    border:1px solid var(--border); border-radius:5px;
    font-size:12px; cursor:pointer; text-align:center; }
  .mode-btn.active { background:#1e2d3e; border-color:var(--accent); color:var(--fg); font-weight:bold; }
  #sweep-controls, #hold-controls { margin-top:4px; }
  #hold-beta-row { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-bottom:8px; }
  .hold-btn { padding:8px; background:var(--bg3); color:var(--fg2);
    border:1px solid var(--border); border-radius:5px; font-size:12px; cursor:pointer; }
  .hold-btn.active { background:#1e2d3e; border-color:var(--accent); color:var(--fg); }
  /* Spiral controls */
  #spiral-controls { margin-top:4px; }
  #spiral-btn-row { display:flex; gap:8px; margin-bottom:10px; }
  #spiral-amp-wrap { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  #spiral-amp-track { flex:1; height:6px; background:var(--bg3); border-radius:3px; }
  #spiral-amp-bar { height:6px; background:var(--accent); border-radius:3px;
    width:100%; transition:width .35s; }

  /* Alpha toggle */
  #alpha-row { margin-bottom:14px; }
  #alpha-toggle {
    width:100%; padding:9px; background:var(--bg3); color:var(--fg2);
    border:1px solid var(--border); border-radius:5px;
    font-size:13px; cursor:pointer; text-align:center;
  }
  #alpha-toggle.active { background:#1e2d3e; border-color:var(--accent); color:var(--fg); }

  /* Visualization row */
  .section-label { color:var(--fg2); font-size:11px; letter-spacing:.06em;
    text-transform:uppercase; margin-bottom:6px; margin-top:14px; }
  #viz-row { display:flex; gap:8px; margin-bottom:8px; align-items:flex-start; }
  #waveform { flex:1; min-width:0; height:72px; border-radius:4px; display:block; }
  #tri-canvas { width:110px; height:90px; flex-shrink:0; border-radius:4px; display:block; }
  /* Beta position indicator */
  #beta-pos { margin-bottom:14px; }
  #beta-track { height:6px; background:var(--bg3); border-radius:3px;
    position:relative; margin:6px 0 2px; }
  #beta-dot { width:14px; height:14px; background:var(--warn); border-radius:50%;
    position:absolute; top:-4px; transform:translateX(-50%); transition:left .35s ease; }
  #beta-labels { display:flex; justify-content:space-between; font-size:10px; color:var(--fg2); }

  /* Ramp */
  #ramp-btn-row { display:flex; gap:8px; margin-top:6px; margin-bottom:8px; }
  .ramp-btn { flex:1; padding:10px; border:none; border-radius:5px;
    font-size:13px; font-weight:bold; cursor:pointer; }
  #ramp-go { background:var(--ok); color:#000; }
  #ramp-go:active { background:#388e3c; }
  #ramp-stop-b { background:var(--bg3); color:var(--fg2); border:1px solid var(--border); }
  #ramp-stop-b:active { background:#333; }
  #ramp-progress-wrap { display:none; align-items:center; gap:8px; }
  #ramp-track { flex:1; height:6px; background:var(--bg3); border-radius:3px; }
  #ramp-bar { height:6px; background:var(--ok); border-radius:3px; width:0%;
    transition:width .35s; }
  #ramp-pct { font-size:11px; color:var(--fg2); min-width:80px; text-align:right; }

  /* Live */
  #live { color:var(--fg2); font-size:11px; font-family:monospace; min-height:18px; }
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px">
  <h1 style="margin:0">RESTIM DRIVE</h1>
  <a href="/touch" style="color:var(--fg2);font-size:11px;text-decoration:none;padding:4px 8px;border:1px solid var(--border);border-radius:4px">Touch ↗</a>
</div>

<div id="status-bar">
  <div id="dot"></div>
  <span id="status-text">Connecting…</span>
</div>

<div class="safety">
  <strong>Safety:</strong> the rider always sets their own maximum power limit on their device.
  This interface only controls pattern and relative intensity within that limit.
</div>

<button id="stop-btn" onclick="sendStop()">⬛  STOP</button>

<div class="section-label">Live</div>
<div id="viz-row">
  <canvas id="waveform" height="72"></canvas>
  <canvas id="tri-canvas" width="110" height="90"></canvas>
</div>
<div id="beta-pos">
  <div id="beta-track"><div id="beta-dot" style="left:50%"></div></div>
  <div id="beta-labels"><span>◄ L</span><span>Centre</span><span>R ►</span></div>
</div>

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
  <button class="ramp-btn" id="ramp-go" onclick="startRamp()">▶  Start Ramp</button>
  <button class="ramp-btn" id="ramp-stop-b" onclick="stopRamp()">■  Stop Ramp</button>
</div>
<div id="ramp-progress-wrap">
  <div id="ramp-track"><div id="ramp-bar"></div></div>
  <span id="ramp-pct">0% → 80%</span>
</div>

<div class="section-label">Beta  ·  sweep between electrodes</div>
<div id="beta-mode-row">
  <button class="mode-btn" data-mode="auto" onclick="setBetaMode(this)">Auto</button>
  <button class="mode-btn active" data-mode="sweep" onclick="setBetaMode(this)">Sweep ↔</button>
  <button class="mode-btn" data-mode="spiral" onclick="setBetaMode(this)">Spiral ◎</button>
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
      <span class="slider-label">Skew  <small style="color:var(--fg2)">← A dwell · even · B dwell →</small></span>
      <span class="slider-val" id="sweep-skew-val">even</span>
    </div>
    <input type="range" id="sweep-skew" min="-100" max="100" value="0"
           oninput="onSweepSkew(this.value)">
  </div>
</div>

<div id="hold-controls" style="display:none">
  <div id="hold-beta-row">
    <button class="hold-btn active" data-beta="8099" onclick="setHoldBeta(this)">◄ A</button>
    <button class="hold-btn" data-beta="5000" onclick="setHoldBeta(this)">Centre</button>
    <button class="hold-btn" data-beta="1900" onclick="setHoldBeta(this)">B ►</button>
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
    <button class="hold-btn" onclick="resetSpiral()">Reset ↺</button>
  </div>
  <div id="spiral-amp-wrap">
    <span style="font-size:11px;color:var(--fg2);min-width:60px">Amplitude</span>
    <div id="spiral-amp-track"><div id="spiral-amp-bar"></div></div>
    <span id="spiral-amp-pct" style="font-size:11px;color:var(--accent);min-width:35px;text-align:right">100%</span>
  </div>
</div>

<div id="alpha-row">
  <button id="alpha-toggle" class="active" onclick="toggleAlpha()">
    α  Alpha oscillation: ON
  </button>
</div>

<div id="live"></div>

<script>
const PATTERNS = ["Hold","Sine","Ramp ↑","Ramp ↓","Pulse","Burst","Random","Edge"];
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
  b.textContent = "★ " + name;
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
  if (v < 1500) return "← A";
  if (v > 8500) return "B →";
  if (v > 4500 && v < 5500) return "Centre";
  return v < 5000 ? "← " + Math.round((5000-v)/50) : Math.round((v-5000)/50) + " →";
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
  btn.textContent = "α  Alpha oscillation: " + (state.alpha ? "ON" : "OFF");
  sendCmd({ alpha: state.alpha });
}

function sendStop() {
  state.intensity = 0;
  document.getElementById("intensity-slider").value = 0;
  document.getElementById("int-val").textContent = "0%";
  sendCmd({ stop: true });
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
    ok ? "Connected to rider" : "Connection lost — retrying…";
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
  ctx.fillText("Vol",2,10); ctx.fillStyle="#2a4a2a"; ctx.fillText("α",2,H-3);
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
        Math.round(d.ramp_progress*100)+"% → "+Math.round(d.ramp_target*100)+"%";
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
      `Vol ${Math.round(d.vol*100)}%  β ${d.beta} (${betaLabel(d.beta)})  α ${Math.round(d.alpha*100)}%  ${d.pattern}`;
  } catch { setConnected(false); }
}

setInterval(pollState, 350);
pollState();
</script>
</body>
</html>
"""


# ── Touch driver page ─────────────────────────────────────────────────────────

TOUCH_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ReStim Drive &middot; Touch</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:#111; --bg2:#1a1a1a; --bg3:#222;
    --border:#2a2a2a; --fg:#fff; --fg2:#999;
    --accent:#5fa3ff; --ok:#4caf50; --err:#f44336; --warn:#ff9800;
    --ea:#5a78ff; --eb:#ffcc14; --ec:#44cc70;
  }
  html, body { height: 100%; overflow: hidden; }
  body {
    background: var(--bg); color: var(--fg);
    font-family: Arial, sans-serif; font-size: 14px;
    display: flex; flex-direction: column;
    padding: 8px; max-width: 480px; margin: 0 auto; gap: 5px;
    user-select: none; -webkit-user-select: none; touch-action: none;
  }
  .top-row { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  #stop-btn {
    flex: 1; padding: 10px; background: var(--err); color: #fff;
    border: none; border-radius: 6px; font-size: 14px; font-weight: bold;
    cursor: pointer; letter-spacing: .08em;
  }
  #stop-btn:active { background: #c62828; }
  #nav-link {
    color: var(--fg2); font-size: 11px; text-decoration: none;
    white-space: nowrap; border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 7px;
  }
  #conn { display: flex; align-items: center; gap: 5px; flex-shrink: 0; }
  #cdot { width: 9px; height: 9px; border-radius: 50%; background: var(--err); flex-shrink: 0; }
  #ctxt { color: var(--fg2); font-size: 11px; }
  .main-area { flex: 1; display: flex; gap: 5px; min-height: 0; }
  #tool-col { width: 50px; flex-shrink: 0; display: flex; flex-direction: column; gap: 5px; }
  .tool-btn {
    flex: 1; background: var(--bg3); color: var(--fg2);
    border: 1px solid var(--border); border-radius: 5px;
    font-size: 18px; cursor: pointer; text-align: center;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 2px; padding: 4px 2px;
  }
  .tool-btn span { font-size: 9px; line-height: 1; }
  .tool-btn.active { font-weight: bold; }
  .tool-btn[data-tool="feather"].active { background:#141428; border-color:#88aaff; color:#88aaff; }
  .tool-btn[data-tool="hand"].active    { background:#1e1e1e; border-color:#ffffff; color:#ffffff; }
  .tool-btn[data-tool="stroker"].active { background:#241400; border-color:#ff8800; color:#ff8800; }
  #anatomy-wrap { flex: 1; min-width: 0; position: relative; border-radius: 6px; }
  @keyframes loop-pulse {
    0%,100% { box-shadow: 0 0 0 0 rgba(95,163,255,0.5); }
    50%      { box-shadow: 0 0 0 8px rgba(95,163,255,0); }
  }
  #anatomy-wrap.looping { animation: loop-pulse 1.1s ease-in-out infinite; }
  #anatomy { width: 100%; height: 100%; display: block; border-radius: 6px; cursor: none; touch-action: none; }
  #electrode-col {
    width: 76px; flex-shrink: 0; display: flex; flex-direction: column;
    justify-content: space-between; gap: 4px; padding: 1px 0;
  }
  .elec-row {
    flex: 1; display: flex; flex-direction: column; align-items: flex-start;
    justify-content: center; gap: 4px; padding: 4px;
    border: 1px solid var(--border); border-radius: 4px; background: var(--bg3);
  }
  .elec-anat { font-size: 9px; color: var(--fg2); font-weight: bold; letter-spacing: .06em; }
  .elec-radios { display: flex; gap: 5px; align-items: center; }
  .elec-radios label { display: flex; flex-direction: column; align-items: center; gap: 1px; cursor: pointer; }
  .elec-radios input[type=radio] { width: 14px; height: 14px; cursor: pointer; accent-color: var(--accent); }
  .elec-radios span { font-size: 10px; font-weight: bold; line-height: 1; }
  .ea-lbl { color: var(--ea); }
  .eb-lbl { color: var(--eb); }
  .ec-lbl { color: var(--ec); }
  #anatomy-picker {
    display: flex; gap: 6px; overflow-x: auto; flex-shrink: 0;
    padding: 2px 0 4px; min-height: 58px; align-items: flex-start;
  }
  .anat-thumb {
    width: 40px; height: 52px; border-radius: 4px; cursor: pointer;
    border: 2px solid var(--border); flex-shrink: 0; overflow: hidden;
    background: var(--bg3); position: relative;
  }
  .anat-thumb canvas, .anat-thumb img { width: 100%; height: 100%; display: block; object-fit: cover; }
  .anat-thumb.active { border-color: var(--accent); }
  .anat-thumb-label {
    position: absolute; bottom: 0; left: 0; right: 0; font-size: 8px;
    text-align: center; background: rgba(0,0,0,0.60); padding: 2px 0;
    color: var(--fg2); pointer-events: none;
  }
  .info-row { display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
  #astatus { color: var(--fg2); font-size: 11px; font-family: monospace; }
  .legend { display: flex; gap: 8px; }
  .leg { font-size: 10px; color: var(--fg2); display: flex; align-items: center; gap: 3px; }
  .ldot { width: 8px; height: 8px; border-radius: 50%; }
</style>
</head>
<body>

<div class="top-row">
  <div id="conn"><div id="cdot"></div><span id="ctxt">Connecting&#8230;</span></div>
  <button id="stop-btn" onclick="doStop()">&#9632; STOP</button>
  <a id="nav-link" href="/">Main &#8599;</a>
</div>

<div class="main-area">
  <div id="tool-col">
    <button class="tool-btn active" data-tool="feather" onclick="selectTool(this)">
      &#129302;<span>Feather</span>
    </button>
    <button class="tool-btn" data-tool="hand" onclick="selectTool(this)">
      &#9995;<span>Hand</span>
    </button>
    <button class="tool-btn" data-tool="stroker" onclick="selectTool(this)">
      &#9889;<span>Stroker</span>
    </button>
  </div>

  <div id="anatomy-wrap">
    <canvas id="anatomy"></canvas>
  </div>

  <div id="electrode-col">
    <div class="elec-row" data-anat="tip">
      <div class="elec-anat">&#9650; TIP</div>
      <div class="elec-radios">
        <label><input type="radio" name="tip-e" value="A"><span class="ea-lbl">A</span></label>
        <label><input type="radio" name="tip-e" value="B" checked><span class="eb-lbl">B</span></label>
        <label><input type="radio" name="tip-e" value="C"><span class="ec-lbl">C</span></label>
      </div>
    </div>
    <div class="elec-row" data-anat="balls">
      <div class="elec-anat">&#9679; BALLS</div>
      <div class="elec-radios">
        <label><input type="radio" name="balls-e" value="A"><span class="ea-lbl">A</span></label>
        <label><input type="radio" name="balls-e" value="B"><span class="eb-lbl">B</span></label>
        <label><input type="radio" name="balls-e" value="C" checked><span class="ec-lbl">C</span></label>
      </div>
    </div>
    <div class="elec-row" data-anat="anus">
      <div class="elec-anat">&#9660; ANUS</div>
      <div class="elec-radios">
        <label><input type="radio" name="anus-e" value="A" checked><span class="ea-lbl">A</span></label>
        <label><input type="radio" name="anus-e" value="B"><span class="eb-lbl">B</span></label>
        <label><input type="radio" name="anus-e" value="C"><span class="ec-lbl">C</span></label>
      </div>
    </div>
  </div>
</div>

<div id="anatomy-picker"></div>

<div class="info-row">
  <span id="astatus">Tap or drag &middot; Y = position &middot; X = intensity</span>
  <div class="legend">
    <div class="leg"><div class="ldot" style="background:var(--ea)"></div>A</div>
    <div class="leg"><div class="ldot" style="background:var(--eb)"></div>B</div>
    <div class="leg"><div class="ldot" style="background:var(--ec)"></div>C</div>
  </div>
</div>

<script>
const TOOLS = {
  feather: { min:0.01, max:0.30, color:'#88aaff', cursorW:0.88 },
  hand:    { min:0.31, max:0.60, color:'#ffffff', cursorW:0.55 },
  stroker: { min:0.61, max:1.00, color:'#ff8800', cursorW:0.35 },
};
let currentTool = 'feather';
let pointerDown = false;
let lastBeta    = 5000;
let lastX       = 0.5;
let lastY       = 0.5;
let gestureRec  = [];
let gestureStart= 0;
let looping     = false;

// Electrode assignment: tip/balls/anus -> A/B/C label
// A = beta 0 (one physical end), B = beta 9999 (other end), C = beta 5000 (neutral)
const ELEC_BETA  = { A:0, B:9999, C:5000 };
const ANAT_YF    = { tip:0.0, balls:0.5, anus:1.0 };
const ELEC_COLOR = { A:'#5a78ff', B:'#ffcc14', C:'#44cc70' };

let elecAt = JSON.parse(localStorage.getItem('elecAt') || 'null')
          || { tip:'B', balls:'C', anus:'A' };

function saveElecAt() { localStorage.setItem('elecAt', JSON.stringify(elecAt)); }

function betaFromY(y) {
  const pts = Object.entries(elecAt)
    .map(([anat, elec]) => ({ y: ANAT_YF[anat], beta: ELEC_BETA[elec] }))
    .sort((a, b) => a.y - b.y);
  if (y <= pts[0].y) return pts[0].beta;
  if (y >= pts[2].y) return pts[2].beta;
  for (let i = 0; i < 2; i++) {
    if (y >= pts[i].y && y <= pts[i+1].y) {
      const f = (y - pts[i].y) / (pts[i+1].y - pts[i].y);
      return Math.round(pts[i].beta + f * (pts[i+1].beta - pts[i].beta));
    }
  }
  return 5000;
}

function intensityFromX(x) {
  const t = TOOLS[currentTool]; return t.min + x * (t.max - t.min);
}

function initRadios() {
  for (const [anat, elec] of Object.entries(elecAt)) {
    const r = document.querySelector(`input[name="${anat}-e"][value="${elec}"]`);
    if (r) r.checked = true;
  }
}

function onElecChange(anat, newElec) {
  const prevElec = elecAt[anat];
  for (const [a, e] of Object.entries(elecAt)) {
    if (a !== anat && e === newElec) { elecAt[a] = prevElec; break; }
  }
  elecAt[anat] = newElec;
  saveElecAt();
  for (const [a, e] of Object.entries(elecAt)) {
    const r = document.querySelector(`input[name="${a}-e"][value="${e}"]`);
    if (r) r.checked = true;
  }
  buildPicker();
  draw();
}

document.querySelectorAll('.elec-radios input').forEach(r => {
  r.addEventListener('change', () => onElecChange(r.closest('.elec-row').dataset.anat, r.value));
});

// ── Anatomy picker ─────────────────────────────────────────────────────────
let anatVariants  = [];
let currentAnatId = localStorage.getItem('anatId') || 'default';
let customAnatImg = null;

async function loadAnatomyList() {
  anatVariants = [
    { id:'default', label:'Default', type:'canvas', drawFn: drawAnatomyDetailed },
    { id:'simple',  label:'Simple',  type:'canvas', drawFn: drawAnatomySimple   },
  ];
  try {
    const resp = await fetch('/touch_assets/list?type=anatomy');
    if (resp.ok) {
      for (const f of await resp.json())
        anatVariants.push({ id:f, label:f.replace(/\.[^.]+$/, ''), type:'png',
                            src:'/touch_assets/anatomy/' + encodeURIComponent(f) });
    }
  } catch(_) {}
  buildPicker();
  applyAnatVariant(currentAnatId);
}

function buildPicker() {
  const el = document.getElementById('anatomy-picker');
  el.innerHTML = '';
  for (const v of anatVariants) {
    const wrap = document.createElement('div');
    wrap.className = 'anat-thumb' + (v.id === currentAnatId ? ' active' : '');
    wrap.title = v.label;
    if (v.type === 'canvas') {
      const tc = document.createElement('canvas');
      tc.width = 40; tc.height = 52;
      v.drawFn(tc.getContext('2d'), 40, 52, true);
      wrap.appendChild(tc);
    } else {
      const img = document.createElement('img'); img.src = v.src; img.alt = v.label;
      wrap.appendChild(img);
    }
    const lbl = document.createElement('div');
    lbl.className = 'anat-thumb-label'; lbl.textContent = v.label;
    wrap.appendChild(lbl);
    wrap.addEventListener('click', () => selectAnat(v.id));
    el.appendChild(wrap);
  }
}

function selectAnat(id) {
  currentAnatId = id;
  localStorage.setItem('anatId', id);
  document.querySelectorAll('.anat-thumb').forEach((t, i) =>
    t.classList.toggle('active', anatVariants[i] && anatVariants[i].id === id));
  applyAnatVariant(id);
}

function applyAnatVariant(id) {
  const v = anatVariants.find(a => a.id === id) || anatVariants[0];
  if (v && v.type === 'png' && v.src) {
    const img = new Image();
    img.onload  = () => { customAnatImg = img; draw(); };
    img.onerror = () => { customAnatImg = null; draw(); };
    img.src = v.src;
  } else { customAnatImg = null; draw(); }
}

// ── Tool cursor PNG overrides ──────────────────────────────────────────────
const toolImages = {};

async function loadToolImages() {
  try {
    const resp = await fetch('/touch_assets/list?type=tools');
    if (resp.ok) {
      for (const f of await resp.json()) {
        const tool = f.replace(/\.[^.]+$/, '').toLowerCase();
        if (tool in TOOLS) {
          const img = new Image();
          img.src = '/touch_assets/tools/' + encodeURIComponent(f);
          img.onload = () => { toolImages[tool] = img; if (pointerDown) draw(); };
        }
      }
    }
  } catch(_) {}
}

const cvs = document.getElementById('anatomy');

function selectTool(btn) {
  document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentTool = btn.dataset.tool;
  draw();
}

function getPos(e) {
  const rect = cvs.getBoundingClientRect(), src = e.touches ? e.touches[0] : e;
  return {
    x: Math.max(0, Math.min(1, (src.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (src.clientY - rect.top)  / rect.height)),
  };
}

cvs.addEventListener('mousedown',    onDown, {passive:false});
cvs.addEventListener('touchstart',   onDown, {passive:false});
cvs.addEventListener('mousemove',    onMove, {passive:false});
cvs.addEventListener('touchmove',    onMove, {passive:false});
document.addEventListener('mouseup',     onUp);
document.addEventListener('touchend',    onUp);
document.addEventListener('touchcancel', onUp);

function onDown(e) {
  e.preventDefault();
  pointerDown = true; gestureRec = []; gestureStart = performance.now();
  const pos = getPos(e);
  lastBeta = betaFromY(pos.y); lastX = pos.x; lastY = pos.y;
  gestureRec.push({t:0, beta:lastBeta, intensity:intensityFromX(pos.x)});
  sendCmd({ gesture_stop:true, beta_mode:'hold', beta:lastBeta, intensity:intensityFromX(pos.x) });
  setLooping(false); draw();
}

function onMove(e) {
  if (!pointerDown) return;
  e.preventDefault();
  const pos = getPos(e);
  lastBeta = betaFromY(pos.y); lastX = pos.x; lastY = pos.y;
  gestureRec.push({t:(performance.now()-gestureStart)/1000, beta:lastBeta, intensity:intensityFromX(pos.x)});
  sendCmd({ beta:lastBeta, intensity:intensityFromX(pos.x) });
  draw();
}

function onUp() {
  if (!pointerDown) return;
  pointerDown = false;
  const dur = gestureRec.length >= 2 ? gestureRec[gestureRec.length-1].t : 0;

  if (dur >= 0.5 && gestureRec.length >= 6) {
    sendCmd({ gesture_record: subsample(gestureRec, 150) });
    setLooping(true);
    setStatus('Looping ' + dur.toFixed(1) + 's  drag to replace');
    draw(); return;
  }

  const betas  = gestureRec.map(p => p.beta);
  const minB   = Math.min(...betas), maxB = Math.max(...betas), rangeB = maxB - minB;

  if (gestureRec.length < 4 || rangeB < 400) {
    sendCmd({ beta_mode:'hold', beta:lastBeta });
    setStatus('Hold ' + betaLabel(lastBeta) + '  ' + Math.round(intensityFromX(lastX)*100) + '%');
    draw(); return;
  }

  const centre = Math.round((minB + maxB) / 2);
  const width  = Math.round(rangeB / 2);
  const dist   = betas.reduce((s,b,i) => i>0 ? s+Math.abs(b-betas[i-1]) : 0, 0);
  const hz     = Math.min(3.0, Math.max(0.05, dist / Math.max(0.1,dur) / (2*Math.max(1,width))));
  const avgInt = gestureRec.reduce((s,p) => s+p.intensity, 0) / gestureRec.length;
  sendCmd({ intensity:avgInt, beta_mode:'sweep', beta_sweep:{centre, width, hz:Math.round(hz*100)/100} });
  setStatus('Sweep ' + hz.toFixed(2) + ' Hz  ' + Math.round(avgInt*100) + '%');
  draw();
}

function subsample(pts, maxN) {
  if (pts.length <= maxN) return pts;
  const t0 = pts[0].t, t1 = pts[pts.length-1].t, out = [];
  for (let i = 0; i < maxN; i++) {
    const t = t0 + (i/(maxN-1))*(t1-t0);
    let j = 0;
    while (j < pts.length-1 && pts[j+1].t < t) j++;
    if (j >= pts.length-1) {
      out.push({t:t-t0, beta:pts[j].beta, intensity:pts[j].intensity});
    } else {
      const f = (t-pts[j].t) / Math.max(0.001, pts[j+1].t-pts[j].t);
      out.push({t:t-t0,
        beta: Math.round(pts[j].beta + f*(pts[j+1].beta-pts[j].beta)),
        intensity: pts[j].intensity + f*(pts[j+1].intensity-pts[j].intensity),
      });
    }
  }
  return out;
}

function betaLabel(v) {
  let best = 'C', bestDist = Infinity;
  for (const [,elec] of Object.entries(elecAt)) {
    const d = Math.abs(ELEC_BETA[elec] - v);
    if (d < bestDist) { bestDist = d; best = elec; }
  }
  const entry = Object.entries(elecAt).find(([,e]) => e === best);
  return best + '(' + (entry ? entry[0] : '') + ')';
}

async function sendCmd(cmd) {
  try {
    const r = await fetch('/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cmd)});
    setConn(r.ok);
  } catch(_) { setConn(false); }
}
function doStop()    { sendCmd({stop:true}); setLooping(false); setStatus('Stopped'); }
function setConn(ok) {
  document.getElementById('cdot').style.background = ok ? 'var(--ok)' : 'var(--err)';
  document.getElementById('ctxt').textContent = ok ? 'Connected' : 'Disconnected';
}
function setStatus(m) { document.getElementById('astatus').textContent = m; }
function setLooping(on) {
  looping = on;
  document.getElementById('anatomy-wrap').classList.toggle('looping', on);
}

// ── Drawing ────────────────────────────────────────────────────────────────
function rgba(hex, a) {
  const n = parseInt(hex.replace('#',''), 16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;
}

function buildAnatGrad(ctx, W, H) {
  const base = { A:'45,75,225', B:'255,200,18', C:'50,195,90' };
  const stops = Object.entries(elecAt)
    .map(([anat,elec]) => ({ y:ANAT_YF[anat], c:base[elec] }))
    .sort((a,b) => a.y - b.y);
  const g = ctx.createLinearGradient(0,0,0,H);
  [0.82, 0.44, 0.76].forEach((op,i) => g.addColorStop(stops[i].y, `rgba(${stops[i].c},${op})`));
  return g;
}

function drawAnatomyDetailed(ctx, W, H, thumb) {
  const cx=W/2, GLY=0.07, SHT=0.15, SHB=0.44, SCY=0.50, PERY=0.72, ANY=0.88;
  const shr=W*0.130, gr=W*0.195, gtv=H*0.055;
  const slx=W*0.195, sla=W*0.205, slb=H*0.115;
  const ar=Math.min(W*0.095,H*0.046), pr=W*0.062, lw=thumb?0.8:1.5;
  ctx.clearRect(0,0,W,H); ctx.fillStyle='#1a1a1a'; ctx.fillRect(0,0,W,H);
  const grad=buildAnatGrad(ctx,W,H);
  const fill=()=>{ ctx.fillStyle=grad; ctx.fill(); ctx.strokeStyle='#2c3558'; ctx.lineWidth=lw; ctx.stroke(); };
  // Perineum
  ctx.beginPath();
  ctx.moveTo(cx-pr,H*PERY);
  ctx.bezierCurveTo(cx-pr*0.7,H*(PERY+ANY)/2,cx-ar*0.85,H*ANY-ar*0.7,cx-ar*0.85,H*ANY);
  ctx.lineTo(cx+ar*0.85,H*ANY);
  ctx.bezierCurveTo(cx+ar*0.85,H*ANY-ar*0.7,cx+pr*0.7,H*(PERY+ANY)/2,cx+pr,H*PERY);
  ctx.closePath(); fill();
  // Scrotum
  ctx.beginPath(); ctx.ellipse(cx-slx,H*SCY+slb*0.18,sla*0.82,slb*0.86,0.08,0,Math.PI*2); fill();
  ctx.beginPath(); ctx.ellipse(cx+slx,H*SCY+slb*0.18,sla*0.82,slb*0.86,-0.08,0,Math.PI*2); fill();
  // Raphe
  ctx.beginPath(); ctx.moveTo(cx,H*SCY-slb*0.12);
  ctx.bezierCurveTo(cx+slb*0.04,H*SCY,cx-slb*0.04,H*(SCY+0.07),cx,H*(SCY+0.10));
  ctx.strokeStyle='rgba(28,38,88,0.50)'; ctx.lineWidth=thumb?1:2; ctx.stroke();
  // Shaft
  ctx.beginPath();
  ctx.moveTo(cx-shr*1.06,H*SHB); ctx.lineTo(cx-shr,H*SHT);
  ctx.lineTo(cx+shr,H*SHT); ctx.lineTo(cx+shr*1.06,H*SHB);
  ctx.closePath(); fill();
  // Glans
  ctx.beginPath(); ctx.ellipse(cx,H*GLY,gr,gtv,0,0,Math.PI*2); fill();
  // Corona
  ctx.beginPath();
  ctx.moveTo(cx-gr*0.87,H*SHT+1);
  ctx.bezierCurveTo(cx-gr*0.20,H*SHT+H*0.013,cx+gr*0.20,H*SHT+H*0.013,cx+gr*0.87,H*SHT+1);
  ctx.strokeStyle='rgba(28,38,88,0.60)'; ctx.lineWidth=thumb?1:2.5; ctx.stroke();
  // Anus
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

function drawAnatomySimple(ctx, W, H, thumb) {
  const cx=W/2;
  ctx.clearRect(0,0,W,H); ctx.fillStyle='#1a1a1a'; ctx.fillRect(0,0,W,H);
  const grad=buildAnatGrad(ctx,W,H);
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
    for (const yf of [0.44, 0.56]) {
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

function drawElecLabels(ctx, W, H) {
  const GLY=0.07, SCY=0.50, ANY=0.88;
  const slb=H*0.115, ar=Math.min(W*0.095,H*0.046), gtv=H*0.055, cx=W/2;
  for (const [anat, elec] of Object.entries(elecAt)) {
    let lblY;
    if (anat==='tip')   lblY = H*GLY + gtv + 14;
    if (anat==='balls') lblY = H*SCY + slb + 14;
    if (anat==='anus')  lblY = H*ANY + ar  + 14;
    ctx.font='bold 12px Arial'; ctx.textAlign='right'; ctx.fillStyle=ELEC_COLOR[elec];
    ctx.fillText(elec, cx+W*0.44, lblY-2);
    ctx.font='9px Arial'; ctx.textAlign='center'; ctx.fillStyle='rgba(180,180,200,0.58)';
    ctx.fillText(anat, cx, lblY);
  }
}

function drawToolBand(ctx, W, H) {
  const t=TOOLS[currentTool], xL=t.min*W, xR=t.max*W;
  ctx.fillStyle=rgba(t.color,0.07); ctx.fillRect(xL,0,xR-xL,H);
  ctx.fillStyle=rgba(t.color,0.28); ctx.fillRect(xL,0,2,3); ctx.fillRect(xR-2,0,2,3);
}

function drawToolCursor(ctx, W, H) {
  const tool=TOOLS[currentTool], curX=lastX*W, curY=lastY*H;
  const cw=W*tool.cursorW, ch=Math.max(16,cw*0.16);
  if (toolImages[currentTool]) {
    const img=toolImages[currentTool], iw=cw, ih=iw*(img.height/img.width);
    ctx.save(); ctx.globalAlpha=0.70; ctx.drawImage(img,curX-iw/2,curY-ih/2,iw,ih); ctx.restore();
  } else {
    const glow=ctx.createRadialGradient(curX,curY,0,curX,curY,cw*0.56);
    glow.addColorStop(0,rgba(tool.color,0.26)); glow.addColorStop(0.65,rgba(tool.color,0.10)); glow.addColorStop(1,rgba(tool.color,0));
    ctx.fillStyle=glow; ctx.beginPath(); ctx.ellipse(curX,curY,cw*0.56,ch*0.95,0,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.ellipse(curX,curY,cw*0.50,ch*0.78,0,0,Math.PI*2);
    ctx.strokeStyle=rgba(tool.color,0.48); ctx.lineWidth=1.5; ctx.stroke();
    ctx.strokeStyle=rgba(tool.color,0.38); ctx.lineWidth=1;
    if (currentTool==='feather') {
      ctx.beginPath(); ctx.moveTo(curX-cw*0.46,curY); ctx.lineTo(curX+cw*0.46,curY); ctx.stroke();
      for (let bx=-0.40; bx<=0.40; bx+=0.10) {
        ctx.beginPath(); ctx.moveTo(curX+cw*bx,curY); ctx.lineTo(curX+cw*bx+cw*0.04,curY-ch*0.48); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(curX+cw*bx,curY); ctx.lineTo(curX+cw*bx-cw*0.04,curY+ch*0.48); ctx.stroke();
      }
    } else if (currentTool==='hand') {
      for (let fi=-2; fi<=2; fi++) {
        ctx.beginPath(); ctx.moveTo(curX+fi*cw*0.09,curY); ctx.lineTo(curX+fi*cw*0.09,curY-ch*0.58); ctx.stroke();
      }
    } else {
      ctx.strokeStyle=rgba(tool.color,0.55); ctx.lineWidth=2;
      ctx.beginPath();
      ctx.moveTo(curX-cw*0.10,curY-ch*0.55); ctx.lineTo(curX+cw*0.04,curY-ch*0.05);
      ctx.lineTo(curX-cw*0.04,curY+ch*0.05); ctx.lineTo(curX+cw*0.10,curY+ch*0.55); ctx.stroke();
    }
  }
  ctx.fillStyle=rgba(tool.color,0.80); ctx.font='bold 10px Arial'; ctx.textAlign='left';
  ctx.fillText(Math.round(intensityFromX(lastX)*100)+'%', curX+cw*0.52+3, curY-3);
  const tkR=W*0.30; ctx.strokeStyle=rgba(tool.color,0.40); ctx.lineWidth=1; ctx.setLineDash([3,4]);
  ctx.beginPath(); ctx.moveTo(W/2-tkR,curY); ctx.lineTo(W/2+tkR,curY); ctx.stroke();
  ctx.setLineDash([]);
}

function draw() {
  const W=cvs.offsetWidth, H=cvs.offsetHeight;
  if (W<10||H<10) return;
  cvs.width=W; cvs.height=H;
  const ctx=cvs.getContext('2d');
  if (customAnatImg) {
    ctx.fillStyle='#1a1a1a'; ctx.fillRect(0,0,W,H);
    ctx.drawImage(customAnatImg,0,0,W,H);
  } else {
    const v=anatVariants.find(a=>a.id===currentAnatId);
    ((v&&v.drawFn)||drawAnatomyDetailed)(ctx,W,H,false);
  }
  drawToolBand(ctx,W,H);
  drawElecLabels(ctx,W,H);
  if (pointerDown) drawToolCursor(ctx,W,H);
}

const ro=new ResizeObserver(()=>draw()); ro.observe(document.getElementById('anatomy-wrap'));

setInterval(async()=>{
  try {
    const d=await(await fetch('/state')).json(); setConn(true);
    if (!pointerDown) setLooping(d.gesture_active);
    if (d.gesture_active&&!pointerDown&&!looping) setStatus('Looping '+d.gesture_dur.toFixed(1)+'s  drag to replace');
  } catch(_) { setConn(false); }
},1500);

initRadios(); loadAnatomyList(); loadToolImages(); draw();
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


    async def _handle_command(self, req):
        try:
            cmd = await req.json()
        except Exception:
            return web.Response(status=400)

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
