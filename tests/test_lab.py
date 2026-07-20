import json
import os
import tempfile
import threading
import time
import unittest
import sys
from unittest import mock

from sim.benchmark import (
    COMPOSITIONS,
    PROFILES,
    assign_implementations,
    build_actions,
    build_base_topology,
    evaluate_thresholds,
    select_direction_pairs,
    topology_fingerprint,
)
from sim.conformance import load_harness, run_command, run_conformance, smoke_cases
from sim.medium_hub import Connection, MediumHub
from sim.node_manager import NodeManager
from sim.reporting import aggregate_samples, migrate_report_v3, percentile, relative_mad
from sim.resource_monitor import ResourceMonitor


class _Socket:
    def __init__(self):
        self.values = []

    def sendall(self, value):
        self.values.append(value)

    def close(self):
        pass


class LabTests(unittest.TestCase):
    def test_seeded_topology_is_identical_except_for_assignment(self):
        first = build_base_topology(20, 17)
        second = build_base_topology(20, 17)
        self.assertEqual(first, second)
        fingerprints = set()
        for composition in COMPOSITIONS:
            cell = assign_implementations(first, composition)
            self.assertEqual(topology_fingerprint(cell), topology_fingerprint(first))
            fingerprints.add(topology_fingerprint(cell, True))
        self.assertEqual(len(fingerprints), 3)

    def test_mixed_assignment_has_exact_node_and_relay_counts(self):
        topology = assign_implementations(build_base_topology(20, 8), "mixed")
        nodes = list(topology["nodes"].values())
        relays = [node for node in nodes if node.get("transport")]
        self.assertEqual(sum(node["implementation"] == "rust" for node in nodes), 10)
        self.assertEqual(sum(node["implementation"] == "python" for node in nodes), 10)
        self.assertEqual(sum(node["implementation"] == "rust" for node in relays), len(relays) // 2)

    def test_mixed_pairs_cover_all_directions_and_profile_contract(self):
        topology = assign_implementations(build_base_topology(8, 1), "mixed")
        pairs = select_direction_pairs(topology, "mixed")
        self.assertEqual(
            {(row["source_implementation"], row["destination_implementation"]) for row in pairs},
            {("python", "python"), ("python", "rust"),
             ("rust", "python"), ("rust", "rust")},
        )
        self.assertEqual(len({row["hops"] for row in pairs}), 1)
        actions, _ = build_actions(topology, "mixed", "smoke")
        direct = next(action for action in actions if action.get("case_id") == "direct-256")
        self.assertEqual((direct["warmups"], direct["samples"], direct["concurrency"]), (1, 3, 1))
        self.assertEqual(PROFILES["smoke"]["window"], 5)

    def test_medium_serializes_airtime_and_seeded_loss(self):
        def exercise(seed):
            hub = MediumHub(port=0, seed=seed)
            hub.set_medium_params("m", bitrate=800, loss=0.4)
            source = Connection(_Socket(), None)
            source.node, source.medium = "source", "m"
            target = Connection(_Socket(), None)
            target.node, target.medium = "target", "m"
            hub.members["m"] = {source, target}
            for _ in range(12):
                hub.forward(source, b"0123456789")
            return hub.snapshot()["m"], list(hub.schedule)

        left, scheduled_left = exercise(123)
        right, scheduled_right = exercise(123)
        self.assertEqual(left["stats"]["dropped"], right["stats"]["dropped"])
        self.assertAlmostEqual(left["stats"]["airtime_seconds"], 1.2)
        self.assertGreater(left["stats"]["queue_delay_seconds"], 0)
        delivery_times = [entry[0] for entry in scheduled_left]
        self.assertEqual(delivery_times, sorted(delivery_times))
        self.assertEqual(len(scheduled_left), len(scheduled_right))

    def test_correlation_rejects_stale_delivery(self):
        manager = NodeManager("127.0.0.1", 1, "")
        manager.record_delivery("n0", "reticulated:%s:old" % ("a" * 32))
        self.assertIsNone(manager.wait_for_delivery("n0", "b" * 32, 0.01))
        timer = threading.Timer(
            0.01,
            lambda: manager.record_delivery("n0", "reticulated:%s:new" % ("b" * 32)),
        )
        timer.start()
        self.assertEqual(manager.wait_for_delivery("n0", "b" * 32, 1)["transfer_id"], "b" * 32)
        timer.join()

    def test_statistics_and_schema_migration(self):
        samples = [
            {"delivered": True, "latency_seconds": value, "bytes": 100,
             "started_monotonic": index, "observed_monotonic": index + value}
            for index, value in enumerate((1.0, 2.0, 3.0, 4.0))
        ]
        aggregate = aggregate_samples(samples)
        self.assertEqual(percentile([1, 2, 3, 4], .95), 4)
        self.assertEqual(aggregate["p50_seconds"], 2.5)
        self.assertAlmostEqual(relative_mad([1, 2, 3]), 0.5)
        migrated = migrate_report_v3({"schema_version": 2, "results": [], "media": {}})
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["source_schema_version"], 2)
        self.assertFalse(migrated["resource_usage"]["available"])

    def test_resource_aggregation_uses_group_peak(self):
        monitor = ResourceMonitor(lambda: [])
        monitor.available = True
        monitor.metadata = {
            10: {"group": "python", "role": "rnsd"},
            11: {"group": "python", "role": "agent"},
        }
        monitor.first_cpu = {10: 1.0, 11: 2.0}
        monitor.last_cpu = {10: 2.5, 11: 2.5}
        monitor.peak_rss = {10: 100, 11: 200}
        monitor.group_peak_rss = {"python": 250}
        report = monitor.report()
        self.assertEqual(report["groups"]["python"]["peak_rss_bytes"], 250)
        self.assertEqual(report["groups"]["python"]["cpu_seconds"], 2.0)

    def test_thresholds_are_only_applied_to_compatible_baseline(self):
        row = {"composition": "mixed", "nodes": 8, "case_id": "direct",
               "source_implementation": "python", "destination_implementation": "rust",
               "delivery_rate": 1.0, "p50_seconds": 1.0, "goodput_bytes_per_second": 100.0}
        row["run"] = 1
        common = {"schema_version": 3, "report_type": "benchmark", "profile": "smoke",
                  "seed": 4, "profile_configuration": PROFILES["smoke"],
                  "references": {"actual": {"reticulum": "r", "lxmf": "l"}},
                  "environment": {"rust_binary_sha256": "binary"},
                  "cells": [{"composition": "mixed", "run": 1, "nodes": 8,
                             "topology_fingerprint": "topology"}]}
        current = {**common, "comparisons": [row]}
        baseline = {**common,
                    "comparisons": [{**row, "p50_seconds": .5, "goodput_bytes_per_second": 200.0}]}
        result = evaluate_thresholds(current, baseline, {
            "max_latency_regression_percent": 10,
            "max_goodput_regression_percent": 10,
            "min_delivery_rate": .99,
        })
        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "fail")
        self.assertGreaterEqual(len(result["failures"]), 2)

    def test_conformance_discovery_and_accounting_with_fake_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            harness_dir = os.path.join(directory, "tools", "scripts")
            os.makedirs(harness_dir)
            path = os.path.join(harness_dir, "python_compat_harness.py")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("SMOKE_SCRIPT_CASES={'direct_python_to_rust','direct_rust_to_python','resource_transfer'}\n")
                stream.write("LOCAL_CARGO_TEST_CASES={'policy_case':'test_name'}\n")
                stream.write("SUPPORTED_CASES=set(SMOKE_SCRIPT_CASES)|set(LOCAL_CARGO_TEST_CASES)\n")
            harness = load_harness(directory)
            self.assertEqual(len(harness["supported"]), 4)
            self.assertEqual(set(smoke_cases(harness)), set(harness["live"]))

    def test_conformance_timeout_retains_diagnostic_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = os.path.join(directory, "timeout.log")
            result = run_command(
                "timeout_case",
                [sys.executable, "-c", "import time; time.sleep(1)"],
                directory,
                dict(os.environ),
                log,
                0.01,
            )
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["exit_code"], 124)
            self.assertIn("timed out", result["error"])
            self.assertEqual(result["log"], os.path.abspath(log))
            self.assertTrue(os.path.isfile(log))

    @mock.patch("sim.conformance.capture_environment", return_value={})
    @mock.patch("sim.conformance.run_command")
    @mock.patch("sim.conformance.load_reference_lock")
    @mock.patch("sim.conformance.load_harness")
    @mock.patch("sim.conformance.git_revision", return_value="same")
    def test_smoke_records_unselected_authoritative_cases_as_skips(
            self, _revision, harness, reference_lock, run, _environment):
        harness.return_value = {
            "path": "/tmp/harness.py",
            "live": ["direct_python_to_rust"],
            "local": ["policy_case"],
            "supported": ["direct_python_to_rust", "policy_case"],
        }
        reference_lock.return_value = {
            "reticulum": "same", "lxmf": "same", "source": "lock",
        }
        run.return_value = {
            "case_id": "direct_python_to_rust", "status": "pass", "command": [],
            "cwd": "/tmp", "exit_code": 0, "duration_seconds": 0.01,
            "log": "/tmp/direct.log", "error": None,
        }
        with tempfile.TemporaryDirectory() as output:
            report = run_conformance("/tmp/LXMF-rs", "smoke", output)
            skipped = next(case for case in report["cases"] if case["status"] == "skip")
            self.assertTrue(os.path.isfile(skipped["log"]))
        self.assertTrue(report["conforming"])
        self.assertEqual(report["summary"]["conformance"]["selected"], 1)
        self.assertEqual(report["summary"]["conformance"]["profile_skipped"], 1)

    @mock.patch("sim.conformance.capture_environment", return_value={})
    @mock.patch("sim.conformance.load_reference_lock")
    @mock.patch("sim.conformance.load_harness")
    @mock.patch("sim.conformance.git_revision")
    def test_conformance_rejects_mismatched_references(
            self, revision, harness, reference_lock, _environment):
        harness.return_value = {"path": "/tmp/harness.py", "live": [], "local": [], "supported": []}
        reference_lock.return_value = {"reticulum": "expected-rns", "lxmf": "expected-lxmf", "source": "lock"}
        revision.side_effect = lambda path: "lxmf-rs" if path.endswith("LXMF-rs") else "wrong"
        with tempfile.TemporaryDirectory() as output:
            report = run_conformance("/tmp/LXMF-rs", "smoke", output)
        self.assertEqual(report["status"], "fail")
        self.assertIn("pinned Python reference mismatch", report["error"])


if __name__ == "__main__":
    unittest.main()
