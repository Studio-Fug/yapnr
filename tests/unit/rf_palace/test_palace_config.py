"""Palace configurations from planar documents, checked against Palace's own (vendored) schema,
and the schema validator itself. Stdlib only: the mesh record is written by hand here."""

from __future__ import annotations

import copy
import hashlib
import math
import unittest

from yapnr.rf.palace import config, schema
from yapnr.rf.planar import adapters, model

# Palace's scripts/schema/config-schema.json at b797ea8 (THIRD_PARTY.md)
SCHEMA_SHA256 = "8f8dbd5588fee4cef97c0a83459ba2b721eb371af4356abd970ac301df4e4b7c"


def _record(doc):
    """The attribute map the mesher would write for ``doc`` (volumes, then surfaces)."""
    names = ["air"] + [f"diel:{d['name']}" for d in doc["stack"]["dielectrics"]]
    names += [f"port:{p['name']}" for p in doc["ports"]]
    names += sorted({f"cond:{c['layer']}:{c['net']}" for c in doc["conductors"]})
    names += sorted({f"via:{v['net']}" for v in doc["vias"]})
    names += [f"wall:{f}" for f in model.FACES]
    groups = {
        n: dict(
            dim=3 if i < 1 + len(doc["stack"]["dielectrics"]) else 2,
            tag=i + 1,
            entities=1,
            elements=10,
        )
        for i, n in enumerate(names)
    }
    return dict(groups=groups, file="mesh.msh")


class SchemaFileTest(unittest.TestCase):
    def test_vendored_unmodified(self):
        path = schema.find_schema()
        self.assertIsNotNone(
            path, "third_party/palace/config-schema.json missing from the runfiles"
        )
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), SCHEMA_SHA256)
        self.assertEqual(schema.version(schema.load()), "2-1-0")


class ValidatorTest(unittest.TestCase):
    S = {
        "$defs": {"pos": {"type": "number", "exclusiveMinimum": 0}},
        "type": "object",
        "additionalProperties": False,
        "required": ["A"],
        "properties": {
            "A": {"$ref": "#/$defs/pos"},
            "B": {"oneOf": [{"type": "boolean"}, {"type": "integer", "minimum": 0}]},
            "C": {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}, "minItems": 1},
            "D": {"anyOf": [{"const": "k"}, {"type": "array", "minItems": 3, "maxItems": 3}]},
            "E": {"type": "number", "x-palace-deprecated": True},
        },
        "allOf": [{"if": {"properties": {"A": {"const": 1}}}, "then": {"required": ["B"]}}],
    }

    def errs(self, v, deprecated=True):
        return schema.validate(v, self.S, deprecated)

    def test_keywords(self):
        self.assertEqual(self.errs({"A": 2.0, "B": True, "C": ["x"], "D": [1, 2, 3]}), [])
        self.assertTrue(self.errs({"A": 0}))  # exclusiveMinimum through $ref
        self.assertTrue(self.errs({"A": 2, "Z": 1}))  # additionalProperties
        self.assertTrue(self.errs({}))  # required
        self.assertTrue(self.errs({"A": 2, "B": -1}))  # oneOf: neither
        self.assertTrue(self.errs({"A": 2, "C": ["z"]}))  # enum in items
        self.assertTrue(self.errs({"A": 2, "C": []}))  # minItems
        self.assertTrue(self.errs({"A": 2, "D": [1]}))  # anyOf
        self.assertTrue(self.errs({"A": 1}))  # if/then
        self.assertEqual(self.errs({"A": 1, "B": 3}), [])
        self.assertTrue(self.errs({"A": 2, "B": 1.5}))  # integer, not number
        self.assertTrue(self.errs({"A": True}))  # a boolean is not a number
        self.assertTrue(self.errs({"A": 2, "E": 1.0}))  # deprecated key
        self.assertEqual(self.errs({"A": 2, "E": 1.0}, deprecated=False), [])


