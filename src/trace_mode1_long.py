"""Two questions.

A. What is the LOWEST duty this unit will actually hold?
   The user wants ~10%. Earlier scans suggested the device rewrites anything
   from 2..24 to 1 (which stops the fan), so this pins the exact boundary.

B. Does mode 1 eventually fall to zero after a manual speed?
   The user reports it holds "for a while" then stops. A 90s window was too
   short to catch it, so this watches for ~8 minutes.

Writes are limited to what these two questions need, and the fan is left in
mode 1 (the unit's own default, per MSI's logs).
"""
import io
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from psu_protocol import CMD, Psu, decode_value


def snap(p):
    f = p.raw(CMD.READ, CMD.READ_ALL)
    fan = p.read_fan()
    return {
        "temp": decode_value(f[39], f[40]) if f else -1,
        "rpm": decode_value(f[41], f[42]) if f else -1,
        "mode": fan.mode,
        "duty": fan.actual_duty,
    }


with Psu() as p:
    print(f"device: {p.info.model}", flush=True)

    print()
    print("=" * 70, flush=True)
    print("A. exact lowest sustainable duty", flush=True)
    print("=" * 70, flush=True)
    lowest = None
    for want in (24, 25, 26):
        p.set_fan(3, want)
        time.sleep(5)
        s = snap(p)
        ok = s["duty"] == want
        if ok and lowest is None:
            lowest = want
        print(f"  asked {want:3d}% -> stored {s['duty']:3d}%  RPM={s['rpm']:7.0f}  "
              f"{'OK' if ok else 'rewritten'}", flush=True)
    print(f"\n  => lowest sustainable duty = {lowest}%", flush=True)

    print()
    print("=" * 70, flush=True)
    print("B. set 50%, switch to mode 1, then watch 8 minutes", flush=True)
    print("=" * 70, flush=True)
    p.set_fan(3, 50)
    time.sleep(6)
    s = snap(p)
    print(f"  custom 50%: duty={s['duty']}% RPM={s['rpm']:.0f}", flush=True)

    p.set_fan(1)
    t0 = time.time()
    prev = None
    while time.time() - t0 < 480:
        s = snap(p)
        mark = ""
        if prev is not None and s["duty"] != prev:
            mark = f"   <== CHANGED {prev}% -> {s['duty']}%"
        print(f"  +{int(time.time()-t0):3d}s  mode={s['mode']} duty={s['duty']:3d}% "
              f"RPM={s['rpm']:7.0f} temp={s['temp']:.0f}C{mark}", flush=True)
        prev = s["duty"]
        time.sleep(15)

    s = snap(p)
    print(f"\n  final: mode={s['mode']} duty={s['duty']}% RPM={s['rpm']:.0f} "
          f"temp={s['temp']:.0f}C", flush=True)
    print("  (left in mode 1, the unit's own default)", flush=True)
