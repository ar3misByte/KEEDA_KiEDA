"""v3.0: PCB add/remove sync, the phantom-retraction regression, component identity.

The retraction bug (present in v2.0): a footprint ADDED on one computer was
written into the other agent's baseline without ever reaching its board, so
the next poll reported it as REMOVED and the server deleted it again.
"""
import copy
import sqlite3

import pytest

from agent.sync_agent import SyncAgent
from common.component_identity import context_of, identity_of, label
from common.diff_engine import summarise
from common.protocol import ValidationError, validate_change
from tests.unit.test_echo_suppression import FakeWS

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"


def part(uid, ref, value="10k", x=10.0, footprint="Resistor_SMD:R_0805"):
    return {"uuid": uid, "reference": ref, "object_type": "footprint",
            "position": {"x": x, "y": 20.0}, "rotation": 0.0, "layer": "F.Cu",
            "value": value, "footprint": footprint}


class BoardLink:
    """A believable KiCad: add/remove/modify, and honest about what it applied."""

    def __init__(self, parts=None):
        self.board = {p["uuid"]: p for p in (parts or [part(A, "R1")])}
        self.last_applied = set()
        self.last_unmatched = []
        self.last_skipped_adds = []
        self.writes = []

    def read_snapshot(self):
        return copy.deepcopy(self.board)

    def selection_uuids(self):
        return {}

    def export_footprint(self, uuid):
        return f"PAYLOAD-{uuid}" if uuid in self.board else None

    def export_all(self):
        return {u: f"PAYLOAD-{u}" for u in self.board}

    def apply_changes(self, changes, description=""):
        self.writes.append(changes)
        self.last_applied, self.last_unmatched, self.last_skipped_adds = set(), [], []
        for c in changes:
            uid, op = c["uuid"], c["operation"]
            if op == "modify":
                if uid in self.board:
                    self.board[uid][c["field"]] = c["new"]
                    self.last_applied.add(uid)
                else:
                    self.last_unmatched.append(c.get("reference") or uid[:8])
            elif op == "add":
                if uid in self.board:
                    self.last_applied.add(uid)
                elif (c.get("state") or {}).get("proto_b64"):
                    state = {k: v for k, v in c["state"].items() if k != "proto_b64"}
                    self.board[uid] = state
                    self.last_applied.add(uid)
                else:
                    self.last_skipped_adds.append(c.get("reference") or uid[:8])
            elif op == "remove":
                self.board.pop(uid, None)
                self.last_applied.add(uid)
        return len(self.last_applied)


def make(parts=None):
    link = BoardLink(parts)
    ws = FakeWS()
    agent = SyncAgent(link, ws, "Tester")
    agent.baseline = link.read_snapshot()
    agent.state_ready.set()
    agent.banners = []
    agent.banner = agent.banners.append
    return agent, link, ws


def remote(*changes):
    return {"origin_user_name": "Other", "changes": list(changes)}


ADD_B = {"operation": "add", "object_type": "footprint", "uuid": B, "reference": "U5",
         "version": 4, "state": {**part(B, "U5", "MPU6050", 30.0, "Sensor:MPU-6050"),
                                 "proto_b64": "PAYLOAD-B"}}


