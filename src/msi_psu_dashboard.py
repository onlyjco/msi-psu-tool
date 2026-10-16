"""Real-time dashboard — the scrolling gauge view from MSI Center.

Reconstructed from ``UC_Dashboard_Page`` in the shipped
``UC_Power Supply Unit.dll``. The original shows nine scrolling traces with
auto-scaling Y axes, and can export the recorded window as CSV or a PNG
screenshot after a timed recording.

Gauges (same set and order as the original):
    CPU Wattage, GPU Wattage, Total Wattage,
    12V, 5V, 3.3V, Temperature, Fan Speed, Efficiency

IMPORTANT — the CPU/GPU traces are estimates, not sensors. The original
computes them from per-rail measurements:

    CPU = 12V2_V x 12V2_A
    GPU = 12V3_V x 12V3_A + 12V4_V x 12V4_A + 12V5_V x 12V5_A

which assumes the PSU's rails are wired CPU / GPU / rest. It is a plausible
convention on MSI's own boards but it is an assumption, so the UI labels
these two as estimates.
"""

from __future__ import annotations

import csv
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from i18n import t

def desktop_dir() -> str:
    """The user's real Desktop, honouring a redirect such as OneDrive.

    %USERPROFILE%\\Desktop is not always correct; the shell known-folder API
    is authoritative, with a filesystem fallback.
    """
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf):
                if buf.value and os.path.isdir(buf.value):
                    return buf.value
        except Exception:
            pass
    cand = os.path.join(os.path.expanduser("~"), "Desktop")
    return cand if os.path.isdir(cand) else os.path.expanduser("~")


BG = "#1b1d23"
PANEL = "#24262e"
FG = "#e8e8ea"
DIM = "#8d919e"
FAINT = "#5c6070"
GRID = "#333744"
ACCENT = "#c8102e"
OK = "#49c46a"
WARN = "#e0a92e"
BAD = "#f0524a"

F = ("Microsoft YaHei UI", 9)
F_S = ("Microsoft YaHei UI", 8)
F_B = ("Microsoft YaHei UI", 9, "bold")
F_T = ("Microsoft YaHei UI", 12, "bold")
F_MONO = ("Consolas", 9)
F_MONO_B = ("Consolas", 11, "bold")

# window length in samples (1 Hz) — the original keeps a comparable span
HISTORY = 300

GAUGES = [
    # key,             i18n key,               unit,  nominal, estimate?
    ("cpu_w",          "gauge_wattage_cpu",    "W",   None,    True),
    ("gpu_w",          "gauge_wattage_gpu",    "W",   None,    True),
    ("total_w",        "gauge_wattage_total",  "W",   None,    False),
    ("v12",            "gauge_voltage_12v",    "V",   12.0,    False),
    ("v5",             "gauge_voltage_5v",     "V",   5.0,     False),
    ("v3",             "gauge_voltage_3v",     "V",   3.3,     False),
    ("temp",           "gauge_temperature",    "℃",  None,    False),
    ("fan",            "gauge_fan",            "RPM", None,    False),
    ("eff",            "gauge_efficiency",     "%",   None,    False),
]


