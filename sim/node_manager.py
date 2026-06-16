import os
import sys
import signal
import subprocess
import threading
import collections
import time
import base64

import RNS

from . import config

APP_NAME = "simnode"
ASPECT = "endpoint"


class NodeProcess:
    def __init__(self, node_id, config_dir):
        self.node_id = node_id
        self.config_dir = config_dir
        self.proc = None
        self.config_text = None
        self.log = collections.deque(maxlen=400)
        self.logpath = None
        self.logfile = None
        self.messenger = None
        self.lxmf_address = None


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
        self.lock = threading.Lock()

    def config_dir_for(self, node_id):
        return os.path.join(config.NODES_DIR, node_id)

    def bridge_port(self, node_id):
        digits = "".join(ch for ch in node_id if ch.isdigit())
        try:
            index = int(digits)
        except Exception:
            index = 0
        return config.TCP_BASE + index

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

    def build_config_text(self, node_id, spec):
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
            lines.append("    mode = " + mode)
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

    def write_node(self, node_id, text):
        cfg_dir = self.config_dir_for(node_id)
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
        try:
            proc.stdout.close()
        except Exception:
            pass

    def get_log(self, node_id):
        np = self.nodes.get(node_id)
        if np is None:
            return ""
        if np.logpath and os.path.isfile(np.logpath):
            try:
                with open(np.logpath, errors="replace") as f:
                    return f.read()
            except Exception:
                pass
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
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [config.rnsd_path(), "--config", cfg_dir],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        np.proc = proc
        thread = threading.Thread(target=self.reader, args=(node_id, proc))
        thread.daemon = True
        thread.start()

    def stop_node(self, node_id):
        np = self.nodes.get(node_id)
        if np is None:
            return
        proc = np.proc
        np.proc = None
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

    def is_running(self, node_id):
        np = self.nodes.get(node_id)
        return np is not None and np.proc is not None and np.proc.poll() is None

    def is_messenger_running(self, node_id):
        np = self.nodes.get(node_id)
        return np is not None and np.messenger is not None and np.messenger.poll() is None

    def start_messenger(self, node_id, interval):
        np = self.nodes.get(node_id)
        if np is None or self.is_messenger_running(node_id):
            return
        cfg_dir = self.config_dir_for(node_id)
        proc = subprocess.Popen(
            [sys.executable, "-m", "sim.lxmf_agent", "--config", cfg_dir, "--node", node_id,
             "--announce-interval", str(interval)],
            cwd=config.BASE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
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
                self.handle_messenger_line(node_id, line)
        try:
            proc.stdout.close()
        except Exception:
            pass

    def handle_messenger_line(self, node_id, line):
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
                self.msg_sink({"type": "message", "node": node_id, "from": parts[1], "text": text})
        elif tag in ("SENDING", "DELIVERED", "SENDFAIL", "ANNOUNCED", "ANNOUNCEFAIL"):
            if self.msg_sink:
                self.msg_sink({"type": "message_status", "node": node_id, "status": line})
        elif self.log_sink:
            self.log_sink({"node": node_id + "/lxmf", "line": line})

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
            if np is None:
                np = NodeProcess(node_id, self.config_dir_for(node_id))
                with self.lock:
                    self.nodes[node_id] = np
            self.write_node(node_id, text)
            if active:
                if not self.is_running(node_id):
                    self.start_node(node_id)
                    np.config_text = text
                elif np.config_text != text:
                    self.stop_messenger(node_id)
                    self.stop_node(node_id)
                    self.start_node(node_id)
                    np.config_text = text
            else:
                np.config_text = text
        for node_id in current - desired:
            self.stop_messenger(node_id)
            self.stop_node(node_id)
            with self.lock:
                self.nodes.pop(node_id, None)

    def restart_node(self, node_id):
        if node_id in self.nodes:
            self.stop_node(node_id)
            self.start_node(node_id)

    def stop_all(self):
        with self.lock:
            ids = list(self.nodes.keys())
        for node_id in ids:
            self.stop_node(node_id)

    def states(self):
        with self.lock:
            return {nid: self.is_running(nid) for nid in self.nodes}
