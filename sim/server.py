import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .manager import Simulator

sim = Simulator()
clients = set()
clients_lock = asyncio.Lock()


class NodeBody(BaseModel):
    label: str | None = None
    x: float = 0.0
    y: float = 0.0


class NodeUpdate(BaseModel):
    label: str | None = None
    x: float | None = None
    y: float | None = None
    transport: bool | None = None
    mode: str | None = None
    announce_interval: float | None = None
    announce_rate_target: int | None = None
    announce_rate_grace: int | None = None
    announce_rate_penalty: int | None = None
    announce_cap: float | None = None
    color: str | None = None


class LinkBody(BaseModel):
    members: list[str]
    mtu: int = config.DEFAULT_MTU
    bitrate: int = config.DEFAULT_BITRATE
    loss: float = config.DEFAULT_LOSS
    propagation: float = config.DEFAULT_PROPAGATION
    x: float = 0.0
    y: float = 0.0


class LinkParams(BaseModel):
    mtu: int | None = None
    bitrate: int | None = None
    loss: float | None = None
    propagation: float | None = None
    x: float | None = None
    y: float | None = None


class LinkMembers(BaseModel):
    members: list[str]


class TrafficBody(BaseModel):
    src: str
    dst: str
    size: int = 32768


class MessageBody(BaseModel):
    src: str
    dst: str
    text: str


class SettingsBody(BaseModel):
    announce_interval: float | None = None
    announce_cap: float | None = None
    loglevel: int | None = None


class GenerateBody(BaseModel):
    nodes: int = 10
    max_hops: int = 4
    presets: list[str] = []
    max_loss: float = 0.0
    shape: str = "hub"


class PositionsBody(BaseModel):
    nodes: dict = {}
    links: dict = {}


class DropBody(BaseModel):
    destination: str


async def broadcast(event):
    async with clients_lock:
        targets = list(clients)
    dead = []
    for ws in targets:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    if dead:
        async with clients_lock:
            for ws in dead:
                clients.discard(ws)


async def event_pump():
    loop = asyncio.get_running_loop()
    while True:
        event = await loop.run_in_executor(None, sim.events.get)
        await broadcast(event)


async def status_pump():
    while True:
        await asyncio.sleep(3)
        if sim.active:
            data = await asyncio.to_thread(sim.all_status)
            await broadcast({"type": "status", "nodes": data, "lxmf": sim.lxmf_map(), "media": sim.hub.snapshot()})


@asynccontextmanager
async def lifespan(app):
    pump = asyncio.create_task(event_pump())
    poller = asyncio.create_task(status_pump())
    yield
    pump.cancel()
    poller.cancel()
    sim.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/api/state")
def get_state():
    return sim.snapshot()


@app.get("/api/status")
async def get_status():
    return await asyncio.to_thread(sim.all_status)


@app.get("/api/paths/{node_id}")
async def get_paths(node_id: str):
    return await asyncio.to_thread(sim.node_paths, node_id)


@app.get("/api/nodes/{node_id}/log")
async def get_node_log(node_id: str):
    return {"log": await asyncio.to_thread(sim.node_log, node_id)}


@app.post("/api/paths/{node_id}/drop")
async def post_drop_path(node_id: str, body: DropBody):
    ok = await asyncio.to_thread(sim.drop_path, node_id, body.destination)
    return {"ok": ok}


@app.post("/api/nodes")
def post_node(body: NodeBody):
    node_id = sim.add_node(body.label, body.x, body.y)
    return {"id": node_id}


@app.patch("/api/nodes/{node_id}")
def patch_node(node_id: str, body: NodeUpdate):
    fields = body.model_dump(exclude_unset=True)
    if any(k in fields for k in ("label", "x", "y")):
        sim.update_node(node_id, fields.get("label"), fields.get("x"), fields.get("y"))
    if "announce_interval" in fields:
        sim.set_node_announce_interval(node_id, fields["announce_interval"])
    if "color" in fields:
        sim.set_node_color(node_id, fields["color"])
    config_fields = {k: fields[k] for k in ("transport", "mode", "announce_rate_target", "announce_rate_grace", "announce_rate_penalty", "announce_cap") if k in fields}
    if config_fields:
        sim.set_node_options(node_id, config_fields)
    return {"ok": True}


@app.post("/api/nodes/{node_id}/announce")
def post_announce(node_id: str):
    ok = sim.announce(node_id)
    return {"ok": ok}


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: str):
    removed = sim.remove_node(node_id)
    return {"ok": True, "removed_links": removed}


@app.post("/api/links")
def post_link(body: LinkBody):
    link_id = sim.add_link(body.members, body.mtu, body.bitrate, body.loss, body.propagation, body.x, body.y)
    return {"id": link_id}


@app.patch("/api/links/{link_id}")
def patch_link(link_id: str, body: LinkParams):
    if body.x is not None or body.y is not None:
        sim.update_link(link_id, body.x, body.y)
    ok = sim.set_link_params(link_id, body.mtu, body.bitrate, body.loss, body.propagation)
    return {"ok": ok}


@app.patch("/api/links/{link_id}/members")
def patch_link_members(link_id: str, body: LinkMembers):
    ok = sim.set_link_members(link_id, body.members)
    return {"ok": ok}


@app.delete("/api/links/{link_id}")
def delete_link(link_id: str):
    ok = sim.remove_link(link_id)
    return {"ok": ok}


@app.get("/api/topology")
def get_topology():
    return sim.export_topology()


@app.post("/api/topology/import")
def post_import(body: dict):
    ok = sim.load_topology(body.get("topology", body))
    return {"ok": ok}


@app.get("/api/route")
async def get_route(src: str, dst: str):
    return await asyncio.to_thread(sim.trace_route, src, dst)


@app.post("/api/reset")
def post_reset():
    sim.reset()
    return {"ok": True}


@app.post("/api/generate")
def post_generate(body: GenerateBody):
    result = sim.generate(body.nodes, body.max_hops, body.presets, body.max_loss, body.shape)
    return {"ok": True, "result": result}


@app.post("/api/positions")
def post_positions(body: PositionsBody):
    sim.set_positions(body.nodes, body.links)
    return {"ok": True}


@app.post("/api/start")
def post_start():
    sim.start()
    return {"active": True}


@app.post("/api/stop")
def post_stop():
    sim.stop()
    return {"active": False}


@app.post("/api/nodes/{node_id}/restart")
def post_restart(node_id: str):
    sim.restart_node(node_id)
    return {"ok": True}


@app.post("/api/traffic")
def post_traffic(body: TrafficBody):
    sim.run_traffic(body.src, body.dst, body.size)
    return {"ok": True}


@app.post("/api/message")
def post_message(body: MessageBody):
    ok = sim.send_message(body.src, body.dst, body.text)
    return {"ok": ok}


@app.post("/api/settings")
def post_settings(body: SettingsBody):
    fields = body.model_dump(exclude_unset=True)
    if "announce_interval" in fields and fields["announce_interval"] is not None:
        sim.set_announce_interval(fields["announce_interval"])
    if "announce_cap" in fields and fields["announce_cap"] is not None:
        sim.set_announce_cap(fields["announce_cap"])
    if "loglevel" in fields and fields["loglevel"] is not None:
        sim.set_loglevel(fields["loglevel"])
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    async with clients_lock:
        clients.add(ws)
    try:
        await ws.send_json({"type": "state", "state": sim.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with clients_lock:
            clients.discard(ws)


@app.get("/")
def index():
    return FileResponse(os.path.join(config.WEB_DIR, "index.html"))


app.mount("/", StaticFiles(directory=config.WEB_DIR), name="static")
