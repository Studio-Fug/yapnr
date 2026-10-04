"""The Gmsh builder on small models: physical groups and their counts, port faces on the walls,
via barrels, a lumped port inside the core, element quality, the msh 2.2 file, and configs
written from the real mesh record. Needs gmsh (the FEA environment): skipped elsewhere, and
manual under Bazel.

    python -m unittest tests/unit/rf_palace/test_palace_mesh.py   # in the FEA environment
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    import gmsh

    HAVE_GMSH = True
except ImportError:
    HAVE_GMSH = False

from yapnr.rf.palace import config, mesh
from yapnr.rf.planar import adapters, model

# short lines keep the meshes small (a few thousand tetrahedra)
FAST = dict(edge_h=0.06, port_h=0.15)


def _bbox_of_group(rec_groups, key):
    a = rec_groups[key]
    boxes = [
        gmsh.model.getBoundingBox(a["dim"], e)
        for e in gmsh.model.getEntitiesForPhysicalGroup(a["dim"], a["tag"])
    ]
    return [min(b[i] for b in boxes) for i in range(3)] + [
        max(b[i] for b in boxes) for i in range(3, 6)
    ]


@unittest.skipUnless(HAVE_GMSH, "gmsh not installed (FEA environment only)")
class LineMeshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.msl = adapters.line_model("msl", length=1.0, half_width=1.0, air=0.8)
        cls.rec = mesh.build(cls.msl, cls.tmp.name, **FAST)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_groups(self):
        g = self.rec["groups"]
        expect = {"air", "diel:RO4835", "port:P1", "port:P2", "cond:L1:SIG"} | {
            f"wall:{f}" for f in model.FACES
        }
        self.assertEqual(set(g), expect)
        self.assertEqual([g[k]["dim"] for k in ("air", "diel:RO4835")], [3, 3])
        tags = sorted(v["tag"] for v in g.values())
        self.assertEqual(tags, list(range(1, len(g) + 1)))
        for key, a in g.items():
            self.assertGreater(a["elements"], 0, key)
        self.assertEqual(
            self.rec["tetrahedra"], g["air"]["elements"] + g["diel:RO4835"]["elements"]
        )

    def test_quality_and_estimate(self):
        q = self.rec["quality"]
        self.assertGreater(q["gamma_min"], 0.1)
        self.assertEqual(q["sicn_below_0"], 0)
        d = self.rec["dofs_estimate"]
        self.assertLess(int(d["1"]), int(d["2"]))
        self.assertLess(int(d["2"]), int(d["3"]))

    def test_file(self):
        path = os.path.join(self.tmp.name, "mesh.msh")
        with open(path, "rb") as fh:
            head = fh.read(40)
        self.assertTrue(head.startswith(b"$MeshFormat\n2.2 1 8"), head)  # msh 2.2, binary
        with open(os.path.join(self.tmp.name, "mesh.json")) as fh:
            self.assertEqual(json.load(fh)["geometry_sha256"], model.geometry_hash(self.msl))

    def test_ports_on_walls(self):
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.open(os.path.join(self.tmp.name, "mesh.msh"))
            b1 = _bbox_of_group(self.rec["groups"], "port:P1")
            b2 = _bbox_of_group(self.rec["groups"], "port:P2")
        finally:
            gmsh.finalize()
        self.assertAlmostEqual(b1[0], 0.0)
        self.assertAlmostEqual(b1[3], 0.0)
        self.assertAlmostEqual(b2[0], 2.0)
        self.assertAlmostEqual(b1[1], -0.8, places=6)
        self.assertAlmostEqual(b1[4], 0.8, places=6)
        self.assertAlmostEqual(b1[2], 0.0, places=6)
        self.assertAlmostEqual(b1[5], 0.1016 + 0.7, places=6)

    def test_config_from_mesh(self):
        cfg = config.driven(self.msl, self.rec, 54, 70, 1, adaptive_tol=1e-3)
        self.assertEqual(config.validate(cfg), [])
        self.assertEqual(cfg["Model"]["Mesh"], "mesh.msh")
        cfg = config.boundary_mode(self.msl, self.rec, "P1", 62.0)
        self.assertEqual(config.validate(cfg), [])

    def test_deterministic(self):
        again = mesh.build(self.msl, **FAST)
        self.assertEqual(again["tetrahedra"], self.rec["tetrahedra"])


@unittest.skipUnless(HAVE_GMSH, "gmsh not installed (FEA environment only)")
class ViaAndPortTest(unittest.TestCase):
    def test_gcpw_vias(self):
        d = adapters.line_model("gcpw", length=0.9, half_width=1.0, air=0.8)
        rec = mesh.build(d, **FAST)
        g = rec["groups"]
        # each barrel leaves its side and its top (under the pad); the bottom is on the floor
        self.assertEqual(g["via:GND"]["entities"], 2 * len(d["vias"]))
        self.assertIn("cond:L1:GND", g)
        self.assertGreater(rec["quality"]["gamma_min"], 0.05)

    def test_lumped_port_inside(self):
        d = adapters.line_model("msl", length=1.0, half_width=1.0, air=0.8)
        d["ports"][0]["kind"] = "lumped"
        rec = mesh.build(d, **FAST)
        self.assertIn("port:P1", rec["groups"])
        self.assertGreater(rec["groups"]["port:P1"]["elements"], 0)
        cfg = config.driven(d, rec, 54, 70, 1)
        self.assertEqual(config.validate(cfg), [])
        self.assertEqual(
            cfg["Boundaries"]["LumpedPort"][0]["Attributes"], [rec["groups"]["port:P1"]["tag"]]
        )

    def test_solid_copper(self):
        d = model.with_layer_model(
            adapters.line_model("msl", length=1.0, half_width=1.0, air=0.8), "solid"
        )
        rec = mesh.build(d, **FAST)
        # the strip is removed: its five free faces (top, sides) form the conductor surface
        self.assertGreaterEqual(rec["groups"]["cond:L1:SIG"]["entities"], 3)
        self.assertEqual(config.validate(config.driven(d, rec, 54, 70, 1)), [])

    def test_rejects_unknown_option(self):
        with self.assertRaises(TypeError):
            mesh.build(adapters.line_model("msl", length=1.0), edge=0.1)


if __name__ == "__main__":
    unittest.main()