class TestRemoteAddRemove:
    @pytest.mark.asyncio
    async def test_a_component_added_elsewhere_appears_here(self):
        agent, link, ws = make()
        await agent._on_remote_change(remote(ADD_B))
        assert B in link.board and link.board[B]["reference"] == "U5"

    @pytest.mark.asyncio
    async def test_the_added_component_is_not_retracted_afterwards(self):
        """THE v2.0 BUG: the add was 'recorded' but never applied; the next poll
        saw it missing and told the server it had been removed."""
        agent, link, ws = make()
        await agent._on_remote_change(remote(ADD_B))
        for _ in range(4):
            await agent.poll_once()
        assert ws.changes_sent() == []

    @pytest.mark.asyncio
    async def test_an_add_that_cannot_be_created_is_not_reported_as_removed(self):
        agent, link, ws = make()
        no_payload = copy.deepcopy(ADD_B)
        del no_payload["state"]["proto_b64"]
        await agent._on_remote_change(remote(no_payload))
        for _ in range(4):
            await agent.poll_once()
        assert B not in link.board
        assert ws.changes_sent() == [], "must not invent a removal of a part we never had"
        assert B not in agent.baseline

    @pytest.mark.asyncio
    async def test_changes_to_parts_this_board_lacks_do_not_create_phantoms(self):
        """Two boards that are different copies: every teammate move used to become
        a phantom baseline entry that was then reported as a removal."""
        agent, link, ws = make()
        await agent._on_remote_change(remote(
            {"operation": "modify", "object_type": "footprint", "uuid": B, "reference": "U5",
             "field": "position", "new": {"x": 5.0, "y": 5.0}, "version": 2}))
        for _ in range(4):
            await agent.poll_once()
        assert ws.changes_sent() == []
        assert B not in agent.baseline
        assert any("IGNORED" in b and "U5" in b for b in agent.banners), "the user must be told"

    @pytest.mark.asyncio
    async def test_a_component_removed_elsewhere_is_removed_here_and_stays_gone(self):
        agent, link, ws = make([part(A, "R1"), part(B, "U5", "MPU6050")])
        await agent._on_remote_change(remote(
            {"operation": "remove", "object_type": "footprint", "uuid": B,
             "reference": "U5", "version": 5}))
        assert B not in link.board
        for _ in range(4):
            await agent.poll_once()
        assert ws.changes_sent() == [], "a deletion must not bounce back as a re-add"

    @pytest.mark.asyncio
    async def test_a_part_added_locally_is_sent_with_its_footprint_and_identity(self):
        agent, link, ws = make()
        link.board[B] = part(B, "U5", "ESP32-WROOM", 40.0, "RF_Module:ESP32-WROOM-32")
        await agent.poll_once()
        add = [c for c in ws.changes_sent() if c["operation"] == "add"][0]
        assert add["state"]["proto_b64"] == f"PAYLOAD-{B}"
        assert add["component"]["component"] == "ESP32-WROOM"
        assert add["component"]["reference"] == "U5"
        assert add["component"]["footprint"] == "RF_Module:ESP32-WROOM-32"

    @pytest.mark.asyncio
    async def test_a_late_joiner_receives_components_that_already_exist(self):
        agent, link, ws = make()
        agent.state_ready.clear()
        server_state = {"objects": {
            A: {**part(A, "R1"), "field_versions": {}},
            B: {**ADD_B["state"], "field_versions": {"position": 4}},
        }, "locks": []}
        await agent._on_project_state(server_state)
        assert B in link.board, "before v3.0 late joiners never received existing components"
        await agent.poll_once()
        assert ws.changes_sent() == []

    @pytest.mark.asyncio
    async def test_boards_with_nothing_in_common_are_called_out(self):
        agent, link, ws = make([part("cccccccc-0000-4000-8000-000000000003", "R9")])
        agent.state_ready.clear()
        await agent._on_project_state({"objects": {A: part(A, "R1")}, "locks": []})
        assert any("NO footprints" in b for b in agent.banners)


class TestComponentIdentity:
    def test_reference_and_component_are_both_available(self):
        ident = identity_of({"reference": "U5", "value": "ESP32-WROOM",
                             "footprint": "RF_Module:ESP32-WROOM-32"})
        assert ident["reference"] == "U5" and ident["component"] == "ESP32-WROOM"
        assert ident["part"] == "ESP32-WROOM-32"

    def test_label_shows_both(self):
        assert label({"reference": "U5", "value": "MPU6050"}) == "U5 (MPU6050)"

    def test_label_falls_back_to_the_reference(self):
        assert label({"reference": "U5"}) == "U5"
        assert label({"reference": "U5", "value": "U5"}) == "U5"

    def test_value_that_only_repeats_the_reference_uses_the_library_part(self):
        ident = identity_of({"reference": "U1", "value": "U1", "lib_id": "Sensor_Motion:MPU-6050"})
        assert ident["component"] == "MPU-6050"

    def test_symbol_lib_id_is_split_into_library_part(self):
        ident = identity_of({"reference": "U2", "value": "", "lib_id": "Sensor_Motion:MPU-6050"})
        assert ident["part"] == "MPU-6050" and ident["lib_id"] == "Sensor_Motion:MPU-6050"

    def test_context_survives_being_read_back(self):
        ctx = context_of({"reference": "U5", "value": "MPU6050", "footprint": "Sensor:MPU-6050"})
        assert label(ctx) == "U5 (MPU6050)"

    def test_nothing_is_invented(self):
        assert identity_of({}) == {}
        assert identity_of(None) == {}


