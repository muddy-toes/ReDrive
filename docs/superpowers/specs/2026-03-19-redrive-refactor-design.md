# ReDrive Refactor: Tests, Preset Fix, Template Extraction, AppImage

## Context

ReDrive is a Python/aiohttp remote-control interface for ReStim e-stim devices. The codebase works but has accumulated technical debt: zero test coverage, duplicated preset data between Python and JS, ~4500 lines of HTML/CSS/JS embedded as Python string constants, and no Linux distributable. This refactor addresses all four issues in a dependency-ordered sequence.

**bd-lite issues:** redrive-9ki (tests), redrive-3kh (presets), redrive-5hg (templates), redrive-b1o (AppImage)

---

## Step 1: Add Test Suite (redrive-9ki)

### Goal
Establish pytest infrastructure and write tests covering the core engine, room lifecycle, and HTTP routes. These tests serve as the safety net for all subsequent refactoring.

### Framework
- pytest + pytest-aiohttp + pytest-asyncio
- `requirements-dev.txt`: pytest, pytest-aiohttp, pytest-asyncio

### File Structure
```
pytest.ini
requirements-dev.txt
tests/
  __init__.py
  conftest.py
  test_pattern_engine.py
  test_drive_engine.py
  test_room.py
  test_server_routes.py
```

### conftest.py Fixtures
- **tkinter mock** - replicate server.py lines 25-27 (`sys.modules["tkinter"] = MagicMock()`) before any imports
- **`drive_config`** - returns `DriveConfig()` with defaults
- **`shared_dict`** - pre-populated `dict` with keys: `__live__l0`, `__live__l1`, `__live__l2`, `__ramp_progress__` (all 0.0)
- **`log_queue`** - `queue.Queue()`
- **`drive_engine`** - `DriveEngine(cfg, shared, log_q, send_hook=captured_list.append)` without calling `.start()` (no background thread)
- **`pattern_engine`** - bare `PatternEngine()`
- **`aiohttp_app`** - calls `build_app()` from server.py; clears global `_rooms` dict in setup/teardown

### Test Cases

**test_pattern_engine.py** (PatternEngine, redrive.py:143-260):
| Test | Validates |
|------|-----------|
| `test_hold_returns_constant` | Hold pattern returns steady intensity regardless of tick |
| `test_sine_oscillates` | Sine output varies over a full cycle (tick enough to complete 1/hz seconds) |
| `test_ramp_up_increases` | Ramp Up output increases over time |
| `test_ramp_down_decreases` | Ramp Down output decreases over time |
| `test_pulse_alternates` | Pulse produces high/low alternation |
| `test_burst_has_gaps` | Burst output has active and silent phases |
| `test_random_varies` | Random output changes between ticks |
| `test_edge_approaches_peak` | Edge pattern approaches but pulls back from peak |
| `test_set_command_changes_pattern` | `set_command({"pattern": "Sine"})` changes `pattern` attribute |
| `test_set_command_changes_intensity` | `set_command({"intensity": 0.5})` clamps to [0, 1] |
| `test_stop_resets` | `stop()` resets intensity to 0 and phase (pattern name is NOT reset) |
| `test_depth_zero_is_flat` | With depth=0, output equals intensity regardless of pattern |
| `test_tick_output_range` | Output of `tick()` is always in [0.0, 1.0] for all patterns |

