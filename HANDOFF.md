# ReDrive — Co-Dev Handoff

## What it is

ReDrive is a **remote-control bridge** for ReStim e-stim devices. A **driver** (the person in control) opens a web UI in any browser. One or more **riders** (the people receiving stimulation) run a local Python app that receives T-code commands from the driver over the internet via a hosted relay server.

The rider never needs to touch controls — the driver handles everything. Riders can see each other, send emote reactions, and receive a "Poppers" prompt overlay triggered by the driver.

---

## Architecture

```
Rider PC (local)           Hosted Server (VPS)         Driver (any browser)
─────────────────          ───────────────────         ────────────────────
rider_app.py               server/server.py            /room/CODE
  └─ connects via WS ───►  /room/CODE/rider  ◄──────── /room/CODE  (driver page)
  └─ sends T-code to       relays T-code to            /room/CODE/rider (rider status page)
     local ReStim device   rider WS clients
```

### Key files

| File | Purpose |
|---|---|
| `redrive.py` | **Monolith** — contains the pattern engine, the entire DRIVER_HTML (driver web UI), and TOUCH_HTML (rider status page). Served via aiohttp. |
| `server/server.py` | Relay server — room management, WebSocket handling, anatomy uploads, API routes |
| `rider_app.py` | Rider's local GUI app (tkinter) — connects to server, outputs T-code to device |
| `rider_client.py` | WebSocket client used by rider_app |
| `touch_assets/anatomy/` | Built-in anatomy PNGs: `hunk1.png`, `hunk2.png`, `hunk3.png`, `furry1.png` |

---

## Server routes

```
GET  /                          Landing page
POST /create                    Create a new room → returns {code, driver_key}
GET  /room/{code}               Driver page (DRIVER_HTML, auth via driver_key cookie)
GET  /room/{code}/rider         Rider status page (TOUCH_HTML) + rider WebSocket upgrade
GET  /room/{code}/driver-ws     Driver WebSocket (auth: ?key=DRIVER_KEY) — receives participants_update
GET  /room/{code}/state         Full state (driver-auth required)
GET  /room/{code}/rider-state   Public state for riders: {intensity, bottle_active, bottle_remaining, bottle_mode}
POST /room/{code}/command       Send pattern/intensity commands (driver-auth)
POST /room/{code}/bottle        Trigger poppers overlay (driver-auth)
POST /room/{code}/upload_anatomy  Rider uploads their custom avatar PNG
GET  /room/{code}/participants  Current participant list (no auth)
GET  /touch_assets/{type}/{name}  Serve anatomy images
GET  /bottle.png                Poppers overlay image
```

---

## Driver page (`/room/CODE`) — `DRIVER_HTML` in `redrive.py`

### Always-visible top bar
- **RESTIM DRIVE** gradient title + connection dot
- Driver name input
- Room code copy + Rider Link copy buttons
- Poppers mode selector (Normal / Deep Huff / Double Hit)
- **■ STOP** button (red, always accessible)
- **🍾 Poppers** button (yellow glow when active)

### Left column (both tabs)
- Rider avatar cards — portrait aspect ratio, name label via `::after`, sorted custom-first
- Guide toggle (`GUIDE ON/OFF`) — **Touch tab only**
- Cursor toggle (`DOT` / `GRID`) — **Touch tab only**

### Tab 1 — Controls
- Live visualisation: waveform canvas + triangle canvas + beta position tracker
- Pattern selector grid: Sine, Hold, Ramp ↑, Ramp ↓, Pulse, Burst, Random, Edge
- Intensity slider (large thumb)
- Speed slider
- Depth slider
- Ramp section: target %, duration, Start/Stop with progress bar
- Beta (electrode sweep): AutoSweep / Spiral / Hold modes, speed, centre, width, skew
- Alpha oscillation toggle

### Tab 2 — Touch
- Portrait anatomy canvas (max 340px wide, `aspect-ratio: 9/16`)
- Touch/drag to set position (Y axis) and intensity (X axis)
- **Base Power slider** — green→yellow→orange→red gradient, sets intensity floor
  - Math: `intensity = slider*0.75 + 0.25*x` — slider sets floor (0–75%), X always adds 25% range above it
- **Category buttons** (right side): HUNK / TOON / FURRY
  - Clicking loads first image in that category
  - Auto-cycles every 10 minutes through all images in category
  - If a rider has uploaded a custom image, their image takes priority
- Power-aware cursor: dot or crosshair (grid) mode
  - Dot: radius = `min(W,H) * (0.06 + power*0.10)`
  - Grid: full-span crosshair lines, `lineWidth = 1.5 + power*4`
  - Both: power-colored (green→yellow→orange→red), % label floats above