class Gauge(tk.Frame):
    """One scrolling trace with a value readout and an auto-scaled Y axis."""

    def __init__(self, parent, label: str, unit: str, colour: str = ACCENT,
                 estimate: bool = False, second_label: str | None = None,
                 second_colour: str = "#4a9eff"):
        super().__init__(parent, bg=PANEL)
        self._unit = unit
        self._colour = colour
        self._estimate = estimate
        self._data: list[float] = []
        # Optional second trace drawn on the SAME axis (used by the total-power
        # gauge to overlay MSI's summed figure with the PSU's measured value).
        self._data2: list[float] = []
        self._second_label = second_label
        self._second_colour = second_colour

        head = tk.Frame(self, bg=PANEL)
        head.pack(fill="x", padx=8, pady=(6, 0))
        self.lbl_label = tk.Label(head, text=label, font=F_B, bg=PANEL, fg=DIM)
        self.lbl_label.pack(side="left")
        if estimate:
            tk.Label(head, text="(估算)", font=F_S, bg=PANEL,
                     fg=FAINT).pack(side="left", padx=3)
        self.lbl_value = tk.Label(head, text="--", font=F_MONO_B, bg=PANEL,
                                  fg=FG)
        self.lbl_value.pack(side="right")

        # legend for the two traces, if a second one is configured
        if second_label:
            leg = tk.Frame(self, bg=PANEL)
            leg.pack(fill="x", padx=8)
            tk.Label(leg, text="\u25cf", font=F_S, bg=PANEL,
                     fg=colour).pack(side="left")
            tk.Label(leg, text=label, font=F_S, bg=PANEL,
                     fg=DIM).pack(side="left", padx=(2, 10))
            tk.Label(leg, text="\u25cf", font=F_S, bg=PANEL,
                     fg=second_colour).pack(side="left")
            self.lbl_leg2 = tk.Label(leg, text=second_label, font=F_S,
                                     bg=PANEL, fg=DIM)
            self.lbl_leg2.pack(side="left", padx=2)
            self.lbl_v2 = tk.Label(leg, text="", font=F_S, bg=PANEL,
                                   fg=second_colour)
            self.lbl_v2.pack(side="right")

        # The canvas must expand with the cell, otherwise the grid row keeps
        # its requested height and every trace collapses to a few pixels.
        self.canvas = tk.Canvas(self, height=70, bg=PANEL,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.canvas.bind("<Configure>", lambda e: self._draw())

    def add(self, value: float, second: float | None = None):
        self._data.append(float(value))
        if len(self._data) > HISTORY:
            del self._data[:-HISTORY]
        txt = f"{value:.2f}" if abs(value) < 100 else f"{value:.0f}"
        self.lbl_value.config(text=f"{txt} {self._unit}")
        if second is not None:
            self._data2.append(float(second))
            if len(self._data2) > HISTORY:
                del self._data2[:-HISTORY]
            if hasattr(self, "lbl_v2"):
                self.lbl_v2.config(text=f"{second:.0f} W")
        self._draw()

    def clear(self):
        self._data.clear()
        self._data2.clear()
        self.lbl_value.config(text="--")
        if hasattr(self, "lbl_v2"):
            self.lbl_v2.config(text="")
        self._draw()

    def series(self) -> list[float]:
        return list(self._data)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 300
        h = c.winfo_height() or 62
        if len(self._data) < 2:
            return

        # Both traces share one axis, so the range must cover both.
        pool = list(self._data)
        if len(self._data2) >= 2:
            pool += list(self._data2)
        lo, hi = min(pool), max(pool)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        pad = (hi - lo) * 0.15
        lo, hi = lo - pad, hi + pad

        # Reserve room above/below for the axis labels and a left gutter so the
        # numbers do not sit on top of the trace. The bottom margin is derived
        # from the label font's real line height: a hard-coded value clipped the
        # time ticks once Tk scaling pushed the 7pt font taller than expected.
        pad_top = 12
        tick_font = ("Microsoft YaHei UI", 7)
        try:
            from tkinter import font as tkfont
            line_h = tkfont.Font(root=self, font=tick_font).metrics("linespace")
        except Exception:
            line_h = 14
        pad_bot = int(line_h) + 8          # tick mark + label + a little air
        gutter = 52                        # wide enough for "nnn.n UNIT"
        tail = 6                           # right margin so the "0s" fits
        plot_w = max(1, w - gutter - tail)
        plot_h = max(1, h - pad_top - pad_bot)
        plot_x = gutter

        # Vertical axis: each number carries its unit
        for frac in (0.0, 0.5, 1.0):
            y = pad_top + plot_h * (1.0 - frac)
            c.create_line(plot_x, y, plot_x + plot_w, y, fill=GRID, dash=(2, 3))
            v = lo + frac * (hi - lo)
            txt = f"{v:.1f}" if abs(v) < 100 else f"{v:.0f}"
            c.create_text(plot_x - 4, y, text=f"{txt} {self._unit}",
                          anchor="e", fill=FAINT, font=tick_font)

        n = len(self._data)
        # Spread the samples we have across the full width, so a short trace
        # still reads as a trend instead of a cramped smear at the right edge.
        step = plot_w / max(1, n - 1)
        pts = []
        for i, v in enumerate(self._data):
            x = plot_x + i * step
            y = pad_top + plot_h * (1.0 - (v - lo) / (hi - lo))
            pts += [x, y]
        c.create_line(pts, fill=self._colour, width=2, smooth=True)
        # leading dot
        c.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3,
                      fill=self._colour, outline="")

        # second trace on the same axis
        if len(self._data2) >= 2:
            n2 = len(self._data2)
            step2 = plot_w / max(1, n2 - 1)
            pts2 = []
            for i, v in enumerate(self._data2):
                x = plot_x + i * step2
                y = pad_top + plot_h * (1.0 - (v - lo) / (hi - lo))
                pts2 += [x, y]
            c.create_line(pts2, fill=self._second_colour, width=2, smooth=True)
            c.create_oval(pts2[-2] - 3, pts2[-1] - 3, pts2[-2] + 3,
                          pts2[-1] + 3, fill=self._second_colour, outline="")

        # Horizontal axis: seconds elapsed across the visible window, drawn
        # last so the ticks sit on top of the fill. Three ticks only: five made
        # the edge labels of neighbouring gauges collide.
        #
        # All three use a NORTH-side anchor so they hang below the tick at the
        # same height; the end ones use nw/ne so their text grows inward and
        # cannot overflow the widget sideways. (sw/se would push them upward
        # and, with n in the middle, leave the row visibly misaligned.)
        span = max(1, n - 1)
        tick_y = pad_top + plot_h
        for frac, anchor in ((0.0, "nw"), (0.5, "n"), (1.0, "ne")):
            x = plot_x + plot_w * frac
            secs = int(round(-(1.0 - frac) * span))
            c.create_line(x, tick_y, x, tick_y + 3, fill=GRID)
            c.create_text(x, tick_y + 5, text=f"{secs}s", anchor=anchor,
                          fill=FAINT, font=tick_font)


