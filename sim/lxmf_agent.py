import argparse
import os
import sys
import time
import random
import threading
import base64

import RNS
import LXMF


def emit(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def load_identity(configdir):
    idpath = os.path.join(configdir, "sim_identity")
    if os.path.isfile(idpath):
        identity = RNS.Identity.from_file(idpath)
        if identity is not None:
            return identity
    identity = RNS.Identity()
    identity.to_file(idpath)
    return identity


def method_name(m):
    return {LXMF.LXMessage.OPPORTUNISTIC: "opportunistic", LXMF.LXMessage.DIRECT: "direct", LXMF.LXMessage.PROPAGATED: "propagated"}.get(m, "unknown")


def repr_name(r):
    return {LXMF.LXMessage.PACKET: "packet", LXMF.LXMessage.RESOURCE: "resource"}.get(r, "unknown")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--node", default="")
    parser.add_argument("--announce-interval", type=float, default=30.0)
    args = parser.parse_args()

    interval = {"v": args.announce_interval}

    RNS.Reticulum(args.config)
    identity = load_identity(args.config)
    storagepath = os.path.join(args.config, "lxmf")
    os.makedirs(storagepath, exist_ok=True)

    router = LXMF.LXMRouter(identity=identity, storagepath=storagepath, autopeer=False)
    source_dest = router.register_delivery_identity(identity, display_name=args.node)

    def inbound(message):
        try:
            content = bytes(message.content)
        except Exception:
            content = b""
        src = message.source_hash.hex() if message.source_hash else ""
        emit("MSG " + src + " " + base64.b64encode(content).decode("ascii"))

    router.register_delivery_callback(inbound)

    def do_announce():
        try:
            router.announce(source_dest.hash)
            emit("ANNOUNCED " + source_dest.hash.hex())
        except Exception as e:
            emit("ANNOUNCEFAIL " + str(e))

    def send_message(dst_hex, text):
        try:
            dst_hash = bytes.fromhex(dst_hex)
        except Exception:
            emit("SENDFAIL badhash")
            return
        deadline = time.time() + 30
        if not RNS.Transport.has_path(dst_hash):
            RNS.Transport.request_path(dst_hash)
            while not RNS.Transport.has_path(dst_hash) and time.time() < deadline:
                time.sleep(0.2)
        if not RNS.Transport.has_path(dst_hash):
            emit("SENDFAIL nopath")
            return
        recipient = RNS.Identity.recall(dst_hash)
        if recipient is None:
            emit("SENDFAIL noidentity")
            return
        out_dest = RNS.Destination(recipient, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        payload = text.encode("utf-8")
        lxm = LXMF.LXMessage(out_dest, source_dest, payload, "", desired_method=LXMF.LXMessage.DIRECT)
        t0 = time.time()
        done = {"v": False}

        def delivered(m):
            done["v"] = True
            emit("DELIVERED %s rtt=%.3f method=%s via=%s size=%d attempts=%d" % (
                dst_hex, time.time() - t0, method_name(m.method), repr_name(m.representation),
                len(payload), getattr(m, "delivery_attempts", 0)))

        def failed(m):
            done["v"] = True
            emit("SENDFAIL delivery attempts=%d" % getattr(m, "delivery_attempts", 0))

        lxm.register_delivery_callback(delivered)
        lxm.register_failed_callback(failed)
        router.handle_outbound(lxm)
        emit("SENDING %s size=%d" % (dst_hex, len(payload)))
        last = 1
        end = time.time() + 120
        while not done["v"] and time.time() < end:
            time.sleep(1)
            attempts = getattr(lxm, "delivery_attempts", 0)
            if attempts > last:
                last = attempts
                emit("RETRY %s attempt=%d" % (dst_hex, attempts))
            if lxm.state in (LXMF.LXMessage.DELIVERED, LXMF.LXMessage.FAILED):
                break

    def reader():
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split(" ")
            cmd = parts[0]
            if cmd == "announce":
                do_announce()
            elif cmd == "interval" and len(parts) >= 2:
                try:
                    interval["v"] = float(parts[1])
                except Exception:
                    pass
            elif cmd == "send" and len(parts) >= 3:
                try:
                    text = base64.b64decode(parts[2]).decode("utf-8", "replace")
                except Exception:
                    text = ""
                threading.Thread(target=send_message, args=(parts[1], text), daemon=True).start()
            elif cmd == "quit":
                os._exit(0)

    emit("LXMF " + source_dest.hash.hex())
    threading.Thread(target=reader, daemon=True).start()

    last = time.time()
    if interval["v"] > 0:
        last -= random.uniform(0, interval["v"])
    while True:
        time.sleep(1)
        iv = interval["v"]
        if iv > 0 and (time.time() - last) >= iv:
            do_announce()
            last = time.time()


if __name__ == "__main__":
    main()
