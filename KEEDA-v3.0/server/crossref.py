"""Schematic <-> PCB cross-reference.

Joins the two halves of a design by REFERENCE DESIGNATOR and reports where they
disagree: a symbol never placed on the board, a footprint with no symbol, a
value that differs, a footprint that is not the one the schematic assigned.

This is also the groundwork for a BOM: it produces one row per component that
carries both identities (symbol library id + value + PCB footprint). It does no
BOM work itself.

Everything is derived from what the team has already shared - the latest
schematic sheets and either the live PCB state (agents) or the shared board file
(file-only mode). Nothing is invented: a side we have no data for is reported as
"unknown", never as "missing".
"""
from __future__ import annotations

import logging
import re

from common.component_identity import part_name

log = logging.getLogger("kicadlive.crossref")

# Status -> (severity, human text). Order = display priority.
STATUS = {
    "not_placed":         ("warn", "In the schematic but not placed on the PCB"),
    "orphan_footprint":   ("warn", "On the PCB but not in the schematic"),
    "footprint_mismatch": ("warn", "PCB footprint differs from the one the schematic assigned"),
    "value_mismatch":     ("warn", "Value differs between schematic and PCB"),
    "no_footprint":       ("info", "Symbol has no footprint assigned"),
    "ok":                 ("ok", "Schematic and PCB agree"),
}
ORDER = {name: i for i, name in enumerate(STATUS)}


def _ref_key(ref: str):
    m = re.match(r"^([A-Za-z#_]+)(\d+)?", ref or "")
    return (m.group(1) if m else ref, int(m.group(2)) if m and m.group(2) else 0, ref)


def _same_footprint(a: str, b: str) -> bool:
    """'Resistor_SMD:R_0805_2012Metric' == 'R_0805_2012Metric' (a live board only
    reports the part name for some footprints), case-insensitive."""
    if not a or not b:
        return True                         # nothing to compare
    return part_name(a).lower() == part_name(b).lower()


def _same_value(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def footprints_from_pcb_file(path: str) -> list[dict]:
    """Reference / value / footprint library id of every footprint in a .kicad_pcb."""
    from common.sexpr import parse_file
    try:
        root = parse_file(path)
    except (OSError, ValueError):
        return []
    rows = []
    for node in root:
        if not (isinstance(node, list) and node and node[0] == "footprint"):
            continue
        lib = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        ref = value = ""
        for child in node:
            if isinstance(child, list) and len(child) >= 3 and child[0] == "property":
                if child[1] == "Reference":
                    ref = str(child[2])
                elif child[1] == "Value":
                    value = str(child[2])
        if ref:
            rows.append({"reference": ref, "value": value, "footprint": lib})
    return rows


def build(schematic: list[dict], pcb: list[dict], pcb_source: str,
          schematic_known: bool = True) -> dict:
    """Compare the two sides. `schematic` rows need reference/value/footprint/lib_id;
    `pcb` rows reference/value/footprint."""
    sch = {r["reference"]: r for r in schematic if r.get("reference")}
    board = {r["reference"]: r for r in pcb if r.get("reference")}

    rows = []
    for ref in sorted(set(sch) | set(board), key=_ref_key):
        s, b = sch.get(ref), board.get(ref)
        if ref.startswith("#"):
            continue                                       # power flags, not parts
        status = "ok"
        detail = ""
        if s and not b:
            status = "not_placed" if pcb_source != "none" else "ok"
            detail = "" if pcb_source != "none" else "no PCB data yet"
        elif b and not s:
            status = "orphan_footprint" if schematic_known else "ok"
        elif s and b:
            if not s.get("footprint"):
                status = "no_footprint"
            elif not _same_footprint(s.get("footprint", ""), b.get("footprint", "")):
                status = "footprint_mismatch"
                detail = f"schematic: {s.get('footprint')}  |  PCB: {b.get('footprint')}"
            elif not _same_value(s.get("value", ""), b.get("value", "")):
                status = "value_mismatch"
                detail = f"schematic: {s.get('value')}  |  PCB: {b.get('value')}"
        severity, text = STATUS[status]
        rows.append({
            "reference": ref,
            "status": status, "severity": severity, "text": text, "detail": detail,
            "component": (s or b or {}).get("component") or (s or b or {}).get("value", ""),
            "lib_id": (s or {}).get("lib_id", ""),
            "schematic_value": (s or {}).get("value"),
            "pcb_value": (b or {}).get("value"),
            "schematic_footprint": (s or {}).get("footprint"),
            "pcb_footprint": (b or {}).get("footprint"),
            "sheet": (s or {}).get("sheet"),
            "in_schematic": s is not None, "on_pcb": b is not None,
        })
    rows.sort(key=lambda r: (ORDER[r["status"]], _ref_key(r["reference"])))
    counts = {name: 0 for name in STATUS}
    for r in rows:
        counts[r["status"]] += 1
    return {
        "rows": rows, "counts": counts,
        "issues": sum(v for k, v in counts.items() if k not in ("ok",)),
        "pcb_source": pcb_source,
        "schematic_components": len(sch), "pcb_footprints": len(board),
    }
