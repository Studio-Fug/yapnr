"""PNR_PAIR_POST_BRIDGE_SURFACE / JOINT_FAIR / PREFER_INLINE / FALLBACK_RESERVE_SECONDS / HAND_SWAP_TRIAL regressions (KiCad Python).

Chain J -> D (ESD) -> U. Stage 0 is a via bridge that ends in a via pair to the
right of D with F.Cu via-to-pad fanouts (stubs). The stage-1 pad-start surface
leg is scripted to cross the p stub (Oracle.clear ignores same-net copper), the
production failure mode that the final check rejects as an ambiguous cycle.
Everything after that is native: real Oracle, real path_metrics, real solve_pair
for the via-start leg, real reference-plane fill.
"""

import math
import os
import time
import unittest
from unittest.mock import patch

import pcbnew as k
from test_native_electrical import FAB, board, pad
from test_native_electrical import rules as power_rules

from pnr.native_electrical import Oracle, _pair_plan_order, pair_plan, surface_leg_graph_failure
from pnr.route.detail import coupled
from pnr.route.detail.coupled import path_metrics

F, B = k.F_Cu, k.B_Cu
PADS = {
    "J": {"p": (3, 5.2), "n": (3, 4.8)},
    "D": {"p": (8, 5.2), "n": (8, 4.8)},
    "U": {"p": (15, 5.2), "n": (15, 4.8)},
}
VIAS0 = {"p": (4, 5.6), "n": (4, 4.4)}  # stage-0 bridge source vias (next to J)
VIAS1 = {"p": (9, 5.6), "n": (9, 4.4)}  # stage-0 bridge target vias (next to D, toward U)
STUB = {"p": [(9, 5.6), (8.4, 5.6), (8, 5.2)], "n": [(9, 4.4), (8.4, 4.4), (8, 4.8)]}
# Scripted stage-1 pad-start legs: p crosses its own stub at (8.8,5.6).
CROSSING = {
    "p": [(8, 5.2), (8.8, 5.2), (8.8, 6.2), (15, 6.2), (15, 5.2)],
    "n": [(8, 4.8), (8.8, 4.8), (8.8, 3.8), (15, 3.8), (15, 4.8)],
}
CLEAN = {
    "p": [(8, 5.2), (8, 6.2), (15, 6.2), (15, 5.2)],
    "n": [(8, 4.8), (8, 3.8), (15, 3.8), (15, 4.8)],
}
plen = lambda path: sum(math.dist(a, z) for a, z in zip(path, path[1:]))


def chain_board():
    b = board()
    chain = []
    for ref in ("J", "D", "U"):
        for num, net in (("1", "p"), ("2", "n")):
            pad(b, ref, num, net, PADS[ref][net], (0.2, 0.2))
        chain.append({"p": ref + ".1", "n": ref + ".2"})
    zone = k.ZONE(b)
    zone.SetLayer(k.In1_Cu)
    zone.SetNetCode(b.FindNet("rail").GetNetCode())
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in [(1, 1), (19, 1), (19, 19), (1, 19)]:
        outline.Append(round(x * 1e6), round(y * 1e6))
    b.Add(zone)
    k.ZONE_FILLER(b).Fill(b.Zones())
    b.BuildConnectivity()
    pair = dict(
        name="usb",
        p="p",
        n="n",
        width_mm=0.2,
        gap_mm=0.2,
        skew_mm=0.1,
        max_uncoupled_mm=2,
        terminal_chain=chain,
        reference_layer="In1.Cu",
    )
    r = dict(
        fab={
            "track_width_mm": 0.2,
            "clearance_mm": 0.15,
            "via_diameter_mm": 0.6,
            "via_drill_mm": 0.3,
        },
        electrical_fab=FAB,
        net_classes=[dict(name="ground", nets=["rail"], plane_layer="In1.Cu")],
    )
    return b, pair, r


