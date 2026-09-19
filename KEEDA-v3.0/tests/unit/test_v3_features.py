"""v3.0 features: schematic section ownership, project inheritance, schematic<->PCB cross-reference."""
import base64
import os

import pytest

from server import crossref
from server.database.db import Database
from server.schematic_files import (SchematicFileError, SchematicFileStore, file_kind,
                                    looks_complete, valid_name)
from server.sections import SectionError, SectionManager
from tests.unit.test_schematic_sync import Hub, Peer, V1, V2, V3, SHEET, sha256_of  # noqa: F401
from tests.unit.test_schematic_sync import no_real_editors, two  # noqa: F401  (fixtures)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "sample_project")
PRO = b'{"meta": {"version": 3}}\n'
TABLE = b"(sym_lib_table (lib (name Device)))\n"
PCB = b"(kicad_pcb (version 1) (footprint R1))\n"


class TestSectionRules:
    @pytest.fixture
    def mgr(self, tmp_path):
        return SectionManager(Database(str(tmp_path / "s.db")))

    def test_unowned_sheets_are_open_to_everyone(self, mgr):
        assert mgr.can_write("p", "power.kicad_sch", "anyone", "designer") == (True, None)

    def test_only_the_owner_or_a_manager_may_write_an_owned_sheet(self, mgr):
        mgr.claim("p", "power.kicad_sch", "alice", "Alice")
        assert mgr.can_write("p", "power.kicad_sch", "alice", "designer")[0]
        assert not mgr.can_write("p", "power.kicad_sch", "bob", "designer")[0]
        assert mgr.can_write("p", "power.kicad_sch", "bob", "manager")[0]

    def test_a_sheet_cannot_be_stolen(self, mgr):
        mgr.claim("p", "power.kicad_sch", "alice", "Alice")
        with pytest.raises(SectionError) as e:
            mgr.claim("p", "power.kicad_sch", "bob", "Bob")
        assert e.value.code == "already_owned"

    def test_reassign_and_release_need_the_owner_or_a_manager(self, mgr):
        mgr.claim("p", "power.kicad_sch", "alice", "Alice")
        with pytest.raises(SectionError):
            mgr.assign("p", "power.kicad_sch", "bob", "Bob", by_id="bob", by_role="designer")
        mgr.assign("p", "power.kicad_sch", "bob", "Bob", by_id="alice", by_role="designer")
        assert mgr.owner_of("p", "power.kicad_sch")["owner_name"] == "Bob"
        with pytest.raises(SectionError):
            mgr.release("p", "power.kicad_sch", "alice", "designer")
        mgr.release("p", "power.kicad_sch", "root", "manager")
        assert mgr.owner_of("p", "power.kicad_sch") is None

    def test_only_schematic_sheets_have_owners(self, mgr):
        with pytest.raises(SectionError):
            mgr.claim("p", "board.kicad_pcb", "alice", "Alice")
        assert mgr.can_write("p", "board.kicad_pcb", "bob", "designer")[0]

    def test_ownership_survives_a_restart_and_shows_in_the_table(self, tmp_path):
        path = str(tmp_path / "s.db")
        SectionManager(Database(path)).claim("p", "power.kicad_sch", "alice", "Alice")
        again = SectionManager(Database(path))
        rows = again.table("p", {"power.kicad_sch": {"author": "Alice", "ts": 1.0, "rev": 3},
                                 "sensors.kicad_sch": {"author": "Bob", "ts": 2.0, "rev": 1}},
                           online={"alice": "Alice"})
        by = {r["file"]: r for r in rows}
        assert by["power.kicad_sch"]["owner_name"] == "Alice" and by["power.kicad_sch"]["owner_online"]
        assert by["sensors.kicad_sch"]["owner_name"] is None and by["sensors.kicad_sch"]["shared"]


