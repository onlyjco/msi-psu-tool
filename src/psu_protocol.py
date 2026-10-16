"""MSI PSU HID protocol library.

Protocol recovered by static analysis of the shipped module
``API_Power Supply Unit.dll`` (MSI Center "Power Supply Unit" component),
verified against a MEG Ai1300P PCIE5 (VID 0x0DB0 / PID 0x56D4).

Wire format (65-byte HID report, report ID 0):

    byte 0     report ID (0)
    byte 1     opcode: 0x51 (81) read, 0x50 (80) write, 0xFA (250) handshake
    byte 2     command
    byte 3..   payload

The device echoes opcode and command. Replies arrive on the input report and
must be matched by that echo; the device also emits unsolicited reports, so
stale frames are discarded rather than interpreted.

Multi-byte values use an 11-bit mantissa / 5-bit signed exponent encoding
(Linear11). The exponent is computed exactly as the original does:

    exponent = ((high << 24) as signed int32) >> 27
    mantissa = ((high & 0x07) << 8) | low
    value    = mantissa * 2**exponent

The signed-32-bit step matters: for ``high >= 0x80`` the shift overflows to a
negative exponent. Reimplementations that use unsigned arithmetic decode
nonsense (e.g. 11.97 V becomes ~5e8).
"""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Iterable

import hid

# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

VID_MSI = 0x0DB0

PID_MEG_Ai1000P = 0xE749   # 59209
PID_MEG_Ai1300P = 0x56D4   # 22228  <- MEG Ai1300P PCIE5
PID_MEG_Ai1600T = 0xC9EB   # 51691
PID_MPG_Ai1300TS = 0xAA6F  # 43631
PID_MPG_Ai1600TS = 0x808C  # 32908

PSU_MODELS = {
    PID_MEG_Ai1000P: "MEG Ai1000P",
    PID_MEG_Ai1300P: "MEG Ai1300P PCIE5",
    PID_MEG_Ai1600T: "MEG Ai1600T",
    PID_MPG_Ai1300TS: "MPG Ai1300TS",
    PID_MPG_Ai1600TS: "MPG Ai1600TS",
}

# Models that expose 12V-2x6 per-pin telemetry and its OCP configuration.
PCIE5_MODELS = (PID_MPG_Ai1300TS, PID_MPG_Ai1600TS)
# Legacy models whose multi-rail OCP is not configurable.
NO_MULTIRAIL_OCP = (PID_MEG_Ai1600T, PID_MPG_Ai1300TS, PID_MPG_Ai1600TS)

USAGE_PAGE = 0xFF00
USAGE = 0x0001

REPORT_LEN = 65


class CMD:
    HANDSHAKE = 250
    READ = 81
    WRITE = 80

    MFR_ID = 16
    MFR_MODEL = 17
    MFR_REVISION = 18
    MFR_SERIAL = 19

    VIN = 32
    VOUT_12V5 = 34
    IOUT_12V5 = 35
    VOUT_12 = 36
    IOUT_12 = 37
    VOUT_5 = 38
    IOUT_5 = 39
    VOUT_3_3 = 40
    IOUT_3_3 = 41
    POUT = 42
    EFFICIENCY = 43
    IOUT_12V2x6 = 44
    TEMPERATURE = 48

    FAN_SPEED = 64
    FAN_SETTING = 65
    READ_FANDUTY = 66
    ZERO_FAN_ENABLE = 67

    MULTIRAIL_OCP = 192
    OCP12V2x6_CONFIG = 192
    OCP12V2x6_STATE = 193
    BUZZER_SWITCH = 194

    RUNTIME_NOW = 208
    RUNTIME_TOTAL = 209

    READ_ALL = 224
    READ_ALERTSTATUS = 225
    SAVE_SETTING = 241


