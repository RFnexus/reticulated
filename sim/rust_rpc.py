"""Client for the LXMF-rs reticulumd MessagePack-over-HTTP RPC."""

import http.client
import itertools
import msgpack

_IDS = itertools.count(1)


class RustRpcError(RuntimeError):
    def __init__(self, message, code="RPC_ERROR"):
        super().__init__(message)
        self.code = code


def _frame(value):
    payload = msgpack.packb(value, use_bin_type=True)
    return len(payload).to_bytes(4, "big") + payload


def _unframe(data):
    if len(data) < 4:
        raise RustRpcError("short RPC response")
    length = int.from_bytes(data[:4], "big")
    if len(data) < length + 4:
        raise RustRpcError("incomplete RPC response")
    return msgpack.unpackb(data[4:4 + length], raw=False)


def call(host, port, method, params=None, timeout=10):
    request = {"id": next(_IDS), "method": method, "params": params}
    body = _frame(request)
    connection = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        connection.request("POST", "/rpc", body=body,
                           headers={"Content-Type": "application/msgpack"})
        response = connection.getresponse()
        data = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise RustRpcError("RPC HTTP status %s" % response.status)
    decoded = _unframe(data)
    # rmp-serde's compact struct representation is a positional tuple unless
    # the producer explicitly selects named maps. RpcResponse fields are
    # (id, result, error).
    if isinstance(decoded, list) and len(decoded) == 3:
        decoded = {"id": decoded[0], "result": decoded[1], "error": decoded[2]}
    if not isinstance(decoded, dict):
        raise RustRpcError("invalid RPC response shape")
    error = decoded.get("error")
    if error:
        if isinstance(error, dict):
            code = error.get("code", "RPC_ERROR")
            message = error.get("message", "unknown error")
        elif isinstance(error, list) and len(error) >= 2:
            code, message = str(error[0]), str(error[1])
        else:
            code, message = "RPC_ERROR", str(error)
        raise RustRpcError("%s: %s" % (code, message), code=code)
    return decoded.get("result")