**test_drive_engine.py** (DriveEngine, redrive.py:2440-2964):
| Test | Validates |
|------|-----------|
| `test_pattern_command` | `_handle_command_data({"pattern": "Sine"})` sets pattern on internal engine |
| `test_intensity_command` | `_handle_command_data({"intensity": 0.75})` sets intensity |
| `test_hz_command` | Sets pattern speed |
| `test_depth_command` | Sets depth |
| `test_stop_command` | `{"stop": true}` zeros intensity, deactivates ramp and gesture |
| `test_ramp_start` | `{"ramp": {"target": 1.0, "duration": 60}}` activates ramp |
| `test_ramp_stop` | `{"ramp_stop": true}` deactivates ramp |
| `test_beta_mode_command` | `{"beta_mode": "sweep"}` sets mode |
| `test_beta_sweep_params` | `{"beta_sweep": {"hz": 1.0, "centre": 5000, "width": 2000, "skew": 0.5}}` sets params with clamping |
| `test_preset_load` | `{"load_preset": "Milking"}` applies all Milking preset values |
| `test_preset_load_unknown` | `{"load_preset": "Nonexistent"}` is a no-op |
| `test_state_endpoint_fields` | `_handle_state()` response JSON contains all expected keys |
| `test_state_after_preset` | After loading Milking preset, state reflects preset values (only fields currently in state: pattern, intensity, beta_mode, sweep params, alpha_on, ramp_target/duration; NOT hz/depth until Step 2) |
| `test_sweep_hz_envelope_after_preset` | After loading Milking preset (which has sweep_hz_envelope), verify envelope is initialized (indirectly via sweep_hz changing over ticks) |

**test_room.py** (Room, server.py:55-155):
| Test | Validates |
|------|-----------|
| `test_room_creation` | Room has code, driver_key, engine |
| `test_room_not_expired_fresh` | Fresh room is not expired |
| `test_room_expired_24h` | Room > 24h old is expired (mock monotonic) |
| `test_room_expired_driver_idle` | Room with driver_last_seen > 5min ago is expired |
| `test_touch_driver_resets` | `touch_driver()` prevents idle expiry |
| `test_waiting_room_no_engine` | Waiting room has `engine=None` |
| `test_rider_count` | Reflects number of items in `rider_wss` |

**test_server_routes.py** (HTTP routes, server.py):
| Test | Validates |
|------|-----------|
| `test_create_room` | POST `/create` returns redirect, room exists in `_rooms` |
| `test_driver_page_requires_key` | GET `/room/{code}` without key returns 403 |
| `test_driver_page_with_key` | GET `/room/{code}?key=...` returns 200 + HTML |
| `test_command_requires_auth` | POST `/room/{code}/command` without X-Driver-Key returns 403 |
| `test_command_sets_pattern` | POST with valid key + `{"pattern":"Sine"}` returns 200 |
| `test_state_requires_auth` | GET `/room/{code}/state` without key returns 403 |
| `test_state_returns_json` | GET with key returns JSON with all expected fields |
| `test_rider_state_no_auth` | GET `/room/{code}/rider-state` returns 200 without auth |
| `test_room_not_found` | GET `/room/BADCODE/state` returns 404 |
| `test_command_stop` | POST `{"stop": true}` + subsequent state shows intensity=0 |
| `test_preset_load_roundtrip` | POST `{"load_preset": "Milking"}` then GET state verifies values match PRESETS dict |

---

## Step 2: Fix Preset Sync (redrive-3kh)

### Goal
Eliminate the duplicated JS_PRESETS. Server is the single source of truth; client syncs UI from state endpoint.

### Changes to redrive.py

**Add `hz`, `depth`, and `spiral_hz` to `_handle_state`** (~line 2728):
```python
"hz":            self._pattern.hz,
"depth":         self._pattern.depth,
"spiral_hz":     self._beta_sweep_hz if self._beta_mode == "spiral" else None,
```

**Remove sync comments** on lines 58-60 and 783-785.

### Changes to DRIVER_HTML JavaScript

**Delete `JS_PRESETS`** (lines 787-802) and the preset-row builder that reads from it (lines 804-812).

**Add inverse slider math functions:**
```javascript
function hzToSlider(hz)      { return Math.max(1, Math.min(100, Math.round(100 * Math.sqrt(Math.max(0, (hz * 100 - 5) / 795))))); }
function sweepHzToSlider(hz) { return Math.max(1, Math.min(200, Math.round(200 * Math.sqrt(Math.max(0, (hz * 100 - 2) / 498))))); }
```

