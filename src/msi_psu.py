"""msi-psu — standalone control and monitoring for MSI power supplies.

Reimplements the MSI Center "Power Supply Unit" module without MSI Center.

Commands:
    info        device identity
    status      full telemetry snapshot
    watch       live refreshing display
    fan         read/write fan mode and duty
    zerofan     read/write zero-fan mode
    ocp         read/write multi-rail OCP
    ocp-pcie5   read/write 12V-2x6 OCP configuration (TS models)
    alerts      active protection alerts
    runtime     operating hours
    buzzer      toggle the audible alarm (TS models)
    raw         send an arbitrary command (protocol exploration)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

# --------------------------------------------------------------------------
# console encoding
#
# On a zh-CN Windows the console defaults to codepage 936 (GBK). Writing
# UTF-8 bytes there produces mojibake, and ANSI colour escapes show up as
# literal characters in older consoles. Reconfigure stdout/stderr to UTF-8
# and ask the console to interpret it as UTF-8 (codepage 65001) so non-ASCII
# text and box drawing render correctly.
# --------------------------------------------------------------------------


def _init_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            k32.SetConsoleOutputCP(65001)
            # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING so ANSI colour works
            # instead of printing literal escape sequences.
            k32.GetStdHandle.restype = ctypes.c_void_p
            handle = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
                k32.SetConsoleMode(ctypes.c_void_p(handle),
                                   mode.value | 0x0004)
        except Exception:
            pass


_init_console()

from psu_protocol import (
    CMD, Psu, PsuError, PSU_MODELS, PCIE5_MODELS, num,
)

# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def _c(text: str, colour: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{colour}{text}{RESET}"


def bar(value: float, maximum: float, width: int = 28) -> str:
    if maximum <= 0:
        return " " * width
    filled = int(round(min(max(value, 0.0), maximum) / maximum * width))
    return "#" * filled + "." * (width - filled)


def v_color(v: float, nominal: float, tol: float = 0.05) -> str:
    """ATX tolerance colouring (+/-5%)."""
    if nominal == 0:
        return str(v)
    dev = abs(v - nominal) / nominal
    if dev <= tol * 0.5:
        return _c(f"{v:7.3f}", GREEN)
    if dev <= tol:
        return _c(f"{v:7.3f}", YELLOW)
    return _c(f"{v:7.3f}", RED)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_info(p: Psu, args):
    i = p.info
    if args.json:
        print(json.dumps({
            "pid": f"0x{i.pid:04X}",
            "model": i.model,
            "mfr_id": i.mfr_id,
            "revision": i.revision,
            "serial": i.serial,
        }, indent=2))
        return 0
    print(f"{BOLD}MSI PSU{RESET}")
    print(f"  Model      : {i.model}")
    print(f"  Vendor ID  : 0x{i.mfr_id and '0DB0' or '?'}   Product ID: 0x{i.pid:04X}")
    print(f"  MFR ID     : {i.mfr_id!r}")
    print(f"  Revision   : {i.revision!r}")
    print(f"  Serial     : {i.serial!r}" if i.serial else "  Serial     : (not reported)")
    return 0


def _collect(p: Psu) -> dict:
    t = p.read_telemetry()
    if t is None:
        raise PsuError("failed to read telemetry")
    fan = p.read_fan()
    alerts = p.read_alerts()
    fan_d = None
    if fan:
        fan_d = asdict(fan)
        fan_d["mode_name"] = fan.mode_name      # property, not a dataclass field
    data = {
        "telemetry": asdict(t),
        "p_in": round(t.p_in, 2),
        "vin": p.read_vin(),
        "fan": fan_d,
        "alerts_ok": alerts[0] if alerts else None,
        "alerts": alerts[1] if alerts else None,
        "ocp": p.read_ocp(),
        "runtime_now": p.read_runtime_now(),
        "runtime_total": p.read_runtime_total(),
    }
    data["telemetry"].pop("raw", None)
    return data


def cmd_status(p: Psu, args):
    data = _collect(p)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    t = data["telemetry"]
    print(f"{BOLD}{p.info.model}{RESET}  "
          f"{DIM}(PID 0x{p.pid:04X}, rev {p.info.revision}){RESET}")
    print()

    print(f"{BOLD}Rails{RESET}")
    for name, v, i, nominal in [
        ("12V ", t["v_12v"], t["i_12v"], 12.0),
        ("12V1", t["v_12v1"], t["i_12v1"], 12.0),
        ("12V2", t["v_12v2"], t["i_12v2"], 12.0),
        ("12V3", t["v_12v3"], t["i_12v3"], 12.0),
        ("12V4", t["v_12v4"], t["i_12v4"], 12.0),
        ("12V5", t["v_12v5"], t["i_12v5"], 12.0),
        ("5V  ", t["v_5v"], t["i_5v"], 5.0),
        ("3.3V", t["v_3v3"], t["i_3v3"], 3.3),
    ]:
        print(f"  {name}  {v_color(v, nominal)} V  {i:7.3f} A")

    print()
    print(f"{BOLD}Power{RESET}")
    print(f"  Output     : {t['p_out']:8.2f} W")
    print(f"  Input      : {data['p_in']:8.2f} W   {DIM}(derived){RESET}")
    print(f"  Efficiency : {t['efficiency']:8.2f} %   [{bar(t['efficiency'], 100)}]")
    if data["vin"]:
        print(f"  AC input   : {data['vin']:8.1f} V")

    print()
    print(f"{BOLD}Thermal{RESET}")
    print(f"  Temperature: {t['temp']:8.1f} C")
    print(f"  Fan speed  : {t['fan_speed']:8.0f} RPM")
    if data["fan"]:
        f = data["fan"]
        print(f"  Fan mode   : {f['mode']} ({f['mode_name']})")
        print(f"  Duty       : actual {f['actual_duty']}%  calc {f['calculated_duty']}%"
              + (f"  set {f['set_duty']}%" if f["set_duty"] else ""))
        print(f"  Zero fan   : {'on' if f['zero_fan'] else 'off'}")

    print()
    print(f"{BOLD}Health{RESET}")
    ok = data["alerts_ok"]
    print(f"  Alerts     : " + (_c("all OK", GREEN) if ok else _c(str(data["alerts"]), RED)))
    print(f"  Multi-rail OCP : {data['ocp']}")
    if data["runtime_now"] is not None:
        print(f"  Runtime now    : {data['runtime_now']} s "
              f"({data['runtime_now']/3600:.2f} h)")
    if data["runtime_total"] is not None:
        print(f"  Runtime total  : {data['runtime_total']} s "
              f"({data['runtime_total']/3600:.1f} h)")
    return 0


def cmd_watch(p: Psu, args):
    interval = max(args.interval, 0.2)
    try:
        while True:
            data = _collect(p)
            t = data["telemetry"]
            if not args.json:
                sys.stdout.write("\033[2J\033[H" if sys.stdout.isatty() else "")
                print(f"{BOLD}{p.info.model}{RESET}   {time.strftime('%H:%M:%S')}")
                print()
                for name, v, i, nominal in [
                    ("12V ", t["v_12v"], t["i_12v"], 12.0),
                    ("5V  ", t["v_5v"], t["i_5v"], 5.0),
                    ("3.3V", t["v_3v3"], t["i_3v3"], 3.3),
                ]:
                    print(f"  {name}  {v_color(v, nominal)} V  {i:7.3f} A")
                print()
                print(f"  Output     : {t['p_out']:8.2f} W   "
                      f"Input: {data['p_in']:8.2f} W   Eff: {t['efficiency']:6.2f} %")
                print(f"  Temperature: {t['temp']:8.1f} C   "
                      f"Fan: {t['fan_speed']:5.0f} RPM")
                if data["fan"]:
                    print(f"  Mode       : {data['fan']['mode_name']}  "
                          f"duty {data['fan']['actual_duty']}%")
                ok = data["alerts_ok"]
                print(f"  Alerts     : " + (_c("all OK", GREEN) if ok
                                            else _c(str(data["alerts"]), RED)))
                print(f"\n{DIM}Ctrl-C to stop{RESET}")
            else:
                print(json.dumps(data))
                sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_fan(p: Psu, args):
    if getattr(args, "list_modes", False):
        modes = p.supported_fan_modes()
        # Only 1 and 3 are named by MSI's own build (Str_bt_Performance /
        # Str_bt_Customized). 0 and 2 exist in the opcode space but are unnamed
        # there, so they print as "mode N" rather than a made-up label.
        names = {1: "auto", 3: "custom"}
        label = lambda m: names.get(m, f"mode {m}")   # noqa: E731
        if args.json:
            print(json.dumps({"supported_modes": modes}, indent=2))
        else:
            print("Fan modes accepted by this unit:")
            for m in modes:
                print(f"  {m}  {label(m)}")
            missing = [m for m in (0, 1, 2, 3) if m not in modes]
            if missing:
                print(f"  (rejected: {', '.join(label(m) for m in missing)})")
        return 0

    if args.mode is None and args.duty is None:
        f = p.read_fan()
        if not f:
            print("failed to read fan state", file=sys.stderr)
            return 1
        if args.json:
            d = asdict(f)
            d["mode_name"] = f.mode_name
            print(json.dumps(d, indent=2))
        else:
            print(f"Fan mode        : {f.mode} ({f.mode_name})")
            print(f"Actual duty     : {f.actual_duty} %")
            print(f"Calculated duty : {f.calculated_duty} %")
            if f.set_duty:
                print(f"Set duty        : {f.set_duty} %")
        return 0

    if args.mode is None:
        print("--mode is required when setting duty", file=sys.stderr)
        return 2
    duty = args.duty if args.duty is not None else 0
    if args.mode == 3 and args.duty is None:
        print("mode 3 (custom) requires --duty", file=sys.stderr)
        return 2
    if not p.set_fan(args.mode, duty):
        print(f"error: the PSU rejected fan mode {args.mode}.", file=sys.stderr)
        print("Not every unit supports every mode; try 'fan --list-modes'.",
              file=sys.stderr)
        return 1
    if not p.save_settings():
        print("warning: fan set but save_settings failed", file=sys.stderr)
    if not args.json:
        print(f"fan mode set to {args.mode}" + (f", duty {duty}%" if args.mode == 3 else ""))
    return 0


def cmd_zerofan(p: Psu, args):
    if args.state is None:
        f = p.read_fan()
        if not f:
            print("failed to read fan state", file=sys.stderr)
            return 1
        print("on" if f.zero_fan else "off")
        return 0
    enable = args.state == "on"
    if not p.set_zero_fan(enable):
        print("set zero-fan failed", file=sys.stderr)
        return 1
    p.save_settings()
    # The original re-reads and retries until the device agrees.
    for _ in range(3):
        f = p.read_fan()
        if f and f.zero_fan == enable:
            break
        p.set_zero_fan(enable)
        p.save_settings()
    print(f"zero fan {'enabled' if enable else 'disabled'}")
    return 0


def cmd_ocp(p: Psu, args):
    if args.state is None:
        v = p.read_ocp()
        if v is None:
            print("multi-rail OCP not supported on this model", file=sys.stderr)
            return 1
        print("enabled" if v else "disabled")
        return 0
    enable = args.state == "on"
    try:
        if not p.set_ocp(enable):
            print("set OCP failed", file=sys.stderr)
            return 1
    except PsuError as e:
        print(str(e), file=sys.stderr)
        return 1
    p.save_settings()
    print(f"multi-rail OCP {'enabled' if enable else 'disabled'}")
    return 0


def cmd_ocp_pcie5(p: Psu, args):
    if p.pid not in PCIE5_MODELS:
        print("12V-2x6 OCP is only available on MPG Ai1300TS / Ai1600TS",
              file=sys.stderr)
        return 1
    if args.state is None:
        cfg = p.read_ocp_12v2x6()
        st = p.read_ocp_12v2x6_state()
        if args.json:
            print(json.dumps({"config": cfg, "state": st}, indent=2))
            return 0
        print("Config:")
        for k, v in (cfg or {}).items():
            print(f"  {k:20s}: {v}")
        print("State:")
        for k, v in (st or {}).items():
            print(f"  {k:20s}: {v}")
        return 0
    # write path
    from psu_protocol import decode_value  # noqa: F401
    cfg = p.read_ocp_12v2x6()
    if cfg is None:
        print("failed to read current OCP config", file=sys.stderr)
        return 1
    cfg["enabled"] = args.state == "on"
    for field, val in (("ocp_point", args.ocp_point),
                       ("current_diff", args.current_diff),
                       ("ocp_trigger_time", args.ocp_trigger),
                       ("diff_trigger_time", args.diff_trigger),
                       ("warning_time", args.warning)):
        if val is not None:
            cfg[field] = val
    if not p.set_ocp_12v2x6(cfg):
        print("set 12V-2x6 OCP failed", file=sys.stderr)
        return 1
    p.save_settings()
    print("12V-2x6 OCP updated")
    return 0


def cmd_alerts(p: Psu, args):
    res = p.read_alerts()
    if res is None:
        print("failed to read alerts", file=sys.stderr)
        return 1
    ok, active = res
    if args.json:
        print(json.dumps({"ok": ok, "active": active}, indent=2))
        return 0
    if ok:
        print(_c("all protection alerts OK", GREEN))
        return 0
    print(_c(f"{len(active)} active alert(s):", RED))
    for a in active:
        print(f"  - {a}")
    return 0


def cmd_runtime(p: Psu, args):
    now = p.read_runtime_now()
    total = p.read_runtime_total()
    if args.json:
        print(json.dumps({"now_s": now, "total_s": total}, indent=2))
        return 0
    if now is not None:
        print(f"Current session : {now} s  ({now/3600:.2f} h)")
    if total is not None:
        print(f"Total           : {total} s  ({total/3600:.1f} h, "
              f"{total/86400:.1f} days)")
    return 0


def cmd_buzzer(p: Psu, args):
    try:
        if not p.buzzer_switch():
            print("buzzer toggle failed", file=sys.stderr)
            return 1
    except PsuError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("buzzer toggled")
    return 0


def cmd_raw(p: Psu, args):
    data = bytes.fromhex(args.data) if args.data else b""
    op = CMD.READ if args.op == "read" else CMD.WRITE
    frame = p.raw(op, args.cmd, data)
    if frame is None:
        print("no reply", file=sys.stderr)
        return 1
    print(" ".join(f"{b:02X}" for b in frame[1:]))
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Double-clicked with no arguments: launch the GUI rather than a console
    # menu. A console program started from Explorer loses its window the
    # instant it exits, which is what made the menu look like a crash.
    if not argv:
        return _launch_gui()
    if argv[0] == "gui":
        return _launch_gui(argv[1:])

    rc = _run(argv)
    return rc


def _launch_gui(extra: list[str] | None = None) -> int:
    try:
        from msi_psu_gui import App
    except ImportError as e:
        print(f"error: GUI unavailable ({e})", file=sys.stderr)
        print("Use a command instead, e.g. 'msi-psu.exe status'.", file=sys.stderr)
        return 2
    import argparse as _ap
    p = _ap.ArgumentParser(prog="msi-psu gui", add_help=False)
    p.add_argument("--pid", type=lambda s: int(s, 0), default=None)
    try:
        ns, _ = p.parse_known_args(extra or [])
    except SystemExit:
        return 2
    try:
        App(pid=ns.pid).mainloop()
        return 0
    except Exception as e:                          # noqa: BLE001
        print(f"error: could not start the GUI: {e}", file=sys.stderr)
        return 1



def _run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="msi-psu",
        description="Standalone control/monitoring for MSI power supplies. "
                    "Run with no arguments to open the graphical interface.",
    )
    # Report errors as a return code instead of raising SystemExit, so the
    # caller can keep the console open for a double-clicked run.
    ap.exit_on_error = False
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=None,
                    help="select a model by product ID (e.g. 0x56D4)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="device identity").set_defaults(func=cmd_info)

    sub.add_parser("status", help="full telemetry snapshot").set_defaults(func=cmd_status)

    w = sub.add_parser("watch", help="live refreshing display")
    w.add_argument("-i", "--interval", type=float, default=1.0)
    w.set_defaults(func=cmd_watch)

    f = sub.add_parser("fan", help="read or set fan mode")
    f.add_argument("--mode", type=int, choices=[0, 1, 2, 3],
                   help="0=auto 1=silent 2=balanced 3=custom")
    f.add_argument("--duty", type=int, help="duty 0..100 (required for mode 3)")
    f.add_argument("--list-modes", action="store_true",
                   help="probe which fan modes this unit accepts")
    f.set_defaults(func=cmd_fan)

    z = sub.add_parser("zerofan", help="read or set zero-fan mode")
    z.add_argument("state", nargs="?", choices=["on", "off"])
    z.set_defaults(func=cmd_zerofan)

    o = sub.add_parser("ocp", help="read or set multi-rail OCP")
    o.add_argument("state", nargs="?", choices=["on", "off"])
    o.set_defaults(func=cmd_ocp)

    op5 = sub.add_parser("ocp-pcie5", help="12V-2x6 OCP config (TS models)")
    op5.add_argument("state", nargs="?", choices=["on", "off"])
    op5.add_argument("--ocp-point", type=float, help="10..14 (A per pin)")
    op5.add_argument("--current-diff", type=float, help="4..8 (A imbalance)")
    op5.add_argument("--ocp-trigger", type=int, help="5..30 ms")
    op5.add_argument("--diff-trigger", type=int, help="5..30 ms")
    op5.add_argument("--warning", type=int, help="30..240 s")
    op5.set_defaults(func=cmd_ocp_pcie5)

    sub.add_parser("alerts", help="protection alert status").set_defaults(func=cmd_alerts)
    sub.add_parser("runtime", help="operating hours").set_defaults(func=cmd_runtime)
    sub.add_parser("buzzer", help="toggle buzzer (TS models)").set_defaults(func=cmd_buzzer)

    r = sub.add_parser("raw", help="send an arbitrary command")
    r.add_argument("op", choices=["read", "write"])
    r.add_argument("cmd", type=lambda s: int(s, 0))
    r.add_argument("data", nargs="?", default="")
    r.set_defaults(func=cmd_raw)

    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        # argparse already printed the message (or --help output).
        return int(e.code or 0)
    except argparse.ArgumentError as e:
        # Python 3.12: an invalid subcommand choice surfaces as ArgumentError
        # rather than SystemExit when exit_on_error is False.
        print(f"error: {e}", file=sys.stderr)
        print("Run 'msi-psu --help' for the command list.", file=sys.stderr)
        return 2
    try:
        psu = Psu(pid=args.pid)
    except PsuError as e:
        print(f"error: {e}", file=sys.stderr)
        print("\nIs the PSU connected, and is this an MSI power supply?",
              file=sys.stderr)
        return 2

    try:
        if not psu.connect():
            print("error: handshake with PSU failed", file=sys.stderr)
            return 2
        psu.read_info()
        return args.func(psu, args)
    except PsuError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        psu.close()


if __name__ == "__main__":
    sys.exit(main())
