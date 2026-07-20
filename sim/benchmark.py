"""Matched Python-only, mixed, and Rust-only network benchmark orchestration."""

from copy import deepcopy
import hashlib
import json
import os
import random
import time

from . import config
from .conformance import load_reference_lock
from .environment import capture_environment, git_revision
from .reporting import aggregate_samples, utc_timestamp
from .scenario import _generated_topology, execute_scenario


PROFILES = {
    "smoke": {"sizes": [8], "runs": 1, "warmups": 1, "samples": 3,
              "window": 5, "concurrency": 1},
    "standard": {"sizes": [20], "runs": 5, "warmups": 1, "samples": 10,
                 "window": 20, "concurrency": 4},
    "full": {"sizes": [20, 50, 100], "runs": 10, "warmups": 2, "samples": 20,
             "window": 60, "concurrency": 8},
}
COMPOSITIONS = ("python", "mixed", "rust")


def topology_fingerprint(topology, include_implementations=False):
    value = deepcopy(topology)
    if not include_implementations:
        for node in value.get("nodes", {}).values():
            node.pop("implementation", None)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_base_topology(nodes, seed):
    transport_nodes = max(2, min(10, nodes // 4))
    rng = random.Random(seed)
    topology = _generated_topology({
        "nodes": nodes,
        "rust_nodes": 0,
        "transport_nodes": transport_nodes,
        "mtu": 500,
        "bitrate": 1_000_000,
        "propagation": round(rng.uniform(0.002, 0.008), 6),
        "loss": 0.0,
    })
    # Keep a transport relay plus a fixed endpoint set propagation-capable in
    # every composition; only implementation assignment changes between cells.
    transports = [node_id for node_id, node in topology["nodes"].items()
                  if node.get("transport")]
    topology["nodes"][transports[len(transports) // 2]]["propagation_node"] = True
    endpoints = [node_id for node_id in topology["nodes"] if node_id not in transports]
    for node_id in endpoints[:min(4, len(endpoints))]:
        topology["nodes"][node_id]["propagation_node"] = True
    return topology


def assign_implementations(base, composition):
    topology = deepcopy(base)
    ids = list(topology["nodes"])
    if composition in ("python", "rust"):
        for node in topology["nodes"].values():
            node["implementation"] = composition
        return topology

    transports = [node_id for node_id in ids if topology["nodes"][node_id].get("transport")]
    endpoints = [node_id for node_id in ids if node_id not in transports]
    total_rust = len(ids) // 2
    transport_rust = len(transports) // 2
    for group, rust_count in ((transports, transport_rust),
                              (endpoints, total_rust - transport_rust)):
        for index, node_id in enumerate(group):
            rust_slot = index % 4 in (0, 1) if group is endpoints else index % 2 == 0
            topology["nodes"][node_id]["implementation"] = (
                "rust" if rust_slot and rust_count > 0 else "python"
            )
        current = [node_id for node_id in group
                   if topology["nodes"][node_id]["implementation"] == "rust"]
        for node_id in current[rust_count:]:
            topology["nodes"][node_id]["implementation"] = "python"
        if len(current) < rust_count:
            candidates = [node_id for node_id in reversed(group)
                          if topology["nodes"][node_id]["implementation"] == "python"]
            for node_id in candidates[:rust_count - len(current)]:
                topology["nodes"][node_id]["implementation"] = "rust"
    return topology


def _adjacency(topology):
    result = {node_id: set() for node_id in topology["nodes"]}
    for link in topology["links"].values():
        for left in link["members"]:
            result[left].update(right for right in link["members"] if right != left)
    return result


def _hop_count(topology, source, destination):
    adjacency = _adjacency(topology)
    queue = [(source, 0)]
    seen = {source}
    while queue:
        node, hops = queue.pop(0)
        if node == destination:
            return hops
        for neighbor in sorted(adjacency[node]):
            if neighbor in seen:
                continue
            if neighbor != destination and not topology["nodes"][neighbor].get("transport"):
                continue
            seen.add(neighbor)
            queue.append((neighbor, hops + 1))
    return None


def select_direction_pairs(topology, composition):
    endpoints = [node_id for node_id, node in topology["nodes"].items()
                 if not node.get("transport")]
    directions = (("python", "python"), ("python", "rust"),
                  ("rust", "python"), ("rust", "rust")) if composition == "mixed" else (
                      (composition, composition),)
    candidates_by_direction = []
    for source_impl, destination_impl in directions:
        candidates = []
        for source in endpoints:
            if topology["nodes"][source]["implementation"] != source_impl:
                continue
            for destination in endpoints:
                if source == destination or topology["nodes"][destination]["implementation"] != destination_impl:
                    continue
                if not topology["nodes"][destination].get("propagation_node"):
                    continue
                hops = _hop_count(topology, source, destination)
                if hops is not None:
                    candidates.append((hops, source, destination))
        if not candidates:
            raise RuntimeError("no endpoint pair for %s-to-%s" % (source_impl, destination_impl))
        candidates_by_direction.append((source_impl, destination_impl, candidates))
    common_hops = set.intersection(*(
        {row[0] for row in candidates} for _, _, candidates in candidates_by_direction
    ))
    if not common_hops:
        raise RuntimeError("no common hop count across implementation directions")
    matched_hops = min(common_hops)
    selected = []
    for source_impl, destination_impl, candidates in candidates_by_direction:
        hops, source, destination = sorted(
            (row for row in candidates if row[0] == matched_hops),
            key=lambda row: (row[1], row[2]),
        )[0]
        selected.append({"source": source, "destination": destination,
                         "source_implementation": source_impl,
                         "destination_implementation": destination_impl, "hops": hops})
    return selected


def build_actions(topology, composition, profile):
    settings = PROFILES[profile]
    pairs = select_direction_pairs(topology, composition)
    actions = [{"action": "wait_ready", "timeout": 180}]
    latency_cases = (
        ("cold-discovery", "cold_path", 256),
        ("direct-256", "direct", 256),
        ("opportunistic-256", "opportunistic", 256),
        ("propagated-256", "propagated", 256),
        ("resource-16k", "direct", 16 * 1024),
    )
    for pair in pairs:
        for case_id, mode, size in latency_cases:
            actions.append({
                "action": mode,
                "case_id": case_id,
                "src": pair["source"],
                "dst": pair["destination"],
                "size": size,
                "kind": "resource" if size >= 16 * 1024 else "packet",
                "mode": "cold_path" if mode == "cold_path" else mode,
                "warmups": settings["warmups"],
                "samples": settings["samples"],
                "concurrency": settings["concurrency"],
                "timeout": 120,
                "discovery_timeout": 15,
                "attempts": 1 if mode == "cold_path" else 3,
                "settle": 0.25,
            })
    representative = pairs[0]
    for case_id, size, kind in (("sustained-direct", 256, "packet"),
                                ("sustained-resource", 16 * 1024, "resource")):
        actions.append({
            "action": "load", "case_id": case_id,
            "src": representative["source"], "dst": representative["destination"],
            "size": size, "kind": kind, "mode": "direct",
            "duration": settings["window"], "warmups": settings["warmups"],
            "concurrency": settings["concurrency"], "timeout": 120, "settle": 0.05,
        })
    return actions, pairs


def _comparison_rows(cells):
    rows = []
    for cell in cells:
        for result in cell.get("results", []):
            if not result.get("transfer"):
                continue
            performance = result.get("performance") or aggregate_samples(result.get("raw_samples") or [])
            rows.append({
                "composition": cell["composition"], "run": cell["run"],
                "nodes": cell["nodes"], "case_id": result.get("case_id") or result.get("action"),
                "mode": result.get("mode"), "source_implementation": result.get("source_implementation"),
                "destination_implementation": result.get("destination_implementation"),
                "hops": result.get("hops"), "status": result.get("status"),
                "retry_count": sum(max(0, int(sample.get("attempts", 1)) - 1)
                                   for sample in result.get("raw_samples", [])),
                **performance,
            })
    return rows


def aggregate_resources(cells):
    groups = {}
    available = False
    reasons = []
    shared_attribution = []
    for cell in cells:
        usage = cell.get("resource_usage") or {}
        available = available or bool(usage.get("available"))
        if not usage.get("available") and usage.get("reason"):
            reasons.append(usage["reason"])
        if usage.get("shared_process_attribution"):
            shared_attribution.append({
                "composition": cell.get("composition"),
                "run": cell.get("run"),
                **usage["shared_process_attribution"],
            })
        for name, value in (usage.get("groups") or {}).items():
            row = groups.setdefault(name, {"processes": 0, "cpu_seconds": 0.0,
                                           "peak_rss_bytes": 0})
            row["processes"] = max(row["processes"], int(value.get("processes", 0)))
            row["cpu_seconds"] += float(value.get("cpu_seconds", 0.0))
            row["peak_rss_bytes"] = max(row["peak_rss_bytes"],
                                         int(value.get("peak_rss_bytes", 0)))
    for row in groups.values():
        row["cpu_seconds"] = round(row["cpu_seconds"], 6)
    return {"available": available, "groups": groups,
            "reason": "; ".join(sorted(set(reasons))) if reasons and not available else None,
            "shared_process_attribution": shared_attribution}


def _load_thresholds(path):
    if not path:
        return None
    with open(path, "rb") as stream:
        raw = stream.read()
    if path.endswith(".json"):
        return json.loads(raw.decode("utf-8"))
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib
    return tomllib.loads(raw.decode("utf-8"))


def evaluate_thresholds(report, baseline, thresholds):
    failures = []
    def topology_signature(value):
        return sorted(
            (cell.get("composition"), cell.get("run"), cell.get("nodes"),
             cell.get("topology_fingerprint"))
            for cell in value.get("cells", [])
        )

    compatible = (
        baseline.get("schema_version") == 3
        and baseline.get("report_type") == "benchmark"
        and baseline.get("profile") == report.get("profile")
        and baseline.get("seed") == report.get("seed")
        and baseline.get("profile_configuration") == report.get("profile_configuration")
        and baseline.get("references", {}).get("actual")
        == report.get("references", {}).get("actual")
        and baseline.get("environment", {}).get("rust_binary_sha256")
        == report.get("environment", {}).get("rust_binary_sha256")
        and topology_signature(baseline) == topology_signature(report)
    )
    if not compatible:
        return {"compatible": False, "applied": False,
                "failures": ["baseline schema/profile/seed/configuration is incompatible"]}
    current = {(row["composition"], row["run"], row["nodes"], row["case_id"],
                row["source_implementation"], row["destination_implementation"]): row
               for row in report.get("comparisons", [])}
    previous = {(row["composition"], row["run"], row["nodes"], row["case_id"],
                 row["source_implementation"], row["destination_implementation"]): row
                for row in baseline.get("comparisons", [])}
    max_latency = float(thresholds.get("max_latency_regression_percent", float("inf")))
    max_goodput = float(thresholds.get("max_goodput_regression_percent", float("inf")))
    min_delivery = float(thresholds.get("min_delivery_rate", 0.0))
    deltas = []
    for key in sorted(current.keys() & previous.keys()):
        now, old = current[key], previous[key]
        if float(now.get("delivery_rate", 0)) < min_delivery:
            failures.append("%s delivery rate %.4f below %.4f" % (key, now["delivery_rate"], min_delivery))
        if now.get("p50_seconds") is not None and old.get("p50_seconds"):
            regression = 100 * (now["p50_seconds"] / old["p50_seconds"] - 1)
            deltas.append({"key": list(key), "metric": "p50_seconds",
                           "baseline": old["p50_seconds"], "current": now["p50_seconds"],
                           "delta_percent": regression})
            if regression > max_latency:
                failures.append("%s p50 latency regressed %.2f%%" % (key, regression))
        if now.get("goodput_bytes_per_second") is not None and old.get("goodput_bytes_per_second"):
            regression = 100 * (1 - now["goodput_bytes_per_second"] / old["goodput_bytes_per_second"])
            deltas.append({"key": list(key), "metric": "goodput_bytes_per_second",
                           "baseline": old["goodput_bytes_per_second"],
                           "current": now["goodput_bytes_per_second"],
                           "regression_percent": regression})
            if regression > max_goodput:
                failures.append("%s goodput regressed %.2f%%" % (key, regression))
    for group, current_group in (report.get("resource_usage", {}).get("groups") or {}).items():
        baseline_group = (baseline.get("resource_usage", {}).get("groups") or {}).get(group)
        if not baseline_group:
            continue
        for metric, threshold_name in (("cpu_seconds", "max_cpu_regression_percent"),
                                       ("peak_rss_bytes", "max_rss_regression_percent")):
            old = float(baseline_group.get(metric, 0))
            now = float(current_group.get(metric, 0))
            if old <= 0:
                continue
            regression = 100 * (now / old - 1)
            deltas.append({"key": [group], "metric": metric, "baseline": old,
                           "current": now, "delta_percent": regression})
            if regression > float(thresholds.get(threshold_name, float("inf"))):
                failures.append("%s %s regressed %.2f%%" % (group, metric, regression))
    return {"compatible": True, "applied": True, "failures": failures,
            "status": "fail" if failures else "pass", "thresholds": thresholds,
            "deltas": deltas}


def run_benchmark(profile, seed, output_dir, lxmf_rs_root, baseline_path=None,
                  thresholds_path=None):
    settings = PROFILES[profile]
    root = os.path.abspath(lxmf_rs_root)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    expected = load_reference_lock(root)
    rns_repo = os.path.join(root, "target", "python-references", "Reticulum")
    lxmf_repo = os.path.join(root, "target", "python-references", "LXMF")
    actual = {"reticulum": git_revision(rns_repo), "lxmf": git_revision(lxmf_repo)}
    mismatches = {key: {"expected": expected[key], "actual": actual[key]}
                  for key in expected if key != "source" and expected[key] != actual[key]}
    started = time.time()
    started_at = utc_timestamp()
    cells = []
    try:
        if mismatches:
            raise RuntimeError("pinned Python reference mismatch: " + json.dumps(mismatches, sort_keys=True))
        config.PYTHON_RNS_REPO = rns_repo
        config.PYTHON_LXMF_REPO = lxmf_repo
        rust_binary = os.path.join(root, "target", "release", "reticulumd")
        if not os.path.isfile(rust_binary):
            raise RuntimeError("missing Rust daemon: " + rust_binary)
        config.RUST_RETICULUMD = rust_binary
        for size_index, nodes in enumerate(settings["sizes"]):
            for run_index in range(settings["runs"]):
                run_seed = int(seed) + size_index * 100_000 + run_index
                base = build_base_topology(nodes, run_seed)
                order = list(COMPOSITIONS)
                shift = run_index % len(order)
                order = order[shift:] + order[:shift]
                for order_index, composition in enumerate(order):
                    topology = assign_implementations(base, composition)
                    actions, pairs = build_actions(topology, composition, profile)
                    scenario = {
                        "name": "%s-%d-nodes-run-%d" % (composition, nodes, run_index + 1),
                        "seed": run_seed,
                        "topology": topology,
                        "actions": actions,
                        "transfer_timeout": 120,
                    }
                    cell = execute_scenario(scenario, lxmf_rs_root=root)
                    cell.update({
                        "composition": composition, "run": run_index + 1, "nodes": nodes,
                        "execution_order": order_index + 1, "topology_fingerprint": topology_fingerprint(base),
                        "implementation_fingerprint": topology_fingerprint(topology, True),
                        "direction_pairs": pairs,
                    })
                    cells.append(cell)
        comparisons = _comparison_rows(cells)
        resource_usage = aggregate_resources(cells)
        correctness_failures = [cell["scenario"] for cell in cells if cell["status"] != "pass"]
        report = {
            "schema_version": 3, "report_type": "benchmark", "scenario": "network-benchmark-" + profile,
            "profile": profile, "seed": int(seed), "topology_seed": int(seed),
            "nodes": max(settings["sizes"]),
            "status": "fail" if correctness_failures else "pass",
            "started_at": started_at, "generated_at": utc_timestamp(),
            "duration_seconds": round(time.time() - started, 3), "profile_configuration": settings,
            "cells": cells, "comparisons": comparisons,
            "raw_samples": [sample for cell in cells for sample in cell.get("raw_samples", [])],
            "correctness_failures": correctness_failures,
            "resource_usage": resource_usage,
            "implementation_roles": [cell.get("implementation_roles") for cell in cells],
            "references": {"expected": expected, "actual": actual, "mismatches": mismatches},
            "environment": capture_environment(root),
        }
        if baseline_path and thresholds_path:
            with open(baseline_path, encoding="utf-8") as stream:
                baseline = json.load(stream)
            report["baseline"] = evaluate_thresholds(report, baseline, _load_thresholds(thresholds_path))
            if report["baseline"].get("status") == "fail":
                report["status"] = "fail"
        else:
            report["baseline"] = {"applied": False, "informational": True}
        return report
    except Exception as error:
        return {
            "schema_version": 3, "report_type": "benchmark", "scenario": "network-benchmark-" + profile,
            "profile": profile, "seed": int(seed), "topology_seed": int(seed), "status": "fail",
            "error": str(error), "started_at": started_at, "generated_at": utc_timestamp(),
            "duration_seconds": round(time.time() - started, 3), "cells": cells,
            "comparisons": _comparison_rows(cells), "raw_samples": [],
            "resource_usage": aggregate_resources(cells),
            "references": {"expected": expected, "actual": actual, "mismatches": mismatches},
            "environment": capture_environment(root),
        }
