"""Assembly outputs (F1b): BOM and CPL for JLCPCB and PCBWay, from the board's footprints.

Design §6.5. Parts are the footprints with a reference that are neither DNP nor excluded from the
BOM (``yapnr.rf`` footprints exclude themselves). The LCSC id comes from the footprint's fields
(atopile writes ``LCSC`` and ``lcsc_id``), the MPN from ``Partnumber``/``MPN``; with a parts lock,
every LCSC id must be one of the lock's.

Placement (CPL) follows Fabrication Toolkit's published conversion [FT]: the part's centroid (the
centre of its pads' bounding box in the footprint's own frame, as JLCPCB's "Mid X/Mid Y" asks;
the anchor when it has no pads) in millimetres with Y up (KiCad's Y negated, absolute origin, the
gerbers' coordinates), rotation counter-clockwise; JLCPCB bottom-side rotation is ``180 - r`` as
seen from the top. Footprints excluded from position files stay in the BOM but not in the CPL. No
vendor correction table is copied into yapnr: every part whose rotation nobody checked in JLC's
preview is listed on the card.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from yapnr.fab.board import Board, Footprint
from yapnr.fab.check import Finding

LCSC_FIELDS = ("LCSC Part #", "JLCPCB Part #", "LCSC", "lcsc_id", "LCSC Part")
MPN_FIELDS = ("Partnumber", "MPN", "Manufacturer Part Number", "Mfr. Part #", "MFR.Part")
MFR_FIELDS = ("Manufacturer", "MFR", "Mfr")
_LCSC = re.compile(r"^C\d+$")


@dataclass
class Part:
    reference: str
    value: str
    footprint: str
    side: str  # top, bottom
    x_mm: float
    y_mm: float  # Y up (KiCad's Y negated)
    rotation: float  # KiCad's footprint orientation, counter-clockwise
    mount: str
    lcsc: str
    mpn: str
    manufacturer: str
    consigned: bool = False
    in_pos: bool = True  # False: excluded from position files (BOM only)


def natural_key(ref: str) -> Tuple:
    """R2 before R10."""
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref))


def _footprint_name(fp: Footprint) -> str:
    return fp.lib_id.split(":", 1)[-1]


def centroid(fp: Footprint) -> Tuple[float, float]:
    """The centre of the pads' bounding box, in board millimetres (Y down) [FT].

    The box is taken in the footprint's own frame, so a part at any angle gets the same centre;
    each pad counts as its rotated size rectangle. No pads: the anchor.
    """
    if not fp.pads:
        return fp.at[0], fp.at[1]
    ox, oy, a = fp.at[0], fp.at[1], math.radians(fp.at[2])
    us, vs = [], []
    for pad in fp.pads:
        b = math.radians(pad.at[2])
        for cx in (-0.5, 0.5):
            for cy in (-0.5, 0.5):
                pu, pv = cx * pad.size[0], cy * pad.size[1]
                # pad frame to board offsets (KiCad: counter-clockwise, Y down)
                dx = pad.at[0] - ox + pu * math.cos(b) + pv * math.sin(b)
                dy = pad.at[1] - oy - pu * math.sin(b) + pv * math.cos(b)
                # board offsets to the footprint frame
                us.append(dx * math.cos(a) - dy * math.sin(a))
                vs.append(dx * math.sin(a) + dy * math.cos(a))
    u, v = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
    return ox + u * math.cos(a) + v * math.sin(a), oy - u * math.sin(a) + v * math.cos(a)


def collect(board: Board, consign: Iterable[str] = ()) -> Tuple[List[Part], Dict[str, List[str]]]:
    """(parts to place, skipped references by reason)."""
    consign = set(consign)
    parts: List[Part] = []
    skipped: Dict[str, List[str]] = {"dnp": [], "excluded": [], "rf": [], "unnamed": []}
    for fp in board.footprints:
        ref = fp.reference.strip()
        if not ref or ref.startswith(("#", "*")) or "?" in ref:
            skipped["unnamed"].append(ref or fp.lib_id)
            continue
        if fp.rf is not None:
            skipped["rf"].append(ref)
            continue
        if fp.excluded_from_bom:
            skipped["excluded"].append(ref)
            continue
        if fp.dnp:
            skipped["dnp"].append(ref)
            continue
        x, y = centroid(fp)
        parts.append(
            Part(
                reference=ref,
                value=fp.value,
                footprint=_footprint_name(fp),
                side=fp.side,
                x_mm=round(x, 6),
                y_mm=round(-y, 6),
                rotation=fp.at[2],
                mount=fp.mount,
                lcsc=fp.field(*LCSC_FIELDS).upper(),
                mpn=fp.field(*MPN_FIELDS),
                manufacturer=fp.field(*MFR_FIELDS),
                consigned=ref in consign,
                in_pos=not fp.excluded_from_pos,
            )
        )
    for key in skipped:
        skipped[key].sort(key=natural_key)
    return sorted(parts, key=lambda p: natural_key(p.reference)), skipped


def _num(x: float) -> str:
    text = f"{x:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def jlc_rotation(part: Part) -> float:
    """JLCPCB CPL rotation: top as KiCad's, bottom ``180 - r`` (as seen from the top) [FT]."""
    r = part.rotation if part.side == "top" else 180.0 - part.rotation
    return r % 360.0


