# KEEDA v3.0 - sections, project inheritance, schematic/PCB cross-reference

## 1. Section ownership (multi-user schematic)

A **section** is one hierarchical sheet = one `.kicad_sch` file. Give a sheet an
owner and only that person (or a manager) can change it:

* claim from the dashboard (Sections page) or `--claim power.kicad_sch` on the agent;
* a manager (or the owner) can reassign or release; anyone can claim an unowned sheet;
* a non-owner's save is **refused by the server**, their sheet is put back to the
  owner's version, their edit is kept in `.kicad_live/backup/`, and the refusal
  appears in Activity and as a dashboard toast;
* unowned sheets behave exactly as before (merged, shared).

Why files: eeschema cannot be made read-only from outside, so ownership is
enforced when the file is shared. Roles are declared by the client (`--role`);
there is no authentication yet, so this prevents accidents, not attackers.

## 2. Project inheritance

Agents share, in addition to the schematic sheets: the board (`.kicad_pcb`), the
project file (`.kicad_pro`) and `sym-lib-table` / `fp-lib-table`.

| File | How it is shared |
|------|------------------|
| schematic sheets | merged, auto-reloaded (as in v2.0) |
| `.kicad_pcb` (live API mode) | **published** when saved (last save wins) so newcomers can start from it; never merged into an open board |
| `.kicad_pcb` (file-only mode) | fully synced and merged |
| `.kicad_pro`, library tables | handed to newcomers; never overwritten on the server |

A computer with no project runs `--inherit-only` and receives everything. `--inherit`
replaces an existing copy with the team's (yours is backed up first).

## 3. Schematic <-> PCB cross-reference (Components page)

Joins the schematic and the PCB by reference designator and lists disagreements
first: not placed on the PCB, on the PCB but not in the schematic, footprint
differs, value differs, no footprint assigned. Updates live. It is the groundwork
for the BOM (no BOM logic is included).

## Tests

* `tests/unit/test_v3_features.py` (24) - rules, agents through a fake server, inheritance, cross-reference.
* `tests/integration/test_v3_integration.py` (3) - real server + WebSockets.
* End to end with real programs (real pcbnew + live agent, `--inherit-only`
  newcomer, ownership refusal, dashboard): 12 of 12 passed on one machine. Two
  physical computers have not been tested.
