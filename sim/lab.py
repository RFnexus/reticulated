"""Command-line entry point for conformance and matched network evidence."""

import argparse
import json
import os
import sys

from .benchmark import PROFILES, run_benchmark
from .conformance import run_conformance
from .dashboard import render_dashboard


def _default_lxmf_rs():
    return os.environ.get(
        "RETICULATED_LXMF_RS",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "LXMF-rs")),
    )


def _write_outputs(report, output_dir):
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    stem = "%s-%s" % (report.get("report_type", "lab"), report.get("profile", "run"))
    report_path = os.path.join(output_dir, stem + ".json")
    dashboard_path = os.path.join(output_dir, stem + ".html")
    with open(report_path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    report["source_file"] = os.path.basename(report_path)
    with open(dashboard_path, "w", encoding="utf-8") as stream:
        stream.write(render_dashboard([report], "Reticulated Python/Rust Lab"))
    return report_path, dashboard_path


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reticulated-lab",
        description="Python/Rust conformance and matched-network performance lab",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    conformance = subcommands.add_parser("conformance", help="run LXMF-rs canonical cases")
    conformance.add_argument("--lxmf-rs", default=_default_lxmf_rs())
    conformance.add_argument("--profile", choices=PROFILES, required=True)
    conformance.add_argument("--output-dir", required=True)
    conformance.add_argument("--allow-unpinned", action="store_true",
                             help="diagnostic only; report is marked non-conforming")
    conformance.add_argument("--timeout", type=int, default=420, help="per-case timeout")

    benchmark = subcommands.add_parser("benchmark", help="run matched composition cells")
    benchmark.add_argument("--profile", choices=PROFILES, required=True)
    benchmark.add_argument("--seed", type=int, required=True)
    benchmark.add_argument("--output-dir", required=True)
    benchmark.add_argument("--baseline")
    benchmark.add_argument("--thresholds")
    benchmark.add_argument("--lxmf-rs", default=_default_lxmf_rs(), help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "benchmark" and bool(args.baseline) != bool(args.thresholds):
        parser.error("--baseline and --thresholds must be supplied together")
    if args.command == "conformance":
        report = run_conformance(
            args.lxmf_rs, args.profile, args.output_dir,
            allow_unpinned=args.allow_unpinned, timeout=args.timeout,
        )
        success = report.get("conforming", False)
    else:
        report = run_benchmark(
            args.profile, args.seed, args.output_dir, args.lxmf_rs,
            baseline_path=args.baseline, thresholds_path=args.thresholds,
        )
        success = report.get("status") == "pass"
    try:
        report_path, dashboard_path = _write_outputs(report, args.output_dir)
    except OSError as error:
        print("reticulated-lab: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps({"status": report.get("status"), "report": report_path,
                      "dashboard": dashboard_path}, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
