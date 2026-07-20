"""HDLC-to-Reticulated adapter for LXMF-rs TCP interfaces.

LXMF-rs speaks the standard Reticulum HDLC TCP framing.  Reticulated's hub
uses a small length-prefixed envelope around otherwise untouched RNS packets.
This adapter translates only those interface envelopes; packet bytes are never
decoded or modified.
"""

import json
import errno
import socket
import threading
import time

from . import wire

HDLC_FLAG = 0x7E
HDLC_ESCAPE = 0x7D
HDLC_XOR = 0x20


def hdlc_encode(payload):
    framed = bytearray([HDLC_FLAG])
    for value in payload:
        if value in (HDLC_FLAG, HDLC_ESCAPE):
            framed.extend((HDLC_ESCAPE, value ^ HDLC_XOR))
        else:
            framed.append(value)
    framed.append(HDLC_FLAG)
    return bytes(framed)


class HdlcDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.escaped = False
        self.in_frame = False

    def feed(self, data):
        frames = []
        for value in data:
            if value == HDLC_FLAG:
                if self.in_frame and self.buffer:
                    frames.append(bytes(self.buffer))
                self.buffer.clear()
                self.escaped = False
                self.in_frame = True
            elif not self.in_frame:
                continue
            elif self.escaped:
                self.buffer.append(value ^ HDLC_XOR)
                self.escaped = False
            elif value == HDLC_ESCAPE:
                self.escaped = True
            else:
                self.buffer.append(value)
        return frames


class RustMediumBridge:
    def __init__(self, node_id, medium_id, listen_host, listen_port,
                 hub_host, hub_port, mtu=500, bitrate=9600, log_sink=None):
        self.node_id = str(node_id)
        self.medium_id = str(medium_id)
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.hub_host = hub_host
        self.hub_port = int(hub_port)
        self.mtu = int(mtu)
        self.bitrate = int(bitrate)
        self.log_sink = log_sink
        self.running = False
        self.server = None
        self.thread = None
        self.sockets = set()
        self.lock = threading.Lock()

    def log(self, line):
        if self.log_sink:
            self.log_sink({"node": self.node_id + "/bridge", "line": line})

    def start(self):
        if self.running:
            return
        server = None
        for attempt in range(20):
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                candidate.bind((self.listen_host, self.listen_port))
                server = candidate
                break
            except OSError as error:
                candidate.close()
                if error.errno != errno.EADDRINUSE or attempt == 19:
                    raise
                time.sleep(0.05)
        if server is None:
            raise OSError(errno.EADDRINUSE, "adapter port did not become available")
        server.listen(4)
        server.settimeout(0.5)
        self.server = server
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                peer, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            thread = threading.Thread(target=self._session, args=(peer,), daemon=True)
            thread.start()

    def _connect_hub(self):
        while self.running:
            try:
                hub = socket.create_connection((self.hub_host, self.hub_port), timeout=2)
                hub.settimeout(None)
                hello = json.dumps({
                    "node": self.node_id,
                    "medium": self.medium_id,
                    "mtu": self.mtu,
                    "bitrate": self.bitrate,
                    "implementation": "rust",
                }).encode("utf-8")
                hub.sendall(wire.encode(wire.MSG_HELLO, hello))
                return hub
            except OSError:
                time.sleep(0.25)
        return None

    def _session(self, peer):
        hub = self._connect_hub()
        if hub is None:
            peer.close()
            return
        with self.lock:
            self.sockets.update((peer, hub))
        stopped = threading.Event()

        def rust_to_hub():
            decoder = HdlcDecoder()
            try:
                while self.running and not stopped.is_set():
                    data = peer.recv(65536)
                    if not data:
                        break
                    for frame in decoder.feed(data):
                        hub.sendall(wire.encode(wire.MSG_DATA, frame))
            except OSError:
                pass
            finally:
                stopped.set()

        def hub_to_rust():
            try:
                while self.running and not stopped.is_set():
                    message = wire.read_message(hub)
                    if message is None:
                        break
                    msg_type, body = message
                    if msg_type == wire.MSG_DATA:
                        peer.sendall(hdlc_encode(body))
            except OSError:
                pass
            finally:
                stopped.set()

        threads = [threading.Thread(target=rust_to_hub, daemon=True),
                   threading.Thread(target=hub_to_rust, daemon=True)]
        for thread in threads:
            thread.start()
        stopped.wait()
        for sock in (peer, hub):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self.lock:
            self.sockets.discard(peer)
            self.sockets.discard(hub)

    def stop(self):
        self.running = False
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        self.thread = None
        with self.lock:
            sockets = list(self.sockets)
            self.sockets.clear()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
