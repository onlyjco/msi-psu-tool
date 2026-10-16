"""Characterise Performance (mode 1) vs Customized (mode 3).

Questions to answer with data, not assumption:
  1. Does Performance ignore a supplied duty entirely?
  2. Does Performance track temperature / load on its own?
  3. Does Customized hold a fixed duty regardless of temperature?
  4. What does the device report for calculated_duty in each mode?
"""
import io
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from psu_protocol import CMD, Psu, decode_value


def snapshot(p):
    f = p.raw(CMD.READ, CMD.READ_ALL)
    fan = p.read_fan()
    return {
        "rpm": decode_value(f[41], f[42]) if f else None,
        "temp": decode_value(f[39], f[40]) if f else None,
        "pout": decode_value(f[35], f[36]) if f else None,
        "mode": fan.mode,
        "actual": fan.actual_duty,
        "calc": fan.calculated_duty,
        "set": fan.set_duty,
    }


with Psu() as p:
    print(f"device: {p.info.model}")
    print()

    print("=" * 74)
    print("1. PERFORMANCE (mode 1) with a deliberately weird duty supplied")
    print("=" * 74)
    print(f"  {'supplied':>9} {'mode':>5} {'actual':>7} {'calc':>5} {'RPM':>7} {'temp':>5}")
    for duty in (0, 50, 100):
        p.set_fan(1, duty)
        time.sleep(6)
        s = snapshot(p)
        print(f"  {duty:9d} {s['mode']:5d} {s['actual']:6d}% {s['calc']:5d} "
              f"{s['rpm']:7.0f} {s['temp']:5.0f}")
    print()
    print("  -> if 'actual' stays the same for all three, Performance ignores duty")

    print()
    print("=" * 74)
    print("2. PERFORMANCE over time — does it drift with temperature?")
    print("=" * 74)
    p.set_fan(1)
    p.save_settings()
    print(f"  {'t':>4} {'RPM':>8} {'actual':>7} {'calc':>5} {'temp':>5} {'P_out':>7}")
    for i in range(10):
        time.sleep(3)
        s = snapshot(p)
        print(f"  {i*3:3d}s {s['rpm']:8.0f} {s['actual']:6d}% {s['calc']:5d} "
              f"{s['temp']:5.0f} {s['pout']:7.0f}")

    print()
    print("=" * 74)
    print("3. CUSTOMIZED (mode 3) — does it hold the duty regardless of temp?")
    print("=" * 74)
    for duty in (40, 60, 80):
        p.set_fan(3, duty)
        time.sleep(6)
        s = snapshot(p)
        print(f"  asked {duty:3d}% -> mode={s['mode']} actual={s['actual']:3d}% "
              f"calc={s['calc']:3d} RPM={s['rpm']:7.0f} temp={s['temp']:.0f}")

    print()
    print("=" * 74)
    print("4. calculated_duty: what is it for?")
    print("=" * 74)
    p.set_fan(1)
    time.sleep(5)
    s = snapshot(p)
    print(f"  Performance: actual={s['actual']}%  calc={s['calc']}%  "
          f"set={s['set']}%")
    p.set_fan(3, 55)
    time.sleep(5)
    s = snapshot(p)
    print(f"  Customized : actual={s['actual']}%  calc={s['calc']}%  "
          f"set={s['set']}%")

    # restore
    p.set_fan(1)
    p.save_settings()
    print()
    print("  restored to Performance (silent) mode")
