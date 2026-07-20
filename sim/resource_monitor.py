"""Best-effort process-tree CPU and resident-memory sampling."""

import os
import threading
import time


class ResourceMonitor:
    def __init__(self, inventory, interval=0.1):
        self.inventory = inventory
        self.interval = max(0.02, float(interval))
        self.stop_event = threading.Event()
        self.thread = None
        self.available = False
        self.reason = None
        self.first_cpu = {}
        self.last_cpu = {}
        self.peak_rss = {}
        self.metadata = {}
        self.group_peak_rss = {}
        try:
            import psutil

            self.psutil = psutil
            self.available = True
        except ImportError:
            self.psutil = None
            self.reason = "psutil is not installed"

    def start(self):
        if not self.available or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            self.sample()
            self.stop_event.wait(self.interval)

    def sample(self):
        if not self.available:
            return
        group_rss = {}
        seen = set()
        for entry in self.inventory() or []:
            pid = int(entry.get("pid", 0))
            if pid <= 0:
                continue
            try:
                process = self.psutil.Process(pid)
                processes = [process] + process.children(recursive=True)
            except (self.psutil.Error, OSError):
                continue
            for child in processes:
                child_pid = int(child.pid)
                if child_pid in seen:
                    continue
                try:
                    cpu = child.cpu_times()
                    cpu_seconds = float(cpu.user) + float(cpu.system)
                    rss = int(child.memory_info().rss)
                except (self.psutil.Error, OSError):
                    continue
                seen.add(child_pid)
                metadata = dict(entry)
                if child_pid != pid:
                    metadata["role"] = "%s-child" % (metadata.get("role") or "process")
                    metadata["parent_pid"] = pid
                self.first_cpu.setdefault(child_pid, cpu_seconds)
                self.last_cpu[child_pid] = cpu_seconds
                self.peak_rss[child_pid] = max(self.peak_rss.get(child_pid, 0), rss)
                self.metadata[child_pid] = metadata
                group = metadata.get("group", "other")
                group_rss[group] = group_rss.get(group, 0) + rss
        for group, rss in group_rss.items():
            self.group_peak_rss[group] = max(self.group_peak_rss.get(group, 0), rss)

    def stop(self):
        if self.thread is not None:
            self.stop_event.set()
            self.thread.join(timeout=max(1.0, self.interval * 4))
            self.thread = None
        self.sample()
        return self.report()

    def report(self):
        if not self.available:
            return {"available": False, "reason": self.reason, "groups": {}}
        groups = {}
        processes = []
        for pid, metadata in sorted(self.metadata.items()):
            group = metadata.get("group", "other")
            cpu_seconds = max(0.0, self.last_cpu.get(pid, 0.0) - self.first_cpu.get(pid, 0.0))
            peak_rss = self.peak_rss.get(pid)
            row = {
                "pid": pid,
                "group": group,
                "role": metadata.get("role"),
                "node": metadata.get("node"),
                "cpu_seconds": cpu_seconds,
                "peak_rss_bytes": peak_rss,
            }
            processes.append(row)
            aggregate = groups.setdefault(group, {
                "processes": 0,
                "cpu_seconds": 0.0,
                "peak_rss_bytes": 0,
            })
            aggregate["processes"] += 1
            aggregate["cpu_seconds"] += cpu_seconds
            aggregate["peak_rss_bytes"] = self.group_peak_rss.get(group, 0)
        for aggregate in groups.values():
            aggregate["cpu_seconds"] = round(aggregate["cpu_seconds"], 6)
        shared_attribution = {}
        if any(row.get("role") == "hub-and-rust-bridges" for row in processes):
            reason = ("Rust medium adapters run as threads in the simulator process; "
                      "per-component RSS and CPU counters are unavailable")
            shared_attribution = {
                "combined_group": "simulator",
                "bridges": {"available": False, "included_in": "simulator", "reason": reason},
                "simulator_overhead": {
                    "available": False, "included_in": "simulator", "reason": reason,
                },
            }
        return {
            "available": True,
            "sampler_pid": os.getpid(),
            "groups": groups,
            "processes": processes,
            "shared_process_attribution": shared_attribution,
        }
