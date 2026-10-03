"""Synthetic boards for the fab tests (never real product data; not in the wheel).

``board_text`` writes a small KiCad 10 board: a resistor on top (and one on the bottom), a
two-pin header, an NPTH mounting hole, an NPTH slot, optional castellated pad, optional
``yapnr.rf`` footprint and LCSC fields, joined by tracks and a via whose sizes the tests vary to
plant violations. ``project_text`` writes the matching ``.kicad_pro``. ``FakeKicadCli`` is a
kicad-cli stand-in for the pipeline tests: it writes canned DRC, stats, gerber and drill files
with real X2 headers, so the whole build runs without KiCad.
"""

from __future__ import annotations

import json
import uuid as uuid_mod
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# KiCad 10's non-copper layer table (each line indented by two tabs in the board file).
LAYER_TABLE = "\n".join(
    "\t\t" + line
    for line in (
        '(9 "F.Adhes" user "F.Adhesive")',
        '(11 "B.Adhes" user "B.Adhesive")',
        '(13 "F.Paste" user)',
        '(15 "B.Paste" user)',
        '(5 "F.SilkS" user "F.Silkscreen")',
        '(7 "B.SilkS" user "B.Silkscreen")',
        '(1 "F.Mask" user)',
        '(3 "B.Mask" user)',
        '(17 "Dwgs.User" user "User.Drawings")',
        '(19 "Cmts.User" user "User.Comments")',
        '(25 "Edge.Cuts" user)',
        '(27 "Margin" user)',
        '(31 "F.CrtYd" user "F.Courtyard")',
        '(29 "B.CrtYd" user "B.Courtyard")',
        '(35 "F.Fab" user)',
        '(33 "B.Fab" user)',
    )
)


class _Ids:
    def __init__(self, seed: str):
        self.seed, self.n = seed, 0

    def __call__(self) -> str:
        self.n += 1
        return str(uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"{self.seed}/{self.n}"))


def copper_names(layers: int) -> List[str]:
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, layers - 1)] + ["B.Cu"]


def _prop(name: str, value: str, ids, hide: bool = True, layer: str = "F.Fab") -> str:
    hidden = "\n\t\t\t(hide yes)" if hide else ""
    mirror = "\n\t\t\t\t(justify mirror)" if layer.startswith("B.") else ""
    return (
        f'\t\t(property "{name}" "{value}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "{layer}"){hidden}\n'
        f'\t\t\t(uuid "{ids()}")\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n'
        f"\t\t\t\t\t(thickness 0.15)\n\t\t\t\t){mirror}\n\t\t\t)\n\t\t)\n"
    )


def _footprint(
    lib_id: str,
    ref: str,
    value: str,
    at: Tuple[float, float, float],
    pads: Sequence[str],
    ids,
    side: str = "F",
    attrs: str = "smd",
    props: Optional[Dict[str, str]] = None,
    descr: str = "",
    extra: str = "",
) -> str:
    text = (
        f'\t(footprint "{lib_id}"\n\t\t(layer "{side}.Cu")\n\t\t(uuid "{ids()}")\n'
        f"\t\t(at {at[0]} {at[1]} {at[2]})\n"
    )
    if descr:
        text += f'\t\t(descr "{descr}")\n'
    text += _prop("Reference", ref, ids, hide=False, layer=f"{side}.Fab")
    text += _prop("Value", value, ids, layer=f"{side}.Fab")
    for key, val in (props or {}).items():
        text += _prop(key, val, ids, layer=f"{side}.Fab")
    text += f"\t\t(attr {attrs})\n" + extra
    for pad in pads:
        text += pad.replace("{uuid}", ids())
    return text + "\t)\n"


def _smd_pad(number: str, x: float, side: str, net: Optional[str], w=1.025, h=1.4) -> str:
    layers = f'"{side}.Cu" "{side}.Mask" "{side}.Paste"'
    net_line = f'\t\t\t(net "{net}")\n' if net else ""
    return (
        f'\t\t(pad "{number}" smd roundrect\n\t\t\t(at {x} 0)\n\t\t\t(size {w} {h})\n'
        f"\t\t\t(layers {layers})\n\t\t\t(roundrect_rratio 0.25)\n{net_line}"
        '\t\t\t(uuid "{uuid}")\n\t\t)\n'
    )


