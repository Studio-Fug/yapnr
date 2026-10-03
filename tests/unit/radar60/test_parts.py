"""The radar60 example's parts (examples/radar60/schematic/tools): every fitted part carries a
verified LCSC id and a manufacturer part number, the parts lock names exactly the generated parts,
and the generated land patterns carry the numbers of the drawings they cite.

Hermetic: no KiCad and no atopile. Parts with a KiCad stock footprint are checked by name only;
their bytes come from the local KiCad library (``gen_parts.py``).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from yapnr.partcache import model
from yapnr.partcache.client import dir_part_id

# examples/radar60/schematic, in the source tree and in Bazel's runfiles
PROJECT = Path(__file__).parents[3] / "examples/radar60/schematic"
sys.path.insert(0, str(PROJECT / "tools"))

import footprints  # noqa: E402
import gen_parts  # noqa: E402

PAD = re.compile(
    r'\(pad "(?P<name>[^"]*)" (?P<kind>\w+) (?P<shape>\w+)\n\t\t\(at (?P<x>[-\d.]+) (?P<y>[-\d.]+)\)\n'
    r"\t\t\(size (?P<w>[\d.]+) (?P<h>[\d.]+)\)(?:\n\t\t\(drill (?P<drill>[\d.]+)\))?"
)
CRTYD = re.compile(r'\(fp_poly\n\t\t\(pts ([^\n]*)\)\n(?:\t\t.*\n)*?\t\t\(layer "F.CrtYd"\)')
XY = re.compile(r"\(xy ([-\d.]+) ([-\d.]+)\)")


def pads(text: str) -> List[Dict]:
    out = []
    for m in PAD.finditer(text):
        d = m.groupdict()
        out.append(
            {
                "name": d["name"],
                "kind": d["kind"],
                "x": float(d["x"]),
                "y": float(d["y"]),
                "w": float(d["w"]),
                "h": float(d["h"]),
                "drill": float(d["drill"]) if d["drill"] else None,
            }
        )
    return out


def courtyard(text: str) -> Tuple[float, float, float, float]:
    pts = [(float(x), float(y)) for x, y in XY.findall(CRTYD.search(text).group(1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


class PartDataTest(unittest.TestCase):
    def setUp(self):
        self.parts = gen_parts.parts()

    def test_no_problems_stop_the_lock(self):
        self.assertEqual(gen_parts.check_parts(self.parts), [])

    def test_every_fitted_part_has_an_lcsc_id_and_part_number(self):
        for p in self.parts:
            if p.board_only:
                self.assertEqual(p.lcsc, "", p.name)
                continue
            self.assertTrue(p.mpn and "TBD" not in p.mpn and p.mfr, p.name)
            if not p.dnp:
                self.assertRegex(p.lcsc, r"^C[1-9][0-9]{0,11}$", p.name)
                # what the part cache reads back out of the generated .ato file
                facts = model.ato_facts(p.name, gen_parts.ato(p, "fp.kicad_mod", "sym.kicad_sym"))
                self.assertEqual((facts["lcsc"], facts["mpn"]), (p.lcsc, p.mpn), p.name)

    def test_check_parts_catches_a_missing_lcsc_id(self):
        broken = [p for p in self.parts if p.name == "Radar60_R_2m_0612"][0]
        broken.lcsc = ""
        self.assertEqual(
            gen_parts.check_parts([broken]), ["Radar60_R_2m_0612: fitted without an LCSC id"]
        )
        broken.lcsc, broken.mpn = "C3917256", "TBD-2mOhm"
        self.assertEqual(
            gen_parts.check_parts([broken]), ["Radar60_R_2m_0612: no manufacturer part number"]
        )

    def test_no_provisional_land_patterns_remain(self):
        for p in self.parts:
            self.assertNotIn("provisional", p.fp.lower(), p.name)
            self.assertNotIn("PROVISIONAL", p.descr, p.name)
            kind, _, ref = p.fp.partition(":")
            if kind == "gen" and ref != "rfm1_placeholder":  # the RF macro stays a placeholder
                text = gen_parts.generated_footprint(ref, p)[1]
                self.assertNotIn('"Unverified"', text, p.name)
                if ref != "kelvin_pads":  # a probe pad pair, not a manufacturer's package
                    self.assertIn('(property "Source"', text, p.name)

    def test_both_fits_share_the_shunt_lands(self):
        by_name = {p.name: p for p in self.parts}
        dev, prod = by_name["Radar60_R_2m_0612"], by_name["Radar60_R_1m_0612"]
        self.assertEqual(dev.fp, prod.fp)
        self.assertEqual(
            (dev.params["resistance_ohm"], prod.params["resistance_ohm"]), (2e-3, 1e-3)
        )
        # the product part never adds IR drop over the development shunt (1.0 V window budget)
        self.assertLessEqual(prod.params["resistance_ohm"], dev.params["resistance_ohm"])

    def test_flash_exposed_pad_is_the_macronix_one(self):
        flash = [p for p in self.parts if p.mpn == "MX25V1635FZNQ"][0]
        d1, e1 = map(float, re.search(r"_EP([\d.]+)x([\d.]+)mm$", flash.fp).groups())
        # Macronix 8-WSON 6x5 drawing 6110-3401: D1 3.30/3.40/3.50, E1 3.90/4.00/4.10
        self.assertEqual((d1, e1), (3.4, 4.0))

    def test_catalog_lists_every_fitted_part(self):
        doc = gen_parts.catalog(self.parts)
        fitted = {p.lcsc for p in self.parts if p.lcsc and not p.dnp}
        self.assertEqual({e["lcsc"] for e in doc["parts"]}, fitted)
        self.assertEqual(doc["provenance"]["retrieved"], gen_parts.LCSC_READ)


class LandPatternTest(unittest.TestCase):
    def test_qth030_follows_the_samtec_layout(self):
        text = footprints.qth030_01_a().render()
        ps = pads(text)
        sig = {int(p["name"]): p for p in ps if p["name"].isdigit()}
        self.assertEqual(sorted(sig), list(range(1, 61)))
        for n, p in sig.items():
            self.assertEqual((p["w"], p["h"]), (0.305, 1.45), n)  # .0120 x .057
            self.assertAlmostEqual(p["x"], -7.25 + 0.5 * ((n - 1) // 2), places=4)
            self.assertAlmostEqual(p["y"], -3.086 if n % 2 else 3.086, places=4)  # .1215
        gnd = {p["name"]: p for p in ps if p["name"].startswith("MP")}
        self.assertEqual(
            {k: (v["x"], v["y"], v["w"], v["h"]) for k, v in gnd.items()},
            {
                "MP1": (-8.445, 0, 2.54, 0.64),
                "MP2": (-3.175, 0, 4.7, 0.64),
                "MP3": (3.175, 0, 4.7, 0.64),
                "MP4": (8.445, 0, 2.54, 0.64),
            },
        )
        # .2075 [5.271] from a long ground land to the short one; .047 [1.19] to the first pin
        self.assertAlmostEqual(gnd["MP4"]["x"] - gnd["MP3"]["x"], 5.27, places=2)
        self.assertAlmostEqual(sig[1]["x"] - gnd["MP1"]["x"], 1.195, places=3)
        holes = [p for p in ps if p["kind"] == "np_thru_hole"]
        self.assertEqual(len(holes), 2)
        for h in holes:
            self.assertEqual((h["drill"], h["y"]), (1.02, -2.03))  # Table 3 -A; .080 to pin 1 row
        xs = sorted(h["x"] for h in holes)
        self.assertAlmostEqual(xs[1] - xs[0], 18.48, places=2)  # Table 1 "B", -030, -A
        self.assertAlmostEqual(sig[1]["x"] - xs[0], 1.99, places=2)  # .0783 [1.989]
        x0, y0, x1, y1 = courtyard(text)
        for p in ps:
            self.assertGreaterEqual(p["x"] - p["w"] / 2, x0)
            self.assertLessEqual(p["x"] + p["w"] / 2, x1)
            self.assertGreaterEqual(p["y"] - p["h"] / 2, y0)
            self.assertLessEqual(p["y"] + p["h"] / 2, y1)
        self.assertLessEqual(x0, -10.0)  # envelope "A" 20.00

    def test_wfcp0612_follows_the_vishay_pad_layout(self):
        text = footprints.wfcp0612().render()
        ps = pads(text)
        self.assertEqual([p["name"] for p in ps], ["1", "2"])
        for p in ps:
            self.assertEqual((p["w"], p["h"]), (1.3, 3.8))  # b x c
        gap = (ps[1]["x"] - ps[1]["w"] / 2) - (ps[0]["x"] + ps[0]["w"] / 2)
        self.assertAlmostEqual(gap, 0.60, places=4)  # a


class LockTest(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((PROJECT / "yapnr-parts.lock.json").read_text(encoding="utf-8"))
        self.parts = {p.name: p for p in gen_parts.parts()}

    def test_lock_names_every_generated_part(self):
        self.assertEqual(self.lock["schema"], "yapnr-atopile-parts-lock-v1")
        self.assertEqual(self.lock["parts_dir"], "elec/src/parts")
        self.assertEqual({e["name"] for e in self.lock["parts"]}, set(self.parts))
        for e in self.lock["parts"]:
            p = self.parts[e["name"]]
            fitted = not (p.dnp or p.board_only)
            self.assertEqual(e.get("lcsc"), p.lcsc if fitted else None, p.name)

    def test_generated_parts_match_their_locked_ids(self):
        """Parts whose files are all generated here (no stock KiCad footprint) must have the ids
        the lock pins: change a generator, regenerate and re-lock (README "Parts lock")."""
        locked = {e["name"]: e["id"] for e in self.lock["parts"]}
        with tempfile.TemporaryDirectory() as tmp:
            checked = 0
            for p in self.parts.values():
                if not p.fp.startswith("gen:"):
                    continue
                gen_parts.write_part(Path(tmp), p, Path(tmp) / "no-stock-library")
                self.assertEqual(dir_part_id(Path(tmp) / p.name), locked[p.name], p.name)
                checked += 1
        self.assertGreaterEqual(checked, 8)


if __name__ == "__main__":
    unittest.main()
