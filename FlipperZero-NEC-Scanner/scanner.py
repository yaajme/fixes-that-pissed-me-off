#!/usr/bin/env python3
"""Standard NEC scanner for Flipper's USB CLI. Python 3 + pyserial + Tk."""
import csv
import datetime
import queue
import re
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog


def scan_codes(start_address=0x71, all_addresses=True, start_command=0):
    """Wrap addresses once; the first address starts at start_command."""
    addresses = [(start_address + n) % 256 for n in range(256 if all_addresses else 1)]
    return [(a, c) for i, a in enumerate(addresses)
            for c in range(start_command if i == 0 else 0, 256)]


def ir_file(address, command):
    return ("Filetype: IR signals file\nVersion: 1\n#\n"
            f"name: NEC_{address:02X}_{command:02X}\ntype: parsed\nprotocol: NEC\n"
            f"address: {address:02X} 00 00 00\ncommand: {command:02X} 00 00 00\n")


def clean_response(data):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", data.decode("utf-8", errors="replace"))


def has_prompt(response):
    # Firmware versions differ: the user's Momentum build returns a bare '#'.
    # Restrict '#' to its own line so ordinary output containing it isn't a prompt. Gay shit n stuff
    lines = response.rstrip().splitlines()
    if not lines:
        return False
    last_line = lines[-1].strip()
    return last_line == "#" or last_line.endswith(">:")


def read_prompt(port, timeout=6):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        data.extend(port.read(port.in_waiting or 1))
        response = clean_response(data)
        if has_prompt(response):
            return response
    raise RuntimeError("No Flipper CLI prompt. Close qFlipper/mobile sessions, return to the "
                       "Flipper home screen and reconnect.\n" + clean_response(data)[-500:])