ALERT_NAMES = [
    "OVP_12V", "OVP_5V", "OVP_3_3V",
    "UVP_12V", "UVP_5V", "UVP_3_3V",
    "OCP_12V", "OCP_5V", "OCP_3_3V",
    "OCP_12V1", "OCP_12V2", "OCP_12V3", "OCP_12V4", "OCP_12V5",
    "OPP", "OTP", "Fan", "AC",
    "OCP_12V2x6_1_1", "OCP_12V2x6_1_2", "OCP_12V2x6_1_3",
    "OCP_12V2x6_1_4", "OCP_12V2x6_1_5", "OCP_12V2x6_1_6",
    "OCP_12V2x6_2_1", "OCP_12V2x6_2_2", "OCP_12V2x6_2_3",
    "OCP_12V2x6_2_4", "OCP_12V2x6_2_5", "OCP_12V2x6_2_6",
]

# Which alert slots a given model actually reports.
ALERT_INDEX_P = list(range(18))                       # Ai1000P / Ai1300P
ALERT_INDEX_T = [0,1,2,3,4,5,6,7,8,14,15,16,17]       # Ai1600T
ALERT_INDEX_TS = [6,7,8,18,19,20,21,22,23,24,
                  25,26,27,28,29,14,15,16]            # TS models


def alert_index_for(pid: int) -> list[int]:
    if pid in (PID_MEG_Ai1000P, PID_MEG_Ai1300P):
        return ALERT_INDEX_P
    if pid == PID_MEG_Ai1600T:
        return ALERT_INDEX_T
    if pid in PCIE5_MODELS:
        return ALERT_INDEX_TS
    return ALERT_INDEX_P



def num(v, default: float = 0.0) -> float:
    """Coerce a possibly-missing telemetry field to a number.

    Fields are None when the current model's READ_ALL layout does not carry
    them, so arithmetic needs an explicit default rather than a bare value.
    """
    return default if v is None else float(v)

def decode_value(low: int, high: int) -> float:
    """Decode the 11-bit mantissa / signed-5-bit exponent encoding."""
    raw = (high << 24) & 0xFFFFFFFF
    if raw & 0x80000000:            # emulate C# int32 overflow
        raw -= 0x100000000
    exponent = raw >> 27            # Python >> is arithmetic (floor)
    mantissa = ((high & 0x07) << 8) | low
    return mantissa * (2.0 ** exponent)


# --------------------------------------------------------------------------
# Cross-process mutex (shared with MSI Center)
# --------------------------------------------------------------------------

_MUTEX_NAME = "Global\\MSI_PSU_Mutex"


class _Mutex:
    """Best-effort share of MSI Center's ``Global\\MSI_PSU_Mutex``.

    MSI Center creates this mutex from its SYSTEM service, so the object lives
    in the global namespace. A standard user process cannot *create* a global
    object (that needs SeCreateGlobalPrivilege) but can normally *open* one
    that already exists, asking only for synchronisation rights.

    When neither is possible the lock degrades to a no-op: the protocol is
    self-synchronising because every reply echoes the request's opcode and
    command, so a frame belonging to another writer is discarded rather than
    misread. ``held`` records whether the real lock was obtained.
    """

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_ABANDONED = 0x00000080
    WAIT_TIMEOUT = 0x00000102

    def __init__(self, name: str = _MUTEX_NAME, timeout_ms: int = 3000):
        self.held = False
        self._timeout_ms = timeout_ms
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                           wintypes.LPCWSTR]
        self._k32.CreateMutexW.restype = wintypes.HANDLE
        self._k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.LPCWSTR]
        self._k32.OpenMutexW.restype = wintypes.HANDLE
        self._k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._k32.WaitForSingleObject.restype = wintypes.DWORD
        self._k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]

        self._handle = self._k32.CreateMutexW(None, False, name)
        if not self._handle:
            # Fall back to opening the existing global object.
            self._handle = self._k32.OpenMutexW(self.SYNCHRONIZE, False, name)

    def __enter__(self):
        if not self._handle:
            return self
        rc = self._k32.WaitForSingleObject(self._handle, self._timeout_ms)
        if rc == self.WAIT_TIMEOUT:
            # Never block the caller forever on MSI Center's poller.
            return self
        self.held = rc in (self.WAIT_OBJECT_0, self.WAIT_ABANDONED)
        return self

    def __exit__(self, *exc):
        if self.held:
            self._k32.ReleaseMutex(self._handle)
            self.held = False
        return False

    def close(self):
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