def _groups(parts: Sequence[Part], key) -> List[List[Part]]:
    groups: Dict[tuple, List[Part]] = {}
    for p in parts:
        groups.setdefault(key(p), []).append(p)
    rows = [sorted(g, key=lambda p: natural_key(p.reference)) for g in groups.values()]
    return sorted(rows, key=lambda g: natural_key(g[0].reference))


def jlc_bom(parts: Sequence[Part]) -> List[List[str]]:
    """``Comment, Designator, Footprint, JLCPCB Part #``: one row per (LCSC id, value, footprint)."""
    placed = [p for p in parts if not p.consigned]
    return [
        [g[0].value, ",".join(p.reference for p in g), g[0].footprint, g[0].lcsc]
        for g in _groups(placed, lambda p: (p.lcsc, p.value, p.footprint))
    ]


def jlc_cpl(parts: Sequence[Part]) -> List[List[str]]:
    """``Designator, Mid X, Mid Y, Layer, Rotation`` in mm, CCW positive, Top/Bottom."""
    return [
        [
            p.reference,
            _num(p.x_mm),
            _num(p.y_mm),
            "Top" if p.side == "top" else "Bottom",
            _num(jlc_rotation(p)),
        ]
        for p in parts
        if not p.consigned and p.in_pos
    ]


def pcbway_bom(parts: Sequence[Part]) -> List[List[str]]:
    """KiKit's PCBWay columns; the LCSC id rides along in the notes as a sourcing hint."""
    placed = [p for p in parts if not p.consigned]
    rows = []
    for i, g in enumerate(
        _groups(placed, lambda p: (p.mpn, p.manufacturer, p.value, p.footprint, p.lcsc)), 1
    ):
        p = g[0]
        notes = f"LCSC {p.lcsc}" if p.lcsc else ""
        rows.append(
            [
                str(i),
                ",".join(q.reference for q in g),
                str(len(g)),
                p.manufacturer,
                p.mpn,
                p.value,
                p.footprint,
                p.mount,
                notes,
            ]
        )
    return rows


def pcbway_cpl(parts: Sequence[Part]) -> List[List[str]]:
    """The JLC columns without corrections (PCBWay's engineers check placement)."""
    return [
        [
            p.reference,
            _num(p.x_mm),
            _num(p.y_mm),
            "Top" if p.side == "top" else "Bottom",
            _num(p.rotation % 360.0),
        ]
        for p in parts
        if not p.consigned and p.in_pos
    ]