class DrivenTest(unittest.TestCase):
    def setUp(self):
        self.doc = adapters.line_model("gcpw", length=5.0)
        self.rec = _record(self.doc)

    def test_mapping(self):
        cfg = config.driven(self.doc, self.rec, 54.0, 70.0, 0.5, adaptive_tol=1e-3)
        self.assertEqual(config.validate(cfg), [])
        tag = {k: v["tag"] for k, v in self.rec["groups"].items()}
        mats = {m["Attributes"][0]: m for m in cfg["Domains"]["Materials"]}
        self.assertEqual(mats[tag["air"]]["Permittivity"], 1.0)
        self.assertEqual(mats[tag["diel:RO4835"]]["Permittivity"], 3.56)
        self.assertEqual(mats[tag["diel:RO4835"]]["LossTan"], 0.0037)
        b = cfg["Boundaries"]
        # PEC: the floor and the via barrels; absorbing: the other walls
        self.assertEqual(sorted(b["PEC"]["Attributes"]), sorted([tag["wall:zmin"], tag["via:GND"]]))
        self.assertEqual(b["Absorbing"]["Order"], 1)
        self.assertNotIn(tag["wall:zmin"], b["Absorbing"]["Attributes"])
        # copper: sigma / K^2, 35 um sheet, internal
        lay = self.doc["stack"]["layers"][0]
        for c in b["Conductivity"]:
            self.assertAlmostEqual(c["Conductivity"], 5.8e7 / lay["rough_k"] ** 2)
            self.assertEqual(c["Thickness"], 0.035)
            self.assertFalse(c["External"])
        self.assertEqual(len(b["Conductivity"]), 2)  # SIG and GND
        # wave ports: offsets back to the reference planes, P1 excited, walls behind them PEC
        # in the port mode solve
        wp = {p["Index"]: p for p in b["WavePort"]}
        self.assertEqual(wp[1]["Offset"], 0.5)
        self.assertEqual(wp[1]["Excitation"], 1)
        self.assertEqual(wp[2]["Excitation"], 0)
        self.assertEqual(wp[1]["VoltagePath"][0][2], lay["z"])
        self.assertEqual(
            sorted(b["WavePortPEC"]["Attributes"]), sorted([tag["wall:xmin"], tag["wall:xmax"]])
        )
        self.assertEqual(cfg["Model"]["L0"], 1e-3)
        self.assertEqual(cfg["Solver"]["Driven"]["AdaptiveTol"], 1e-3)
        self.assertEqual(cfg["Solver"]["Order"], 2)

    def test_amr_and_excitation(self):
        cfg = config.driven(
            self.doc, self.rec, 54, 70, 0.5, excite=["P2"], amr=dict(MaxIts=4), amr_freqs=[62.0]
        )
        self.assertEqual(config.validate(cfg), [])
        self.assertEqual(cfg["Model"]["Refinement"]["MaxIts"], 4)
        self.assertTrue(cfg["Model"]["Refinement"]["SaveAdaptMesh"])
        self.assertEqual(
            cfg["Solver"]["Driven"]["Samples"], [dict(Type="Point", Freq=[62.0], SaveStep=0)]
        )
        wp = {p["Index"]: p["Excitation"] for p in cfg["Boundaries"]["WavePort"]}
        self.assertEqual(wp, {1: 0, 2: 2})
        with self.assertRaises(ValueError):
            config.driven(self.doc, self.rec, 54, 70, 0.5, excite=[])
        with self.assertRaises(ValueError):
            config.driven(self.doc, self.rec, 70, 54, 0.5)

    def test_pec_and_solid_copper(self):
        cfg = config.driven(model.with_layer_model(self.doc, "pec"), self.rec, 54, 70, 1)
        self.assertNotIn("Conductivity", cfg["Boundaries"])
        tag = {k: v["tag"] for k, v in self.rec["groups"].items()}
        self.assertIn(tag["cond:L1:SIG"], cfg["Boundaries"]["PEC"]["Attributes"])
        solid = model.with_layer_model(self.doc, "solid")
        cfg = config.driven(solid, self.rec, 54, 70, 1)
        self.assertEqual(config.validate(cfg), [])
        self.assertTrue(
            all(c["External"] and "Thickness" not in c for c in cfg["Boundaries"]["Conductivity"])
        )

    def test_lumped_port(self):
        doc = copy.deepcopy(self.doc)
        doc["ports"][0]["kind"] = "lumped"
        cfg = config.driven(doc, _record(doc), 54, 70, 1)
        self.assertEqual(config.validate(cfg), [])
        lp = cfg["Boundaries"]["LumpedPort"][0]
        self.assertEqual((lp["R"], lp["Direction"], lp["Excitation"]), (50.0, "-Z", 1))
        self.assertEqual(len(cfg["Boundaries"]["WavePort"]), 1)

    def test_schema_catches_mistakes(self):
        cfg = config.driven(self.doc, self.rec, 54, 70, 1)
        bad = copy.deepcopy(cfg)
        bad["Boundaries"]["WavePort"][0]["Ofset"] = 1.0
        self.assertTrue(config.validate(bad))
        bad = copy.deepcopy(cfg)
        bad["Solver"]["Order"] = "2"
        self.assertTrue(config.validate(bad))
        bad = copy.deepcopy(cfg)
        del bad["Solver"]["Driven"]
        self.assertTrue(any("Driven" in e for e in config.validate(bad)))
        with self.assertRaises(ValueError):
            config.check(bad)

    def test_boundary_mode(self):
        cfg = config.boundary_mode(self.doc, self.rec, "P1", 62.0)
        self.assertEqual(config.validate(cfg), [])
        tag = {k: v["tag"] for k, v in self.rec["groups"].items()}
        self.assertEqual(cfg["Solver"]["BoundaryMode"]["Attributes"], [tag["port:P1"]])
        self.assertIn(tag["wall:xmin"], cfg["Boundaries"]["PEC"]["Attributes"])
        self.assertEqual(
            cfg["Boundaries"]["Postprocessing"]["Impedance"][0]["VoltagePath"][1][2], 0.0
        )
        with self.assertRaises(KeyError):
            config.boundary_mode(self.doc, self.rec, "P9", 62.0)
        # a guarded wall is PEC in the port-face solve, never also absorbing
        absorbing = cfg["Boundaries"].get("Absorbing", {}).get("Attributes", [])
        self.assertFalse(set(absorbing) & set(cfg["Boundaries"]["PEC"]["Attributes"]))

    def test_impedance_copper(self):
        cfg = config.driven(
            self.doc, self.rec, 54.0, 70.0, 0.5, copper_bc="impedance", excite=["P1"]
        )
        self.assertEqual(config.validate(cfg), [])
        self.assertNotIn("Conductivity", cfg["Boundaries"])
        imp = cfg["Boundaries"]["Impedance"]
        tag = {k: v["tag"] for k, v in self.rec["groups"].items()}
        self.assertEqual(
            sorted(i["Attributes"][0] for i in imp),
            sorted([tag["cond:L1:SIG"], tag["cond:L1:GND"]]),
        )
        # frozen at the band centre (62 GHz): thick copper, Rs = omega Ls = 1/(sigma delta)
        sigma = model.sigma_eff(self.doc["stack"]["layers"][0])
        omega = 2 * math.pi * 62e9
        rs = math.sqrt(omega * 4e-7 * math.pi / (2 * sigma))
        self.assertAlmostEqual(imp[0]["Rs"] / rs, 1.0, places=9)
        self.assertAlmostEqual(imp[0]["Ls"] * omega / rs, 1.0, places=9)
        mode = config.boundary_mode(self.doc, self.rec, "P1", 1.0, copper_bc="impedance")
        self.assertEqual(config.validate(mode), [])
        self.assertIn("Impedance", mode["Boundaries"])
        with self.assertRaises(ValueError):
            config.driven(self.doc, self.rec, 54.0, 70.0, 0.5, copper_bc="lossy")

    def test_impedance_rl(self):
        # a sheet much thinner than the skin depth: Palace's per-face 2/(sigma t), two faces,
        # so the boundary as a whole is the DC sheet resistance 1/(sigma t), not inductive
        rs, ls = config.impedance_rl(5.8e7, 1e-5, 1.0, interior=True)  # 10 nm, delta 2 um
        self.assertAlmostEqual(rs * 5.8e7 * 1e-8, 1.0, places=3)
        # thick: interior sheet Rs = omega Ls = Re Z, solid copper's outside 2 Re Z (one face)
        z = config.face_impedance(5.8e7, 0.035, 62.0)
        omega = 2 * math.pi * 62e9
        rs, ls = config.impedance_rl(5.8e7, 0.035, 62.0, interior=True)
        self.assertAlmostEqual(rs / z.real, 1.0, places=9)
        self.assertAlmostEqual(omega * ls / z.real, 1.0, places=9)
        rs, ls = config.impedance_rl(5.8e7, 0.035, 62.0, interior=False)
        self.assertAlmostEqual(rs / z.real, 2.0, places=9)
        self.assertAlmostEqual(omega * ls / z.real, 2.0, places=9)
        # a saved adapted mesh: the sheet is two exterior faces already, each 1 / Z
        rs, ls = config.impedance_rl(5.8e7, 0.035, 62.0, interior=True, precracked=True)
        self.assertAlmostEqual(rs / z.real, 2.0, places=9)

    def test_port_face_ends_in_ground(self):
        g = model.port_geometry(self.doc)["P1"]
        self.assertEqual(g["half_width_actual"], [0.8, 0.8])  # the edges lie inside the ground
        doc = copy.deepcopy(self.doc)
        gnd = next(c for c in doc["conductors"] if c["net"] == "GND")
        for pg in gnd["polygons"]:  # the upper ground strip now stops at y 0.6, short of 0.8
            if pg["outer"][0][1] > 0:
                pg["outer"] = [[0.0, 0.3], [6.0, 0.3], [6.0, 0.6], [0.0, 0.6]]
        g = model.port_geometry(doc)["P1"]
        self.assertAlmostEqual(g["half_width_actual"][1], 0.45)  # its middle, not a floating strip
        self.assertAlmostEqual(g["half_width_actual"][0], 0.8)
        msl = adapters.line_model("msl", length=5.0)
        self.assertEqual(model.port_geometry(msl)["P1"]["half_width_actual"], [0.8, 0.8])

    def test_boundary_mode_wall(self):
        cfg = config.boundary_mode(self.doc, self.rec, "P1", 1.0, face="wall")
        self.assertEqual(config.validate(cfg), [])
        tag = {k: v["tag"] for k, v in self.rec["groups"].items()}
        self.assertEqual(
            cfg["Solver"]["BoundaryMode"]["Attributes"],
            sorted([tag["port:P1"], tag["wall:xmin"]]),
        )
        absorbing = cfg["Boundaries"]["Absorbing"]["Attributes"]
        self.assertNotIn(tag["wall:xmin"], absorbing)
        self.assertNotIn(tag["wall:xmin"], cfg["Boundaries"]["PEC"]["Attributes"])
        self.assertIn(tag["wall:ymin"], absorbing)
        self.assertIn(tag["wall:xmax"], absorbing)  # parallel to the plane: no edge there
        with self.assertRaises(ValueError):
            config.boundary_mode(self.doc, self.rec, "P1", 1.0, face="box")


if __name__ == "__main__":
    unittest.main()