def _tht_pad(
    number: str,
    x: float,
    y: float,
    net: Optional[str],
    shape="circle",
    size=1.7,
    drill=1.0,
    castellated: bool = False,
) -> str:
    net_line = f'\t\t\t(net "{net}")\n' if net else ""
    prop = "\t\t\t(property pad_prop_castellated)\n" if castellated else ""
    return (
        f'\t\t(pad "{number}" thru_hole {shape}\n\t\t\t(at {x} {y})\n\t\t\t(size {size} {size})\n'
        f'\t\t\t(drill {drill})\n\t\t\t(layers "*.Cu" "*.Mask")\n{prop}{net_line}'
        '\t\t\t(uuid "{uuid}")\n\t\t)\n'
    )


def _npth_pad(x: float, y: float, drill: float, oval: Optional[Tuple[float, float]] = None) -> str:
    if oval:
        size = f"{oval[0]} {oval[1]}"
        drill_text = f"(drill oval {oval[0]} {oval[1]})"
        shape = "oval"
    else:
        size = f"{drill} {drill}"
        drill_text = f"(drill {drill})"
        shape = "circle"
    return (
        f'\t\t(pad "" np_thru_hole {shape}\n\t\t\t(at {x} {y})\n\t\t\t(size {size})\n'
        f'\t\t\t{drill_text}\n\t\t\t(layers "*.Cu" "*.Mask")\n\t\t\t(uuid "{{uuid}}")\n\t\t)\n'
    )


def _segment(a, b, width, layer, net, ids) -> str:
    return (
        f"\t(segment\n\t\t(start {a[0]} {a[1]})\n\t\t(end {b[0]} {b[1]})\n\t\t(width {width})\n"
        f'\t\t(layer "{layer}")\n\t\t(net "{net}")\n\t\t(uuid "{ids()}")\n\t)\n'
    )


def _via(at, size, drill, net, ids) -> str:
    return (
        f"\t(via\n\t\t(at {at[0]} {at[1]})\n\t\t(size {size})\n\t\t(drill {drill})\n"
        f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "{net}")\n\t\t(uuid "{ids()}")\n\t)\n'
    )


def rf_description(er: float = 3.55, tan_delta: float = 0.0027, h_mm: float = 0.813) -> str:
    """The description yapnr.rf.export.kicad writes on an RF footprint."""
    return (
        f"yapnr RF inverse design 'test-line': microstrip copper on er {er:g}, tan_delta "
        f"{tan_delta:g}, h {h_mm:g} mm with a solid ground on the next copper layer; spec sha256 "
        + "0" * 63
        + "1"
    )


