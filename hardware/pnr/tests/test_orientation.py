"""Phase 3 acceptance — orientation search (design §9.3).

The placer co-optimizes a 90° rotation per movable part (a temperature-annealed
softmax over {0,90,180,270}). On the splanc_dev fixture this asserts that
orientations settle to **legal discrete angles**, the result stays fully legal,
orientation is actually exercised, the placement is deterministic, and orientation
**costs no wirelength** against the position-only Phase 2 placement. On a small
synthetic board whose best orientations are known, it asserts that the search
**finds them and shortens the wires**.

The fixture's wirelength check compares means over three seeds with a margin
(Studio-Fug/yapnr#6). A placement is deterministic on one platform, not across
platforms: the macOS and Linux torch wheels round ``exp``, ``log`` and ``addcmul``
differently in the last bit, and the non-convex global placement turns that into a
different legal placement. One seed's final HPWL is a draw whose spread (about 5 %)
exceeds orientation's mean gain on this fixture (1 to 2 %), so the former
single-seed "never worse" check failed on 22 of 60 measured (platform, seed) pairs;
the three-seed mean with a 5 % margin fails on about 2 % of seed triples.
docs/decisions.md has the measurements.
"""

import json
import os
import statistics
import sys
import unittest

import yaml
from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph
from pnr.place import place

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "testdata", "splanc_dev")
LEGAL_ANGLES = {0, 90, 180, 270}
ITERS = 600
# The fixture's seeds: the first three, compared on their mean final HPWL.
SEEDS = (0, 1, 2)
# Orientation may cost at most this fraction of the position-only mean HPWL.
HPWL_MARGIN = 0.05


def _load():
    with open(os.path.join(FIXTURE, "graph.json"), encoding="utf-8") as fh:
        graph = BoardGraph.from_json(fh.read())
    with open(os.path.join(FIXTURE, "constraints.yaml"), encoding="utf-8") as fh:
        constraints = compile_constraints(yaml.safe_load(fh), graph.refs)
    return graph, constraints


class OrientationAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.constraints = _load()
        cls.oriented = {
            seed: place(cls.graph, cls.constraints, seed=seed, iters=ITERS, orient=True)
            for seed in SEEDS
        }
        cls.position_only = {
            seed: place(cls.graph, cls.constraints, seed=seed, iters=ITERS, orient=False)[1]
            for seed in SEEDS
        }
        for seed in SEEDS:
            print(
                f"seed {seed}: HPWL oriented {cls.oriented[seed][1].hpwl_placed:.1f} mm, "
                f"position-only {cls.position_only[seed].hpwl_placed:.1f} mm",
                file=sys.stderr,
            )

    def test_result_is_legal(self):
        for seed, (_, report) in self.oriented.items():
            with self.subTest(seed=seed):
                self.assertTrue(report.legal, report.summary())

    def test_orientations_are_legal_discrete_angles(self):
        for seed, (placed, _) in self.oriented.items():
            for c in placed.components:
                with self.subTest(seed=seed, ref=c.ref):
                    self.assertIn(int(round(c.rot)) % 360, LEGAL_ANGLES)

    def test_orientation_is_exercised(self):
        # The search should actually rotate parts (not collapse to all-zero).
        for seed, (_, report) in self.oriented.items():
            with self.subTest(seed=seed):
                self.assertGreater(report.rotated, 0, report.summary())

    def test_hpwl_not_worse_than_position_only(self):
        # Design §9.3: orientation must not cost wirelength against Phase 2. Compared
        # on the mean over SEEDS with HPWL_MARGIN (module docstring, Studio-Fug/yapnr#6).
        oriented = statistics.mean(report.hpwl_placed for _, report in self.oriented.values())
        position_only = statistics.mean(
            report.hpwl_placed for report in self.position_only.values()
        )
        self.assertLessEqual(
            oriented,
            position_only * (1.0 + HPWL_MARGIN),
            f"mean HPWL over seeds {SEEDS}: oriented {oriented:.0f} vs position-only "
            f"{position_only:.0f} (margin {HPWL_MARGIN:.0%})",
        )

    def test_deterministic(self):
        seed = SEEDS[0]
        placed2, _ = place(self.graph, self.constraints, seed=seed, iters=ITERS, orient=True)
        self.assertEqual(placed2.to_json(), self.oriented[seed][0].to_json())


