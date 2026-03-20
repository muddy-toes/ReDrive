const PATTERNS = ["Hold","Sine","Ramp \u2191","Ramp \u2193","Pulse","Burst","Random","Edge"];
let state = { pattern:"Hold", intensity:0, hz:0.5, depth:1.0,
              betaMode:"sweep", beta:5000, alpha:true };
let spiralTighten = false;

// ── Presets ───────────────────────────────────────────────────────────────────

function hzToSlider(hz) {
  return Math.max(1, Math.min(100, Math.round(100 * Math.sqrt(Math.max(0, (hz * 100 - 5) / 795)))));
}
function sweepHzToSlider(hz) {
  return Math.max(1, Math.min(200, Math.round(200 * Math.sqrt(Math.max(0, (hz * 100 - 2) / 498)))));
}

function syncUIFromState(d) {
  // Pattern
  state.pattern = d.pattern;
  document.querySelectorAll(".pat-btn").forEach(b =>
    b.classList.toggle("active", b.textContent === d.pattern));

  // Intensity
  let intPct = Math.round(d.intensity * 100);
  document.getElementById("intensity-slider").value = intPct;
  document.getElementById("int-val").textContent = intPct + "%";
  state.intensity = d.intensity;

  // Speed Hz
  let hzSlider = hzToSlider(d.hz);
  document.getElementById("hz-slider").value = hzSlider;
  let hzDisplay = Math.round(Math.pow(hzSlider/100, 2) * 795 + 5) / 100;
  document.getElementById("hz-val").textContent = hzDisplay.toFixed(2) + " Hz";
  state.hz = d.hz;

  // Depth
  let depthPct = Math.round(d.depth * 100);
  document.getElementById("depth-slider").value = depthPct;
  document.getElementById("depth-val").textContent = depthPct + "%";
  state.depth = d.depth;

  // Alpha
  state.alpha = d.alpha_on;
  let abtn = document.getElementById("alpha-toggle");
  abtn.classList.toggle("active", d.alpha_on);
  abtn.textContent = "\u03b1  Alpha oscillation: " + (d.alpha_on ? "ON" : "OFF");

  // Beta mode
  state.betaMode = d.beta_mode;
  document.querySelectorAll(".mode-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === d.beta_mode));
  document.getElementById("sweep-controls").style.display =
    d.beta_mode === "sweep" ? "block" : "none";
  document.getElementById("hold-controls").style.display =
    d.beta_mode === "hold"  ? "block" : "none";

  // Sweep Hz
  let swSlider = sweepHzToSlider(d.sweep_hz);
  document.getElementById("sweep-hz").value = swSlider;
  let swHz = Math.round(Math.pow(swSlider/200, 2) * 498 + 2) / 100;
  document.getElementById("sweep-hz-val").textContent = swHz.toFixed(2) + " Hz";

  // Sweep centre
  document.getElementById("sweep-centre").value = d.sweep_centre;
  document.getElementById("sweep-ctr-val").textContent = betaLabel(d.sweep_centre);

  // Sweep width
  document.getElementById("sweep-width").value = d.sweep_width;
  document.getElementById("sweep-width-val").textContent =
    Math.round(d.sweep_width / 49.99) + "%";

  // Sweep skew
  document.getElementById("sweep-skew").value = d.sweep_skew;
  document.getElementById("sweep-skew-val").textContent =
    d.sweep_skew === 0 ? "even"
      : d.sweep_skew < 0 ? "A +" + (-d.sweep_skew) + "%"
                          : "B +" + d.sweep_skew + "%";

  // Ramp (pre-fill without starting)
  let rampPct = Math.round(d.ramp_target * 100);
  document.getElementById("ramp-target").value = rampPct;
  document.getElementById("ramp-target-val").textContent = rampPct + "%";
  document.getElementById("ramp-duration").value = d.ramp_duration;
  onRampDur(d.ramp_duration);
  document.getElementById("ramp-progress-wrap").style.display =
    d.ramp_active ? "block" : "none";
}

// Build preset buttons from server's preset list
(async function buildPresetButtons() {
  try {
    const resp = await fetch(API_PREFIX + "/state");
    if (!resp.ok) return;
    const d = await resp.json();
    const presetRow = document.getElementById("preset-row");
    (d.presets || []).forEach(name => {
      const b = document.createElement("button");
      b.className = "preset-btn";
      b.textContent = "\u2605 " + name;
      b.onclick = () => loadPreset(name);
      presetRow.appendChild(b);
    });
  } catch(e) { console.warn("Could not load presets:", e); }
})();

async function loadPreset(name) {
  await sendCmd({ load_preset: name });
  const resp = await fetch(API_PREFIX + "/state");
  if (!resp.ok) return;
  const d = await resp.json();
  syncUIFromState(d);
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
    const r = await fetch(API_PREFIX + "/command", {
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
    const r = await fetch(API_PREFIX + "/state");
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
  const touchOnly = tab === 'touch' ? '' : 'none';
  document.getElementById('overlay-btn').style.display = touchOnly;
  document.getElementById('cursor-btn').style.display = touchOnly;
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
let _tcHovering   = false;
let _tcHoverX     = 0, _tcHoverY = 0;
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
      // Dark shadow for contrast against anatomy image
      ctx.shadowColor='rgba(0,0,0,0.8)';
      ctx.shadowBlur=4;
      ctx.strokeStyle=_tcPowerColor(power,0.55+power*0.40);
      ctx.lineWidth=1.5+power*4; ctx.lineCap='butt';
      ctx.beginPath(); ctx.moveTo(0,curY); ctx.lineTo(W,curY); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(curX,0); ctx.lineTo(curX,H); ctx.stroke();
      ctx.shadowColor='transparent';
      ctx.shadowBlur=0;
    } else {
      // Core dot
      ctx.beginPath(); ctx.arc(curX,curY,dotR,0,Math.PI*2);
      ctx.fillStyle=_tcPowerColor(power,0.55+power*0.40); ctx.fill();
      // Dark outline for contrast against anatomy image
      ctx.strokeStyle='rgba(0,0,0,0.7)';
      ctx.lineWidth=2; ctx.stroke();
      // Ring — thicker/harder at high power
      ctx.beginPath(); ctx.arc(curX,curY,dotR,0,Math.PI*2);
      ctx.strokeStyle=_tcPowerColor(power,0.85);
      ctx.lineWidth=1+power*2.5; ctx.stroke();
    }
    // % label (same for both modes) — floats above cursor point
    // Dark shadow for text contrast against anatomy image
    ctx.shadowColor='rgba(0,0,0,0.9)';
    ctx.shadowBlur=3;
    const pct=Math.round(power*100)+'%';
    ctx.fillStyle=_tcPowerColor(power,0.95);
    ctx.font=`bold ${11+Math.round(power*5)}px Arial`;
    ctx.textAlign='center'; ctx.textBaseline='bottom';
    ctx.fillText(pct,curX,curY-dotR-4);
    ctx.textBaseline='alphabetic';
    ctx.shadowColor='transparent';
    ctx.shadowBlur=0;
  }
  // Hover cursor - visible crosshair when pointer is over canvas but not pressed
  if (_tcHovering && !tcPointerDown && !_tcLooping) {
    const hx = _tcHoverX * W, hy = _tcHoverY * H;
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.6)';
    ctx.lineWidth = 1.5;
    ctx.shadowColor = 'rgba(0,0,0,0.8)';
    ctx.shadowBlur = 3;
    ctx.beginPath();
    ctx.moveTo(hx - 12, hy); ctx.lineTo(hx + 12, hy);
    ctx.moveTo(hx, hy - 12); ctx.lineTo(hx, hy + 12);
    ctx.stroke();
    ctx.restore();
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
  canvas.addEventListener('pointerenter', () => { _tcHovering = true; });
  canvas.addEventListener('pointerleave', () => { _tcHovering = false; requestAnimationFrame(tcDraw); });
  canvas.addEventListener('pointermove', e => {
    if (!tcPointerDown) {
      const r = canvas.getBoundingClientRect();
      _tcHoverX = (e.clientX - r.left) / r.width;
      _tcHoverY = (e.clientY - r.top) / r.height;
      requestAnimationFrame(tcDraw);
    }
  });
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
      const d=await(await fetch(API_PREFIX + '/state')).json();
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
  if (typeof DRIVER_KEY === 'undefined' || !_ROOM_CODE) return;
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = wsProto + '//' + location.host + '/room/' + _ROOM_CODE + '/driver-ws?key=' + encodeURIComponent(DRIVER_KEY);
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
