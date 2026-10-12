"""Pad ownership, duplicate-pad geometry, and capture-independent slot decisions."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from pnr.constraints import BoardSpec, CompiledConstraints, Constraint, Enforcement
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.cost_inspect import Objective
from pnr.place.geometry import pin_positions
from pnr.place.legalize import legalize
from pnr.place.model import global_place
from pnr.place.pad_locality import Cost, Slots, anchor_index, attachments, audit


def fixture():
    def p(name, net, xy):
        return Pad(name, net, xy, (0.4, 0.4))

    hub = Component(
        "H",
        "hub",
        (10, 10),
        90,
        "top",
        (4, 4),
        (4, 4),
        pads=[
            p("1", "private", (2, 0)),
            p("2", "return", (2, 1)),
            p("3", "third", (-2, 0)),
            p("2", "return", (1, 1)),
        ],
    )
    leaf = Component(
        "L",
        "leaf",
        (4, 4),
        0,
        "top",
        (1, 1),
        (1, 1),
        pads=[p("a", "private", (-0.3, 0)), p("b", "return", (0.3, 0))],
    )
    graph = BoardGraph(
        "generic",
        [hub, leaf],
        [
            Net(n, i, [(c.ref, p.name) for c in [hub, leaf] for p in c.pads if p.net == n])
            for i, n in enumerate(["private", "return", "third"], 1)
        ],
        BoardOutline(20, 20),
    )
    cc = CompiledConstraints(board=BoardSpec(20, 20), constraints=[])
    return graph, cc


class Locality(unittest.TestCase):
    def test_two_terminals_and_duplicate_pad_alternatives(self):
        g, c = fixture()
        a = attachments(g, c)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(a[0]["links"]), 2)
        self.assertEqual(len(a[0]["links"][1]["targets"]), 2)
        xy = np.array([point for comp in g.components for _, point in pin_positions(comp)])
        value = Cost(g, c).torch(torch.tensor(xy)).item()
        self.assertAlmostEqual(value, sum(v for _, v in Cost(g, c).numpy_terms(xy)))
        self.assertAlmostEqual(
            4 * value, Slots(g, c)(g.component("L"), [g.component("H")], *g.component("L").pos)
        )
        self.assertEqual(anchor_index(g, "H", "2"), 1)

    def test_shared_rails_do_not_infer_ownership_from_broad_hierarchy(self):
        g, c = fixture()
        other = copy.deepcopy(g.component("H"))
        other.ref = "OTHER"
        g.components.append(other)
        c.constraints = [
            Constraint("group", Enforcement.HARD, ("H", "L"), dict(anchor="H", radius_mm=8))
        ]
        self.assertEqual(attachments(g, c), [])
        c.constraints[0].params["anchor_pad"] = "1"
        self.assertEqual(attachments(g, c)[0]["anchor"], "H")

    def test_no_threshold_means_unqualified_and_explicit_limit_catches_bad_placement(self):
        g, c = fixture()
        a = audit(g, c)
        self.assertFalse(a["qualified"])
        self.assertEqual(a["missing_limits"], 2)
        a = audit(g, c, {("L", "a", "H"): 2, ("L", "b", "H"): 2})
        self.assertTrue(all(r["passed"] is False for r in a["links"]))

    def test_actual_loss_matches_replay_with_pad_anchor_and_tuned_weights(self):
        g, c = fixture()
        c.constraints = [
            Constraint(
                "group", Enforcement.HARD, ("H", "L"), dict(anchor="H", anchor_pad="1", radius_mm=3)
            )
        ]
        c.constraints.append(
            Constraint("fixed", Enforcement.HARD, ("H",), dict(at=[10, 10], rot=90, side="top"))
        )
        actual = []
        old = torch.Tensor.backward

        def capture(t, *a, **kw):
            actual.append(float(t))
            return old(t, *a, **kw)

        with patch.dict(os.environ, {"PNR_PAD_LOCALITY": "1"}), patch.object(
            torch.Tensor, "backward", capture
        ):
            global_place(
                g,
                c,
                20,
                20,
                iters=1,
                lr=0,
                orient=False,
                initial_positions={p.ref: p.pos for p in g.components},
                initial_rotations={p.ref: p.rot for p in g.components},
                w_wire=2,
                w_locality=7,
            )
            # orient=False retains graph rotations in the engine.
            value = Objective(g, c, parameters={"w_wire": 2, "w_locality": 7}).report()[
                "board_total"
            ]
        self.assertAlmostEqual(actual[0], value, delta=0.01)

    def test_saved_converter_bootstrap_obligations_cover_both_pads(self):
        data = json.loads(
            (
                Path(__file__).resolve().parents[1] / "testdata/power_topology/converter.json"
            ).read_text()
        )
        g = BoardGraph.from_json(json.dumps(data["graph"]))
        cons = CompiledConstraints(board=BoardSpec(30, 30))
        relation = next(r for r in attachments(g, cons) if r["member"] == "C9")
        self.assertEqual(relation["anchor"], "U5")
        self.assertEqual({r["pad"] for r in relation["links"]}, {"1", "2"})
        self.assertEqual(relation["links"][0]["target_pads"], ["20"])
        g.component("C9").pos = (100, 100)
        audit_rows = audit(g, cons, {("C9", "1", "U5"): 2, ("C9", "2", "U5"): 2})["links"]
        self.assertEqual([r["passed"] for r in audit_rows if r["member"] == "C9"], [False, False])
        with self.assertRaises(ValueError):
            audit(g, cons, {("C9", "1", "U5"): float("nan")})

    def test_unlimited_mode_preserves_finite_prefix_and_continues_with_live_temperature(self):
        g, c = fixture()
        finite, streamed = [], []
        with patch.dict(os.environ, {"PNR_PAD_LOCALITY": "1", "PNR_COST_CAPTURE_DIR": ""}):
            global_place(
                g,
                c,
                20,
                20,
                seed=12,
                iters=3,
                updated=lambda step, pos, p: finite.append((step, pos, p)),
            )

            def control(step):
                if step == 8:
                    raise InterruptedError("test stop")
                return {} if step < 3 else dict(temperature=1.5, next_temperature=1.5)

            with self.assertRaisesRegex(InterruptedError, "test stop"):
                global_place(
                    g,
                    c,
                    20,
                    20,
                    seed=12,
                    iters=3,
                    run_forever=True,
                    control=control,
                    updated=lambda step, pos, p: streamed.append((step, pos, p)),
                )
            self.assertEqual(finite, streamed[:3])
            self.assertEqual(len(streamed), 8)
            self.assertNotEqual(streamed[2][1], streamed[-1][1])
            self.assertTrue(all(0 <= q <= 1 for row in streamed[-1][2] for q in row))
            with self.assertRaises(ValueError):
                global_place(g, c, 20, 20, iters=3, run_forever=True)

    def test_live_objective_schedule_is_reproducible_and_callbacks_do_not_move_parts(self):
        g, c = fixture()
        with patch.dict(os.environ, {"PNR_PAD_LOCALITY": "1", "PNR_COST_CAPTURE_DIR": ""}):
            plain = global_place(g, c, 20, 20, seed=12, iters=20)
            observed = []
            initial = []
            updates = []
            controls = []

            def observe_control(step):
                self.assertEqual(len(initial), 1)
                controls.append(step)
                return {}

            observed_result = global_place(
                g,
                c,
                20,
                20,
                seed=12,
                iters=20,
                initialized=lambda pos, p: initial.append((pos, p)),
                updated=lambda step, pos, p: updates.append((step, pos, p)),
                control=observe_control,
                progress=lambda step, pos, p, loss: observed.append((step, loss, pos, p)),
            )
            self.assertEqual(plain, observed_result)
            self.assertEqual(len(observed), 20)
            self.assertEqual(controls, list(range(20)))
            self.assertEqual(initial[0], (observed[0][2], observed[0][3]))
            self.assertEqual([u[0] for u in updates], list(range(1, 21)))
            for update, evaluation in zip(updates, observed[1:]):
                self.assertEqual(update[1:], evaluation[2:])
            self.assertEqual(
                {comp.ref: tuple(pos) for comp, pos in zip(g.components, updates[-1][1])},
                plain[0],
            )

            def schedule(step):
                return {"w_locality": 4 if step < 8 else 20}

            tuned = global_place(g, c, 20, 20, seed=12, iters=20, control=schedule)
            replay = global_place(g, c, 20, 20, seed=12, iters=20, control=schedule)
            self.assertEqual(tuned, replay)
            self.assertNotEqual(plain, tuned)

    def test_capture_does_not_change_locality_legalization_and_terms_reconcile(self):
        g, c = fixture()
        kw = dict(
            fixed={"H": (10, 10)},
            keepouts=[],
            allow_rotation=True,
            grid_mm=0.25,
            locality=Slots(g, c),
        )
        with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": ""}):
            base = legalize(g, 20, 20, **kw)
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"PNR_COST_CAPTURE_DIR": td}
        ):
            result = legalize(g, 20, 20, **kw)
            self.assertEqual(base.to_json(), result.to_json())
            docs = [json.loads(p.read_text()) for p in Path(td).glob("*.json")]
            d = next(d for d in docs if d["kind"] == "legalizer-decision")
            self.assertEqual(d["original_position"], [4, 4])
            f = next(f for f in d["candidate_fields"] if f["rotation"] == d["rotation"])
            with np.load(f["path"]) as z:
                v = z["values"]
            row = v[np.argmin(((v[:, :2] - d["position"]) ** 2).sum(1))]
            self.assertAlmostEqual(d["total"], row[2] + 25 * row[3] + row[4] + row[5] + row[6])
            self.assertGreater(d["terms"][-2]["weighted"], 0)


if __name__ == "__main__":
    unittest.main()