def _component(ref, courtyard, pads):
    return {
        "ref": ref,
        "footprint": f"synthetic:{ref}",
        "pos": [0.0, 0.0],
        "rot": 0.0,
        "side": "top",
        "courtyard": list(courtyard),
        "bbox": list(courtyard),
        "pads": [
            {"name": name, "net": net, "offset": list(offset), "size": [0.6, 0.6]}
            for name, net, offset in pads
        ],
    }


def _synthetic():
    """A 20 x 20 mm board: full-width fixed anchors N1 (north) and S1 (south), each
    with a pad at x = 6 and x = 14, and two movable 6 x 1.5 mm parts with a pad at
    each end, started between the anchors. R1's pad 1 connects to N1 and its pad 2
    to S1, so R1 is shortest turned 270° (pad 1 up); R2 is wired the other way round
    and is shortest at 90°. Aligned, each part's two nets span 11 mm; at any other
    angle they span 21 mm."""
    graph = BoardGraph.from_json(
        json.dumps(
            {
                "name": "orientation-synthetic",
                "components": [
                    _component(
                        "N1", (18.0, 2.0), [("1", "a", (-4.0, 0.0)), ("2", "c", (4.0, 0.0))]
                    ),
                    _component(
                        "S1", (18.0, 2.0), [("1", "b", (-4.0, 0.0)), ("2", "d", (4.0, 0.0))]
                    ),
                    _component("R1", (6.0, 1.5), [("1", "a", (-2.5, 0.0)), ("2", "b", (2.5, 0.0))]),
                    _component("R2", (6.0, 1.5), [("1", "d", (-2.5, 0.0)), ("2", "c", (2.5, 0.0))]),
                ],
                "nets": [
                    {"name": "a", "code": 1, "pins": [["N1", "1"], ["R1", "1"]]},
                    {"name": "b", "code": 2, "pins": [["S1", "1"], ["R1", "2"]]},
                    {"name": "c", "code": 3, "pins": [["N1", "2"], ["R2", "2"]]},
                    {"name": "d", "code": 4, "pins": [["S1", "2"], ["R2", "1"]]},
                ],
                "outline": {"width": 20.0, "height": 20.0},
            }
        )
    )
    constraints = compile_constraints(
        {
            "schema": "v0",
            "board": {"outline": {"w": 20, "h": 20}, "layers": 2, "default_clearance_mm": 0.2},
            "fixed": {"N1": {"at": [10.0, 18.0], "rot": 0}, "S1": {"at": [10.0, 2.0], "rot": 0}},
        },
        graph.refs,
    )
    return graph, constraints


class OrientationSearchTest(unittest.TestCase):
    """The search finds known best orientations: a well-conditioned problem whose
    result does not depend on the platform's last-bit rounding."""

    @classmethod
    def setUpClass(cls):
        graph, constraints = _synthetic()
        # Explicit starts between the anchors, so the result does not depend on the
        # seeded random start either.
        starts = {"R1": (6.0, 10.0), "R2": (14.0, 10.0)}
        cls.placed, cls.report = place(
            graph, constraints, seed=0, iters=ITERS, orient=True, initial_positions=starts
        )
        _, cls.report_noorient = place(
            graph, constraints, seed=0, iters=ITERS, orient=False, initial_positions=starts
        )

    def test_finds_the_aligning_orientations(self):
        rotations = {ref: int(round(self.placed.component(ref).rot)) % 360 for ref in ("R1", "R2")}
        self.assertEqual(rotations, {"R1": 270, "R2": 90}, self.report.summary())

    def test_hpwl_shorter_than_position_only(self):
        self.assertTrue(self.report.legal, self.report.summary())
        # Aligned: 2 x 11 mm, plus the legalizer's 0.25 mm grid; position-only: 2 x 21 mm.
        self.assertLessEqual(self.report.hpwl_placed, 23.0, self.report.summary())
        self.assertGreaterEqual(
            self.report_noorient.hpwl_placed, 42.0, self.report_noorient.summary()
        )


if __name__ == "__main__":
    unittest.main()
