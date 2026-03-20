#!/usr/bin/env python3
"""ReDrive Rider — connects to a relay room and forwards T-code to local ReStim."""

import asyncio
import argparse
import json
import platform
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from pathlib import Path
import urllib.request
import urllib.error
import webbrowser

import aiohttp

APP_VERSION  = "0.1.0"
UPDATE_URL   = "https://redrive.estimstation.com/version.json"
RELAY_HOST   = "redrive.estimstation.com"

IS_MAC   = platform.system() == "Darwin"
IS_WIN   = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# ── Colours ───────────────────────────────────────────────────────────────────
BG     = "#111111"
BG2    = "#1a1a1a"
BORDER = "#2a2a2a"
FG     = "#ffffff"
FG2    = "#999999"
ACC    = "#5fa3ff"
OK     = "#4caf50"
ERR    = "#f44336"
WARN   = "#ff9800"

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_DIR = Path.home() / ".redrive"
CONFIG_FILE = CONFIG_DIR / "config.json"

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except:
        return {}

def save_config(data):
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


class RiderApp:
    def __init__(self, root: tk.Tk, room_code: str = ""):
        self.root = root
        self.root.title("ReDrive Rider")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_ev: asyncio.Event | None = None
        self._connected = False

        self._config = load_config()

        self._build_ui(room_code)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Version check runs in a daemon thread — silently ignored on failure
        threading.Thread(target=self._check_update, daemon=True).start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self, room_code: str):
        ENTRY_KW = dict(
            bg="#222222", fg=FG, insertbackground=FG,
            relief="flat", font=("Helvetica", 12),
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACC,
        )

        # ── Title bar ─────────────────────────────────────────────────────────
        title_row = tk.Frame(self.root, bg=BG)
        title_row.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(title_row, text="ReDrive Rider", bg=BG, fg=FG,
                 font=("Helvetica", 17, "bold")).pack(side="left")
        tk.Label(title_row, text=f"v{APP_VERSION}", bg=BG, fg=FG2,
                 font=("Helvetica", 10)).pack(side="right", anchor="s", pady=(0, 2))

        # ── Update banner (hidden until a newer version is found) ─────────────
        self._update_banner = tk.Frame(self.root, bg="#7c4d00", cursor="hand2")
        # pack() is called lazily inside _check_update → show_update_banner
        inner = tk.Frame(self._update_banner, bg="#7c4d00")
        inner.pack(fill="x", padx=10, pady=6)
        self._update_lbl = tk.Label(inner, text="", bg="#7c4d00", fg="#ffe082",
                                    font=("Helvetica", 10, "bold"))
        self._update_lbl.pack(side="left")
        self._dl_btn = tk.Button(inner, text="Download", relief="flat",
                                 bg=WARN, fg="#000",
                                 font=("Helvetica", 9, "bold"),
                                 cursor="hand2",
                                 command=self._open_download)
        self._dl_btn.pack(side="right")
        self._update_url = ""

        # ── Room code ─────────────────────────────────────────────────────────
        self._add_label("Room Code:")
        rc_row = tk.Frame(self.root, bg=BG)
        rc_row.pack(fill="x", padx=16, pady=(2, 10))
        self._room_var = tk.StringVar(value=room_code.upper())
        room_entry = tk.Entry(rc_row, textvariable=self._room_var,
                              width=14, justify="center", **ENTRY_KW)
        room_entry.pack(side="left", ipady=6, fill="x", expand=True)
        self._connect_btn = tk.Button(rc_row, text="Connect",
                                      bg=ACC, fg="#000",
                                      activebackground="#4090ee",
                                      font=("Helvetica", 11, "bold"),
                                      relief="flat", cursor="hand2",
                                      command=self._toggle_connect)
        self._connect_btn.pack(side="right", padx=(8, 0), ipady=6, ipadx=10)

        # ── ReStim URL ────────────────────────────────────────────────────────
        self._add_label("ReStim:")
        self._restim_var = tk.StringVar(value="ws://localhost:12346")
        tk.Entry(self.root, textvariable=self._restim_var, **ENTRY_KW).pack(
            fill="x", padx=16, ipady=5, pady=(2, 8))

        # ── Server URL ────────────────────────────────────────────────────────
        self._add_label("Server:")
        self._relay_var = tk.StringVar(value=f"wss://{RELAY_HOST}")
        tk.Entry(self.root, textvariable=self._relay_var, **ENTRY_KW).pack(
            fill="x", padx=16, ipady=5, pady=(2, 10))

        # ── My Anatomy Overlay ────────────────────────────────────────────────
        self._add_label("My overlay:")
        overlay_row = tk.Frame(self.root, bg=BG)
        overlay_row.pack(fill="x", padx=16, pady=(2, 10))

        stored_path = self._config.get("anatomy_path", "")
        if stored_path and Path(stored_path).is_file():
            display_name = Path(stored_path).name
        else:
            display_name = "No overlay set"

        self._overlay_lbl = tk.Label(
            overlay_row, text=display_name,
            bg="#222222", fg=FG2,
            font=("Helvetica", 10),
            anchor="w", relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            width=28,
        )
        self._overlay_lbl.pack(side="left", ipady=4, fill="x", expand=True)

        tk.Button(overlay_row, text="Set...", relief="flat",
                  bg=BG2, fg=FG2, activebackground=BORDER,
                  font=("Helvetica", 9), cursor="hand2",
                  command=self._pick_overlay).pack(side="left", padx=(6, 2), ipady=4, ipadx=6)

        tk.Button(overlay_row, text="Clear", relief="flat",
                  bg=BG2, fg=FG2, activebackground=BORDER,
                  font=("Helvetica", 9), cursor="hand2",
                  command=self._clear_overlay).pack(side="left", padx=(0, 0), ipady=4, ipadx=6)

        # ── My Wiring (collapsible) ───────────────────────────────────────────
        self._wiring_expanded = False

        wiring_section = tk.Frame(self.root, bg=BG)
        wiring_section.pack(fill="x", padx=16, pady=(0, 4))

        self._wiring_toggle = tk.Button(
            wiring_section, text="▶ My Wiring",
            bg=BG, fg=FG2, activebackground=BG, activeforeground=FG,
            font=("Helvetica", 10), relief="flat", bd=0,
            cursor="hand2", anchor="w",
            command=self._toggle_wiring,
        )
        self._wiring_toggle.pack(fill="x")

        self._wiring_frame = tk.Frame(wiring_section, bg=BG2,
                                      highlightthickness=1,
                                      highlightbackground=BORDER)
        # starts hidden
        self._wiring_frame.grid_remove()

        CHANNEL_OPTIONS = ["Red", "Blue", "Neutral", "Green"]
        CHANNEL_TO_NUM  = {"Red": "1", "Blue": "2", "Neutral": "3", "Green": "4"}

        saved_wiring = self._config.get("wiring", {})

        self._wiring_vars: dict[str, tk.StringVar] = {}
        for row_idx, (position, default) in enumerate(
            [("tip", "Blue"), ("balls", "Neutral"), ("anus", "Red")]
        ):
            saved_name = next(
                (name for name, num in CHANNEL_TO_NUM.items()
                 if num == saved_wiring.get(position, CHANNEL_TO_NUM[default])),
                default,
            )
            var = tk.StringVar(value=saved_name)
            self._wiring_vars[position] = var

            tk.Label(
                self._wiring_frame,
                text=position.capitalize(),
                bg=BG2, fg=FG2,
                font=("Helvetica", 10),
                width=7, anchor="w",
            ).grid(row=row_idx, column=0, padx=(10, 4), pady=4, sticky="w")

            cb = ttk.Combobox(
                self._wiring_frame,
                textvariable=var,
                values=CHANNEL_OPTIONS,
                state="readonly",
                width=9,
            )
            cb.grid(row=row_idx, column=1, padx=(0, 10), pady=4, sticky="w")
            cb.bind("<<ComboboxSelected>>",
                    lambda _e: self._save_wiring())

        self._CHANNEL_TO_NUM = CHANNEL_TO_NUM

        # ── Status row ────────────────────────────────────────────────────────
        status_row = tk.Frame(self.root, bg=BG)
        status_row.pack(fill="x", padx=16, pady=(2, 10))
        tk.Label(status_row, text="Status:", bg=BG, fg=FG2,
                 font=("Helvetica", 11)).pack(side="left")
        self._dot = tk.Label(status_row, text="●", bg=BG, fg=FG2,
                             font=("Helvetica", 13))
        self._dot.pack(side="left", padx=(8, 4))
        self._status_lbl = tk.Label(status_row, text="Not connected",
                                    bg=BG, fg=FG2,
                                    font=("Helvetica", 11))
        self._status_lbl.pack(side="left")

        # ── Driver / poppers indicators ──────────────────────────────────────
        info_row = tk.Frame(self.root, bg=BG)
        info_row.pack(fill="x", padx=16, pady=(0, 6))
        self._driver_indicator = tk.Label(
            info_row, text="Driver: unknown", bg=BG, fg=FG2,
            font=("Helvetica", 9))
        self._driver_indicator.pack(side="left")
        self._poppers_lbl = tk.Label(
            info_row, text="", bg=BG, fg=WARN,
            font=("Helvetica", 9, "bold"))
        self._poppers_lbl.pack(side="right")

        # ── Log area ──────────────────────────────────────────────────────────
        log_outer = tk.Frame(self.root, bg=BORDER, bd=1, relief="flat")
        log_outer.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        self._log = tk.Text(log_outer, bg="#0d0d0d", fg=FG2,
                            font=("Courier", 9), state="disabled",
                            height=10, width=48, relief="flat", wrap="word")
        sb = tk.Scrollbar(log_outer, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True)

        # ── Clear Log button ──────────────────────────────────────────────────
        tk.Button(self.root, text="Clear Log", relief="flat",
                  bg=BG2, fg=FG2, activebackground=BORDER,
                  font=("Helvetica", 9), cursor="hand2",
                  command=self._clear_log).pack(
            anchor="e", padx=16, pady=(0, 14))

        self.root.minsize(360, 540)

    def _add_label(self, text: str):
        tk.Label(self.root, text=text, bg=BG, fg=FG2,
                 font=("Helvetica", 10)).pack(anchor="w", padx=16)

    # ── Wiring helpers ────────────────────────────────────────────────────────

    def _toggle_wiring(self):
        self._wiring_expanded = not self._wiring_expanded
        if self._wiring_expanded:
            self._wiring_toggle.config(text="▼ My Wiring")
            self._wiring_frame.pack(fill="x", pady=(2, 4))
        else:
            self._wiring_toggle.config(text="▶ My Wiring")
            self._wiring_frame.pack_forget()

    def _save_wiring(self):
        self._config["wiring"] = {
            pos: self._CHANNEL_TO_NUM[var.get()]
            for pos, var in self._wiring_vars.items()
        }
        save_config(self._config)

    def _get_wiring_payload(self) -> dict:
        return {
            pos: self._CHANNEL_TO_NUM[var.get()]
            for pos, var in self._wiring_vars.items()
        }

    # ── Overlay helpers ───────────────────────────────────────────────────────

    def _pick_overlay(self):
        path = filedialog.askopenfilename(
            title="Select anatomy overlay image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp"),
                       ("All files", "*.*")],
        )
        if not path:
            return
        self._config["anatomy_path"] = path
        save_config(self._config)
        self._overlay_lbl.config(text=Path(path).name, fg=FG)

    def _clear_overlay(self):
        self._config.pop("anatomy_path", None)
        save_config(self._config)
        self._overlay_lbl.config(text="No overlay set", fg=FG2)

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_line(self, msg: str):
        def _do():
            self._log.configure(state="normal")
            self._log.insert("end", msg + "\n")
            self._log.see("end")
            self._log.configure(state="disabled")
        self.root.after(0, _do)

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_status(self, text: str, color: str):
        def _do():
            self._dot.config(fg=color)
            self._status_lbl.config(text=text)
        self.root.after(0, _do)

    # ── JSON message handlers ────────────────────────────────────────────────

    def _on_driver_status(self, data):
        connected = data.get("connected", False)
        name = data.get("name", "Anonymous") or "Anonymous"
        color = "#4ade80" if connected else "#f43f5e"
        text = f"Driver: {name}" if connected else "Driver: disconnected"
        self.root.after(0, lambda: self._driver_indicator.config(
            text=text, foreground=color))

    def _on_bottle_status(self, data):
        if data.get("active"):
            remaining = int(data.get("remaining", 0))
            mode = data.get("mode", "normal").replace("_", " ").title()
            self.root.after(0, lambda: self._poppers_lbl.config(
                text=f"POPPERS ({mode}) - {remaining}s"))
        else:
            self.root.after(0, lambda: self._poppers_lbl.config(text=""))

    def _on_rider_state(self, data):
        pass  # Placeholder for future use

    # ── Connect / disconnect ──────────────────────────────────────────────────

    def _toggle_connect(self):
        if self._connected or (self._thread and self._thread.is_alive()):
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        room = self._room_var.get().strip().upper()
        if not room:
            self._log_line("Enter a room code first.")
            return
        relay  = self._relay_var.get().strip().rstrip("/")
        restim = self._restim_var.get().strip()
        self._set_status("Connecting...", WARN)
        self.root.after(0, lambda: self._connect_btn.config(
            text="Disconnect", bg=ERR, activebackground="#b71c1c"))
        self._loop   = asyncio.new_event_loop()
        self._stop_ev = asyncio.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(room, relay, restim),
            daemon=True,
        )
        self._thread.start()

    def _disconnect(self):
        if self._stop_ev and self._loop:
            self._loop.call_soon_threadsafe(self._stop_ev.set)
        self._connected = False
        self._set_status("Not connected", FG2)
        self.root.after(0, lambda: self._connect_btn.config(
            text="Connect", bg=ACC, activebackground="#4090ee"))

    # ── Anatomy upload ────────────────────────────────────────────────────────

    async def _upload_anatomy(self, relay_host: str, room: str):
        path = self._config.get("anatomy_path", "")
        if not path or not Path(path).is_file():
            return
        upload_url = f"https://{relay_host}/room/{room}/upload_anatomy"
        self._log_line("Uploading anatomy overlay...")
        try:
            data = aiohttp.FormData()
            data.add_field(
                "file",
                open(path, "rb"),
                filename=Path(path).name,
                content_type="image/png",
            )
            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, data=data) as resp:
                    if resp.status in (200, 201, 204):
                        self._log_line("Anatomy overlay uploaded ✓")
                    else:
                        text = await resp.text()
                        self._log_line(f"Anatomy upload failed ({resp.status}): {text[:120]}")
        except Exception as e:
            self._log_line(f"Anatomy upload error: {e}")

    # ── Async rider loop (runs in background thread) ──────────────────────────

    def _run_loop(self, room: str, relay: str, restim: str):
        self._loop.run_until_complete(self._rider_loop(room, relay, restim))

    async def _rider_loop(self, room: str, relay: str, restim: str):
        relay_url       = f"{relay}/room/{room}/rider-ws"
        RECONNECT_DELAY = 5.0

        # Derive relay_host from the relay URL (strip scheme)
        relay_host = relay.split("://", 1)[-1].rstrip("/")

        while not self._stop_ev.is_set():
            self._log_line("Connecting to relay…")
            self._set_status("Connecting...", WARN)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(relay_url) as relay_ws:
                        self._log_line("Connected to relay. Connecting to ReStim…")
                        self._set_status("Connected to relay", ACC)

                        # Upload anatomy overlay after successful WebSocket handshake
                        await self._upload_anatomy(relay_host, room)

                        # Send wiring config so server knows physical channel mapping
                        wiring_msg = json.dumps({
                            "type": "rider_wiring",
                            "wiring": self._get_wiring_payload(),
                        })
                        await relay_ws.send_str(wiring_msg)
                        self._log_line("Wiring config sent.")

                        try:
                            async with session.ws_connect(restim) as restim_ws:
                                self._connected = True
                                self._log_line("Connected to ReStim. Forwarding T-code.")
                                self._set_status("Live — forwarding T-code", OK)
                                async for msg in relay_ws:
                                    if self._stop_ev.is_set():
                                        break
                                    if msg.type == aiohttp.WSMsgType.TEXT:
                                        if msg.data.startswith('{'):
                                            try:
                                                data = json.loads(msg.data)
                                                msg_type = data.get("type")
                                                if msg_type == "driver_status":
                                                    self._on_driver_status(data)
                                                elif msg_type == "bottle_status":
                                                    self._on_bottle_status(data)
                                                elif msg_type == "rider_state":
                                                    self._on_rider_state(data)
                                            except json.JSONDecodeError:
                                                pass
                                            continue
                                        try:
                                            await restim_ws.send_str(msg.data)
                                        except Exception:
                                            break
                                    elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                                      aiohttp.WSMsgType.ERROR):
                                        break
                        except Exception as e:
                            self._log_line(f"ReStim error: {e}")
                            self._set_status(f"Error: {e}", ERR)
            except Exception as e:
                self._log_line(f"Relay error: {e}")
                self._set_status(f"Error: {e}", ERR)

            if self._stop_ev.is_set():
                break
            self._connected = False
            self._set_status(f"Reconnecting in {int(RECONNECT_DELAY)}s…", ERR)
            try:
                await asyncio.wait_for(self._stop_ev.wait(), timeout=RECONNECT_DELAY)
            except asyncio.TimeoutError:
                pass

        self._connected = False
        self._set_status("Not connected", FG2)
        self.root.after(0, lambda: self._connect_btn.config(
            text="Connect", bg=ACC, activebackground="#4090ee"))

    # ── Update check ──────────────────────────────────────────────────────────

    def _check_update(self):
        try:
            with urllib.request.urlopen(UPDATE_URL, timeout=5) as r:
                data = json.loads(r.read())
            latest = data.get("version", "0.0.0")
            if (tuple(int(x) for x in latest.split(".")) >
                    tuple(int(x) for x in APP_VERSION.split("."))):
                if IS_MAC:
                    key = "download_mac"
                elif IS_LINUX:
                    key = "download_linux"
                else:
                    key = "download_windows"
                url = data.get(key) or data.get("download_windows", "")
                self._show_update_banner(latest, url)
        except Exception:
            pass  # silently ignore if can't reach server

    def _show_update_banner(self, version: str, url: str):
        self._update_url = url

        def _do():
            self._update_lbl.config(text=f"New version v{version} available")
            # Insert banner between title and room-code row
            self._update_banner.pack(fill="x", padx=16, pady=(6, 0))
            # Re-pack the banner just after the title widgets
            self._update_banner.pack_configure(before=self.root.winfo_children()[1])
        self.root.after(0, _do)

    def _open_download(self):
        if self._update_url:
            webbrowser.open(self._update_url)

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        self._disconnect()
        self.root.after(200, self.root.destroy)


def main():
    parser = argparse.ArgumentParser(description="ReDrive Rider")
    parser.add_argument("room", nargs="?", default="",
                        help="Room code (optional — can be typed in the app)")
    args = parser.parse_args()

    root = tk.Tk()
    RiderApp(root, room_code=args.room)
    root.mainloop()


if __name__ == "__main__":
    main()
