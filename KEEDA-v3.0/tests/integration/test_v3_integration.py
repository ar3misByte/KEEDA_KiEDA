"""v3.0 through a real server and real WebSockets: sections, project files, cross-reference."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request

import pytest

from tests.integration.test_collab_integration import Client, server  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "sample_project")


def sample(name: str) -> bytes:
    with open(os.path.join(SAMPLE, name), "rb") as fh:
        return fh.read()


def push(name: str, data: bytes, base=None) -> dict:
    return {"type": "schematic_push", "files": [{
        "name": name, "sha256": hashlib.sha256(data).hexdigest(), "base_sha256": base,
        "content_b64": base64.b64encode(data).decode()}]}


def get(server, path):
    with urllib.request.urlopen(f"http://{server}{path}", timeout=10) as r:
        return json.loads(r.read())


async def join(server, project, name, cid, role="designer"):
    c = Client(server, name, project, cid, role=role)
    await c.connect()
    return c


@pytest.mark.asyncio
async def test_section_ownership_end_to_end(server):
    alice = await join(server, "v3_sec", "Alice", "alice-v3")
    bob = await join(server, "v3_sec", "Bob", "bob-v3")
    boss = await join(server, "v3_sec", "Boss", "boss-v3", role="manager")
    sheet = "power.kicad_sch"
    v1 = b"(kicad_sch (version 1) (symbol R1 10k))\n"
    v2 = b"(kicad_sch (version 1) (symbol R1 4k7))\n"
    try:
        await alice.send(push(sheet, v1))
        sha1 = (await alice.wait_for("schematic_push_result"))["accepted"][0]["sha256"]

        await alice.send({"type": "section_claim", "file": sheet})
        table = await bob.wait_for("sections", predicate=lambda m: any(
            r["file"] == sheet and r["owner_name"] == "Alice" for r in m["sections"]))
        assert table["sections"][0]["owner_name"] == "Alice"

        # Bob cannot overwrite Alice's section, and the dashboard is told
        await bob.send(push(sheet, v2, base=sha1))
        result = await bob.wait_for("schematic_push_result")
        assert result["rejected"][0]["code"] == "not_owner"
        assert result["rejected"][0]["owner_name"] == "Alice"
        blocked = await alice.wait_for("blocked_event")
        assert blocked["blocked"][0]["owner_name"] == "Alice"

        # nor steal it
        await bob.send({"type": "section_claim", "file": sheet})
        assert (await bob.wait_for("error"))["code"] == "already_owned"

        # Alice still can
        await alice.send(push(sheet, v2, base=sha1))
        assert (await alice.wait_for("schematic_push_result"))["accepted"]

        # a manager reassigns it to Bob
        await boss.send({"type": "section_assign", "file": sheet, "owner_id": "bob-v3"})
        await bob.wait_for("sections", predicate=lambda m: m["sections"][0]["owner_name"] == "Bob")
        view = get(server, "/api/sections?project=v3_sec")
        assert view["sections"][0]["owner_name"] == "Bob"
        assert {p["user_name"] for p in view["people"]} >= {"Alice", "Bob", "Boss"}
        assert [f["name"] for f in view["files"]] == [sheet]

        # a designer cannot reassign
        await alice.send({"type": "section_assign", "file": sheet, "owner_id": "alice-v3"})
        assert (await alice.wait_for("error"))["code"] == "not_allowed"

        # ownership is in the activity feed
        events = get(server, "/api/activity?project=v3_sec&limit=50")["events"]
        text = " | ".join(e["description"] for e in events)
        assert "took ownership of section power.kicad_sch" in text
        assert "owned by Alice" in text and "assigned section power.kicad_sch to Bob" in text
    finally:
        for c in (alice, bob, boss):
            await c.close()


@pytest.mark.asyncio
async def test_project_files_are_listed_and_a_late_joiner_learns_of_them(server):
    host = await join(server, "v3_files", "Host", "host-v3")
    try:
        for name in ("demo_board.kicad_sch", "demo_board.kicad_pcb", "demo_board.kicad_pro"):
            await host.send(push(name, sample(name)))
            assert (await host.wait_for("schematic_push_result"))["accepted"]
        late = await join(server, "v3_files", "Late", "late-v3")
        try:
            manifest = await late.wait_for("schematic_manifest")
            assert {"demo_board.kicad_sch", "demo_board.kicad_pcb", "demo_board.kicad_pro"} <= set(
                manifest["files"])
            assert manifest["files"]["demo_board.kicad_pro"]["kind"] == "pro"
        finally:
            await late.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_crossref_reads_the_teams_shared_files(server):
    host = await join(server, "v3_xref", "Host", "host-xr")
    try:
        sch = sample("demo_board.kicad_sch")
        pcb = sample("demo_board.kicad_pcb")
        await host.send(push("demo_board.kicad_sch", sch))
        await host.wait_for("schematic_push_result")
        # schematic alone: the PCB side is unknown, not "everything missing"
        report = get(server, "/api/crossref?project=v3_xref")
        assert report["pcb_source"] == "none" and report["issues"] == 0
        assert report["schematic_components"] >= 5

        await host.send(push("demo_board.kicad_pcb", pcb))
        await host.wait_for("schematic_push_result")
        report = get(server, "/api/crossref?project=v3_xref")
        assert report["pcb_source"] == "file"
        by = {r["reference"]: r for r in report["rows"]}
        assert by["R1"]["in_schematic"] and by["R1"]["on_pcb"]
        assert report["counts"]["ok"] + report["issues"] == len(report["rows"])
    finally:
        await host.close()
