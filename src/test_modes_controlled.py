"""Controlled comparison of Performance (1) vs Customized (3).

Earlier runs gave contradictory duty readings because the duty reported back
depends on what was set previously and on the 1 Hz device refresh. This run
sets a mode, waits long enough for the device to settle, and reports the
steady-state values several times so a transient cannot be mistaken for
behaviour.
"""
import io
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from psu_protocol import CMD, Psu, decode_value


def read(p):
    f = p.raw(CMD.READ, CMD.READ_ALL)
    fan = p.read_fan()
    return {
        "rpm": decode_value(f[41], f[42]) if f else -1,
        "temp": decode_value(f[39], f[40]) if f else -1,
        "pout": decode_value(f[35], f[36]) if f else -1,
        "mode": fan.mode,
        "actual": fan.actual_duty,
        "calc": fan.calculated_duty,
    }


def settle(p, label, secs=12, samples=4):
    print(f"  {label}")
    print(f"    {'t':>4} {'mode':>5} {'actual':>7} {'calc':>5} {'RPM':>7} {'temp':>5}")
    for i in range(samples):
        time.sleep(secs / samples)
        r = read(p)
        print(f"    {int((i+1)*secs/samples):3d}s {r['mode']:5d} "
              f"{r['actual']:6d}% {r['calc']:5d} {r['rpm']:7.0f} {r['temp']:5.0f}")
    return read(p)


with Psu() as p:
    print(f"device: {p.info.model}")
    print()
    print("=" * 76)
    print("A. PERFORMANCE (mode 1), duty field set to several values")
    print("=" * 76)
    for duty in (0, 30, 70, 100):
        p.set_fan(1, duty)
        r = settle(p, f"set_fan(1, duty={duty})")
        print(f"    => steady: actual={r['actual']}%  rpm={r['rpm']:.0f}")
        print()

    print("=" * 76)
    print("B. CUSTOMIZED (mode 3), duty set to the same values")
    print("=" * 76)
    for duty in (30, 70, 100):
        p.set_fan(3, duty)
        r = settle(p, f"set_fan(3, duty={duty})")
        print(f"    => steady: actual={r['actual']}%  rpm={r['rpm']:.0f}")
        print()

    p.set_fan(1)
    p.save_settings()
    print("  restored to Performance (silent) mode")
