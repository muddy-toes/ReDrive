# ReDrive

Remote driver/rider control interface for ReStim-compatible e-stim devices.

The **rider** runs ReDrive on their machine and connects it to ReStim. The **driver** opens a browser — on any device, including a phone — and takes control of patterns, intensity, electrode position, and more in real time.

---

## Features

- **Pattern engine** — Hold, Sine, Ramp, Pulse, Burst, Random, Edge
- **Beta sweep** — slow/fast oscillation between electrodes with skew (dwell bias toward A or B end)
- **Spiral mode** — quadrature beta/alpha sweep that tightens toward centre and auto-resets
- **Intensity ramp** — smooth ramp to target over configurable duration
- **Touch interface** — phone-friendly `/touch` page with anatomy overlay, vertical position/intensity axes, and A/B/C electrode assignment
- **Gesture looping** — draw a gesture on the touch canvas, release, and it loops indefinitely
- **Presets** — one-click full state recall (includes Milking out of the box)
- **Custom anatomy overlays** — drop PNGs into `touch_assets/anatomy/` to swap the anatomy graphic
- **Custom tool cursors** — drop PNGs into `touch_assets/tools/` (`feather.png`, `hand.png`, `stroker.png`)

---

## Requirements

```
pip install aiohttp
```

ReStim must be running and have its WebSocket server enabled (default `ws://localhost:12346`).

---

## Usage

```
python redrive.py
```

| Who    | What to open |
|--------|-------------|
| Rider  | Just run the script — keep the terminal visible for status |
| Driver (desktop) | `http://<rider-ip>:8765` |
| Driver (phone)   | `http://<rider-ip>:8765/touch` |

The rider always controls their own maximum power on their ReStim device. ReDrive only controls pattern shape and relative intensity within that limit.

---

## Configuration

On first run, `redrive_config.json` is created with defaults. Copy `redrive_config.json.example` as a starting point or edit the generated file directly.

| Key | Default | Description |
|-----|---------|-------------|
| `restim_url` | `ws://localhost:12346` | ReStim WebSocket address |
| `ctrl_port` | `8765` | Port for the driver browser UI |
| `axis_volume` | `L0` | T-code axis for intensity |
| `axis_beta` | `L1` | T-code axis for electrode position |
| `axis_alpha` | `L2` | T-code axis for alpha oscillation |
| `tcode_floor` | `0` | Minimum T-code value when intensity > 0 |
| `send_interval_ms` | `50` | Command send rate (ms) |

---

## Touch page — electrode assignment

The `/touch` page has three anatomy positions (Tip, Balls, Anus) each assignable to electrode label **A**, **B**, or **C**:

- **A** = beta 0 (one physical wire end)
- **B** = beta 9999 (other physical wire end)
- **C** = beta 5000 (neutral centre)

Reassign to match however the rider has physically wired their electrode — no need to swap wires.

---

## Adding presets

Presets are stored in two places that must stay in sync:

1. `PRESETS` dict near the top of `redrive.py` (applied server-side)
2. `JS_PRESETS` object in the `DRIVER_HTML` string (updates the driver UI sliders)

Both are marked with a warning comment pointing at each other.

---

## Acknowledgements

- [ReStim](https://github.com/diglet48/restim) by diglet48 — the e-stim engine this bridges to
- T-code protocol — standard used across the e-stim / toy ecosystem
