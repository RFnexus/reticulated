import os
import shutil
import hashlib
import sys

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
RUST_BRIDGE_BASE = int(os.environ.get("SIM_RUST_BRIDGE_BASE", "16000"))
RUST_RPC_BASE = int(os.environ.get("SIM_RUST_RPC_BASE", "20000"))
RUST_RPC_HOST = os.environ.get("SIM_RUST_RPC_HOST", "127.0.0.1")
RUST_BRIDGE_STRIDE = int(os.environ.get("SIM_RUST_BRIDGE_STRIDE", "16"))
RUST_RETICULUMD = os.environ.get("SIM_RUST_RETICULUMD", "reticulumd")
RANDOM_SEED = int(os.environ.get("SIM_RANDOM_SEED", "0"))
PYTHON_RNS_REPO = os.environ.get("SIM_PYTHON_RNS_REPO")
PYTHON_LXMF_REPO = os.environ.get("SIM_PYTHON_LXMF_REPO")

DEFAULT_MTU = 500
DEFAULT_BITRATE = 9600
DEFAULT_LOSS = 0.0
DEFAULT_PROPAGATION = 0.0

MIN_MTU = 500
MAX_MTU = 32768


def rns_tool(name):
    sibling = os.path.join(os.path.dirname(sys.executable), name)
    return shutil.which(name) or (sibling if os.path.isfile(sibling) else name)


def rnsd_path():
    return rns_tool("rnsd")


def reticulumd_path():
    return shutil.which(RUST_RETICULUMD) or RUST_RETICULUMD


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(NODES_DIR, exist_ok=True)
