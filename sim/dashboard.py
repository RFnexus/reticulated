"""Generate a standalone HTML dashboard from one or more scenario reports."""

import argparse
import html
from importlib import resources
import json
import os
import sys
import webbrowser

from .reporting import migrate_report_v3


ASSET_PACKAGE = "sim.dashboard_assets"


def _asset(name):
    return resources.files(ASSET_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def _javascript_json(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render_dashboard(reports, title="Reticulated Scenario Dashboard"):
    template = _asset("dashboard.html")
    return (template
            .replace("/*__DASHBOARD_CSS__*/", _asset("dashboard.css"))
            .replace("/*__DASHBOARD_DATA__*/", "const EMBEDDED_REPORTS = " +
                     _javascript_json(reports) + ";")
            .replace("/*__DASHBOARD_JS__*/", _asset("dashboard.js"))
            .replace("__DASHBOARD_TITLE__", html.escape(str(title))))


def load_reports(paths):
    reports = []
    for path in paths:
        with open(path, encoding="utf-8") as stream:
            report = json.load(stream)
        if not isinstance(report, dict):
            raise ValueError("report must be a JSON object: " + path)
        report = migrate_report_v3(report)
        report.setdefault("source_file", os.path.basename(path))
        reports.append(report)
    return reports


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a standalone dashboard from Reticulated scenario reports")
    parser.add_argument("reports", nargs="*", help="scenario report JSON files")
    parser.add_argument("-o", "--output", default="reticulated-dashboard.html")
    parser.add_argument("--title", default="Reticulated Scenario Dashboard")
    parser.add_argument("--open", action="store_true", help="open the generated dashboard")
    args = parser.parse_args(argv)

    try:
        reports = load_reports(args.reports)
        output = os.path.abspath(args.output)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(render_dashboard(reports, args.title))
        print(json.dumps({"dashboard": output, "reports": len(reports)}, sort_keys=True))
        if args.open:
            webbrowser.open("file://" + output)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print("reticulated-dashboard: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
