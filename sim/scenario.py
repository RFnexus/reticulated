"""Headless mixed-network scenario runner."""

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import uuid

from .reporting import aggregate_samples, summarize_report, topology_summary, utc_timestamp


def _configure_runtime(data_dir, port_base, seed=0):
    from . import config

    config.DATA_DIR = data_dir
    config.NODES_DIR = os.path.join(data_dir, "nodes")
    config.TOPOLOGY_FILE = os.path.join(data_dir, "topology.json")
    config.HUB_HOST = "127.0.0.1"
    config.HUB_PORT = port_base
    config.RUST_BRIDGE_BASE = port_base + 100
    config.RUST_RPC_BASE = port_base + 1800
    config.RUST_RPC_HOST = "127.0.0.1"
    config.TCP_BASE = port_base + 2000
    config.RANDOM_SEED = int(seed)
    config.ensure_dirs()


def _port_available(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_port_base(node_count):
    span = 2200 + node_count
    for base in range(28000, 62000 - span, 2300):
        probes = (base, base + 100, base + 1800, base + 2000, base + span - 1)
        if all(_port_available(port) for port in probes):
            return base
    raise RuntimeError("no free local port range for scenario")


def _generated_topology(spec):
    from .topology import Topology

    count = max(2, int(spec.get("nodes", 10)))
    rust_nodes = spec.get("rust_nodes")
    if rust_nodes is None:
        rust_nodes = round(count * float(spec.get("rust_ratio", 0.5)))
    rust_nodes = max(0, min(count, int(rust_nodes)))
    transport_count = max(1, min(count - 1, int(spec.get("transport_nodes", min(10, count - 1)))))
    mtu = int(spec.get("mtu", 500))
    bitrate = int(spec.get("bitrate", 1_000_000))
    propagation = float(spec.get("propagation", 0.0))
    loss = float(spec.get("loss", 0.0))

    topology = Topology()
    rust_indexes = list(range(0, count, 2)) + list(range(1, count, 2))
    rust_indexes = set(rust_indexes[:rust_nodes])
    for index in range(count):
        implementation = "rust" if index in rust_indexes else "python"
        topology.add_node("%s-%d" % (implementation, index), implementation=implementation)
    ids = list(topology.nodes)
    transports = ids[:transport_count]
    for node_id in transports:
        topology.set_node_transport(node_id, True)
        topology.set_node_mode(node_id, "gateway")
    for index in range(1, len(transports)):
        topology.add_link([transports[index - 1], transports[index]], mtu, bitrate,
                          loss, propagation, name="backbone-%d" % index)
    leaves = ids[transport_count:]
    for index, node_id in enumerate(leaves):
        parent = transports[index % len(transports)]
        topology.add_link([node_id, parent], mtu, bitrate, loss, propagation,
                          name="access-%d" % index)
    return topology.to_dict()


def _node_count(scenario):
    if "generate" in scenario:
        return int(scenario["generate"].get("nodes", 10))
    return len(scenario.get("topology", {}).get("nodes", {}))


def _find_value(value, key, expected):
    if isinstance(value, dict):
        if value.get(key) == expected:
            return True
        return any(_find_value(item, key, expected) for item in value.values())
    if isinstance(value, list):
        return any(_find_value(item, key, expected) for item in value)
    return False


class ScenarioRunner:
    def __init__(self, simulator, scenario):
        self.sim = simulator
        self.scenario = scenario
        self.results = []
        self.started = time.monotonic()

    def _record(self, action, passed, **details):
        result = {"action": action.get("action"), "status": "pass" if passed else "fail"}
        result.update(details)
        self.results.append(result)
        return passed

    def _wait_ready(self, timeout):
        deadline = time.time() + timeout
        expected = set(self.sim.topology.nodes)
        while time.time() < deadline:
            running = {node for node, state in self.sim.node_manager.states().items() if state}
            connected = {node for medium in self.sim.hub.snapshot().values()
                         for node in medium.get("members", [])}
            addressed = {node for node, address in self.sim.node_manager.lxmf_addresses.items()
                         if address}
            propagation_expected = {
                node for node, spec in self.sim.topology.nodes.items()
                if spec.get("propagation_node")
            }
            propagation_ready = set(self.sim.node_manager.propagation_addresses)
            if (expected <= running and expected <= connected and expected <= addressed
                    and propagation_expected <= propagation_ready):
                return True, {"running": len(running), "connected": len(connected),
                              "addressed": len(addressed),
                              "propagation_ready": len(propagation_ready)}
            time.sleep(0.25)
        return False, {"running": sum(self.sim.node_manager.states().values()),
                       "connected": len({node for medium in self.sim.hub.snapshot().values()
                                         for node in medium.get("members", [])}),
                       "addressed": len(self.sim.node_manager.lxmf_addresses)}

    @staticmethod
    def _mode(action):
        name = action.get("action", "transfer")
        mode = action.get("mode")
        if mode:
            return str(mode)
        if name in ("direct", "opportunistic", "propagated"):
            return name
        return "direct"

    @staticmethod
    def _payload(action, transfer_id):
        size = int(action.get("size", 64))
        marker = "reticulated:%s:" % transfer_id
        content = marker + str(action.get("content") or "")
        encoded_size = len(content.encode("utf-8"))
        if encoded_size < size:
            content += "x" * (size - encoded_size)
        return content

    def _prepare_transfer(self, action):
        src = action["src"]
        dst = action["dst"]
        mode = self._mode(action)
        if mode == "cold_path":
            destination = self.sim.node_manager.lxmf_addresses.get(dst)
            if destination:
                self.sim.drop_path(src, destination)
        else:
            if mode == "propagated":
                if not self.sim.announce_propagation(dst):
                    raise RuntimeError("cannot announce propagation node %s" % dst)
                if not self.sim.configure_outbound_propagation(src, dst):
                    raise RuntimeError("no ready propagation node for %s" % src)
                if not self.sim.configure_outbound_propagation(dst, dst):
                    raise RuntimeError("no ready propagation node for %s" % dst)
            self.sim.announce_lxmf(dst)
            self.sim.announce_lxmf(src)
        return src, dst, mode

    def _wait_for_sample(self, destination, transfer_id, timeout, mode):
        if mode != "propagated":
            return self.sim.node_manager.wait_for_delivery(destination, transfer_id, timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            record = self.sim.node_manager.wait_for_delivery(
                destination, transfer_id, min(2.0, remaining))
            if record is not None:
                return record
            self.sim.sync_propagation(destination, destination)
        return None

    def _request_cold_path(self, source, destination, timeout):
        address = self.sim.node_manager.lxmf_addresses.get(destination)
        if not address:
            return False
        # Start discovery inside the measured interval. A destination announce is
        # the common stimulus both implementations can observe; request_path then
        # waits for the resulting route instead of relying on a pre-warmed cache.
        self.sim.announce_lxmf(destination)
        return self.sim.request_path(source, address, timeout=min(30.0, timeout))

    def _send_sample(self, action, sample_index, warmup=False, prepared=None):
        src, dst, mode = prepared or self._prepare_transfer(action)
        max_attempts = max(1, int(action.get("attempts", 3)))
        timeout = float(action.get("timeout", self.scenario.get("transfer_timeout", 90)))
        attempt_timeout = max(5.0, timeout / max_attempts)
        started = False
        record = None
        started_at = None
        transfer_id = None
        discovery_found = None
        for attempts in range(1, max_attempts + 1):
            transfer_id = uuid.uuid4().hex
            content = self._payload(action, transfer_id)
            time.sleep(float(action.get("settle", 1.0)) if attempts == 1 else 0.1)
            started_at = time.monotonic()
            if mode == "cold_path":
                discovery_found = self._request_cold_path(
                    src, dst, float(action.get("discovery_timeout", min(15.0, attempt_timeout))))
            if mode != "cold_path" or discovery_found:
                started = self.sim.send_message(
                    src,
                    dst,
                    content,
                    mode="direct" if mode == "cold_path" else mode,
                    transfer_id=transfer_id,
                ) or started
            if started:
                record = self._wait_for_sample(dst, transfer_id, attempt_timeout, mode)
            if record is not None:
                break
        observed = record.get("observed_monotonic") if record else None
        return {
            "sample_index": sample_index,
            "warmup": bool(warmup),
            "transfer_id": transfer_id,
            "started": started,
            "delivered": record is not None,
            "attempts": attempts,
            "bytes": len(content.encode("utf-8")),
            "started_monotonic": started_at,
            "observed_monotonic": observed,
            "latency_seconds": observed - started_at if observed is not None else None,
            "receiver_event": record,
            "path_discovered": discovery_found,
        }

    def _send_batch(self, action, first_index, count, warmup=False, prepared=None):
        src, dst, mode = prepared or self._prepare_transfer(action)
        timeout = float(action.get("timeout", self.scenario.get("transfer_timeout", 90)))
        time.sleep(float(action.get("settle", 1.0)))
        samples = []
        for offset in range(count):
            transfer_id = uuid.uuid4().hex
            content = self._payload(action, transfer_id)
            started_at = time.monotonic()
            if mode == "cold_path":
                self._request_cold_path(src, dst, timeout)
            started = self.sim.send_message(
                src,
                dst,
                content,
                mode="direct" if mode == "cold_path" else mode,
                transfer_id=transfer_id,
            )
            samples.append({
                "sample_index": first_index + offset,
                "warmup": bool(warmup),
                "transfer_id": transfer_id,
                "started": started,
                "delivered": False,
                "attempts": 1,
                "bytes": len(content.encode("utf-8")),
                "started_monotonic": started_at,
                "observed_monotonic": None,
                "latency_seconds": None,
                "receiver_event": None,
            })
        deadline = time.monotonic() + timeout
        for sample in samples:
            remaining = max(0.0, deadline - time.monotonic())
            if not sample["started"] or remaining <= 0:
                continue
            record = self._wait_for_sample(
                dst, sample["transfer_id"], remaining, mode)
            if record is None:
                continue
            sample["delivered"] = True
            sample["observed_monotonic"] = record["observed_monotonic"]
            sample["latency_seconds"] = (
                record["observed_monotonic"] - sample["started_monotonic"]
            )
            sample["receiver_event"] = record
        return samples

    def _transfer(self, action):
        src = action["src"]
        dst = action["dst"]
        warmups = max(0, int(action.get("warmups", 0)))
        sample_count = max(1, int(action.get("samples", 1)))
        concurrency = max(1, int(action.get("concurrency", 1)))
        prepared = None if self._mode(action) == "cold_path" else self._prepare_transfer(action)
        warmup_samples = []
        for index in range(warmups):
            warmup_samples.append(self._send_sample(
                action, index + 1, warmup=True, prepared=prepared))
        samples = []
        while len(samples) < sample_count:
            chunk = min(concurrency, sample_count - len(samples))
            if chunk == 1:
                samples.append(self._send_sample(
                    action, len(samples) + 1, prepared=prepared))
            else:
                samples.extend(self._send_batch(
                    action, len(samples) + 1, chunk, prepared=prepared))
        aggregate = aggregate_samples(samples)
        trace = self.sim.trace_route(src, dst)
        source_impl = self.sim.topology.nodes[src].get("implementation", "python")
        destination_impl = self.sim.topology.nodes[dst].get("implementation", "python")
        single = samples[-1]
        details = {
            "case_id": action.get("case_id"),
            "source": src,
            "destination": dst,
            "source_implementation": source_impl,
            "destination_implementation": destination_impl,
            "path": trace.get("path"),
            "hops": max(0, len(trace.get("path") or []) - 1),
            "transfer": action.get("kind", action.get("action", "transfer")),
            "mode": self._mode(action),
            "bytes": int(action.get("size", 64)),
            "started": all(sample["started"] for sample in samples),
            "delivered": aggregate["delivered_count"] == len(samples),
            "attempts": sum(sample["attempts"] for sample in samples),
            "transfer_id": single["transfer_id"],
            "measured_latency_seconds": single.get("latency_seconds"),
            "measured_rtt_seconds": single.get("latency_seconds"),
            "events": [sample.get("receiver_event") for sample in samples[-20:]
                       if sample.get("receiver_event")],
            "raw_samples": samples,
            "warmup_samples": warmup_samples,
            "performance": aggregate,
            "warmups": warmups,
            "samples": sample_count,
            "concurrency": concurrency,
            "logs": {
                src: self.sim.node_log(src).splitlines()[-40:],
                dst: self.sim.node_log(dst).splitlines()[-40:],
            },
        }
        passed = details["started"] and details["delivered"] and all(
            sample["delivered"] for sample in warmup_samples
        )
        return self._record(action, passed, **details)

    def _load(self, action):
        duration = max(0.1, float(action.get("duration", 5.0)))
        concurrency = max(1, int(action.get("concurrency", 1)))
        warmups = max(0, int(action.get("warmups", 0)))
        prepared = self._prepare_transfer(action)
        warmup_samples = [self._send_sample(action, index + 1, warmup=True, prepared=prepared)
                          for index in range(warmups)]
        samples = []
        window_start = time.monotonic()
        while time.monotonic() - window_start < duration:
            samples.extend(self._send_batch(
                action, len(samples) + 1, concurrency, prepared=prepared))
        aggregate = aggregate_samples(samples)
        src, dst = action["src"], action["dst"]
        source_impl = self.sim.topology.nodes[src].get("implementation", "python")
        destination_impl = self.sim.topology.nodes[dst].get("implementation", "python")
        trace = self.sim.trace_route(src, dst)
        details = {
            "case_id": action.get("case_id"),
            "source": src,
            "destination": dst,
            "source_implementation": source_impl,
            "destination_implementation": destination_impl,
            "path": trace.get("path"),
            "hops": max(0, len(trace.get("path") or []) - 1),
            "transfer": action.get("kind", "load"),
            "mode": self._mode(action),
            "bytes": int(action.get("size", 64)),
            "started": all(sample["started"] for sample in samples),
            "delivered": bool(samples) and all(sample["delivered"] for sample in samples),
            "attempts": sum(sample["attempts"] for sample in samples),
            "raw_samples": samples,
            "warmup_samples": warmup_samples,
            "performance": aggregate,
            "warmups": warmups,
            "samples": len(samples),
            "concurrency": concurrency,
            "load_window_seconds": round(time.monotonic() - window_start, 6),
            "logs": {
                src: self.sim.node_log(src).splitlines()[-40:],
                dst: self.sim.node_log(dst).splitlines()[-40:],
            },
        }
        passed = details["started"] and details["delivered"] and all(
            sample["delivered"] for sample in warmup_samples)
        return self._record(action, passed, **details)

    def run_action(self, action):
        name = action.get("action")
        if name == "wait_ready":
            passed, counts = self._wait_ready(float(action.get("timeout", 120)))
            return self._record(action, passed, **counts)
        if name in ("transfer", "packet", "link", "resource", "direct",
                    "opportunistic", "propagated", "cold_path"):
            action = dict(action)
            if name != "transfer":
                action.setdefault("kind", name)
            if name == "resource":
                action.setdefault("size", 2048)
            return self._transfer(action)
        if name == "load":
            return self._load(dict(action))
        if name == "announce":
            nodes = action.get("nodes", list(self.sim.topology.nodes))
            passed = all(self.sim.announce_lxmf(node) for node in nodes)
            return self._record(action, passed, nodes=nodes)
        if name == "restart":
            passed = self.sim.restart_node(action["node"])
            return self._record(action, passed, node=action["node"])
        if name == "shutdown":
            return self._record(action, self.sim.shutdown_node(action["node"]), node=action["node"])
        if name == "start":
            return self._record(action, self.sim.start_node(action["node"]), node=action["node"])
        if name in ("disconnect", "reconnect"):
            connected = name == "reconnect"
            passed = self.sim.set_medium_connection(action["node"], action["medium"], connected)
            return self._record(action, passed, node=action["node"], medium=action["medium"])
        if name == "partition":
            passed = self.sim.partition_medium(action["medium"], action["groups"])
            return self._record(action, passed, medium=action["medium"], groups=action["groups"])
        if name == "recover":
            passed = self.sim.recover_partition(action["medium"])
            return self._record(action, passed, medium=action["medium"])
        if name == "sleep":
            time.sleep(float(action.get("seconds", 1)))
            return self._record(action, True)
        return self._record(action, False, error="unknown action")

    def run(self):
        self.sim.start()
        passed = True
        for action in self.scenario.get("actions", [{"action": "wait_ready"}]):
            action_started = time.monotonic()
            try:
                action_passed = self.run_action(action)
                passed = action_passed and passed
                if not action_passed and action.get("required", True):
                    self._finish_action(action_started)
                    break
            except Exception as error:
                passed = False
                self._record(action, False, error=str(error))
                if action.get("required", True):
                    self._finish_action(action_started)
                    break
            self._finish_action(action_started)
        return passed

    def _finish_action(self, action_started):
        if not self.results:
            return
        result = self.results[-1]
        result.setdefault("action_index", len(self.results))
        result.setdefault("started_at_seconds", round(action_started - self.started, 3))
        result.setdefault("duration_seconds", round(time.monotonic() - action_started, 3))


def execute_scenario(scenario, keep=False, data_dir=None, lxmf_rs_root=None):
    """Execute one scenario object and return a schema-v3 report."""
    from .environment import capture_environment
    from .manager import Simulator
    from .resource_monitor import ResourceMonitor

    node_count = _node_count(scenario)
    data_dir = os.path.abspath(data_dir) if data_dir else tempfile.mkdtemp(prefix="reticulated-")
    port_base = int(scenario.get("port_base", _find_port_base(node_count)))
    seed = int(scenario.get("seed", 0))
    _configure_runtime(data_dir, port_base, seed)

    simulator = None
    monitor = None
    resource_usage = {"available": False, "reason": "scenario did not start", "groups": {}}
    started = time.time()
    started_at = utc_timestamp()
    try:
        simulator = Simulator()
        monitor = ResourceMonitor(simulator.process_inventory)
        monitor.start()
        topology = (_generated_topology(scenario["generate"])
                    if "generate" in scenario else scenario["topology"])
        if not simulator.load_topology(topology):
            raise RuntimeError("invalid scenario topology")
        runner = ScenarioRunner(simulator, scenario)
        passed = runner.run()
        resource_usage = monitor.stop()
        media = simulator.hub.snapshot()
        network = topology_summary(topology, media)
        ready = next((result for result in runner.results
                      if result.get("action") == "wait_ready"), None)
        raw_samples = [sample for result in runner.results
                       for sample in result.get("raw_samples", [])]
        report = {
            "schema_version": 3,
            "report_type": "scenario",
            "scenario": scenario.get("name", "inline-scenario"),
            "status": "pass" if passed else "fail",
            "nodes": len(simulator.topology.nodes),
            "implementations": sorted(network["implementation_counts"]),
            "implementation_counts": network["implementation_counts"],
            "implementation_roles": {
                "nodes": network["implementation_counts"],
                "transport_nodes": network["transport_counts"],
            },
            "started_at": started_at,
            "generated_at": utc_timestamp(),
            "duration_seconds": round(time.time() - started, 3),
            "results": runner.results,
            "raw_samples": raw_samples,
            "media": media,
            "network": network,
            "scenario_configuration": scenario.get("generate"),
            "topology_seed": seed,
            "convergence": {
                "ready_seconds": ready.get("duration_seconds") if ready else None,
                "running": ready.get("running") if ready else None,
                "connected": ready.get("connected") if ready else None,
                "addressed": ready.get("addressed") if ready else None,
            },
            "resource_usage": resource_usage,
            "environment": capture_environment(lxmf_rs_root),
            "data_dir": data_dir if keep else None,
        }
        report["summary"] = summarize_report(report)
    except Exception as error:
        passed = False
        if monitor is not None:
            resource_usage = monitor.stop()
        report = {
            "schema_version": 3,
            "report_type": "scenario",
            "scenario": scenario.get("name", "inline-scenario"),
            "status": "fail",
            "error": str(error),
            "nodes": node_count,
            "started_at": started_at,
            "generated_at": utc_timestamp(),
            "duration_seconds": round(time.time() - started, 3),
            "topology_seed": seed,
            "results": [],
            "raw_samples": [],
            "resource_usage": resource_usage,
            "environment": capture_environment(lxmf_rs_root),
        }
        report["summary"] = summarize_report(report)
    finally:
        if simulator is not None:
            simulator.shutdown()
        if not keep:
            shutil.rmtree(data_dir, ignore_errors=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a headless Reticulated mixed-network scenario")
    parser.add_argument("scenario")
    parser.add_argument("--report", default="reticulated-results.json")
    parser.add_argument("--dashboard", nargs="?", const="reticulated-dashboard.html",
                        help="also write a standalone HTML dashboard")
    parser.add_argument("--keep", action="store_true", help="keep node data and logs")
    parser.add_argument("--data-dir")
    parser.add_argument("--seed", type=int, help="override the scenario random seed")
    args = parser.parse_args(argv)

    with open(args.scenario, encoding="utf-8") as stream:
        scenario = json.load(stream)
    scenario.setdefault("name", os.path.basename(args.scenario))
    if args.seed is not None:
        scenario["seed"] = args.seed
    report = execute_scenario(scenario, keep=args.keep, data_dir=args.data_dir)
    passed = report["status"] == "pass"

    report_path = os.path.abspath(args.report)
    with open(report_path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    output = {"status": report["status"], "report": report_path,
              "nodes": report.get("nodes")}
    if args.dashboard:
        from .dashboard import render_dashboard

        dashboard_path = os.path.abspath(args.dashboard)
        report["source_file"] = os.path.basename(report_path)
        with open(dashboard_path, "w", encoding="utf-8") as stream:
            stream.write(render_dashboard([report]))
        output["dashboard"] = dashboard_path
    print(json.dumps(output, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
