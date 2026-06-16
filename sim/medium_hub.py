import socket
import struct
import threading
import json
import random

from . import wire


class Connection:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.node = None
        self.medium = None
        self.mtu = 500
        self.bitrate = 9600
        self.send_lock = threading.Lock()


def default_params():
    return {"mtu": 500, "bitrate": 9600, "loss": 0.0, "propagation": 0.0}


def default_stats():
    return {"tx": 0, "rx": 0, "dropped": 0, "oversize": 0}


class MediumHub:
    def __init__(self, host="127.0.0.1", port=5800, event_sink=None):
        self.host = host
        self.port = port
        self.event_sink = event_sink
        self.media = {}
        self.members = {}
        self.stats = {}
        self.connections = []
        self.lock = threading.Lock()
        self.server = None
        self.running = False

    def set_medium_params(self, medium_id, mtu=None, bitrate=None, loss=None, propagation=None):
        with self.lock:
            m = self.media.setdefault(medium_id, default_params())
            if mtu is not None:
                m["mtu"] = int(mtu)
            if bitrate is not None:
                m["bitrate"] = int(bitrate)
            if loss is not None:
                m["loss"] = max(0.0, min(1.0, float(loss)))
            if propagation is not None:
                m["propagation"] = max(0.0, float(propagation))
            self.stats.setdefault(medium_id, default_stats())

    def remove_medium(self, medium_id):
        with self.lock:
            self.media.pop(medium_id, None)
            self.members.pop(medium_id, None)
            self.stats.pop(medium_id, None)

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(256)
        self.running = True
        thread = threading.Thread(target=self.accept_loop)
        thread.daemon = True
        thread.start()

    def accept_loop(self):
        while self.running:
            try:
                sock, addr = self.server.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = Connection(sock, addr)
            with self.lock:
                self.connections.append(conn)
            handler = threading.Thread(target=self.handle_connection, args=(conn,))
            handler.daemon = True
            handler.start()

    def handle_connection(self, conn):
        try:
            while self.running:
                message = wire.read_message(conn.sock)
                if message is None:
                    break
                msg_type, body = message
                if msg_type == wire.MSG_HELLO:
                    self.register(conn, body)
                elif msg_type == wire.MSG_DATA:
                    self.forward(conn, body)
        except Exception:
            pass
        finally:
            self.drop_connection(conn)

    def register(self, conn, body):
        try:
            info = json.loads(body.decode("utf-8"))
        except Exception:
            return
        conn.node = str(info.get("node"))
        conn.medium = str(info.get("medium"))
        conn.mtu = int(info.get("mtu", 500))
        conn.bitrate = int(info.get("bitrate", 9600))
        with self.lock:
            self.media.setdefault(conn.medium, {
                "mtu": conn.mtu,
                "bitrate": conn.bitrate,
                "loss": 0.0,
                "propagation": 0.0,
            })
            self.members.setdefault(conn.medium, set()).add(conn)
            self.stats.setdefault(conn.medium, default_stats())
        self.emit({"type": "link_up", "node": conn.node, "medium": conn.medium})

    def forward(self, conn, data):
        medium_id = conn.medium
        if medium_id is None:
            return
        with self.lock:
            params = dict(self.media.get(medium_id, default_params()))
            targets = [c for c in self.members.get(medium_id, set()) if c is not conn]
            st = self.stats.setdefault(medium_id, default_stats())
            st["tx"] += 1
        size = len(data)
        if size > 0 and (data[0] & 0x03) == 0x01:
            self.emit({"type": "frame", "ptype": "announce", "medium": medium_id, "src": conn.node, "size": size})
        if size > params["mtu"]:
            with self.lock:
                st["oversize"] += 1
                st["dropped"] += 1
            self.emit({"type": "oversize", "medium": medium_id, "node": conn.node, "size": size, "mtu": params["mtu"]})
            return
        bitrate = max(1, params["bitrate"])
        tx_time = (size * 8) / bitrate
        for target in targets:
            if random.random() < params["loss"]:
                with self.lock:
                    st["dropped"] += 1
                self.emit({"type": "drop", "medium": medium_id, "src": conn.node, "dst": target.node, "size": size})
                continue
            delay = tx_time + params["propagation"]
            timer = threading.Timer(delay, self.deliver, args=(target, data, medium_id))
            timer.daemon = True
            timer.start()

    def deliver(self, target, data, medium_id):
        framed = wire.encode(wire.MSG_DATA, data)
        try:
            with target.send_lock:
                target.sock.sendall(framed)
            with self.lock:
                st = self.stats.setdefault(medium_id, default_stats())
                st["rx"] += 1
        except Exception:
            self.drop_connection(target)

    def drop_connection(self, conn):
        node = conn.node
        medium = conn.medium
        with self.lock:
            if conn in self.connections:
                self.connections.remove(conn)
            if medium is not None:
                members = self.members.get(medium)
                if members is not None:
                    members.discard(conn)
        try:
            conn.sock.close()
        except Exception:
            pass
        if node is not None:
            self.emit({"type": "link_down", "node": node, "medium": medium})

    def clear(self):
        with self.lock:
            conns = list(self.connections)
        for conn in conns:
            try:
                conn.sock.close()
            except Exception:
                pass
        with self.lock:
            self.connections = []
            self.media.clear()
            self.members.clear()
            self.stats.clear()

    def node_connected(self, node_id):
        with self.lock:
            for members in self.members.values():
                for c in members:
                    if c.node == node_id:
                        return True
        return False

    def snapshot(self):
        with self.lock:
            result = {}
            for mid in set(list(self.media.keys()) + list(self.members.keys())):
                params = dict(self.media.get(mid, default_params()))
                members = sorted([c.node for c in self.members.get(mid, set()) if c.node is not None])
                result[mid] = {
                    "params": params,
                    "members": members,
                    "stats": dict(self.stats.get(mid, default_stats())),
                }
            return result

    def emit(self, event):
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            self.server.close()
        except Exception:
            pass
        with self.lock:
            conns = list(self.connections)
        for conn in conns:
            try:
                conn.sock.close()
            except Exception:
                pass
