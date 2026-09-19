"""What a part IS, not just what it is called on this sheet.

KiCad gives every part a *reference designator* (`U1`, `U5`, `R3`). That is only
a slot name: two boards can both have a `U5` that are completely different
chips. The identity of the part is in its other fields:

    reference    U5                       where it sits in the design
    component    ESP32-WROOM-32           the part's Value field (its name)
    part         ESP32-WROOM-32E          the library symbol / footprint part name
    lib_id       RF_Module:ESP32-WROOM-32 the full library identifier (symbols)
    footprint    RF_Module:ESP32-WROOM-32 the PCB footprint library id
    description  "802.11 b/g/n module"    the Description field, when set

Everything here is best-effort and never invents data: a field KiCad did not
provide is simply absent, and `label()` falls back to the bare reference.
This is the groundwork the future BOM system will read; it deliberately does
no BOM work itself.
"""
from __future__ import annotations

from typing import Any

MAX_LEN = 120


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:MAX_LEN]


def part_name(lib_id: str) -> str:
    """'Sensor_Motion:MPU-6050' -> 'MPU-6050'."""
    lib_id = _clean(lib_id)
    return lib_id.rsplit(":", 1)[-1] if lib_id else ""


def identity_of(state: dict | None) -> dict:
    """Identity fields of a PCB footprint state or a schematic symbol state.

    `component` is the Value field when it is informative (not empty, not just
    the reference repeated), otherwise the library part name.
    """
    state = state or {}
    reference = _clean(state.get("reference"))
    # `component` may already be a computed name (an identity block re-read).
    value = _clean(state.get("value")) or _clean(state.get("component"))
    lib_id = _clean(state.get("lib_id"))
    footprint = _clean(state.get("footprint"))
    description = _clean(state.get("description"))
    part = _clean(state.get("part")) or part_name(lib_id) or part_name(footprint)

    component = value if value and value.lower() != reference.lower() else part
    out = {
        "reference": reference,
        "component": component,
        "part": part,
        "lib_id": lib_id,
        "footprint": footprint,
        "description": description,
    }
    return {k: v for k, v in out.items() if v}


def label(state_or_identity: dict | None, reference: str = "") -> str:
    """'U5 (ESP32-WROOM)' when a name is known, else 'U5'."""
    ident = identity_of(state_or_identity)
    ref = ident.get("reference") or _clean(reference)
    name = ident.get("component", "")
    if ref and name and name.lower() != ref.lower():
        return f"{ref} ({name})"
    return ref or name or "?"


def context_of(state: dict | None) -> dict:
    """The small identity block attached to every change and event.

    Excludes the description (long) and anything not a plain string, so it is
    always safe to put in a WebSocket message and in the database.
    """
    ident = identity_of(state)
    ident.pop("description", None)
    return ident
