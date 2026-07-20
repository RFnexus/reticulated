"""Scenario report summaries shared by the CLI and HTML dashboard."""

from collections import Counter
from datetime import datetime, timezone
import re
import statistics


FAULT_ACTIONS = {
    "restart", "shutdown", "start", "disconnect", "reconnect", "partition", "recover",
}
_DELIVERED_RTT = re.compile(r"\bDELIVERED\b.*?\brtt=([0-9]+(?:\.[0-9]+)?)\b")


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def measured_rtt(result):
    """Return a measured sender delivery RTT, excluding modeled EXPECT values."""
    for key in ("measured_latency_seconds", "measured_rtt_seconds"):
        value = result.get(key)
        if value is not None:
            return float(value)
    values = list(result.get("delivery_evidence") or [])
    values.extend(
        event.get("status", "")
        for event in result.get("events") or []
        if isinstance(event, dict)
    )
    for value in reversed(values):
        match = _DELIVERED_RTT.search(str(value))
        if match:
            return float(match.group(1))
    return None


def percentile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    return values[round((len(values) - 1) * float(fraction))]


def relative_mad(values):
    values = [float(value) for value in values]
    if not values:
        return None
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return mad / median if median else 0.0


def aggregate_samples(samples):
    measured = [
        sample for sample in samples
        if sample.get("delivered") and sample.get("latency_seconds") is not None
    ]
    latencies = [float(sample["latency_seconds"]) for sample in measured]
    delivered_bytes = sum(int(sample.get("bytes", 0)) for sample in measured)
    starts = [float(sample["started_monotonic"]) for sample in measured
              if sample.get("started_monotonic") is not None]
    finishes = [float(sample["observed_monotonic"]) for sample in measured
                if sample.get("observed_monotonic") is not None]
    window = max(finishes) - min(starts) if starts and finishes else None
    total = len(samples)
    return {
        "sample_count": total,
        "delivered_count": len(measured),
        "delivery_rate": len(measured) / total if total else 0.0,
        "p50_seconds": statistics.median(latencies) if latencies else None,
        "p95_seconds": percentile(latencies, 0.95),
        "p99_seconds": percentile(latencies, 0.99),
        "relative_mad": relative_mad(latencies),
        "delivered_bytes": delivered_bytes,
        "measurement_window_seconds": window,
        "goodput_bytes_per_second": delivered_bytes / window if window and window > 0 else None,
    }


def migrate_report_v3(report):
    """Return a dashboard-safe schema-v3 view of a legacy scenario report."""
    migrated = dict(report)
    version = int(migrated.get("schema_version", 1))
    if version >= 3:
        return migrated
    migrated["source_schema_version"] = version
    migrated["schema_version"] = 3
    migrated.setdefault("report_type", "scenario")
    migrated.setdefault("raw_samples", [
        sample for result in migrated.get("results", [])
        for sample in result.get("raw_samples", [])
    ])
    migrated.setdefault("resource_usage", {
        "available": False,
        "reason": "not recorded by schema v%d" % version,
        "groups": {},
    })
    migrated.setdefault("topology_seed", None)
    migrated.setdefault("summary", summarize_report(migrated))
    return migrated


def topology_summary(topology, media=None):
    nodes = topology.get("nodes", {})
    implementation_counts = Counter(
        node.get("implementation", "python") for node in nodes.values()
    )
    transport_counts = Counter(
        node.get("implementation", "python")
        for node in nodes.values()
        if node.get("transport")
    )
    profiles = Counter()
    for medium in (media or {}).values():
        params = medium.get("params", {})
        profile = (
            int(params.get("bitrate", 0)),
            int(params.get("mtu", 0)),
            float(params.get("propagation", 0)),
            float(params.get("loss", 0)),
        )
        profiles[profile] += 1
    medium_profiles = [
        {
            "count": count,
            "bitrate": profile[0],
            "mtu": profile[1],
            "propagation": profile[2],
            "loss": profile[3],
        }
        for profile, count in sorted(profiles.items())
    ]
    return {
        "implementation_counts": dict(sorted(implementation_counts.items())),
        "transport_counts": dict(sorted(transport_counts.items())),
        "transport_nodes": sum(transport_counts.values()),
        "media_count": len((media or topology.get("links") or {})),
        "medium_profiles": medium_profiles,
    }


def summarize_report(report):
    results = report.get("results") or []
    transfers = [result for result in results if result.get("transfer")]
    faults = [result for result in results if result.get("action") in FAULT_ACTIONS]
    passed_actions = sum(result.get("status") == "pass" for result in results)
    passed_transfers = sum(result.get("status") == "pass" for result in transfers)
    passed_faults = sum(result.get("status") == "pass" for result in faults)
    medium_stats = Counter()
    for medium in (report.get("media") or {}).values():
        medium_stats.update(medium.get("stats") or {})
    measured = [measured_rtt(result) for result in transfers]
    measured = [value for value in measured if value is not None]
    return {
        "actions": {
            "total": len(results),
            "passed": passed_actions,
            "failed": len(results) - passed_actions,
            "success_percent": round(100 * passed_actions / len(results), 1) if results else 0,
        },
        "transfers": {
            "total": len(transfers),
            "passed": passed_transfers,
            "failed": len(transfers) - passed_transfers,
            "success_percent": round(100 * passed_transfers / len(transfers), 1) if transfers else 0,
            "first_attempt_passed": sum(
                result.get("status") == "pass" and result.get("attempts") == 1
                for result in transfers
            ),
            "measured_rtt_count": len(measured),
            "measured_rtt_min_seconds": min(measured) if measured else None,
            "measured_rtt_max_seconds": max(measured) if measured else None,
        },
        "faults": {
            "total": len(faults),
            "passed": passed_faults,
            "failed": len(faults) - passed_faults,
            "success_percent": round(100 * passed_faults / len(faults), 1) if faults else 0,
        },
        "max_hops": max((int(result.get("hops", 0)) for result in transfers), default=0),
        "media": {
            "count": len(report.get("media") or {}),
            "tx": medium_stats["tx"],
            "rx": medium_stats["rx"],
            "dropped": medium_stats["dropped"],
            "oversize": medium_stats["oversize"],
            "airtime_seconds": round(float(medium_stats["airtime_seconds"]), 6),
            "queue_delay_seconds": round(float(medium_stats["queue_delay_seconds"]), 6),
        },
    }
