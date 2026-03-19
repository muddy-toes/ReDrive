(function () {
  const PREVIEW_W = 280, PREVIEW_H = 700;
  const EXPORT_W  = 400, EXPORT_H  = 1000;

  const canvas    = document.getElementById('preview');
  const ctx       = canvas.getContext('2d');
  const slScale   = document.getElementById('sl-scale');
  const slRotate  = document.getElementById('sl-rotate');
  const lblScale  = document.getElementById('lbl-scale');
  const lblRotate = document.getElementById('lbl-rotate');
  const dlBtn     = document.getElementById('dl-btn');

  let userImg   = null;
  let overlayImg = null;
  let panX = PREVIEW_W / 2;   // 140
  let panY = PREVIEW_H / 2;   // 350
  let zoom = 1.0;
  let rotRad = 0;

  // ── Load overlay image ───────────────────────────────────────────────────
  (function loadOverlay() {
    const img = new Image();
    img.onload = () => { overlayImg = img; redraw(); };
    img.onerror = () => { overlayImg = null; redraw(); };
    img.src = '/touch_assets/anatomy/anatomyexampleOVERLAY.png';
  })();

  // ── Fallback outline ─────────────────────────────────────────────────────
  function drawFallbackOutline(c, W, H) {
    c.save();
    c.globalAlpha = 0.55;
    c.strokeStyle = '#aaaacc';
    c.lineWidth = 2;
    // Glans
    c.beginPath();
    c.ellipse(W/2, H*0.08, W*0.22, H*0.06, 0, 0, Math.PI*2);
    c.stroke();
    // Shaft
    c.beginPath();
    c.moveTo(W/2 - W*0.12, H*0.13);
    c.lineTo(W/2 - W*0.10, H*0.42);
    c.moveTo(W/2 + W*0.12, H*0.13);
    c.lineTo(W/2 + W*0.10, H*0.42);
    c.stroke();
    // Left testicle
    c.beginPath();
    c.ellipse(W/2 - W*0.22, H*0.50, W*0.18, H*0.10, -0.2, 0, Math.PI*2);
    c.stroke();
    // Right testicle
    c.beginPath();
    c.ellipse(W/2 + W*0.22, H*0.50, W*0.18, H*0.10, 0.2, 0, Math.PI*2);
    c.stroke();
    // Perineum/anus region
    c.beginPath();
    c.ellipse(W/2, H*0.80, W*0.08, H*0.04, 0, 0, Math.PI*2);
    c.stroke();
    c.restore();
  }

  // ── Redraw ───────────────────────────────────────────────────────────────
  function redraw() {
    ctx.clearRect(0, 0, PREVIEW_W, PREVIEW_H);

    // Layer 1: user photo
    if (userImg) {
      ctx.save();
      ctx.translate(panX, panY);
      ctx.rotate(rotRad);
      ctx.scale(zoom, zoom);
      ctx.drawImage(userImg, -userImg.naturalWidth / 2, -userImg.naturalHeight / 2);
      ctx.restore();
    }

    // Layer 2: anatomy outline at 60% opacity
    if (overlayImg && overlayImg.complete && overlayImg.naturalWidth > 0) {
      ctx.save();
      ctx.globalAlpha = 0.6;
      ctx.drawImage(overlayImg, 0, 0, PREVIEW_W, PREVIEW_H);
      ctx.restore();
    } else {
      drawFallbackOutline(ctx, PREVIEW_W, PREVIEW_H);
    }
  }

  // ── File input ───────────────────────────────────────────────────────────
  document.getElementById('photo-input').addEventListener('change', function () {
    const file = this.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = e => {
      const img = new Image();
      img.onload = () => {
        userImg = img;
        // Auto-fit: scale so the image fills the canvas height
        const fitScale = PREVIEW_H / img.naturalHeight;
        zoom = fitScale;
        slScale.value = Math.round(zoom * 100);
        lblScale.textContent = slScale.value + '%';
        panX = PREVIEW_W / 2;
        panY = PREVIEW_H / 2;
        rotRad = 0;
        slRotate.value = 0;
        lblRotate.textContent = '0\u00b0';
        dlBtn.disabled = false;
        redraw();
      };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  });

  // ── Sliders ──────────────────────────────────────────────────────────────
  slScale.addEventListener('input', function () {
    zoom = parseInt(this.value) / 100;
    lblScale.textContent = this.value + '%';
    redraw();
  });

  slRotate.addEventListener('input', function () {
    rotRad = parseInt(this.value) * Math.PI / 180;
    lblRotate.textContent = this.value + '\u00b0';
    redraw();
  });

  // ── Mouse drag ───────────────────────────────────────────────────────────
  let dragging = false, dragStartX = 0, dragStartY = 0, panStartX = 0, panStartY = 0;

  canvas.addEventListener('mousedown', e => {
    dragging = true;
    dragStartX = e.clientX; dragStartY = e.clientY;
    panStartX = panX; panStartY = panY;
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    panX = panStartX + (e.clientX - dragStartX);
    panY = panStartY + (e.clientY - dragStartY);
    redraw();
  });
  window.addEventListener('mouseup', () => { dragging = false; });

  // ── Mouse wheel zoom ─────────────────────────────────────────────────────
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    zoom *= Math.pow(1.001, e.deltaY);
    zoom = Math.max(0.1, Math.min(5.0, zoom));
    slScale.value = Math.round(zoom * 100);
    lblScale.textContent = slScale.value + '%';
    redraw();
  }, { passive: false });

  // ── Touch (pan + pinch) ──────────────────────────────────────────────────
  let lastTouches = [];

  canvas.addEventListener('touchstart', e => {
    e.preventDefault();
    lastTouches = Array.from(e.touches);
  }, { passive: false });

  canvas.addEventListener('touchmove', e => {
    e.preventDefault();
    const touches = Array.from(e.touches);

    if (touches.length === 1 && lastTouches.length >= 1) {
      // Pan
      const dx = touches[0].clientX - lastTouches[0].clientX;
      const dy = touches[0].clientY - lastTouches[0].clientY;
      panX += dx; panY += dy;
      redraw();
    } else if (touches.length === 2 && lastTouches.length >= 2) {
      // Pinch-zoom
      const prevDist = Math.hypot(
        lastTouches[0].clientX - lastTouches[1].clientX,
        lastTouches[0].clientY - lastTouches[1].clientY);
      const newDist = Math.hypot(
        touches[0].clientX - touches[1].clientX,
        touches[0].clientY - touches[1].clientY);
      if (prevDist > 0) {
        zoom *= newDist / prevDist;
        zoom = Math.max(0.1, Math.min(5.0, zoom));
        slScale.value = Math.round(zoom * 100);
        lblScale.textContent = slScale.value + '%';
        redraw();
      }
    }

    lastTouches = touches;
  }, { passive: false });

  canvas.addEventListener('touchend', e => {
    lastTouches = Array.from(e.touches);
  }, { passive: false });

  // ── Export ───────────────────────────────────────────────────────────────
  window.downloadOverlay = function () {
    const scale = EXPORT_W / PREVIEW_W; // 1.4286
    const off = document.createElement('canvas');
    off.width = EXPORT_W; off.height = EXPORT_H;
    const octx = off.getContext('2d');

    // Draw photo
    if (userImg) {
      octx.save();
      octx.translate(panX * scale, panY * scale);
      octx.rotate(rotRad);
      octx.scale(zoom * scale, zoom * scale);
      octx.drawImage(userImg, -userImg.naturalWidth / 2, -userImg.naturalHeight / 2);
      octx.restore();
    }

    // Draw overlay at full opacity
    octx.globalAlpha = 1.0;
    if (overlayImg && overlayImg.complete && overlayImg.naturalWidth > 0) {
      octx.drawImage(overlayImg, 0, 0, EXPORT_W, EXPORT_H);
    } else {
      drawFallbackOutline(octx, EXPORT_W, EXPORT_H);
    }

    off.toBlob(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'redrive-overlay.png';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
    }, 'image/png');
  };

  // Initial draw (shows outline only until photo loaded)
  redraw();
})();