**Add `syncUIFromState(d)` function** that takes a state JSON object and updates:
- Pattern button highlight: `d.pattern`
- Intensity slider + label: `Math.round(d.intensity * 100)`
- Hz slider + label: `hzToSlider(d.hz)` + forward formula for display
- Depth slider + label: `Math.round(d.depth * 100)`
- Alpha toggle: `d.alpha_on`
- Beta mode buttons + show/hide sweep/hold controls: `d.beta_mode`
- Sweep hz slider: `sweepHzToSlider(d.sweep_hz)` (needs inverse formula, NOT direct)
- Sweep centre/width sliders: direct from state (slider range matches state values)
- Sweep skew slider: direct from state (already `int(skew * 100)`, matches slider range -100..100)
- Ramp target + duration sliders: `Math.round(d.ramp_target * 100)`, `d.ramp_duration`
- Ramp progress display: show/hide based on `d.ramp_active`

**Rewrite `loadPreset(name)`:**
```javascript
async function loadPreset(name) {
  await sendCmd({ load_preset: name });
  const resp = await fetch(STATE_URL, { headers: { "X-Driver-Key": DRIVER_KEY } });
  const d = await resp.json();
  syncUIFromState(d);
}
```

**Build preset buttons dynamically** from `presets` array in the initial `/state` fetch on page load.

### Tests (TDD)
Write these tests BEFORE implementation:
- `test_state_has_hz_and_depth` - verifies the new fields exist
- `test_preset_load_state_roundtrip` - load preset, fetch state, verify all values match PRESETS dict including hz and depth
- `test_sync_inverse_math` - verify hz inverse formula round-trips: `hzToSlider(forwardHz(v)) ~= v` for representative slider values

---

## Step 3: Extract HTML/JS/CSS into Jinja2 Templates (redrive-5hg)

### Goal
Extract all embedded HTML from Python strings into Jinja2 template files. Extract inline CSS and JS into separate static files. Configure static file serving.

### Dependencies
- `jinja2` (add to requirements)
- `aiohttp-jinja2` (add to requirements)

### Directory Structure
```
templates/
  driver.html
  touch.html
  landing.html
  rider_join.html
  waiting.html
  anatomy_maker.html
public/
  css/
    driver.css
    touch.css
    landing.css
    rider_join.css
    waiting.css
    anatomy_maker.css
  js/
    driver.js
    touch.js
    landing.js
    rider_join.js
    waiting.js
    anatomy_maker.js
  img/
    bottle.png
template_env.py              # shared Jinja2 Environment helper
```

### HTML Blocks to Extract

| Block | Source | Template | Variables |
|-------|--------|----------|-----------|
| `DRIVER_HTML` | redrive.py:264-2013 | driver.html | `api_prefix`, `driver_key`, `room_code`, `room_banner` (see Banner Partial below) |
| `TOUCH_HTML` | redrive.py:2018-2436 | touch.html | `api_prefix`, `room_code`, `show_room_code_btn` (see Touch Injections below) |
| `_LANDING_HTML` | server.py:327-686 | landing.html | (none) |
| `_RIDER_PAGE_HTML` | server.py:159-297 | rider_join.html | `code`, `prefix` |
| `_WAITING_HTML` | server.py:1107-1217 | waiting.html | `code`, `invite_url`, `ms_remaining` |
| `_ANATOMY_MAKER_HTML` | server.py:1304-1637 | anatomy_maker.html | (none) |

### Replacing `_inject_prefix()` (server.py:302-324)

This function does brittle string replacements for API paths. Kill it entirely. In each template, define variables at the top:

```html
<script>
  const API_PREFIX = "{{ api_prefix }}";
  {% if driver_key %}const DRIVER_KEY = "{{ driver_key }}";{% endif %}
  {% if room_code %}const ROOM_CODE = "{{ room_code }}";{% endif %}
</script>
```

JS code references `API_PREFIX + "/command"` instead of hardcoded `"/command"`.

