"""Synthetic atopile parts and projects for tests (never real product data).

``write_part`` writes a two-pad resistor-like atomic part whose footprint and symbol are drawn
here, not generated from any library; ``write_project`` a small project that uses it, either
directly (no picking) or through a pick by LCSC id or by type, served by the local picker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

SYNTHETIC_LCSC = "C990000001"
SYNTHETIC_PART = "Yapnr_Synthetic_SR1K"

FOOTPRINT = """(footprint "SYN_R0603"
\t(version 20241229)
\t(generator "yapnr")
\t(layer "F.Cu")
\t(property "Reference" "REF**"
\t\t(at 0 -1.5 0)
\t\t(layer "F.SilkS")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.15)
\t\t\t)
\t\t)
\t)
\t(property "Value" "SYN_R0603"
\t\t(at 0 1.5 0)
\t\t(layer "F.Fab")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.15)
\t\t\t)
\t\t)
\t)
\t(attr smd)
\t(fp_line
\t\t(start -1.5 -0.75)
\t\t(end 1.5 -0.75)
\t\t(stroke
\t\t\t(width 0.12)
\t\t\t(type solid)
\t\t)
\t\t(layer "F.SilkS")
\t)
\t(fp_line
\t\t(start -1.5 0.75)
\t\t(end 1.5 0.75)
\t\t(stroke
\t\t\t(width 0.12)
\t\t\t(type solid)
\t\t)
\t\t(layer "F.SilkS")
\t)
\t(pad "1" smd roundrect
\t\t(at -0.8 0)
\t\t(size 0.8 0.95)
\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t(roundrect_rratio 0.25)
\t)
\t(pad "2" smd roundrect
\t\t(at 0.8 0)
\t\t(size 0.8 0.95)
\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t(roundrect_rratio 0.25)
\t)
)
"""


def _pin(number: str, x: float, angle: int) -> str:
    return f"""\t\t\t(pin passive line
\t\t\t\t(at {x} 0 {angle})
\t\t\t\t(length 2.54)
\t\t\t\t(name "{number}"
\t\t\t\t\t(effects
\t\t\t\t\t\t(font
\t\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t\t)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t\t(number "{number}"
\t\t\t\t\t(effects
\t\t\t\t\t\t(font
\t\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t\t)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
"""


def _property(name: str, value: str, y: float, hidden: bool = False) -> str:
    hide = "\t\t\t\t(hide yes)\n" if hidden else ""
    return f"""\t\t(property "{name}" "{value}"
\t\t\t(at 0 {y} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
{hide}\t\t\t)
\t\t)
"""


SYMBOL = (
    '(kicad_symbol_lib\n\t(version 20241229)\n\t(generator "yapnr")\n\t(symbol "SR1K"\n'
    + _property("Reference", "R", 5.08)
    + _property("Value", "SR1K", -5.08)
    + _property("Footprint", "", -7.62, hidden=True)
    + _property("Datasheet", "", -10.16, hidden=True)
    + '\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(symbol "SR1K_0_1"\n'
    + "\t\t\t(rectangle\n\t\t\t\t(start -2.54 1.02)\n\t\t\t\t(end 2.54 -1.02)\n"
    + "\t\t\t\t(stroke\n\t\t\t\t\t(width 0)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n"
    + "\t\t\t\t(fill\n\t\t\t\t\t(type background)\n\t\t\t\t)\n\t\t\t)\n"
    + _pin("1", -5.08, 0)
    + _pin("2", 5.08, 180)
    + "\t\t)\n\t)\n)\n"
)


def part_ato(name: str = SYNTHETIC_PART, lcsc: str = SYNTHETIC_LCSC) -> str:
    return f'''import has_designator_prefix
import has_part_picked
import is_atomic_part

component {name}_package:
    """Synthetic 1 kohm 0603 resistor (a yapnr test part, not a real product)"""

    trait is_atomic_part<manufacturer="yapnr-synthetic", partnumber="SR1K", footprint="SYN_R0603.kicad_mod", symbol="SR1K.kicad_sym">
    trait has_part_picked::by_supplier<supplier_id="lcsc", supplier_partno="{lcsc}", manufacturer="yapnr-synthetic", partno="SR1K">
    trait has_designator_prefix<prefix="R">

    # pins
    pin 1
    signal p1 ~ pin 1
    pin 2
    signal p2 ~ pin 2
'''  # noqa: E501


def write_part(parts_dir: Path, name: str = SYNTHETIC_PART, lcsc: str = SYNTHETIC_LCSC) -> Path:
    part = Path(parts_dir) / name
    part.mkdir(parents=True, exist_ok=True)
    (part / f"{name}.ato").write_text(part_ato(name, lcsc), encoding="utf-8")
    (part / "SYN_R0603.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (part / "SR1K.kicad_sym").write_text(SYMBOL, encoding="utf-8")
    return part


def catalog_entry(lcsc: str = SYNTHETIC_LCSC) -> Dict[str, object]:
    return {
        "lcsc": lcsc,
        "mpn": "SR1K",
        "manufacturer": "yapnr-synthetic",
        "package": "0603",
        "kind": "resistor",
        "description": "synthetic 1 kohm 1 % 0603 resistor",
        "params": {
            "resistance_ohm": 1000.0,
            "tolerance_pct": 1.0,
            "power_max_w": 0.1,
            "voltage_max_v": 75.0,
        },
        "basic": True,
    }


PICKS = {
    None: "",
    "lcsc": '    r_pick = new Resistor\n    r_pick.lcsc_id = "{lcsc}"\n',
    "type": "    r_pick = new Resistor\n    r_pick.resistance = 1kohm +/- 5%\n"
    '    r_pick.package = "0603"\n',
}


def write_project(root: Path, pick: Optional[str] = None, name: str = SYNTHETIC_PART) -> Path:
    """A voltage divider of two pre-picked parts, plus one picked resistor when ``pick`` is set."""
    root = Path(root)
    (root / "elec/src").mkdir(parents=True, exist_ok=True)
    (root / "ato.yaml").write_text(
        "requires-atopile: '>=0.15.0,<0.16.0'\n\nbuilds:\n  default:\n"
        "    entry: elec/src/main.ato:Main\n",
        encoding="utf-8",
    )
    main = (
        "import ElectricPower\n"
        + ("import Resistor\n" if pick else "")
        + f'\nfrom "parts/{name}/{name}.ato" import {name}_package\n\n\n'
        + "module Main:\n"
        + '    """A synthetic voltage divider."""\n'
        + "    power = new ElectricPower\n"
        + "    signal mid\n"
        + f"    r_top = new {name}_package\n"
        + f"    r_bot = new {name}_package\n"
        + "    r_top.p1 ~ power.hv\n"
        + "    r_top.p2 ~ mid\n"
        + "    r_bot.p1 ~ mid\n"
        + "    r_bot.p2 ~ power.lv\n"
    )
    if pick:
        main += PICKS[pick].format(lcsc=SYNTHETIC_LCSC)
        main += "    r_pick.unnamed[0] ~ mid\n    r_pick.unnamed[1] ~ power.lv\n"
    (root / "elec/src/main.ato").write_text(main, encoding="utf-8")
    return root


def write_fixture(root: Path) -> Path:
    """The committed Bazel fixture (tests/fixtures/atopile/synthetic): one type pick, the part in
    the tree, and a catalog file for the picker."""
    import json

    project = write_project(root, pick="type")
    write_part(project / "elec/src/parts")
    doc = {
        "schema": "yapnr-picker-catalog-v1",
        "provenance": {"source": "yapnr test fixture (synthetic)"},
        "parts": [catalog_entry()],
    }
    (project / "catalog.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return project
