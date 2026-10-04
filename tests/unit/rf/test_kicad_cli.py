"""KiCad parses the exported footprints (design §10.3); KiCad lane only (tag `kicad`).

Needs a headless kicad-cli in YAPNR_KICAD_CLI (DEVELOPERS.md, never the GUI bundle). The
footprint has a net-tie island with a keyholed hole, a custom pad, a netless island and two
rule areas. KiCad must load it (`fp upgrade --force` rewrites it) and keep every pad, copper
polygon and rule area, and it must plot it (`fp export svg`).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

import numpy as np

from yapnr.rf.export.contour import islands
from yapnr.rf.export.kicad import Footprint, PortPad, RuleArea, read_footprint, write_footprint

CLI = os.environ.get("YAPNR_KICAD_CLI", "")


def _footprint():
    m = np.zeros((12, 8), bool)
    m[0:12, 3:5] = True  # a through line from pad 1 to pad 2
    m[3:9, 1:7] = True
    m[5:7, 3:5] = False  # with a hole: keyholed
    m[0:2, 0:1] = True  # small island at pad 3
    m[10:12, 7:8] = True  # free island
    pitch = 0.3
    shapes = []
    pads = [
        PortPad(1, (0.3, 1.2), (0.6, 0.6)),
        PortPad(2, (3.3, 1.2), (0.6, 0.6)),
        PortPad(3, (0.15, 0.15), (0.3, 0.3)),
    ]
    touch = {0: [1, 2], 1: [3], 2: []}
    for k, isl in enumerate(sorted(islands(m), key=lambda i: -i.area)):
        shapes.append((isl.polygon * pitch, touch[k]))
    return Footprint(
        name="RF_cli",
        origin=(1.8, 1.2),
        region=(0.0, 3.6, 0.0, 2.4),
        pads=pads,
        islands=shapes,
        description="kicad-cli parse test",
        seed="cli",
        rule_areas=[
            RuleArea(
                "pour keepout", np.array([[-1.0, -1.0], [4.6, -1.0], [4.6, 3.4], [-1.0, 3.4]])
            ),
            RuleArea(
                "track keepout",
                np.array([[-1.0, -1.0], [0.0, -1.0], [0.0, 0.85], [-1.0, 0.85]]),
                ("tracks", "vias", "copperpour", "footprints"),
            ),
        ],
    )


@unittest.skipUnless(
    CLI and os.access(CLI, os.X_OK), "needs a headless kicad-cli (YAPNR_KICAD_CLI)"
)
class KiCadCliTest(unittest.TestCase):
    def test_kicad_loads_and_plots(self):
        with tempfile.TemporaryDirectory() as d:
            lib = os.path.join(d, "rf.pretty")
            os.makedirs(lib)
            path = os.path.join(lib, "RF_cli.kicad_mod")
            write_footprint(_footprint(), path)
            out = os.path.join(d, "up.pretty")
            subprocess.run(
                [CLI, "fp", "upgrade", "--force", "-o", out, lib],
                check=True,
                capture_output=True,
                timeout=120,
            )
            mine, theirs = read_footprint(path), read_footprint(
                os.path.join(out, "RF_cli.kicad_mod")
            )
            self.assertEqual(theirs.net_tie_groups, mine.net_tie_groups)
            self.assertEqual(
                [(p["number"], p["shape"]) for p in theirs.pads],
                [(p["number"], p["shape"]) for p in mine.pads],
            )
            self.assertEqual(len(theirs.polygons), len(mine.polygons))

            def key(p):  # KiCad sorts graphic items its own way
                return (p.shape[0], round(float(p[:, 0].min()), 6), round(float(p[:, 1].min()), 6))

            for a, b in zip(sorted(mine.polygons, key=key), sorted(theirs.polygons, key=key)):
                np.testing.assert_allclose(a, b, atol=1e-6)
            for a, b in zip(mine.pads, theirs.pads):
                for pa, pb in zip(a["primitives"], b["primitives"]):
                    np.testing.assert_allclose(pa, pb, atol=1e-6)
            # The rule areas survive with their names, keepout kinds and outlines.
            self.assertEqual(
                sorted((a["name"], tuple(a["not_allowed"])) for a in theirs.rule_areas),
                sorted((a["name"], tuple(a["not_allowed"])) for a in mine.rule_areas),
            )
            for a in mine.rule_areas:
                b = [r for r in theirs.rule_areas if r["name"] == a["name"]][0]
                np.testing.assert_allclose(
                    sorted(map(tuple, a["polygon"])), sorted(map(tuple, b["polygon"])), atol=1e-6
                )
            svg = os.path.join(d, "svg")
            os.makedirs(svg)
            subprocess.run(
                [CLI, "fp", "export", "svg", "--layers", "F.Cu", "-o", svg, lib],
                check=True,
                capture_output=True,
                timeout=120,
            )
            self.assertTrue(os.path.getsize(os.path.join(svg, "RF_cli.svg")) > 0)


if __name__ == "__main__":
    unittest.main()
