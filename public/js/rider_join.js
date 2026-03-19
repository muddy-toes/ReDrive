let errCount = 0;
async function poll() {
  try {
    const d = await (await fetch(STATE_URL)).json();
    errCount = 0;
    document.getElementById('status-dot').style.background = 'var(--ok)';
    document.getElementById('status-txt').textContent = 'live';
    document.getElementById('s-pattern').textContent   = d.pattern  ?? '\u2014';
    document.getElementById('s-intensity').textContent = d.intensity != null
      ? Math.round(d.intensity * 100) + '%' : '\u2014';
    const volPct = d.vol != null ? Math.round(d.vol * 100) : 0;
    document.getElementById('vol-bar').style.width = volPct + '%';
    document.getElementById('s-vol').textContent   = volPct + '%';
    const rampRow = document.getElementById('ramp-row');
    if (d.ramp_active) {
      rampRow.style.display = 'flex';
      const pct = Math.round((d.ramp_progress ?? 0) * 100);
      document.getElementById('ramp-bar').style.width = pct + '%';
      document.getElementById('s-ramp').textContent   =
        pct + '% \u2192 ' + Math.round((d.ramp_target ?? 0) * 100) + '%';
    } else {
      rampRow.style.display = 'none';
    }
    const overlay = document.getElementById('bottle-overlay');
    if (d.bottle_active) {
      overlay.style.display = 'flex';
      document.getElementById('bottle-cd').textContent = d.bottle_remaining + 's';
    } else {
      overlay.style.display = 'none';
    }
  } catch(e) {
    errCount++;
    if (errCount > 2) {
      document.getElementById('status-dot').style.background = 'var(--err)';
      document.getElementById('status-txt').textContent = 'disconnected';
    }
  }
}
poll();
setInterval(poll, 1500);