### Banner Partial (driver.html)

The room banner (currently built as a ~70-line Python f-string in server.py:722-796 and injected via `.replace("<body>", ...)`) must become a Jinja2 partial: `templates/_partials/room_banner.html`. It contains room code display, copy-to-clipboard, privacy toggle, rider count polling, and driver name input. In `driver.html`:

```html
{% if room_code %}
  {% include "_partials/room_banner.html" %}
{% endif %}
```

The partial receives variables: `room_code`, `api_prefix`, `touch_url`. For LAN mode (redrive.py), the banner is not rendered (no room_code).

### Touch Page Injections (touch.html)

Server.py:800-818 does TWO injections beyond `_inject_prefix`:
1. `ROOM_CODE` constant injected into `<head>` (covered by the variable-defining `<script>` block)
2. A `<script>` block before `</body>` that sets room-code-btn visibility and text

Both become template logic in `touch.html`:
```html
{% if room_code %}
<script>
  document.getElementById("room-code-btn").style.display = "inline-block";
  document.getElementById("room-code-btn").textContent = "Room: {{ room_code }}";
</script>
{% endif %}
```

### Heartbeat Preservation

`_inject_prefix()` also injects a 60-second heartbeat ping (`setInterval(()=>fetch("ping",{method:"POST"}),60000)`) into the driver page's `</head>`. Without this, driver sessions expire after 5 minutes of inactivity even with the page open. In the Jinja2 template, this goes in the variable-defining `<script>` block:

```html
<script>
  const API_PREFIX = "{{ api_prefix }}";
  {% if driver_key %}
  const DRIVER_KEY = "{{ driver_key }}";
  setInterval(() => fetch(API_PREFIX + "/ping", {method: "POST", headers: {"X-Driver-Key": DRIVER_KEY}}), 60000);
  {% endif %}
</script>
```

### Phased Extraction (3 sub-phases, tests run between each)

1. **Phase A:** Move HTML strings to template files with inline CSS/JS intact. Update Python to render via Jinja2. Verify tests pass.
2. **Phase B:** Extract `<style>` blocks to `public/css/*.css` files. Replace with `<link>` tags. Verify tests pass.
3. **Phase C:** Extract `<script>` blocks to `public/js/*.js` files. Replace with `<script src>` tags (after the variable-defining `<script>` block). Verify tests pass.

### Template Loading

**Shared helper** (`template_env.py`):
```python
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

def get_jinja_env(template_dir: Path = None) -> Environment:
    if template_dir is None:
        template_dir = Path(__file__).parent / "templates"
    return Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
```

**server.py:** Uses `aiohttp_jinja2.setup(app, loader=FileSystemLoader(...))`. Handlers use `aiohttp_jinja2.render_template("driver.html", request, context)`.

**redrive.py (LAN mode):** Uses raw `get_jinja_env().get_template("driver.html").render(context)` and returns `web.Response(text=html, content_type="text/html")`.

### Import Change (server.py line 31)

Before: `from redrive import DriveEngine, DriveConfig, DRIVER_HTML, TOUCH_HTML, PRESETS`
After: `from redrive import DriveEngine, DriveConfig, PRESETS`

### Static File Serving

**nginx** (`server/nginx.conf`):
```nginx
location /public/ {
    alias /opt/redrive/public/;
    expires 1h;
    add_header Cache-Control "public, immutable";
}
```

**aiohttp fallback** (both server.py and redrive.py):
```python
app.router.add_static("/public", Path(__file__).parent / "public")
```

**`touch_assets/`** remains served by aiohttp handlers (custom upload/subdirectory logic).

**`bottle.png`** moves to `public/img/bottle.png`. Update `handle_bottle_png` and all references.

### Handling `.format()` Pages

`_RIDER_PAGE_HTML` and `_WAITING_HTML` currently use Python `.format()` with `{{`/`}}` to escape JS braces. When converting to Jinja2, revert all `{{` back to `{` and `}}` back to `}` since Jinja2's delimiters (`{{ }}` for expressions, `{% %}` for blocks) don't collide with JS single braces.

