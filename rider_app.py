"""rider_app.py — ReDrive Rider  (Windows GUI)

Standalone GUI for riders.  Enter a room code, click Connect.
No Python knowledge required.

Build to .exe:
    pip install pyinstaller
    pyinstaller rider.spec
"""

import asyncio
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
import sys

# ── Try aiohttp; show a friendly error if missing ────────────────────────────
try:
    import aiohttp
except ImportError:
    import tkinter.messagebox as mb
    root = tk.Tk(); root.withdraw()
    mb.showerror("Missing dependency",
                 "aiohttp is not installed.\n\nPlease run:\n  pip install aiohttp")
    sys.exit(1)

# ── Theme ─────────────────────────────────────────────────────────────────────
BG     = "#111111"
BG2    = "#1a1a1a"
BG3    = "#222222"
BORDER = "#333333"
FG     = "#ffffff"
FG2    = "#888888"
ACCENT = "#5fa3ff"
GREEN  = "#4caf50"
RED    = "#f44336"
YELLOW = "#ff9800"

DEFAULT_SERVER = "wss://redrive.estimstation.com"
DEFAULT_RESTIM = "ws://localhost:12346"
_VALID_CHARS   = set("BCDFGHJKMNPQRSTVWXYZ23456789")


# ── Async rider loop (runs in background thread) ──────────────────────────────

async def _rider_loop(room_code: str, server_url: str, restim_url: str,
                      log_q: queue.Queue, stop_ev: asyncio.Event):

    relay_url = f"{server_url.rstrip('/')}/room/{room_code}/rider"

    def log(msg):
        log_q.put_nowait(("log", msg))

    def status(state):        # "connecting" | "connected" | "disconnected" | "error"
        log_q.put_nowait(("status", state))

    while not stop_ev.is_set():
        status("connecting")
        log(f"Connecting to relay…")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(relay_url, heartbeat=30) as relay:
                    log("Relay connected.  Connecting to ReStim…")
                    try:
                        async with session.ws_connect(restim_url, heartbeat=30) as restim:
                            status("connected")
                            log("ReStim connected.  Receiving T-code.\n")
                            async for msg in relay:
                                if stop_ev.is_set():
                                    break
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        await restim.send_str(msg.data)
                                    except Exception as e:
                                        log(f"ReStim send error: {e}")
                                        break
                                elif msg.type in (aiohttp.WSMsgType.ERROR,
                                                  aiohttp.WSMsgType.CLOSE):
                                    break
                    except aiohttp.ClientConnectorError:
                        log(f"Could not reach ReStim at {restim_url}\n"
                            "Make sure ReStim is open with WebSocket enabled.")
                        status("error")
                        await asyncio.sleep(5)
                        continue

        except aiohttp.ClientConnectorError as e:
            log(f"Could not reach relay: {e}")
            status("error")
        except aiohttp.WSServerHandshakeError as e:
            if e.status == 404:
                log("Room not found — check the code and try again.")
            else:
                log(f"Relay error: {e}")
            status("error")
        except Exception as e:
            log(f"Unexpected error: {e}")
            status("error")

        if stop_ev.is_set():
            break
        status("connecting")
        log("Reconnecting in 5 s…")
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    status("disconnected")
    log("Disconnected.")


# ── Main GUI ──────────────────────────────────────────────────────────────────