def board_text(
    layers: int = 2,
    *,
    track_width: float = 0.25,
    via: Tuple[float, float] = (0.6, 0.3),
    via_in_pad: Optional[Tuple[float, float]] = None,
    npth: bool = True,
    slot: bool = True,
    castellated: bool = False,
    bottom_part: bool = True,
    rf: Optional[Dict[str, float]] = None,
    lcsc: bool = True,
    size: Tuple[float, float] = (30.0, 20.0),
    second_outline: bool = False,
    thickness: float = 1.6,
    seed: str = "yapnr-fab-test",
) -> str:
    """A small KiCad 10 board (origin at 100, 100) with the planted features asked for."""
    ids = _Ids(seed)
    ox, oy = 100.0, 100.0
    w, h = size
    cu = copper_names(layers)
    ordinals = {"F.Cu": 0, "B.Cu": 2}
    for i, name in enumerate(cu[1:-1], 1):
        ordinals[name] = 2 + 2 * i
    layer_lines = "\n".join(f'\t\t({ordinals[n]} "{n}" signal)' for n in cu)
    parts = [
        '(kicad_pcb\n\t(version 20260206)\n\t(generator "yapnr-test")\n'
        '\t(generator_version "10.0")\n'
        f"\t(general\n\t\t(thickness {thickness})\n\t\t(legacy_teardrops no)\n\t)\n"
        '\t(paper "A4")\n'
        f"\t(layers\n{layer_lines}\n{LAYER_TABLE}\n\t)\n"
        "\t(setup\n\t\t(pad_to_mask_clearance 0)\n"
        "\t\t(allow_soldermask_bridges_in_footprints no)\n"
        "\t\t(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)\n\t\t)\n\t)\n"
    ]
    r1 = (ox + 8, oy + 6, 0)
    r1_props = (
        {"LCSC": "C990000001", "Manufacturer": "Yapnr", "Partnumber": "YP-R0805-1K"} if lcsc else {}
    )
    parts.append(
        _footprint(
            "Resistor_SMD:R_0805_2012Metric",
            "R1",
            "1k",
            r1,
            [_smd_pad("1", -0.9125, "F", "A"), _smd_pad("2", 0.9125, "F", "B")],
            ids,
            props=r1_props,
        )
    )
    if bottom_part:
        r2_props = {"LCSC": "C990000002", "Partnumber": "YP-R0805-10K"} if lcsc else {}
        parts.append(
            _footprint(
                "Resistor_SMD:R_0805_2012Metric",
                "R2",
                "10k",
                (ox + 8, oy + 12, 90),
                [_smd_pad("1", -0.9125, "B", None), _smd_pad("2", 0.9125, "B", None)],
                ids,
                side="B",
                props=r2_props,
            )
        )
    j1 = (ox + 20, oy + 6, 0)
    parts.append(
        _footprint(
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            "J1",
            "Conn_01x02",
            j1,
            [_tht_pad("1", 0, 0, "A", "rect"), _tht_pad("2", 0, 2.54, "B")],
            ids,
            attrs="through_hole",
            props={"LCSC": "C990000003"} if lcsc else {},
        )
    )
    if npth:
        parts.append(
            _footprint(
                "MountingHole:MountingHole_2.2mm_M2",
                "H1",
                "MountingHole",
                (ox + 24, oy + 15, 0),
                [_npth_pad(0, 0, 2.2)],
                ids,
                attrs="exclude_from_pos_files exclude_from_bom",
            )
        )
    if slot:
        parts.append(
            _footprint(
                "yapnr_test:Slot_1x2",
                "H2",
                "Slot",
                (ox + 14, oy + 15, 0),
                [_npth_pad(0, 0, 1.0, oval=(1.0, 2.0))],
                ids,
                attrs="exclude_from_pos_files exclude_from_bom",
            )
        )
    if castellated:
        parts.append(
            _footprint(
                "yapnr_test:Castellated_1",
                "J2",
                "Edge",
                (ox + w, oy + 10, 0),
                [_tht_pad("1", 0, 0, None, "circle", 1.2, 0.6, castellated=True)],
                ids,
                attrs="through_hole",
                props={"LCSC": "C990000004"} if lcsc else {},
            )
        )
    if rf is not None:
        rect = (
            '\t\t(pad "1" smd rect\n\t\t\t(at 0 0)\n\t\t\t(size 4 0.44)\n'
            '\t\t\t(layers "F.Cu" "F.Mask")\n\t\t\t(uuid "{uuid}")\n\t\t)\n'
        )
        parts.append(
            _footprint(
                "yapnr_rf:RF_test-line",
                "RF1",
                "RF_test-line",
                (ox + 24, oy + 1.5, 0),
                [rect],
                ids,
                attrs="smd exclude_from_pos_files exclude_from_bom",
                descr=rf_description(**rf),
            )
        )
    # Net A: R1.1 -> J1.1 on F.Cu. Net B: R1.2 -> via -> B.Cu -> J1.2.
    pad_a = (r1[0] - 0.9125, r1[1])
    pad_b = (r1[0] + 0.9125, r1[1])
    j1_1, j1_2 = (j1[0], j1[1]), (j1[0], j1[1] + 2.54)
    parts.append(_segment(pad_a, (pad_a[0], oy + 3), track_width, "F.Cu", "A", ids))
    parts.append(_segment((pad_a[0], oy + 3), (j1_1[0], oy + 3), track_width, "F.Cu", "A", ids))
    parts.append(_segment((j1_1[0], oy + 3), j1_1, track_width, "F.Cu", "A", ids))
    if via_in_pad:
        parts.append(_via(pad_b, via_in_pad[0], via_in_pad[1], "B", ids))
        parts.append(_segment(pad_b, (pad_b[0], j1_2[1]), 0.25, "B.Cu", "B", ids))
    else:
        v = (pad_b[0] + 2.0, pad_b[1])
        parts.append(_segment(pad_b, v, 0.25, "F.Cu", "B", ids))
        parts.append(_via(v, via[0], via[1], "B", ids))
        parts.append(_segment(v, (v[0], j1_2[1]), 0.25, "B.Cu", "B", ids))
        pad_b = v
    parts.append(_segment((pad_b[0], j1_2[1]), j1_2, 0.25, "B.Cu", "B", ids))
    outlines = [(ox, oy, ox + w, oy + h)]
    if second_outline:
        outlines.append((ox + w + 5, oy, ox + w + 15, oy + 10))
    for x0, y0, x1, y1 in outlines:
        parts.append(
            f"\t(gr_rect\n\t\t(start {x0} {y0})\n\t\t(end {x1} {y1})\n\t\t(stroke\n"
            f'\t\t\t(width 0.1)\n\t\t\t(type solid)\n\t\t)\n\t\t(fill no)\n\t\t(layer "Edge.Cuts")\n'
            f'\t\t(uuid "{ids()}")\n\t)\n'
        )
    parts.append("\t(embedded_fonts no)\n)\n")
    return "".join(parts)


