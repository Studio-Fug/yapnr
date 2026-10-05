"""Contracts of the hard rungs (hard_rungs.py): circuits, one-dimension variants, judge rules."""

import math
import re
import unittest
from collections import Counter

from designs import designs, showcases
from hard_rungs import (
    HARD_LIB,
    REPEATED_PADS,
    STACKUPS,
    buck_pour,
    chaser_base,
    connected_pads,
    derived_checks,
    dru_text,
    hard_rungs,
    plane_layers,
    ufbga_base,
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
    "copper_digest",
    "no_copper",
    "via_class",
    "escape",
    "pad_distance",
    "net_vias",
    "unconnected",
    "ir_drop",
    "rail_zones",
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
        # The BGA rung's base (two layers) is not a hard rung either: its drops need planes.
        bga = ufbga_base()
        # Nor is the buck stage's (its hot-rod stage is drawn on four layers).
        buck = buck_pour()
        bases = {"chaser-20": chaser, "ufbga201-fanout": bga, "buck-vqfnhr": buck}
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
        self.assertEqual(
            set(bases),
            {
                "mcu-usb-31",
                "quad-bank-56",
                "power-switch-31",
                "chaser-20",
                "ufbga201-fanout",
                "buck-vqfnhr",
            },
        )
        family = {}
        for spec in self.rungs + [chaser, bga, buck]:
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
                # A partitioned plane layer (plane_partition) carries several rails,
                # each in a class of its own naming the layer, and a rail_zones check
                # in place of the one-net plane check.
                # (An outer pour, a partition with a region, is on a signal layer.)
                parts = [
                    p
                    for p in spec["constraints"].get("plane_partition") or []
                    if not p.get("region")
                ]
                shared = sum(len(p["nets"]) - 1 for p in parts)
                self.assertEqual(len(planes), len(first) + shared)
                self.assertEqual(
                    sum(c["kind"] == "plane" for c in spec["checks"]),
                    code.count("G") + code.count("P") - len(parts),
                )
                self.assertEqual(sum(c["kind"] == "rail_zones" for c in spec["checks"]), len(parts))

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

    def test_arc_block_rung(self):
        """The fixed-copper rung: 64 arcs in one group with the U.FL connectors, the
        digest the checker expects, keep-outs expressed to the engine and judged by
        the tool-neutral checks, one dimension (parts) away from 4L-SGPS."""
        from hard_rungs import ARC_GROUP, block_sha256

        from pnr.constraints import compile_constraints, compile_routing_rules
        from pnr.fixed_block import arc_center

        spec = self.by_name["07-chaser-20-4L-SGPS-arcblock"]
        block = spec["fixed_block"]
        self.assertGreaterEqual(len(block["arcs"]), 60)
        for net, layer, a, m, z, w in block["arcs"]:
            (cx, cy), r, sweep = arc_center(a, m, z)
            self.assertAlmostEqual(r, 0.25, places=9)
            self.assertAlmostEqual(abs(sweep), math.pi / 2, places=9)
        # Every piece of copper ends on another (no dangling end) but the two ports.
        ends = Counter()
        for net, layer, a, z, w in block["tracks"]:
            ends[(net, tuple(a))] += 1
            ends[(net, tuple(z))] += 1
        for net, layer, a, m, z, w in block["arcs"]:
            ends[(net, tuple(a))] += 1
            ends[(net, tuple(z))] += 1
        single = sorted(
            (round(p[0], 6), round(p[1], 6))
            for (net, p), n in ends.items()
            if n == 1 and net == "CLOCK"
        )
        self.assertEqual(single, [(24.8, 22.4), (24.8, 27.6), (38.175, 22.4), (38.175, 27.6)])
        self.assertEqual(block["sha256"], block_sha256(block, 32))
        parent = self.by_name["07-chaser-20-4L-SGPS"]
        self.assertEqual(
            [k for k in spec["dims"] if spec["dims"][k] != parent["dims"][k]], ["parts"]
        )
        kinds = Counter(c["kind"] for c in spec["checks"])
        self.assertEqual((kinds["copper_digest"], kinds["no_copper"]), (1, 4))
        refs = [p["ref"] for p in spec["parts"]]
        compiled = compile_constraints(spec["constraints"], refs)
        self.assertEqual(compiled.fixed_blocks[0]["group"], ARC_GROUP)
        rules = compile_routing_rules(
            compiled, sorted({n for p in spec["parts"] for n in p["pins"].values() if n})
        )
        guard = [k for k in rules["copper_keepouts"] if k["name"] == "guard-west"][0]
        self.assertEqual(guard["allowed_nets"], ["CLOCK", "GND", "VCC"])
        self.assertIn("arcs", spec["features"])

    def test_bga_classes_rung(self):
        """The BGA rung with class clearances, custom rules, a fiducial and a rounded
        outline: the engine switches, the judge's rules and the checks that hold them."""
        from hard_rungs import CLASSES_FIDUCIAL, CLASSES_OUTLINE

        from pnr.constraints import compile_constraints, compile_routing_rules

        spec = self.by_name["11-ufbga201-fanout-6L-SGSGPS-classes"]
        board = spec["constraints"]["board"]
        self.assertEqual(
            (board["class_clearance"], board["dru_routing"], board["edge"]),
            ("maze", True, "exact"),
        )
        self.assertEqual(spec["outline_shape"], CLASSES_OUTLINE)
        text = dru_text(spec)
        self.assertIn("A.hasNetclass('clk')", text)
        self.assertIn("physical_hole_clearance (min 0.425mm)", text)
        (check,) = [c for c in spec["checks"] if c["kind"] == "net_vias"]
        self.assertEqual(check["nets"], ["PB1", "PE7"])
        self.assertEqual(spec["constraints"]["fixed"]["FID1"]["at"], list(CLASSES_FIDUCIAL))
        refs = [p["ref"] for p in spec["parts"]]
        compiled = compile_constraints(spec["constraints"], refs)
        nets = sorted({n for p in spec["parts"] for n in p["pins"].values() if n})
        rules = compile_routing_rules(compiled, nets)
        self.assertEqual(rules["class_clearance"], "maze")
        classes = {c["name"]: c for c in rules["net_classes"]}
        self.assertEqual(classes["clk"]["clearance_mm"], 0.2)
        self.assertEqual(classes["plane_vcc"]["clearance_mm"], 0.12)

    def test_bga_pairs_rung(self):
        """The BGA rung with a coupled LVDS pair: two adjacent unused south balls to a
        header, the engine switch and pair keys, the judge's rules (skew, uncoupled
        length, gap) and the no-via check, one dimension (parts) from the 6L rung."""
        from hard_rungs import PAIRS_BALLS, PAIRS_HEADER

        from pnr.constraints import compile_constraints, compile_routing_rules

        spec = self.by_name["11-ufbga201-fanout-6L-SGSGPS-pairs"]
        parent = self.by_name["11-ufbga201-fanout-6L-SGSGPS"]
        self.assertEqual(
            [k for k in spec["dims"] if spec["dims"][k] != parent["dims"][k]], ["parts"]
        )
        u1, base = spec["parts"][0]["pins"], parent["parts"][0]["pins"]
        for ball, net in PAIRS_BALLS.items():
            self.assertEqual((base[ball], u1[ball]), ("", net))
        self.assertEqual(spec["constraints"]["fixed"][PAIRS_HEADER[0]]["rot"], 180)
        text = dru_text(spec)
        self.assertIn("A.inDiffPair('LVDS_')", text)
        self.assertIn("diff_pair_uncoupled (max 3mm)", text)
        self.assertIn("skew (max 0.1mm)", text)
        (check,) = [c for c in spec["checks"] if c["kind"] == "net_vias"]
        self.assertEqual((check["nets"], check["max"]), (["LVDS_P", "LVDS_N"], 0))
        refs = [p["ref"] for p in spec["parts"]]
        compiled = compile_constraints(spec["constraints"], refs)
        nets = sorted({n for p in spec["parts"] for n in p["pins"].values() if n})
        rules = compile_routing_rules(compiled, nets)
        self.assertEqual(rules["route_pairs"], "coupled")
        (pair,) = rules["diff_pairs"]
        self.assertEqual((pair["layers"], pair["max_uncoupled_mm"]), (["F.Cu"], 3.0))
        self.assertNotIn(
            "route_pairs",
            compile_routing_rules(
                compile_constraints(parent["constraints"], [p["ref"] for p in parent["parts"]]),
                sorted({n for p in parent["parts"] for n in p["pins"].values() if n}),
            ),
        )

    def test_bga_block_rung(self):
        """The BGA rung with a fixed block: its launch copper digest, ground stitching
        vias on U1's interstitial lattice, a group rule area on F.Cu alone, keep-outs
        the engine compiles (ground allowed in the launch, the plane nets in the
        guards), checks that judge zones, one dimension (parts) from the 6L rung."""
        from hard_rungs import LAUNCH_GROUP, block_sha256

        from pnr.constraints import compile_constraints, compile_routing_rules

        spec = self.by_name["11-ufbga201-fanout-6L-SGSGPS-block"]
        block = spec["fixed_block"]
        self.assertEqual(block["sha256"], block_sha256(block, 36))
        self.assertEqual(block["rule_areas"][0]["layers"], ["F.Cu"])
        lattice = [v for v in block["vias"] if 12 < v[1][0] < 24]
        for _net, (x, y), d, h in lattice:  # interstitial: half a pitch off every ball
            self.assertAlmostEqual(((x - 18) / 0.65) % 1, 0.5, places=6)
            self.assertAlmostEqual(((y - 18) / 0.65) % 1, 0.5, places=6)
            self.assertEqual((d, h), (0.35, 0.15))
        self.assertEqual(len(lattice), 2)
        u1 = spec["parts"][0]["pins"]
        self.assertEqual((u1["E15"], u1["F15"], u1["G15"]), ("GND", "RF_OUT", "GND"))
        parent = self.by_name["11-ufbga201-fanout-6L-SGSGPS"]
        self.assertEqual(
            [k for k in spec["dims"] if spec["dims"][k] != parent["dims"][k]], ["parts"]
        )
        refs = [p["ref"] for p in spec["parts"]]
        compiled = compile_constraints(spec["constraints"], refs)
        self.assertEqual(compiled.fixed_blocks[0]["group"], LAUNCH_GROUP)
        rules = compile_routing_rules(
            compiled, sorted({n for p in spec["parts"] for n in p["pins"].values() if n})
        )
        keepouts = {k["name"]: k for k in rules["copper_keepouts"]}
        self.assertEqual(keepouts["launch"]["allowed_nets"], ["GND"])
        self.assertIn("pours", keepouts["launch"]["items"])
        self.assertEqual(keepouts["guard-north"]["allowed_nets"], ["GND", "VCC"])
        self.assertEqual(rules["fanouts"][0]["skip_pads"], ["F15"])
        launch = [c for c in spec["checks"] if c["id"] == "keepout-launch"][0]
        self.assertIn("zones", launch["items"])

    def test_partial_and_rails_rungs(self):
        from hard_rungs import PARTIAL_BRIDGED, PARTIAL_OPEN, RAIL_HEADERS, _ball_xy

        from pnr.constraints import compile_constraints, compile_routing_rules
        from pnr.fanout.geom import segment_polygon

        parent = self.by_name["11-ufbga201-fanout-6L-SGSGPS"]
        spec = self.by_name["11-ufbga201-fanout-6L-SGSGPS-partial"]
        (fanout,) = spec["constraints"]["fanout"]
        self.assertEqual(fanout["partial"], {"bridge": True})
        self.assertEqual(nets_of(spec), nets_of(parent))
        u1 = spec["parts"][0]["pins"]
        self.assertEqual(u1[PARTIAL_BRIDGED], "VCC")
        self.assertEqual(u1[PARTIAL_OPEN], "VCC")
        # The bridge's path to C7 clears every reserved square (0.1 mm stub, 1 um margin).
        a, b = _ball_xy(PARTIAL_BRIDGED), _ball_xy("C7")
        self.assertEqual(u1["C7"], "VCC")
        for r in fanout["reserved"]:
            if r["name"].startswith("c8-"):
                self.assertGreater(segment_polygon(a, b, r["polygon"]), 0.05 + 1e-3)
        (check,) = [c for c in spec["checks"] if c["kind"] == "unconnected"]
        self.assertEqual(check["pads"], ["U1." + PARTIAL_OPEN])
        self.assertEqual(spec["designed_open"], check["pads"])
        # A designed open is only excused where an unconnected check holds it exact.
        for other in self.by_name.values():
            if other.get("designed_open"):
                pads = [c["pads"] for c in other["checks"] if c["kind"] == "unconnected"]
                self.assertEqual(pads, [other["designed_open"]], other["name"])

        rails = self.by_name["11-ufbga201-fanout-6L-SGSGPS-rails"]
        nets = {n for p in rails["parts"] for n in p["pins"].values() if n}
        self.assertNotIn("VCC", nets)
        self.assertTrue({"VDD", "VDDA", "VBAT"} <= nets)
        refs = [p["ref"] for p in rails["parts"]]
        for net, (ref, _at) in RAIL_HEADERS.items():
            self.assertIn(ref, refs)
            self.assertIn(ref, rails["constraints"]["fixed"])
        c = compile_constraints(rails["constraints"], refs)
        rules = compile_routing_rules(c, sorted(nets))
        (part,) = rules["plane_partition"]
        self.assertEqual(part["nets"], ["VDD", "VDDA", "VBAT"])
        self.assertEqual(part["fill"], "GND")
        self.assertTrue(part["protect_fanouts"])
        self.assertEqual([e["net"] for e in rules["ir_drop"]], ["VDD", "VDDA", "VBAT"])
        balls = [e for e in rules["ir_drop"] if e["net"] == "VDDA"][0]["sinks"]
        self.assertEqual(sorted(balls), ["U1:P1", "U1:R1"])
        classes = [x for x in rules["net_classes"] if x.get("plane_layer") == part["layer"]]
        self.assertEqual(sorted(n for x in classes for n in x["nets"]), ["VBAT", "VDD", "VDDA"])

    def test_buck_pour_rung(self):
        from pnr.constraints import compile_constraints, compile_routing_rules

        pour = self.by_name["11-buck-vqfnhr-4L-SGPS-pour"]
        plain = self.by_name["11-buck-vqfnhr-4L-SGPS"]
        self.assertEqual(nets_of(pour), nets_of(plain))
        self.assertNotIn("plane_partition", plain["constraints"])
        refs = [p["ref"] for p in pour["parts"]]
        c = compile_constraints(pour["constraints"], refs)
        nets = sorted({n for p in pour["parts"] for n in p["pins"].values() if n})
        (entry,) = compile_routing_rules(c, nets)["plane_partition"]
        self.assertEqual(entry["layer"], "F.Cu")
        self.assertEqual(entry["nets"], ["VIN", "SW", "GND"])
        self.assertEqual(entry["region"]["refs"], ["U1", "L1", "C1"])
        self.assertEqual((entry["terminals"], entry["connect"]), ("pad", "solid"))
        for ref in ("U1", "L1", "C1"):
            self.assertIn(ref, pour["constraints"]["fixed"])

    def test_runner_offers_the_hard_rungs(self):
        args = parser().parse_args(["--out", "x", "--hard"])
        self.assertTrue(args.hard)
        self.assertEqual(parser().parse_args(["--out", "x", "--lane", "nightly"]).lane, "nightly")


if __name__ == "__main__":
    unittest.main()
