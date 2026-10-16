"""msi-psu GUI — layout and feature set matched to MSI Center's Power Supply
Unit module.

Structure taken from the shipped WPF module ``UC_Power Supply Unit.dll``
(decompiled), so the panels, their order and their controls correspond to what
MSI Center shows:

    title row          "<PSU> - <model>"                    [Dashboard]
    stat row           Output Wattage | AVG Wattage | PSU Efficiency |
                       AVG Efficiency | Temperature
    DC Output          [Multi-rail | Single-rail]  rail picker
                       12V1..12V5  V / A
                       [Apply] / [Applied]
    Fan Speed          [Performance | Customized]  duty slider  RPM
                       [Apply] / [Applied]
    Daily Activity     hours / minutes
    Total Time         hours
    Total kWh          kWh
    Consumption        weekly bar chart (7 days)

The original hides the fan panel when the Cooling Wizard owns the fan, and
swaps the DC Output panel for a simplified one on the 1600T-class units; both
behaviours are reproduced.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict
from tkinter import messagebox, ttk

from psu_protocol import (
    PCIE5_MODELS, PSU_MODELS, EnergyProfile, Psu, PsuError, num,
)
from i18n import t, alert as tr_alert
from msi_psu_dashboard import Dashboard
import win_dpi

# Declare DPI awareness at import time, before Tk builds any window.
# Without it Windows bitmap-stretches the finished window on a scaled display
# and every glyph comes out soft.
win_dpi.apply()

# ------------------------------------------------------------------ palette

BG = "#1b1d23"
PANEL = "#24262e"
CARD = "#2b2e38"
FG = "#e8e8ea"
DIM = "#8d919e"
FAINT = "#5c6070"
ACCENT = "#c8102e"        # MSI red
ACCENT_D = "#8f0b20"
OK = "#e8e8ea"          # healthy indicators read white
WARN = "#e0a92e"
BAD = "#f0524a"
TRACK = "#3a3e4a"
CHIP_ON = "#3d4250"     # selected chip fill
CHIP_OFF = "#20222a"    # unselected chip fill
GRID = "#333744"

F = ("Microsoft YaHei UI", 9)
F_S = ("Microsoft YaHei UI", 8)
F_B = ("Microsoft YaHei UI", 9, "bold")
F_T = ("Microsoft YaHei UI", 11, "bold")
F_HEAD = ("Microsoft YaHei UI", 12, "bold")   # card headings
F_COLHEAD = ("Microsoft YaHei UI", 14, "bold")  # the three column headings
F_BIG = ("Microsoft YaHei UI", 20, "bold")
F_UNIT = ("Microsoft YaHei UI", 13)       # units on the largest readouts
F_MONO = ("Consolas", 9)
F_MONO_B = ("Consolas", 11, "bold")
F_MONO_L = ("Consolas", 12, "bold")     # enlarged rail table

# Right inset shared by the fan bar and the rail current bars, so the gap
# between the fan bar and the diagram matches the left column's spacing.
COL_RIGHT_INSET = 46

# height of the rolling output-power chart (fixed, never squeezed)
SPARK_H = 92

# shared vertical padding for the header buttons, so their boxes align
BUTTON_PADY = 4

# width of the rail/connector diagram inside the DC Output panel
PORT_DIAGRAM_W = 516


def _asset_dir() -> str:
    """Directory holding bundled images, for both source and frozen builds."""
    if getattr(sys, "frozen", False):
        # PyInstaller unpacks --add-data payloads under sys._MEIPASS
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets")


def _find_portinfo_image() -> str | None:
    """Locate the rail-to-connector diagram.

    The source art is black line work on transparency. It is shown on a light
    card, exactly as MSI's own popup does — inverting it to light-on-dark made
    a technical drawing look wrong, so the original polarity is used.
    """
    d = _asset_dir()
    for name in ("portinfo_card_dark.png", "portinfo_card_light.png",
                 "portinfo_on_light_dark.png", "portinfo_on_light_light.png"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _colour_for(v: float, nominal: float, tol: float = 0.05) -> str:
    if nominal <= 0:
        return FG
    dev = abs(v - nominal) / nominal
    return OK if dev <= tol * 0.5 else (WARN if dev <= tol else BAD)


class Meter(tk.Canvas):
    """Horizontal bar used for the fan-speed and load readouts."""

    def __init__(self, parent, width=180, height=8, colour=ACCENT, **kw):
        super().__init__(parent, width=width, height=height, bg=CARD,
                         highlightthickness=0, **kw)
        self._bw, self._bh = width, height
        self._frac = 0.0
        self._track = self.create_rectangle(0, 0, width, height,
                                            fill=TRACK, outline="")
        self._bar = self.create_rectangle(0, 0, 0, height, fill=colour, outline="")
        # Track the live size: the widget is usually packed with fill="x", and
        # a track drawn only to the constructor width left the remainder
        # showing the canvas background, which read as two different shades.
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, ev):
        self._bw = max(1, ev.width)
        self._bh = max(1, ev.height)
        self.coords(self._track, 0, 0, self._bw, self._bh)
        self._redraw()

    def _redraw(self):
        self.coords(self._bar, 0, 0, self._bw * self._frac, self._bh)

    def set(self, frac: float, colour: str | None = None):
        self._frac = max(0.0, min(1.0, frac))
        if colour:
            self.itemconfig(self._bar, fill=colour)
        self._redraw()


class Slider(tk.Canvas):
    """Dark-theme horizontal slider.

    tk.Scale draws its own trough and border, which looks wrong on a dark
    panel; this draws only a track, a filled portion and a handle.
    """

    def __init__(self, parent, width=260, height=22, command=None,
                 dead_below=None, **kw):
        super().__init__(parent, width=width, height=height, bg=PANEL,
                         highlightthickness=0, **kw)
        self._cw, self._ch = width, height
        self._min, self._max = 0.0, 100.0
        self._value = 50.0
        self._command = command
        self._enabled = True
        self._dragging = False
        self._track_y = height / 2
        # Values below this are rewritten by the device (2..24 -> 1 = stopped),
        # so the region is drawn grey and the handle snaps out of it.
        self._dead_below = dead_below
        self.create_rectangle(0, self._track_y - 2, width, self._track_y + 2,
                              fill=TRACK, outline="", tags="track")
        # grey dead-zone band, drawn over the track
        self._dead = self.create_rectangle(0, self._track_y - 2, 0,
                                           self._track_y + 2,
                                           fill="#2a2d36", outline="")
        self._fill = self.create_rectangle(0, self._track_y - 2, 0,
                                           self._track_y + 2,
                                           fill=ACCENT, outline="")
        # 25% boundary marker (a thin vertical tick) plus the three scale
        # labels. All labels share one baseline inside the canvas so 0% / 25%
        # / 100% line up; a separate row underneath could never match.
        tick_font = ("Microsoft YaHei UI", 9)
        self._mark = self.create_line(0, self._track_y - 6, 0,
                                      self._track_y + 6, fill=DIM, width=1)
        # 0 is the stopped state, so label it as such rather than "0%"
        self._lbl_min = self.create_text(0, 0, text=t("stopped"), anchor="w",
                                         fill=FAINT, font=tick_font)
        self._lbl_max = self.create_text(0, 0, text="100%", anchor="e",
                                         fill=FAINT, font=tick_font)
        self._mark_lbl = self.create_text(0, 0, text="", fill=FAINT,
                                          font=tick_font)
        # Percentage bubble that follows the handle while dragging.
        self._bubble = self.create_rectangle(0, 0, 0, 0, fill=CARD,
                                             outline="", state="hidden")
        self._bubble_txt = self.create_text(0, 0, text="", fill=FG,
                                            font=("Microsoft YaHei UI", 8),
                                            state="hidden")
        self._knob = self.create_oval(0, 0, 0, 0, fill=FG, outline="")
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", self._on_resize)
        self._redraw()

    def _on_resize(self, ev):
        self._cw = ev.width
        self.coords("track", 0, self._track_y - 2, self._cw, self._track_y + 2)
        self._redraw()

    def _frac(self) -> float:
        span = self._max - self._min
        return (self._value - self._min) / span if span else 0.0

    def _x_for(self, value: float) -> float:
        r = 8
        span = self._max - self._min
        frac = (value - self._min) / span if span else 0.0
        return frac * max(1, self._cw - 2 * r) + r

    def _redraw(self):
        r = 8
        x = self._x_for(self._value)
        self.coords(self._fill, 0, self._track_y - 2, x, self._track_y + 2)
        self.coords(self._knob, x - r, self._track_y - r,
                    x + r, self._track_y + r)
        colour = FG if self._enabled else FAINT
        self.itemconfig(self._knob, fill=colour)
        # The live fill spans from 0 to the handle, so it would paint over the
        # grey band. Raise the band (and the marker) above it, and keep the
        # handle on top of everything.
        self.tag_raise(self._dead)
        self.tag_raise(self._mark)
        self.tag_raise(self._mark_lbl)
        self.tag_raise(self._lbl_min)
        self.tag_raise(self._lbl_max)
        self.tag_raise(self._knob)
        self.tag_raise(self._bubble)
        self.tag_raise(self._bubble_txt)
        self._place_bubble(x)
        # Shared baseline for every scale label. Placed below the track with
        # a real gap: when the font grew from 7pt to 9pt the glyph tops ended
        # up 4px above the track's bottom edge, i.e. overlapping it.
        ly = self._track_y + 16
        self.coords(self._lbl_min, 4, ly)
        self.coords(self._lbl_max, max(4, self._cw - 4), ly)
        # dead zone + boundary marker
        if self._dead_below:
            dx = self._x_for(self._dead_below)
            self.coords(self._dead, 0, self._track_y - 2, dx,
                        self._track_y + 2)
            self.itemconfig(self._dead, state="normal")
            self.coords(self._mark, dx, self._track_y - 7, dx,
                        self._track_y + 7)
            self.coords(self._mark_lbl, dx, ly)
            self.itemconfig(self._mark_lbl,
                            text=f"{self._dead_below:g}%")
            self.itemconfig(self._mark, state="normal")
            self.itemconfig(self._mark_lbl, state="normal")
        else:
            self.itemconfig(self._dead, state="hidden")
            self.itemconfig(self._mark, state="hidden")
            self.itemconfig(self._mark_lbl, state="hidden")

    def _place_bubble(self, x: float):
        """Draw the live percentage above the handle, clamped to the canvas."""
        if not self._dragging:
            self.itemconfig(self._bubble, state="hidden")
            self.itemconfig(self._bubble_txt, state="hidden")
            return
        txt = f"{self._value:.0f}%"
        self.itemconfig(self._bubble_txt, text=txt, state="normal")
        bx0, by0, bx1, by1 = self.bbox(self._bubble_txt)
        bw = (bx1 - bx0) / 2 + 8
        bh = 11
        cy = self._track_y - 16
        cx = min(max(x, bw), self._cw - bw)
        self.coords(self._bubble, cx - bw, cy - bh, cx + bw, cy + bh)
        self.coords(self._bubble_txt, cx, cy)
        self.itemconfig(self._bubble, state="normal")
        self.itemconfig(self._bubble_txt, state="normal")

    def _pos_to_value(self, x: float) -> float:
        r = 8
        span = max(1, self._cw - 2 * r)
        frac = min(1.0, max(0.0, (x - r) / span))
        v = self._min + frac * (self._max - self._min)
        # Snap out of the dead zone: the hardware rewrites those values, so
        # parking the handle there would misrepresent what is applied.
        if self._dead_below and v < self._dead_below:
            # nearer to 0 means the user wants it off; nearer to the boundary
            # means they want the lowest usable speed
            v = 0.0 if v < self._dead_below / 2 else self._dead_below
        return v

    def _on_press(self, ev):
        if not self._enabled:
            return
        self._dragging = True
        self.set(self._pos_to_value(ev.x))
        if self._command:
            self._command(self._value)

    def _on_drag(self, ev):
        if self._dragging and self._enabled:
            self.set(self._pos_to_value(ev.x))
            if self._command:
                self._command(self._value)

    def _on_release(self, _ev):
        self._dragging = False
        self._redraw()

    def set(self, value: float):
        self._value = max(self._min, min(self._max, float(value)))
        self._redraw()

    def get(self) -> float:
        return self._value

    def config_state(self, enabled: bool):
        self._enabled = enabled
        self._redraw()


class Section(tk.Frame):
    """A titled panel with a header bar, matching the module's card style."""

    def __init__(self, parent, title: str, width: int | None = None):
        super().__init__(parent, bg=PANEL)
        self.head = tk.Frame(self, bg=PANEL)
        # An empty title (the DC-output card, whose three columns carry their
        # own headings) used to still pack the header strip, leaving a blank
        # 10px band above the content.
        if title:
            self.head.pack(fill="x", padx=10, pady=(8, 2))
            tk.Label(self.head, text=title, font=F_HEAD, bg=PANEL,
                     fg=FG).pack(side="left")
            self.title_label = self.head.winfo_children()[-1]
        else:
            self.title_label = None
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        if width:
            self.configure(width=width)