def stage0_bridge():
    tracks = []
    lengths = {}
    for net in ("p", "n"):
        tracks += [(net, F, PADS["J"][net], VIAS0[net], 0.2), (net, B, VIAS0[net], VIAS1[net], 0.2)]
        tracks += [(net, F, a, z, 0.2) for a, z in zip(STUB[net], STUB[net][1:])]
        lengths[net] = (
            math.dist(PADS["J"][net], VIAS0[net])
            + 1.6
            + math.dist(VIAS0[net], VIAS1[net])
            + 1.6
            + plen(STUB[net])
        )
    return dict(
        status="routed",
        pair_tracks=tracks,
        pair_vias=[(net, v) for vs in (VIAS0, VIAS1) for net, v in vs.items()],
        via_diameter_mm=0.6,
        via_drill_mm=0.3,
        bridge_target=dict(
            sites=dict(VIAS1),
            paths={net: list(reversed(STUB[net])) for net in ("p", "n")},
            lengths={net: plen(STUB[net]) for net in ("p", "n")},
        ),
        lengths=lengths,
        reference_paths={net: [VIAS0[net], VIAS1[net]] for net in ("p", "n")},
    )


class PostBridgeSurfaceTest(unittest.TestCase):
    def plan(self, flag, stage1):
        """Run the real _pair_plan_order with a scripted stage 0 and stage-1 pad-start leg."""
        b, pair, r = chain_board()
        oracle = Oracle(b, r, deadline=time.monotonic() + 60)
        calls = []
        real = coupled.solve_pair

        def solve(p, n, terminals, *args, **kw):
            start = {net: tuple(terminals[net][0]) for net in (p, n)}
            calls.append((start, dict(kw.get("offsets") or {})))
            if start == {net: PADS["J"][net] for net in (p, n)}:
                return dict(status="no_coupled_channel", failures={"scripted": 1})
            if start == {net: PADS["D"][net] for net in (p, n)}:
                offsets = kw.get("offsets") or {}
                return dict(
                    status="routed",
                    paths={net: list(stage1[net]) for net in (p, n)},
                    lengths={net: plen(stage1[net]) + offsets.get(net, 0) for net in (p, n)},
                )
            return real(p, n, terminals, *args, **kw)

        bridges = []

        def bridge(*args, **kw):
            bridges.append(kw.get("reuse_source"))
            return (
                stage0_bridge()
                if len(bridges) == 1
                else dict(status="pair_no_matched_layer_bridge", failures={})
            )

        env = {"PNR_PAIR_POST_BRIDGE_SURFACE": "1"} if flag else {}
        with patch.dict(os.environ, env, clear=False), patch(
            "pnr.native_electrical.solve_pair", side_effect=solve
        ), patch("pnr.native_electrical.pair_layer_bridge", side_effect=bridge):
            if not flag:
                os.environ.pop("PNR_PAIR_POST_BRIDGE_SURFACE", None)
            result = _pair_plan_order(b, pair, r, oracle, (1, 1, 19, 19), 0.2, ("p", "n"))
        return result, calls, bridges, pair

    def test_flag_off_reproduces_late_cycle_rejection(self):
        result, calls, bridges, _ = self.plan(False, CROSSING)
        self.assertEqual(result["status"], "pair_endpoint_graph_invalid")
        self.assertEqual(result["endpoint_metrics"]["p"].get("reason"), "ambiguous_cycle")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(bridges), 1)
        self.assertNotIn("post_bridge_legs", result)

    def test_crossing_leg_is_rejected_and_rerouted_from_bridge_vias(self):
        result, calls, bridges, pair = self.plan(True, CROSSING)
        self.assertEqual(
            result["status"],
            "routed",
            {key: result.get(key) for key in ("status", "post_bridge_legs", "failed_stage")},
        )
        self.assertEqual(
            [leg["start"] for leg in result["post_bridge_legs"]], ["pad", "bridge_vias"]
        )
        self.assertEqual(result["post_bridge_legs"][0]["status"], "pair_surface_leg_cycle")
        self.assertEqual(result["post_bridge_legs"][0]["net"], "p")
        # Third solve starts at the bridge vias; its timing origin is the measured
        # prefix up to the via (prefix to the D pad minus the via-to-pad stub).
        start, offsets = calls[2]
        self.assertEqual(start, {net: tuple(VIAS1[net]) for net in ("p", "n")})
        pre = stage0_bridge()["lengths"]
        for net in ("p", "n"):
            self.assertAlmostEqual(offsets[net], pre[net] - plen(STUB[net]), places=6)
        self.assertEqual(len(bridges), 1)  # no stage-1 bridge, no new vias
        self.assertEqual(len(result["pair_vias"]), 4)
        segment = result["segments"][1]
        self.assertEqual(
            segment["post_bridge_start"]["sites"], {net: tuple(VIAS1[net]) for net in ("p", "n")}
        )
        # Independent re-measurement of the committed copper: a tree per net, D is a
        # dangling stub, lengths/skew match the plan and stay inside the contract.
        for net in ("p", "n"):
            metric = path_metrics(
                [(la, a, z) for nn, la, a, z, w in result["pair_tracks"] if nn == net],
                [(v, [F, B]) for nn, v in result["pair_vias"] if nn == net],
                (PADS["J"][net], F),
                (PADS["U"][net], F),
                layer_heights={F: 0, B: 1.6},
            )
            self.assertTrue(metric["valid"], metric)
            self.assertGreater(metric["branch_vertices"], 0)
            self.assertAlmostEqual(
                metric["length_mm"], result["endpoint_metrics"][net]["length_mm"], places=6
            )
            self.assertAlmostEqual(metric["length_mm"], segment["lengths"][net], places=6)
        self.assertLessEqual(
            abs(
                result["endpoint_metrics"]["p"]["length_mm"]
                - result["endpoint_metrics"]["n"]["length_mm"]
            ),
            pair["skew_mm"] + 1e-6,
        )

    def test_loop_free_pad_start_leg_is_kept_unchanged(self):
        off, off_calls, _, _ = self.plan(False, CLEAN)
        on, on_calls, _, _ = self.plan(True, CLEAN)
        self.assertEqual(off["status"], "routed")
        self.assertEqual(on["status"], "routed")
        self.assertEqual(len(off_calls), 2)
        self.assertEqual(len(on_calls), 2)
        self.assertEqual(off["pair_tracks"], on["pair_tracks"])
        self.assertEqual(off["endpoint_metrics"], on["endpoint_metrics"])
        self.assertEqual(on["post_bridge_legs"], [dict(stage=1, start="pad", status="routed")])
        self.assertNotIn("post_bridge_start", on["segments"][1])

    def test_graph_helper_names_the_looping_net(self):
        bridge = stage0_bridge()
        origins = {net: PADS["J"][net] for net in ("p", "n")}
        ends = {net: PADS["U"][net] for net in ("p", "n")}
        broken = surface_leg_graph_failure(
            dict(p="p", n="n"),
            bridge["pair_tracks"],
            bridge["pair_vias"],
            CROSSING,
            F,
            origins,
            ends,
            1.6,
        )
        self.assertEqual((broken["net"], broken["reason"]), ("p", "ambiguous_cycle"))
        self.assertIsNone(
            surface_leg_graph_failure(
                dict(p="p", n="n"),
                bridge["pair_tracks"],
                bridge["pair_vias"],
                CLEAN,
                F,
                origins,
                ends,
                1.6,
            )
        )


