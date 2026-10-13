"""Surface slices preserve the live optimizer and reuse its cost semantics."""

import copy
import dataclasses
import json
import os
import unittest
from unittest.mock import patch

import numpy as np

from pnr.constraints import Constraint, Enforcement, compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.model import global_place
from yapnr.viewer.services.placement_objective import DEFAULT
from yapnr.viewer.services.placement_surface_expression import evaluate, expression
from yapnr.viewer.services.placement_surface_worker import compute


class SurfaceTest(unittest.TestCase):
    def test_arithmetic_and_rejected_execution_or_nonfinite_results(self):
        terms = {"wirelength": np.array([1.0, 4.0]), "outline": np.array([2.0, 3.0])}
        np.testing.assert_equal(evaluate("sqrt(wirelength) + max(2*outline, 5) - 1", terms), [5, 7])
        for text in (
            "__import__('os')",
            "wirelength[0]",
            "x.y",
            "wirelength**wirelength",
            "wirelength**99",
            "1e999",
            "True",
            "max(x=1)",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                expression(text)
        with self.assertRaisesRegex(ValueError, "non-finite"):
            evaluate("wirelength/0", terms)

    def test_four_rotations_match_engine_rigid_loss_without_mutating_input(self):
        parts = [
            Component(
                ref,
                "fixture",
                pos,
                0,
                "top",
                (2, 1),
                (2, 1),
                pads=[Pad("1", "V", (1, 0), (0.5, 0.5))],
            )
            for ref, pos in [("A", (3, 4)), ("B", (8, 7))]
        ]
        graph = BoardGraph(
            "surface", parts, [Net("V", 1, [("A", "1"), ("B", "1")])], BoardOutline(10, 10)
        )
        cc = compile_constraints({"board": {"width_mm": 10, "height_mm": 10}}, graph.refs)
        request = dict(
            frame=dict(graph=json.loads(graph.to_json()), step=0),
            source=dict(constraints=dataclasses.asdict(cc)),
            ref="A",
            expression="objective",
            objective_expression=DEFAULT,
            pitch=5,
            frame_sha256="a" * 64,
        )
        before = copy.deepcopy(request)
        result = compute(request)
        self.assertEqual(request, before)
        self.assertEqual([f["rotation"] for f in result["fields"]], [0, 90, 180, 270])
        for field in result["fields"]:
            graph.components[0].rot = field["rotation"]
            graph.components[0].pos = (2.5, 2.5)
            cc.constraints = [
                Constraint("orientation", Enforcement.HARD, ("A",), dict(rot=field["rotation"]))
            ]
            losses = []
            with patch.dict(os.environ, {"PNR_PAD_LOCALITY": "1", "PNR_COST_CAPTURE_DIR": ""}):
                global_place(
                    graph,
                    cc,
                    10,
                    10,
                    iters=1,
                    lr=0,
                    orient=False,
                    initial_positions={c.ref: c.pos for c in graph.components},
                    initial_rotations={c.ref: c.rot for c in graph.components},
                    progress=lambda step, pos, probs, loss: losses.append(loss),
                )
            self.assertAlmostEqual(field["values"][0], losses[0], delta=0.001)
        self.assertNotEqual(result["fields"][0]["values"], result["fields"][1]["values"])
        self.assertEqual(result["minimum"], min(min(f["values"]) for f in result["fields"]))
        request["expression"] = "0*wirelength + 7"
        constant = compute(request)
        self.assertTrue(all(v == 7 for f in constant["fields"] for v in f["values"]))


if __name__ == "__main__":
    unittest.main()