def _attach_tooltip(widget, text: str):
    """Minimal hover tooltip (no extra dependency)."""
    tip = {"win": None}

    def show(_e=None):
        if tip["win"] is not None:
            return
        w = tk.Toplevel(widget)
        w.wm_overrideredirect(True)
        w.configure(bg="#3a3e4a")
        tk.Label(w, text=text, font=F_S, bg="#3a3e4a", fg=FG,
                 padx=8, pady=4).pack()
        x = widget.winfo_rootx() + widget.winfo_width() + 6
        y = widget.winfo_rooty()
        w.wm_geometry(f"+{x}+{y}")
        tip["win"] = w

    def hide(_e=None):
        if tip["win"] is not None:
            tip["win"].destroy()
            tip["win"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<Button-1>", hide, add="+")


class Stat(tk.Frame):
    """One figure in the top statistics row."""

    def __init__(self, parent, title: str, unit: str, big: bool = False):
        super().__init__(parent, bg=CARD)
        tk.Label(self, text=title, font=F, bg=CARD, fg=DIM).pack(anchor="w",
                                                                 padx=12, pady=(8, 0))
        row = tk.Frame(self, bg=CARD)
        row.pack(anchor="w", padx=12, pady=(0, 8))
        self.value = tk.Label(row, text="--", font=F_BIG if big else F_T,
                              bg=CARD, fg=FG)
        self.value.pack(side="left")
        self.unit = tk.Label(row, text=unit,
                             font=F_UNIT if big else F, bg=CARD, fg=DIM)
        self.unit.pack(side="left", padx=(4, 0), pady=(6 if big else 2, 0))

    def set(self, text: str, colour: str = FG):
        self.value.config(text=text, fg=colour)


class App(tk.Tk):
    def __init__(self, pid: int | None = None):
        super().__init__()
        self.title(t("app_title"))
        self.configure(bg=BG)
        # Render at the display's true DPI instead of being bitmap-stretched,
        # which is what makes text look blurry on a scaled monitor.
        self.dpi_scale = win_dpi.setup_tk(self, 1180, 780)

        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._psu: Psu | None = None
        self._connected = False
        self._writing = False
        self._interval = 1.0
        self._supported_modes = [1, 3]
        self._cooling_wizard = False
        self._last = None
        self._last_fan = None
        self._fail_count = 0
        self._mode = 1
        self._custom_duty = 50
        self._history: list[float] = []
        # True while the user has a selection that has not been applied yet.
        # Without these the 1 Hz poll overwrites the radio buttons and the
        # choice appears to snap back on its own.
        self._rail_dirty = False
        self._fan_dirty = False
        self._last_ocp: bool | None = None

        # energy accounting, stored beside the tool rather than in MSI's tree
        prof_dir = os.path.join(os.path.expanduser("~"), ".msi-psu")
        self._profile = EnergyProfile.load(os.path.join(prof_dir, "profile.json"))
        self._last_sample = time.time()

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._pump)
        threading.Thread(target=self._worker, args=(pid,), daemon=True).start()

    # ------------------------------------------------------------- layout

    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TCombobox", font=F)
        style.configure("MSI.TRadiobutton", background=PANEL, foreground=FG,
                        font=F)
        style.map("MSI.TRadiobutton", background=[("active", PANEL)])

        # ---- title row
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=14, pady=(10, 6))
        self.lbl_title = tk.Label(head, text=t("psu"),
                                  font=("Microsoft YaHei UI", 16, "bold"),
                                  bg=BG, fg=FG)
        self.lbl_title.pack(side="left")
        # The connection state sits right after the model name, prefixed with
        # "状态：", rather than floating at the far right of the row.
        self.lbl_state_caption = tk.Label(head, text=t("state_label"),
                                          font=F, bg=BG, fg=DIM)
        self.lbl_state_caption.pack(side="left", padx=(12, 4))
        self.lbl_status = tk.Label(head, text=t("connecting"), font=F, bg=BG,
                                   fg=DIM)
        self.lbl_status.pack(side="left")
        # Separate label for transient write results, so the connection state
        # above is never overwritten by an "applied" message.
        self.lbl_write_msg = tk.Label(head, text="", font=F, bg=BG, fg=DIM)
        self.lbl_write_msg.pack(side="left", padx=(14, 0))

        # dashboard toggle (the module's bt_Dashboard) — plain filled box,
        # same geometry as the temperature-unit button beside it
        self.btn_dash = tk.Button(
            head, text=t("dashboard"), font=F, bg=ACCENT, fg="white",
            relief="flat", bd=0, highlightthickness=0,
            activebackground=ACCENT_D, activeforeground="white",
            padx=14, pady=BUTTON_PADY, cursor="hand2",
            command=self._toggle_dashboard)
        self.btn_dash.pack(side="right", padx=(10, 0))

        # Temperature unit. Only the unit symbol (C/F) sits in a filled box —
        # that box is the clickable affordance. The "温度单位" wording beside it
        # is plain text on the panel background, as requested.
        # Both pack right-to-left, so the box keeps the same pady as the
        # dashboard button and the two line up top and bottom.
        self._fahrenheit = False
        self.btn_unit = tk.Button(
            head, text=t("unit_c"), font=F, bg=CARD, fg=FG,
            relief="flat", bd=0, highlightthickness=0,
            activebackground=TRACK, activeforeground=FG,
            padx=12, pady=BUTTON_PADY, cursor="hand2",
            command=self._toggle_unit)
        self.btn_unit.pack(side="right", padx=(10, 0))

        self.lbl_unit = tk.Label(head, text=t("temp_unit"), font=F, bg=BG,
                                 fg=DIM, cursor="hand2")
        self.lbl_unit.pack(side="right", padx=(10, 8))
        # the wording is clickable too, but carries no box of its own
        self.lbl_unit.bind("<Button-1>", lambda e: self._toggle_unit())

        # two swappable pages: the main view and the dashboard
        self.page_main = tk.Frame(self, bg=BG)
        self.page_dash = tk.Frame(self, bg=BG)
        self.dashboard = Dashboard(self.page_dash, self)
        self.dashboard.pack(fill="both", expand=True)
        self.page_main.pack(fill="both", expand=True)

        self._build_main(self.page_main)

    def _build_main(self, root):
        # ---- statistics row
        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", padx=14)
        self.st_out = Stat(row, t("output_wattage"), "W", big=True)
        self.st_avgw = Stat(row, t("avg_wattage"), "W", big=True)
        self.st_eff = Stat(row, t("psu_efficiency"), "%", big=True)
        self.st_avge = Stat(row, t("avg_efficiency"), "%", big=True)
        self.st_temp = Stat(row, t("temperature"), t("unit_c"), big=True)
        for i, s in enumerate((self.st_out, self.st_avgw, self.st_eff,
                               self.st_avge, self.st_temp)):
            s.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            row.columnconfigure(i, weight=1)

        # ---- middle: DC output + fan speed
        mid = tk.Frame(root, bg=BG)
        mid.pack(fill="both", expand=True, padx=14, pady=8)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        # Fan controls live inside the DC Output panel, between the readouts
        # and the diagram, so the whole power section reads as one card.
        self._build_dc_output(mid)

        # ---- bottom row: breakdown (left), statistics, weekly chart.
        # All three cards share the width equally; weighting only the middle
        # column made the outer two size to their content and look narrower.
        bot = tk.Frame(root, bg=BG)
        bot.pack(fill="x", padx=14, pady=(0, 12))
        for col in (0, 1, 2):
            bot.columnconfigure(col, weight=1, uniform="bottom")
        bot.rowconfigure(0, weight=1)

        self._build_breakdown(bot)
        self._build_stats(bot)
        self._build_chart(bot)

        # ---- protection alerts strip (the module raises toasts for these)
        self.alert_bar = tk.Frame(root, bg=BG)
        self.alert_bar.pack(fill="x", padx=14, pady=(0, 10))
        self.lbl_alerts = tk.Label(self.alert_bar, text="", font=F, bg=BG,
                                   fg=DIM, anchor="w")
        self.lbl_alerts.pack(side="left")
        self.lbl_ports = tk.Label(self.alert_bar, text="", font=F_MONO, bg=BG,
                                  fg=DIM, anchor="e")
        self.lbl_ports.pack(side="right")

    def _toggle_dashboard(self):
        """Swap between the main view and the scrolling dashboard."""
        if self.page_dash.winfo_ismapped():
            self.page_dash.pack_forget()
            self.page_main.pack(fill="both", expand=True)
            self.btn_dash.config(text=t("dashboard"), bg=ACCENT)
        else:
            self.page_main.pack_forget()
            self.page_dash.pack(fill="both", expand=True)
            self.btn_dash.config(text=t("back"), bg=CARD)

    def _build_portinfo(self, parent):
        """Rail-to-connector diagram, placed beside the rail readouts.

        Artwork is MSI's own ic_psu_popinfo PNG from UC_Power Supply Unit.dll,
        kept in its original black-on-white polarity and shown on a white card
        the way MSI's own popup presents it.
        """
        holder = tk.Frame(parent, bg="#ffffff", highlightthickness=1,
                          highlightbackground=TRACK)
        # anchor="se" pins the diagram to the bottom-RIGHT of its cell, so it
        # sits against the card's right edge and lines up with the Apply row
        holder.pack(anchor="se", side="bottom")
        self._port_thumb = None
        self._port_full_img = None

        path = _find_portinfo_image()
        if not path:
            tk.Label(holder, text=t("portinfo_missing"), font=F_S, bg=PANEL,
                     fg=WARN, wraplength=300, justify="left").pack(
                padx=6, pady=6)
            return holder

        try:
            from PIL import Image, ImageTk
            im = Image.open(path).convert("RGBA")
        except Exception:
            tk.Label(holder, text=t("portinfo_missing"), font=F_S, bg=PANEL,
                     fg=WARN, wraplength=300, justify="left").pack(
                padx=6, pady=6)
            return holder

        self._port_full_img = im
        # one fixed size — no dynamic refitting
        ratio = PORT_DIAGRAM_W / im.width
        thumb = im.resize((PORT_DIAGRAM_W, int(im.height * ratio)),
                          Image.LANCZOS)
        self._port_thumb = ImageTk.PhotoImage(thumb)

        lbl = tk.Label(holder, image=self._port_thumb, bg="#ffffff",
                       bd=0, cursor="hand2")
        lbl.pack(padx=3, pady=3)
        lbl.bind("<Button-1>", lambda e: self._show_port_zoom())
        _attach_tooltip(lbl, t("portinfo_click"))
        tk.Label(holder, text=t("portinfo_click"), font=F_S, bg="#ffffff",
                 fg="#7a7f8a", cursor="hand2").pack(pady=(0, 4))
        return holder

    def _show_port_zoom(self):
        """Open the diagram large, with scroll-to-zoom and drag-to-pan."""
        if getattr(self, "_port_win", None) is not None:
            try:
                self._port_win.lift()
                return
            except tk.TclError:
                self._port_win = None
        if not getattr(self, "_port_full_img", None):
            return

        win = tk.Toplevel(self)
        self._port_win = win
        win.title(t("portinfo_title"))
        win.configure(bg=PANEL)

        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(bar, text=t("portinfo_title"), font=F_T, bg=PANEL,
                 fg=FG).pack(side="left")
        tk.Button(bar, text=t("close"), font=F_S, bg=ACCENT, fg="white",
                  relief="flat", activebackground=ACCENT_D,
                  activeforeground="white", padx=14, pady=2, cursor="hand2",
                  command=self._close_port_popup).pack(side="right")

        canvas = tk.Canvas(win, bg="#f0f1f3", highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # "fit" scales the image to the window and follows resizes; once the
        # user zooms manually that override is kept until they press Fit again.
        state = {"fit": True, "scale": 1.0, "photo": None}

        def fit_scale():
            iw, ih = self._port_full_img.size
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            return max(0.05, min(cw / iw, ch / ih))

        def render():
            from PIL import Image, ImageTk
            im = self._port_full_img
            sc = fit_scale() if state["fit"] else state["scale"]
            w = max(1, int(im.width * sc))
            h = max(1, int(im.height * sc))
            resized = im.resize((w, h), Image.LANCZOS) if sc != 1.0 else im
            state["photo"] = ImageTk.PhotoImage(resized)
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=state["photo"])
            canvas.configure(scrollregion=(0, 0, w, h))
            state["shown"] = sc

        def zoom(direction):
            # leaving fit mode starts from whatever is currently displayed
            base = state.get("shown", 1.0) if state["fit"] else state["scale"]
            state["fit"] = False
            state["scale"] = max(0.1, min(8.0,
                                          base * (1.15 if direction > 0
                                                  else 1 / 1.15)))
            render()

        def refit():
            state["fit"] = True
            render()

        canvas.bind("<MouseWheel>",
                    lambda e: zoom(1 if e.delta > 0 else -1))
        canvas.bind("<Button-4>", lambda e: zoom(1))
        canvas.bind("<Button-5>", lambda e: zoom(-1))
        canvas.bind("<ButtonPress-1>", lambda e: canvas.scan_mark(e.x, e.y))
        canvas.bind("<B1-Motion>",
                    lambda e: canvas.scan_dragto(e.x, e.y, gain=1))
        # follow window resizes while in fit mode
        canvas.bind("<Configure>", lambda e: render() if state["fit"] else None)

        btns = tk.Frame(win, bg=PANEL)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(btns, text=t("zoom_in"), font=F_S, bg=CARD, fg=FG,
                  relief="flat", activebackground=TRACK, activeforeground=FG,
                  padx=10, cursor="hand2",
                  command=lambda: zoom(1)).pack(side="left", padx=(0, 6))
        tk.Button(btns, text=t("zoom_out"), font=F_S, bg=CARD, fg=FG,
                  relief="flat", activebackground=TRACK, activeforeground=FG,
                  padx=10, cursor="hand2",
                  command=lambda: zoom(-1)).pack(side="left", padx=(0, 6))
        tk.Button(btns, text=t("zoom_fit"), font=F_S, bg=CARD, fg=FG,
                  relief="flat", activebackground=TRACK, activeforeground=FG,
                  padx=10, cursor="hand2",
                  command=refit).pack(side="left")
        tk.Label(btns, text=t("zoom_hint"), font=F_S, bg=PANEL,
                 fg=FAINT).pack(side="left", padx=10)

        # size the window to the image's aspect ratio, then let it be resized
        iw, ih = self._port_full_img.size
        avail_h = max(400, self.winfo_screenheight() - 220)
        scale = min(1100 / iw, avail_h / ih)
        win.geometry(f"{int(iw * scale)}x{int(ih * scale) + 96}")
        win.minsize(480, 360)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._close_port_popup)
        # render once the canvas has real dimensions
        win.after(80, render)

    def _close_port_popup(self):
        if getattr(self, "_port_win", None) is not None:
            try:
                self._port_win.destroy()
            except tk.TclError:
                pass
            self._port_win = None

    # -- DC output ---------------------------------------------------------

    def _build_dc_output(self, parent):
        # No Section wrapper title: the card holds three side-by-side columns,
        # each with its own heading, so a card-level "直流输出" would duplicate
        # the first column's label.
        sec = Section(parent, "")
        sec.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        b = sec.body

        # Three columns inside one card: rail readouts, fan controls, diagram.
        # uniform="cols" gives equal widths.
        #
        # Every column owns a heading row plus a content row, and the content
        # row absorbs the slack (rowconfigure(1, weight=1)). Headings therefore
        # share the top baseline and the three bottoms share the other, no
        # matter how tall each block is.
        cols = tk.Frame(b, bg=PANEL)
        cols.pack(fill="both", expand=True)
        for col in (0, 1, 2):
            cols.columnconfigure(col, weight=1, uniform="cols")
        cols.rowconfigure(0, weight=1)

        # A uniform gap on the left of the middle and right columns keeps the
        # three blocks evenly separated instead of the fan column sitting
        # flush against the rail readouts.
        COL_GAP = 24
        left = tk.Frame(cols, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew")
        midcol = tk.Frame(cols, bg=PANEL)
        midcol.grid(row=0, column=1, sticky="nsew", padx=(COL_GAP, 0))
        right = tk.Frame(cols, bg=PANEL)
        right.grid(row=0, column=2, sticky="nsew", padx=(COL_GAP, 0))

        for frame in (left, midcol, right):
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(1, weight=1)      # content row takes the slack

        # headings, one per column, all on the same baseline as the card title
        # above them (same font, same 10px inset, same padding)
        def heading(frame, text, col=0):
            # padx=0 so the glyphs sit on the column edge, matching the
            # content below (padx=10 left all three headings 10px right).
            lbl = tk.Label(frame, text=text, font=F_COLHEAD, bg=PANEL, fg=FG)
            lbl.grid(row=0, column=col, sticky="w", padx=0, pady=(6, 2))
            return lbl

        heading(left, t("dc_output"))
        heading(midcol, t("fan_speed"))
        heading(right, t("connector_layout"))

        # content lives in row 1 of each column; it absorbs the slack so the
        # bottom edge lands on the shared baseline
        lbody = tk.Frame(left, bg=PANEL)
        lbody.grid(row=1, column=0, sticky="nsew")
        mbody = tk.Frame(midcol, bg=PANEL)
        mbody.grid(row=1, column=0, sticky="nsew")
        # Give each body a bottom row that hugs the baseline. The body itself
        # fills the cell, and the action row is packed with side="bottom" so it
        # sits at the very bottom regardless of how tall the content above is.
        lbody.columnconfigure(0, weight=1)
        lbody.rowconfigure(0, weight=1)
        mbody.columnconfigure(0, weight=1)
        mbody.rowconfigure(0, weight=1)
        ltop = tk.Frame(lbody, bg=PANEL)
        ltop.grid(row=0, column=0, sticky="nsew")
        lbot = tk.Frame(lbody, bg=PANEL)
        lbot.grid(row=1, column=0, sticky="ew")
        mtop = tk.Frame(mbody, bg=PANEL)
        mtop.grid(row=0, column=0, sticky="nsew")
        mbot = tk.Frame(mbody, bg=PANEL)
        mbot.grid(row=1, column=0, sticky="ew")
        left, midcol = ltop, mtop
        # kept as attributes because _build_fan() runs later and packs its
        # action row into the bottom cell of this column
        self._lbot = lbot
        self._mbot = mbot

        # rail mode radios
        modes = tk.Frame(left, bg=PANEL)
        modes.pack(fill="x", pady=(0, 6))
        self.var_rail = tk.StringVar(value="single")
        self.rb_multi = self._make_chip(
            modes, t("multi_rail"), lambda: self._set_rail_mode("multi"),
            first=True)
        self.rb_single = self._make_chip(
            modes, t("single_rail"), lambda: self._set_rail_mode("single"))
        # The rail-limit note lives below the 12V5 row instead of crowding the
        # mode row; see _build_rail_note().
        self._sync_rail_mode_chips()

        # rail picker
        pick = tk.Frame(left, bg=PANEL)
        pick.pack(fill="x", pady=(0, 6))
        tk.Label(pick, text=t("rail"), font=F, bg=PANEL, fg=DIM).pack(side="left")
        # Plain clickable labels instead of a readonly Combobox: the combobox
        # drew a bright system-styled box that clashed with the panel.
        self._rail_names = ["12V", "5V", "3.3V"]
        self._rail_sel = "12V"
        self._rail_btns = {}
        for _n in self._rail_names:
            _first = (_n == "12V")
            _l = tk.Label(pick, text=_n, font=F, bg=CHIP_OFF, fg=DIM,
                          padx=0 if _first else 10, pady=2, cursor="hand2")
            _l.pack(side="left", padx=(0, 0) if _first else (6, 0),
                     ipadx=10 if _first else 0)
            _l.bind("<Button-1>", lambda e, n=_n: self._select_rail(n))
            self._rail_btns[_n] = _l
        self._sync_rail_buttons()
        # "电压 / 电流" belongs under the selector, centred over the numbers

        # Selected rail, large. The wrapper shrink-wraps (no fill) so the
        # caption centred inside it is centred on the *numbers*, not on the
        # whole column, which is much wider.
        # Voltage and current each get their own column: caption centred above,
        # number + unit centred below. Grouping them lets each caption track its
        # own number instead of the width of both together.
        wrap = tk.Frame(left, bg=PANEL)
        wrap.pack(anchor="w", pady=(4, 8))
        self.single = wrap

        self.col_v = tk.Frame(wrap, bg=PANEL)
        self.col_v.pack(side="left", padx=(0, 26))
        # DIM, matching the rail-limit note below: FAINT was the dimmest tone
        # in the palette and read as disabled next to it.
        self.lbl_cap_v = tk.Label(self.col_v, text=t("caption_voltage"),
                                  font=F, bg=PANEL, fg=DIM)
        self.lbl_cap_v.pack()
        row_v = tk.Frame(self.col_v, bg=PANEL)
        row_v.pack()
        self.lbl_single_v = tk.Label(row_v, text="--", font=F_BIG,
                                     bg=PANEL, fg=FG)
        self.lbl_single_v.pack(side="left")
        tk.Label(row_v, text="V", font=F_UNIT, bg=PANEL, fg=DIM).pack(
            side="left", padx=(5, 0), pady=(0, 2))

        self.col_a = tk.Frame(wrap, bg=PANEL)
        self.col_a.pack(side="left")
        self.lbl_cap_a = tk.Label(self.col_a, text=t("caption_current"),
                                  font=F, bg=PANEL, fg=DIM)
        self.lbl_cap_a.pack()
        row_a = tk.Frame(self.col_a, bg=PANEL)
        row_a.pack()
        self.lbl_single_a = tk.Label(row_a, text="--", font=F_BIG,
                                     bg=PANEL, fg=FG)
        self.lbl_single_a.pack(side="left")
        tk.Label(row_a, text="A", font=F_UNIT, bg=PANEL, fg=DIM).pack(
            side="left", padx=(5, 0), pady=(0, 2))

        # 12V per-rail table
        self.tbl = tk.Frame(left, bg=PANEL)
        self.tbl.pack(fill="x")
        self.rail_rows = {}
        for i, name in enumerate(["12V1", "12V2", "12V3", "12V4", "12V5"]):
            tk.Label(self.tbl, text=name, font=F_MONO_L, bg=PANEL, fg=DIM,
                     width=6, anchor="w").grid(row=i, column=0, sticky="w",
                                               pady=1)
            v = tk.Label(self.tbl, text="--", font=F_MONO_L, bg=PANEL, fg=FG,
                         width=8, anchor="e")
            v.grid(row=i, column=1, sticky="e", padx=4)
            tk.Label(self.tbl, text="V", font=F, bg=PANEL, fg=FAINT).grid(
                row=i, column=2, sticky="w")
            a = tk.Label(self.tbl, text="--", font=F_MONO_L, bg=PANEL, fg=FG,
                         width=8, anchor="e")
            a.grid(row=i, column=3, sticky="e", padx=(16, 4))
            tk.Label(self.tbl, text="A", font=F, bg=PANEL, fg=FAINT).grid(
                row=i, column=4, sticky="w")
            load = Meter(self.tbl, width=140, height=8)
            load.grid(row=i, column=5, padx=(16, COL_RIGHT_INSET), sticky="w")
            self.rail_rows[name] = (v, a, load)

        # Rail current limits, in the space under the table. Taken from MSI's
        # own manual: single-rail +12V is 108.33A; in multi-rail 12V1 is 40A
        # and 12V2..12V5 are 50A each.
        self.lbl_rail_hint = tk.Label(left, text="", font=F, bg=PANEL, fg=DIM,
                                      justify="left", anchor="w",
                                      wraplength=520)
        self.lbl_rail_hint.pack(fill="x", pady=(10, 0))

        act = tk.Frame(self._lbot, bg=PANEL)
        act.pack(fill="x", pady=(12, 0))
        self.btn_rail_apply = tk.Button(
            act, text=t("apply"), font=F, bg=ACCENT, fg="white", relief="flat",
            activebackground=ACCENT_D, activeforeground="white", padx=18,
            pady=3, cursor="hand2", command=self._on_rail_apply)
        self.btn_rail_apply.pack(side="left")
        self.lbl_rail_applied = tk.Label(act, text="", font=F, bg=PANEL, fg=OK)
        self.lbl_rail_applied.pack(side="left", padx=10)

        # fan controls (heading already placed by heading() above)
        self._build_fan(midcol)

        # the diagram occupies row 1 of the right column, anchored bottom-right
        rbody = tk.Frame(right, bg=PANEL)
        rbody.grid(row=1, column=0, sticky="nsew")
        rbody.columnconfigure(0, weight=1)
        rbody.rowconfigure(0, weight=1)
        port = self._build_portinfo(rbody)
        port.grid(row=0, column=0, sticky="sew")

        # live power history spans the full panel width, below all columns.
        # It expands into whatever vertical space is left in the card, with
        # SPARK_H as the minimum — a fixed height left the bottom of the card
        # empty while the axis was cramped.
        hist = tk.Frame(b, bg=PANEL)
        hist.pack(fill="both", expand=True, pady=(12, 0))
        tk.Label(hist, text=t("output_power_60s"), font=F, bg=PANEL,
                 fg=FAINT).pack(anchor="w")
        self.spark = tk.Canvas(hist, height=SPARK_H, bg=PANEL,
                               highlightthickness=0)
        self.spark.pack(fill="both", expand=True)
        self.spark.bind("<Configure>", lambda e: self._draw_spark())

    # -- fan ---------------------------------------------------------------

    def _build_fan(self, parent):
        """Fan controls; embedded in the DC Output card, not a separate panel."""
        b = parent

        # Live RPM. The number and its unit share one row; the bar sits on
        # its own full-width row underneath so it can run the panel's width
        # instead of being squeezed beside the number.
        rpm = tk.Frame(b, bg=PANEL)
        rpm.pack(fill="x")
        self.lbl_rpm = tk.Label(rpm, text="--", font=F_BIG, bg=PANEL, fg=FG)
        self.lbl_rpm.pack(side="left")
        tk.Label(rpm, text="RPM", font=F, bg=PANEL, fg=DIM).pack(
            side="left", padx=(4, 0), pady=(8, 0))

        # The 20pt digits have a tall glyph box, so a 2px gap made the bar
        # look like it was touching them; 10px reads clearly separated.
        # The right inset matches the gap between the rail current bars and the
        # column edge, so both sides of the card are spaced the same.
        self.meter_rpm = Meter(b, width=300, height=14)
        self.meter_rpm.pack(fill="x", padx=(0, COL_RIGHT_INSET), pady=(10, 4))

        # mode radios — the module offers exactly these two
        modes = tk.Frame(b, bg=PANEL)
        modes.pack(fill="x", pady=(10, 4))
        self.var_fan = tk.StringVar(value="perf")
        self.rb_perf = self._make_chip(
            modes, t("performance"), lambda: self._set_fan_mode("perf"),
            first=True)
        self.rb_cust = self._make_chip(
            modes, t("customized"), lambda: self._set_fan_mode("cust"))
        self._sync_fan_mode_chips()

        # duty slider
        self.duty_box = tk.Frame(b, bg=PANEL)
        self.duty_box.pack(fill="x", pady=(6, 2))
        self.sl_duty = Slider(self.duty_box, width=300, height=58,
                              command=self._on_duty_move, dead_below=25)
        self.sl_duty.pack(fill="x")
        self.lbl_duty = tk.Label(b, text="", font=F, bg=PANEL, fg=DIM)
        self.lbl_duty.pack(anchor="w")

        # Why this exists: the device rewrites part of the duty range, which
        # looks like a broken slider unless it is called out.
        tk.Label(self.duty_box, text=t("duty_deadzone"), font=F, bg=PANEL,
                 fg=DIM, justify="left",
                 wraplength=430).pack(anchor="w", pady=(4, 0))

        # Mode explanation, mirroring the original's two hint texts.
        self.lbl_fan_tip = tk.Label(b, text="", font=F, bg=PANEL, fg=DIM,
                                    justify="left", wraplength=430)
        self.lbl_fan_tip.pack(anchor="w", pady=(2, 0))

        act = tk.Frame(self._mbot, bg=PANEL)
        act.pack(fill="x", pady=(8, 0))
        self.btn_fan_apply = tk.Button(
            act, text=t("apply"), font=F, bg=ACCENT, fg="white", relief="flat",
            activebackground=ACCENT_D, activeforeground="white", padx=18,
            pady=3, cursor="hand2", command=self._on_fan_apply)
        self.btn_fan_apply.pack(side="left")
        self.lbl_fan_applied = tk.Label(act, text="", font=F, bg=PANEL, fg=OK)
        self.lbl_fan_applied.pack(side="left", padx=10)

        # Zero-fan state. The Ai1000P / Ai1300P / Ai1600T do not report it at
        # all — the original returns early for exactly those PIDs, so the field
        # stays at its default and showing "off" would be a fabricated reading.
        # On those models the label is hidden entirely rather than announcing a
        # capability the device does not have.
        self.lbl_zf = tk.Label(self._mbot, text="", font=F, bg=PANEL, fg=FAINT)
        self._zf_supported = None

    # -- statistics --------------------------------------------------------

    def _build_stats(self, parent):
        sec = Section(parent, t("power_stats"))
        sec.grid(row=0, column=1, sticky="nsew")
        b = sec.body

        self.lbl_daily = self._stat_line(b, t("daily_activity"))
        self.lbl_total = self._stat_line(b, t("total_time"))
        self.lbl_kwh = self._stat_line(b, t("total_kwh"))
        self.lbl_avge2 = self._stat_line(b, t("avg_efficiency"))

        self.btn_reset = tk.Button(
            b, text=t("reset_stats"), font=F, bg=CARD, fg=DIM, relief="flat",
            activebackground=TRACK, activeforeground=FG, padx=10, pady=2,
            cursor="hand2", command=self._on_reset_stats)
        self.btn_reset.pack(anchor="w", pady=(8, 0))

    def _stat_line(self, parent, label: str):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, font=F, bg=PANEL, fg=DIM, width=18,
                 anchor="w").pack(side="left")
        v = tk.Label(row, text="--", font=F_MONO_B, bg=PANEL, fg=FG, anchor="e")
        v.pack(side="right")
        return v

    # -- weekly chart ------------------------------------------------------

    def _build_breakdown(self, parent):
        """CPU / GPU / total split of the output wattage.

        MSI's dashboard graphs three traces derived from the 12V rails rather
        than from real sensors:

            CPU   = 12V2_V x 12V2_A
            GPU   = 12V3 + 12V4 + 12V5
            total = CPU + GPU + 12V1 + 5V + 3.3V

        The important subtlety: MSI's "total" is the SUM of the per-rail
        figures, not the PSU's own measured total (cmd 224 P_Out). Because the
        rails are measured in parallel the sum runs higher than the measured
        total, so both are shown and labelled to avoid a false mismatch.
        """
        sec = Section(parent, t("output_breakdown"))
        sec.grid(row=0, column=0, sticky="nsew")
        b = sec.body
        self.bd = {}
        for key, label in (("total", t("bd_total")),
                           ("gpu", t("gpu_wattage")),
                           ("cpu", t("cpu_wattage")),
                           ("other", t("other_rails")),
                           ("measured", t("bd_measured"))):
            row = tk.Frame(b, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=F, bg=PANEL, fg=DIM,
                     width=12, anchor="w").pack(side="left")
            tk.Label(row, text="W", font=F_S, bg=PANEL, fg=FAINT).pack(
                side="right")
            colour = ACCENT if key == "total" else (
                OK if key == "measured" else FG)
            val = tk.Label(row, text="--", font=F_MONO_B, bg=PANEL, fg=colour,
                           anchor="e")
            val.pack(side="right", padx=(0, 3))
            self.bd[key] = val
        self.bd_bar = Meter(b, width=230, height=8)
        self.bd_bar.pack(fill="x", pady=(8, 2))
        tk.Label(b, text=t("estimated_note"), font=F_S, bg=PANEL,
                 fg=FAINT, wraplength=520, justify="left").pack(anchor="w")

    def _build_chart(self, parent):
        sec = Section(parent, t("consumption"))
        sec.grid(row=0, column=2, sticky="nsew")
        # width=1 keeps the canvas from demanding a wide minimum: its default
        # 394px asked for more than an equal third, which squeezed the other
        # two cards and made all three unequal. The grid cell still expands it.
        self.chart = tk.Canvas(sec.body, height=120, width=1, bg=PANEL,
                               highlightthickness=0)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda e: self._draw_chart())

    def _draw_spark(self):
        """Rolling output-power trace with a labelled value axis.

        Previously this had no axis at all and the trace hugged the top edge,
        because the y mapping used the full canvas height. It now reserves a
        left gutter for value labels and a top margin, and draws gridlines.
        """
        c = self.spark
        c.delete("all")
        data = self._history
        s = getattr(self, "dpi_scale", 1.0)
        w = c.winfo_width() or 400
        h = c.winfo_height() or 64
        if len(data) < 2:
            return

        gut = int(48 * s)          # left gutter, wide enough for "nnn W"
        pad_t = int(10 * s)        # keep the curve off the top edge
        # bottom margin from the label font's real height, so the time ticks
        # are never clipped (a fixed 20px was too small once Tk scaling grew it)
        try:
            from tkinter import font as tkfont
            line_h = tkfont.Font(root=self, font=("Microsoft YaHei UI", 7)
                                 ).metrics("linespace")
        except Exception:
            line_h = 14
        pad_b = line_h + int(10 * s)
        plot_w = max(1, w - gut - int(6 * s))
        plot_h = max(1, h - pad_t - pad_b)

        peak = max(max(data), 1.0) * 1.15
        # round the axis top to a friendly number so labels are readable
        step = 50 if peak > 200 else 20 if peak > 80 else 10 if peak > 30 else 5
        top = max(step, int((peak + step - 1) // step) * step)

        # Vertical axis: the unit rides on each number. A standalone "W" used
        # to be drawn above the top value and overlapped it.
        for frac in (0.0, 0.5, 1.0):
            val = top * frac
            y = pad_t + plot_h * (1.0 - frac)
            c.create_line(gut, y, w - int(6 * s), y, fill=GRID, dash=(2, 3))
            c.create_text(gut - int(5 * s), y, text=f"{val:.0f} W",
                          anchor="e", fill=FAINT,
                          font=("Microsoft YaHei UI", 7))

        # Horizontal axis: seconds elapsed across the visible window.
        # Anchors are all north-side (nw/n/ne) so every label hangs BELOW the
        # tick at one height. sw/se would push the outer two upward into the
        # plot, where they overlapped the trace and its gridline.
        span = max(1, len(data) - 1)
        tick_y = pad_t + plot_h
        for frac, anchor in ((0.0, "nw"), (0.5, "n"), (1.0, "ne")):
            x = gut + plot_w * frac
            secs = int(round(-(1.0 - frac) * span))
            c.create_line(x, tick_y, x, tick_y + int(3 * s), fill=GRID)
            c.create_text(x, tick_y + int(5 * s),
                          text=f"{secs}s", anchor=anchor, fill=FAINT,
                          font=("Microsoft YaHei UI", 7))

        n = len(data)
        step_x = plot_w / max(1, n - 1)
        pts = []
        for i, v in enumerate(data):
            x = gut + i * step_x
            y = pad_t + plot_h * (1.0 - max(0.0, min(1.0, v / top)))
            pts += [x, y]

        # filled area under the curve
        poly = [gut, pad_t + plot_h] + pts + [gut + plot_w, pad_t + plot_h]
        c.create_polygon(poly, fill="#3a1d24", outline="")
        c.create_line(pts, fill=ACCENT, width=2, smooth=True)
        # current value marker
        c.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3,
                      fill=ACCENT, outline="")

    def _draw_chart(self):
        c = self.chart
        c.delete("all")
        s = getattr(self, "dpi_scale", 1.0)
        w = c.winfo_width() or 360
        h = c.winfo_height() or 120
        data = self._profile.week()
        if not data:
            return

        # Reserve enough headroom for the value printed above the tallest bar;
        # too little padding was clipping those numbers.
        pad_l = int(6 * s)
        pad_b = int(22 * s)
        pad_t = int(24 * s)
        plot_h = max(1, h - pad_b - pad_t)
        peak = max([v for _, v in data] + [0.001])
        n = len(data)
        slot = (w - pad_l * 2) / n
        bar_w = min(int(28 * s), slot * 0.55)

        for i, (label, val) in enumerate(data):
            x = pad_l + slot * i + (slot - bar_w) / 2
            bh = plot_h * (val / peak) if peak > 0 else 0
            y0 = h - pad_b
            y1 = y0 - bh
            colour = ACCENT if i == n - 1 else "#4a4e5c"
            c.create_rectangle(x, y1, x + bar_w, y0, fill=colour, outline="")
            c.create_text(x + bar_w / 2, h - pad_b + int(10 * s), text=label,
                          fill=FAINT, font=("Microsoft YaHei UI", 7))
            if val > 0:
                # unit rides with the number, matching the other readouts
                c.create_text(x + bar_w / 2, y1 - int(8 * s),
                              text=f"{val:.2f} {t('kwh_unit')}", fill=DIM,
                              font=("Microsoft YaHei UI", 7))

    # ------------------------------------------------------------- worker

    def _worker(self, pid):
        try:
            psu = Psu(pid=pid)
            if not psu.connect():
                self._q.put(("error", "Handshake with the PSU failed."))
                return
            psu.read_info()
            self._psu = psu
            try:
                self._supported_modes = psu.supported_fan_modes()
            except Exception:
                pass
            self._q.put(("connected", psu.info.model))
        except PsuError as e:
            self._q.put(("error", str(e)))
            return
        except Exception as e:                          # noqa: BLE001
            self._q.put(("error", f"{type(e).__name__}: {e}"))
            return

        while not self._stop.is_set():
            t0 = time.time()
            try:
                if not self._writing:
                    t = psu.read_telemetry()
                    fan = psu.read_fan()
                    al = psu.read_alerts()
                    ocp = psu.read_ocp()
                    ports = None
                    if psu.pid in PCIE5_MODELS:
                        try:
                            # READ_ALL already carries the twelve per-pin
                            # currents on these models (TS layout)
                            if t is not None and t.pins_12v2x6:
                                pins = t.pins_12v2x6
                                ports = (pins[:6], pins[6:])
                            else:
                                ports = psu.read_12v2x6_currents()
                        except Exception:
                            ports = None
                    if t is not None:
                        # energy accounting needs both watts and efficiency,
                        # which the TS layout does not carry
                        if t.p_out > 0 and t.efficiency > 0:
                            self._profile.add_sample(
                                t.p_out, t.efficiency,
                                max(0.5, time.time() - self._last_sample))
                        self._last_sample = time.time()
                    if self._fail_count:
                        # recovered: re-announce so the header shows it again
                        self._fail_count = 0
                        self._q.put(("reconnected", None))
                    self._q.put(("data", {
                        "t": asdict(t) if t else None,
                        "fan": asdict(fan) if fan else None,
                        "fan_mode_name": fan.mode_name if fan else "",
                        "alerts_ok": al[0] if al else None,
                        "alerts": al[1] if al else [],
                        "ocp": ocp,
                        "ports": ports,
                    }))
            except Exception as e:                      # noqa: BLE001
                # Consecutive failures mean the device is gone (unplugged, or
                # MSI Center took it). Report it so the header stops claiming
                # a connection that is not there.
                self._fail_count += 1
                if self._fail_count == 1:
                    self._q.put(("error", f"Read failed: {e}"))
                elif self._fail_count == 3:
                    self._q.put(("disconnected", None))
                time.sleep(2)
            self._stop.wait(max(0.2, self._interval - (time.time() - t0)))

        try:
            self._profile.save()
            psu.close()
        except Exception:
            pass

    def _do_write(self, fn):
        def run():
            self._writing = True
            try:
                time.sleep(0.25)
                ok, msg = fn(self._psu)
                self._q.put(("write_result", (ok, msg)))
            except Exception as e:                      # noqa: BLE001
                self._q.put(("write_result", (False, f"{type(e).__name__}: {e}")))
            finally:
                self._writing = False
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------- UI

    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "data":
                    self._render(payload)
                elif kind == "connected":
                    self._connected = True
                    self.lbl_title.config(text=f"{t('psu')} - {payload}")
                    self._set_conn_status(True)
                    self._set_enabled(True)
                elif kind == "reconnected":
                    self._connected = True
                    self._set_conn_status(True)
                    self._set_enabled(True)
                elif kind == "disconnected":
                    # the reader lost the device: say so rather than leaving a
                    # stale "connected" on screen
                    self._connected = False
                    self.lbl_title.config(text=t("psu"))
                    self._set_conn_status(False)
                    self._set_enabled(False)
                elif kind == "error":
                    self._set_conn_status(False, detail=payload)
                    if not self._connected:
                        self.lbl_title.config(text=t("psu"))
                        messagebox.showerror("MSI PSU", payload, parent=self)
                elif kind == "write_result":
                    # transient feedback goes to the fan/rail rows, which have
                    # their own "applied" labels; it must not replace the
                    # connection state
                    ok, msg = payload
                    self.lbl_write_msg.config(text=msg, fg=OK if ok else BAD)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _set_conn_status(self, connected: bool, detail: str = ""):
        """Show the real device state; never claim connected without a device."""
        if connected:
            self.lbl_status.config(text=t("connected"), fg=OK)
        else:
            self.lbl_status.config(
                text=detail or t("disconnected"), fg=BAD)

    def _set_enabled(self, on: bool):
        st = "normal" if on else "disabled"
        # chips are plain Labels: no "disabled" state, so repaint them and
        # gate the click handlers via the cursor instead
        for w in (self.btn_rail_apply, self.btn_fan_apply):
            try:
                w.config(state="normal" if on else "disabled")
            except tk.TclError:
                pass
        self._sync_rail_mode_chips()
        self._sync_fan_mode_chips()
        self._sync_rail_buttons()
        self.sl_duty.config_state(on and self.var_fan.get() == "cust")
        for _l in getattr(self, "_rail_btns", {}).values():
            _l.config(cursor="hand2" if on else "arrow")

    def _toggle_unit(self):
        self._fahrenheit = not self._fahrenheit
        # only the symbol lives in the box; the wording beside it is static
        self.btn_unit.config(
            text=t("unit_f") if self._fahrenheit else t("unit_c"))
        self.st_temp.unit.config(text=t("unit_f") if self._fahrenheit
                                 else t("unit_c"))
        if self._last:
            self._show_temp(self._last["temp"])

    def _show_temp(self, celsius: float):
        if self._fahrenheit:
            val = celsius * 9.0 / 5.0 + 32.0
        else:
            val = celsius
        self.st_temp.set(f"{val:.0f}",
                         OK if celsius < 60 else (WARN if celsius < 75 else BAD))

    def _render(self, d):
        tel = d.get("t")
        if tel:
            self._last = tel
            self.st_out.set(f"{num(tel['p_out']):.0f}")
            eff = num(tel["efficiency"])
            self.st_eff.set(f"{eff:.1f}", OK if eff >= 85 else WARN)
            temp = num(tel["temp"])
            self._show_temp(temp)
            self.lbl_rpm.config(text=f"{num(tel['fan_speed']):.0f}")
            self.meter_rpm.set(num(tel["fan_speed"]) / 2500.0)
            self._history.append(num(tel["p_out"]))
            if len(self._history) > 60:
                del self._history[:-60]
            self._draw_spark()
            self._render_rails()
            # feed the dashboard, including the original's CPU/GPU estimates
            cpu_w = num(tel["v_12v2"]) * num(tel["i_12v2"])
            gpu_w = (num(tel["v_12v3"]) * num(tel["i_12v3"])
                     + num(tel["v_12v4"]) * num(tel["i_12v4"])
                     + num(tel["v_12v5"]) * num(tel["i_12v5"]))
            other_w = (num(tel["v_12v1"]) * num(tel["i_12v1"])
                       + num(tel["v_5v"]) * num(tel["i_5v"])
                       + num(tel["v_3v3"]) * num(tel["i_3v3"]))
            self.dashboard.feed({
                "cpu_w": cpu_w,
                "gpu_w": gpu_w,
                "total_w": num(tel["p_out"]),          # PSU's measured total
                "total_msi": cpu_w + gpu_w + other_w,  # MSI's summed figure
                "v12": num(tel["v_12v"]),
                "v5": num(tel["v_5v"]),
                "v3": num(tel["v_3v3"]),
                "temp": num(tel["temp"]),
                "fan": num(tel["fan_speed"]),
                "eff": num(tel["efficiency"]),
            })
            self._render_breakdown(tel)

        fan = d.get("fan")
        if fan and not self._writing:
            # keep the latest sample so _render_duty() can show the live value
            # without waiting for the next poll
            self._last_fan = fan
            self._mode = fan["mode"]
            self._custom_duty = fan["customize_duty"] or self._custom_duty
            # Only mirror the device while the user has no unapplied change;
            # otherwise the 1 Hz poll would yank their selection back.
            if not self._fan_dirty:
                if fan["mode"] == 3:
                    self.var_fan.set("cust")
                    self.sl_duty.set(self._custom_duty)
                else:
                    self.var_fan.set("perf")
            if self._zf_supported is None:
                # the device only carries this field on models other than the
                # Ai1000P / Ai1300P / Ai1600T
                from psu_protocol import PID_MEG_Ai1000P, PID_MEG_Ai1300P, PID_MEG_Ai1600T
                self._zf_supported = self._psu is not None and \
                    self._psu.pid not in (PID_MEG_Ai1000P, PID_MEG_Ai1300P,
                                          PID_MEG_Ai1600T)
            if self._zf_supported:
                # pack lazily: the widget is created but only shown on models
                # that actually carry the field
                if not self.lbl_zf.winfo_manager():
                    self.lbl_zf.pack(anchor="w", pady=(8, 0))
                self.lbl_zf.config(
                    text=f"{t('zero_fan')}: "
                         f"{t('on') if fan['zero_fan'] else t('off')}",
                    fg=DIM)
            else:
                # hide it entirely rather than announcing an absent capability
                if self.lbl_zf.winfo_manager():
                    self.lbl_zf.pack_forget()
            # The firmware reports a stopped fan as duty 1, and rewrites any
            # request in 2..24 to 1 as well, so "1%" means "off" rather than a
            # real 1% speed. Say so instead of printing a bare number.
            self._render_duty()
            # Recompute pending from the full desired-vs-device state. This
            # must compare mode *and* duty: clearing on mode alone made the
            # Apply button flash enabled then disabled while dragging the
            # slider in custom mode.
            self._recompute_fan_dirty()
            self._sync_fan_widgets()

        ocp = d.get("ocp")
        if ocp is not None:
            self._last_ocp = bool(ocp)
        if ocp is not None and not self._writing:
            if not self._rail_dirty:
                self.var_rail.set("multi" if ocp else "single")
                self._sync_rail_widgets()
            else:
                want = "multi" if ocp else "single"
                if want == self.var_rail.get():
                    self._rail_dirty = False
                    self._sync_rail_widgets()

        ok = d.get("alerts_ok")
        if ok is None:
            self.lbl_alerts.config(text="")
        elif ok:
            self.lbl_alerts.config(text=t("alerts_ok"), fg=OK)
        else:
            self.lbl_alerts.config(
                text=t("alert_prefix") + ", ".join(tr_alert(a) for a in d["alerts"]), fg=BAD)
            self.lbl_write_msg.config(text=t("alert_prefix"), fg=BAD)

        ports = d.get("ports")
        if ports:
            rail1, rail2 = ports
            self.lbl_ports.config(
                text=t("ports_12v2x6") + "  " +
                     " ".join(f"{c:.1f}" for c in rail1) + "  |  " +
                     " ".join(f"{c:.1f}" for c in rail2) + " A")
        else:
            self.lbl_ports.config(text="")

        self._render_stats()

    def _render_breakdown(self, tel):
        cpu = num(tel["v_12v2"]) * num(tel["i_12v2"])
        gpu = (num(tel["v_12v3"]) * num(tel["i_12v3"])
               + num(tel["v_12v4"]) * num(tel["i_12v4"])
               + num(tel["v_12v5"]) * num(tel["i_12v5"]))
        other = (num(tel["v_12v1"]) * num(tel["i_12v1"])
                 + num(tel["v_5v"]) * num(tel["i_5v"])
                 + num(tel["v_3v3"]) * num(tel["i_3v3"]))
        # MSI's "total" is the sum of the per-rail figures (its dashboard
        # graphs exactly this). The PSU also reports its own measured total,
        # which is lower because the rails are sampled in parallel.
        # Display each part rounded, then total the DISPLAYED values, so the
        # card always adds up. Totalling the raw floats and rounding afterwards
        # could differ by 0.1 and look like an arithmetic error.
        cpu_r = round(cpu, 1)
        gpu_r = round(gpu, 1)
        other_r = round(other, 1)
        msi_total = cpu_r + gpu_r + other_r
        measured = num(tel["p_out"])
        self.bd["total"].config(text=f"{msi_total:6.1f}")
        self.bd["gpu"].config(text=f"{gpu_r:6.1f}")
        self.bd["cpu"].config(text=f"{cpu_r:6.1f}")
        self.bd["other"].config(text=f"{other_r:6.1f}")
        self.bd["measured"].config(text=f"{measured:6.1f}")
        # the bar shows how much of the sum is CPU+GPU
        self.bd_bar.set(min(1.0, (cpu_r + gpu_r) / max(msi_total, 0.001)))

    def _render_rails(self):
        tel = self._last
        if not tel:
            return
        scale = max(12.0, num(tel["i_12v"]) * 1.2)
        for name, (vlab, alab, load) in self.rail_rows.items():
            key = name.lower()
            # A model whose READ_ALL layout carries no per-rail voltages (the
            # TS units) must show "--" rather than a fabricated 0.000 V.
            v = tel.get(f"v_{key}")
            a = tel.get(f"i_{key}")
            if v is None:
                vlab.config(text="   --  ", fg=FAINT)
            else:
                vlab.config(text=f"{v:7.3f}", fg=_colour_for(v, 12.0))
            if a is None:
                alab.config(text="   --  ")
                load.set(0.0)
            else:
                alab.config(text=f"{a:7.3f}")
                load.set(a / scale)

        which = self._rail_sel
        pair_for = {
            "12V": ("v_12v", "i_12v"),
            "5V": ("v_5v", "i_5v"),
            "3.3V": ("v_3v3", "i_3v3"),
        }[which]
        v = tel.get(pair_for[0])
        a = tel.get(pair_for[1])
        self.lbl_single_v.config(text=f"{v:.3f}" if v is not None else "--")
        self.lbl_single_a.config(text=f"{a:.3f}" if a is not None else "--")

    def _render_stats(self):
        p = self._profile
        if self._last:
            p.add_sample(0.0, 0.0, 0.0)  # no-op, keeps the API honest
        h = int(p.time_daily // 3600)
        m = int((p.time_daily % 3600) // 60)
        self.lbl_daily.config(text=f"{h} h {m} min")
        self.lbl_total.config(text=f"{p.time_total / 3600:.0f} h")
        self.lbl_kwh.config(text=f"{p.kwh_total:.2f} {t('kwh_unit')}")
        self.lbl_avge2.config(text=f"{p.avg_efficiency:.1f} %")
        if self._last:
            self.st_avgw.set(f"{p.avg_wattage:.1f}")
            self.st_avge.set(f"{p.avg_efficiency:.1f}")
        self._draw_chart()

    # ---------------------------------------------------------- commands

    def _sync_fan_widgets(self):
        custom = self.var_fan.get() == "cust"
        # The original hides the duty slider outright in auto mode and shows a
        # read-only bar instead. Leaving it on screen (even disabled) suggests
        # it can be dragged, which caused real confusion, so hide it.
        if custom:
            if not self.duty_box.winfo_manager():
                self.duty_box.pack(fill="x", pady=(6, 2),
                                   before=self.lbl_duty)
            self.sl_duty.config_state(self._connected)
        else:
            if self.duty_box.winfo_manager():
                self.duty_box.pack_forget()
            self.sl_duty.config_state(False)
        self.lbl_duty.config(fg=FG if custom else DIM)
        self.lbl_fan_tip.config(
            text=t("customized_tip") if custom else t("performance_tip"))
        self._sync_fan_mode_chips()
        # The original reveals Apply only once something is pending; mirror it.
        pending = self._fan_dirty
        self.btn_fan_apply.config(
            state="normal" if (self._connected and pending) else "disabled",
            bg=ACCENT if pending else "#4a4e5c")
        self.lbl_fan_applied.config(
            text=t("pending") if pending else "")

    def _make_chip(self, parent, text, on_click, first=False):
        """A clickable label used for every choice in this panel.

        Keeps the rail selector, the rail mode and the fan mode visually
        identical instead of mixing ttk widgets with hand-drawn ones.

        A chip carries padx=10 for a comfortable hit area, so the first one in
        a group is offset by -10: that puts its *text* on the column edge,
        lining it up with the headings and the rows below.
        """
        # Tk rejects negative padding, so the first chip simply has no left
        # padding and no outer gap: its text then starts on the column edge.
        lbl = tk.Label(parent, text=text, font=F, bg=PANEL, fg=DIM,
                       padx=10, pady=2, cursor="hand2")
        # A tk.Label takes a single padx, so the first chip simply uses 0 and
        # the rest use 10; pack's ipadx gives the first one a hit area without
        # pushing its text off the column edge.
        lbl.configure(padx=0 if first else 10)
        if first:
            lbl.pack(side="left", padx=(0, 0), ipadx=10)
        else:
            lbl.pack(side="left", padx=(6, 0))
        lbl.bind("<Button-1>", lambda e: on_click())
        return lbl

    def _paint_chip(self, lbl, active: bool, enabled: bool = True):
        """Active chips get a bright fill plus an accent underline bar."""
        if active:
            lbl.config(bg=CHIP_ON if enabled else TRACK,
                       fg=FG if enabled else DIM,
                       font=F_B if enabled else F)
        else:
            lbl.config(bg=CHIP_OFF, fg=DIM if enabled else FAINT, font=F)

    def _select_rail(self, name: str):
        """Switch the large readout to another rail (click, not a dropdown)."""
        self._rail_sel = name
        self._sync_rail_buttons()
        self._render_rails()

    def _sync_rail_buttons(self):
        for name, lbl in self._rail_btns.items():
            self._paint_chip(lbl, name == self._rail_sel, self._connected)

    def _sync_rail_mode_chips(self):
        want = self.var_rail.get()
        self._paint_chip(self.rb_multi, want == "multi", self._connected)
        self._paint_chip(self.rb_single, want == "single", self._connected)

    def _sync_fan_mode_chips(self):
        want = self.var_fan.get()
        self._paint_chip(self.rb_perf, want == "perf", self._connected)
        self._paint_chip(self.rb_cust, want == "cust", self._connected)

    def _set_rail_mode(self, value: str):
        self.var_rail.set(value)
        self._sync_rail_mode_chips()
        self._on_rail_mode()

    def _set_fan_mode(self, value: str):
        self.var_fan.set(value)
        self._sync_fan_mode_chips()
        self._on_fan_mode()

    def _sync_rail_widgets(self):
        self._sync_rail_mode_chips()
        multi = self.var_rail.get() == "multi"
        self.lbl_rail_hint.config(
            text=t("per_rail_ocp") if multi else t("combined_rail"))
        pending = self._rail_dirty
        self.btn_rail_apply.config(
            state="normal" if (self._connected and pending) else "disabled",
            bg=ACCENT if pending else "#4a4e5c")
        self.lbl_rail_applied.config(
            text=t("pending") if pending else "")

    def _on_rail_mode(self):
        """A click only marks the choice; Apply commits it (as the original)."""
        if self._rail_applied_value() != (self.var_rail.get() == "multi"):
            self._rail_dirty = True
        else:
            self._rail_dirty = False
        self._sync_rail_widgets()

    def _rail_applied_value(self) -> bool | None:
        return self._last_ocp

    def _on_rail_apply(self):
        want = self.var_rail.get() == "multi"

        def act(p):
            try:
                if not p.set_ocp(want):
                    return False, t("rail_rejected")
            except PsuError as e:
                return False, str(e)
            p.save_settings()
            return True, t("multi_rail") if want else t("single_rail")
        self._rail_dirty = False
        self._do_write(act)

    def _on_fan_mode(self):
        self._recompute_fan_dirty()
        self._sync_fan_widgets()

    def _desired_mode(self) -> int:
        return 3 if self.var_fan.get() == "cust" else 1

    def _fan_applied_mode(self) -> int | None:
        return self._mode

    def _recompute_fan_dirty(self):
        """Pending = the UI's requested fan state differs from the device's.

        Compares both the mode and, for custom mode, the duty. Comparing the
        mode alone cleared the flag as soon as the device already reported
        "custom", so the Apply button flickered enabled->disabled on every
        poll while the duty was still unapplied.
        """
        want_mode = self._desired_mode()
        if want_mode != self._mode:
            self._fan_dirty = True
            return
        if want_mode == 3:
            try:
                want_duty = int(float(self.sl_duty.get()))
            except (TypeError, ValueError):
                want_duty = self._custom_duty
            self._fan_dirty = want_duty != int(self._custom_duty)
        else:
            self._fan_dirty = False

    def _on_duty_move(self, _v=None):
        """Dragging the slider marks the setting as pending.

        Also updates the readout immediately: waiting for the next 1 Hz poll
        left the label showing the old value while the user was dragging.
        """
        if self.var_fan.get() != "cust":
            return
        self._recompute_fan_dirty()
        self._sync_fan_widgets()
        self._render_duty(pending=True)

    def _render_duty(self, pending: bool = False):
        """Show the live duty, or the value being dragged.

        The firmware encodes a stopped fan as duty 1 (and rewrites any request
        in 2..24 to 1), so a raw "1%" would be misleading — it reads "停转".
        """
        fan = self._last_fan
        actual = fan["actual_duty"] if fan else None
        if pending:
            want = int(float(self.sl_duty.get()))
            want_txt = t("stopped") if want <= 1 else f"{want}%"
            self.lbl_duty.config(
                text=f"{t('pending')} {want_txt}"
                     + (f"   {t('actual')} "
                        f"{t('stopped') if actual is not None and actual <= 1 else str(actual) + '%'}"
                        if actual is not None else ""))
            return
        if actual is None:
            return
        _d_txt = t("stopped") if actual <= 1 else f"{actual}%"
        self.lbl_duty.config(
            text=f"{t('actual')} {_d_txt}   {t('target')} "
                 f"{fan['calculated_duty']}%")

    def _on_fan_apply(self):
        if self.var_fan.get() == "cust":
            mode = 3
            duty = int(float(self.sl_duty.get()))
        else:
            mode = 1
            duty = 0

        def act(p):
            if not p.set_fan(mode, duty):
                return False, t("fan_rejected")
            if not p.save_settings():
                return False, t("save_failed")
            return True, (f"{t('customized')} {duty}%" if mode == 3
                          else t("performance"))
        self._fan_dirty = False
        self._do_write(act)

    def _on_reset_stats(self):
        if not messagebox.askyesno(t("app_title"), t("reset_confirm"), parent=self):
            return
        self._profile = EnergyProfile(path=self._profile.path)
        self._profile.save()
        self._render_stats()

    def _on_close(self):
        self._stop.set()
        # finalise a live recording so its CSV is flushed and closed
        try:
            self.dashboard.stop_and_close()
        except Exception:
            pass
        try:
            self._profile.save()
        except Exception:
            pass
        self.after(150, self.destroy)


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="msi-psu-gui")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=None)
    args = ap.parse_args()
    App(pid=args.pid).mainloop()


if __name__ == "__main__":
    main()
