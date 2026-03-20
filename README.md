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
pip install aiohttp jinja2 aiohttp-jinja2
```

ReStim must be running and have its WebSocket server enabled (default `ws://localhost:12346`).

---

## Usage

### LAN mode (single rider, local ReStim)

```
python server.py --local
```

This starts a single-room server that connects directly to your local ReStim instance. URLs for the driver and rider pages are printed on startup.

| Who    | What to open |
|--------|-------------|
| Rider  | Open the rider URL printed at startup, or the `/touch` page on your phone |
| Driver (desktop) | Open the driver URL printed at startup |
| Driver (phone)   | Same driver URL on your phone |

The rider always controls their own maximum power on their ReStim device. ReDrive only controls pattern shape and relative intensity within that limit.

### Relay mode (multi-rider, cloud server)

```
python server.py --port 8765
```

Starts the relay server. Drivers create rooms; riders connect via room codes. The browser rider page can connect to ReStim directly using the ReStim Bridge (no separate client needed).

---

## Configuration

On first run, `redrive_config.json` is created with defaults. Copy `redrive_config.json.example` as a starting point or edit the generated file directly.

| Key | Default | Description |
|-----|---------|-------------|
| `restim_url` | `ws://localhost:12346/tcode` | ReStim WebSocket address |
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

Presets live in the `PRESETS` dict in `engine.py`. The driver UI
fetches the preset list and syncs all sliders from the server's `/state`
endpoint automatically - no client-side duplication needed.

---

## Running tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

The test suite covers the pattern engine, drive engine command handling, room
lifecycle, and HTTP route integration tests (auth, state, presets).

---

## Cloud relay (multi-rider)

ReDrive can run on a public server so the driver uses a shareable URL instead
of a LAN IP.  Multiple riders connect to the same room simultaneously.

### Architecture

```
Driver browser ──POST /room/CODE/command──▶ server.py (DriveEngine per room)
                                                │
                                      T-code broadcast
                                                │
Rider 1  rider_client.py ◀──WS /room/CODE/rider◀┘
Rider 2  rider_client.py ◀──WS /room/CODE/rider
```

The pattern engine lives on the server.  `rider_client.py` is a thin bridge
that forwards T-code from the relay to the local ReStim WebSocket.

### Quick deploy (Ubuntu 22.04 droplet)

```bash
# On the server (as root):
bash deploy/setup.sh
```

The script installs nginx, certbot, a Python venv, the systemd service, and
obtains a TLS certificate for `redrive.estimstation.com`.

### Rider setup

On the rider's machine (the one connected to the ReStim device):

```bash
pip install aiohttp
python rider_client.py XXXXXXXXXX          # 10-char code from driver
```

The rider can also open `https://redrive.estimstation.com/room/CODE/touch`
on their phone for the touch control page.

### Room codes

- 10 characters from an unambiguous alphabet (no 0/O/1/I/L)
- Each room expires after 24 hours of inactivity
- Driver copies the code via the banner shown at the top of the driver page

---

## Acknowledgements

- [ReStim](https://github.com/diglet48/restim) by diglet48 — the e-stim engine this bridges to
- T-code protocol — standard used across the e-stim / toy ecosystem
