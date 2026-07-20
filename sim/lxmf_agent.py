import argparse
import json
import os
import re
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


TRANSFER_ID = re.compile(r"reticulated:([0-9a-f]{32}):")


def emit_event(event):
    emit("EVENT " + json.dumps(event, separators=(",", ":"), sort_keys=True))


def transfer_id_for(text):
    match = TRANSFER_ID.search(text)
    return match.group(1) if match else None


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
    parser.add_argument("--announce-interval", type=float, default=300.0)
    parser.add_argument("--propagation-node", action="store_true")
    args = parser.parse_args()

    interval = {"v": args.announce_interval}

    RNS.Reticulum(args.config)
    identity = load_identity(args.config)
    storagepath = os.path.join(args.config, "lxmf")
    os.makedirs(storagepath, exist_ok=True)

    router = LXMF.LXMRouter(
        identity=identity,
        storagepath=storagepath,
        autopeer=False,
        propagation_cost=LXMF.LXMRouter.PROPAGATION_COST_MIN,
    )
    source_dest = router.register_delivery_identity(identity, display_name=args.node)

    def inbound(message):
        try:
            content = bytes(message.content)
        except Exception:
            content = b""
        src = message.source_hash.hex() if message.source_hash else ""
        text = content.decode("utf-8", "replace")
        emit_event({
            "type": "message",
            "from": src,
            "text": text,
            "transfer_id": transfer_id_for(text),
        })

    router.register_delivery_callback(inbound)

    if args.propagation_node:
        router.enable_propagation()
        emit_event({"type": "propagation_up",
                    "address": router.propagation_destination.hash.hex()})

        def watch_propagation_store():
            seen = set()
            while True:
                try:
                    names = os.listdir(router.messagepath)
                except OSError:
                    names = []
                for name in names:
                    path = os.path.join(router.messagepath, name)
                    if path in seen or not os.path.isfile(path):
                        continue
                    try:
                        with open(path, "rb") as stream:
                            stored = stream.read()
                        payload = stored[:-LXMF.LXStamper.STAMP_SIZE]
                        message = LXMF.LXMessage.unpack_from_bytes(
                            payload, original_method=LXMF.LXMessage.PROPAGATED)
                        text = message.content_as_string()
                    except Exception:
                        continue
                    seen.add(path)
                    if message.destination_hash == source_dest.hash:
                        emit_event({
                            "type": "message",
                            "from": message.source_hash.hex() if message.source_hash else "",
                            "text": text,
                            "transfer_id": transfer_id_for(text),
                            "storage": "propagation",
                            "diagnostic_path": path,
                        })
                time.sleep(0.2)

        threading.Thread(target=watch_propagation_store, daemon=True).start()

    def do_announce():
        try:
            router.announce(source_dest.hash)
            emit("ANNOUNCED " + source_dest.hash.hex())
        except Exception as e:
            emit("ANNOUNCEFAIL " + str(e))

    def send_message(dst_hex, text, mode="direct", transfer_id=None):
        try:
            dst_hash = bytes.fromhex(dst_hex)
        except Exception:
            emit("SENDFAIL badhash")
            return
        deadline = time.time() + 30
        if mode == "direct" and not RNS.Transport.has_path(dst_hash):
            RNS.Transport.request_path(dst_hash)
            while not RNS.Transport.has_path(dst_hash) and time.time() < deadline:
                time.sleep(0.2)
        if mode == "direct" and not RNS.Transport.has_path(dst_hash):
            emit("SENDFAIL nopath")
            return
        if mode == "propagated":
            propagation_hash = router.outbound_propagation_node
            if propagation_hash is None:
                emit("SENDFAIL no-propagation-node")
                return
            if not RNS.Transport.has_path(propagation_hash):
                RNS.Transport.request_path(propagation_hash)
            while not RNS.Transport.has_path(propagation_hash) and time.time() < deadline:
                time.sleep(0.2)
            if not RNS.Transport.has_path(propagation_hash):
                emit("SENDFAIL no-propagation-path")
                return
            while RNS.Identity.recall(propagation_hash) is None and time.time() < deadline:
                time.sleep(0.2)
        recipient = RNS.Identity.recall(dst_hash)
        while recipient is None and time.time() < deadline:
            time.sleep(0.2)
            recipient = RNS.Identity.recall(dst_hash)
        if recipient is None:
            emit("SENDFAIL noidentity")
            return
        out_dest = RNS.Destination(recipient, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        payload = text.encode("utf-8")
        desired_method = {
            "direct": LXMF.LXMessage.DIRECT,
            "opportunistic": LXMF.LXMessage.OPPORTUNISTIC,
            "propagated": LXMF.LXMessage.PROPAGATED,
        }.get(mode, LXMF.LXMessage.DIRECT)
        lxm = LXMF.LXMessage(out_dest, source_dest, payload, "", desired_method=desired_method)
        t0 = time.monotonic()
        if mode == "propagated":
            propagation_cost = LXMF.LXMRouter.PROPAGATION_COST_MIN
            if not lxm.transient_id:
                lxm.pack()
            workblock = LXMF.LXStamper.stamp_workblock(
                lxm.transient_id,
                expand_rounds=LXMF.LXStamper.WORKBLOCK_EXPAND_ROUNDS_PN,
            )
            propagation_stamp, _ = LXMF.LXStamper.job_simple(
                propagation_cost, workblock, lxm.transient_id)
            if propagation_stamp is None:
                emit("SENDFAIL propagation-stamp")
                return
            lxm.propagation_stamp = propagation_stamp
            lxm.propagation_target_cost = propagation_cost
            lxm.propagation_stamp_value = LXMF.LXStamper.stamp_value(
                workblock, propagation_stamp)
            lxm.propagation_stamp_valid = True
            lxm.packed = None
            lxm.pack()
            lxm.defer_propagation_stamp = False
        done = {"v": False}

        def delivered(m):
            done["v"] = True
            emit_event({
                "type": "message_status",
                "status": "delivered",
                "destination": dst_hex,
                "transfer_id": transfer_id,
                "rtt_seconds": time.monotonic() - t0,
                "method": method_name(m.method),
                "representation": repr_name(m.representation),
                "bytes": len(payload),
                "attempts": getattr(m, "delivery_attempts", 0),
            })

        def failed(m):
            done["v"] = True
            emit_event({
                "type": "message_status",
                "status": "failed",
                "destination": dst_hex,
                "transfer_id": transfer_id,
                "attempts": getattr(m, "delivery_attempts", 0),
            })

        lxm.register_delivery_callback(delivered)
        lxm.register_failed_callback(failed)
        router.handle_outbound(lxm)
        emit_event({
            "type": "message_status",
            "status": "sending",
            "destination": dst_hex,
            "transfer_id": transfer_id,
            "mode": mode,
            "bytes": len(payload),
        })
        last = 1
        last_state = None
        end = time.time() + 120
        while not done["v"] and time.time() < end:
            time.sleep(1)
            if lxm.state != last_state:
                last_state = lxm.state
                emit_event({"type": "message_status", "status": "state",
                            "state": int(lxm.state), "transfer_id": transfer_id,
                            "method": method_name(lxm.method)})
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
            elif cmd == "send_v2" and len(parts) >= 5:
                try:
                    text = base64.b64decode(parts[4]).decode("utf-8", "replace")
                except Exception:
                    text = ""
                threading.Thread(
                    target=send_message,
                    args=(parts[1], text, parts[2], parts[3]),
                    daemon=True,
                ).start()
            elif cmd == "propagation" and len(parts) >= 2:
                try:
                    router.set_outbound_propagation_node(bytes.fromhex(parts[1]))
                    emit_event({"type": "propagation_selected", "address": parts[1]})
                except Exception as error:
                    emit_event({"type": "propagation_failed", "error": str(error)})
            elif cmd == "propagation_sync":
                try:
                    router.request_messages_from_propagation_node(identity)
                    emit_event({"type": "propagation_sync", "status": "requested"})
                except Exception as error:
                    emit_event({"type": "propagation_sync", "status": "failed",
                                "error": str(error)})
            elif cmd == "propagation_announce":
                try:
                    router.announce_propagation_node()
                    emit_event({"type": "propagation_announce", "status": "announced"})
                except Exception as error:
                    emit_event({"type": "propagation_announce", "status": "failed",
                                "error": str(error)})
            elif cmd == "request_path" and len(parts) >= 2:
                try:
                    destination_hash = bytes.fromhex(parts[1])
                    timeout = float(parts[2]) if len(parts) >= 3 else 30.0
                    if not RNS.Transport.has_path(destination_hash):
                        RNS.Transport.request_path(destination_hash)
                    deadline = time.monotonic() + timeout
                    while (not RNS.Transport.has_path(destination_hash)
                           and time.monotonic() < deadline):
                        time.sleep(0.05)
                    emit_event({
                        "type": "path_discovery",
                        "destination": parts[1],
                        "status": "found" if RNS.Transport.has_path(destination_hash) else "timeout",
                    })
                except Exception as error:
                    emit_event({"type": "path_discovery", "destination": parts[1],
                                "status": "failed", "error": str(error)})
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