class RiderApp:
    def __init__(self):
        self._thread:  threading.Thread | None = None
        self._loop:    asyncio.AbstractEventLoop | None = None
        self._stop_ev: asyncio.Event | None = None
        self._log_q:   queue.Queue = queue.Queue()
        self._running: bool = False

        self.root = tk.Tk()
        self.root.title("ReDrive Rider")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self.root.after(100, self._poll)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        st = ttk.Style(self.root)
        try: st.theme_use("clam")
        except Exception: pass
        st.configure("TFrame",   background=BG)
        st.configure("TLabel",   background=BG,  foreground=FG,  font=("Arial", 9))
        st.configure("TButton",  background=BG3, foreground=FG,
                     bordercolor=BORDER, relief="flat",
                     font=("Arial", 9), padding=[8, 4])
        st.map("TButton", background=[("active", "#333"), ("disabled", BG2)],
                          foreground=[("disabled", FG2)])
        st.configure("Connect.TButton", background=ACCENT, foreground="#000",
                     font=("Arial", 10, "bold"), padding=[12, 6])
        st.map("Connect.TButton", background=[("active", "#4a8fe0")])
        st.configure("Disconnect.TButton", background="#c0392b", foreground="#fff",
                     font=("Arial", 10, "bold"), padding=[12, 6])
        st.map("Disconnect.TButton", background=[("active", "#a93226")])

        pad = dict(padx=16, pady=8)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG2,
                       highlightbackground=BORDER, highlightthickness=1)
        hdr.pack(fill="x")
        tk.Label(hdr, text="ReDrive  Rider",
                 bg=BG2, fg=ACCENT,
                 font=("Arial", 14, "bold")).pack(side="left", padx=16, pady=10)
        self._dot = tk.Canvas(hdr, width=12, height=12, bg=BG2,
                              highlightthickness=0)
        self._dot_oval = self._dot.create_oval(2, 2, 11, 11, fill=FG2, outline="")
        self._dot.pack(side="right", padx=4)
        self._status_lbl = tk.Label(hdr, text="Not connected",
                                    bg=BG2, fg=FG2, font=("Arial", 9))
        self._status_lbl.pack(side="right", padx=4, pady=10)

        # ── Room code ─────────────────────────────────────────────────────────
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        tk.Label(body, text="Room Code", bg=BG, fg=FG2,
                 font=("Arial", 8)).grid(row=0, column=0, sticky="w", pady=(0, 2))

        self._code_var = tk.StringVar()
        self._code_var.trace_add("write", self._on_code_change)
        self._code_entry = tk.Entry(
            body, textvariable=self._code_var,
            font=("Courier New", 22, "bold"),
            bg=BG3, fg=ACCENT, insertbackground=ACCENT,
            relief="flat", bd=8,
            justify="center",
            width=12)
        self._code_entry.grid(row=1, column=0, columnspan=2, sticky="ew",
                              pady=(0, 10))
        self._code_entry.bind("<KeyRelease>", self._sanitise_code)

        # ── Connect button ────────────────────────────────────────────────────
        self._btn = ttk.Button(body, text="Connect",
                               style="Connect.TButton",
                               command=self._toggle,
                               state="disabled")
        self._btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        # ── Advanced (collapsible) ────────────────────────────────────────────
        adv_toggle = tk.Label(body, text="▸  Advanced settings",
                              bg=BG, fg=FG2, font=("Arial", 8),
                              cursor="hand2")
        adv_toggle.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self._adv_frame = tk.Frame(body, bg=BG)
        self._adv_visible = False
        adv_toggle.bind("<Button-1>", self._toggle_adv)

        for i, (lbl, attr, default) in enumerate([
            ("Relay server",   "_server_var", DEFAULT_SERVER),
            ("ReStim address", "_restim_var", DEFAULT_RESTIM),
        ]):
            tk.Label(self._adv_frame, text=lbl, bg=BG, fg=FG2,
                     font=("Arial", 8)).grid(row=i*2, column=0, sticky="w")
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            tk.Entry(self._adv_frame, textvariable=var,
                     bg=BG3, fg=FG, insertbackground=FG,
                     relief="flat", bd=6, font=("Arial", 9),
                     width=38).grid(row=i*2+1, column=0, sticky="ew",
                                    pady=(0, 6))

        body.columnconfigure(0, weight=1)

        # ── Log ───────────────────────────────────────────────────────────────
        log_frame = tk.Frame(self.root, bg=BG,
                             highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        self._log_box = scrolledtext.ScrolledText(
            log_frame,
            bg=BG2, fg=FG2,
            font=("Courier New", 8),
            relief="flat", bd=0,
            height=8, wrap="word",
            state="disabled")
        self._log_box.pack(fill="both", expand=True, padx=8, pady=6)
        self._log_box.tag_config("hi", foreground=FG)

        # ── Footer ────────────────────────────────────────────────────────────
        foot = tk.Frame(self.root, bg=BG2,
                        highlightbackground=BORDER, highlightthickness=1)
        foot.pack(fill="x")
        tk.Label(foot,
                 text="Your maximum power is always controlled on your ReStim device — "
                      "ReDrive only shapes the pattern.",
                 bg=BG2, fg=FG2, font=("Arial", 8),
                 wraplength=380, justify="left").pack(padx=14, pady=6)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _sanitise_code(self, _ev=None):
        raw = self._code_var.get().upper()
        clean = "".join(c for c in raw if c in _VALID_CHARS)[:10]
        if clean != raw:
            self._code_var.set(clean)
            self._code_entry.icursor(len(clean))

    def _on_code_change(self, *_):
        ok = len(self._code_var.get()) == 10
        self._btn.config(state="normal" if ok and not self._running else
                         ("normal" if self._running else "disabled"))

    def _toggle_adv(self, _ev=None):
        self._adv_visible = not self._adv_visible
        if self._adv_visible:
            self._adv_frame.grid(row=4, column=0, columnspan=2,
                                 sticky="ew", pady=(0, 4))
        else:
            self._adv_frame.grid_forget()

    def _log(self, msg: str):
        self._log_box.config(state="normal")
        self._log_box.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _set_status(self, state: str):
        colour = {
            "connecting":   YELLOW,
            "connected":    GREEN,
            "disconnected": FG2,
            "error":        RED,
        }.get(state, FG2)
        label = {
            "connecting":   "Connecting…",
            "connected":    "Connected",
            "disconnected": "Not connected",
            "error":        "Connection error",
        }.get(state, state)
        self._dot.itemconfig(self._dot_oval, fill=colour)
        self._status_lbl.config(text=label, fg=colour)

    # ── Connect / disconnect ──────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        code   = self._code_var.get().strip()
        server = self._server_var.get().strip()
        restim = self._restim_var.get().strip()

        self._running = True
        self._btn.config(text="Disconnect", style="Disconnect.TButton")
        self._code_entry.config(state="disabled")
        self._log(f"── Connecting to room {code} ──")

        def run():
            loop = asyncio.new_event_loop()
            self._loop    = loop
            self._stop_ev = asyncio.Event()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                _rider_loop(code, server, restim, self._log_q, self._stop_ev))
            loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _disconnect(self):
        if self._stop_ev and self._loop:
            self._loop.call_soon_threadsafe(self._stop_ev.set)
        self._running = False
        self._btn.config(text="Connect", style="Connect.TButton")
        self._code_entry.config(state="normal")
        self._on_code_change()

    # ── Poll log queue ────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                kind, data = self._log_q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self._set_status(data)
                    if data == "disconnected" and self._running:
                        # Engine finished — shouldn't happen, but reset UI
                        self._disconnect()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        self._disconnect()
        self.root.after(300, self.root.destroy)

    def run(self):
        # Centre on screen
        self.root.update_idletasks()
        w, h = 420, 520
        x = (self.root.winfo_screenwidth()  - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.mainloop()


if __name__ == "__main__":
    RiderApp().run()
