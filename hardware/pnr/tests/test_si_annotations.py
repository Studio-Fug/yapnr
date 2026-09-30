"""@pnr-si / @pnr-si-waiver parsing, resolution against graph components, and waivers."""

import atexit
import copy
import json
import tempfile
import unittest
from pathlib import Path

from pnr.graph import Component, Pad
from pnr.si import annotations as A
from pnr.si import models

TESTDATA = Path(__file__).resolve().parents[1] / "testdata/si"
LED0 = (
    '{"name":"led0_data","profile":"ws2812b_din","driver":"led0.shifter:4","series":"led0.term",'
    '"connector":"led0.conn:2","return":"led0.conn:3"}'
)


def fixture_components():
    out = []
    for c in json.loads((TESTDATA / "p027-led-components.json").read_text()):
        c = dict(c, pads=[Pad(**p) for p in c["pads"]])
        out.append(Component(**c))
    return out


_SCRATCH = tempfile.TemporaryDirectory(prefix="pnr-si-test-")
atexit.register(_SCRATCH.cleanup)  # every .ato written by these tests goes away at exit


def write(lines):
    f = tempfile.NamedTemporaryFile("w", suffix=".ato", delete=False, dir=_SCRATCH.name)
    f.write("module X:\n" + "".join("    %s\n" % l for l in lines))
    f.close()
    return f.name


class ParseTest(unittest.TestCase):
    def test_fixture_bindings(self):
        reqs, waivers = A.parse([TESTDATA / "p027-si.ato"])
        self.assertEqual([r["name"] for r in reqs], ["led0_data", "led1_data"])
        self.assertEqual(reqs[0]["series"], ["led0.term"])
        self.assertEqual(reqs[0]["source"]["line"], 2)
        self.assertEqual(len(reqs[0]["source"]["sha256"]), 64)
        self.assertEqual(waivers, [])

    def test_waiver_and_other_annotations_ignored(self):
        path = write(
            [
                '# @pnr-current {"target":"x","pads":["1"],"rms_current_a":1,"peak_current_a":1}',
                "# @pnr-si " + LED0,
                '# @pnr-si-waiver {"name":"led0_data","reason":"accepted by review 2026-09-29","cable_m":[3]}',
            ]
        )
        reqs, waivers = A.parse([path])
        self.assertEqual(len(reqs), 1)
        self.assertEqual(waivers[0]["cable_m"], [3])

    def test_errors(self):
        bad = [
            '# @pnr-si {"name":"a b","profile":"p","driver":"x:1","series":"r","connector":"c:2"}',  # name
            '# @pnr-si {"name":"a","driver":"x:1","series":"r","connector":"c:2"}',  # profile
            '# @pnr-si {"name":"a","profile":"p","driver":"x","series":"r","connector":"c:2"}',  # pin
            '# @pnr-si {"name":"a","profile":"p","driver":"x:1","series":[],"connector":"c:2"}',  # series
            '# @pnr-si {"name":"a","profile":"p","driver":"x:1","series":"r:1","connector":"c:2"}',  # series pin
            '# @pnr-si {"name":"a","profile":"p","driver":"x:1","series":"r","connector":"c:2","bogus":1}',
            '# @pnr-si {"name":"a","profile":"p","driver":"x:1","series":"r","connector":"c:2","measure_at":"driver"}',
            "# @pnr-si {not json}",
        ]
        for line in bad:
            with self.assertRaises(A.AnnotationError, msg=line):
                A.parse([write([line])])
        with self.assertRaises(A.AnnotationError):  # duplicate names
            A.parse([write(["# @pnr-si " + LED0, "# @pnr-si " + LED0])])
        with self.assertRaises(A.AnnotationError):  # waiver without a reason
            A.parse(
                [
                    write(
                        ["# @pnr-si " + LED0, '# @pnr-si-waiver {"name":"led0_data","reason":"ok"}']
                    )
                ]
            )
        with self.assertRaises(A.AnnotationError):  # waiver for an unknown requirement
            A.parse([write(['# @pnr-si-waiver {"name":"nope","reason":"a long enough reason"}'])])


class ResolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = models.Library()
        cls.comps = fixture_components()

    def resolve(self, lines, comps=None, env=None):
        reqs, waivers = A.parse([write(lines)])
        return A.resolve(reqs, waivers, comps or self.comps, self.lib, env=env or {})

    def test_p027_chains(self):
        reqs, waivers = A.parse([TESTDATA / "p027-si.ato"])
        out = A.resolve(reqs, waivers, self.comps, self.lib, env={})
        led0, led1 = out
        self.assertEqual(led0["driver"]["ref"], "U9")
        self.assertEqual(led0["driver"]["net"], "board.led0-B")
        self.assertEqual(led0["driver"]["model"], "sn74lvc1t45")
        self.assertEqual(led0["series"][0]["ref"], "R21")
        self.assertEqual((led0["series"][0]["in_pad"], led0["series"][0]["out_pad"]), ("1", "2"))
        self.assertEqual(led0["series"][0]["ohm"], 33.0)  # YAGEO RC0402FR-0733RL on p027
        self.assertEqual(led0["connector"]["ref"], "CN1")
        self.assertEqual(led0["connector"]["model"], "jst-ph")
        self.assertEqual(led0["return"]["net"], "lv")
        self.assertEqual(led0["nets"], ["board.led0-B", "data_out"])
        self.assertEqual(led1["nets"], ["board.led1-B", "board.led1-data_out"])
        self.assertEqual(led0["shunts"], [])
        json.dumps(out)  # rules.json-serialisable

    def test_r100_part_value_and_orientation(self):
        comps = copy.deepcopy(self.comps)
        r = next(c for c in comps if c.ref == "R21")
        r.footprint = "UNI_ROYAL_0402WGF1000TCE:R0402"
        r.pads.reverse()  # pad order must not matter
        out = self.resolve(["# @pnr-si " + LED0], comps)
        self.assertEqual(out[0]["series"][0]["ohm"], 100.0)
        self.assertFalse(out[0]["series"][0]["derived"])
        r.footprint = "UNI_ROYAL_0402WGF1500TCE:R0402"  # not in parts.json: decoded, flagged
        out = self.resolve(["# @pnr-si " + LED0], comps)
        self.assertEqual(out[0]["series"][0]["ohm"], 150.0)
        self.assertTrue(out[0]["series"][0]["derived"])
        r.footprint = "Unknown_Part:R0402"
        with self.assertRaises(A.AnnotationError):
            self.resolve(["# @pnr-si " + LED0], comps)

    def test_shunt_pads_found(self):
        comps = copy.deepcopy(self.comps)
        tp = next(c for c in comps if c.ref == "TP1")
        tp.pads[10].net = "data_out"  # a test pad on the connector net
        out = self.resolve(["# @pnr-si " + LED0], comps)
        self.assertEqual([(s["ref"], s["pad"]) for s in out[0]["shunts"]], [("TP1", "11")])
        self.assertTrue(any("TP1.11" in w for w in out[0]["warnings"]))

    def test_resolution_errors(self):
        for line in [
            LED0.replace("led0.shifter:4", "led0.shifter:9"),  # no such pad
            LED0.replace("led0.shifter:4", "nope:4"),  # missing target
            LED0.replace("led0.conn:2", "led1.conn:2"),  # chain ends elsewhere
            LED0.replace('"led0.term"', '"led1.term"'),  # series not on driver net
            LED0.replace("ws2812b_din", "no_such_profile"),
            LED0.replace("led0.shifter:4", "led0.term:1"),
        ]:  # driver without a model
            with self.assertRaises(A.AnnotationError, msg=line):
                self.resolve(["# @pnr-si " + line])
        comps = copy.deepcopy(self.comps) + copy.deepcopy([c for c in self.comps if c.ref == "U9"])
        with self.assertRaises(A.AnnotationError):  # ambiguous target
            self.resolve(["# @pnr-si " + LED0], comps)

    def test_subboard_skips_foreign_requirement(self):
        comps = [c for c in self.comps if c.ref != "CN1"]
        self.assertEqual(self.resolve(["# @pnr-si " + LED0], comps, env={"PNR_SUBBOARD": "1"}), [])
        with self.assertRaises(A.AnnotationError):
            self.resolve(["# @pnr-si " + LED0], comps, env={})

    def test_waiver_scope(self):
        out = self.resolve(
            [
                "# @pnr-si " + LED0,
                '# @pnr-si-waiver {"name":"led0_data","reason":"3 m lead not shipped","cable_m":[3],"metrics":["rise_10_90_ns"]}',
            ]
        )
        it = out[0]
        self.assertIsNotNone(
            A.waived(it, dict(corner="typ", cable_m=3.0, metrics=["rise_10_90_ns"]))
        )
        self.assertIsNone(A.waived(it, dict(corner="typ", cable_m=1.0, metrics=["rise_10_90_ns"])))
        self.assertIsNone(
            A.waived(
                it, dict(corner="typ", cable_m=3.0, metrics=["rise_10_90_ns", "nonmonotonic_edges"])
            )
        )


if __name__ == "__main__":
    unittest.main()