def project_text(name: str, clearance: float = 0.15, track: float = 0.25) -> str:
    """A minimal ``.kicad_pro`` with one Default netclass and loose board minimums."""
    doc = {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.1,
                    "min_track_width": 0.1,
                    "min_via_diameter": 0.3,
                    "min_via_annular_width": 0.05,
                    "min_through_hole_diameter": 0.15,
                    "min_hole_clearance": 0.15,
                    "min_hole_to_hole": 0.2,
                    "min_copper_edge_clearance": 0.2,
                },
                "rule_severities": {
                    "missing_courtyard": "ignore",
                    "lib_footprint_issues": "ignore",
                    "lib_footprint_mismatch": "ignore",
                    "silk_overlap": "ignore",
                    "silk_over_copper": "ignore",
                    "silk_edge_clearance": "ignore",
                    "text_height": "ignore",
                    "text_thickness": "ignore",
                    "footprint_type_mismatch": "ignore",
                    "footprint_filters_mismatch": "ignore",
                },
            }
        },
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": clearance,
                    "track_width": track,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "priority": 2147483647,
                }
            ],
            "meta": {"version": 4},
        },
    }
    return json.dumps(doc, indent=2) + "\n"


def write_board(directory: Path, name: str = "testboard", project: bool = True, **kwargs) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    board = directory / f"{name}.kicad_pcb"
    board.write_text(board_text(**kwargs))
    if project:
        board.with_suffix(".kicad_pro").write_text(project_text(name))
    return board


# ------------------------------------------------------------------------- fake kicad-cli

_GERBER = """%TF.GenerationSoftware,KiCad,Pcbnew,10.0.6*%
%TF.CreationDate,{date}+00:00*%
%TF.ProjectId,{name},00000000-0000-0000-0000-000000000000,rev?*%
%TF.SameCoordinates,Original*%
%TF.FileFunction,{function}*%
%TF.FilePolarity,Positive*%
%FSLAX46Y46*%
G04 Gerber Fmt 4.6, Leading zero omitted, Abs format (unit mm)*
G04 Created by KiCad (PCBNEW 10.0.6) date {space}*
%MOMM*%
%LPD*%
G01*
G04 APERTURE LIST*
%ADD10C,0.100000*%
G04 APERTURE END LIST*
D10*
X100000000Y-100000000D02*
X130000000Y-100000000D01*
X130000000Y-120000000D01*
X100000000Y-120000000D01*
X100000000Y-100000000D01*
M02*
"""

_DRILL = """M48
; DRILL file KiCad 10.0.6 date {date}
; FORMAT={{-:-/ absolute / {units} / decimal}}
; #@! TF.CreationDate,{date}+00:00
; #@! TF.GenerationSoftware,Kicad,Pcbnew,10.0.6
; #@! TF.FileFunction,{function}
FMAT,2
{unit_word}
T1C{tool}
%
G90
G05
T1
{holes}M30
"""

_PROTEL = {
    "F.Cu": "gtl",
    "B.Cu": "gbl",
    "F.Mask": "gts",
    "B.Mask": "gbs",
    "F.Silkscreen": "gto",
    "B.Silkscreen": "gbo",
    "F.Paste": "gtp",
    "B.Paste": "gbp",
    "Edge.Cuts": "gm1",
}