def csv_text(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    writer.writerows(rows)
    return buf.getvalue()


def outputs(vendor: Dict, parts: Sequence[Part]) -> Dict[str, str]:
    """{"bom": csv text, "cpl": csv text} for the vendor."""
    spec = vendor["assembly"]
    if vendor["vendor"] == "jlcpcb":
        bom, cpl = jlc_bom(parts), jlc_cpl(parts)
    else:
        bom, cpl = pcbway_bom(parts), pcbway_cpl(parts)
    return {
        "bom": csv_text(spec["bom"]["columns"], bom),
        "cpl": csv_text(spec["cpl"]["columns"], cpl),
    }


def findings(
    vendor: Dict,
    parts: Sequence[Part],
    skipped: Dict[str, List[str]],
    lock_lcsc: Optional[Iterable[str]] = None,
    checked_rotation: Iterable[str] = (),
) -> List[Finding]:
    """FAB-ASSEMBLY: missing ids, lock conflicts, unchecked rotations, bottom side, DNP."""
    spec = vendor["assembly"]
    src = f"vendors/{vendor['vendor']}.json"
    out: List[Finding] = []
    placed = [p for p in parts if not p.consigned]
    if not placed:
        out.append(Finding("FAB-ASSEMBLY", "error", "no parts to assemble", src))
        return out
    bad = [p.reference for p in placed if p.lcsc and not _LCSC.match(p.lcsc)]
    if bad:
        out.append(
            Finding(
                "FAB-ASSEMBLY", "error", f"LCSC ids that are not C<digits>: {', '.join(bad)}", src
            )
        )
    if spec["part_key"] == "lcsc":
        missing = [p.reference for p in placed if not p.lcsc]
        if missing:
            out.append(
                Finding(
                    "FAB-ASSEMBLY",
                    "error",
                    f"{len(missing)} parts have no LCSC id ({_refs(missing)}): add an LCSC field "
                    "or --consign them",
                    src,
                    {"missing": missing},
                )
            )
    else:
        none = [p.reference for p in placed if not p.mpn and not p.lcsc]
        hint = [p.reference for p in placed if not p.mpn and p.lcsc]
        if none:
            out.append(
                Finding(
                    "FAB-ASSEMBLY",
                    "error",
                    f"{len(none)} parts have no MPN and no LCSC id ({_refs(none)}): add one or "
                    "--consign them",
                    src,
                    {"missing": none},
                )
            )
        if hint:
            out.append(
                Finding(
                    "FAB-ASSEMBLY",
                    "warning",
                    f"{len(hint)} parts have only an LCSC id ({_refs(hint)}): it goes in the BOM "
                    "notes as a sourcing hint",
                    src,
                )
            )
    if lock_lcsc is not None:
        locked = {x.upper() for x in lock_lcsc}
        stray = [p.reference for p in placed if p.lcsc and p.lcsc not in locked]
        if stray:
            out.append(
                Finding(
                    "FAB-ASSEMBLY",
                    "error",
                    f"LCSC ids not in the parts lock: {_refs(stray)}",
                    "yapnr-parts.lock.json",
                )
            )
    if spec.get("rotation_check"):
        checked = set(checked_rotation)
        unchecked = [p.reference for p in placed if p.reference not in checked]
        if unchecked:
            out.append(
                Finding(
                    "FAB-ASSEMBLY",
                    "warning",
                    f"check rotation in {vendor['title']}'s placement preview: {_refs(unchecked)}",
                    src,
                    {"unchecked_rotation": unchecked},
                )
            )
    bottom = [p.reference for p in placed if p.side == "bottom"]
    if bottom:
        out.append(
            Finding(
                "FAB-ASSEMBLY",
                "warning",
                f"bottom-side parts ({_refs(bottom)}): rotation 180 - r (Fabrication Toolkit's "
                "conversion), not yet confirmed by an assembled order",
                "FT",
            )
        )
    consigned = [p.reference for p in parts if p.consigned]
    if consigned:
        out.append(
            Finding(
                "FAB-ASSEMBLY",
                "info",
                f"not assembled by the vendor (consigned): {_refs(consigned)}",
            )
        )
    if skipped.get("dnp"):
        out.append(Finding("FAB-ASSEMBLY", "info", f"DNP, not placed: {_refs(skipped['dnp'])}"))
    if skipped.get("rf"):
        out.append(
            Finding(
                "FAB-ASSEMBLY", "info", f"RF copper footprints, not parts: {_refs(skipped['rf'])}"
            )
        )
    return out


def _refs(refs: Sequence[str], limit: int = 12) -> str:
    refs = list(refs)
    shown = ", ".join(refs[:limit])
    return shown + (f" and {len(refs) - limit} more" if len(refs) > limit else "")
