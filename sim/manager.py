import os
import sys
import time
import queue
import random
import shutil
import threading
import subprocess
import base64
import uuid
from collections import deque

from . import config
from .topology import Topology
from .medium_hub import MediumHub
from .node_manager import NodeManager
from . import status

GEN_PRESETS = {
    "tcp": {"bitrate": 10000000, "mtu": 32768},
    "ethernet": {"bitrate": 100000000, "mtu": 32768},
    "packet1200": {"bitrate": 1200, "mtu": 500},
    "packet9600": {"bitrate": 9600, "mtu": 500},
    "lora_fast": {"bitrate": 21875, "mtu": 500},
    "lora_mid": {"bitrate": 5469, "mtu": 500},
    "lora_slow": {"bitrate": 293, "mtu": 500},
}


class Simulator:
    def __init__(self):
        config.ensure_dirs()
        self.events = queue.Queue()
        self.transport_map = {}
        self.topology = Topology()
        self.topology.load()
        self.hub = MediumHub(
            config.HUB_HOST,
            config.HUB_PORT,
            event_sink=self.events.put,
            seed=config.RANDOM_SEED,
        )
        template_path = os.path.join(os.path.dirname(__file__), "sim_interface_template.py")
        with open(template_path) as f:
            template_code = f.read()
        self.node_manager = NodeManager(
            config.HUB_HOST,
            config.HUB_PORT,
            template_code,
            log_sink=self.on_log,
            msg_sink=self.on_msg,
        )
        self.active = False
        self.lock = threading.Lock()
        self.hub.start()
        self.push_all_params()
        supervisor = threading.Thread(target=self._messenger_supervisor)
        supervisor.daemon = True
        supervisor.start()

    def on_log(self, entry):
        self.events.put({"type": "log", "node": entry["node"], "line": entry["line"]})

    def on_msg(self, event):
        self.events.put(event)

    def _messenger_supervisor(self):
        while True:
            try:
                if self.active:
                    node_ids = list(self.topology.nodes.keys())
                    for node_id in node_ids:
                        connected = self.hub.node_connected(node_id)
                        running = self.node_manager.is_messenger_running(node_id)
                        if connected and not running:
                            self.node_manager.start_messenger(node_id, self.effective_interval(node_id))
                        elif running and not connected:
                            self.node_manager.stop_messenger(node_id)
            except Exception:
                pass
            time.sleep(1.5)

    def emit(self, event):
        self.events.put(event)

    def push_all_params(self):
        for link_id, link in self.topology.links.items():
            self.hub.set_medium_params(
                link_id,
                mtu=link["mtu"],
                bitrate=link["bitrate"],
                loss=link["loss"],
                propagation=link["propagation"],
            )

    def node_specs(self):
        default_cap = self.topology.settings.get("announce_cap", 2.0)
        loglevel = self.topology.settings.get("loglevel", 4)
        result = {nid: {
            "links": [],
            "transport": node.get("transport", False),
            "mode": node.get("mode", "full"),
            "rate_target": node.get("announce_rate_target"),
            "rate_grace": node.get("announce_rate_grace"),
            "rate_penalty": node.get("announce_rate_penalty"),
            "announce_cap": node.get("announce_cap") if node.get("announce_cap") is not None else default_cap,
            "loglevel": loglevel,
            "implementation": node.get("implementation", "python"),
            "propagation_node": node.get("propagation_node", False),
        } for nid, node in self.topology.nodes.items()}
        for link_id, link in self.topology.links.items():
            for nid in link["members"]:
                if nid in result:
                    result[nid]["links"].append({
                        "id": link_id,
                        "mtu": link["mtu"],
                        "bitrate": link["bitrate"],
                        "mode": self.topology.node_link_mode(nid, link_id),
                    })
        return result

    def sync(self):
        if self.active:
            self.push_all_params()
            self.node_manager.apply(self.node_specs(), True)
        self.topology.save()

    def add_node(self, label=None, x=0.0, y=0.0, implementation="python"):
        with self.lock:
            node_id = self.topology.add_node(label, x, y, implementation)
            self.sync()
        self.emit({"type": "topology"})
        return node_id

    def remove_node(self, node_id):
        with self.lock:
            removed_links = self.topology.remove_node(node_id)
            for link_id in removed_links:
                self.hub.remove_medium(link_id)
            self.sync()
        self.emit({"type": "topology"})
        return removed_links

    def update_node(self, node_id, label=None, x=None, y=None):
        with self.lock:
            ok = self.topology.update_node(node_id, label, x, y)
            self.topology.save()
        return ok

    def set_node_options(self, node_id, fields):
        with self.lock:
            ok = node_id in self.topology.nodes
            if "transport" in fields:
                self.topology.set_node_transport(node_id, fields["transport"])
            if "mode" in fields:
                self.topology.set_node_mode(node_id, fields["mode"])
            if "implementation" in fields:
                self.topology.set_node_implementation(node_id, fields["implementation"])
            for key in ("announce_rate_target", "announce_rate_grace", "announce_rate_penalty", "announce_cap"):
                if key in fields:
                    self.topology.set_node_field(node_id, key, fields[key])
            self.sync()
        self.emit({"type": "topology"})
        return ok

    def set_node_link_mode(self, node_id, link_id, mode):
        with self.lock:
            ok = self.topology.set_node_link_mode(node_id, link_id, mode)
            if ok:
                self.sync()
        if ok:
            self.emit({"type": "topology"})
        return ok

    def set_announce_cap(self, cap):
        try:
            cap = max(0.1, min(100.0, float(cap)))
        except Exception:
            return False
        with self.lock:
            self.topology.settings["announce_cap"] = cap
            self.sync()
        self.emit({"type": "settings", "settings": dict(self.topology.settings)})
        self.emit({"type": "topology"})
        return True

    def set_loglevel(self, level):
        try:
            level = max(0, min(7, int(level)))
        except Exception:
            return False
        with self.lock:
            self.topology.settings["loglevel"] = level
            self.sync()
        self.emit({"type": "settings", "settings": dict(self.topology.settings)})
        self.emit({"type": "topology"})
        return True

    def set_node_color(self, node_id, color):
        with self.lock:
            ok = self.topology.set_node_field(node_id, "color", color)
            self.topology.save()
        return ok

    def node_log(self, node_id):
        return self.node_manager.get_log(node_id)

    def effective_interval(self, node_id):
        node = self.topology.nodes.get(node_id, {})
        value = node.get("announce_interval")
        if value is None:
            return self.topology.settings.get("announce_interval", 300.0)
        return value

    def set_node_announce_interval(self, node_id, interval):
        if node_id not in self.topology.nodes:
            return False
        try:
            interval = max(0.0, float(interval))
        except Exception:
            return False
        if 0 < interval < 60:
            interval = 60.0
        with self.lock:
            self.topology.set_node_field(node_id, "announce_interval", interval)
            self.node_manager.send_to_messenger(node_id, "interval " + str(self.effective_interval(node_id)))
            self.topology.save()
        return True

    def add_link(self, members, mtu=None, bitrate=None, loss=None, propagation=None, x=0.0, y=0.0):
        with self.lock:
            link_id = self.topology.add_link(members, mtu, bitrate, loss, propagation, x, y)
            link = self.topology.links[link_id]
            self.hub.set_medium_params(link_id, link["mtu"], link["bitrate"], link["loss"], link["propagation"])
            self.sync()
        self.emit({"type": "topology"})
        return link_id

    def remove_link(self, link_id):
        with self.lock:
            ok = self.topology.remove_link(link_id)
            self.hub.remove_medium(link_id)
            self.sync()
        self.emit({"type": "topology"})
        return ok

    def set_link_params(self, link_id, mtu=None, bitrate=None, loss=None, propagation=None):
        with self.lock:
            ok = self.topology.set_link_params(link_id, mtu, bitrate, loss, propagation)
            if ok:
                link = self.topology.links[link_id]
                self.hub.set_medium_params(link_id, link["mtu"], link["bitrate"], link["loss"], link["propagation"])
            self.topology.save()
        if ok:
            link = self.topology.links[link_id]
            self.emit({"type": "link_update", "link": link_id, "params": {
                "mtu": link["mtu"],
                "bitrate": link["bitrate"],
                "loss": link["loss"],
                "propagation": link["propagation"],
            }})
        return ok

    def set_link_name(self, link_id, name):
        with self.lock:
            ok = self.topology.set_link_name(link_id, name)
            self.topology.save()
        if ok:
            self.emit({"type": "link_update", "link": link_id, "params": {"name": self.topology.links[link_id]["name"]}})
        return ok

    def set_link_members(self, link_id, members):
        with self.lock:
            ok = self.topology.set_link_members(link_id, members)
            self.sync()
        self.emit({"type": "topology"})
        return ok

    def update_link(self, link_id, x=None, y=None):
        with self.lock:
            ok = self.topology.update_link(link_id, x, y)
            self.topology.save()
        return ok

    def start(self):
        with self.lock:
            self.active = True
            self.push_all_params()
            self.node_manager.apply(self.node_specs(), True)
        self.emit({"type": "sim", "active": True})

    def stop(self):
        with self.lock:
            self.active = False
            self.node_manager.stop_all_messengers()
            self.node_manager.stop_all()
        self.emit({"type": "sim", "active": False})

    def shortest_path_links(self, src, dst):
        adjacency = {}
        for link_id, link in self.topology.links.items():
            for a in link["members"]:
                for b in link["members"]:
                    if a != b:
                        adjacency.setdefault(a, []).append((b, link_id))
        prev = {src: None}
        prev_link = {}
        q = deque([src])
        while q:
            node = q.popleft()
            if node == dst:
                break
            for neighbor, link_id in adjacency.get(node, []):
                if neighbor in prev:
                    continue
                if neighbor != dst and not self.topology.nodes.get(neighbor, {}).get("transport", False):
                    continue
                prev[neighbor] = node
                prev_link[neighbor] = link_id
                q.append(neighbor)
        if dst not in prev:
            return None
        links = []
        node = dst
        while prev[node] is not None:
            links.append(prev_link[node])
            node = prev[node]
        links.reverse()
        return links

    def expected_rtt(self, src, dst, msg_bytes):
        links = self.shortest_path_links(src, dst)
        if links is None:
            return None
        control_bytes = 100
        overhead_bytes = 60

        def hop_delay(byte_count, link_id):
            link = self.topology.links[link_id]
            bitrate = max(1, link["bitrate"])
            return (byte_count * 8) / bitrate + link["propagation"]

        one_way_control = sum(hop_delay(control_bytes, lid) for lid in links)
        one_way_message = sum(hop_delay(msg_bytes + overhead_bytes, lid) for lid in links)
        hops = len(links)
        return {"rtt": 3 * one_way_control + one_way_message, "hops": hops, "timeout": 6 * (hops + 1)}

    def send_message(self, src, dst, text, mode="direct", transfer_id=None):
        if src not in self.topology.nodes or dst not in self.topology.nodes:
            return False
        dst_addr = self.node_manager.lxmf_addresses.get(dst)
        if not dst_addr:
            self.emit({"type": "message_status", "node": src,
                       "status": "SENDFAIL peer not ready", "transfer_id": transfer_id})
            return False
        expected = self.expected_rtt(src, dst, len(text.encode("utf-8")))
        if expected is not None:
            self.emit({"type": "message_status", "node": src,
                       "status": "EXPECT rtt=%.3f hops=%d timeout=%.0f" % (expected["rtt"], expected["hops"], expected["timeout"]),
                       "transfer_id": transfer_id})
        implementation = self.topology.nodes[src].get("implementation", "python")
        if implementation == "rust":
            message_id = "reticulated-" + (transfer_id or uuid.uuid4().hex)
            method = mode if mode in ("direct", "opportunistic", "propagated") else "direct"
            params = {
                "id": message_id,
                "source": self.node_manager.lxmf_addresses.get(src),
                "destination": dst_addr,
                "content": text,
                "fields": None,
                "method": method,
            }
            try:
                result = self.node_manager.rust_call(src, "send_message_v2", params, timeout=15)
                self.emit({"type": "message_status", "node": src,
                           "status": "SENDING " + dst_addr, "message_id": message_id,
                           "transfer_id": transfer_id, "mode": method, "result": result})
                return True
            except Exception as error:
                self.emit({"type": "message_status", "node": src,
                           "status": "SENDFAIL " + str(error), "message_id": message_id,
                           "transfer_id": transfer_id, "mode": method})
                return False
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        method = mode if mode in ("direct", "opportunistic", "propagated") else "direct"
        return self.node_manager.send_to_messenger(
            src,
            "send_v2 %s %s %s %s" % (dst_addr, method, transfer_id or "none", encoded),
        )

    def configure_outbound_propagation(self, node_id, preferred=None):
        """Select the lab's enabled propagation node for one sender."""
        candidates = [nid for nid, node in self.topology.nodes.items()
                      if node.get("propagation_node")]
        if not candidates:
            return False
        propagation_node = preferred if preferred in candidates else candidates[0]
        propagation_hash = self.node_manager.propagation_addresses.get(propagation_node)
        if not propagation_hash:
            return False
        if self.topology.nodes[node_id].get("implementation", "python") == "rust":
            try:
                self.node_manager.rust_call(
                    node_id,
                    "set_outbound_propagation_node",
                    {"peer": propagation_hash},
                )
                return True
            except Exception:
                return False
        return self.node_manager.send_to_messenger(
            node_id, "propagation " + propagation_hash)

    def sync_propagation(self, node_id, preferred=None):
        candidates = [nid for nid, node in self.topology.nodes.items()
                      if node.get("propagation_node")]
        if not candidates:
            return False
        propagation_node = preferred if preferred in candidates else candidates[0]
        propagation_hash = self.node_manager.propagation_addresses.get(propagation_node)
        if not propagation_hash:
            return False
        if not self.configure_outbound_propagation(node_id, propagation_node):
            return False
        if self.topology.nodes[node_id].get("implementation", "python") == "rust":
            try:
                self.node_manager.rust_call(
                    node_id,
                    "propagation_remote_download",
                    {"remote": propagation_hash, "timeout_secs": 5.0},
                    timeout=8,
                )
                return True
            except Exception:
                return False
        return self.node_manager.send_to_messenger(node_id, "propagation_sync")

    def announce_propagation(self, node_id):
        if not self.topology.nodes.get(node_id, {}).get("propagation_node"):
            return False
        if self.topology.nodes[node_id].get("implementation", "python") == "rust":
            try:
                self.node_manager.rust_call(node_id, "announce_now")
                return True
            except Exception:
                return False
        return self.node_manager.send_to_messenger(node_id, "propagation_announce")

    def restart_node(self, node_id):
        with self.lock:
            if self.active:
                return self.node_manager.restart_node(node_id)
        return False

    def shutdown_node(self, node_id):
        with self.lock:
            return self.node_manager.shutdown_node(node_id)

    def start_node(self, node_id):
        with self.lock:
            np = self.node_manager.nodes.get(node_id)
            if not self.active or np is None or self.node_manager.is_running(node_id):
                return False
            if np.spec is not None:
                self.node_manager.start_bridges(np, np.spec)
            self.node_manager.start_node(node_id)
            return True

    def set_medium_connection(self, node_id, medium_id, connected):
        if node_id not in self.topology.nodes or medium_id not in self.topology.links:
            return False
        self.hub.set_connected(node_id, medium_id, connected)
        return True

    def partition_medium(self, medium_id, groups):
        if medium_id not in self.topology.links:
            return False
        self.hub.partition(medium_id, groups)
        return True

    def recover_partition(self, medium_id):
        if medium_id not in self.topology.links:
            return False
        self.hub.recover_partition(medium_id)
        return True

    def snapshot(self):
        with self.lock:
            addresses = {nid: self.node_manager.address_for(nid) for nid in self.topology.nodes}
            lxmf = {nid: self.node_manager.lxmf_addresses.get(nid) for nid in self.topology.nodes}
            bridges = {nid: {"host": config.BRIDGE_HOST, "port": self.node_manager.bridge_port(nid)}
                       for nid, node in self.topology.nodes.items() if node.get("transport", False)}
            return {
                "active": self.active,
                "topology": self.topology.to_dict(),
                "media": self.hub.snapshot(),
                "nodes": self.node_manager.states(),
                "addresses": addresses,
                "lxmf": lxmf,
                "bridges": bridges,
                "settings": dict(self.topology.settings),
                "implementations": {nid: node.get("implementation", "python")
                                    for nid, node in self.topology.nodes.items()},
            }

    def process_inventory(self):
        inventory = self.node_manager.process_inventory()
        inventory.append({
            "pid": os.getpid(),
            "group": "simulator",
            "role": "hub-and-rust-bridges",
            "node": None,
        })
        return inventory

    def lxmf_map(self):
        with self.lock:
            return {nid: self.node_manager.lxmf_addresses.get(nid) for nid in self.topology.nodes}

    def set_announce_interval(self, interval):
        try:
            interval = max(0.0, float(interval))
        except Exception:
            return False
        if 0 < interval < 60:
            interval = 60.0
        with self.lock:
            self.topology.settings["announce_interval"] = interval
            for node_id in self.topology.nodes:
                self.node_manager.send_to_messenger(node_id, "interval " + str(self.effective_interval(node_id)))
            self.topology.save()
        self.emit({"type": "settings", "settings": dict(self.topology.settings)})
        return True

    def announce(self, node_id):
        if node_id not in self.topology.nodes:
            return False
        if not self.active:
            self.emit({"type": "announce", "node": node_id, "line": "simulation not running"})
            return False
        if self.topology.nodes[node_id].get("implementation", "python") == "rust":
            return self.announce_lxmf(node_id)
        cfg_dir = self.node_manager.config_dir_for(node_id)
        thread = threading.Thread(target=self._announce_worker, args=(node_id, cfg_dir), daemon=True)
        thread.start()
        return True

    def announce_lxmf(self, node_id):
        if node_id not in self.topology.nodes:
            return False
        if not self.active:
            self.emit({"type": "announce", "node": node_id, "line": "simulation not running"})
            return False
        if self.topology.nodes[node_id].get("implementation", "python") == "rust":
            try:
                self.node_manager.rust_call(node_id, "announce_now")
                self.emit({"type": "announce", "node": node_id,
                           "address": self.node_manager.lxmf_addresses.get(node_id)})
                return True
            except Exception as error:
                self.emit({"type": "announce", "node": node_id, "line": str(error)})
                return False
        return self.node_manager.send_to_messenger(node_id, "announce")

    def _announce_worker(self, node_id, cfg_dir):
        proc = subprocess.Popen(
            [sys.executable, "-m", "sim.traffic", "announce", "--config", cfg_dir],
            cwd=config.BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith("ANNOUNCED"):
                parts = line.split(" ", 1)
                self.emit({"type": "announce", "node": node_id, "address": parts[1].strip() if len(parts) > 1 else ""})
            else:
                self.emit({"type": "announce", "node": node_id, "line": line})
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()

    def node_status(self, node_id):
        if self.topology.nodes.get(node_id, {}).get("implementation", "python") == "rust":
            try:
                data = self.node_manager.rust_call(node_id, "status") or {}
                interfaces = data.get("interfaces", [])
                return {"online": bool(data.get("running", False)),
                        "interfaces": interfaces,
                        "transport_id": data.get("identity_hash"),
                        "implementation": "rust"}
            except Exception:
                return {"online": False, "interfaces": [], "transport_id": None,
                        "implementation": "rust"}
        cfg_dir = self.node_manager.config_dir_for(node_id)
        result = status.node_status(cfg_dir)
        result["implementation"] = "python"
        return result

    def node_paths(self, node_id):
        if self.topology.nodes.get(node_id, {}).get("implementation", "python") == "rust":
            try:
                data = self.node_manager.rust_call(node_id, "list_announces") or {}
                return [{"destination": entry.get("peer"), "hops": entry.get("hops"),
                         "via": entry.get("next_hop"), "interface": entry.get("interface")}
                        for entry in data.get("announces", [])]
            except Exception:
                return []
        cfg_dir = self.node_manager.config_dir_for(node_id)
        return status.node_paths(cfg_dir)

    def drop_path(self, node_id, dest_hex):
        if self.topology.nodes.get(node_id, {}).get("implementation", "python") == "rust":
            try:
                self.node_manager.rust_call(node_id, "drop_path", {"destination": dest_hex})
                return True
            except Exception:
                return False
        cfg_dir = self.node_manager.config_dir_for(node_id)
        return status.drop_path(cfg_dir, dest_hex)

    def request_path(self, node_id, dest_hex, timeout=30):
        """Request a path without hiding discovery time from the measurement."""
        if self.topology.nodes.get(node_id, {}).get("implementation", "python") == "rust":
            try:
                result = self.node_manager.rust_call(
                    node_id,
                    "request_path",
                    {"destination": dest_hex, "timeout_secs": max(0, int(timeout))},
                    timeout=max(2, float(timeout) + 2),
                ) or {}
                return result.get("status") == "found" or bool(result.get("path_found"))
            except Exception:
                return False
        return self.node_manager.send_to_messenger(
            node_id, "request_path %s %s" % (dest_hex, max(0.0, float(timeout))))

    def all_status(self):
        result = {}
        tmap = {}
        for node_id in list(self.topology.nodes.keys()):
            st = self.node_status(node_id)
            result[node_id] = st
            tid = st.get("transport_id")
            if tid:
                tmap[tid] = node_id
        self.transport_map = tmap
        return result

    def dest_addresses(self, node_id):
        addrs = set()
        sa = self.node_manager.address_for(node_id)
        if sa:
            addrs.add(sa)
        la = self.node_manager.lxmf_addresses.get(node_id)
        if la:
            addrs.add(la)
        return addrs

    def trace_route(self, src, dst):
        if src not in self.topology.nodes or dst not in self.topology.nodes:
            return {"path": None, "reason": "unknown node"}
        if src == dst:
            return {"path": [src], "reason": "same node"}
        adjacency = {}
        for link in self.topology.links.values():
            for a in link["members"]:
                for b in link["members"]:
                    if a != b:
                        adjacency.setdefault(a, set()).add(b)
        prev = {src: None}
        queue = deque([src])
        while queue:
            node = queue.popleft()
            if node == dst:
                break
            for neighbor in adjacency.get(node, ()):
                if neighbor in prev:
                    continue
                if neighbor != dst and not self.topology.nodes.get(neighbor, {}).get("transport", False):
                    continue
                prev[neighbor] = node
                queue.append(neighbor)
        if dst not in prev:
            return {"path": None, "reason": "no path (intermediate nodes must be transport routers)"}
        path = []
        node = dst
        while node is not None:
            path.append(node)
            node = prev[node]
        path.reverse()
        return {"path": path, "reason": "ok"}

    def run_traffic(self, src, dst, size):
        thread = threading.Thread(target=self._traffic_worker, args=(src, dst, size))
        thread.daemon = True
        thread.start()

    def _read_process(self, proc, label, on_line):
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if not line:
                continue
            self.emit({"type": "traffic", "stage": label, "line": line})
            on_line(line)
        try:
            proc.stdout.close()
        except Exception:
            pass

    def _traffic_worker(self, src, dst, size):
        if src not in self.topology.nodes or dst not in self.topology.nodes:
            self.emit({"type": "traffic", "stage": "error", "line": "unknown node"})
            return
        if not self.active:
            self.emit({"type": "traffic", "stage": "error", "line": "simulation not running"})
            return
        dst_dir = self.node_manager.config_dir_for(dst)
        src_dir = self.node_manager.config_dir_for(src)
        src_name = self.topology.nodes[src].get("label", src)
        dst_name = self.topology.nodes[dst].get("label", dst)
        expected = self.expected_rtt(src, dst, size)
        self.emit({"type": "traffic", "stage": "begin", "src": src, "dst": dst,
                   "src_name": src_name, "dst_name": dst_name, "size": size, "expected": expected})

        recv = subprocess.Popen(
            [sys.executable, "-m", "sim.traffic", "recv", "--config", dst_dir, "--timeout", "90"],
            cwd=config.BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        dest_holder = {"hash": None}
        ready = threading.Event()

        def recv_line(line):
            if line.startswith("DEST ") and dest_holder["hash"] is None:
                dest_holder["hash"] = line.split(" ", 1)[1].strip()
                ready.set()

        recv_thread = threading.Thread(target=self._read_process, args=(recv, "recv", recv_line))
        recv_thread.daemon = True
        recv_thread.start()

        if not ready.wait(timeout=20):
            self.emit({"type": "traffic", "stage": "error", "line": "receiver did not start"})
            try:
                recv.terminate()
            except Exception:
                pass
            return

        send = subprocess.Popen(
            [sys.executable, "-m", "sim.traffic", "send", "--config", src_dir,
             "--dest", dest_holder["hash"], "--size", str(size), "--timeout", "90"],
            cwd=config.BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        result = {"status": "failed", "rtt": None}

        def send_line(line):
            if line.startswith("SEND COMPLETE"):
                result["status"] = "complete"
                for tok in line.split(" "):
                    if tok.startswith("rtt="):
                        try:
                            result["rtt"] = float(tok[4:])
                        except Exception:
                            result["rtt"] = None

        send_thread = threading.Thread(target=self._read_process, args=(send, "send", send_line))
        send_thread.daemon = True
        send_thread.start()

        send.wait()
        try:
            recv.terminate()
        except Exception:
            pass
        self.emit({"type": "traffic", "stage": "result", "src": src, "dst": dst,
                   "src_name": src_name, "dst_name": dst_name, "size": size,
                   "expected": expected, "actual_rtt": result["rtt"], "status": result["status"]})
        self.emit({"type": "traffic", "stage": "end", "src": src, "dst": dst})

    def reset(self):
        with self.lock:
            self.active = False
            self.node_manager.stop_all_messengers()
            self.node_manager.stop_all()
            self.node_manager.nodes.clear()
            self.node_manager.addresses.clear()
            self.node_manager.lxmf_addresses.clear()
            self.hub.clear()
            self.topology.nodes.clear()
            self.topology.links.clear()
            self.topology.node_seq = 0
            self.topology.link_seq = 0
            try:
                shutil.rmtree(config.NODES_DIR)
            except Exception:
                pass
            os.makedirs(config.NODES_DIR, exist_ok=True)
            self.topology.save()
        self.emit({"type": "sim", "active": False})
        self.emit({"type": "topology"})
        return True

    def generate(self, num_nodes, max_hops, preset_keys, max_loss, shape="hub",
                 rust_nodes=None, rust_ratio=None):
        num_nodes = max(1, min(200, int(num_nodes)))
        max_hops = max(2, int(max_hops))
        try:
            max_loss = max(0.0, min(1.0, float(max_loss)))
        except Exception:
            max_loss = 0.0
        chosen = [GEN_PRESETS[k] for k in preset_keys if k in GEN_PRESETS]
        if not chosen:
            chosen = list(GEN_PRESETS.values())
        self.reset()
        depth_limit = max(1, max_hops // 2)
        with self.lock:
            positions = {}
            degree = {}
            pairs = set()
            ids = []

            def rand_pos():
                return (random.uniform(80.0, 1200.0), random.uniform(60.0, 760.0))

            def new_node():
                x, y = rand_pos()
                nid = self.topology.add_node(None, x, y)
                ids.append(nid)
                positions[nid] = (x, y)
                degree[nid] = 0
                return nid

            def link_between(a, b):
                preset = random.choice(chosen)
                loss = round(random.uniform(0, max_loss), 2) if max_loss > 0 else 0.0
                ax, ay = positions[a]
                bx, by = positions[b]
                lid = self.topology.add_link([a, b], mtu=preset["mtu"], bitrate=preset["bitrate"],
                                             loss=loss, x=(ax + bx) / 2, y=(ay + by) / 2)
                self.hub.set_medium_params(lid, mtu=preset["mtu"], bitrate=preset["bitrate"], loss=loss, propagation=0.0)
                degree[a] += 1
                degree[b] += 1
                pairs.add(frozenset((a, b)))

            transports = []
            if shape == "hub":
                num_hubs = max(1, min(max_hops - 1, num_nodes))
                hub_ids = [new_node() for _ in range(num_hubs)]
                for i in range(1, num_hubs):
                    link_between(hub_ids[i - 1], hub_ids[i])
                num_leaves = num_nodes - num_hubs
                for i in range(num_leaves):
                    if i == 0:
                        hub = hub_ids[0]
                    elif i == 1:
                        hub = hub_ids[-1]
                    else:
                        hub = random.choice(hub_ids)
                    link_between(new_node(), hub)
                transports = list(hub_ids)
            elif num_nodes >= 1:
                depths = {}
                root = new_node()
                depths[root] = 0
                for _ in range(1, num_nodes):
                    parent = random.choice([n for n in ids if depths.get(n, 0) < depth_limit])
                    child = new_node()
                    depths[child] = depths[parent] + 1
                    link_between(parent, child)
                if shape == "mesh":
                    extra = num_nodes // 3
                    attempts = 0
                    while extra > 0 and len(ids) > 2 and attempts < num_nodes * 5:
                        attempts += 1
                        a, b = random.sample(ids, 2)
                        if frozenset((a, b)) in pairs:
                            continue
                        link_between(a, b)
                        extra -= 1
                transports = [n for n in ids if degree.get(n, 0) >= 2]

            for node_id in transports:
                self.topology.set_node_transport(node_id, True)
                self.topology.set_node_mode(node_id, "gateway")
            if rust_nodes is not None or rust_ratio is not None:
                if rust_nodes is None:
                    rust_nodes = round(num_nodes * max(0.0, min(1.0, float(rust_ratio))))
                rust_nodes = max(0, min(num_nodes, int(rust_nodes)))
                mixed_order = ids[::2] + ids[1::2]
                for node_id in mixed_order[:rust_nodes]:
                    self.topology.set_node_implementation(node_id, "rust")
            self.topology.save()
        self.emit({"type": "topology"})
        rust_count = sum(node.get("implementation") == "rust"
                         for node in self.topology.nodes.values())
        return {"nodes": len(ids), "links": len(pairs), "transport_nodes": len(transports),
                "rust_nodes": rust_count, "python_nodes": len(ids) - rust_count}

    def set_positions(self, node_positions, link_positions):
        with self.lock:
            for nid, pos in (node_positions or {}).items():
                node = self.topology.nodes.get(nid)
                if node is not None and isinstance(pos, (list, tuple)) and len(pos) == 2:
                    try:
                        node["x"] = float(pos[0])
                        node["y"] = float(pos[1])
                    except (TypeError, ValueError):
                        pass
            for lid, pos in (link_positions or {}).items():
                link = self.topology.links.get(lid)
                if link is not None and isinstance(pos, (list, tuple)) and len(pos) == 2:
                    try:
                        link["x"] = float(pos[0])
                        link["y"] = float(pos[1])
                    except (TypeError, ValueError):
                        pass
            self.topology.save()
        return True

    def export_topology(self):
        with self.lock:
            return self.topology.to_dict()

    def load_topology(self, data):
        if not isinstance(data, dict) or "nodes" not in data or "links" not in data:
            return False
        self.reset()
        with self.lock:
            self.topology.from_dict(data)
            self.push_all_params()
            self.topology.save()
        self.emit({"type": "topology"})
        return True

    def shutdown(self):
        self.stop()
        self.hub.stop()