`_LANDING_HTML` and `_ANATOMY_MAKER_HTML` are static (no `.format()` call) and use normal single braces in their JS. No brace conversion needed for these.

### Risks
- **Highest risk:** Breaking driver.js during extraction. Mitigation: literal copy-paste of JS, only change is replacing hardcoded API paths with `API_PREFIX` variable. Diff rendered output before/after.
- **Dual WebSocket route:** server.py has TWO `add_get` calls for `/room/{code}/rider` (one for HTML, one for WS upgrade). aiohttp resolves by first-match for HTTP, second for WS. This is fragile; be aware when modifying route setup but don't change the behavior.
- **PyInstaller builds unaffected:** rider_app.py does not serve HTML, so template/static extraction doesn't touch the build pipeline.

---

## Step 4: Linux AppImage Build (redrive-b1o)

### Goal
Create a distributable Linux AppImage for `rider_app.py` alongside the existing Windows and macOS builds.

### Tooling
**python-appimage** (niess/python-appimage) - bundles complete CPython + pip packages. Chosen because it includes tkinter by default, which is the primary pain point of PyInstaller on Linux.

### New Files

**`build/build_linux.sh`:**
1. Download python-appimage base (Python 3.11, manylinux2014, x86_64)
2. Extract base AppImage to `AppDir/`
3. Install aiohttp into AppDir's Python
4. Copy `rider_app.py` and `rider_client.py` to `AppDir/usr/src/`
5. Create custom `AppRun` entry point
6. Add `.desktop` file and icon
7. Build with `appimagetool` -> `ReDriveRider-x86_64.AppImage`

**`.github/workflows/build-linux.yml`:**
- Triggers: tag push (`v*`) + manual `workflow_dispatch`
- Runs on `ubuntu-22.04`
- Executes `build/build_linux.sh`
- Uploads artifact

### Updates to Existing Files
- `version.json` - add `download_linux` URL
- `rider_app.py` - add `IS_LINUX = platform.system() == "Linux"` and update `_check_update()` key selection to 3-way conditional (currently only has `IS_MAC` / fallback to windows)
- `build/README.md` - add Linux build instructions (if this file exists)

### Production Requirements File
Create `requirements.txt` with `aiohttp` (currently only mentioned in README/setup.sh, no formal file). Step 3 will add `jinja2` and `aiohttp-jinja2` to it.

### Independence
This step has zero dependencies on steps 1-3. It can run in a parallel worktree immediately.

---

## Execution Strategy

### Agent Team with Worktrees

```
Agent 1: Step 1 (tests)       ~/claudescratch/step1-tests/      branch: step1-tests
Agent 2: Step 4 (appimage)    ~/claudescratch/step4-appimage/    branch: step4-appimage
  -- parallel, both start immediately --

Agent 3: Step 2 (presets)     ~/claudescratch/step2-presets/     branch: step2-presets
  -- starts after step 1 merges --

Agent 4: Step 3 (templates)   ~/claudescratch/step3-templates/   branch: step3-templates
  -- starts after step 2 merges --
```

### Merge Order
1. Step 1 (purely additive, no production code changes)
2. Step 2 (modifies redrive.py: state endpoint + DRIVER_HTML JS)
3. Step 4 (purely additive, new build files)
4. Step 3 (major refactor: touches both redrive.py and server.py)

### TDD Throughout
Each step writes failing tests first, then implements to make them pass. Step 1 establishes the infrastructure; steps 2-4 add step-specific tests before implementation.

### Verification
After each merge:
- `cd /home/skm/ossm/ReDrive && python -m pytest tests/ -v`
- Manual smoke test: `python server/server.py --port 8765`, open browser, create room, verify driver page loads and controls work
- For step 3 specifically: diff rendered HTML output before/after to catch extraction errors
