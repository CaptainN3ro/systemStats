#!/usr/bin/env python3
"""
systemStats by CaptainN3ro
==========================
A fullscreen system monitoring dashboard built with Python & Tkinter.
Supports Windows, Linux, and macOS — with optional Docker integration.

Repository : https://github.com/CaptainN3ro/systemStats
License    : MIT
"""

import sys
import os
import platform
import subprocess
import socket
import json
import math
import time
from datetime import datetime, timedelta
from threading import Thread, Lock

# ---------------------------------------------------------------------------
# Auto-install psutil if missing
# ---------------------------------------------------------------------------
def _install_deps():
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("[systemStats] Installing psutil …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "psutil", "--quiet"]
        )

_install_deps()

import psutil
import tkinter as tk
from tkinter import font as tkfont  # noqa: F401 (kept for future use)
import tkinter.ttk as ttk  # noqa: F401 (kept for future use)

# Prime the CPU counter so the first real reading is accurate
psutil.cpu_percent(interval=None)
time.sleep(0.3)
psutil.cpu_percent(interval=None)


# ---------------------------------------------------------------------------
# Safe PowerShell runner  (Windows only, forces UTF-8 to avoid encoding errors)
# ---------------------------------------------------------------------------
def _ps(cmd: str, timeout: int = 8) -> subprocess.CompletedProcess:
    """Run a PowerShell command and return the CompletedProcess result."""
    full_cmd = f"[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; {cmd}"
    return subprocess.run(
        ["powershell", "-NonInteractive", "-NoProfile", "-Command", full_cmd],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# CPU temperature
# ---------------------------------------------------------------------------
def get_cpu_temp():
    """Return (temperature_float, source_str) or (None, reason_str)."""
    os_name = platform.system()

    # ── Linux ────────────────────────────────────────────────────────────────
    if os_name == "Linux":
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                priority = [
                    "coretemp", "k10temp", "cpu_thermal", "cpu-thermal",
                    "acpitz", "it8686", "zenpower",
                ]
                for name in priority:
                    if name in temps:
                        return round(temps[name][0].current, 1), name
                # Fallback: first available sensor
                for name, entries in temps.items():
                    if entries:
                        return round(entries[0].current, 1), name
        except Exception:
            pass
        return None, "lm-sensors not found"

    # ── Windows ──────────────────────────────────────────────────────────────
    elif os_name == "Windows":
        # 1) LibreHardwareMonitor / OpenHardwareMonitor via WMI (most reliable)
        for namespace, label in [
            ("root\\LibreHardwareMonitor", "LibreHW"),
            ("root\\OpenHardwareMonitor",  "OpenHW"),
        ]:
            try:
                import wmi
                w = wmi.WMI(namespace=namespace)
                best = None
                for sensor in w.Sensor():
                    if sensor.SensorType == "Temperature":
                        n = sensor.Name.lower()
                        if "cpu" in n or "core" in n or "package" in n:
                            val = round(float(sensor.Value), 1)
                            if best is None or "package" in n:
                                best = val
                if best is not None:
                    return best, label
            except Exception:
                pass

        # 2) Built-in WMI thermal zone (often inaccurate but widely available)
        try:
            import wmi
            w = wmi.WMI(namespace="root\\wmi")
            zones = w.MSAcpi_ThermalZoneTemperature()
            if zones:
                temps_c = [(z.CurrentTemperature / 10.0) - 273.15 for z in zones]
                plausible = [t for t in temps_c if 10 < t < 105]
                if plausible:
                    return round(max(plausible), 1), "ACPI"
        except Exception:
            pass

        # 3) PowerShell fallback for the same ACPI data
        try:
            ps = (
                "Get-WmiObject MSAcpi_ThermalZoneTemperature "
                "-Namespace root/wmi | "
                "Select-Object -ExpandProperty CurrentTemperature"
            )
            r = _ps(ps, timeout=5)
            vals = []
            for line in r.stdout.strip().splitlines():
                try:
                    c = (int(line.strip()) / 10.0) - 273.15
                    if 10 < c < 105:
                        vals.append(c)
                except ValueError:
                    pass
            if vals:
                return round(max(vals), 1), "ACPI"
        except Exception:
            pass

        return None, "LibreHardwareMonitor\nrequired"

    # ── macOS ─────────────────────────────────────────────────────────────────
    elif os_name == "Darwin":
        try:
            r = subprocess.run(
                ["sudo", "powermetrics", "--samplers", "smc", "-n", "1", "-i", "100"],
                capture_output=True, encoding="utf-8", errors="replace", timeout=5,
            )
            for line in r.stdout.splitlines():
                if "CPU die temperature" in line:
                    val = float(line.split(":")[1].strip().replace(" C", ""))
                    return round(val, 1), "SMC"
        except Exception:
            pass
        return None, "powermetrics not found"

    return None, "Unknown OS"


# ---------------------------------------------------------------------------
# Docker container info
# ---------------------------------------------------------------------------
def get_docker_containers():
    """Return a list of running container dicts, or None if Docker is absent."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode != 0:
            return None
        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                c = json.loads(line)
                containers.append({
                    "name":   c.get("Names", "unknown"),
                    "image":  c.get("Image", ""),
                    "status": c.get("Status", ""),
                    "ports":  c.get("Ports", ""),
                    "id":     c.get("ID", "")[:12],
                })
            except Exception:
                pass
        return containers
    except FileNotFoundError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Local IP address
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "N/A"


# ---------------------------------------------------------------------------
# Disk type detection
# ---------------------------------------------------------------------------
_win_disk_cache: dict = {}   # drive_letter → (type_label, icon)
_win_disk_loaded: bool = False


def _load_windows_disk_types() -> None:
    """Query all physical disks once via PowerShell and cache the results."""
    global _win_disk_cache, _win_disk_loaded
    if _win_disk_loaded:
        return
    _win_disk_loaded = True
    try:
        ps_cmd = (
            "Get-PhysicalDisk | Select-Object -Property MediaType,DeviceId | "
            "ConvertTo-Json -Compress"
        )
        r = _ps(ps_cmd, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            raw = r.stdout.strip()
            if raw.startswith("{"):
                raw = f"[{raw}]"
            disks = json.loads(raw)

            # Map partitions to drive letters
            ps_part = (
                "Get-Partition | Where-Object {$_.DriveLetter} | "
                "Select-Object DriveLetter,DiskNumber | ConvertTo-Json -Compress"
            )
            rp = _ps(ps_part, timeout=8)
            partitions: dict = {}
            if rp.returncode == 0 and rp.stdout.strip():
                praw = rp.stdout.strip()
                if praw.startswith("{"):
                    praw = f"[{praw}]"
                for p in json.loads(praw):
                    dl = str(p.get("DriveLetter", "")).strip()
                    dn = str(p.get("DiskNumber", "")).strip()
                    if dl and dn:
                        partitions[dl.upper()] = dn

            disk_map: dict = {}
            for d in disks:
                did = str(d.get("DeviceId", "")).strip()
                mt  = str(d.get("MediaType", "")).strip().lower()
                disk_map[did] = mt

            for letter, disk_num in partitions.items():
                mt = disk_map.get(disk_num, "")
                if "ssd" in mt or "solid" in mt:
                    _win_disk_cache[letter] = ("SSD",  "⚡")
                elif "hdd" in mt or "hard" in mt or "rotate" in mt:
                    _win_disk_cache[letter] = ("HDD",  "💿")
                elif mt in ("", "unspecified"):
                    # Modern unspecified drives are almost always NVMe/SSD
                    _win_disk_cache[letter] = ("SSD",  "⚡")
                else:
                    _win_disk_cache[letter] = ("Disk", "💾")
    except Exception:
        pass


def get_disk_type(device: str) -> tuple:
    """Return (type_label, icon) e.g. ('SSD', '⚡') or ('HDD', '💿')."""
    os_name = platform.system()

    if os_name == "Linux":
        try:
            import re
            dev = os.path.basename(device)
            dev = re.sub(r"p?\d+$", "", dev)   # strip partition suffix
            for candidate in [dev, re.sub(r"n\d+$", "", dev)]:
                rotational = f"/sys/block/{candidate}/queue/rotational"
                if os.path.exists(rotational):
                    with open(rotational) as f:
                        val = f.read().strip()
                    if val == "0":
                        return ("NVMe", "⚡") if "nvme" in candidate else ("SSD", "⚡")
                    return ("HDD", "💿")
        except Exception:
            pass
        dev_lower = device.lower()
        if "nvme"   in dev_lower: return ("NVMe", "⚡")
        if "mmcblk" in dev_lower: return ("eMMC", "⚡")
        return ("Disk", "💾")

    elif os_name == "Windows":
        _load_windows_disk_types()
        letter = device[0].upper() if device else ""
        if letter in _win_disk_cache:
            return _win_disk_cache[letter]
        if "nvme" in device.lower():
            return ("NVMe", "⚡")
        return ("Disk", "💾")

    elif os_name == "Darwin":
        try:
            dev = os.path.basename(device).rstrip("s0123456789")
            r = subprocess.run(
                ["diskutil", "info", dev],
                capture_output=True, encoding="utf-8", errors="replace", timeout=4,
            )
            info = r.stdout.lower()
            if "solid state" in info: return ("SSD", "⚡")
            if "rotational"  in info: return ("HDD", "💿")
        except Exception:
            pass
        return ("Disk", "💾")

    return ("Disk", "💾")


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def gather_data() -> dict:
    """Collect all system metrics and return them as a dictionary."""
    data: dict = {}

    # Platform
    data["os"]         = platform.system()
    data["os_version"] = platform.version()
    data["hostname"]   = socket.gethostname()

    # CPU
    data["cpu_percent"]  = psutil.cpu_percent(interval=1)
    data["cpu_count"]    = psutil.cpu_count(logical=True)
    data["cpu_freq"]     = psutil.cpu_freq()
    data["cpu_temp"], data["cpu_temp_src"] = get_cpu_temp()

    # RAM
    mem = psutil.virtual_memory()
    data["ram_total"]     = mem.total
    data["ram_used"]      = mem.used
    data["ram_available"] = mem.available
    data["ram_percent"]   = mem.percent

    # Disks
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            dtype, dicon = get_disk_type(part.device)
            disks.append({
                "device":     part.device,
                "mountpoint": part.mountpoint,
                "fstype":     part.fstype,
                "total":      usage.total,
                "used":       usage.used,
                "free":       usage.free,
                "percent":    usage.percent,
                "dtype":      dtype,
                "dicon":      dicon,
            })
        except PermissionError:
            pass
    data["disks"] = disks

    # Boot / uptime
    boot_ts = psutil.boot_time()
    boot_dt = datetime.fromtimestamp(boot_ts)
    uptime  = datetime.now() - boot_dt
    days    = uptime.days
    hours, rem = divmod(uptime.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    data["boot_time"] = boot_dt.strftime("%d.%m.%Y %H:%M:%S")
    data["uptime"]    = f"{days}d {hours:02d}h {minutes:02d}m"

    # Network
    data["ip"]       = get_local_ip()
    net = psutil.net_io_counters()
    data["net_sent"] = net.bytes_sent
    data["net_recv"] = net.bytes_recv

    # Docker
    data["docker"] = get_docker_containers()

    data["timestamp"] = datetime.now().strftime("%H:%M:%S")
    return data


# ---------------------------------------------------------------------------
# Helper formatters
# ---------------------------------------------------------------------------
def fmt_bytes(b: float) -> str:
    """Convert a byte count to a human-readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# ===========================================================================
#  Dashboard Application
# ===========================================================================
class Dashboard(tk.Tk):
    # Colour palette
    BG      = "#0a0e17"
    PANEL   = "#0f1520"
    BORDER  = "#1e2d45"
    ACCENT  = "#00d4ff"
    ACCENT2 = "#7b61ff"
    GREEN   = "#00e676"
    ORANGE  = "#ff9100"
    RED     = "#ff1744"
    TEXT    = "#e8f0fe"
    MUTED   = "#4a6080"
    WARN    = "#ffab00"

    IDLE_TIMEOUT = 300      # seconds until screensaver activates
    REFRESH_MS   = 60_000   # data refresh interval (ms)

    def __init__(self):
        super().__init__()
        self.title("systemStats by CaptainN3ro")
        self.configure(bg=self.BG)
        self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.bind("<F11>",    lambda e: self._toggle_fullscreen())

        # Activity tracking for screensaver
        self._last_activity       = time.time()
        self._screensaver_active  = False
        self._ss_angle            = 0.0
        self._ss_radius           = 0.0
        self._ss_growing          = True
        self._ss_alpha            = 0
        for seq in ("<Motion>", "<Key>", "<Button>"):
            self.bind(seq, self._on_activity)

        self._data          = {}
        self._lock          = Lock()
        self._prev_net_sent = 0
        self._prev_net_recv = 0
        self._prev_net_time = time.time()

        self._build_ui()
        self._show_loading()
        self.after(100, self._refresh_data)   # slight delay so window renders first
        self._schedule_refresh()
        self._check_screensaver()

    # ── Activity / Screensaver ────────────────────────────────────────────────
    def _on_activity(self, event=None):
        self._last_activity = time.time()
        if self._screensaver_active:
            self._hide_screensaver()

    def _check_screensaver(self):
        idle = time.time() - self._last_activity
        if idle >= self.IDLE_TIMEOUT and not self._screensaver_active:
            self._show_screensaver()
        elif self._screensaver_active:
            self._animate_screensaver()
        self.after(50, self._check_screensaver)

    def _show_screensaver(self):
        self._screensaver_active = True
        self._ss_frame = tk.Frame(self, bg="black")
        self._ss_frame.place(x=0, y=0, relwidth=1, relheight=1)
        self._ss_canvas = tk.Canvas(
            self._ss_frame, bg="black", highlightthickness=0
        )
        self._ss_canvas.pack(fill="both", expand=True)
        self._ss_angle   = 0.0
        self._ss_radius  = 0.0
        self._ss_growing = True

    def _hide_screensaver(self):
        self._screensaver_active = False
        if hasattr(self, "_ss_frame"):
            self._ss_frame.destroy()

    def _animate_screensaver(self):
        c  = self._ss_canvas
        c.delete("all")
        W  = self.winfo_width()
        H  = self.winfo_height()
        cx = W // 2
        cy = H // 2

        os_name = platform.system()

        # Pulsing outer glow ring
        if self._ss_growing:
            self._ss_radius += 1.5
            if self._ss_radius >= 130:
                self._ss_growing = False
        else:
            self._ss_radius -= 1.5
            if self._ss_radius <= 40:
                self._ss_growing = True

        r = self._ss_radius
        for i in range(5):
            alpha_r = int(max(0, 160 - i * 30))
            col = self._hex_with_intensity(self.ACCENT, alpha_r / 255)
            c.create_oval(
                cx - r - i * 6, cy - r - i * 6,
                cx + r + i * 6, cy + r + i * 6,
                outline=col, width=max(1, 3 - i),
            )

        # Rotating arc of dots
        self._ss_angle = (self._ss_angle + 2) % 360
        arc_r = r + 20
        for seg in range(0, 360, 6):
            a          = math.radians(self._ss_angle + seg)
            brightness = (math.sin(math.radians(seg)) + 1) / 2
            col        = self._hex_with_intensity(self.ACCENT2, brightness * 0.9)
            x1         = cx + arc_r * math.cos(a)
            y1         = cy + arc_r * math.sin(a)
            c.create_oval(x1 - 2, y1 - 2, x1 + 2, y1 + 2, fill=col, outline="")

        # OS logo
        logo_size = 48
        if   os_name == "Linux":   self._draw_tux(c, cx, cy, logo_size)
        elif os_name == "Windows": self._draw_windows_logo(c, cx, cy, logo_size)
        elif os_name == "Darwin":  self._draw_apple_logo(c, cx, cy, logo_size)
        else:
            c.create_text(cx, cy, text="⚙", font=("Helvetica", 60), fill=self.ACCENT)

        c.create_text(cx, cy + r + 50, text=os_name.upper(),
                      font=("Courier", 16, "bold"), fill=self.MUTED)
        c.create_text(cx, H - 60, text=datetime.now().strftime("%H:%M:%S"),
                      font=("Courier", 22, "bold"), fill=self.ACCENT)
        c.create_text(cx, H - 30, text="Move to continue",
                      font=("Courier", 11), fill=self.MUTED)

    # ── Colour utility ────────────────────────────────────────────────────────
    def _hex_with_intensity(self, hex_color: str, intensity: float) -> str:
        hex_color = hex_color.lstrip("#")
        r = min(255, int(int(hex_color[0:2], 16) * intensity))
        g = min(255, int(int(hex_color[2:4], 16) * intensity))
        b = min(255, int(int(hex_color[4:6], 16) * intensity))
        return f"#{r:02x}{g:02x}{b:02x}"

    # ── OS logo drawers ───────────────────────────────────────────────────────
    def _draw_tux(self, c, cx, cy, s):
        c.create_oval(cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2,
                      fill="#111", outline=self.ACCENT, width=2)
        c.create_oval(cx - s // 4, cy - s // 6, cx + s // 4, cy + s // 3,
                      fill="#fff8dc", outline="")
        for dx in (-s // 5, s // 5):
            c.create_oval(cx + dx - 4, cy - s // 5, cx + dx + 4, cy - s // 5 + 8,
                          fill=self.ACCENT, outline="")
        c.create_polygon(cx - 5, cy, cx + 5, cy, cx, cy + 10,
                         fill=self.ORANGE, outline="")

    def _draw_windows_logo(self, c, cx, cy, s):
        gap     = 4
        h       = s // 2 - gap
        colors  = [self.ACCENT, self.GREEN, self.ORANGE, self.RED]
        offsets = [(-h - gap, -h - gap), (gap, -h - gap), (gap, gap), (-h - gap, gap)]
        for col, (ox, oy) in zip(colors, offsets):
            c.create_rectangle(cx + ox, cy + oy, cx + ox + h, cy + oy + h,
                                fill=col, outline="")

    def _draw_apple_logo(self, c, cx, cy, s):
        c.create_text(cx, cy, text="", font=("Helvetica", s), fill=self.TEXT)

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        self._title_bar()

        # Row 0 – CPU | RAM | System + Network
        top = tk.Frame(self, bg=self.BG)
        top.pack(fill="x", padx=16, pady=(0, 6))
        top.columnconfigure((0, 1, 2), weight=1, uniform="top")

        self._panels = {}
        for key, col in [("cpu", 0), ("ram", 1), ("sysnet", 2)]:
            frm = tk.Frame(top, bg=self.PANEL,
                           highlightbackground=self.BORDER, highlightthickness=1)
            frm.grid(row=0, column=col, sticky="nsew", padx=6, pady=0)
            self._panels[key] = frm

        # Row 1 – Docker (hidden when Docker is not installed)
        self._docker_row = tk.Frame(self, bg=self.BG)
        self._docker_row.pack(fill="x", padx=16, pady=(0, 6))

        # Row 2 – Disks
        disk_outer = tk.Frame(self, bg=self.PANEL,
                              highlightbackground=self.BORDER, highlightthickness=1)
        disk_outer.pack(fill="x", padx=16, pady=(0, 12))
        self._panels["disks"] = disk_outer

    def _title_bar(self):
        bar = tk.Frame(self, bg=self.BG, height=62)
        bar.pack(fill="x", padx=16, pady=(12, 4))
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=self.BG)
        left.pack(side="left", fill="y")

        btn_frame = tk.Frame(left, bg=self.BG)
        btn_frame.pack(side="left", padx=(0, 18), pady=12)

        def make_btn(parent, color, icon, label, cmd):
            outer = tk.Frame(parent, bg=color, cursor="hand2")
            outer.pack(side="left", padx=4)
            inner = tk.Frame(outer, bg="#1a1a2e", padx=8, pady=4)
            inner.pack(padx=1, pady=1)
            lbl = tk.Label(inner, text=f"{icon}  {label}", bg="#1a1a2e", fg=color,
                           font=("Courier", 10, "bold"), cursor="hand2")
            lbl.pack()
            for w in (outer, inner, lbl):
                w.bind("<Button-1>", lambda e: cmd())
                w.bind("<Enter>",
                       lambda e, _i=inner, _l=lbl, _c=color:
                           (_i.config(bg=_c), _l.config(bg=_c, fg="#0a0e17")))
                w.bind("<Leave>",
                       lambda e, _i=inner, _l=lbl, _c=color:
                           (_i.config(bg="#1a1a2e"), _l.config(bg="#1a1a2e", fg=_c)))
            return outer

        make_btn(btn_frame, self.RED,    "✕", "Close",     self.destroy)
        make_btn(btn_frame, self.ORANGE, "–", "Minimise",  self.iconify)
        make_btn(btn_frame, self.GREEN,  "⛶", "Fullscreen", self._toggle_fullscreen)

        tk.Label(left, text="systemStats  by CaptainN3ro",
                 bg=self.BG, fg=self.TEXT,
                 font=("Courier", 17, "bold")).pack(side="left", pady=14)

        right = tk.Frame(bar, bg=self.BG)
        right.pack(side="right", fill="y")

        self._lbl_time = tk.Label(right, text="",
                                  bg=self.BG, fg=self.ACCENT,
                                  font=("Courier", 13))
        self._lbl_time.pack(side="right", pady=14, padx=(0, 16))

        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x", padx=16, pady=(0, 8))

    def _toggle_fullscreen(self):
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    # ── Panel helpers ─────────────────────────────────────────────────────────
    def _clear(self, panel):
        for w in panel.winfo_children():
            w.destroy()

    def _section_title(self, parent, icon: str, title: str, color: str = None):
        color = color or self.ACCENT
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(row, text=icon, bg=self.PANEL, fg=color,
                 font=("Helvetica", 14)).pack(side="left", padx=(0, 6))
        tk.Label(row, text=title.upper(), bg=self.PANEL, fg=color,
                 font=("Courier", 11, "bold")).pack(side="left")
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", padx=14, pady=(0, 8))

    def _bar(self, parent, percent: float, color: str = None):
        color  = color or self.ACCENT
        canvas = tk.Canvas(parent, bg="#1a2535", height=10,
                           highlightthickness=0, bd=0)
        canvas.pack(fill="x", padx=14, pady=(2, 8))

        def draw():
            canvas.update_idletasks()
            w = canvas.winfo_width() or 300
            fw = max(4, int(w * percent / 100))
            canvas.delete("all")
            canvas.create_rectangle(0, 0, fw, 10, fill=color, outline="")
            highlight = self._hex_with_intensity(
                color, 1.3 if color != self.GREEN else 0.8
            )
            canvas.create_rectangle(0, 0, fw, 3, fill=highlight, outline="")

        canvas.after(10, draw)

    def _stat_row(self, parent, label: str, value: str, vcolor: str = None):
        vcolor = vcolor or self.TEXT
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill="x", padx=14, pady=2)
        tk.Label(row, text=label, bg=self.PANEL, fg=self.MUTED,
                 font=("Courier", 10), width=18, anchor="w").pack(side="left")
        tk.Label(row, text=value, bg=self.PANEL, fg=vcolor,
                 font=("Courier", 10, "bold"), anchor="w").pack(side="left")

    def _percent_color(self, pct: float) -> str:
        if pct < 60: return self.GREEN
        if pct < 80: return self.WARN
        return self.RED

    # ── Panel renderers ───────────────────────────────────────────────────────
    def _render_cpu(self, d: dict):
        p = self._panels["cpu"]
        self._clear(p)
        self._section_title(p, "⚡", "Processor", self.ACCENT)

        pct   = d.get("cpu_percent", 0)
        color = self._percent_color(pct)

        big = tk.Frame(p, bg=self.PANEL)
        big.pack(pady=(4, 0))
        tk.Label(big, text=f"{pct:.0f}", bg=self.PANEL, fg=color,
                 font=("Courier", 42, "bold")).pack(side="left", padx=(14, 0))
        tk.Label(big, text="%", bg=self.PANEL, fg=self.MUTED,
                 font=("Courier", 18)).pack(side="left", pady=12)

        self._bar(p, pct, color)

        freq     = d.get("cpu_freq")
        freq_str = f"{freq.current / 1000:.2f} GHz" if freq else "N/A"
        temp     = d.get("cpu_temp")
        temp_src = d.get("cpu_temp_src", "")

        if temp is not None:
            temp_str   = f"{temp} °C"
            temp_color = self._percent_color((temp - 30) * 2)
            src_str    = f"via {temp_src}" if temp_src else ""
        else:
            temp_str   = "N/A"
            temp_color = self.MUTED
            src_str    = temp_src

        self._stat_row(p, "Cores:",       str(d.get("cpu_count", "?")))
        self._stat_row(p, "Frequency:",   freq_str)
        self._stat_row(p, "Temperature:", temp_str, temp_color)
        if src_str:
            for line in src_str.split("\n"):
                self._stat_row(p, "", line.strip(), self.MUTED)

    def _render_ram(self, d: dict):
        p = self._panels["ram"]
        self._clear(p)
        self._section_title(p, "🧠", "Memory", self.ACCENT2)

        pct   = d.get("ram_percent", 0)
        color = self._percent_color(pct)

        big = tk.Frame(p, bg=self.PANEL)
        big.pack(pady=(4, 0))
        tk.Label(big, text=f"{pct:.0f}", bg=self.PANEL, fg=color,
                 font=("Courier", 42, "bold")).pack(side="left", padx=(14, 0))
        tk.Label(big, text="%", bg=self.PANEL, fg=self.MUTED,
                 font=("Courier", 18)).pack(side="left", pady=12)

        self._bar(p, pct, color)

        self._stat_row(p, "Total:",     fmt_bytes(d.get("ram_total",     0)))
        self._stat_row(p, "Used:",      fmt_bytes(d.get("ram_used",      0)), color)
        self._stat_row(p, "Available:", fmt_bytes(d.get("ram_available", 0)), self.GREEN)

    def _render_sysnet(self, d: dict):
        p = self._panels["sysnet"]
        self._clear(p)
        self._section_title(p, "🖥", "System & Network", self.GREEN)

        tk.Label(p, text=d.get("ip", "N/A"),
                 bg=self.PANEL, fg=self.ACCENT,
                 font=("Courier", 20, "bold")).pack(padx=14, pady=(2, 0), anchor="w")
        tk.Label(p, text="Local IP address",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Courier", 9)).pack(padx=14, pady=(0, 6), anchor="w")

        tk.Frame(p, bg=self.BORDER, height=1).pack(fill="x", padx=14, pady=(0, 6))

        self._stat_row(p, "Hostname:",   d.get("hostname",  "N/A"))
        self._stat_row(p, "OS:",         d.get("os",        "?"))
        self._stat_row(p, "Last boot:",  d.get("boot_time", "?"))
        self._stat_row(p, "Uptime:",     d.get("uptime",    "?"), self.GREEN)

        tk.Frame(p, bg=self.BORDER, height=1).pack(fill="x", padx=14, pady=6)

        self._stat_row(p, "Sent:",     fmt_bytes(d.get("net_sent", 0)))
        self._stat_row(p, "Received:", fmt_bytes(d.get("net_recv", 0)))
        self._stat_row(p, "Updated:",  d.get("timestamp", "?"), self.MUTED)

    def _render_sysnet_disks(self, d: dict):
        p = self._panels["disks"]
        self._clear(p)
        self._section_title(p, "💾", "Storage", self.ORANGE)

        disks = d.get("disks", [])
        if not disks:
            tk.Label(p, text="No storage devices found",
                     bg=self.PANEL, fg=self.MUTED,
                     font=("Courier", 10)).pack(padx=14, pady=6, anchor="w")
            return

        for disk in disks[:12]:
            row = tk.Frame(p, bg=self.PANEL)
            row.pack(fill="x", padx=12, pady=3)

            pct   = disk["percent"]
            col   = self._percent_color(pct)
            mount = disk["mountpoint"]
            if len(mount) > 16: mount = "…" + mount[-15:]
            dev   = disk["device"]
            if len(dev) > 20:   dev   = "…" + dev[-19:]

            # Left column: mountpoint + device + type badge
            left = tk.Frame(row, bg=self.PANEL, width=200)
            left.pack(side="left", fill="y")
            left.pack_propagate(False)

            top_row = tk.Frame(left, bg=self.PANEL)
            top_row.pack(fill="x")
            tk.Label(top_row, text=mount, bg=self.PANEL, fg=self.TEXT,
                     font=("Courier", 10, "bold"), anchor="w").pack(side="left")

            dtype     = disk.get("dtype", "Disk")
            dicon     = disk.get("dicon", "💾")
            badge_col = {
                "SSD": "#00d4ff", "NVMe": "#7b61ff", "HDD": "#ff9100",
                "USB": "#00e676", "eMMC": "#ff9100", "CD":  "#4a6080",
                "Net": "#00e676",
            }.get(dtype, "#4a6080")
            badge = tk.Label(top_row, text=f" {dicon} {dtype} ",
                             bg=badge_col, fg="#0a0e17",
                             font=("Courier", 7, "bold"))
            badge.pack(side="left", padx=(6, 0), pady=1)

            tk.Label(left, text=dev, bg=self.PANEL, fg=self.MUTED,
                     font=("Courier", 8), anchor="w").pack(fill="x")

            # Middle column: usage bar
            mid   = tk.Frame(row, bg=self.PANEL)
            mid.pack(side="left", fill="both", expand=True, padx=10)
            bar_c = tk.Canvas(mid, bg="#1a2535", height=8, highlightthickness=0, bd=0)
            bar_c.pack(fill="x", pady=6)

            def draw_bar(c=bar_c, p_=pct, clr=col):
                c.update_idletasks()
                w = c.winfo_width() or 300
                fw = max(3, int(w * p_ / 100))
                c.delete("all")
                c.create_rectangle(0, 0, fw, 8, fill=clr, outline="")

            bar_c.after(15, draw_bar)

            # Right column: sizes + percentage
            right = tk.Frame(row, bg=self.PANEL, width=220)
            right.pack(side="right", fill="y")
            right.pack_propagate(False)
            tk.Label(right,
                     text=f"{fmt_bytes(disk['used'])} / {fmt_bytes(disk['total'])}  ({pct:.0f}%)",
                     bg=self.PANEL, fg=col,
                     font=("Courier", 10, "bold"), anchor="e").pack(fill="x")
            tk.Label(right,
                     text=f"{fmt_bytes(disk['free'])} free",
                     bg=self.PANEL, fg=self.MUTED,
                     font=("Courier", 8), anchor="e").pack(fill="x")

            tk.Frame(p, bg=self.BORDER, height=1).pack(fill="x", padx=12)

    def _render_docker(self, d: dict):
        for w in self._docker_row.winfo_children():
            w.destroy()

        containers = d.get("docker")
        if containers is None:
            return  # Docker not installed — row stays empty

        outer = tk.Frame(self._docker_row, bg=self.PANEL,
                         highlightbackground=self.BORDER, highlightthickness=1)
        outer.pack(fill="x")
        self._section_title(outer, "🐳", "Docker Containers", "#2496ed")

        if not containers:
            tk.Label(outer, text="No running containers",
                     bg=self.PANEL, fg=self.MUTED,
                     font=("Courier", 12)).pack(padx=16, pady=10, anchor="w")
            return

        # Table – header and data rows share the same grid for guaranteed alignment
        table = tk.Frame(outer, bg=self.PANEL)
        table.pack(fill="x", padx=14, pady=(0, 8))

        col_cfg = [
            (0, 30,  1,   False),  # status dot
            (1, 180, 1,   False),  # name
            (2, 200, 1,   False),  # image
            (3, 160, 1,   False),  # status text
            (4, 120, 100, True),   # ports (expands)
        ]
        for ci, minw, wt, exp in col_cfg:
            table.columnconfigure(ci, minsize=minw, weight=wt if exp else 0)

        hbg = "#0d1826"
        for ci, txt in enumerate(["", "Name", "Image", "Status", "Ports"]):
            tk.Label(table, text=txt, bg=hbg, fg="#2496ed",
                     font=("Courier", 11, "bold"), anchor="w",
                     padx=6, pady=5).grid(row=0, column=ci, sticky="ew")

        tk.Frame(table, bg=self.BORDER, height=1).grid(
            row=1, column=0, columnspan=5, sticky="ew")

        for i, c in enumerate(containers):
            base_row = 2 + i * 2
            status   = c["status"].lower()
            dot_col  = self.GREEN if "up" in status else self.RED
            stat_col = self.GREEN if "up" in status else self.RED
            row_bg   = self.PANEL if i % 2 == 0 else "#111c2e"

            tk.Label(table, text="⬤", bg=row_bg, fg=dot_col,
                     font=("Courier", 10), anchor="center", padx=6
                     ).grid(row=base_row, column=0, sticky="ew", ipady=5)
            tk.Label(table, text=c["name"], bg=row_bg, fg=self.TEXT,
                     font=("Courier", 11, "bold"), anchor="w", padx=6
                     ).grid(row=base_row, column=1, sticky="ew", ipady=5)
            tk.Label(table, text=c["image"], bg=row_bg, fg=self.MUTED,
                     font=("Courier", 11), anchor="w", padx=6
                     ).grid(row=base_row, column=2, sticky="ew", ipady=5)
            tk.Label(table, text=c["status"], bg=row_bg, fg=stat_col,
                     font=("Courier", 11), anchor="w", padx=6
                     ).grid(row=base_row, column=3, sticky="ew", ipady=5)
            ports = c["ports"] if c["ports"] else "—"
            tk.Label(table, text=ports, bg=row_bg, fg=self.ACCENT,
                     font=("Courier", 11), anchor="w", padx=6
                     ).grid(row=base_row, column=4, sticky="ew", ipady=5)
            tk.Frame(table, bg=self.BORDER, height=1).grid(
                row=base_row + 1, column=0, columnspan=5, sticky="ew")

    def _show_loading(self):
        for panel in self._panels.values():
            self._clear(panel)
            tk.Label(panel, text="⏳  Loading …", bg=self.PANEL, fg=self.MUTED,
                     font=("Courier", 11)).pack(expand=True)

    # ── Data refresh ──────────────────────────────────────────────────────────
    def _refresh_data(self):
        def worker():
            data = gather_data()
            with self._lock:
                self._data = data
            self.after(0, self._update_ui)

        Thread(target=worker, daemon=True).start()

    def _schedule_refresh(self):
        self.after(self.REFRESH_MS, self._periodic_refresh)
        self.after(1000, self._tick_clock)

    def _periodic_refresh(self):
        self._refresh_data()
        self.after(self.REFRESH_MS, self._periodic_refresh)

    def _tick_clock(self):
        self._lbl_time.config(text=datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _update_ui(self):
        with self._lock:
            d = dict(self._data)
        self._render_cpu(d)
        self._render_ram(d)
        self._render_sysnet(d)
        self._render_docker(d)
        self._render_sysnet_disks(d)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
