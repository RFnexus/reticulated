import RNS
import socket
import struct
import threading
import time
import json

from RNS.Interfaces.Interface import Interface

MSG_HELLO = 0x01
MSG_DATA = 0x02


class SimInterface(Interface):
    DEFAULT_IFAC_SIZE = 16
    AUTOCONFIGURE_MTU = False
    FIXED_MTU = False

    def __init__(self, owner, configuration):
        super().__init__()
        c = Interface.get_config_obj(configuration)
        self.owner = owner
        self.name = c["name"]
        self.medium = str(c["medium"]) if "medium" in c else self.name
        self.target_host = str(c["target_host"]) if "target_host" in c else "127.0.0.1"
        self.target_port = int(c["target_port"]) if "target_port" in c else 5800
        self.node = str(c["node"]) if "node" in c else self.name

        mtu = int(c["mtu"]) if "mtu" in c else 500
        bitrate = int(c["bitrate"]) if "bitrate" in c else 9600

        self.HW_MTU = mtu
        self.bitrate = bitrate
        self.IN = True
        self.OUT = False
        self.online = False
        self.detached = False

        self.socket = None
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()

        self.connect()

        reader = threading.Thread(target=self.read_loop)
        reader.daemon = True
        reader.start()

    def encode(self, msg_type, body):
        payload = bytes([msg_type]) + body
        return struct.pack(">H", len(payload)) + payload

    def connect(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.connect((self.target_host, self.target_port))
            hello = json.dumps({
                "node": self.node,
                "medium": self.medium,
                "mtu": self.HW_MTU,
                "bitrate": self.bitrate,
            }).encode("utf-8")
            s.sendall(self.encode(MSG_HELLO, hello))
            with self.state_lock:
                self.socket = s
                self.online = True
            return True
        except Exception:
            with self.state_lock:
                self.socket = None
                self.online = False
            return False

    def recv_exact(self, sock, n):
        data = b""
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def read_loop(self):
        while not self.detached:
            with self.state_lock:
                sock = self.socket
            if sock is None:
                if not self.connect():
                    time.sleep(1)
                continue
            header = self.recv_exact(sock, 2)
            if header is None:
                self.handle_disconnect(sock)
                continue
            length = struct.unpack(">H", header)[0]
            payload = self.recv_exact(sock, length)
            if payload is None:
                self.handle_disconnect(sock)
                continue
            if len(payload) == 0:
                continue
            msg_type = payload[0]
            body = payload[1:]
            if msg_type == MSG_DATA:
                self.rxb += len(body)
                self.owner.inbound(body, self)

    def handle_disconnect(self, sock):
        with self.state_lock:
            if self.socket is sock:
                self.socket = None
                self.online = False
        try:
            sock.close()
        except Exception:
            pass
        if not self.detached:
            time.sleep(1)

    def process_incoming(self, data):
        self.rxb += len(data)
        self.owner.inbound(data, self)

    def process_outgoing(self, data):
        with self.state_lock:
            sock = self.socket
        if sock is None:
            return
        try:
            framed = self.encode(MSG_DATA, data)
            with self.send_lock:
                sock.sendall(framed)
            self.txb += len(data)
        except Exception:
            self.handle_disconnect(sock)

    def detach(self):
        self.detached = True
        with self.state_lock:
            self.online = False
            sock = self.socket
            self.socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def __str__(self):
        return "SimInterface[" + self.name + "/" + self.medium + "]"


interface_class = SimInterface
