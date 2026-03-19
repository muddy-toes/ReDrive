function joinRider() {
  const c = document.getElementById('code-in').value.trim();
  if (c.length === 10) window.location = '/room/' + c + '/rider';
  else alert('Enter a 10-character room code');
}

function toggleFaq() {
  const body = document.getElementById('faq-body');
  const hint = document.getElementById('faq-hint');
  const open = body.classList.toggle('open');
  hint.innerHTML = open ? '&#9660; Hide' : '&#9654; Show';
}

// ── Public live sessions ──────────────────────────────────────────────────────
async function refreshPublicRooms() {
  try {
    const resp = await fetch('/api/rooms');
    const rooms = await resp.json();
    const section = document.getElementById('live-section');
    const el = document.getElementById('live-sessions-list');
    if (!rooms.length) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';
    let html = '<table class="live-table"><thead><tr>' +
      '<th>Room Code</th><th>Riders</th><th>Running</th><th></th>' +
      '</tr></thead><tbody>';
    for (const r of rooms) {
      html += `<tr>
        <td class="td-code">${r.code}</td>
        <td>${r.riders}</td>
        <td>${r.age_minutes}m</td>
        <td class="td-join"><a class="join-link" href="/room/${r.code}/rider">Join</a></td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch(_) {}
}
refreshPublicRooms();
setInterval(refreshPublicRooms, 30000);
