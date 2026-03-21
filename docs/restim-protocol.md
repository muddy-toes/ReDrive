# ReStim T-Code Protocol Reference

Reference for communicating with ReStim via its WebSocket interface.
Derived from reading the ReStim source code at `../restim/`.

## Connection

- WebSocket endpoint: `ws://localhost:12346/tcode` (default port, configurable in ReStim preferences)
- ReStim's server is a Qt QWebSocketServer that routes by path
- `/tcode` is the T-code handler; other paths: `/sensors/as5311`, `/sensors/pressure`, `/sensors/imu`
- Connecting to `/` or any unrecognized path returns 404 and closes the socket
- ReStim does NOT respond to WebSocket PING frames (Qt QWebSocket behavior) - do not use `heartbeat` parameter in aiohttp's `ws_connect()`

## T-Code Format

```
AAXXXXIYYYY
```

- `AA` = 2-character axis identifier (e.g., `V0`, `L0`, `L1`)
- `XXXX` = value digits (0-9999)
- `I` = optional interval separator
- `YYYY` = interpolation interval in milliseconds

Multiple commands can be sent in one message separated by spaces:
```
V05000I50 L10000I50 L05000I50
```

### Value Parsing

ReStim normalizes the value: `float_value = int_value / (10 ** num_digits)`
- `V07500` -> 7500 / 10000 = 0.75
- `L05000` -> 5000 / 10000 = 0.50
- `V00000` -> 0 / 10000 = 0.0
- `V09999` -> 9999 / 10000 = 0.9999

### Remap Formula

Each axis has configurable min/max limits. The normalized value is remapped:
```
remapped = normalized_value * (limit_max - limit_min) + limit_min
```

## Default Axis Mappings

| T-Code Axis | ReStim Axis | Purpose | Limits | Remap Example |
|---|---|---|---|---|
| `V0` | VOLUME_API | Master volume multiplier | 0 to 1 | V05000 -> 0.5 (50% volume) |
| `L0` | POSITION_ALPHA | Alpha electrode position | -1 to 1 | L05000 -> 0.0 (center) |
| `L1` | POSITION_BETA | Beta electrode position | -1 to 1 | L15000 -> 0.0 (center) |

### Other Available Axes (not used by ReDrive)

| T-Code | ReStim Axis | Limits | Notes |
|---|---|---|---|
| `C0` | CARRIER_FREQUENCY | 500-1000 Hz | |
| `P0` | PULSE_FREQUENCY | 0-100 Hz | |
| `P1` | PULSE_WIDTH | 4-10 us | |
| `P2` | PULSE_INTERVAL_RANDOM | 0-1 | |
| `P3` | PULSE_RISE_TIME | 2-20 ms | |

## Volume System

ReStim computes effective volume as:
```
effective_volume = master_volume * api_volume * inactivity_volume * external_volume
```

- `api_volume` = from T-code V0 (what ReDrive sends)
- `master_volume` = ReStim UI slider (user's max power limit)
- `inactivity_volume` = auto-ramp when idle
- `external_volume` = separate external axis (not used by ReDrive)

ReDrive's intensity slider controls `api_volume`. The rider's safety limit is ReStim's `master_volume` slider.

## Beta Position (L1) - Electrode Mapping

The beta axis controls signal distribution between electrodes:

```
T-code 0    -> beta -1.0 -> R+ electrode (right side of ReStim triangle)
T-code 5000 -> beta  0.0 -> Center (neutral)
T-code 9999 -> beta +1.0 -> L+ electrode (left side of ReStim triangle)
```

**IMPORTANT: ReStim's triangle widget has an inverted X coordinate** (`b * -83` in `threephase_widget.py`).
This means T-code 9999 (beta +1) appears on the LEFT side of ReStim's triangle where the L+ label is.
ReDrive's indicators are drawn with `1 - beta/9999` to match this visual convention.

## Alpha Position (L0) - Second Axis

Alpha controls a second axis of electrode distribution:
```
T-code 0    -> alpha -1.0
T-code 5000 -> alpha  0.0 (center/parked)
T-code 9999 -> alpha +1.0
```

ReDrive oscillates alpha as a sine wave when alpha mode is enabled, creating a second movement layer.

## ReStim Source Files (for reference)

Key files if ReStim source is available at `../restim/`:
- `net/tcode.py` - T-code parsing
- `net/websocketserver.py` - WebSocket server, path routing
- `net/websocket_tcode.py` - T-code WS handler
- `qt_ui/tcode_command_router.py` - Routes parsed T-code to axes, remap logic
- `qt_ui/models/funscript_kit.py` - Default axis assignments (L0=alpha, L1=beta, V0=volume)
- `qt_ui/device_wizard/axes.py` - Axis enum and defaults
- `qt_ui/volume_control_widget.py` - Volume computation chain
- `qt_ui/widgets/threephase_widget.py` - Triangle display, coordinate conversion (`ab_to_item_pos`)
- `resources/phase diagram stereostim.svg` - Triangle SVG with L+/R+ labels
