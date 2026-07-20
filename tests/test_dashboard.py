import json
import os
import tempfile
import unittest

from sim.dashboard import load_reports, main, render_dashboard
from sim.reporting import measured_rtt, summarize_report, topology_summary


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "scenario": "mixed-test",
            "status": "pass",
            "nodes": 2,
            "implementations": ["python", "rust"],
            "duration_seconds": 1.25,
            "results": [
                {"action": "wait_ready", "status": "pass", "running": 2},
                {
                    "action": "packet", "status": "pass", "transfer": "packet",
                    "source": "p0", "destination": "r0",
                    "source_implementation": "python",
                    "destination_implementation": "rust",
                    "hops": 3, "attempts": 1,
                    "delivery_evidence": [
                        "EXPECT rtt=0.010 hops=3",
                        "DELIVERED abc rtt=0.123 method=direct",
                    ],
                },
                {"action": "restart", "status": "pass", "node": "r0"},
            ],
            "media": {
                "m0": {"stats": {"tx": 4, "rx": 3, "dropped": 1, "oversize": 0}}
            },
        }

    def test_summary_uses_delivered_rtt_and_aggregates_kpis(self):
        summary = summarize_report(self.report)
        self.assertEqual(summary["actions"]["passed"], 3)
        self.assertEqual(summary["transfers"]["passed"], 1)
        self.assertEqual(summary["faults"]["passed"], 1)
        self.assertEqual(summary["max_hops"], 3)
        self.assertEqual(summary["media"]["dropped"], 1)
        self.assertEqual(summary["transfers"]["measured_rtt_max_seconds"], 0.123)
        self.assertEqual(measured_rtt(self.report["results"][1]), 0.123)

    def test_topology_summary_counts_implementations_and_transports(self):
        summary = topology_summary({"nodes": {
            "p0": {"implementation": "python", "transport": True},
            "r0": {"implementation": "rust", "transport": False},
        }, "links": {"m0": {}}})
        self.assertEqual(summary["implementation_counts"], {"python": 1, "rust": 1})
        self.assertEqual(summary["transport_counts"], {"python": 1})
        self.assertEqual(summary["media_count"], 1)

    def test_dashboard_is_standalone_and_escapes_embedded_script_content(self):
        report = dict(self.report)
        report["scenario"] = "unsafe </script><script>alert(1)</script>"
        rendered = render_dashboard([report], "Test <Dashboard>")
        self.assertIn("const EMBEDDED_REPORTS =", rendered)
        self.assertIn("RNS interoperability lab", rendered)
        self.assertNotIn("unsafe </script>", rendered)
        self.assertIn("unsafe \\u003c/script>", rendered)
        self.assertIn("Test &lt;Dashboard&gt;", rendered)
        self.assertNotIn('src="', rendered)
        self.assertNotIn('href="', rendered)

    def test_cli_loads_multiple_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(2):
                path = os.path.join(directory, f"report-{index}.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump({**self.report, "scenario": f"run-{index}"}, stream)
                paths.append(path)
            output = os.path.join(directory, "dashboard.html")
            self.assertEqual(main([*paths, "--output", output]), 0)
            self.assertEqual(len(load_reports(paths)), 2)
            with open(output, encoding="utf-8") as stream:
                rendered = stream.read()
            self.assertIn("run-0", rendered)
            self.assertIn("run-1", rendered)


if __name__ == "__main__":
    unittest.main()
