import os
import shutil
import hashlib

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SIM_DATA_DIR", os.path.join(BASE_DIR, "simdata"))
NODES_DIR = os.path.join(DATA_DIR, "nodes")
WEB_DIR = os.path.join(BASE_DIR, "web")
TOPOLOGY_FILE = os.path.join(DATA_DIR, "topology.json")

INSTANCE_PREFIX = hashlib.md5(os.path.abspath(DATA_DIR).encode("utf-8")).hexdigest()[:6]

HUB_HOST = os.environ.get("SIM_HUB_HOST", "127.0.0.1")
HUB_PORT = int(os.environ.get("SIM_HUB_PORT", "5800"))

HTTP_HOST = os.environ.get("SIM_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("SIM_PORT", "8000"))

BRIDGE_HOST = os.environ.get("SIM_BRIDGE_HOST", "127.0.0.1")
TCP_BASE = int(os.environ.get("SIM_TCP_BASE", "6000"))

DEFAULT_MTU = 500
DEFAULT_BITRATE = 9600
DEFAULT_LOSS = 0.0
DEFAULT_PROPAGATION = 0.0

MIN_MTU = 500
MAX_MTU = 32768


def rns_tool(name):
    return shutil.which(name) or name


def rnsd_path():
    return rns_tool("rnsd")


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(NODES_DIR, exist_ok=True)
