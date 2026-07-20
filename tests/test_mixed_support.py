import socket
import unittest

from sim.medium_hub import Connection, MediumHub
from sim.rust_bridge import HdlcDecoder, RustMediumBridge, hdlc_encode
from sim.rust_rpc import _frame, _unframe
from sim.scenario import _generated_topology
from sim.topology import Topology


class MixedSupportTests(unittest.TestCase):
    def test_hdlc_roundtrip_preserves_packet_bytes(self):
        packet = bytes([0x7E, 0x7D, 0x00, 0xFF, 0x7E])
        decoder = HdlcDecoder()
        encoded = hdlc_encode(packet)
        frames = decoder.feed(encoded[:3]) + decoder.feed(encoded[3:])
        self.assertEqual(frames, [packet])

    def test_rpc_frame_roundtrip(self):
        request = {"id": 7, "method": "status", "params": None}
        self.assertEqual(_unframe(_frame(request)), request)

    def test_compact_rpc_response_shape_is_documented(self):
        response = _unframe(_frame([7, {"running": True}, None]))
        self.assertEqual(response, [7, {"running": True}, None])

    def test_topology_defaults_old_nodes_to_python(self):
        topology = Topology()
        topology.from_dict({"nodes": {"n0": {"label": "old"}}, "links": {}})
        self.assertEqual(topology.nodes["n0"]["implementation"], "python")

    def test_generated_hundred_node_topology_is_mixed_and_multihop(self):
        data = _generated_topology({"nodes": 100, "rust_nodes": 50, "transport_nodes": 10})
        implementations = [node["implementation"] for node in data["nodes"].values()]
        transports = [node for node in data["nodes"].values() if node["transport"]]
        self.assertEqual(len(data["nodes"]), 100)
        self.assertEqual(implementations.count("rust"), 50)
        self.assertEqual(implementations.count("python"), 50)
        self.assertEqual({node["implementation"] for node in transports}, {"python", "rust"})
        self.assertEqual(len(data["links"]), 99)

    def test_medium_disconnect_and_partition_are_reported(self):
        hub = MediumHub(port=0)
        hub.set_medium_params("m0")
        hub.set_connected("n0", "m0", False)
        hub.partition("m0", [["n0"], ["n1"]])
        snapshot = hub.snapshot()["m0"]
        self.assertEqual(snapshot["disconnected"], ["n0"])
        self.assertNotEqual(snapshot["partitions"]["n0"], snapshot["partitions"]["n1"])
        hub.recover_partition("m0")
        self.assertEqual(hub.snapshot()["m0"]["partitions"], {})

    def test_partition_blocks_cross_group_forwarding(self):
        hub = MediumHub(port=0)
        hub.set_medium_params("m0", bitrate=10_000_000)
        source = Connection(None, None)
        source.node, source.medium = "n0", "m0"
        target = Connection(None, None)
        target.node, target.medium = "n1", "m0"
        hub.members["m0"] = {source, target}
        hub.partition("m0", [["n0"], ["n1"]])

        hub.forward(source, b"blocked")

        self.assertEqual(hub.stats["m0"]["dropped"], 1)
        self.assertEqual(hub.stats["m0"]["rx"], 0)
        hub.recover_partition("m0")
        self.assertTrue(hub._can_deliver("m0", "n0", "n1"))

    def test_rust_bridge_port_can_be_rebound_after_stop(self):
        reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        reservation.close()
        first = RustMediumBridge("n0", "m0", "127.0.0.1", port,
                                 "127.0.0.1", port + 1)
        first.start()
        first.stop()
        second = RustMediumBridge("n0", "m0", "127.0.0.1", port,
                                  "127.0.0.1", port + 1)
        second.start()
        second.stop()


if __name__ == "__main__":
    unittest.main()
