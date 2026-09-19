# KEEDA v3.0 - stabilisation notes (PCB sync + dashboard identity)

v3.0 starts from the v2.0 code. Nothing was redesigned; these are the fixes.

## 1. PCB / layout sync between two computers

Root causes found (in order of how often they bite):

| # | Cause | Where | Status |
|---|-------|-------|--------|
| 1 | **A component added on one computer never appeared on the other.** `apply_changes` skipped every `add`/`remove` ("out of MVP scope"). | `agent/kicad_link.py` | Fixed: the whole footprint (pads, graphics, fields) travels as base64 protobuf and is created / deleted with one undoable commit. Verified on real KiCad 10.0.6. |
| 2 | **The other agent then retracted the component.** `_on_remote_change` wrote the add into its baseline without touching the board; the next poll saw a part "missing" and sent the server a *remove*. Same for deletions (re-added) and for any change to a footprint the board lacks (phantom baseline entry -> removal). | `agent/sync_agent.py` | Fixed: the baseline only moves for changes that really reached the board (`last_applied`). Reproduced on unmodified v2.0 first. |
| 3 | **Late joiners never received existing components** (state sync skipped unknown footprints). | `agent/sync_agent.py::_sync_state` | Fixed. |
| 4 | **Two boards that are different copies** (footprint uuids differ) silently match nothing. | agent | Now detected and reported ("shares NO footprints", "changes to U5 were IGNORED"). |
| 5 | **Wrong KiCad program owns the API socket** (first KiCad process started - project manager, schematic editor, leftover process - owns `api.sock`; a later PCB editor never gets it). | environment | Diagnosed with a clear error (v2.0.x); the durable fix (launch as a KiCad IPC plugin) is proposed, not built. |
| 6 | The auto-save loop could save **any** open PCB/schematic editor when only one was open, even an unrelated project. | `agent/win_ui.py` | Fixed: only editors whose title matches this project are touched. |

Not covered (by design, still true): only footprints are synchronised live (no tracks, vias, zones, graphics); KiCad's IPC does not let a caller choose ids, but on 10.0.6 the id inside the payload was preserved (verified) - other builds may reassign it (see "ID mapping" in the research list).

## 2. Component identity

`common/component_identity.py` defines what a part IS: `reference` (U5), `component`
(its Value, e.g. ESP32-WROOM), `part` (library part name), `lib_id`, `footprint`,
`description`. PCB footprints now carry the footprint library id; every change and
event carries an identity block; events store `object_name` (SQLite column added by
an in-place migration). The dashboard shows both: `U5 | AMS1117-3.3`, and activity
reads `added U6 (BME280)`. No BOM logic was added.

## 3. Dashboard synchronisation

* Schematic and PCB pages re-read when a teammate changes something (they used to
  load once).
* The Schematic page shows the **team's latest shared schematic** (the file agents
  uploaded), not the static folder the server was started with, and reads symbols
  directly so a server without KiCad still works.
* `project_state` sent to dashboards no longer carries raw footprint data.

## 4. Tests

* `tests/unit/test_v3_pcb_and_identity.py` (23 tests) - add/remove, the retraction
  regression, phantom baseline, late joiner, different-board detection, identity,
  wire format, DB migration, editor safety.
* `tests/manual/test_live_addremove.py` - needs a real KiCad; 16 of 16 passed.
