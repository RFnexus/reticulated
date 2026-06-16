import struct

MSG_HELLO = 0x01
MSG_DATA = 0x02


def encode(msg_type, body):
    payload = bytes([msg_type]) + body
    return struct.pack(">H", len(payload)) + payload


def recv_exact(sock, n):
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


def read_message(sock):
    header = recv_exact(sock, 2)
    if header is None:
        return None
    length = struct.unpack(">H", header)[0]
    payload = recv_exact(sock, length)
    if payload is None:
        return None
    if len(payload) == 0:
        return (None, b"")
    return (payload[0], payload[1:])