# --------------------------------------------------------------------------
# Data containers
# --------------------------------------------------------------------------

@dataclass
class Telemetry:
    """One decoded READ_ALL sample.

    Fields are None when the model's layout does not carry them, so a caller
    can tell "this rail is not reported" from "this rail reads zero". The TS
    models, for instance, report no voltages at all.
    """

    v_12v1: float | None = None
    i_12v1: float | None = None
    v_12v2: float | None = None
    i_12v2: float | None = None
    v_12v3: float | None = None
    i_12v3: float | None = None
    v_12v4: float | None = None
    i_12v4: float | None = None
    v_12v5: float | None = None
    i_12v5: float | None = None
    v_12v: float | None = None
    i_12v: float | None = None
    v_5v: float | None = None
    i_5v: float | None = None
    v_3v3: float | None = None
    i_3v3: float | None = None
    p_out: float | None = None
    efficiency: float | None = None
    temp: float | None = None
    fan_speed: float | None = None
    # TS models only: the twelve 12V-2x6 per-pin currents from READ_ALL
    pins_12v2x6: list[float] = field(default_factory=list)
    raw: bytes = b""

    @property
    def p_in(self) -> float:
        """Input wattage implied by output power and efficiency (percent)."""
        if not self.efficiency or not self.p_out:
            return 0.0
        return self.p_out / (self.efficiency / 100.0)

    def rails(self) -> list[tuple[str, float | None, float | None]]:
        return [
            ("12V",   self.v_12v,   self.i_12v),
            ("12V1",  self.v_12v1,  self.i_12v1),
            ("12V2",  self.v_12v2,  self.i_12v2),
            ("12V3",  self.v_12v3,  self.i_12v3),
            ("12V4",  self.v_12v4,  self.i_12v4),
            ("12V5",  self.v_12v5,  self.i_12v5),
            ("5V",    self.v_5v,    self.i_5v),
            ("3.3V",  self.v_3v3,   self.i_3v3),
        ]


@dataclass
class FanState:
    mode: int = 0
    customize_duty: int = 0
    actual_duty: int = 0
    calculated_duty: int = 0
    set_duty: int = 0
    zero_fan: bool = False

    # MSI's shipped module names only two of the four opcode values:
    # Str_bt_Performance = 1 and Str_bt_Customized = 3. Values 0 and 2 exist in
    # the opcode space but are not named anywhere in that build, and this unit
    # rejects them (0xFE), so they are reported as unknown rather than given
    # invented names. Mode 1 is MSI's "Performance" but behaves as automatic
    # control: the fan is stopped below ~55% load and runs above it.
    MODE_NAMES = {1: "auto", 3: "custom"}

    @property
    def mode_name(self) -> str:
        return self.MODE_NAMES.get(self.mode, f"unknown({self.mode})")


@dataclass
class DeviceInfo:
    pid: int = 0
    model: str = ""
    mfr_id: str = ""
    revision: str = ""
    serial: str = ""


# --------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------

class PsuError(RuntimeError):
    pass


