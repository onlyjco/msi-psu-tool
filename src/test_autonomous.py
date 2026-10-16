"""Does the PSU control its own fan, or does software drive it?

Key experiment: leave the device completely untouched (no writes at all) and
watch whether duty/RPM change as the load rises. If the PSU is autonomous, the
duty must move on its own. If software drives it, it stays wherever it was
last told.
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


def burn(seconds):
    end = time.time() + seconds
    while time.time() < end and not stop.is_set():
        sum(i * i for i in range(30000))


with Psu() as p:
    print(f"device: {p.info.model}")
    print()
    print("=" * 78)
    print("PASSIVE OBSERVATION — we only READ, never write")
    print("=" * 78)
    f = p.raw(CMD.READ, CMD.READ_ALL)
    fan = p.read_fan()
    print(f"  starting state: mode={fan.mode} duty={fan.actual_duty}% "
          f"rpm={decode_value(f[41], f[42]):.0f} temp={decode_value(f[39], f[40]):.0f}C")
    print()

    # raise the load without touching the fan
    threads = [threading.Thread(target=burn, args=(120,), daemon=True)
               for _ in range(16)]
    for t in threads:
        t.start()

    print(f"  {'t':>4} {'P_out':>7} {'temp':>5} {'duty':>5} {'RPM':>8}   note")
    t0 = time.time()
    prev = None
    while time.time() - t0 < 115:
        time.sleep(5)
        f = p.raw(CMD.READ, CMD.READ_ALL)
        fan = p.read_fan()
        pout = decode_value(f[35], f[36])
        temp = decode_value(f[39], f[40])
        rpm = decode_value(f[41], f[42])
        note = ""
        if prev is not None and fan.actual_duty != prev:
            note = f"duty changed {prev}% -> {fan.actual_duty}%"
        print(f"  {int(time.time()-t0):3d}s {pout:7.0f} {temp:5.0f} "
              f"{fan.actual_duty:4d}% {rpm:8.0f}   {note}")
        prev = fan.actual_duty

    stop.set()
    print()
    print("  -> duty moving on its own  = the PSU controls the fan itself")
    print("  -> duty frozen at 1%       = software was driving it")
