"""Contracts of the hard rungs (hard_rungs.py): circuits, one-dimension variants, judge rules."""

import re
import unittest
from collections import Counter

from designs import designs, showcases
from hard_rungs import (
    HARD_LIB,
    REPEATED_PADS,
    STACKUPS,
    chaser_base,
    connected_pads,
    derived_checks,
    dru_text,
    hard_rungs,
    plane_layers,
)
from run import parser

CHECK_KINDS = {
    "inside_board",
    "side",
    "fixed",
    "edge",
    "orientation",
    "keepout",
    "region",
    "proximity",
    "line",
    "align",
    "plane",
    "microvia_span",
}


def nets_of(spec):
    """Each part's pin -> net map, mechanical parts (no net at all) left out."""
    return {p["ref"]: (p["footprint"], p["pins"]) for p in spec["parts"] if any(p["pins"].values())}


class HardRungContract(unittest.TestCase):
    def setUp(self):
        self.rungs = hard_rungs()
        self.by_name = {r["name"]: r for r in self.rungs}

    def test_names_are_unique_and_outside_the_gate(self):
        names = [r["name"] for r in self.rungs]
        self.assertEqual(len(names), len(set(names)))
        others = {c["name"] for c in designs() + showcases()}
        self.assertFalse(set(names) & others)
        self.assertEqual(len(designs()), 8)

    def test_circuits_are_consistent(self):
        for spec in self.rungs:
            with self.subTest(spec=spec["name"]):
                refs = [p["ref"] for p in spec["parts"]]
                self.assertEqual(len(refs), len(set(refs)))
                counts = Counter(n for p in spec["parts"] for n in p["pins"].values() if n)
                self.assertTrue(all(v >= 2 for v in counts.values()), counts)
                self.assertEqual(spec["expected_components"], len(spec["parts"]))
                self.assertEqual(spec["expected_connected_pads"], connected_pads(spec["parts"]))
                self.assertEqual(
                    spec["constraints"]["board"]["layers"], spec["stackup"]["copper_layers"]
                )
                self.assertIn(spec["ci"]["lane"], ("nightly", "manual"))
                self.assertGreater(spec["ci"]["minutes"], 0)
                self.assertEqual(spec["tier"], "hard")

    def test_checks_are_well_formed(self):
        for spec in self.rungs:
            with self.subTest(spec=spec["name"]):
                ids = [c["id"] for c in spec["checks"]]
                self.assertEqual(len(ids), len(set(ids)))
                refs = {p["ref"] for p in spec["parts"]}
                for c in spec["checks"]:
                    self.assertIn(c["kind"], CHECK_KINDS)
                    self.assertTrue(c.get("engine"))
                    named = [c[k] for k in ("ref", "anchor") if k in c]
                    if isinstance(c.get("refs"), list):
                        named += c["refs"]
                    self.assertTrue(set(named) <= refs, c)
                for ref in spec["constraints"].get("fixed") or {}:
                    self.assertIn("fixed-" + ref, ids)

    def test_variants_change_one_dimension_of_their_base(self):
        # The chaser variants' base is the ladder's own 07-chaser-20 (not a hard rung).
        chaser = chaser_base()
        self.assertEqual(chaser["parts"], designs()[6]["parts"])
        bases = {"chaser-20": chaser}
        for spec in self.rungs:
            dims = spec["dims"]
            if all(
                dims[k] == v
                for k, v in dict(
                    stackup="2L",
                    via_policy="through",
                    sides="single",
                    constraints="base",
                    search="single",
                    parts="base",
                ).items()
            ):
                bases[spec["base"]] = spec
        self.assertEqual(set(bases), {"mcu-usb-31", "quad-bank-56", "power-switch-31", "chaser-20"})
        family = {}
        for spec in self.rungs + [chaser]:
            family.setdefault(spec["base"], []).append(spec)
        for spec in self.rungs:
            base = bases[spec["base"]]
            if spec is base:
                continue
            with self.subTest(spec=spec["name"]):
                # a parent (the base or a sibling) differs from it in exactly one dimension
                parents = [
                    other
                    for other in family[spec["base"]]
                    if sum(other["dims"][k] != spec["dims"][k] for k in spec["dims"]) == 1
                ]
                self.assertTrue(parents, spec["dims"])
                if spec["dims"]["parts"] == "base":
                    self.assertEqual(nets_of(spec), nets_of(base))

    def test_stackups_map_to_one_engine_plane_layer_per_net(self):
        for spec in self.rungs:
            with self.subTest(spec=spec["name"]):
                code = STACKUPS[spec["stackup"]["name"]]
                self.assertEqual(len(code), spec["stackup"]["copper_layers"])
                classes = spec["constraints"]["net_class"]
                first = {}
                for layer, net in plane_layers(spec):
                    first.setdefault(net, layer)
                for net, layer in first.items():
                    owners = [c for c in classes.values() if net in c.get("nets", [])]
                    self.assertEqual(len(owners), 1)
                    self.assertEqual(owners[0].get("plane_layer"), layer)
                planes = [c for c in classes.values() if c.get("plane_layer")]
                self.assertEqual(len(planes), len(first))
                self.assertEqual(
                    sum(c["kind"] == "plane" for c in spec["checks"]),
                    code.count("G") + code.count("P"),
                )

    def test_the_judge_enforces_via_policy_planes_and_pairs(self):
        for spec in self.rungs:
            with self.subTest(spec=spec["name"]):
                text = dru_text(spec)
                allowed = set(spec["via_policy"]["allowed"])
                for kind in ("blind", "buried", "micro"):
                    banned = re.search(r"disallow[^)]*\b%s_via\b" % kind, text) is not None
                    self.assertEqual(banned, kind not in allowed)
                for layer, net in plane_layers(spec):
                    self.assertIn('(layer "%s")' % layer, text)
                self.assertEqual(
                    text.count("constraint skew"), len(spec["constraints"].get("diff_pair") or [])
                )
                self.assertEqual(text.count("(rule"), text.count("(constraint"))
        self.assertIsNone(dru_text(designs()[0]))

    def test_microvias_are_held_to_one_dielectric_where_permitted(self):
        # KiCad's DRC accepts a microvia of any span, so the checker holds the span.
        for spec in self.rungs:
            with self.subTest(spec=spec["name"]):
                spans = [c for c in spec["checks"] if c["kind"] == "microvia_span"]
                if "micro" in spec["via_policy"]["allowed"]:
                    self.assertEqual([c["max_dielectrics"] for c in spans], [1])
                else:
                    self.assertEqual(spans, [])
        self.assertTrue(any("micro" in s["via_policy"]["allowed"] for s in self.rungs))

    def test_footprints_and_pads_match_the_library(self):
        """Every pad name a part maps exists in its KiCad footprint and every footprint
        pad is mapped (with REPEATED_PADS' multiplicity). Skipped without the library."""
        from run import kicad_footprints

        library = kicad_footprints()
        if not library.is_dir():
            self.skipTest("no KiCad footprint library at %s" % library)
        seen = {}
        for spec in self.rungs:
            for p in spec["parts"]:
                seen[p["footprint"]] = set(p["pins"])
        for footprint, pins in sorted(seen.items()):
            with self.subTest(footprint=footprint):
                lib, name = footprint.split(":")
                text = (library / (lib + ".pretty") / (name + ".kicad_mod")).read_text()
                pads = Counter(re.findall(r'\(pad "([^"]*)"', text))
                self.assertEqual(set(pads), pins)
                for pad, count in REPEATED_PADS.get(footprint, {}).items():
                    self.assertEqual(pads[pad], count)
        self.assertTrue(set(HARD_LIB.values()) >= {f for f in seen if f in HARD_LIB.values()})

    def test_constraint_checks_are_expressed_to_the_engine(self):
        """A region or align check is the engine's own constraint over the same refs,
        rectangle, axis and tolerance (the check measures footprint origins, so the
        align anchors are the origins), and every rung's constraint file compiles
        against its parts. A check the engine cannot express stays allowed: it keeps
        ``"engine": "unsupported"`` and is still measured (hard_rungs docstring)."""
        from pnr.constraints import compile_constraints

        kinds = set()
        for spec in self.rungs:
            with self.subTest(spec=spec["name"]):
                cons = spec["constraints"]
                for c in spec["checks"]:
                    if c["kind"] not in ("region", "align"):
                        continue
                    kinds.add(c["kind"])
                    self.assertEqual(c["engine"], c["kind"])
                    (rule,) = [r for r in cons[c["kind"]] if r["name"] == c["id"]]
                    self.assertEqual(rule["refs"], c["refs"])
                    self.assertIs(rule["hard"], True)
                    if c["kind"] == "region":
                        self.assertEqual(rule["rect"], c["rect"])
                    else:
                        self.assertEqual(rule["axis"], c["axis"])
                        self.assertEqual(rule["tol_mm"], c["tol_mm"])
                        self.assertEqual(rule["anchor"], "origin")
                refs = [p["ref"] for p in spec["parts"]]
                compiled = compile_constraints(cons, refs)
                self.assertFalse(
                    [w for w in compiled.warnings if "unknown" in w], compiled.warnings
                )
        self.assertEqual(kinds, {"region", "align"})

    def test_ladder_checks_derive_from_the_engine_constraints(self):
        line, free, edge = showcases()[:3]
        self.assertIn("line-chaser_leds", [c["id"] for c in derived_checks(line)])
        ids = [c["id"] for c in derived_checks(edge)]
        self.assertTrue({"edge-J1", "edge-SW1", "edge-D1", "rot-J1"} <= set(ids))
        self.assertNotIn("fixed-J1", [c["id"] for c in derived_checks(free)])

    def test_derived_checks_cover_hard_regions_and_aligns(self):
        """A design without a checks list: each hard rectangle region and each hard
        origin-anchored align becomes the checker's own region / align check; what the
        checker cannot measure (a polygon, another anchor, a glob) or a soft rule is left
        out."""
        spec = {
            "constraints": {
                "region": [
                    dict(name="clock", refs=["U1", "C1"], rect=[0, 0, 21, 32]),
                    dict(name="soft", refs=["U2"], rect=[0, 0, 21, 32], hard=False),
                    dict(name="poly", refs=["U2"], polygon=[[0, 0], [9, 0], [0, 9]]),
                    dict(name="glob", refs=["C*"], rect=[0, 0, 21, 32]),
                ],
                "align": [
                    dict(name="ics", refs=["U1", "U2"], axis="y", tol_mm=0.1),
                    dict(name="flush", refs=["J1", "J2"], axis="x", anchor="east"),
                    dict(name="pins", refs=["J1", "J2"], axis="x", anchor={"J2": "pad1"}),
                    dict(name="loose", refs=["D1", "D2"], axis="x", hard=False),
                ],
            }
        }
        checks = {c["id"]: c for c in derived_checks(spec)}
        region = dict(kind="region", refs=["U1", "C1"], rect=[0, 0, 21, 32], engine="region")
        self.assertEqual(checks["region-clock"], dict(id="region-clock", **region))
        align = dict(kind="align", refs=["U1", "U2"], axis="y", tol_mm=0.1, engine="align")
        self.assertEqual(checks["align-ics"], dict(id="align-ics", **align))
        for name in ("soft", "poly", "glob"):
            self.assertNotIn("region-" + name, checks)
        for name in ("flush", "pins", "loose"):
            self.assertNotIn("align-" + name, checks)

    def test_runner_offers_the_hard_rungs(self):
        args = parser().parse_args(["--out", "x", "--hard"])
        self.assertTrue(args.hard)
        self.assertEqual(parser().parse_args(["--out", "x", "--lane", "nightly"]).lane, "nightly")


if __name__ == "__main__":
    unittest.main()
