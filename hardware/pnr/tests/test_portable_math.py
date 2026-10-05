"""Platform-independent global placement (pnr.place.portable_math).

The nightly ladder's 08-chaser-20-plane seed 0 passed on macOS arm64 and failed on the
Linux arm64 runner: torch's exp/log (softmax, sigmoid, logsumexp) and Adam's compound
kernels round the last bit differently per platform, and 350 Adam steps turned that into
a different placement. These tests pin the replacement:

* accuracy against torch (values and gradients);
* golden bits on fixed inputs, the same on every platform (this test runs on the Mac and
  in the Linux CI);
* global_place calls none of torch's platform math;
* a global placement of the splanc_dev fixture gives golden bits.

A deliberate change to the placement model changes the last golden: rerun this test and
take the printed hash, on any platform.
"""

import hashlib
import os
import unittest
from unittest import mock

import numpy as np
import torch
import yaml

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph
from pnr.place import portable_math as pm
from pnr.place.geometry import outline_size
from pnr.place.model import global_place

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "testdata", "splanc_dev")


def _digest(*tensors) -> str:
    h = hashlib.sha256()
    for t in tensors:
        h.update(np.ascontiguousarray(t.detach().numpy()).tobytes())
    return h.hexdigest()[:16]


def _inputs(n=2048):
    # Exact IEEE arithmetic only (no platform math): a ramp, scaled and folded.
    k = torch.arange(n, dtype=torch.float32)
    return ((k * 0.37) % 61.0 - 30.0) * 1.25


class AccuracyTest(unittest.TestCase):
    def _check(self, f, ref, x, rtol, gtol):
        x = x.clone().requires_grad_()
        w = torch.arange(1, x.numel() + 1, dtype=torch.float32).view_as(x) / x.numel()
        y = f(x)
        (g,) = torch.autograd.grad((y * w.view_as(y) if y.shape == x.shape else y).sum(), x)
        yr = ref(x)
        (gr,) = torch.autograd.grad((yr * w.view_as(yr) if yr.shape == x.shape else yr).sum(), x)
        torch.testing.assert_close(y, yr, rtol=rtol, atol=1e-30)
        torch.testing.assert_close(g, gr, rtol=gtol, atol=1e-6)

    def test_exp(self):
        self._check(pm.exp, torch.exp, _inputs() / 2.0, 2e-7, 2e-7)

    def test_log(self):
        x = _inputs().abs() + 1e-3
        self._check(pm.log, torch.log, x, 2e-7, 2e-7)

    def test_logsumexp(self):
        x = _inputs().view(32, 64)
        self._check(lambda t: pm.logsumexp(t, 0), lambda t: torch.logsumexp(t, 0), x, 2e-7, 1e-5)

    def test_softmax(self):
        x = _inputs().view(512, 4) / 4.0
        self._check(lambda t: pm.softmax(t, 1), lambda t: torch.softmax(t, 1), x, 1e-6, 1e-5)

    def test_sigmoid(self):
        x = _inputs() / 4.0
        self._check(pm.sigmoid, torch.sigmoid, x, 1e-6, 1e-5)

    def test_float64_kernels(self):
        x = torch.linspace(-700.0, 700.0, 20001, dtype=torch.float64)
        rel = ((pm._exp64(x) - torch.exp(x)).abs() / torch.exp(x)).max().item()
        self.assertLess(rel, 1e-11)
        y = torch.exp(x)
        err = (pm._log64(y) - torch.log(y)).abs().max().item()
        self.assertLess(err, 1e-11)
        self.assertEqual(pm.log(torch.tensor([0.0])).item(), -float("inf"))

    def test_quarter_turns_are_exact(self):
        self.assertEqual(pm.quarter_turn(90.0), (0.0, 1.0))
        self.assertEqual(pm.quarter_turn(270), (0.0, -1.0))
        with self.assertRaises(ValueError):
            pm.quarter_turn(45.0)

    def test_adam_matches_torch(self):
        torch.manual_seed(0)
        a = torch.randn(64, 2, requires_grad=True)
        b = a.detach().clone().requires_grad_()
        oa, ob = pm.Adam([a], lr=0.3), torch.optim.Adam([b], lr=0.3)
        for _ in range(50):
            for p, o in ((a, oa), (b, ob)):
                o.zero_grad()
                ((p - 1.5) ** 2 * torch.arange(1.0, 3.0)).sum().backward()
                o.step()
        torch.testing.assert_close(a, b, rtol=1e-5, atol=1e-5)


class GoldenBitsTest(unittest.TestCase):
    """The same bits on every platform (the Mac and the Linux CI run this test)."""

    def test_functions(self):
        x = _inputs().requires_grad_()
        outputs = []
        for f in (
            lambda t: pm.exp(t / 2.0),
            lambda t: pm.log(t.abs() + 1e-3),
            lambda t: pm.logsumexp(t.view(32, 64), 0),
            lambda t: pm.softmax(t.view(512, 4) / 4.0, 1),
            lambda t: pm.sigmoid(t / 4.0),
        ):
            y = f(x)
            (g,) = torch.autograd.grad(y.sum(), x)
            outputs += [y, g]
        self.assertEqual(_digest(*outputs), "d0c07503f34cd4f6")

    def test_adam(self):
        p = (_inputs(256) / 10.0).view(128, 2).requires_grad_()
        opt = pm.Adam([p], lr=0.3)
        for _ in range(100):
            opt.zero_grad()
            pm.logsumexp(p * p, 0).sum().backward()
            opt.step()
        self.assertEqual(_digest(p), "d327ddd63624e1e9")


def _fixture():
    with open(os.path.join(FIXTURE, "graph.json"), encoding="utf-8") as fh:
        graph = BoardGraph.from_json(fh.read())
    with open(os.path.join(FIXTURE, "constraints.yaml"), encoding="utf-8") as fh:
        constraints = compile_constraints(yaml.safe_load(fh), graph.refs)
    return graph, constraints


def _forbidden(name):
    def fail(*_args, **_kwargs):
        raise AssertionError("global_place called platform math: " + name)

    return fail


class GlobalPlaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.constraints = _fixture()
        cls.size = outline_size(cls.graph, cls.constraints)

    def _place(self):
        w, h = self.size
        return global_place(self.graph, self.constraints, w, h, seed=0, iters=120)

    def test_no_platform_math(self):
        names = ["exp", "log", "logsumexp", "softmax", "sigmoid", "cos", "sin", "tanh"]
        with mock.patch.multiple(torch, **{n: _forbidden("torch." + n) for n in names}):
            with mock.patch.object(torch.optim, "Adam", _forbidden("torch.optim.Adam")):
                self._place()

    def test_golden_placement(self):
        positions, rotations = self._place()
        h = hashlib.sha256()
        for ref in sorted(positions):
            x, y = positions[ref]
            h.update(("%s %s %s %r\n" % (ref, x.hex(), y.hex(), rotations[ref])).encode())
        self.assertEqual(h.hexdigest()[:16], "9cc44fdd53772399", "golden: " + h.hexdigest()[:16])


if __name__ == "__main__":
    unittest.main()