class Psu:
    """Direct HID access to an MSI PSU. No MSI Center dependency."""

    def __init__(self, pid: int | None = None, path: bytes | None = None):
        self._dev = hid.device()
        self._dev.open_path(path or self._find_path(pid))
        self._dev.set_nonblocking(1)
        self._mutex = _Mutex()
        self.pid = pid or self._detect_pid()
        self.info = DeviceInfo(pid=self.pid,
                               model=PSU_MODELS.get(self.pid, f"unknown(0x{self.pid:04X})"))

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def _find_path(pid: int | None) -> bytes:
        candidates = []
        for d in hid.enumerate(VID_MSI):
            if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE:
                if pid is None or d["product_id"] == pid:
                    candidates.append(d)
        if not candidates:
            raise PsuError(
                "No MSI PSU HID interface found "
                f"(VID 0x{VID_MSI:04X}, usage_page 0x{USAGE_PAGE:04X})"
            )
        return candidates[0]["path"]

    def _detect_pid(self) -> int:
        for d in hid.enumerate(VID_MSI):
            if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE:
                if d["product_id"] in PSU_MODELS:
                    return d["product_id"]
        raise PsuError("MSI PSU interface found but PID is not a known model")

    # -- transport ---------------------------------------------------------

    def _drain(self):
        while self._dev.read(REPORT_LEN, timeout_ms=0):
            pass

    def _exchange(self, opcode: int, cmd: int, data: bytes = b"",
                  timeout: float = 1.5) -> list[int] | None:
        """Send one report and return the echo-matched reply.

        Returns a list normalised to the DLL's indexing (index 1 = opcode).
        """
        tx = bytearray(REPORT_LEN)
        tx[1] = opcode
        tx[2] = cmd
        if data:
            tx[3:3 + len(data)] = data

        self._drain()
        if self._dev.write(bytes(tx)) <= 0:
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            rx = self._dev.read(REPORT_LEN, timeout_ms=150)
            if not rx:
                continue
            frame = [0] + list(rx)
            if len(frame) < REPORT_LEN:
                frame += [0] * (REPORT_LEN - len(frame))
            if frame[1] == opcode and frame[2] == cmd:
                return frame
        return None

    @staticmethod
    def _status_ok(frame: list[int]) -> bool:
        """Whether the device accepted a command.

        A reply always echoes opcode and command. The status byte at index 3
        carries the outcome, and on a MEG Ai1300P it *echoes the accepted
        value* rather than being a plain 0/1 ack:

            FAN_SETTING mode 1 (silent)   -> 0x01  accepted
            FAN_SETTING mode 2 (balanced) -> 0xFE  REJECTED (mode unsupported)
            FAN_SETTING mode 3 (custom)   -> 0x03  accepted
            MULTIRAIL_OCP enable          -> 0x02  accepted
            SAVE_SETTING                  -> 0xFC  accepted (no payload)

        0xFE is therefore a rejection. Treating every echo as success
        silently reports writes the device refused -- which is exactly how a
        "fan mode set to balanced" message once appeared while the fan stayed
        in silent.
        """
        return frame[3] != 0xFE

    def _read(self, cmd: int, retries: int = 5) -> list[int] | None:
        for _ in range(retries):
            frame = self._exchange(CMD.READ, cmd)
            if frame is not None:
                return frame
            time.sleep(0.03)
        return None

    def _write(self, cmd: int, data: bytes = b"", retries: int = 5,
               verify: bool = False) -> bool:
        """Send a write command and report whether the device accepted it.

        With ``verify`` the same command is read back and compared, which
        catches a device that acknowledges but silently ignores the value.
        """
        accepted = False
        for _ in range(retries):
            frame = self._exchange(CMD.WRITE, cmd, data)
            if frame is not None:
                accepted = self._status_ok(frame)
                break
            time.sleep(0.03)
        if not accepted:
            return False
        if verify:
            back = self._read(cmd)
            if back is None:
                return False
            # Compare only the payload the caller supplied.
            return bytes(back[3:3 + len(data)]) == data if data else True
        return True

    def _scalar(self, cmd: int) -> float | None:
        frame = self._read(cmd)
        if frame is None:
            return None
        return decode_value(frame[3], frame[4])

    def _string(self, cmd: int, count: int) -> str | None:
        frame = self._read(cmd)
        if frame is None:
            return None
        return bytes(frame[3:3 + count]).decode("ascii", errors="replace")

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> bool:
        """Handshake (opcode 250 / cmd 81)."""
        with self._mutex:
            frame = self._exchange(CMD.HANDSHAKE, CMD.READ, timeout=2.0)
        return frame is not None

    def close(self):
        try:
            self._dev.close()
        except Exception:
            pass

    def __enter__(self):
        if not self.connect():
            raise PsuError("handshake failed")
        self.read_info()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- identity ----------------------------------------------------------

    def read_info(self) -> DeviceInfo:
        mfr = self._string(CMD.MFR_ID, 3)
        if mfr:
            self.info.mfr_id = mfr.rstrip("\x00 ")
        model = self._string(CMD.MFR_MODEL, 11 if self.pid in (
            PID_MEG_Ai1000P, PID_MEG_Ai1300P, PID_MEG_Ai1600T) else 12)
        if model:
            self.info.model = model.rstrip("\x00 ")
        rev = self._string(CMD.MFR_REVISION, 2)
        if rev and len(rev) == 2:
            self.info.revision = f"{rev[0]}.{rev[1]}"
        serial = self._string(CMD.MFR_SERIAL, 24)
        if serial:
            self.info.serial = serial.rstrip("\x00 ")
        return self.info

    # -- telemetry ---------------------------------------------------------

    def read_telemetry(self) -> Telemetry | None:
        """READ_ALL (cmd 224), decoded per the layout this model uses.

        The original switches on PID and has three distinct layouts:

          Ai1000P / Ai1300P  20 pairs: 12V1..12V5 (V+I), 12V, 5V, 3.3V,
                             P_Out, Eff, Temp, Fan
          Ai1600T            10 pairs: 12V, 5V, 3.3V, P_Out, Eff, Temp, Fan
                             (no per-rail fields at all)
          TS models          currents only: 12V, 5V, 3.3V then the twelve
                             12V-2x6 per-pin currents

        Applying layout 1 to any other model yields plausible-looking but
        wrong numbers, so the branch matters.
        """
        frame = self._read(CMD.READ_ALL)
        if frame is None:
            return None
        t = Telemetry(raw=bytes(frame[1:]))
        i = 3

        def pair() -> float:
            nonlocal i
            v = decode_value(frame[i], frame[i + 1])
            i += 2
            return v

        if self.pid in PCIE5_MODELS:
            # currents only; voltages are not carried in this layout
            t.i_12v = pair()
            t.i_5v = pair()
            t.i_3v3 = pair()
            pins = [pair() for _ in range(12)]
            t.pins_12v2x6 = pins
            # the PSU's own total is still available from its dedicated field
            return t

        if self.pid == PID_MEG_Ai1600T:
            (t.v_12v, t.i_12v, t.v_5v, t.i_5v,
             t.v_3v3, t.i_3v3, t.p_out, t.efficiency,
             t.temp, t.fan_speed) = [pair() for _ in range(10)]
            return t

        # default: Ai1000P / Ai1300P
        (t.v_12v1, t.i_12v1, t.v_12v2, t.i_12v2,
         t.v_12v3, t.i_12v3, t.v_12v4, t.i_12v4,
         t.v_12v5, t.i_12v5, t.v_12v, t.i_12v,
         t.v_5v, t.i_5v, t.v_3v3, t.i_3v3,
         t.p_out, t.efficiency, t.temp, t.fan_speed) = [pair() for _ in range(20)]
        return t

    def read_vin(self) -> float | None:
        if self.pid in PCIE5_MODELS:
            return None
        return self._scalar(CMD.VIN)

    def read_rails_12v5(self) -> list[float] | None:
        if self.pid in (PID_MEG_Ai1600T,) + PCIE5_MODELS:
            return None
        frame = self._read(CMD.VOUT_12V5)
        if frame is None:
            return None
        return [decode_value(frame[3 + i * 2], frame[4 + i * 2]) for i in range(5)]

    def read_currents_12v5(self) -> list[float] | None:
        if self.pid in (PID_MEG_Ai1600T,) + PCIE5_MODELS:
            return None
        frame = self._read(CMD.IOUT_12V5)
        if frame is None:
            return None
        return [decode_value(frame[3 + i * 2], frame[4 + i * 2]) for i in range(5)]

    def read_runtime_now(self) -> int | None:
        frame = self._read(CMD.RUNTIME_NOW)
        if frame is None:
            return None
        return frame[3] | (frame[4] << 8) | (frame[5] << 16) | (frame[6] << 24)

    def read_runtime_total(self) -> int | None:
        frame = self._read(CMD.RUNTIME_TOTAL)
        if frame is None:
            return None
        return frame[3] | (frame[4] << 8) | (frame[5] << 16) | (frame[6] << 24)

    def read_12v2x6_currents(self) -> tuple[list[float], list[float]] | None:
        """Per-pin 12V-2x6 currents. Only available on the TS models."""
        if self.pid not in PCIE5_MODELS:
            return None
        frame = self._read(CMD.IOUT_12V2x6)
        if frame is None:
            return None
        rail2 = [decode_value(frame[3 + i * 2], frame[4 + i * 2]) for i in range(6)]
        rail1 = [decode_value(frame[15 + j * 2], frame[16 + j * 2]) for j in range(6)]
        return rail1, rail2

    # -- alerts ------------------------------------------------------------

    def read_alerts(self) -> tuple[bool, list[str]] | None:
        """Returns (all_ok, [active alert names])."""
        frame = self._read(CMD.READ_ALERTSTATUS)
        if frame is None:
            return None
        idx = alert_index_for(self.pid)
        active = [ALERT_NAMES[idx[k]] for k in range(len(idx))
                  if frame[3 + k] != 0 and idx[k] < len(ALERT_NAMES)]
        return (not active), active

    # -- fan ---------------------------------------------------------------

    def read_fan(self) -> FanState | None:
        st = FanState()
        frame = self._read(CMD.FAN_SETTING)
        if frame is not None:
            st.mode = frame[3]
            st.customize_duty = frame[5] if frame[3] == 3 else 0
        frame = self._read(CMD.READ_FANDUTY)
        if frame is not None:
            st.actual_duty = frame[3]
            st.calculated_duty = frame[4]
            if self.pid not in (PID_MEG_Ai1000P, PID_MEG_Ai1300P, PID_MEG_Ai1600T):
                st.set_duty = frame[5]
                st.zero_fan = frame[6] == 1
        return st

    def set_fan(self, mode: int, duty: int = 0) -> bool:
        """Set the fan mode. Returns False if the device rejects the value.

        Not every mode is accepted by every unit: a MEG Ai1300P accepts 1
        (silent) and 3 (custom) but answers 0xFE for 0 and 2. The caller must
        check the return value rather than assuming success.
        """
        if not 0 <= mode <= 3:
            raise ValueError("fan mode must be 0..3")
        if not 0 <= duty <= 100:
            raise ValueError("duty must be 0..100")
        return self._write(CMD.FAN_SETTING, bytes([mode, 0, duty]))

    def supported_fan_modes(self) -> list[int]:
        """Probe which fan modes this unit accepts.

        Uses a no-op duty of 0 for custom so the probe does not change the
        running fan curve beyond the mode switch itself.
        """
        current = self.read_fan()
        ok: list[int] = []
        for mode in (0, 1, 2, 3):
            frame = self._exchange(CMD.WRITE, CMD.FAN_SETTING,
                                   bytes([mode, 0, 0]))
            if frame is not None and self._status_ok(frame):
                ok.append(mode)
            time.sleep(0.05)
        # put the original mode back
        if current and current.mode in ok:
            self._write(CMD.FAN_SETTING,
                        bytes([current.mode, 0, current.customize_duty]))
        return ok

    def set_zero_fan(self, enable: bool) -> bool:
        """Toggle zero-fan mode.

        The original module issues ZERO_FAN_ENABLE (cmd 67) for every model,
        but a MEG Ai1300P never answers it — three consecutive attempts with a
        drained input buffer all time out, and the fan state is unchanged
        afterwards. The call is therefore reported as unsupported here rather
        than silently claiming success.
        """
        if self.pid in (PID_MEG_Ai1000P, PID_MEG_Ai1300P):
            raise PsuError(
                "zero-fan control is not supported by this model "
                "(the device does not answer ZERO_FAN_ENABLE)")
        return self._write(CMD.ZERO_FAN_ENABLE, bytes([1 if enable else 0]))

    # -- OCP ---------------------------------------------------------------

    def read_ocp(self) -> bool | None:
        if self.pid in NO_MULTIRAIL_OCP:
            return None
        frame = self._read(CMD.MULTIRAIL_OCP)
        if frame is None:
            return None
        return frame[3] == 2

    def set_ocp(self, enable: bool) -> bool:
        if self.pid in NO_MULTIRAIL_OCP:
            raise PsuError("multi-rail OCP is not configurable on this model")
        return self._write(CMD.MULTIRAIL_OCP, bytes([1 if not enable else 2]))

    def read_ocp_12v2x6(self) -> dict | None:
        """12V-2x6 OCP configuration. TS models only."""
        if self.pid not in PCIE5_MODELS:
            return None
        frame = self._read(CMD.OCP12V2x6_CONFIG)
        if frame is None:
            return None
        return {
            "enabled": frame[3] == 1,
            "ocp_point": decode_value(frame[4], frame[5]),
            "current_diff": decode_value(frame[6], frame[7]),
            "ocp_trigger_time": frame[8],
            "diff_trigger_time": frame[9],
            "warning_time": frame[10],
        }

    def set_ocp_12v2x6(self, cfg: dict) -> bool:
        """Write the 12V-2x6 OCP configuration. TS models only.

        The device stores the OCP point and current-difference thresholds as a
        single byte: the integer part in the high nibble and 0xE0 in the low
        bits. The original clamps each field to its documented range.
        """
        if self.pid not in PCIE5_MODELS:
            raise PsuError("12V-2x6 OCP is not supported on this model")

        def clamp(v, lo, hi):
            return max(lo, min(hi, v))

        ocp_point = clamp(int(cfg.get("ocp_point", 10)), 10, 14)
        current_diff = clamp(int(cfg.get("current_diff", 4)), 4, 8)
        ocp_trig = clamp(int(cfg.get("ocp_trigger_time", 5)), 5, 30)
        diff_trig = clamp(int(cfg.get("diff_trigger_time", 5)), 5, 30)
        warning = clamp(int(cfg.get("warning_time", 30)), 30, 240)

        payload = bytes([
            1 if cfg.get("enabled") else 0,
            (ocp_point << 4) & 0xFF,
            0xE0,
            (current_diff << 4) & 0xFF,
            0xE0,
            ocp_trig,
            diff_trig,
            warning,
        ])
        return self._write(CMD.OCP12V2x6_CONFIG, payload)

    def read_ocp_12v2x6_state(self) -> dict | None:
        if self.pid not in PCIE5_MODELS:
            return None
        frame = self._read(CMD.OCP12V2x6_STATE)
        if frame is None:
            return None
        names = ["Normal", "OCP_Point", "Current_Diff", "OCP_18A"]
        out = {}
        for rail, base in ((1, 3), (2, 24)):
            status = frame[base]
            out[f"rail{rail}_status"] = names[status] if status < len(names) else status
            out[f"rail{rail}_runtime"] = (frame[base+1] | (frame[base+2] << 8)
                                          | (frame[base+3] << 16) | (frame[base+4] << 24))
            out[f"rail{rail}_totaltime"] = (frame[base+5] | (frame[base+6] << 8)
                                            | (frame[base+7] << 16) | (frame[base+8] << 24))
            out[f"rail{rail}_pins"] = [
                decode_value(frame[base + 9 + k * 2], frame[base + 10 + k * 2])
                for k in range(6)
            ]
        return out

    # -- misc --------------------------------------------------------------

    def save_settings(self) -> bool:
        return self._write(CMD.SAVE_SETTING)

    def buzzer_switch(self) -> bool:
        if self.pid in (PID_MEG_Ai1000P, PID_MEG_Ai1300P, PID_MEG_Ai1600T):
            raise PsuError("buzzer switch is not supported on this model")
        return self._write(CMD.BUZZER_SWITCH)

    def raw(self, opcode: int, cmd: int, data: bytes = b"") -> list[int] | None:
        return self._exchange(opcode, cmd, data)


