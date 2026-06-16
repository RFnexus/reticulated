import json
import subprocess

from . import config


def run_json(tool, cfg_dir, extra):
    try:
        result = subprocess.run(
            [config.rns_tool(tool), "--config", cfg_dir] + extra + ["-j"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def node_status(cfg_dir):
    data = run_json("rnstatus", cfg_dir, ["-a"])
    if not isinstance(data, dict):
        return {"online": False, "interfaces": [], "transport_id": None}
    iface_list = data.get("interfaces", [])
    interfaces = []
    online = False
    for value in iface_list:
        if not isinstance(value, dict):
            continue
        status = bool(value.get("status", False))
        itype = value.get("type")
        interfaces.append({
            "name": value.get("name", ""),
            "type": itype,
            "status": status,
            "bitrate": value.get("bitrate"),
            "rxb": value.get("rxb", 0),
            "txb": value.get("txb", 0),
        })
        if status:
            online = True
    transport_id = data.get("transport_id")
    return {"online": online, "interfaces": interfaces, "transport_id": transport_id}


def node_paths(cfg_dir):
    data = run_json("rnpath", cfg_dir, ["-t"])
    if not isinstance(data, list):
        return []
    paths = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        paths.append({
            "destination": entry.get("hash"),
            "hops": entry.get("hops"),
            "via": entry.get("via"),
            "interface": entry.get("interface"),
        })
    return paths


def drop_path(cfg_dir, dest_hex):
    try:
        subprocess.run(
            [config.rns_tool("rnpath"), "--config", cfg_dir, "-d", dest_hex],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except Exception:
        return False