class TestOwnershipThroughAgents:
    @pytest.mark.asyncio
    async def test_a_non_owners_edit_is_refused_and_the_sheet_is_put_back(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        hub.own(SHEET, alice)                             # Alice owns the sheet

        bob.write(V2)                                     # Bob edits it anyway
        await bob.sync.push_local()
        assert hub.store.get("p", SHEET)[0] == V1, "the server refused Bob's edit"
        await bob.sync.cycle()

        assert bob.read() == V1, "Bob's copy is put back to the owner's version"
        backups = os.listdir(bob.dir / ".kicad_live" / "backup")
        assert any(V2 == (bob.dir / ".kicad_live" / "backup" / b).read_bytes() for b in backups), \
            "Bob's edit is kept in a backup, never destroyed"
        assert any("owned by alice" in b.lower() for b in bob.banners)

    @pytest.mark.asyncio
    async def test_the_owner_keeps_editing_and_the_non_owner_receives_it(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        hub.own(SHEET, alice)
        alice.write(V2)
        await alice.sync.push_local()
        await bob.sync.cycle()
        assert hub.store.get("p", SHEET)[0] == V2 and bob.read() == V2

    @pytest.mark.asyncio
    async def test_a_manager_may_edit_any_sheet(self, tmp_path):
        hub = Hub(tmp_path)
        alice = Peer(hub, tmp_path, "alice")
        boss = Peer(hub, tmp_path, "boss", role="manager")
        alice.write(V1)
        await alice.sync.cycle()
        await boss.sync.cycle()
        hub.own(SHEET, alice)
        boss.write(V2)
        await boss.sync.push_local()
        assert hub.store.get("p", SHEET)[0] == V2

    @pytest.mark.asyncio
    async def test_unowned_sheets_behave_exactly_as_before(self, two):
        alice, bob, hub = two
        alice.write(V1)
        await alice.sync.cycle()
        await bob.sync.cycle()
        bob.write(V2)
        await bob.sync.push_local()
        assert hub.store.get("p", SHEET)[0] == V2

    @pytest.mark.asyncio
    async def test_a_second_sheet_owned_by_someone_else_is_independent(self, two):
        alice, bob, hub = two
        alice.write(V1, name="power.kicad_sch")
        bob.write(V1, name="sensors.kicad_sch")
        await alice.sync.cycle()
        await bob.sync.cycle()
        hub.own("power.kicad_sch", alice)
        hub.own("sensors.kicad_sch", bob)
        bob.write(V3, name="sensors.kicad_sch")
        await bob.sync.push_local()
        assert hub.store.get("p", "sensors.kicad_sch")[0] == V3, "Bob edits his own section"
        assert hub.store.get("p", "power.kicad_sch")[0] == V1


class TestProjectFiles:
    def test_new_file_kinds_are_accepted_and_paths_still_are_not(self):
        assert all(valid_name(n) for n in ("a.kicad_pro", "sym-lib-table", "fp-lib-table"))
        assert not any(valid_name(n) for n in ("../fp-lib-table", "a/b.kicad_pro", "notes.txt"))
        assert (file_kind("x.kicad_sch"), file_kind("x.kicad_pcb"), file_kind("x.kicad_pro"),
                file_kind("fp-lib-table")) == ("sch", "pcb", "pro", "table")

    def test_a_project_file_is_json_the_rest_are_sexpressions(self):
        assert looks_complete("a.kicad_pro", b'{"a": 1}\n')
        assert not looks_complete("a.kicad_pro", b'{"a": ')
        assert looks_complete("a.kicad_sch", b"(a)") and not looks_complete("a.kicad_sch", b"(a")

    def test_pcb_publishing_is_last_writer_wins_but_sheets_are_not(self, tmp_path):
        store = SchematicFileStore(str(tmp_path))

        def put(name, data, base):
            return store.put("p", name, base64.b64encode(data).decode(), sha256_of(data), base, "x")

        put("b.kicad_pcb", PCB, None)
        newer = PCB.replace(b"R1", b"R2")
        put("b.kicad_pcb", newer, "*")                    # allowed for the board file
        assert store.get("p", "b.kicad_pcb")[0] == newer
        put("s.kicad_sch", V1, None)
        with pytest.raises(SchematicFileError) as e:
            put("s.kicad_sch", V2, "*")                   # never for a schematic
        assert e.value.code == "stale_base"


class TestInheritance:
    async def _publisher(self, hub, tmp_path):
        host = Peer(hub, tmp_path, "host")
        host.write(V1)
        host.write(PCB, name="board.kicad_pcb")
        host.write(PRO, name="board.kicad_pro")
        host.write(TABLE, name="sym-lib-table")
        await host.sync.cycle()
        return host

    @pytest.mark.asyncio
    async def test_a_computer_with_no_project_receives_all_of_it(self, tmp_path):
        hub = Hub(tmp_path)
        await self._publisher(hub, tmp_path)
        assert set(hub.store.manifest("p")) == {SHEET, "board.kicad_pcb", "board.kicad_pro",
                                                "sym-lib-table"}
        newbie = Peer(hub, tmp_path, "newbie")
        await newbie.sync.cycle()
        assert newbie.read() == V1
        assert newbie.read("board.kicad_pcb") == PCB
        assert newbie.read("board.kicad_pro") == PRO
        assert newbie.read("sym-lib-table") == TABLE

    @pytest.mark.asyncio
    async def test_existing_files_are_not_replaced_unless_asked(self, tmp_path):
        hub = Hub(tmp_path)
        await self._publisher(hub, tmp_path)
        mine = Peer(hub, tmp_path, "mine")
        mine.write(PCB.replace(b"R1", b"R9"), name="board.kicad_pcb")
        mine.write(PRO.replace(b"3", b"4"), name="board.kicad_pro")
        await mine.sync.cycle()
        assert b"R9" in mine.read("board.kicad_pcb"), "my board is not overwritten"
        assert b'"version": 4' in mine.read("board.kicad_pro")

    @pytest.mark.asyncio
    async def test_inherit_replaces_my_copies_after_a_backup(self, tmp_path):
        hub = Hub(tmp_path)
        await self._publisher(hub, tmp_path)
        mine = Peer(hub, tmp_path, "mine", inherit=True)
        stale = PCB.replace(b"R1", b"R9")
        mine.write(stale, name="board.kicad_pcb")
        mine.write(V2)                                    # a stale schematic too
        await mine.sync.cycle()
        assert mine.read("board.kicad_pcb") == PCB and mine.read() == V1
        folder = mine.dir / ".kicad_live" / "backup"
        kept = [(folder / f).read_bytes() for f in os.listdir(folder)]
        assert stale in kept and V2 in kept, "nothing is thrown away"

    @pytest.mark.asyncio
    async def test_project_and_table_files_are_never_overwritten_on_the_server(self, tmp_path):
        hub = Hub(tmp_path)
        await self._publisher(hub, tmp_path)
        other = Peer(hub, tmp_path, "other")
        other.write(PRO.replace(b"3", b"9"), name="board.kicad_pro")
        await other.sync.cycle()
        assert hub.store.get("p", "board.kicad_pro")[0] == PRO

    @pytest.mark.asyncio
    async def test_saving_the_board_updates_the_teams_starting_copy(self, tmp_path):
        hub = Hub(tmp_path)
        host = await self._publisher(hub, tmp_path)
        newer = PCB.replace(b"R1", b"R1 R2")
        host.write(newer, name="board.kicad_pcb")
        await host.sync.push_local()
        assert hub.store.get("p", "board.kicad_pcb")[0] == newer


class TestCrossReference:
    @staticmethod
    def sch(ref, value="10k", fp="Resistor_SMD:R_0805", lib="Device:R"):
        return {"reference": ref, "value": value, "footprint": fp, "lib_id": lib, "sheet": "/"}

    @staticmethod
    def brd(ref, value="10k", fp="R_0805"):
        return {"reference": ref, "value": value, "footprint": fp}

    @staticmethod
    def status(report):
        return {r["reference"]: r["status"] for r in report["rows"]}

    def test_every_disagreement_is_named(self):
        report = crossref.build(
            [self.sch("R1"), self.sch("R2"), self.sch("R3", value="4k7"), self.sch("R4", fp=""),
             self.sch("R5", fp="Resistor_SMD:R_1206"), self.sch("#PWR01")],
            [self.brd("R1"), self.brd("R3", value="10k"), self.brd("R4"),
             self.brd("R5", fp="R_0805"), self.brd("C9")], "live")
        assert self.status(report) == {
            "R1": "ok", "R2": "not_placed", "R3": "value_mismatch", "R4": "no_footprint",
            "R5": "footprint_mismatch", "C9": "orphan_footprint"}
        assert report["issues"] == 5 and report["counts"]["ok"] == 1

    def test_issues_are_listed_before_agreements(self):
        rows = crossref.build([self.sch("R1"), self.sch("R2")], [self.brd("R1")], "live")["rows"]
        assert rows[0]["status"] == "not_placed" and rows[-1]["status"] == "ok"

    def test_footprint_names_compare_by_part_name_not_library(self):
        report = crossref.build([self.sch("R1", fp="Resistor_SMD:R_0805")],
                                [self.brd("R1", fp="R_0805")], "live")
        assert self.status(report)["R1"] == "ok"

    def test_a_missing_side_is_unknown_not_a_flood_of_errors(self):
        report = crossref.build([self.sch("R1"), self.sch("R2")], [], "none")
        assert report["issues"] == 0, "no PCB data yet must not read as 'nothing placed'"
        report = crossref.build([], [self.brd("R1")], "live", schematic_known=False)
        assert report["issues"] == 0

    def test_the_sample_board_is_read_from_its_file(self):
        board = crossref.footprints_from_pcb_file(os.path.join(SAMPLE, "demo_board.kicad_pcb"))
        assert {"R1", "R2", "C1", "C2", "J1", "U1"} <= {b["reference"] for b in board}
        assert all(b["footprint"] for b in board)
