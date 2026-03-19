function copyInvite() {
  navigator.clipboard.writeText(INVITE_URL).then(() => {
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = "Copied!";
    btn.style.background = "var(--ok)";
    setTimeout(() => { btn.textContent = orig; btn.style.background = ""; }, 1500);
  });
}

// Countdown timer
function updateCountdown() {
  const ms = EXPIRES_AT - Date.now();
  if (ms <= 0) {
    document.getElementById('cd-timer').textContent = "Expired";
    return;
  }
  const mins = Math.floor(ms / 60000);
  const secs = Math.floor((ms % 60000) / 1000);
  document.getElementById('cd-timer').textContent =
    mins + ":" + String(secs).padStart(2, "0");
}
setInterval(updateCountdown, 1000);
updateCountdown();

// Poll for driver claim
async function pollStatus() {
  try {
    const d = await (await fetch(STATUS_URL)).json();
    if (d.claimed && d.touch_url) {
      window.location = d.touch_url;
      return;
    }
  } catch(_) {}
  setTimeout(pollStatus, 3000);
}
pollStatus();
