"""Trace what mode 1 actually does after a manual speed was set.

The user reports: set duty above 30%, switch back to Performance, the speed
holds for a while, then falls to zero. This run captures the whole sequence
with fine sampling so the transition is visible instead of inferred.

Also answers directly: can the fan hold ~10%?
Earlier scans showed the device rewrites the requested duty:
    0        -> 30
    2..24    -> 1
    26..100  -> accepted as-is
so 10% may be unreachable. This checks it rather than assuming.
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
        "pout": decode_value(f[35], f[36]) if f else -1,
        "mode": fan.mode,
        "duty": fan.actual_duty,
        "custom": fan.customize_duty,
    }


def line(tag, s):
    print(f"  {tag:>4}  mode={s['mode']} duty={s['duty']:3d}% "
          f"custom={s['custom']:3d} RPM={s['rpm']:7.0f} "
          f"temp={s['temp']:.0f}C P={s['pout']:.0f}W", flush=True)


with Psu() as p:
    print(f"device: {p.info.model}")
    print()

    print("=" * 74)
    print("1. CURRENT STATE (read only)")
    print("=" * 74)
    s = snap(p)
    line("now", s)

    print()
    print("=" * 74)
    print("2. IS 10% REACHABLE?  ask for 10, see what the device stores")
    print("=" * 74)
    for want in (10, 20, 25, 26):
        p.set_fan(3, want)
        time.sleep(4)
        s = snap(p)
        stored = s["duty"]
        verdict = "accepted" if stored == want else f"REWRITTEN to {stored}%"
        print(f"  asked {want:3d}% -> device stored {stored:3d}%  "
              f"RPM={s['rpm']:.0f}   [{verdict}]", flush=True)

    print()
    print("=" * 74)
    print("3. SET 50%, then switch to PERFORMANCE and watch it decay")
    print("=" * 74)
    p.set_fan(3, 50)
    time.sleep(6)
    line("50%", snap(p))

    print()
    print("  switching to mode 1 now, sampling every 2s for 90s:")
    p.set_fan(1)
    t0 = time.time()
    prev = None
    while time.time() - t0 < 90:
        s = snap(p)
        mark = ""
        if prev is not None and s["duty"] != prev:
            mark = f"   <-- duty {prev}% -> {s['duty']}%"
        print(f"  +{int(time.time()-t0):3d}s  mode={s['mode']} duty={s['duty']:3d}% "
              f"RPM={s['rpm']:7.0f} temp={s['temp']:.0f}C{mark}", flush=True)
        prev = s["duty"]
        time.sleep(2)

    print()
    print("  final:", end=" ")
    line("end", snap(p))
