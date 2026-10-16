"""Does Performance mode respond to temperature?

The earlier run had the PSU idling at ~33 C where its fan legitimately stops,
so nothing changed. This drives real load (GPU/CPU work) and watches whether
Performance raises the duty on its own, while logging what the device reports.
"""
import io
import subprocess
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from psu_protocol import CMD, Psu, decode_value

stop = threading.Event()


def load_gen():
    """Busy-loop on all cores to raise system power draw."""
    end = time.time() + 70
    while time.time() < end and not stop.is_set():
        sum(i * i for i in range(20000))


def snap(p):
    f = p.raw(CMD.READ, CMD.READ_ALL)
    fan = p.read_fan()
    return (decode_value(f[41], f[42]) if f else None,
            decode_value(f[39], f[40]) if f else None,
            decode_value(f[35], f[36]) if f else None,
            fan.actual_duty, fan.calculated_duty, fan.mode)


with Psu() as p:
    print(f"device: {p.info.model}")
    print()
    print("=" * 78)
    print("PERFORMANCE MODE under rising load (fan should speed up if automatic)")
    print("=" * 78)

    p.set_fan(1)
    p.save_settings()
    time.sleep(2)
    rpm, temp, pout, actual, calc, mode = snap(p)
    print(f"  baseline (idle): mode={mode} duty={actual}% rpm={rpm:.0f} "
          f"temp={temp:.0f} P={pout:.0f}W")
    print()

    threads = [threading.Thread(target=load_gen, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()

    print(f"  {'t':>4} {'P_out':>7} {'temp':>5} {'duty':>5} {'calc':>5} {'RPM':>7}")
    t0 = time.time()
    while time.time() - t0 < 75:
        time.sleep(5)
        rpm, temp, pout, actual, calc, mode = snap(p)
        print(f"  {int(time.time()-t0):3d}s {pout:7.0f} {temp:5.0f} "
              f"{actual:4d}% {calc:5d} {rpm:7.0f}")

    stop.set()
    print()
    print("  -> duty changing with temperature means Performance is automatic")
    print("  -> duty pinned at 1% means it does not react on this model")

    # restore
    p.set_fan(1)
    p.save_settings()
    print()
    print("  restored to Performance (silent) mode")