class JointFairScheduleTest(unittest.TestCase):
    def schedule(self, fair):
        b = board()
        for label, pt in {
            "J.B+": (1.5, 0),
            "J.A+": (0.5, 0),
            "J.B-": (0, 0),
            "J.A-": (1, 0),
        }.items():
            ref, num = label.rsplit(".", 1)
            pad(b, ref, num, "p" if "+" in num else "n", pt, (0.2, 0.2))
        pair = dict(
            p="p",
            n="n",
            gap_mm=0.15,
            skew_mm=0.3,
            terminal_chain=[{"p": "J.A+", "n": "J.A-"}, {"p": "U.+", "n": "U.-"}],
            auxiliary_pairs=[
                dict(
                    source={"p": "J.B+", "n": "J.B-"},
                    target={"p": "J.A+", "n": "J.A-"},
                    max_length_mm=3,
                )
            ],
        )
        clock = [100.0]
        r = power_rules()
        o = Oracle(b, r, deadline=190.0)
        seen = []

        def attempt(b, p, r, trial, bounds, pitch, order, topology=None):
            seen.append(
                (
                    (topology or {}).get("bridge_hand"),
                    (topology or {}).get("takeoff"),
                    trial.deadline - clock[0],
                )
            )
            clock[0] = trial.deadline
            return dict(status="time_budget")

        env = {
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
            "PNR_PAIR_JOINT_TRIAL_SECONDS": "240",
            "PNR_PAIR_JOINT_MAX_TRIALS": "6",
            "PNR_PAIR_AUXILIARY_SCOPE": "separate",
        }
        if fair:
            env["PNR_PAIR_JOINT_FAIR"] = "1"
        with patch.dict(os.environ, env), patch(
            "pnr.native_electrical.time.monotonic", side_effect=lambda: clock[0]
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            if not fair:
                os.environ.pop("PNR_PAIR_JOINT_FAIR", None)
            result = pair_plan(b, pair, r, o, (0, 0, 20, 20), 0.15)
        return seen, result

    def test_default_first_hand_takes_whole_joint_window(self):
        seen, result = self.schedule(False)
        joint = [s for s in seen if s[1] == "bridge_join_via"]
        self.assertEqual(len(joint), 1)
        self.assertEqual(joint[0][0], 1)
        self.assertAlmostEqual(joint[0][2], 60.0)
        self.assertNotIn("pair_engine_flags", result)

    def test_fair_split_gives_each_hand_half_of_a_starved_window(self):
        seen, result = self.schedule(True)
        joint = [s for s in seen if s[1] == "bridge_join_via"]
        self.assertEqual([(h, round(d, 6)) for h, _, d in joint], [(1, 30.0), (-1, 30.0)])
        self.assertEqual(
            sum(a["status"] == "joint_phase_budget" for a in result["order_attempts"]), 4
        )
        self.assertTrue(
            any(t == "declared" for _, t, _ in seen)
        )  # legacy fallback reserve untouched
        self.assertEqual(result["pair_engine_flags"], {"PNR_PAIR_JOINT_FAIR": True})
        self.assertEqual(
            [round(a["budget_seconds"], 6) for a in result["order_attempts"][:2]], [30.0, 30.0]
        )

    def test_fair_split_is_not_a_cap_when_the_first_hand_finishes_early(self):
        b = board()
        clock = [0.0]
        r = power_rules()
        o = Oracle(b, r, deadline=300.0)
        seen = []
        for label, pt in {
            "J.B+": (1.5, 0),
            "J.A+": (0.5, 0),
            "J.B-": (0, 0),
            "J.A-": (1, 0),
        }.items():
            ref, num = label.rsplit(".", 1)
            pad(b, ref, num, "p" if "+" in num else "n", pt, (0.2, 0.2))
        pair = dict(
            p="p",
            n="n",
            gap_mm=0.15,
            skew_mm=0.3,
            terminal_chain=[{"p": "J.A+", "n": "J.A-"}, {"p": "U.+", "n": "U.-"}],
            auxiliary_pairs=[
                dict(
                    source={"p": "J.B+", "n": "J.B-"},
                    target={"p": "J.A+", "n": "J.A-"},
                    max_length_mm=3,
                )
            ],
        )

        def attempt(b, p, r, trial, bounds, pitch, order, topology=None):
            seen.append(((topology or {}).get("bridge_hand"), trial.deadline - clock[0]))
            clock[0] += 10
            return dict(status="no_route")

        env = {
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
            "PNR_PAIR_JOINT_TRIAL_SECONDS": "240",
            "PNR_PAIR_JOINT_MAX_TRIALS": "6",
            "PNR_PAIR_AUXILIARY_SCOPE": "separate",
            "PNR_PAIR_JOINT_FAIR": "1",
        }
        with patch.dict(os.environ, env), patch(
            "pnr.native_electrical.time.monotonic", side_effect=lambda: clock[0]
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            pair_plan(b, pair, r, o, (0, 0, 20, 20), 0.15)
        # window 270 s: hand +1 first gets 135, hand -1 first gets all 260 left
        # (<=240 trial cap), later joint configs keep the unchanged trial cap.
        self.assertEqual(
            [round(d, 6) for _, d in seen[:6]], [135.0, 240.0, 240.0, 240.0, 230.0, 220.0]
        )

    def test_reserve_knob_widens_the_window_that_fair_splits(self):
        b = board()
        clock = [100.0]
        r = power_rules()
        o = Oracle(b, r, deadline=190.0)
        seen = []
        for label, pt in {
            "J.B+": (1.5, 0),
            "J.A+": (0.5, 0),
            "J.B-": (0, 0),
            "J.A-": (1, 0),
        }.items():
            ref, num = label.rsplit(".", 1)
            pad(b, ref, num, "p" if "+" in num else "n", pt, (0.2, 0.2))
        pair = dict(
            p="p",
            n="n",
            gap_mm=0.15,
            skew_mm=0.3,
            terminal_chain=[{"p": "J.A+", "n": "J.A-"}, {"p": "U.+", "n": "U.-"}],
            auxiliary_pairs=[
                dict(
                    source={"p": "J.B+", "n": "J.B-"},
                    target={"p": "J.A+", "n": "J.A-"},
                    max_length_mm=3,
                )
            ],
        )

        def attempt(b, p, r, trial, bounds, pitch, order, topology=None):
            seen.append(((topology or {}).get("takeoff"), trial.deadline - clock[0]))
            clock[0] = trial.deadline
            return dict(status="time_budget")

        env = {
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
            "PNR_PAIR_JOINT_TRIAL_SECONDS": "240",
            "PNR_PAIR_JOINT_MAX_TRIALS": "6",
            "PNR_PAIR_AUXILIARY_SCOPE": "separate",
            "PNR_PAIR_JOINT_FAIR": "1",
            "PNR_PAIR_FALLBACK_RESERVE_SECONDS": "6",
        }
        with patch.dict(os.environ, env), patch(
            "pnr.native_electrical.time.monotonic", side_effect=lambda: clock[0]
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            result = pair_plan(b, pair, r, o, (0, 0, 20, 20), 0.15)
        self.assertEqual([round(d, 6) for t, d in seen[:2]], [42.0, 42.0])  # (90-6)/2 each
        self.assertAlmostEqual(sum(d for t, d in seen if t != "bridge_join_via"), 6.0)
        self.assertEqual(
            result["pair_engine_flags"],
            {"PNR_PAIR_JOINT_FAIR": True, "PNR_PAIR_FALLBACK_RESERVE_SECONDS": 6.0},
        )
        with patch.dict(os.environ, {"PNR_PAIR_FALLBACK_RESERVE_SECONDS": "-1"}):
            with self.assertRaises(ValueError):
                pair_plan(b, pair, r, Oracle(b, r), (0, 0, 20, 20), 0.15)


class PreferInlineScoreTest(unittest.TestCase):
    def choose(self, prefer):
        from pnr.native_electrical import stub_legs

        b = board()
        r = power_rules()
        o = Oracle(b, r, deadline=time.monotonic() + 30)
        bridge = dict(
            bridge_target=dict(sites={}), fanout_lengths=[{"p": 0, "n": 0}, {"p": 1, "n": 1}]
        )
        joint = dict(
            bridge_target=dict(sites={}), fanout_lengths=[{"p": 0, "n": 0}, {"p": 1, "n": 1}]
        )
        inline = [joint, dict(paths={})]
        stub = [joint, dict(paths={}, post_bridge_start={})]
        reuse = [joint, dict(bridge)]
        self.assertEqual([stub_legs(x) for x in (inline, stub, reuse)], [0, 1, 1])
        routes = iter(
            [("stub", stub, 4, 1.0), ("inline", inline, 4, 1.5)]
        )  # two legacy order configs

        def attempt(b, pair, r, trial, bounds, pitch, order, topology=None):
            name, segments, vias, length = next(routes, (None, None, 0, 0))
            if name is None:
                return dict(status="no_route")
            return dict(
                status="routed",
                name=name,
                segments=segments,
                pair_vias=[("p", (0, 0))] * vias,
                pair_tracks=[("p", F, (0, 0), (length, 0), 0.2)],
            )

        env = {"PNR_PAIR_PREFER_INLINE": "1"} if prefer else {}
        with patch.dict(os.environ, env), patch(
            "pnr.native_electrical._pair_plan_order", side_effect=attempt
        ):
            if not prefer:
                os.environ.pop("PNR_PAIR_PREFER_INLINE", None)
            return pair_plan(b, {"auxiliary_pairs": [{}]}, r, o, (0, 0, 20, 20), 0.2)

    def test_default_keeps_shortest_equal_via_route(self):
        self.assertEqual(self.choose(False)["name"], "stub")

    def test_flag_prefers_inline_intermediate_device_on_equal_vias(self):
        result = self.choose(True)
        self.assertEqual(result["name"], "inline")
        self.assertEqual(result["pair_engine_flags"], {"PNR_PAIR_PREFER_INLINE": True})


class HandSwapTrialTest(unittest.TestCase):
    def test_schedule_inserts_one_unmoved_hand_minus_trial_only_when_enabled(self):
        from pnr.paired_bootstrap import trial_env, trial_schedule

        poses = [None, {"ref": "D2", "rotation": 270}, {"ref": "D2", "rotation": 0}]
        with patch.dict(os.environ, {"PNR_PAIR_JOINT_TOPOLOGIES": "1"}):
            os.environ.pop("PNR_PAIR_HAND_SWAP_TRIAL", None)
            self.assertEqual(trial_schedule(poses), [(p, None) for p in poses])
        with patch.dict(
            os.environ, {"PNR_PAIR_JOINT_TOPOLOGIES": "1", "PNR_PAIR_HAND_SWAP_TRIAL": "1"}
        ):
            self.assertEqual(
                trial_schedule(poses),
                [(None, None), (None, -1), (poses[1], None), (poses[2], None)],
            )
            self.assertEqual(trial_schedule([None]), [(None, None), (None, -1)])
        with patch.dict(
            os.environ, {"PNR_PAIR_JOINT_TOPOLOGIES": "0", "PNR_PAIR_HAND_SWAP_TRIAL": "1"}
        ):
            self.assertEqual(trial_schedule(poses), [(p, None) for p in poses])
        self.assertEqual(trial_env({"A": "1", "PNR_PAIR_JOINT_HAND_FIRST": "1"}, None), {"A": "1"})
        self.assertEqual(trial_env({"A": "1"}, -1), {"A": "1", "PNR_PAIR_JOINT_HAND_FIRST": "-1"})

    def test_hand_first_reorders_within_each_seed_and_gets_the_whole_window(self):
        from pnr.native_electrical import hand_first_order
        from pnr.pair_joint import joint_topologies

        positions = {"J.B+": (1.5, 0), "J.A+": (0.5, 0), "J.B-": (0, 0), "J.A-": (1, 0)}
        pair = dict(
            p="p",
            n="n",
            gap_mm=0.15,
            skew_mm=0.3,
            terminal_chain=[{"p": "J.A+", "n": "J.A-"}, {"p": "U.+", "n": "U.-"}],
            auxiliary_pairs=[
                dict(
                    source={"p": "J.B+", "n": "J.B-"},
                    target={"p": "J.A+", "n": "J.A-"},
                    max_length_mm=3,
                )
            ],
        )
        joint = joint_topologies(pair, positions)
        self.assertEqual([c["bridge_hand"] for c in joint], [1, -1] * 3)
        self.assertIs(hand_first_order(joint, None), joint)
        swapped = hand_first_order(joint, "-1")
        self.assertEqual(
            [(c["bridge_hand"], c["prefix_timing_target_mm"]) for c in swapped],
            [(-1, 0.3), (1, 0.3), (-1, -0.3), (1, -0.3), (-1, 0.0), (1, 0.0)],
        )
        with self.assertRaises(ValueError):
            hand_first_order(joint, "2")
        b = board()
        for label, pt in positions.items():
            ref, num = label.rsplit(".", 1)
            pad(b, ref, num, "p" if "+" in num else "n", pt, (0.2, 0.2))
        clock = [100.0]
        r = power_rules()
        o = Oracle(b, r, deadline=190.0)
        seen = []

        def attempt(b, p, r, trial, bounds, pitch, order, topology=None):
            seen.append(((topology or {}).get("bridge_hand"), trial.deadline - clock[0]))
            clock[0] = trial.deadline
            return dict(status="time_budget")

        env = {
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
            "PNR_PAIR_JOINT_TRIAL_SECONDS": "240",
            "PNR_PAIR_JOINT_MAX_TRIALS": "6",
            "PNR_PAIR_AUXILIARY_SCOPE": "separate",
            "PNR_PAIR_JOINT_HAND_FIRST": "-1",
        }
        with patch.dict(os.environ, env), patch(
            "pnr.native_electrical.time.monotonic", side_effect=lambda: clock[0]
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            os.environ.pop("PNR_PAIR_JOINT_FAIR", None)
            result = pair_plan(b, pair, r, o, (0, 0, 20, 20), 0.15)
        self.assertEqual(seen[0], (-1, 60.0))
        self.assertEqual(result["pair_engine_flags"], {"PNR_PAIR_JOINT_HAND_FIRST": -1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
