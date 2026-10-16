"""Is the PSU's fan autonomous? Put real heat into it and watch.

The user's point is that a PSU works fine with no software installed, which
implies it must manage its own fan. This drives a genuine load (GPU burn via
the display adapter is not available headlessly, so this saturates the CPU with
many threads) and observes whether duty/RPM move WITHOUT any write from us.

Note honestly: if the temperature never rises meaningfully, the test proves
nothing either way, and it says so.
"""
import io
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from psu_protocol import CMD, Psu, decode_value

stop = threading.Event()


def burn():
    x = 0
    while not stop.is_set():
        for i in range(50000):
            x += i * i
        x = 0


with Psu() as p:
    f = p.raw(CMD.READ, CMD.READ_ALL)
    fan0 = p.read_fan()
    print(f"device: {p.info.model}")
    print(f"before load: mode={fan0.mode} duty={fan0.actual_duty}% "
          f"rpm={decode_value(f[41], f[42]):.0f} "
          f"temp={decode_value(f[39], f[40]):.0f}C "
          f"P={decode_value(f[35], f[36]):.0f}W")
    print()
    print("Starting 16 CPU-burn threads. NOT writing to the fan at all.")
    print()

    threads = [threading.Thread(target=burn, daemon=True) for _ in range(16)]
    for t in threads:
        t.start()

    print(f"  {'t':>4} {'P_out':>7} {'temp':>5} {'duty':>5} {'RPM':>8}  change?")
    t0 = time.time()
    prev = fan0.actual_duty
    peak_temp = 0
    duty_moved = False
    rows = []
    while time.time() - t0 < 150:
        time.sleep(6)
        f = p.raw(CMD.READ, CMD.READ_ALL)
        fan = p.read_fan()
        pout = decode_value(f[35], f[36])
        temp = decode_value(f[39], f[40])
        rpm = decode_value(f[41], f[42])
        peak_temp = max(peak_temp, temp)
        ch = ""
        if fan.actual_duty != prev:
            ch = f"duty {prev}% -> {fan.actual_duty}%"
            duty_moved = True
            prev = fan.actual_duty
        rows.append((pout, temp, fan.actual_duty, rpm))
        print(f"  {int(time.time()-t0):3d}s {pout:7.0f} {temp:5.0f} "
              f"{fan.actual_duty:4d}% {rpm:8.0f}  {ch}")

    stop.set()
    time.sleep(1)

    print()
    print("=" * 72)
    print("RESULT")
    print("=" * 72)
    temps = [r[1] for r in rows]
    print(f"  peak temperature   : {peak_temp:.0f} C")
    print(f"  temperature rise   : {max(temps) - min(temps):.0f} C")
    print(f"  duty moved by itself: {duty_moved}")
    if max(temps) - min(temps) < 3:
        print()
        print("  INCONCLUSIVE: the temperature barely moved, so this run")
        print("  cannot show whether the fan is autonomous. It needs a load")
        print("  that actually heats the PSU (e.g. a GPU-heavy workload).")
    else:
        print()
        print(f"  CONCLUSION: duty {'is' if duty_moved else 'is NOT'} "
              f"autonomous under load.")