def x2_function(layer: str, n_copper: int) -> str:
    if layer == "F.Cu":
        return "Copper,L1,Top"
    if layer == "B.Cu":
        return f"Copper,L{n_copper},Bot"
    if layer.startswith("In"):
        return f"Copper,L{int(layer[2:-3]) + 1},Inr"
    side = "Top" if layer.startswith("F.") else "Bot"
    kind = {"Mask": "Soldermask", "Silkscreen": "Legend", "Paste": "Paste"}.get(layer[2:])
    return "Profile,NP" if layer == "Edge.Cuts" else f"{kind},{side}"


class FakeKicadCli:
    """Stands in for ``yapnr.fab.kicad.Cli``: canned outputs with real headers and names.

    ``date`` is the time stamp it writes (vary it to prove normalization); ``violations`` and
    ``stats`` override the DRC report and the board statistics; ``calls`` records every call.
    """

    def __init__(self, date: str = "2026-10-02T12:34:56", violations=None, stats=None, npth=False):
        self.date = date
        self.violations = violations or []
        self.stats_doc = stats
        self.npth = npth
        self.calls: List[str] = []

    def version(self) -> str:
        return "10.0.6"

    def drc(self, board: Path, report: Path, refill: bool = True, save: bool = True) -> Dict:
        self.calls.append("drc")
        doc = {
            "$schema": "https://schemas.kicad.org/drc.v1.json",
            "date": self.date,
            "kicad_version": "10.0.6",
            "source": Path(board).name,
            "ignored_checks": [],
            "unconnected_items": [],
            "violations": list(self.violations),
            "schematic_parity": [],
        }
        Path(report).write_text(json.dumps(doc))
        return doc

    def stats(self, board: Path, out: Path) -> Dict:
        self.calls.append("stats")
        doc = self.stats_doc or {
            "board": {"has_outline": True, "width": "30.0000 mm", "height": "20.0000 mm"},
            "pads": {"castellated": 0},
            "vias": {"through": 1, "blind": 0, "buried": 0, "micro": 0},
            "drill_holes": [
                {"count": 1, "shape": "Round", "x_size": "0.3000 mm", "y_size": "0.3000 mm"}
            ],
        }
        Path(out).write_text(json.dumps(doc))
        return doc

    def gerbers(self, board, outdir, layers, protel=True, subtract_soldermask=True, x2=True):
        self.calls.append("gerbers")
        name = Path(board).stem
        n_cu = sum(1 for layer in layers if layer.endswith(".Cu"))
        for layer in layers:
            if protel:
                ext = _PROTEL.get(layer) or f"g{int(layer[2:-3])}"
            else:
                ext = "gbr"
            path = Path(outdir) / f"{name}-{layer.replace('.', '_')}.{ext}"
            path.write_text(
                _GERBER.format(
                    date=self.date,
                    space=self.date.replace("T", " "),
                    name=name,
                    function=x2_function(layer, n_cu),
                )
            )
        (Path(outdir) / f"{name}-job.gbrjob").write_text(
            json.dumps({"Header": {"CreationDate": self.date + "+00:00"}}, indent=2) + "\n"
        )

    def drill(
        self,
        board,
        outdir,
        units,
        zeros,
        oval,
        origin="absolute",
        separate_th=False,
        map_format=None,
    ):
        self.calls.append("drill")
        name = Path(board).stem
        unit_word, tool = ("INCH", "0.0118") if units == "in" else ("METRIC", "0.300")
        hole = "X4.33Y-4.3\n" if units == "in" else "X110.0Y-109.0\n"
        files = [("", "MixedPlating,1,2", hole)]
        if separate_th:
            files = [
                ("-PTH", "Plated,1,2,PTH", hole),
                ("-NPTH", "NonPlated,1,2,NPTH", hole if self.npth else ""),
            ]
        for suffix, function, holes in files:
            (Path(outdir) / f"{name}{suffix}.drl").write_text(
                _DRILL.format(
                    date=self.date,
                    units="inch" if units == "in" else "metric",
                    unit_word=unit_word,
                    tool=tool,
                    function=function,
                    holes=holes,
                )
            )
            if map_format:
                (Path(outdir) / f"{name}{suffix}-drl_map.gbr").write_text(
                    _GERBER.format(
                        date=self.date,
                        space=self.date.replace("T", " "),
                        name=name,
                        function="Drillmap",
                    )
                )

    def ipcd356(self, board, out):
        self.calls.append("ipcd356")
        Path(out).write_text("C  IPC-D-356 netlist (fake)\nP  CODE 00\n999\n")