class Scanner:
    def __init__(self, root):
        self.root = root
        self.port = None
        self.busy = False
        self.running = False
        self.codes = []
        self.index = 0
        self.history = []
        self.events = queue.Queue()
        self.timer = None
        self.log = None
        self.writer = None
        root.title("Flipper NEC Scanner 1.1")
        root.geometry("850x650")
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Flipper NEC scanner", font=("Arial", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Connect by USB, close qFlipper, and leave Flipper on its home screen.").pack(anchor="w", pady=6)
        row = ttk.Frame(frame); row.pack(fill="x", pady=4)
        ttk.Label(row, text="Port:").pack(side="left")
        self.port_name = tk.StringVar()
        self.ports_box = ttk.Combobox(row, textvariable=self.port_name, width=22)
        self.ports_box.pack(side="left", padx=6)
        ttk.Button(row, text="Refresh ports", command=self.refresh).pack(side="left")
        ttk.Button(row, text="Connect", command=self.connect).pack(side="left", padx=6)
        row = ttk.Frame(frame); row.pack(fill="x", pady=8)
        self.address = tk.StringVar(value="71")
        self.command = tk.StringVar(value="00")
        self.delay = tk.StringVar(value="1.0")
        self.all_addresses = tk.BooleanVar(value=True)
        for label, variable in [("Start address (hex)", self.address), ("Command (hex)", self.command), ("Gap (seconds)", self.delay)]:
            ttk.Label(row, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(row, textvariable=variable, width=6).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(frame, text="Scan all 256 NEC addresses (65,536 codes from command 00)", variable=self.all_addresses).pack(anchor="w")
        ttk.Label(frame, text="All-address scan wraps FF → 00. Changes to range apply with New scan; gap changes apply live.").pack(anchor="w", pady=4)
        row = ttk.Frame(frame); row.pack(fill="x", pady=10)
        for label, callback in [("New scan", self.new_scan), ("Pause", self.pause), ("Resume", self.resume), ("Next only", self.step), ("Test Right 71:59", self.test_right)]:
            ttk.Button(row, text=label, command=callback).pack(side="left", padx=(0, 6))
        self.status = tk.StringVar(value="Not connected. No commands sent.")
        ttk.Label(frame, textvariable=self.status, wraplength=790, font=("Arial", 11, "bold")).pack(anchor="w", pady=6)
        self.progress = ttk.Progressbar(frame)
        self.progress.pack(fill="x", pady=6)
        ttk.Label(frame, text="Recent transmissions — select a row to replay or save it:").pack(anchor="w")
        self.listbox = tk.Listbox(frame, height=13, exportselection=False, font=("Courier", 10))
        self.listbox.pack(fill="both", expand=True, pady=5)
        row = ttk.Frame(frame); row.pack(fill="x")
        ttk.Button(row, text="Replay selected (pauses scan)", command=self.replay).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Save selected as .ir", command=self.save).pack(side="left")
        ttk.Label(frame, text="Known power, eject and exit codes are INCLUDED. Watch the TV and pause to restore its menu.\n"
                  "CLI completion confirms a send request, not a response from the TV. Closing this window stops the scan.", wraplength=790).pack(anchor="w", pady=(12, 0))
        root.after(50, self.poll)
        self.refresh()

    def refresh(self):
        try:
            from serial.tools import list_ports
            ports = list(list_ports.comports())
            self.ports_box["values"] = [p.device for p in ports]
            if not self.port_name.get():
                preferred = next((p for p in ports if "flipper" in (p.description + " " + (p.manufacturer or "")).lower()), None)
                if preferred or len(ports) == 1:
                    self.port_name.set((preferred or ports[0]).device)
        except ImportError:
            self.status.set("Install pyserial first: python -m pip install pyserial")

    def background(self, task):
        self.busy = True
        def worker():
            try:
                self.events.put((True, task()))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        self.pause()
        if self.busy:
            return
        name = self.port_name.get().strip()
        if not name:
            messagebox.showerror("Port required", "Select the Flipper's USB serial port.")
            return
        def task():
            import serial
            if self.port:
                self.port.close()
                self.port = None
            port = serial.Serial(name, baudrate=115200, timeout=0.1, write_timeout=2)
            try:
                port.reset_input_buffer()
                port.write(b"\r")
                read_prompt(port)
            except Exception:
                port.close()
                raise
            self.port = port
            return ("connected", name)
        self.status.set("Connecting...")
        self.background(task)

    def gap(self):
        value = float(self.delay.get())
        if not 0.1 <= value <= 60:
            raise ValueError("Set a gap between 0.1 and 60 seconds.")
        return value

    def new_scan(self):
        self.pause()
        if self.busy:
            return
        if not self.port:
            messagebox.showerror("Not connected", "Connect to the Flipper first.")
            return
        try:
            address, command = int(self.address.get(), 16), int(self.command.get(), 16)
            if not (0 <= address <= 255 and 0 <= command <= 255):
                raise ValueError("Address and command must be 00 through FF.")
            self.gap()
            codes = scan_codes(address, self.all_addresses.get(), command)
            log_path = Path(__file__).resolve().parent / ("scan_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".csv")
            new_log = log_path.open("w", newline="", encoding="utf-8")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Cannot start", str(exc)); return
        if self.log:
            self.log.close()
        self.log = new_log
        self.writer = csv.writer(self.log)
        self.writer.writerow(["time", "protocol", "address_hex", "command_hex", "kind"])
        self.log.flush()
        self.codes, self.index = codes, 0
        self.progress["maximum"] = len(codes)
        self.progress["value"] = 0
        self.running = True
        self.send_next()

    def pause(self):
        self.running = False
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.codes:
            self.status.set(f"Paused. Completed {self.index}/{len(self.codes)}. An in-flight command may still finish.")

    def resume(self):
        if not self.codes:
            return
        self.pause()
        self.running = True
        if not self.busy:
            self.send_next()

    def step(self):
        self.pause()
        if not self.busy:
            self.send_next()

    def send_next(self):
        self.timer = None
        if self.busy or not self.codes:
            return
        if self.index >= len(self.codes):
            self.running = False
            self.status.set("Scan complete. All codes in the selected range sent.")
            return
        self.send(self.codes[self.index], "scan")

    def send(self, pair, kind):
        if self.busy or not self.port:
            return
        a, c = pair
        self.status.set(f"Sending NEC address 0x{a:02X}  command 0x{c:02X}")
        def task():
            self.port.reset_input_buffer()
            self.port.write(f"ir tx NEC {a:02X} {c:02X}\r".encode("ascii"))
            response = read_prompt(self.port)
            if re.search(r"error|invalid|usage|busy|failed|unknown|not found|cannot|wrong", response, re.I):
                raise RuntimeError(response[-1200:])
            return ("sent", (pair, kind))
        self.background(task)

    def selected(self):
        selection = self.listbox.curselection()
        return self.history[selection[0]] if selection else (self.history[-1] if self.history else None)

    def replay(self):
        self.pause()
        pair = self.selected()
        if pair and not self.busy:
            self.send(pair, "replay")

    def test_right(self):
        self.pause()
        if not self.busy:
            self.send((0x71, 0x59), "known-right")

    def save(self):
        self.pause()
        pair = self.selected()
        if pair is None:
            return
        a, c = pair
        path = filedialog.asksaveasfilename(defaultextension=".ir", initialfile=f"NEC_{a:02X}_{c:02X}.ir", filetypes=[("Flipper infrared", "*.ir")])
        if path:
            try:
                Path(path).write_text(ir_file(a, c), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc))

    def poll(self):
        try:
            success, payload = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if not success:
                self.pause()
                self.status.set("Stopped on error; current scan code has not been advanced.")
                messagebox.showerror("Flipper communication error", payload)
            else:
                event, value = payload
                if event == "connected":
                    self.status.set(f"Connected to {value}. Test Right 71:59 in the DVD language menu first.")
                else:
                    pair, kind = value
                    a, c = pair
                    stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
                    self.history.append(pair)
                    self.listbox.insert("end", f"{stamp[11:]}   NEC  A:0x{a:02X}  C:0x{c:02X}   {kind}")
                    if len(self.history) > 500:
                        self.history.pop(0); self.listbox.delete(0)
                    self.listbox.see("end")
                    if kind == "scan":
                        self.index += 1
                    self.progress["value"] = self.index
                    self.status.set(f"Last: NEC 0x{a:02X}:0x{c:02X} | {self.index}/{len(self.codes)} | " + ("Running" if self.running else "Paused"))
                    try:
                        if self.writer:
                            self.writer.writerow([stamp, "NEC", f"{a:02X}", f"{c:02X}", kind])
                            self.log.flush()
                        if self.running:
                            self.timer = self.root.after(round(self.gap() * 1000), self.send_next)
                    except (ValueError, OSError) as exc:
                        self.pause()
                        messagebox.showerror("Scan paused", str(exc))
        self.root.after(50, self.poll)

    def close(self):
        self.pause()
        if self.busy:
            self.status.set("Stopping after the current USB operation finishes...")
            self.root.after(100, self.close)
            return
        if self.port:
            self.port.close()
        if self.log:
            self.log.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    Scanner(root)
    root.mainloop()
