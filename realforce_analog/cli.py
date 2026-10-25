import argparse
from dataclasses import asdict
import json
from .protocol import SensorReader, discover, device_summary
from .diagnostics import benchmark, matrix_snapshot, save_report


def main():
    parser = argparse.ArgumentParser(description="Custom R3SD11 read-only sensor diagnostics")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("devices", help="Enumerate the R3SD11 vendor HID interface")
    scan = commands.add_parser("scan", help="Read all 128 matrix slots once")
    scan.add_argument("--out", default="reports/matrix.json")
    bench = commands.add_parser("benchmark", help="Measure sequential C5 timing")
    bench.add_argument("--keys", type=int, nargs="+", required=True)
    bench.add_argument("--seconds", type=float, default=5)
    bench.add_argument("--out", default="reports/benchmark.json")
    commands.add_parser("gui", help="Open the calibration and steering utility")
    commands.add_parser("check-output", help="Sweep vJoy device 1 briefly and verify Windows readback")
    args = parser.parse_args()
    if args.command in (None, "gui"):
        from .gui import main as gui_main
        gui_main()
    elif args.command == "devices":
        print(json.dumps([device_summary(row) for row in discover()], indent=2))
    elif args.command == "check-output":
        from .windows_check import verify_output
        report = verify_output()
        save_report("reports/windows-vjoy-check.json", report)
        print(json.dumps(report, indent=2))
    else:
        with SensorReader() as reader:
            if args.command == "scan":
                rows = matrix_snapshot(reader)
                report = {"device": reader.info, "populated": sum(s.populated for s in rows),
                          "sensors": [dict(asdict(s), delta=s.delta) for s in rows]}
                save_report(args.out, report)
                print(f"{report['populated']} populated sensors; saved {args.out}")
            else:
                report = benchmark(reader, args.keys, args.seconds)
                save_report(args.out, report)
                print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
                print(f"Saved raw samples to {args.out}")
