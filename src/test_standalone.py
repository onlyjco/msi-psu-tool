"""Rigorous standalone test.

Three real risks for running without MSI Center installed:
  A. the named mutex may not exist and cannot be created by a normal user
  B. the hidapi native DLL might come from somewhere MSI installed
  C. the HID driver might have been installed by MSI Center

(A) is tested here by forcing the exact "no handle" state.
(B) and (C) are checked on disk / in the driver store.
"""
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psu_protocol
from psu_protocol import Psu

print("=== A. force the real 'no mutex available' state ===")


class DeadMutex:
    """Exactly what _Mutex looks like when Create and Open both fail."""

    def __init__(self, *a, **k):
        self._handle = None
        self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def close(self):
        pass


orig = psu_protocol._Mutex
psu_protocol._Mutex = DeadMutex
try:
    with Psu() as p:
        t = p.read_telemetry()
        fan = p.read_fan()
        print(f"  model     : {p.info.model}")
        print(f"  12V       : {t.v_12v:.3f} V   P_Out {t.p_out:.1f} W   "
              f"Eff {t.efficiency:.2f} %")
        print(f"  fan mode  : {fan.mode} ({fan.mode_name})")
        al = p.read_alerts()
        print(f"  alerts    : {'all OK' if al and al[0] else al}")
    print("  RESULT    : fully operational with NO mutex")
finally:
    psu_protocol._Mutex = orig

print()
print("=== B. where does the hidapi native DLL come from? ===")
try:
    import hid
    pkg_dir = os.path.dirname(hid.__file__)
    print(f"  python package : {pkg_dir}")
    natives = glob.glob(os.path.join(pkg_dir, "*.dll")) + \
              glob.glob(os.path.join(pkg_dir, "**", "*.dll"), recursive=True)
    if natives:
        for n in sorted(set(natives)):
            print(f"  native         : {n}  ({os.path.getsize(n):,} bytes)")
    else:
        print("  native         : (none inside package -> system hidapi?)")
except Exception as e:
    print(f"  error: {e}")

print()
print("=== C. which driver serves the PSU HID device? ===")
import subprocess
for key in ("DEVPKEY_Device_Service", "DEVPKEY_Device_DriverInfPath",
            "DEVPKEY_Device_DriverProvider"):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-PnpDeviceProperty -InstanceId 'HID\\VID_0DB0&PID_56D4\\A&1CFD035F&0&0000' "
         f"-KeyName {key} -ErrorAction SilentlyContinue).Data"],
        capture_output=True, text=True)
    print(f"  {key:32s}: {r.stdout.strip()!r}")