# --------------------------------------------------------------------------
# Energy statistics
#
# Mirrors the accounting the MSI module performs in its 60-second profile
# tick, but stores state in this tool's own directory instead of MSI Center's
# so the two never fight over the same file.
# --------------------------------------------------------------------------

@dataclass
class EnergyProfile:
    """Cumulative energy accounting plus a rolling 7-day kWh history."""

    path: str = ""
    count: int = 0
    wattage_total: float = 0.0      # sum of output watts samples
    efficiency_total: float = 0.0   # sum of efficiency samples
    wattage_in_total: float = 0.0   # sum of input watts samples
    time_daily: float = 0.0         # seconds powered today
    time_total: float = 0.0         # seconds powered, all time
    kwh_total: float = 0.0
    pre_kwh_total: float = 0.0      # value at the start of today
    daily: dict[str, float] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------

    @property
    def avg_wattage(self) -> float:
        return self.wattage_total / self.count if self.count else 0.0

    @property
    def avg_efficiency(self) -> float:
        return self.efficiency_total / self.count if self.count else 0.0

    @property
    def today_kwh(self) -> float:
        return max(0.0, self.kwh_total - self.pre_kwh_total)

    def week(self) -> list[tuple[str, float]]:
        """The last 7 days, oldest first, as (MM/DD, kWh)."""
        today = datetime.date.today()
        out = []
        for back in range(6, -1, -1):
            d = today - datetime.timedelta(days=back)
            key = d.strftime("%m-%d-%Y")
            out.append((d.strftime("%m/%d"), self.daily.get(key, 0.0)))
        return out

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        if not self.path:
            return
        data = {
            "count": self.count,
            "wattage_total": self.wattage_total,
            "efficiency_total": self.efficiency_total,
            "wattage_in_total": self.wattage_in_total,
            "time_daily": self.time_daily,
            "time_total": self.time_total,
            "kwh_total": self.kwh_total,
            "pre_kwh_total": self.pre_kwh_total,
            "daily": self.daily,
            "saved_on": datetime.date.today().isoformat(),
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str) -> "EnergyProfile":
        prof = cls(path=path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return prof
        for key in ("count", "wattage_total", "efficiency_total",
                    "wattage_in_total", "time_daily", "time_total",
                    "kwh_total", "pre_kwh_total"):
            if key in data:
                setattr(prof, key, data[key])
        prof.daily = dict(data.get("daily") or {})
        # A new calendar day resets the daily counter and rebases kWh.
        if data.get("saved_on") != datetime.date.today().isoformat():
            prof.pre_kwh_total = prof.kwh_total
            prof.time_daily = 0.0
        return prof

    def add_sample(self, watts_out: float, efficiency: float,
                   seconds: float, input_watts: float | None = None) -> None:
        """Fold one interval into the running totals."""
        if watts_out <= 0 or efficiency <= 0:
            return
        p_in = input_watts if input_watts is not None else watts_out / (efficiency / 100.0)
        self.count += 1
        self.wattage_total += watts_out
        self.efficiency_total += efficiency
        self.wattage_in_total += p_in
        self.time_daily += seconds
        self.time_total += seconds
        # Energy = mean watts x hours; keep it incremental so it survives restarts.
        self.kwh_total += watts_out * (seconds / 3600.0) / 1000.0
        key = datetime.date.today().strftime("%m-%d-%Y")
        self.daily[key] = self.daily.get(key, 0.0) + \
            watts_out * (seconds / 3600.0) / 1000.0
        # keep the history bounded
        if len(self.daily) > 60:
            for k in sorted(self.daily)[:-60]:
                self.daily.pop(k, None)
