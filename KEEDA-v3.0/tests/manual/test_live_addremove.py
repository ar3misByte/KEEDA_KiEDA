"""v3.0 - live test: footprints ADDED / REMOVED on one board reach another board.

Needs a real KiCad with sample_project/demo_board.kicad_pcb open (PCB editor
started first) and the API enabled. It starts its own server on a spare port.

    python tests/manual/test_live_addremove.py

Roles: the real KiCad + a real agent play "computer B"; a protocol client plays
"computer A". Before v3.0 a component added on one computer never appeared on
the other, because the agent ignored `add` and `remove`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid as uuidlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import websockets

from agent.kicad_link import KiCadLink, KiCadUnavailable
from agent.sync_agent import SyncAgent
from agent.ws_client import WSClient

RESULTS: list[tuple[bool, str]] = []
PROJECT = "demo_board"


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label))
    print(f"{'[PASS]' if ok else '[FAIL]'} {label}{(' - ' + str(detail)) if detail else ''}", flush=True)
    return ok


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.2)
    return False


def clone_payload(link, source_ref, new_ref, x_mm, y_mm):
    """A footprint payload copied from an existing part, as a new part."""
    from kipy.board_types import FootprintInstance
    from kipy.geometry import Vector2
    from kipy.proto.board import board_types_pb2
    from common.diff_engine import mm_to_nm

    source = next(f for f in link._board.get_footprints()
                  if f.reference_field.text.value == source_ref)
    proto = board_types_pb2.FootprintInstance()
    proto.CopyFrom(source.proto)
    new_uuid = str(uuidlib.uuid4())
    proto.id.value = new_uuid
    fp = FootprintInstance(proto)
    fp.reference_field.text.value = new_ref
    fp.position = Vector2.from_xy(mm_to_nm(x_mm), mm_to_nm(y_mm))
    return new_uuid, fp, base64.b64encode(fp.proto.SerializeToString()).decode("ascii")


async def main() -> int:
    print("=" * 62)
    print(" KEEDA v3.0 - live add / remove of footprints")
    print("=" * 62)

    probe = KiCadLink(client_name_prefix="kl-addtest")
    try:
        board = probe.connect()
    except KiCadUnavailable as exc:
        check(False, "KiCad reachable", exc)
        return 1
    check(True, "KiCad reachable", board)
    have = {s["reference"] for s in probe.read_snapshot().values()}
    for stale in ("C8", "C9"):                         # leftovers of an earlier run
        for fp in [f for f in probe._board.get_footprints() if f.reference_field.text.value == stale]:
            c = probe._board.begin_commit(); probe._board.remove_items([fp]); probe._board.push_commit(c, "cleanup")

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="v3live_")
    env = dict(os.environ, KICADLIVE_DATA_DIR=data_dir, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
    server = subprocess.Popen([sys.executable, "-m", "server.main", "--host", "127.0.0.1",
                               "--port", str(port)], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    agent_tasks = []
    peer = None
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                break
            except OSError:
                await asyncio.sleep(0.25)

        # ---- the real agent (computer B)
        link = KiCadLink(client_name_prefix="kl-agent")
        link.connect()
        ws = WSClient("127.0.0.1", port, "agent-b", "Designer B", PROJECT)
        agent = SyncAgent(link, ws, "Designer B", poll_interval=0.25)

        async def on_connect():
            agent.on_reconnect()
            await agent.send_presence("viewing")
        ws.on_connect = on_connect
        agent_tasks = [asyncio.create_task(ws.run()), asyncio.create_task(agent.run())]
        check(await until(agent.state_ready.is_set, 20), "agent adopted the (seeded) project state")

        # ---- the other computer (protocol client)
        peer = await websockets.connect(f"ws://127.0.0.1:{port}/ws", open_timeout=10)
        await peer.send(json.dumps({"type": "hello", "client_id": "peer-a",
                                    "user_name": "Designer A", "project_id": PROJECT}))
        seen: list[dict] = []

        async def pump():
            async for raw in peer:
                seen.append(json.loads(raw))
        pumper = asyncio.create_task(pump())
        await until(lambda: any(m["type"] == "project_state" for m in seen), 10)
        state = next(m for m in seen if m["type"] == "project_state")
        objs = state["objects"]
        check(bool(objs) and all("proto_b64" in o for o in objs.values()),
              "server holds full footprint data for every part (seeded by the agent)",
              f"{len(objs)} parts")
        check(all(o.get("footprint") for o in objs.values()), "footprint library id is captured",
              next(iter(objs.values())).get("footprint"))

        # ---- 1. a component added on the other computer appears on this board
        new_uuid, _fp, payload = clone_payload(probe, "C1", "C9", 60.0, 70.0)
        await peer.send(json.dumps({"type": "change", "client_id": "peer-a", "change_id": "a-1",
            "changes": [{"operation": "add", "object_type": "footprint", "uuid": new_uuid,
                         "reference": "C9", "base_version": 0,
                         "component": {"reference": "C9", "component": "100nF"},
                         "state": {"uuid": new_uuid, "reference": "C9", "object_type": "footprint",
                                   "position": {"x": 60.0, "y": 70.0}, "rotation": 0.0,
                                   "layer": "F.Cu", "value": "100nF", "proto_b64": payload}}]}))
        on_board = lambda ref: any(f.reference_field.text.value == ref for f in probe._board.get_footprints())
        check(await until(lambda: on_board("C9"), 12), "ADD: C9 placed on the other computer appears on the real board")
        created = next((f for f in probe._board.get_footprints() if f.reference_field.text.value == "C9"), None)
        check(created is not None and created.id.value == new_uuid, "ADD: same uuid, so later edits match it")
        check(created is not None and len(list(created.definition.pads)) == 2, "ADD: pads came with it")

        # ---- 2. it is then a normal, syncable object
        await peer.send(json.dumps({"type": "change", "client_id": "peer-a", "change_id": "a-2",
            "changes": [{"operation": "modify", "object_type": "footprint", "uuid": new_uuid,
                         "reference": "C9", "field": "position", "base_version": 0,
                         "old": None, "new": {"x": 66.0, "y": 71.0}}]}))
        def moved():
            f = next((f for f in probe._board.get_footprints() if f.reference_field.text.value == "C9"), None)
            return f is not None and abs(f.position.x / 1e6 - 66.0) < 0.01
        check(await until(moved, 10), "MODIFY: the added part can be moved afterwards")

        # ---- 3. removal
        await peer.send(json.dumps({"type": "change", "client_id": "peer-a", "change_id": "a-3",
            "changes": [{"operation": "remove", "object_type": "footprint", "uuid": new_uuid,
                         "reference": "C9", "base_version": 0}]}))
        check(await until(lambda: not on_board("C9"), 10), "REMOVE: deleting C9 on the other computer removes it here")

        # ---- 4. a component added HERE is detected, packaged and sent
        seen.clear()
        local_uuid, local_fp, _ = clone_payload(probe, "R1", "C8", 40.0, 40.0)
        c = probe._board.begin_commit()
        probe._board.create_items(local_fp)
        probe._board.push_commit(c, "test: place C8")
        def got_add():
            for m in seen:
                if m.get("type") == "remote_change":
                    for ch in m["changes"]:
                        if ch["uuid"] == local_uuid and ch["operation"] == "add":
                            return ch
            return None
        await until(lambda: got_add() is not None, 10)
        add = got_add()
        check(add is not None, "LOCAL ADD: a part placed in KiCad is detected and broadcast")
        check(add is not None and add["state"].get("proto_b64"), "LOCAL ADD: broadcast carries the full footprint")
        check(add is not None and (add.get("component") or {}).get("component") == "10k",
              "LOCAL ADD: broadcast names the part, not just C8",
              str(add.get("component")) if add else "")

        # ---- 5. dashboard data: events + /api/pcb carry the identity
        await asyncio.sleep(0.8)
        events = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/activity?project={PROJECT}&limit=50").read())["events"]
        ev = next((e for e in events if e.get("object_ref") == "C8"), None)
        check(ev is not None and ev.get("object_name") == "10k",
              "DASHBOARD: activity event stores the component name", str(ev and ev.get("object_name")))
        check(ev is not None and "C8 (10k)" in ev["description"],
              "DASHBOARD: description reads 'C8 (10k)'", str(ev and ev["description"]))
        rows = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/pcb?project={PROJECT}").read())["objects"]
        row = next((r for r in rows if r.get("reference") == "C8"), None)
        check(row is not None and row.get("component") == "10k" and row.get("footprint")
              and "proto_b64" not in row, "DASHBOARD: /api/pcb row has component + footprint, no raw data",
              str({k: row.get(k) for k in ('reference', 'component', 'footprint')}) if row else "")

        # ---- 6. local removal
        seen.clear()
        victim = next(f for f in probe._board.get_footprints() if f.reference_field.text.value == "C8")
        c = probe._board.begin_commit(); probe._board.remove_items([victim]); probe._board.push_commit(c, "test: delete C8")
        def got_remove():
            return any(m.get("type") == "remote_change" and any(
                ch["uuid"] == local_uuid and ch["operation"] == "remove" for ch in m["changes"]) for m in seen)
        check(await until(got_remove, 10), "LOCAL REMOVE: a part deleted in KiCad is broadcast")
        pumper.cancel()
    finally:
        for t in agent_tasks:
            t.cancel()
        if peer:
            await peer.close()
        server.terminate()
        # leave the board as we found it
        for stale in ("C8", "C9"):
            for fp in [f for f in probe._board.get_footprints() if f.reference_field.text.value == stale]:
                c = probe._board.begin_commit(); probe._board.remove_items([fp]); probe._board.push_commit(c, "cleanup")

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\nRESULT: {len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
