import os
import sys
import signal
import subprocess
import threading
import collections
import json
import time
import base64
import re

import RNS

from . import config
from .rust_bridge import RustMediumBridge
from . import rust_rpc

APP_NAME = "simnode"
ASPECT = "endpoint"
TRANSFER_ID = re.compile(r"reticulated:([0-9a-f]{32}):")


class NodeProcess:
    def __init__(self, node_id, config_dir, implementation="python"):
        self.node_id = node_id
        self.config_dir = config_dir
        self.implementation = implementation
        self.spec = None
        self.proc = None
        self.config_text = None
        self.log = collections.deque(maxlen=400)
        self.logpath = None
        self.logfile = None
        self.messenger = None
        self.lxmf_address = None
        self.bridges = []
        self.rpc_port = None
        self.event_stop = threading.Event()
        self.event_thread = None
        self.event_cursor = None
        self.generation = 0


class NodeManager:
    def __init__(self, hub_host, hub_port, template_code, log_sink=None, msg_sink=None):
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.template_code = template_code
        self.log_sink = log_sink
        self.msg_sink = msg_sink
        self.nodes = {}
        self.addresses = {}
        self.lxmf_addresses = {}
        self.propagation_addresses = {}
        self.lock = threading.Lock()
        self.delivery_condition = threading.Condition()
        self.deliveries = {}

    def python_reference_env(self):
        env = dict(os.environ)
        roots = [path for path in (config.PYTHON_LXMF_REPO, config.PYTHON_RNS_REPO) if path]
        if roots:
            current = env.get("PYTHONPATH")
            env["PYTHONPATH"] = os.pathsep.join(roots + ([current] if current else []))
        env["PYTHONUNBUFFERED"] = "1"
        return env

    @staticmethod
    def transfer_id_for(text):
        match = TRANSFER_ID.search(str(text))
        return match.group(1) if match else None

    def record_delivery(self, node_id, text, event=None, transfer_id=None):
        transfer_id = transfer_id or self.transfer_id_for(text)
        record = {
            "type": "message",
            "node": node_id,
            "text": text,
            "transfer_id": transfer_id,
            "observed_monotonic": time.monotonic(),
        }
        if event is not None:
            record["raw_event"] = event
        if not transfer_id:
            if self.msg_sink:
                self.msg_sink(record)
            return
        with self.delivery_condition:
            self.deliveries[(node_id, transfer_id)] = record
            self.delivery_condition.notify_all()
        if self.msg_sink:
            self.msg_sink(record)

    def wait_for_delivery(self, node_id, transfer_id, timeout):
        deadline = time.monotonic() + float(timeout)
        key = (node_id, transfer_id)
        with self.delivery_condition:
            while key not in self.deliveries:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.delivery_condition.wait(timeout=remaining)
            return self.deliveries.pop(key)

    def config_dir_for(self, node_id):
        return os.path.join(config.NODES_DIR, node_id)

    def bridge_port(self, node_id):
        digits = "".join(ch for ch in node_id if ch.isdigit())
        try:
            index = int(digits)
        except Exception:
            index = 0
        return config.TCP_BASE + index

    def node_index(self, node_id):
        digits = "".join(ch for ch in node_id if ch.isdigit())
        try:
            return int(digits)
        except Exception:
            return 0

    def rust_bridge_port(self, node_id, link_index):
        return config.RUST_BRIDGE_BASE + self.node_index(node_id) * config.RUST_BRIDGE_STRIDE + link_index

    def rust_rpc_port(self, node_id):
        return config.RUST_RPC_BASE + self.node_index(node_id)

    def ensure_identity(self, node_id):
        cfg_dir = self.config_dir_for(node_id)
        os.makedirs(cfg_dir, exist_ok=True)
        idpath = os.path.join(cfg_dir, "sim_identity")
        try:
            if os.path.isfile(idpath):
                identity = RNS.Identity.from_file(idpath)
            else:
                identity = RNS.Identity()
                identity.to_file(idpath)
            return identity
        except Exception:
            return None

    def address_for(self, node_id):
        np = self.nodes.get(node_id)
        if np is not None and np.implementation == "rust":
            return np.lxmf_address
        if node_id in self.addresses:
            return self.addresses[node_id]
        identity = self.ensure_identity(node_id)
        if identity is None:
            return None
        try:
            addr = RNS.Destination.hash(identity, APP_NAME, ASPECT).hex()
        except Exception:
            return None
        self.addresses[node_id] = addr
        return addr

    def build_python_config_text(self, node_id, spec):
        links_for_node = spec["links"]
        transport = spec["transport"]
        mode = spec["mode"]
        rate_keys = (("announce_rate_target", spec.get("rate_target")),
                     ("announce_rate_grace", spec.get("rate_grace")),
                     ("announce_rate_penalty", spec.get("rate_penalty")))
        lines = []
        lines.append("[reticulum]")
        lines.append("  enable_transport = " + ("yes" if transport else "no"))
        lines.append("  share_instance = yes")
        lines.append("  instance_name = " + config.INSTANCE_PREFIX + "-" + node_id)
        lines.append("  respond_to_probes = yes")
        lines.append("  panic_on_interface_error = no")
        lines.append("")
        lines.append("[logging]")
        lines.append("  loglevel = " + str(spec.get("loglevel", 4)))
        lines.append("")
        lines.append("[interfaces]")
        for link in links_for_node:
            lines.append("  [[medium-" + link["id"] + "]]")
            lines.append("    type = SimInterface")
            lines.append("    enabled = yes")
            lines.append("    mode = " + link.get("mode", mode))
            lines.append("    node = " + node_id)
            lines.append("    medium = " + link["id"])
            lines.append("    target_host = " + self.hub_host)
            lines.append("    target_port = " + str(self.hub_port))
            lines.append("    mtu = " + str(link["mtu"]))
            lines.append("    bitrate = " + str(link["bitrate"]))
            if spec.get("announce_cap") is not None:
                lines.append("    announce_cap = " + str(spec["announce_cap"]))
            for key, value in rate_keys:
                if value is not None:
                    lines.append("    " + key + " = " + str(value))
        if transport:
            lines.append("  [[bridge]]")
            lines.append("    type = TCPServerInterface")
            lines.append("    enabled = yes")
            lines.append("    listen_ip = " + config.BRIDGE_HOST)
            lines.append("    listen_port = " + str(self.bridge_port(node_id)))
        lines.append("")
        return "\n".join(lines)

    def build_rust_config_text(self, node_id, spec):
        lines = [
            'display_name = "' + node_id.replace('"', '') + '"',
            'announce_capabilities = ["lxmf"]',
            "",
            "[reticulum]",
            "enable_transport = " + ("true" if spec["transport"] else "false"),
            "panic_on_interface_error = false",
            "respond_to_probes = true",
            "",
        ]
        if spec.get("propagation_node"):
            lines.extend([
                "[propagation_node]",
                "enabled = true",
                "node_announce_at_start = true",
                "peer_announce_at_start = true",
                "stamp_cost = 13",
                "",
            ])
        for index, link in enumerate(spec["links"]):
            lines.extend([
                "[[interfaces]]",
                'type = "tcp_client"',
                "enabled = true",
                'name = "medium-' + str(link["id"]).replace('"', '') + '"',
                'host = "127.0.0.1"',
                "port = " + str(self.rust_bridge_port(node_id, index)),
                "mtu = " + str(link["mtu"]),
                "bitrate = " + str(link["bitrate"]),
                'interface_mode = "' + str(link.get("mode", spec["mode"])) + '"',
            ])
            if spec.get("announce_cap") is not None:
                lines.append("announce_cap = " + str(max(1, round(float(spec["announce_cap"])))))
            for key, field in (("announce_rate_target", "rate_target"),
                               ("announce_rate_grace", "rate_grace"),
                               ("announce_rate_penalty", "rate_penalty")):
                if spec.get(field) is not None:
                    lines.append(key + " = " + str(spec[field]))
            lines.append("")
        return "\n".join(lines)

    def build_config_text(self, node_id, spec):
        if spec.get("implementation", "python") == "rust":
            return self.build_rust_config_text(node_id, spec)
        return self.build_python_config_text(node_id, spec)

    def write_node(self, node_id, text, implementation="python"):
        cfg_dir = self.config_dir_for(node_id)
        os.makedirs(cfg_dir, exist_ok=True)
        if implementation == "rust":
            with open(os.path.join(cfg_dir, "reticulum.toml"), "w") as f:
                f.write(text)
        else:
            iface_dir = os.path.join(cfg_dir, "interfaces")
            os.makedirs(iface_dir, exist_ok=True)
            self.ensure_identity(node_id)
            with open(os.path.join(iface_dir, "SimInterface.py"), "w") as f:
                f.write(self.template_code)
            with open(os.path.join(cfg_dir, "config"), "w") as f:
                f.write(text)

    def reader(self, node_id, proc):
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            np = self.nodes.get(node_id)
            if np is not None:
                np.log.append(line)
                if np.logfile is not None:
                    try:
                        np.logfile.write(line + "\n")
                    except Exception:
                        pass
            if self.log_sink is not None:
                try:
                    self.log_sink({"node": node_id, "line": line})
                except Exception:
                    pass
            if np is not None and np.implementation == "rust":
                marker = "delivery destination hash="
                if marker in line:
                    address = line.split(marker, 1)[1].strip().split()[0]
                    np.lxmf_address = address
                    self.lxmf_addresses[node_id] = address
                    if self.msg_sink:
                        self.msg_sink({"type": "lxmf_up", "node": node_id, "address": address})
                propagation_marker = "propagation destination hash="
                if propagation_marker in line:
                    address = line.split(propagation_marker, 1)[1].strip().split()[0]
                    self.propagation_addresses[node_id] = address
                    if self.msg_sink:
                        self.msg_sink({"type": "propagation_up", "node": node_id,
                                       "address": address})
        try:
            proc.stdout.close()
        except Exception:
            pass

    def get_log(self, node_id):
        np = self.nodes.get(node_id)
        if np is None:
            return ""
        return "\n".join(np.log)

    def start_node(self, node_id):
        np = self.nodes.get(node_id)
        if np is None:
            return
        cfg_dir = self.config_dir_for(node_id)
        np.logpath = os.path.join(cfg_dir, "rns.log")
        if np.logfile is not None:
            try:
                np.logfile.close()
            except Exception:
                pass
            np.logfile = None
        try:
            np.logfile = open(np.logpath, "w", buffering=1)
        except Exception:
            np.logfile = None
        env = self.python_reference_env()
        if np.implementation == "rust":
            np.rpc_port = self.rust_rpc_port(node_id)
            env.setdefault("RUST_LOG", "reticulumd=info,reticulum_rs_transport=info")
            command = [
                config.reticulumd_path(),
                "--rpc", config.RUST_RPC_HOST + ":" + str(np.rpc_port),
                "--rpc-unix", os.path.join(cfg_dir, "rpc.sock"),
                "--db", os.path.join(cfg_dir, "reticulum.db"),
                "--identity", os.path.join(cfg_dir, "rust_identity"),
                "--config", os.path.join(cfg_dir, "reticulum.toml"),
            ]
        else:
            command = [config.rnsd_path(), "--config", cfg_dir]
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        np.proc = proc
        np.generation += 1
        thread = threading.Thread(target=self.reader, args=(node_id, proc))
        thread.daemon = True
        thread.start()
        if np.implementation == "rust":
            self.start_rust_event_reader(np)

    def stop_node(self, node_id):
        np = self.nodes.get(node_id)
        if np is None:
            return
        proc = np.proc
        np.proc = None
        self.stop_rust_event_reader(np)
        self.propagation_addresses.pop(node_id, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except Exception:
                pass
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        if np.logfile is not None:
            try:
                np.logfile.close()
            except Exception:
                pass
            np.logfile = None

    def start_bridges(self, np, spec):
        self.stop_bridges(np)
        if np.implementation != "rust":
            return
        if len(spec["links"]) > config.RUST_BRIDGE_STRIDE:
            raise ValueError("Rust node %s has %d media; SIM_RUST_BRIDGE_STRIDE is %d" % (
                np.node_id, len(spec["links"]), config.RUST_BRIDGE_STRIDE))
        for index, link in enumerate(spec["links"]):
            bridge = RustMediumBridge(
                np.node_id, link["id"], "127.0.0.1",
                self.rust_bridge_port(np.node_id, index),
                self.hub_host, self.hub_port,
                mtu=link["mtu"], bitrate=link["bitrate"], log_sink=self.log_sink,
            )
            bridge.start()
            np.bridges.append(bridge)

    def stop_bridges(self, np):
        for bridge in np.bridges:
            bridge.stop()
        np.bridges = []

    def is_running(self, node_id):
        np = self.nodes.get(node_id)
        return np is not None and np.proc is not None and np.proc.poll() is None

    def is_messenger_running(self, node_id):
        np = self.nodes.get(node_id)
        return np is not None and np.messenger is not None and np.messenger.poll() is None

    def start_messenger(self, node_id, interval):
        np = self.nodes.get(node_id)
        if np is None or np.implementation != "python" or self.is_messenger_running(node_id):
            return
        cfg_dir = self.config_dir_for(node_id)
        proc = subprocess.Popen(
            [sys.executable, "-m", "sim.lxmf_agent", "--config", cfg_dir, "--node", node_id,
             "--announce-interval", str(interval)] +
            (["--propagation-node"] if np.spec and np.spec.get("propagation_node") else []),
            cwd=config.BASE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=self.python_reference_env(),
        )
        np.messenger = proc
        thread = threading.Thread(target=self.messenger_reader, args=(node_id, proc))
        thread.daemon = True
        thread.start()

    def stop_messenger(self, node_id):
        np = self.nodes.get(node_id)
        if np is None or np.messenger is None:
            return
        proc = np.messenger
        np.messenger = None
        np.lxmf_address = None
        self.lxmf_addresses.pop(node_id, None)
        self.propagation_addresses.pop(node_id, None)
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except Exception:
            pass

    def send_to_messenger(self, node_id, line):
        if not self.is_messenger_running(node_id):
            return False
        proc = self.nodes[node_id].messenger
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            return True
        except Exception:
            return False

    def messenger_reader(self, node_id, proc):
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if line:
                np = self.nodes.get(node_id)
                if np is not None:
                    np.log.append("[lxmf] " + line)
                self.handle_messenger_line(node_id, line)
        try:
            proc.stdout.close()
        except Exception:
            pass

    def handle_messenger_line(self, node_id, line):
        if line.startswith("EVENT "):
            try:
                event = json.loads(line[6:])
            except (TypeError, ValueError, json.JSONDecodeError):
                event = None
            if isinstance(event, dict):
                event.setdefault("node", node_id)
                if event.get("type") == "message":
                    self.record_delivery(
                        node_id,
                        event.get("text", ""),
                        event=event,
                        transfer_id=event.get("transfer_id"),
                    )
                else:
                    if event.get("type") == "propagation_up" and event.get("address"):
                        self.propagation_addresses[node_id] = event["address"]
                    if self.msg_sink:
                        self.msg_sink(event)
                return
        parts = line.split(" ")
        tag = parts[0]
        if tag == "LXMF" and len(parts) >= 2:
            np = self.nodes.get(node_id)
            if np is not None:
                np.lxmf_address = parts[1]
            self.lxmf_addresses[node_id] = parts[1]
            if self.msg_sink:
                self.msg_sink({"type": "lxmf_up", "node": node_id, "address": parts[1]})
        elif tag == "MSG" and len(parts) >= 3:
            try:
                text = base64.b64decode(parts[2]).decode("utf-8", "replace")
            except Exception:
                text = ""
            if self.msg_sink:
                self.record_delivery(node_id, text, {"from": parts[1]})
        elif tag in ("SENDING", "DELIVERED", "SENDFAIL", "ANNOUNCED", "ANNOUNCEFAIL"):
            if self.msg_sink:
                self.msg_sink({"type": "message_status", "node": node_id, "status": line})
        elif self.log_sink:
            self.log_sink({"node": node_id + "/lxmf", "line": line})

    def start_rust_event_reader(self, np):
        self.stop_rust_event_reader(np)
        np.event_stop = threading.Event()
        np.event_cursor = None
        generation = np.generation
        thread = threading.Thread(
            target=self.rust_event_reader,
            args=(np.node_id, generation, np.event_stop),
            daemon=True,
        )
        np.event_thread = thread
        thread.start()

    def stop_rust_event_reader(self, np):
        np.event_stop.set()
        thread = np.event_thread
        np.event_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        np.event_cursor = None

    def rust_event_reader(self, node_id, generation, stop_event):
        cursor = None
        last_propagation_probe = 0.0
        stop_event.wait((self.node_index(node_id) % 8) * 0.05)
        while not stop_event.is_set():
            np = self.nodes.get(node_id)
            if np is None or np.generation != generation or np.proc is None:
                return
            try:
                now = time.monotonic()
                if (np.spec and np.spec.get("propagation_node")
                        and node_id not in self.propagation_addresses
                        and now - last_propagation_probe >= 5.0):
                    last_propagation_probe = now
                    status = self.rust_call(node_id, "propagation_status", timeout=2) or {}
                    propagation = status.get("propagation", status)
                    address = (propagation.get("destination_hash")
                               or propagation.get("selected_node"))
                    if address:
                        self.propagation_addresses[node_id] = address
                        if self.msg_sink:
                            self.msg_sink({"type": "propagation_up", "node": node_id,
                                           "address": address})
                result = rust_rpc.call(
                    config.RUST_RPC_HOST,
                    np.rpc_port,
                    "sdk_poll_events_v2",
                    {"cursor": cursor, "max": 100},
                    timeout=2,
                ) or {}
                cursor = result.get("next_cursor", cursor)
                np.event_cursor = cursor
                events = result.get("events") or []
                for event in events:
                    if event.get("event_type") != "inbound":
                        continue
                    message = (event.get("payload") or {}).get("message") or {}
                    content = message.get("content")
                    if content is not None:
                        self.record_delivery(node_id, str(content), event=event)
                # Keep enough of the daemon's 120 request/minute budget available
                # for measured sends, path discovery, and propagation operations.
                stop_event.wait(0.5 if events else 2.0)
            except rust_rpc.RustRpcError as error:
                if error.code in ("SDK_RUNTIME_CURSOR_EXPIRED", "SDK_RUNTIME_STREAM_DEGRADED"):
                    cursor = None
                stop_event.wait(2.0 if error.code == "SDK_SECURITY_RATE_LIMITED" else 0.5)
            except (OSError, ConnectionError):
                stop_event.wait(0.5)

    def stop_all_messengers(self):
        with self.lock:
            ids = list(self.nodes.keys())
        for node_id in ids:
            self.stop_messenger(node_id)


    def apply(self, node_specs, active):
        desired = set(node_specs.keys())
        with self.lock:
            current = set(self.nodes.keys())
        for node_id in desired:
            spec = node_specs[node_id]
            text = self.build_config_text(node_id, spec)
            np = self.nodes.get(node_id)
            implementation = spec.get("implementation", "python")
            if np is not None and np.implementation != implementation:
                self.stop_messenger(node_id)
                self.stop_node(node_id)
                self.stop_bridges(np)
                with self.lock:
                    self.nodes.pop(node_id, None)
                self.lxmf_addresses.pop(node_id, None)
                np = None
            if np is None:
                np = NodeProcess(node_id, self.config_dir_for(node_id), implementation)
                with self.lock:
                    self.nodes[node_id] = np
            np.spec = spec
            self.write_node(node_id, text, implementation)
            if active:
                if not self.is_running(node_id):
                    self.start_bridges(np, spec)
                    self.start_node(node_id)
                    np.config_text = text
                elif np.config_text != text:
                    self.stop_messenger(node_id)
                    self.stop_node(node_id)
                    self.start_bridges(np, spec)
                    self.start_node(node_id)
                    np.config_text = text
            else:
                np.config_text = text
        for node_id in current - desired:
            self.stop_messenger(node_id)
            self.stop_node(node_id)
            self.stop_bridges(self.nodes[node_id])
            with self.lock:
                self.nodes.pop(node_id, None)
            self.lxmf_addresses.pop(node_id, None)
            self.propagation_addresses.pop(node_id, None)
            self.addresses.pop(node_id, None)

    def restart_node(self, node_id):
        if node_id in self.nodes:
            np = self.nodes[node_id]
            self.stop_messenger(node_id)
            self.stop_node(node_id)
            if np.spec is not None:
                self.start_bridges(np, np.spec)
            self.start_node(node_id)
            return True
        return False

    def shutdown_node(self, node_id):
        np = self.nodes.get(node_id)
        if np is None:
            return False
        self.stop_messenger(node_id)
        self.stop_node(node_id)
        self.stop_bridges(np)
        return True

    def stop_all(self):
        with self.lock:
            ids = list(self.nodes.keys())
        for node_id in ids:
            self.stop_node(node_id)
            self.stop_bridges(self.nodes[node_id])

    def states(self):
        with self.lock:
            return {nid: self.is_running(nid) for nid in self.nodes}

    def process_inventory(self):
        inventory = []
        with self.lock:
            nodes = list(self.nodes.values())
        for np in nodes:
            if np.proc is not None and np.proc.poll() is None:
                inventory.append({
                    "pid": np.proc.pid,
                    "group": np.implementation,
                    "role": "reticulumd" if np.implementation == "rust" else "rnsd",
                    "node": np.node_id,
                })
            if np.messenger is not None and np.messenger.poll() is None:
                inventory.append({
                    "pid": np.messenger.pid,
                    "group": "python",
                    "role": "lxmf-agent",
                    "node": np.node_id,
                })
        return inventory

    def implementation(self, node_id):
        np = self.nodes.get(node_id)
        return np.implementation if np is not None else None

    def rust_call(self, node_id, method, params=None, timeout=10):
        np = self.nodes.get(node_id)
        if np is None or np.implementation != "rust" or np.rpc_port is None:
            raise rust_rpc.RustRpcError("Rust node is not running")
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                return rust_rpc.call(
                    config.RUST_RPC_HOST, np.rpc_port, method, params,
                    max(0.2, deadline - time.monotonic()),
                )
            except rust_rpc.RustRpcError as error:
                if (error.code != "SDK_SECURITY_RATE_LIMITED"
                        or time.monotonic() >= deadline):
                    raise
                time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
