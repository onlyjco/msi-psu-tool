"""Capture a GUI window to a BMP so we can verify it renders.

This tool must itself be DPI-aware: a DPI-unaware process asking for a
DPI-aware window's rect gets *virtualized* coordinates (scaled down by the
display factor), so it would silently capture a downscaled or clipped image
and misreport the window size.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# Declare per-monitor-v2 awareness BEFORE any coordinate is read.
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]

title = sys.argv[1] if len(sys.argv) > 1 else "MSI PSU"
out = sys.argv[2] if len(sys.argv) > 2 else "shot.bmp"

hwnd = user32.FindWindowW(None, title)
if not hwnd:
    print("window not found:", title)
    sys.exit(1)
user32.SetForegroundWindow(hwnd)
time.sleep(1.5)

rect = wt.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
w, h = rect.right - rect.left, rect.bottom - rect.top
hdc = user32.GetWindowDC(hwnd)
mem = gdi32.CreateCompatibleDC(hdc)
bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
gdi32.SelectObject(mem, bmp)
ok = user32.PrintWindow(hwnd, mem, 2)


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


bi = BIH()
bi.biSize = ctypes.sizeof(BIH)
bi.biWidth, bi.biHeight = w, -h
bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
buf = ctypes.create_string_buffer(w * h * 4)
lines = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)

hdr = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(buf), 0, 0, 14 + 40)
info = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(buf), 0, 0, 0, 0)
with open(out, "wb") as f:
    f.write(hdr + info + buf.raw)

gdi32.DeleteObject(bmp)
gdi32.DeleteDC(mem)
user32.ReleaseDC(hwnd, hdc)
print(f"captured {w}x{h} printwindow={ok} lines={lines} -> {out}")
