"""Windows DPI awareness, applied before any Tk window exists.

Without this the process is DPI-*unaware*: on a 150% display Windows renders
the window at 96 DPI and then bitmap-stretches the finished pixels up to the
panel's real resolution, which is exactly what makes text look soft/blurry.

Declaring per-monitor-v2 awareness makes the app render at native DPI, so
fonts are drawn at the true pixel size and stay sharp.

Measured on the target machine before the fix:
    real display DPI        144  (150% scaling)
    DPI seen by the process  96
    tk scaling               1.333   -> everything stretched 1.5x

After: the process sees 144 DPI and tk scaling is 2.0 (144/72), which is the
correct points-to-pixels ratio.
"""

from __future__ import annotations

import ctypes
import sys

_applied = False


def apply() -> float:
    """Declare DPI awareness and return the resulting scale factor (1.0, 1.5…).

    Safe to call repeatedly and on non-Windows platforms; returns 1.0 when
    awareness could not be established.
    """
    global _applied
    if sys.platform != "win32":
        return 1.0
    if not _applied:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Win10 1703+).
        # Fall back to the older shcore/system APIs on earlier builds.
        try:
            if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                raise OSError
        except Exception:
            try:
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)   # per-monitor
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    return 1.0
        _applied = True
    return scale()


def scale() -> float:
    """Current DPI divided by 96, clamped to a sane range."""
    if sys.platform != "win32":
        return 1.0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dpi = user32.GetDpiForSystem()
        if not dpi:
            raise OSError
    except Exception:
        try:
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            hdc = user32.GetDC(0)
            dpi = gdi32.GetDeviceCaps(hdc, 88)      # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
        except Exception:
            return 1.0
    return max(1.0, min(4.0, dpi / 96.0))


def setup_tk(root, base_w: int, base_h: int) -> float:
    """Apply scaling to an existing Tk root and size its window.

    Tk sizes fonts in *points*, so once the process is DPI-aware a 9pt font
    becomes 18px at 144 DPI — physically identical to before, but rendered
    sharply instead of being stretched.
    """
    s = scale()
    try:
        root.tk.call("tk", "scaling", (96.0 * s) / 72.0)
    except Exception:
        pass
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w = min(int(base_w * s), int(sw * 0.96))
    h = min(int(base_h * s), int(sh * 0.96))
    root.geometry(f"{w}x{h}")
    root.minsize(int(900 * s), int(600 * s))
    return s