class TestWireFormat:
    def test_component_context_passes_validation_and_junk_is_dropped(self):
        out = validate_change({"operation": "modify", "uuid": A, "reference": "U5",
                               "field": "value", "old": "a", "new": "b",
                               "component": {"reference": "U5", "component": "MPU6050",
                                             "evil": "x", "footprint": "L:F"}})
        assert out["component"] == {"reference": "U5", "component": "MPU6050", "footprint": "L:F"}

    def test_old_agents_without_the_block_still_validate(self):
        out = validate_change({"operation": "remove", "uuid": A, "reference": "R1"})
        assert "component" not in out

    def test_bad_operation_still_rejected(self):
        with pytest.raises(ValidationError):
            validate_change({"operation": "explode", "uuid": A})

    def test_summaries_name_the_component(self):
        change = {"operation": "modify", "uuid": A, "reference": "U5", "field": "position",
                  "new": {"x": 1, "y": 2}, "component": {"component": "MPU6050"}}
        assert summarise(change) == "moved U5 (MPU6050) to (1.00, 2.00)"
        assert summarise({"operation": "add", "uuid": A, "reference": "R1"}) == "added R1"


class TestEventStorage:
    def test_old_databases_are_upgraded_in_place(self, tmp_path):
        from server.database.db import Database
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
              ts REAL NOT NULL, user_id TEXT, username TEXT, domain TEXT NOT NULL, object_type TEXT,
              object_id TEXT, object_ref TEXT, action TEXT NOT NULL, field TEXT, old_value TEXT,
              new_value TEXT, description TEXT NOT NULL, version INTEGER);
            INSERT INTO events (project_id, ts, domain, action, description) VALUES ('p', 1, 'pcb', 'added', 'old row');
        """)
        conn.commit(); conn.close()
        db = Database(path)
        event = db.add_event("p", "pcb", "added", "added U5 (MPU6050)", object_ref="U5",
                             object_name="MPU6050")
        rows = db.events("p", limit=10)
        assert {r["description"]: r["object_name"] for r in rows} == {
            "old row": None, "added U5 (MPU6050)": "MPU6050"}
        assert event["object_name"] == "MPU6050"

    def test_schematic_symbol_events_carry_the_part_name(self):
        from server.schematic.diff import diff_snapshots, summarise as sch_summary
        old = {A: {"uuid": A, "object_type": "symbol", "reference": "U2", "value": "MPU6050",
                   "lib_id": "Sensor_Motion:MPU-6050", "position": {"x": 1, "y": 1}}}
        new = {A: dict(old[A], value="MPU6500")}
        change = diff_snapshots(old, new)[0]
        assert change["component"]["part"] == "MPU-6050"
        assert sch_summary(change) == "changed U2 value from MPU6050 to MPU6500"
        moved = diff_snapshots(new, {A: dict(new[A], position={"x": 5.0, "y": 5.0})})[0]
        assert "U2 (MPU6500)" in sch_summary(moved)


class TestEditorSafety:
    def test_strict_find_never_returns_an_unrelated_open_board(self, monkeypatch):
        """v2.0 auto-saved ANY open PCB editor when only one was open."""
        from agent import win_ui
        monkeypatch.setattr(win_ui, "AVAILABLE", True)
        monkeypatch.setattr(win_ui, "_top_windows", lambda: [1])
        monkeypatch.setattr(win_ui, "_text", lambda h: "*some_other_board — PCB Editor")
        monkeypatch.setattr(win_ui, "_class", lambda h: "wxWindowNR")
        assert win_ui.find_editor("pcb", ["my_project"], strict=True) is None
        assert win_ui.find_editor("pcb", ["some_other_board"], strict=True) is not None

    def test_value_edit_description_does_not_echo_the_new_value(self):
        change = {"operation": "modify", "uuid": A, "reference": "R1", "field": "value",
                  "old": "4k7", "new": "10k", "component": {"component": "10k"}}
        assert summarise(change) == "changed R1 value"
