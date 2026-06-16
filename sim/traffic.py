import argparse
import os
import sys
import time

import RNS

APP_NAME = "simnode"
ASPECT = "endpoint"


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


def run_announce(configdir):
    RNS.Reticulum(configdir)
    identity = load_identity(configdir)
    destination = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        ASPECT,
    )
    destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
    destination.accepts_links(True)
    destination.announce()
    emit("ANNOUNCED " + destination.hash.hex())
    time.sleep(4)


def run_recv(configdir, timeout):
    RNS.Reticulum(configdir)
    identity = load_identity(configdir)
    destination = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        ASPECT,
    )
    destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
    destination.accepts_links(True)

    state = {"done": False, "linked": False}

    def resource_concluded(resource):
        if resource.status == RNS.Resource.COMPLETE:
            emit("RECV COMPLETE " + str(resource.get_transfer_size()))
        else:
            emit("RECV FAILED")
        state["done"] = True

    def resource_started(resource):
        emit("RECV STARTED")

    def link_established(link):
        state["linked"] = True
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_started_callback(resource_started)
        link.set_resource_concluded_callback(resource_concluded)

    destination.set_link_established_callback(link_established)
    emit("DEST " + destination.hash.hex())

    deadline = time.time() + timeout
    next_announce = 0.0
    while not state["done"] and time.time() < deadline:
        now = time.time()
        if not state["linked"] and now >= next_announce:
            destination.announce()
            next_announce = now + 60.0
        time.sleep(0.2)
    if not state["done"]:
        emit("RECV TIMEOUT")


def run_send(configdir, dest_hex, size, timeout):
    RNS.Reticulum(configdir)
    dest_hash = bytes.fromhex(dest_hex)
    deadline = time.time() + timeout

    if not RNS.Transport.has_path(dest_hash):
        RNS.Transport.request_path(dest_hash)
        while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
            time.sleep(0.2)

    if not RNS.Transport.has_path(dest_hash):
        emit("SEND NOPATH")
        return

    server_identity = RNS.Identity.recall(dest_hash)
    if server_identity is None:
        emit("SEND NOIDENTITY")
        return

    destination = RNS.Destination(
        server_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        APP_NAME,
        ASPECT,
    )

    state = {"done": False, "last": -1.0}

    def resource_done(resource):
        if resource.status == RNS.Resource.COMPLETE:
            emit("SEND COMPLETE " + str(size))
        else:
            emit("SEND FAILED")
        state["done"] = True

    def resource_progress(resource):
        pct = round(resource.get_progress() * 100.0, 1)
        if pct != state["last"]:
            state["last"] = pct
            emit("SEND PROGRESS " + str(pct))

    def link_established(link):
        emit("LINK UP")
        data = os.urandom(size)
        RNS.Resource(data, link, callback=resource_done, progress_callback=resource_progress)

    def link_closed(link):
        if not state["done"]:
            emit("SEND LINKCLOSED")
            state["done"] = True

    link = RNS.Link(destination)
    link.set_link_established_callback(link_established)
    link.set_link_closed_callback(link_closed)

    while not state["done"] and time.time() < deadline:
        time.sleep(0.2)
    if not state["done"]:
        emit("SEND TIMEOUT")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    p_announce = sub.add_parser("announce")
    p_announce.add_argument("--config", required=True)

    p_recv = sub.add_parser("recv")
    p_recv.add_argument("--config", required=True)
    p_recv.add_argument("--timeout", type=float, default=60.0)

    p_send = sub.add_parser("send")
    p_send.add_argument("--config", required=True)
    p_send.add_argument("--dest", required=True)
    p_send.add_argument("--size", type=int, default=32768)
    p_send.add_argument("--timeout", type=float, default=60.0)

    args = parser.parse_args()
    if args.mode == "announce":
        run_announce(args.config)
    elif args.mode == "recv":
        run_recv(args.config, args.timeout)
    elif args.mode == "send":
        run_send(args.config, args.dest, args.size, args.timeout)


if __name__ == "__main__":
    main()
