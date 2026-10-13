"""The SMPS placement surrogate steers geometry without replacing native checks."""

import copy
import os
import unittest
from unittest.mock import patch

import numpy as np
import torch

from pnr.constraints import BoardSpec, CompiledConstraints
from pnr.graph import BoardGraph, BoardOutline, Component, Pad
from pnr.place.geometry import pin_positions
from pnr.place.pad_locality import legalize_kwargs
from pnr.place.smps_cost import WEIGHT, Cost, Slots


def fixture():
    def part(ref, pos, pads):
        return Component(ref, ref, pos, 0, "top", (1, 1), (1, 1), pads=pads)

    g = BoardGraph(
        "smps-cost",
        [
            part(
                "U1",
                (10, 10),
                [
                    Pad("1", "rail", (0, 0), (0.4, 0.4)),
                    Pad("1", "rail", (1, 0), (0.4, 0.4)),
                    Pad("2", "gnd", (0, 1), (0.4, 0.4)),
                ],
            ),
            part(
                "C1",
                (15, 12),
                [Pad("1", "rail", (-0.3, 0), (0.4, 0.4)), Pad("2", "gnd", (0.3, 0), (0.4, 0.4))],
            ),
        ],
        [],
        BoardOutline(25, 25),
    )
    c = CompiledConstraints(board=BoardSpec(25, 25))
    c.smps_checks = [
        dict(id="bypass", kind="pad_distance", terminals=["U1:1", "C1:1"], max_mm=3),
        dict(
            id="loop",
            kind="loop_geometry",
            terminals=["U1:1", "C1:1", "C1:2", "U1:2"],
            max_perimeter_mm=9,
            max_area_mm2=3,
        ),
        dict(id="path", kind="route_path", terminals=["U1:2", "C1:2"], max_length_mm=4, max_vias=0),
    ]
    return g, c


def points(g):
    return np.array([p for c in g.components for _, p in pin_positions(c)])


class SMPSPlacement(unittest.TestCase):
    def test_numpy_torch_batch_and_gradient_agree(self):
        g, c = fixture()
        cost = Cost(g, c)
        xy = np.stack([points(g), points(g) + [10, -3]])
        values = sum(v for _, v in cost.terms(xy))
        t = torch.tensor(xy, requires_grad=True)
        np.testing.assert_allclose(cost.torch(t).detach(), values)
        cost.torch(t).sum().backward()
        eps = 1e-5
        up, down = xy.copy(), xy.copy()
        up[0, 3, 0] += eps
        down[0, 3, 0] -= eps
        derivative = (
            sum(v for _, v in cost.terms(up))[0] - sum(v for _, v in cost.terms(down))[0]
        ) / (2 * eps)
        self.assertAlmostEqual(t.grad[0, 3, 0].item(), derivative, places=6)
        self.assertAlmostEqual(values[0], values[1])

    def test_explicit_perimeter_is_not_replaced_by_stricter_area_surrogate(self):
        g, c = fixture()
        row = Cost(g, c).rows[1]
        self.assertEqual(row["limit"], 9)
        # Area remains an independent audit obligation, not a claimed pass.
        del c.smps_checks[1]["max_perimeter_mm"]
        self.assertAlmostEqual(Cost(g, c).rows[1]["limit"], np.sqrt(12 * np.pi))

    def test_slots_match_independent_full_pose_evaluation(self):
        g, c = fixture()
        comp = copy.deepcopy(g.component("C1"))
        comp.rot = 90
        xs, ys = np.array([14, 16]), np.array([11, 13])
        before = g.to_json()
        values = Slots(g, c)(comp, [g.component("U1")], xs, ys)
        for i in range(2):
            trial = copy.deepcopy(g)
            trial.component("C1").rot = 90
            trial.component("C1").pos = (xs[i], ys[i])
            expected = WEIGHT * sum(v for _, v in Cost(trial, c).terms(points(trial)))
            self.assertAlmostEqual(values[i], expected)
        self.assertEqual(g.to_json(), before)
        np.testing.assert_array_equal(Slots(g, c)(comp, [], xs, ys), [0, 0])

    def test_default_off_and_missing_terminal_rejected(self):
        g, c = fixture()
        with patch.dict(os.environ, {"PNR_SMPS_PLACEMENT": "0", "PNR_PAD_LOCALITY": "0"}):
            self.assertEqual(legalize_kwargs(g, c), {})
        with patch.dict(os.environ, {"PNR_SMPS_PLACEMENT": "1", "PNR_PAD_LOCALITY": "0"}):
            self.assertIsInstance(legalize_kwargs(g, c)["locality"], Slots)
        c.smps_checks[0]["terminals"][0] = "U1:missing"
        with self.assertRaisesRegex(ValueError, "terminal missing"):
            Cost(g, c)


if __name__ == "__main__":
    unittest.main()
