"""Design sections: who owns which sheet of the schematic.

A large KiCad project is split into hierarchical sheets, and every sheet is its
own `.kicad_sch` file. A *section* is one such sheet. Giving a sheet an owner
lets a team divide the schematic (User A -> power, User B -> sensors, ...)
while it stays one KiCad project.

Rules (all enforced on the SERVER, at the point a saved file is shared):

  * an unowned sheet is open to everyone (nothing changes for existing teams);
  * an owned sheet can only be changed by its owner or by a manager;
  * anyone can claim an unowned sheet; the owner or a manager can release or
    reassign it.

eeschema cannot be told "this sheet is read-only", so enforcement happens when
the file is shared: a non-owner's save is refused, the agent tells them, and
their copy is put back to the owner's version (with a backup). Roles are
declared by the client (there is no authentication yet), so this protects
teammates from accidents, not from attackers.
"""
from __future__ import annotations

import logging

from server.database.db import Database
from server.schematic_files import file_kind

log = logging.getLogger("kicadlive.sections")

MANAGER = "manager"


class SectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SectionManager:
    def __init__(self, database: Database):
        self.db = database

    # ---------------------------------------------------------------- reads

    def owners(self, project_id: str) -> dict[str, dict]:
        return {row["file"]: row for row in self.db.sections(project_id)}

    def owner_of(self, project_id: str, file: str) -> dict | None:
        return self.owners(project_id).get(file)

    def can_write(self, project_id: str, file: str, client_id: str, role: str) -> tuple[bool, dict | None]:
        """(allowed, owner). Only schematic sheets have owners."""
        if file_kind(file) != "sch":
            return True, None
        owner = self.owner_of(project_id, file)
        if owner is None or owner["owner_id"] == client_id or role == MANAGER:
            return True, owner
        return False, owner

    def table(self, project_id: str, manifest: dict[str, dict],
              online: dict[str, str] | None = None) -> list[dict]:
        """One row per schematic sheet: the ownership map the dashboard shows."""
        owners = self.owners(project_id)
        online = online or {}
        rows = []
        for name in sorted(set(owners) | {n for n in manifest if file_kind(n) == "sch"},
                           key=str.lower):
            entry = manifest.get(name) or {}
            owner = owners.get(name)
            rows.append({
                "file": name,
                "owner_id": owner["owner_id"] if owner else None,
                "owner_name": owner["owner_name"] if owner else None,
                "owner_online": bool(owner and owner["owner_id"] in online),
                "assigned_by": owner.get("assigned_by") if owner else None,
                "assigned_at": owner.get("assigned_at") if owner else None,
                "last_author": entry.get("author"),
                "last_ts": entry.get("ts"),
                "rev": entry.get("rev"),
                "shared": bool(entry),
            })
        return rows

    # --------------------------------------------------------------- writes

    def claim(self, project_id: str, file: str, client_id: str, user_name: str) -> dict:
        self._check_file(file)
        current = self.owner_of(project_id, file)
        if current is not None and current["owner_id"] != client_id:
            raise SectionError("already_owned", f"{file} is already owned by {current['owner_name']}")
        return self.db.set_section(project_id, file, client_id, user_name, client_id)

    def assign(self, project_id: str, file: str, owner_id: str, owner_name: str,
               by_id: str, by_role: str) -> dict:
        self._check_file(file)
        current = self.owner_of(project_id, file)
        if by_role != MANAGER and not (current and current["owner_id"] == by_id):
            raise SectionError("not_allowed", "only a manager or the current owner can reassign a section")
        return self.db.set_section(project_id, file, owner_id, owner_name, by_id)

    def release(self, project_id: str, file: str, by_id: str, by_role: str) -> None:
        self._check_file(file)
        current = self.owner_of(project_id, file)
        if current is None:
            return
        if by_role != MANAGER and current["owner_id"] != by_id:
            raise SectionError("not_allowed", "only a manager or the owner can release a section")
        self.db.clear_section(project_id, file)

    @staticmethod
    def _check_file(file: str) -> None:
        if not isinstance(file, str) or file_kind(file) != "sch":
            raise SectionError("bad_file", "sections are schematic sheets (*.kicad_sch)")