class Dashboard(tk.Frame):
    """The full dashboard view, embedded in the main window."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self._recording = False
        self._rec_until = 0.0
        self._rec_rows: list[dict] = []
        self._last_sample = 0.0
        self._rec_file = None       # open handle while recording
        self._rec_path = ""         # destination shown in the status line

        # ---- toolbar
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(bar, text=t("dashboard"), font=F_T, bg=BG,
                 fg=FG).pack(side="left")
        self.lbl_rec = tk.Label(bar, text="", font=F, bg=BG, fg=ACCENT)
        self.lbl_rec.pack(side="left", padx=12)

        self.btn_csv = self._btn(bar, t("export_csv"), self._export_csv)
        self.btn_csv.pack(side="right")
        self.btn_png = self._btn(bar, t("export_png"), self._export_png)
        self.btn_png.pack(side="right", padx=6)

        self.btn_rec = self._btn(bar, t("start_record"), self._toggle_record,
                                 accent=True)
        self.btn_rec.pack(side="right", padx=6)
        tk.Label(bar, text=f"{t('duration')}:", font=F, bg=BG,
                 fg=DIM).pack(side="right")
        self.spn_min = tk.Spinbox(bar, from_=1, to=1440, width=5, font=F,
                                  bg=PANEL, fg=FG, insertbackground=FG,
                                  relief="flat", buttonbackground=PANEL)
        self.spn_min.delete(0, "end")
        self.spn_min.insert(0, "10")
        self.spn_min.pack(side="right", padx=6)
        tk.Label(bar, text=t("minutes"), font=F, bg=BG,
                 fg=DIM).pack(side="right")

        # ---- gauge grid (3 columns, as the original lays out its charts)
        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="g")
        for row in range(3):
            grid.rowconfigure(row, weight=1, uniform="g")

        self.gauges: dict[str, Gauge] = {}
        for i, (key, label_key, unit, nominal, est) in enumerate(GAUGES):
            colour = ACCENT if not est else "#d4663a"
            # the total-power gauge carries a second trace: MSI's summed figure
            # alongside the PSU's own measured total, on one shared axis
            second = t("gauge_total_msi") if key == "total_w" else None
            g = Gauge(grid, t(label_key), unit, colour=colour, estimate=est,
                      second_label=second)
            g.grid(row=i // 3, column=i % 3, sticky="nsew",
                   padx=(0 if i % 3 == 0 else 6, 0),
                   pady=(0 if i < 3 else 6, 0))
            self.gauges[key] = g

        # ---- status line
        self.lbl_info = tk.Label(self, text="", font=F, bg=BG, fg=DIM,
                                 anchor="w")
        self.lbl_info.pack(fill="x", padx=14, pady=(0, 10))

    def _btn(self, parent, text, cmd, accent=False):
        return tk.Button(parent, text=text, font=F,
                         bg=ACCENT if accent else PANEL, fg="white" if accent else FG,
                         relief="flat", activebackground="#8f0b20" if accent else GRID,
                         activeforeground="white", padx=12, pady=3,
                         cursor="hand2", command=cmd)

    # ------------------------------------------------------------ sampling

    def feed(self, sample: dict):
        """Called once per second with a decoded telemetry sample."""
        now = time.time()
        if now - self._last_sample < 0.9:
            return
        self._last_sample = now

        for key, _l, _u, _n, _e in GAUGES:
            v = sample.get(key)
            if v is None:
                continue
            # the total gauge receives MSI's summed value as its second trace
            second = sample.get("total_msi") if key == "total_w" else None
            self.gauges[key].add(v, second)

        if self._recording:
            row = dict(sample, t=now)
            self._rec_rows.append(row)
            self._write_rec_row(row)     # write through immediately
            left = max(0, int(self._rec_until - now))
            self.lbl_rec.config(
                text=f"{t('recording')} — {left // 60}:{left % 60:02d}")
            if now >= self._rec_until:
                self._stop_record(auto=True)

    def stop_and_close(self):
        """Called on window close so a live recording is finalised."""
        if self._recording:
            self._stop_record()

    def clear(self):
        for g in self.gauges.values():
            g.clear()

    # ----------------------------------------------------------- recording

    def _toggle_record(self):
        if self._recording:
            self._stop_record()
        else:
            try:
                minutes = max(1, int(self.spn_min.get()))
            except ValueError:
                minutes = 10
            self._rec_rows = []
            self._recording = True
            self._rec_until = time.time() + minutes * 60
            self.btn_rec.config(text=t("stop_record"), bg="#5a5f6e")
            # create the destination now so the file exists for the whole run
            self._open_rec_file()
            self.lbl_info.config(text=self._rec_path, fg=ACCENT)
            # show the countdown immediately rather than waiting for the first
            # sample to arrive
            self.lbl_rec.config(
                text=f"{t('recording')} — {minutes}:00")

    def _stop_record(self, auto: bool = False):
        self._recording = False
        self.btn_rec.config(text=t("start_record"), bg=ACCENT)
        # Clear the countdown label. It was left showing the last "recording —
        # m:ss" text after the run ended, so the UI looked like it was still
        # recording even though the timer had stopped.
        self.lbl_rec.config(text="")
        n = len(self._rec_rows)
        self._close_rec_file()
        verb = t("record_done") if auto else t("record_stopped")
        tail = f" · {self._rec_path}" if self._rec_path else ""
        self.lbl_info.config(text=f"{verb} — {n} {t('samples')}{tail}",
                             fg=OK if auto else DIM)

    # -------------------------------------------------- streaming to disk

    def _open_rec_file(self):
        """Create the destination CSV and write its header row."""
        self._close_rec_file()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(desktop_dir(), f"MSI-PSU记录-{stamp}.csv")
        cols = [t(l) for _k, l, _u, _n, _e in GAUGES] + [t("bd_total")]
        try:
            fh = open(path, "w", newline="", encoding="utf-8-sig")
            csv.writer(fh).writerow(["time"] + cols)
            fh.flush()
            self._rec_file = fh
            self._rec_path = path
        except OSError as e:
            self._rec_file = None
            self._rec_path = ""
            self.lbl_info.config(text=f"{t('write_failed')}: {e}", fg=BAD)

    def _write_rec_row(self, row: dict):
        """Append one sample and flush, so nothing waits in a buffer."""
        if self._rec_file is None:
            return
        cells = []
        for key, _l, _u, _n, _e in GAUGES:
            from psu_protocol import num
            cells.append(f"{num(row.get(key)):.4f}")
        extra = row.get("total_msi")
        cells.append(f"{extra:.4f}" if extra is not None else "")
        ts = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(row.get("t", time.time())))
        try:
            csv.writer(self._rec_file).writerow([ts] + cells)
            self._rec_file.flush()
        except (OSError, ValueError):
            pass        # a failed write must not stop sampling

    def _close_rec_file(self):
        if self._rec_file is not None:
            try:
                self._rec_file.flush()
                self._rec_file.close()
            except OSError:
                pass
            self._rec_file = None

    # -------------------------------------------------------------- export

    def _export_csv(self):
        # prefer the live stream's data; fall back to whatever the gauges hold
        rows = self._rec_rows or self._snapshot_rows()
        if not rows:
            messagebox.showinfo(t("app_title"), "No data yet.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv",
            initialfile=f"msi-psu-{time.strftime('%Y%m%d-%H%M%S')}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        cols = [k for k, *_ in GAUGES]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["time"] + [t(l) for _k, l, _u, _n, _e
                                       in GAUGES] + [t("bd_total")])
                for r in rows:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(r.get("t", time.time())))
                    # keep MSI's summed total alongside the PSU's measured one
                    extra = r.get("total_msi")
                    w.writerow([ts] + [f"{r.get(c, 0):.4f}" for c in cols]
                               + [f"{extra:.4f}" if extra is not None else ""])
            self.lbl_info.config(text=f"{t('saved_to')} {path}", fg=OK)
        except OSError as e:
            messagebox.showerror(t("app_title"), str(e), parent=self)

    def _snapshot_rows(self) -> list[dict]:
        """Build rows from whatever the gauges currently hold."""
        series = {k: self.gauges[k].series() for k, *_ in GAUGES}
        # the dual-trace total gauge keeps its second series separately
        series["total_msi"] = self.gauges["total_w"]._data2
        n = max((len(v) for v in series.values()), default=0)
        rows = []
        for i in range(n):
            row = {}
            for k, vals in series.items():
                idx = len(vals) - n + i
                row[k] = vals[idx] if 0 <= idx < len(vals) else 0.0
            rows.append(row)
        return rows

    def _grab_self_image(self):
        """Render this widget's area from the window's own pixels.

        PrintWindow asks the window to draw itself into a memory DC, so content
        drawn on top of it (the taskbar over the window's lower edge) is not
        captured. The result is then cropped to this widget's rectangle.
        Returns None if the platform call is unavailable.
        """
        import ctypes
        from ctypes import wintypes
        from PIL import Image

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        hwnd = user32.GetAncestor(self.winfo_toplevel().winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hdc = user32.GetWindowDC(hwnd)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mem, bmp)
        try:
            # 2 = PW_RENDERFULLCONTENT (needed for composited windows)
            if not user32.PrintWindow(hwnd, mem, 2):
                return None

            class BIH(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

            bi = BIH()
            bi.biSize = ctypes.sizeof(BIH)
            bi.biWidth, bi.biHeight = w, -h        # negative = top-down rows
            bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mem, bmp, 0, h, buf,
                                   ctypes.byref(bi), 0):
                return None
            full = Image.frombuffer("RGB", (w, h), buf.raw,
                                    "raw", "BGRX", 0, 1)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, hdc)

        # crop this widget out of the window image
        dx = self.winfo_rootx() - rect.left
        dy = self.winfo_rooty() - rect.top
        return full.crop((dx, dy, dx + self.winfo_width(),
                          dy + self.winfo_height()))

    def _export_png(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".png",
            initialfile=f"msi-psu-{time.strftime('%Y%m%d-%H%M%S')}.png",
            filetypes=[("PNG", "*.png")])
        if not path:
            return
        self.update_idletasks()
        try:
            img = self._grab_self_image()
        except Exception:                               # noqa: BLE001
            img = None
        if img is None:
            # fallback: a plain screen grab. This can include whatever overlaps
            # the window (the taskbar sits over its lower edge on this display).
            try:
                from PIL import ImageGrab
            except ImportError:
                messagebox.showinfo(
                    t("app_title"),
                    "PNG export needs Pillow (pip install pillow).\n"
                    "Use CSV export instead.", parent=self)
                return
            x, y = self.winfo_rootx(), self.winfo_rooty()
            img = ImageGrab.grab(
                bbox=(x, y, x + self.winfo_width(), y + self.winfo_height()))
        try:
            img.save(path)
            self.lbl_info.config(text=f"{t('saved_to')} {path}", fg=OK)
        except Exception as e:                          # noqa: BLE001
            messagebox.showerror(t("app_title"), str(e), parent=self)
