# ReDrive - AI Development Guide

## What This Is

ReDrive is a remote-control interface for ReStim-compatible e-stim devices. A **driver** controls patterns, intensity, and electrode position via a browser UI. **Riders** receive the signal on their local ReStim device, also via browser. Communication flows over WebSocket.

## Architecture

```
engine.py     (919 lines)  Pattern engine + ReStim connection (no HTTP, no UI)
server.py     (1042 lines) Unified server: --local for LAN, default for relay
public/js/    driver.js (1350), touch.js (439) - browser UI
templates/    Jinja2 templates for all pages
```

**Two modes:**
- `python server.py --local` - LAN mode. Auto-creates one room, engine connects to local ReStim directly. Driver at `http://localhost:8765`, rider at the printed URL.
- `python server.py` - Relay mode. Multi-room server for VPS deployment. Riders connect to ReStim from their browser via the ReStim Bridge (WebSocket from browser to `ws://localhost:12346/tcode`).

**Deleted in simplify-architecture branch:** `redrive.py` (tkinter monolith), `rider_app.py` (tkinter rider), all build scripts. Everything is browser-based now.

## Branch Status

- **`main`** - Stable. Has WebSocket unification but still has old tkinter code.
- **`simplify-architecture`** - Active development branch. 25 commits ahead of main. Eliminates tkinter, unifies server, adds browser ReStim bridge. Needs Rusty's review before merge. 105 tests passing.

## Key Files

| File | What it does |
|------|-------------|
| `engine.py` | `DriveConfig`, `PatternEngine`, `DriveEngine`. Pattern generation, T-code output, ReStim WS connection. No HTTP. |
| `server.py` | Room management, WS handlers (driver + rider), state push loop, HTTP routes, Jinja2 rendering. |
| `public/js/driver.js` | Driver controls: patterns, intensity, sweep, spiral, touch panel, gesture recording. |
| `public/js/touch.js` | Rider page: power meter, emotes, poppers overlay, ReStim bridge, avatar upload, stop/resume. |
| `rider_client.py` | Headless CLI bridge (88 lines). Forwards T-code from relay to local ReStim. |
| `template_env.py` | Shared Jinja2 Environment helper. |
| `redrive_config.json` | Server config (axis mappings, ReStim URL, touch images, overlay). Gitignored. |

## T-Code Axis Mapping (CRITICAL)

ReDrive sends three T-code axes to ReStim. Getting these wrong means controls do the wrong thing silently.

| ReDrive config key | Default | ReStim axis | What it controls |
|---|---|---|---|
| `axis_volume` | `V0` | VOLUME_API | Output volume (0=silent, 9999=max) |
| `axis_alpha` | `L0` | POSITION_ALPHA | Alpha electrode position |
| `axis_beta` | `L1` | POSITION_BETA | Beta electrode position |

**Common mistake:** Assuming L0 = volume. It's NOT. L0 = alpha position. V0 = volume.

See `docs/restim-protocol.md` for the complete ReStim protocol reference including value parsing, remap formulas, and the volume computation chain.

## Beta Position Convention

```
T-code 9999 = L+ (left electrode)   -- ReStim beta +1
T-code 5000 = Centre (neutral)      -- ReStim beta  0
T-code 0    = R+ (right electrode)  -- ReStim beta -1
```

ReDrive's UI shows L+ on the left side of the screen and R+ on the right, with indicator dots inverted (`1 - beta/9999`) to match this visual convention. The T-code values themselves are correct.

## WebSocket Protocol

All communication is WebSocket-based. Messages are JSON with a `type` field except T-code which is raw strings.

**Driver -> Server:** `{type: "command", data: {...}}`, `{type: "ping"}`
**Server -> Driver:** `{type: "state", data: {...}}` at 5Hz, `{type: "participants_update", ...}`, `{type: "command_ack", ok: bool}`
**Server -> Rider:** Raw T-code strings, `{type: "rider_state", intensity: ..., bottle_active: ...}` at 5Hz, `{type: "driver_status", connected: bool, name: "..."}`, `{type: "bottle_status", ...}`, `{type: "participants_update", ...}`
**Rider -> Server:** `{type: "set_name", name: "..."}`, `{type: "set_avatar", data: "data:image/..."}`, `{type: "like", emoji: "..."}`, `{type: "ping"}`

HTTP endpoints (`/command`, `/state`, `/rider-state`) still exist for backward compatibility but are not polled by the browser.

## Touch Panel / Gesture System

The touch panel on the driver page lets you draw patterns:
- **Y axis** = electrode position (beta). Top = L+, bottom = R+.
- **X axis** = intensity within a 25% sliding window of the base power slider.
- **Drawing and releasing** records a gesture that loops on the engine.
- **"Touch" beta mode** plays the gesture. Switching to other beta modes pauses (preserves) the gesture. Switching back or clicking "Resume" re-engages it.

## Rider Avatar System

- Riders upload a photo via the browser. It's resized client-side (max 512px, JPEG, <400KB).
- Stored in `localStorage('reDriveAnatomyB64')` for persistence across sessions.
- Sent to server via WS `set_avatar` message on connect. Server stores in memory on the participant record.
- Broadcast to all clients via `participants_update`. No filesystem storage.
- Rider WS `max_msg_size` is 1MB to accommodate avatar data.

## Config System

`redrive_config.json` (gitignored, auto-created from defaults):
- T-code axis mappings, ReStim URL, port
- Beta positions, alpha oscillation parameters
- `touch_images`: array of `{name, filename}` for the touch panel image selector. Files in `touch_assets/anatomy/`.
- `overlay_image`: transparent PNG overlay filename in `touch_assets/anatomy/`.
- Server admin edits this file. Changes picked up on restart (or by `/touch_config` endpoint).

## Testing

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

105 tests across 9 test files. Tests use `aiohttp_client` for HTTP/WS integration testing. No tkinter mock needed (engine.py has no tkinter dependency).

## Running

```bash
pip install aiohttp jinja2 aiohttp-jinja2

# LAN mode (single room, connects to local ReStim):
python server.py --local

# Relay mode (multi-room, VPS deployment):
python server.py --port 8765
```

## Known Issues / Open Items

1. **Rider avatar on rider's own page** - sometimes shows the default anatomy image instead of the uploaded avatar. The rendering code checks `p.avatar` first and looks correct. May be a timing issue with the initial participants_update arriving before the set_avatar message. Needs more testing.

2. **Touch X-axis feels like it does nothing** - it does work, but only adjusts intensity within a 25% window of the base power slider. At low base power, the change is subtle. This is by design but might confuse users. Consider making the window wider or adding visual feedback.

3. **Build scripts deleted** - The AppImage/Windows/Mac builds packaged the old `rider_app.py` which no longer exists. If distributable builds are needed, new build scripts for `server.py --local` would need to be created.

4. **`deploy/` directory** - Contains nginx.conf, systemd service, setup.sh for VPS deployment. Was renamed from `server/` to avoid Python import conflict. Deployment configs reference `server.py` at project root.

5. **Poppers modes** - Normal mode countdown works. Deep Huff and Double Hit modes have visual implementations in touch.js but haven't been thoroughly tested end-to-end.

## Don't

- Don't use L0 for volume. It's alpha position. V0 is volume.
- Don't set `heartbeat` on WS connections to ReStim. Qt WebSocket doesn't respond to PING frames.
- Don't store rider avatars on the filesystem. They go in memory on the Room's participant records.
- Don't hardcode touch panel image categories. They come from `touch_images` in the config file.
- Don't assume beta 0 = "left". In T-code/ReStim terms, 0 = R+ and 9999 = L+.
