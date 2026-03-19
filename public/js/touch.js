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