- Guide overlay toggle (semi-transparent reference image)
- Trail — fading power-colored dots showing recent drag path
- Loop mode (continuous pattern)

---

## Rider page (`/room/CODE/rider`) — `TOUCH_HTML` in `redrive.py`

- Connection status dot (red = disconnected, green = live)
- Rider name input (sent to server via WebSocket `set_name` message)
- **Driven by: [driver name]** banner
- Other riders' avatar cards (with names)
- **Power bar** — 48px tall, gradient, pulses when live. Polls `/rider-state` every 1.2s (no auth needed)
- **Emote grid** — 6 buttons: 😍 ⚡ 💦 🔥 👋 😈 — sends `like` WS message
- **Poppers overlay** — full-screen takeover triggered by bottle_active in `/rider-state`
- **📷 My Pic** button — opens file picker, uploads PNG to server as rider avatar
- Footer: room code copy button

---

## WebSocket flow

### Rider connects
1. `rider_app.py` opens WS to `/room/CODE/rider`
2. Server assigns slot: `{name: "Rider N", anatomy: "hunk1.png", idx: N}`
3. Server broadcasts `participants_update` to **all** `rider_wss` + `driver_wss`
4. Driver page receives update → `renderParticipants()` rebuilds rider column
5. Rider types name → `set_name` message → another `participants_update` broadcast

### Driver WebSocket (`/room/CODE/driver-ws`)
- Authenticated via `?key=DRIVER_KEY`
- On connect: immediately receives current participant list
- Receives `participants_update` whenever a rider joins/leaves/renames

### T-code flow
- Driver page POSTs to `/room/CODE/command` (driver-auth via `X-Driver-Key` header, injected by server into every `fetch()`)
- Server calls engine, outputs T-code string
- T-code sent to all `rider_wss` connections
- `rider_app.py` receives and writes to local serial/COM port

---

## Auth model

- Driver key is generated on room creation, injected into DRIVER_HTML as `const DRIVER_KEY="..."` and patched into every `fetch()` as `X-Driver-Key` header
- Riders have **no auth** — the rider page is public, uses room code only
- `/rider-state` is intentionally public (no auth) so riders can poll intensity

---

## Design system

```css
--bg: #0a0a0a          /* near-black */
--bg3: #1a1a1a         /* card backgrounds */
--glass: rgba(20,20,35,0.75)   /* frosted glass panels */
--border: rgba(95,163,255,0.20)
--accent: #5fa3ff      /* blue */
--accent-glow: #5fa3ff44
--err: #f43f5e         /* red (stop, disconnect) */
--warn: #fbbf24        /* yellow (beta dot, poppers active) */
--ok: #4ade80          /* green */
body background: linear-gradient(160deg, #0a0a0a 0%, #1a2333 100%) fixed
```

Panels use `backdrop-filter: blur(20px)` glass morphism. Rider cards are portrait `aspect-ratio: 58/80` with name via `::after { content: attr(data-name) }`.

---

## Known pending work / feature list

### High priority
- [ ] **Rider page fully tested end-to-end** — power bar, poppers overlay, emote reactions
- [ ] **Rider custom avatar upload** — `📷 My Pic` uploads to server, auto-uploads on rejoin if stored in localStorage
- [ ] **Driver touch panel: auto-select rider image** — if only one rider has a custom image, load it into the canvas automatically
- [ ] **Controls tab: verify no scrollbar** — all content should fit without overflow-y

### Design / UX
- [ ] Controls tab layout still needs review — spacing may still push content out of view on smaller screens
- [ ] Rider column: consider a "no riders yet" placeholder state when empty
- [ ] Emote animations — floating emoji reaction when driver receives a like

### Infrastructure
- [ ] Route naming: `/room/{code}/rider` serves TOUCH_HTML (rider status page) **and** is the WebSocket upgrade endpoint — these currently coexist via content-negotiation but should be verified
- [ ] `redrive.py` vs `server/server.py` — the local `redrive.py` has its own embedded HTTP server (aiohttp); the hosted relay is `server/server.py`. They share DRIVER_HTML/TOUCH_HTML templates but are separate deployments. The local app is for direct LAN use; the server is for internet relay.

BUGS: -The driver hasen't yet confirled seeing riders as either custom images or default ones
- the UI is kinda big and ugly
- a drivers UI elements  unified across
- oh and write installers for the drivers to download and add their own pics in it so the custom pics stay local because privacy also i don't want people uploading random stuff and clogging my droplet 
## Running locally (LAN mode)

```bash
pip install aiohttp
python redrive.py
# Open http://localhost:8080 in browser as driver
```

## Running the relay server

```bash
cd server
pip install aiohttp
python server.py
# Driver opens https://yourserver.com/
# Rider runs rider_app.py pointed at yourserver.com
```
