import json
import os

from . import config


class Topology:
    def __init__(self):
        self.nodes = {}
        self.links = {}
        self.node_seq = 0
        self.link_seq = 0
        self.settings = {"announce_interval": 300.0, "announce_cap": 2.0, "loglevel": 4}

    def add_node(self, label=None, x=0.0, y=0.0):
        node_id = "n" + str(self.node_seq)
        self.node_seq += 1
        self.nodes[node_id] = {
            "label": label if label else node_id,
            "x": float(x),
            "y": float(y),
            "transport": False,
            "mode": "full",
        }
        return node_id

    def remove_node(self, node_id):
        self.nodes.pop(node_id, None)
        empty = []
        for link_id, link in self.links.items():
            if node_id in link["members"]:
                link["members"] = [m for m in link["members"] if m != node_id]
            if len(link["members"]) == 0:
                empty.append(link_id)
        for link_id in empty:
            self.links.pop(link_id, None)
        return empty

    INTERFACE_MODES = ["full", "gateway", "access_point", "roaming", "boundary", "ptp"]

    def set_node_transport(self, node_id, transport):
        node = self.nodes.get(node_id)
        if node is None:
            return False
        node["transport"] = bool(transport)
        return True

    def set_node_mode(self, node_id, mode):
        node = self.nodes.get(node_id)
        if node is None or mode not in Topology.INTERFACE_MODES:
            return False
        node["mode"] = mode
        return True

    def set_node_field(self, node_id, key, value):
        node = self.nodes.get(node_id)
        if node is None:
            return False
        if value is None:
            node.pop(key, None)
        else:
            node[key] = value
        return True

    def update_node(self, node_id, label=None, x=None, y=None):
        node = self.nodes.get(node_id)
        if node is None:
            return False
        if label is not None:
            node["label"] = label
        if x is not None:
            node["x"] = float(x)
        if y is not None:
            node["y"] = float(y)
        return True

    def add_link(self, members, mtu=None, bitrate=None, loss=None, propagation=None, x=0.0, y=0.0):
        link_id = "l" + str(self.link_seq)
        self.link_seq += 1
        self.links[link_id] = {
            "members": [m for m in members if m in self.nodes],
            "mtu": int(mtu) if mtu is not None else config.DEFAULT_MTU,
            "bitrate": int(bitrate) if bitrate is not None else config.DEFAULT_BITRATE,
            "loss": float(loss) if loss is not None else config.DEFAULT_LOSS,
            "propagation": float(propagation) if propagation is not None else config.DEFAULT_PROPAGATION,
            "x": float(x),
            "y": float(y),
        }
        return link_id

    def remove_link(self, link_id):
        return self.links.pop(link_id, None) is not None

    def set_link_params(self, link_id, mtu=None, bitrate=None, loss=None, propagation=None):
        link = self.links.get(link_id)
        if link is None:
            return False
        if mtu is not None:
            link["mtu"] = max(config.MIN_MTU, min(config.MAX_MTU, int(mtu)))
        if bitrate is not None:
            link["bitrate"] = max(1, int(bitrate))
        if loss is not None:
            link["loss"] = max(0.0, min(1.0, float(loss)))
        if propagation is not None:
            link["propagation"] = max(0.0, float(propagation))
        return True

    def set_link_members(self, link_id, members):
        link = self.links.get(link_id)
        if link is None:
            return False
        link["members"] = [m for m in members if m in self.nodes]
        return True

    def update_link(self, link_id, x=None, y=None):
        link = self.links.get(link_id)
        if link is None:
            return False
        if x is not None:
            link["x"] = float(x)
        if y is not None:
            link["y"] = float(y)
        return True

    def to_dict(self):
        return {
            "nodes": self.nodes,
            "links": self.links,
            "node_seq": self.node_seq,
            "link_seq": self.link_seq,
            "settings": self.settings,
        }

    def from_dict(self, data):
        self.nodes = data.get("nodes", {})
        self.links = data.get("links", {})
        self.node_seq = data.get("node_seq", len(self.nodes))
        self.link_seq = data.get("link_seq", len(self.links))
        loaded = data.get("settings", {})
        self.settings = {
            "announce_interval": loaded.get("announce_interval", 300.0),
            "announce_cap": loaded.get("announce_cap", 2.0),
            "loglevel": loaded.get("loglevel", 4),
        }

    def save(self, path=None):
        path = path or config.TOPOLOGY_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def load(self, path=None):
        path = path or config.TOPOLOGY_FILE
        if not os.path.isfile(path):
            return False
        with open(path) as f:
            self.from_dict(json.load(f))
        return True
