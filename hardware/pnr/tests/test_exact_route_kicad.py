"""Exact-separation routes judged by KiCad's own DRC.

The dense random boards of ``test_exact_route`` are routed with the exact pairwise
separation (:func:`pnr.route.detail.exact_route.route_exact`), their tracks and vias
are written into one KiCad board (each board a tile with its own nets) by KiCad's
Python, the project gets the same rules (0.25 mm tracks, 0.2 mm clearance, 0.6/0.3 mm
vias, 0.25 mm between holes), and kicad-cli's DRC must report no clearance, hole or
short finding. Needs ``PNR_KICAD_PYTHON`` (a Python with ``pcbnew``) and
``PNR_KICAD_CLI``; skipped otherwise.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_exact_route import random_board

from pnr.route.detail.exact_route import route_exact
from pnr.writeback import patch_project_rules

KICAD_PYTHON = os.environ.get("PNR_KICAD_PYTHON", "")
KICAD_CLI = os.environ.get("PNR_KICAD_CLI", "")

# Copper and hole spacing findings: none may appear.
SPACING = {
    "clearance",
    "hole_clearance",
    "hole_to_hole",
    "shorting_items",
    "tracks_crossing",
    "copper_edge_clearance",
}

BUILD = r"""
import json, sys
import pcbnew as k

spec = json.load(open(sys.argv[1]))
b = k.BOARD()
b.SetCopperLayerCount(4)
nm = lambda v: int(round(v * 1e6))
V = lambda x, y: k.VECTOR2I(nm(x), nm(y))
nets = {}

def net(name):
    if name not in nets:
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    return nets[name]

w, h = spec["outline"]
for a, z in [((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))]:
    s = k.PCB_SHAPE(b)
    s.SetShape(k.SHAPE_T_SEGMENT)
    s.SetStart(V(*a))
    s.SetEnd(V(*z))
    s.SetLayer(k.Edge_Cuts)
    s.SetWidth(nm(0.1))
    b.Add(s)
for name, layer, a, z, width in spec["tracks"]:
    t = k.PCB_TRACK(b)
    t.SetStart(V(*a))
    t.SetEnd(V(*z))
    t.SetWidth(nm(width))
    t.SetLayer(b.GetLayerID(layer))
    t.SetNet(net(name))
    b.Add(t)
for name, p, diameter, drill in spec["vias"]:
    v = k.PCB_VIA(b)
    v.SetPosition(V(*p))
    v.SetViaType(k.VIATYPE_THROUGH)
    v.SetLayerPair(k.F_Cu, k.B_Cu)
    v.SetFrontWidth(nm(diameter))
    v.SetDrill(nm(drill))
    v.SetNet(net(name))
    b.Add(v)
k.SaveBoard(sys.argv[2], b)
"""


@unittest.skipUnless(
    KICAD_PYTHON and Path(KICAD_PYTHON).exists() and KICAD_CLI and Path(KICAD_CLI).exists(),
    "PNR_KICAD_PYTHON and PNR_KICAD_CLI (headless KiCad) required",
)
class ExactRouteKiCadDrcTest(unittest.TestCase):
    def test_dense_boards_have_no_spacing_findings(self):
        tracks, vias = [], []
        x0, routed = 2.0, 0
        with patch.dict(os.environ, {"PNR_SINGLE_TRACK_WORKERS": "1"}):
            for seed in range(16):
                grid, access, widths = random_board(seed)
                result = route_exact(grid, access, max_iters=4, via_cost=12.0)
                for net, rn in result.nets.items():
                    name = "S%d_%s" % (seed, net)
                    width = widths.get(net, grid.track_width)
                    for layer, a, b in rn.segments:
                        (ax, ay), (bx, by) = grid.center_of(*a), grid.center_of(*b)
                        tracks.append(
                            (name, grid.layers[layer], (x0 + ax, 2 + ay), (x0 + bx, 2 + by), width)
                        )
                        routed += 1
                    for i, j in rn.vias:
                        x, y = grid.center_of(i, j)
                        vias.append((name, (x0 + x, 2 + y), 2 * grid.via_radius, 0.3))
                x0 += grid.width + 3.0
        self.assertGreater(routed, 500)
        self.assertTrue(vias)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # KiCad keeps its settings under HOME, which a test runner may not set.
            env = dict(os.environ, HOME=os.environ.get("HOME") or directory)
            spec = root / "copper.json"
            spec.write_text(
                json.dumps(dict(outline=[x0, 15.0], tracks=tracks, vias=vias)), encoding="utf-8"
            )
            board = root / "exact.kicad_pcb"
            built = subprocess.run(
                [KICAD_PYTHON, "-c", BUILD, str(spec), str(board)],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            self.assertEqual(built.returncode, 0, built.stderr[-3000:])
            fab = dict(
                track_width_mm=0.25,
                clearance_mm=0.2,
                via_diameter_mm=0.6,
                via_drill_mm=0.3,
                hole_clearance_mm=0.2,
                hole_to_hole_mm=0.25,
                min_through_drill_mm=0.3,
                edge_clearance_mm=0.2,
            )
            self.assertTrue(
                patch_project_rules(str(board.with_suffix(".kicad_pro")), dict(fab=fab))
            )
            report = root / "drc.json"
            judged = subprocess.run(
                [KICAD_CLI, "pcb", "drc", str(board), "--format", "json", "--output", str(report)],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            self.assertEqual(judged.returncode, 0, (judged.stdout + judged.stderr)[-3000:])
            findings = json.loads(report.read_text())["violations"]
        spacing = [v for v in findings if v["type"] in SPACING]
        self.assertEqual(spacing, [], json.dumps(spacing[:5], indent=1))


if __name__ == "__main__":
    unittest.main()
