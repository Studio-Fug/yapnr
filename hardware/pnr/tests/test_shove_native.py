"""PNR_SHOVE=1 native pieces (run under KiCad Python, headless).

* general in-pad attach sites on a non-analytic (L-shaped) custom land, and the
  flag-off identity of via_in_pad.attach_windows;
* the make-room World: a soft signal via is pushed clear of a claim by exactly the
  rule, a FIX obstacle is reported instead of moved, bank vias move rigidly;
* case-board integration (skipped when the converter case is absent): the forced
  in-pad escape of U5.13 and the whole-transaction gate rejections.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pcbnew as k

from pnr import fab_profile as fp
from pnr.electrical import compile_policy

V = lambda x, y: k.VECTOR2I(round(x * 1e6), round(y * 1e6))
FAB = dict(
    outer_copper_oz=1,
    inner_copper_oz=1,
    delta_t_c=40,
    via_drill_mm=0.3,
    via_diameter_mm=0.6,
    min_via_plating_um=20,
    board_thickness_mm=1.6,
    copper_resistivity_ohm_mm=2.1e-5,
    via_barrel_loss_budget_w=0.01,
    via_array_peak_drop_v=0.01,
    neck_loss_budget_w=0.01,
    neck_peak_drop_v=0.005,
)
CASE = Path(
    "<repo>/output/hier/" "blocks/nb3-pf/d5be6b6d0e99/native/board_converter-s1-35.75x27.75"
)
CLI = os.environ.get(
    "PNR_KICAD_CLI", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
)  # headless via PNR_KICAD_CLI (src15)
PNR_PYTHON = "<repo>/output/pnr-regression-runtime/bin/python"


def rules():
    base = compile_policy(
        dict(fab=dict(fp.LEGACY_FAB), net_classes=[], diff_pairs=[]),
        [
            dict(
                ref="U9",
                pads=["1"],
                net="sense",
                rms_current_a=0.01,
                peak_current_a=0.01,
                scope="terminal",
                source={},
            )
        ],
        FAB,
    )
    return fp.apply_rules(base, "jlc-pofv")


def board():
    b = k.BOARD()
    b.SetCopperLayerCount(4)
    for name in ("sense", "sig", "lv", "rail"):
        b.Add(k.NETINFO_ITEM(b, name))
    corners = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
    for a, z in zip(corners, corners[1:]):
        e = k.PCB_SHAPE(b)
        e.SetShape(k.SHAPE_T_SEGMENT)
        e.SetLayer(k.Edge_Cuts)
        e.SetStart(V(*a))
        e.SetEnd(V(*z))
        e.SetWidth(50000)
        b.Add(e)
    return b


def l_land(b, ref, num, net, pos):
    """U5.13-like L land: a 0.22 x 0.80 arm and a 0.60 x 0.40 foot (mm)."""
    f = k.FOOTPRINT(b)
    f.SetReference(ref)
    b.Add(f)
    f.SetPosition(V(*pos))
    p = k.PAD(f)
    p.SetNumber(num)
    p.SetAttribute(k.PAD_ATTRIB_SMD)
    ls = k.LSET()
    ls.AddLayer(k.F_Cu)
    p.SetLayerSet(ls)
    p.SetShape(k.PAD_SHAPE_CUSTOM)
    p.SetAnchorPadShape(k.F_Cu, k.PAD_SHAPE_RECT)
    p.SetSize(V(0.1, 0.1))
    poly = k.VECTOR_VECTOR2I()
    for x, y in [(0, 0), (0.22, 0), (0.22, 0.4), (0.6, 0.4), (0.6, 0.8), (0, 0.8)]:
        poly.append(V(x, y))
    p.AddPrimitivePoly(k.F_Cu, poly, 0, True)
    f.Add(p)
    p.SetPosition(V(*pos))
    p.SetNetCode(b.FindNet(net).GetNetCode())
    return p


def smd(b, ref, num, net, pos, size=(0.3, 0.3)):
    f = next((f for f in b.GetFootprints() if f.GetReference() == ref), None)
    if f is None:
        f = k.FOOTPRINT(b)
        f.SetReference(ref)
        b.Add(f)
        f.SetPosition(V(*pos))
    p = k.PAD(f)
    p.SetNumber(num)
    p.SetAttribute(k.PAD_ATTRIB_SMD)
    p.SetShape(k.PAD_SHAPE_RECT)
    ls = k.LSET()
    ls.AddLayer(k.F_Cu)
    p.SetLayerSet(ls)
    p.SetSize(V(*size))
    f.Add(p)
    p.SetPosition(V(*pos))
    p.SetNetCode(b.FindNet(net).GetNetCode())
    return p


def track(b, net, a, z, w=0.2, layer=k.F_Cu):
    t = k.PCB_TRACK(b)
    t.SetNetCode(b.FindNet(net).GetNetCode())
    t.SetLayer(layer)
    t.SetStart(V(*a))
    t.SetEnd(V(*z))
    t.SetWidth(round(w * 1e6))
    b.Add(t)
    return t


def via(b, net, p, d=0.45, h=0.3):
    v = k.PCB_VIA(b)
    v.SetNetCode(b.FindNet(net).GetNetCode())
    v.SetPosition(V(*p))
    v.SetViaType(k.VIATYPE_THROUGH)
    v.SetLayerPair(k.F_Cu, k.B_Cu)
    v.SetFrontWidth(round(d * 1e6))
    v.SetDrill(round(h * 1e6))
    b.Add(v)
    return v


class GeneralSitesTest(unittest.TestCase):
    def test_flag_off_keeps_no_window(self):
        from pnr.via_in_pad import attach_windows

        b = board()
        pad = l_land(b, "U9", "1", "sense", (5, 5))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PNR_SHOVE", None)
            self.assertEqual(attach_windows(b, pad, 1, fp.geometry(rules())), [])

    def test_sites_lie_on_the_foot_centreline(self):
        from pnr.via_in_pad import attach_windows, qualifies

        b = board()
        pad = l_land(b, "U9", "1", "sense", (5, 5))
        g = fp.geometry(rules())
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            windows = attach_windows(b, pad, 1, g)
        self.assertTrue(windows)
        for w in windows:
            (x, y), d, h = w["vias"][0]
            self.assertEqual(w["variant"], "general")
            self.assertTrue(qualifies(g, pad, (x, y), d, h))
            self.assertAlmostEqual(
                y, 5.6, delta=0.012
            )  # the 0.40 foot only fits a 0.35/0.20 on its centreline
            self.assertTrue(5.19 <= x <= 5.41, x)
        depths = [w["depth_mm"] for w in windows]
        self.assertEqual(depths, sorted(depths, reverse=True))


class SenseReservationTest(unittest.TestCase):
    def test_isolated_sense_leaf_keeps_its_in_pad_escape(self):
        from pnr.native_electrical import Oracle
        from pnr.shove.targets import reserve_sense_escapes

        b = board()
        l_land(b, "U9", "1", "sense", (5, 5))
        smd(b, "R9", "1", "sense", (10, 5.6))
        b.BuildConnectivity()
        r = rules()
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            oracle = Oracle(b, r)
            made = reserve_sense_escapes(b, r, oracle, "rail")
        self.assertEqual([m["pad"] for m in made], ["U9.1"])
        site, end = tuple(made[0]["via"]), tuple(made[0]["stub_end"])
        self.assertEqual(made[0]["layer"], "B.Cu")
        self.assertGreater(end[0], site[0])  # the stub points at the other land
        # A foreign route may no longer take the escape (via site or stub).
        self.assertFalse(oracle.via("rail", site, 0.45, 0.3))
        mid = ((site[0] + end[0]) / 2, (site[1] + end[1]) / 2)
        self.assertFalse(
            oracle.clear("rail", k.B_Cu, (mid[0], mid[1] - 1), (mid[0], mid[1] + 1), 0.5)
        )
        # The routing net itself is never reserved against, nor a connected leaf.
        self.assertEqual(reserve_sense_escapes(b, r, Oracle(b, r), "sense"), [])
        track(b, "sense", (5.1, 5.1), (5.1, 4.0))
        b.BuildConnectivity()
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            self.assertEqual(reserve_sense_escapes(b, r, Oracle(b, r), "rail"), [])


class WorldTest(unittest.TestCase):
    def world(self, b, plan, **kw):
        from pnr.shove.world import World

        b.BuildConnectivity()
        return World(b, rules(), "sense", plan, [(5, 5)], 2.5, **kw)

    def test_soft_via_is_pushed_by_the_rule(self):
        b = board()
        blocker = via(b, "sig", (5.9, 5.1))  # overlaps the claim by 0.33 mm, off its centreline
        track(b, "sig", (5.9, 5.1), (5.9, 8.0))
        for x in (4.0, 8.0):  # the claim's terminal lands: its attaches are FIX
            smd(b, "U9", str(x), "sense", (x, 5.0))
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (4.0, 5.0), (8.0, 5.0), 0.2)],
            banks=[],
        )
        w = self.world(b, claim, margin=0.002)
        self.assertEqual(w.fixed_claim_conflicts(), [])
        solved = w.solve()
        self.assertEqual(solved["final_violations"], [])
        moved = {m["uid"]: m for m in w.moved()}
        v = moved[blocker.m_Uuid.AsString()]
        # claim half width .1 + via radius .225 + clearance (.127 class + 1 um) + 2 um margin
        need = 0.1 + 0.225 + rules()["fab"]["clearance_mm"] + 0.001 + 0.002
        self.assertAlmostEqual(
            v["to"][1] - 5.0, need, delta=2e-4
        )  # pushed away, along its own side
        self.assertLess(v["disp_mm"], need + 1e-3)

    def test_fixed_obstacle_is_reported_not_moved(self):
        b = board()
        t = track(b, "sig", (5.9, 4.0), (5.9, 6.0))
        t.SetLocked(True)
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (4.0, 5.0), (8.0, 5.0), 0.2)],
            banks=[],
        )
        w = self.world(b, claim)
        conflicts = w.fixed_claim_conflicts()
        # The claim's interior vertices are free, but its end vertices outside the
        # soft radius are not: a locked crossing stays a certificate row.
        solved = w.solve()
        self.assertTrue(conflicts or solved["final_violations"])
        self.assertNotIn(t.m_Uuid.AsString(), {m["uid"] for m in w.moved()})

    def test_bank_moves_rigidly(self):
        b = board()
        v1, v2 = via(b, "rail", (6.0, 5.2)), via(b, "rail", (6.6, 5.2))  # hole edges 0.30 apart
        track(b, "rail", (6.3, 5.2), (6.0, 5.2), 0.5)
        track(b, "rail", (6.3, 5.2), (6.6, 5.2), 0.5)
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (4.0, 5.0), (8.0, 5.0), 0.2)],
            banks=[],
        )
        w = self.world(b, claim)
        solved = w.solve()
        self.assertEqual(solved["final_violations"], [])
        moved = {m["uid"]: m for m in w.moved()}
        d1 = [
            a - b_
            for a, b_ in zip(moved[v1.m_Uuid.AsString()]["to"], moved[v1.m_Uuid.AsString()]["from"])
        ]
        d2 = [
            a - b_
            for a, b_ in zip(moved[v2.m_Uuid.AsString()]["to"], moved[v2.m_Uuid.AsString()]["from"])
        ]
        self.assertAlmostEqual(d1[0], d2[0], places=6)
        self.assertAlmostEqual(d1[1], d2[1], places=6)


def power_rules():
    """'rail': a 3 A / 4 A net (net-scope L1.1), a full-current terminal U8.1 with a
    0.5 mm neck allowance, and a 10 mA sense terminal U9.2."""
    src = {}
    base = compile_policy(
        dict(fab=dict(fp.LEGACY_FAB), net_classes=[], diff_pairs=[]),
        [
            dict(
                ref="L1",
                pads=["1"],
                net="rail",
                rms_current_a=3,
                peak_current_a=4,
                scope="net",
                source=src,
            ),
            dict(
                ref="U8",
                pads=["1"],
                net="rail",
                rms_current_a=3,
                peak_current_a=4,
                scope="terminal",
                neck_max_length_mm=0.5,
                source=src,
            ),
            dict(
                ref="U9",
                pads=["2"],
                net="rail",
                rms_current_a=0.01,
                peak_current_a=0.01,
                scope="terminal",
                source=src,
            ),
        ],
        FAB,
    )
    return fp.apply_rules(base, "jlc-pofv")


def power_board():
    """U8.1 (0.4 mm land, full-current terminal) at (5, 5), U9.2 (sense) at (5, 10),
    L1.1 (net-scope carrier) at (10, 5), all on 'rail'."""
    b = board()
    smd(b, "U8", "1", "rail", (5, 5), (0.4, 0.4))
    smd(b, "U9", "2", "rail", (5, 10), (0.3, 0.3))
    smd(b, "L1", "1", "rail", (10, 5), (1.2, 1.2))
    return b


class NeckTest(unittest.TestCase):
    """G1: a source-bounded neck is rigid with its land (it can neither stretch
    nor turn), a full-width entry is not."""

    def test_neck_outer_vertex_is_bound_to_its_land(self):
        from pnr.shove.world import FIX, World

        b = power_board()
        r = power_rules()
        outer = r["electrical_nets"]["rail"]["outer_width_mm"]
        neck = track(b, "rail", (5, 5), (5.3, 5), 0.3)
        trunk = track(b, "rail", (5.3, 5), (8, 5), outer + 0.05)
        stub = track(b, "rail", (5, 5), (5, 3.8), outer + 0.05)
        b.BuildConnectivity()
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (3, 7), (8, 7), 0.2)],
            banks=[],
        )
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            w = World(b, r, "sig", claim, [(5, 6)], 2.5)
        uid = lambda t: t.m_Uuid.AsString()
        self.assertIn(uid(neck), w.rigid_necks)
        self.assertEqual(
            w.find(("T", uid(neck), 1)), FIX
        )  # the neck end (and the trunk vertex on it)
        self.assertEqual(w.find(("T", uid(trunk), 0)), FIX)
        self.assertNotIn(uid(stub), w.rigid_necks)  # full width: its free end may move
        self.assertNotEqual(w.find(("T", uid(stub), 1)), FIX)


class LengthRowTest(unittest.TestCase):
    def test_rows_hold_a_power_line_within_five_percent(self):
        from pnr.shove.world import World, line_length_limit

        b = power_board()
        line = track(b, "rail", (6, 3), (8, 3), 0.65)  # free ends, full width: not a neck
        b.BuildConnectivity()
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (3, 5.5), (9, 5.5), 0.2)],
            banks=[],
        )
        w = World(b, power_rules(), "sig", claim, [(7, 4)], 2.5)
        pm = next(pm for pm in w.power_lines() if pm["id"] == line.m_Uuid.AsString())
        rows, excess = w.length_rows(pm, [0.0] * (2 * len(w.roots)))
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(excess, -line_length_limit(2.0))
        end = w.var[w.find(pm["keys"][1])]

        def holds(dx):
            x = [0.0] * (2 * len(w.roots))
            x[2 * end] = dx
            return all(sum(a * x[j] for j, a in c.items()) >= rhs - 1e-12 for c, rhs, _ in rows)

        self.assertTrue(holds(0.05))  # +2.5 % of 2 mm
        self.assertFalse(holds(0.2))  # +10 %: beyond min(0.5 mm, 5 %)
        self.assertFalse(holds(-0.2))


class CourtyardSideTest(unittest.TestCase):
    def test_bottom_courtyards_are_rows_only_against_the_same_side(self):
        from pnr.shove.world import World

        b = board()

        def part(ref, x, flip):
            f = k.FOOTPRINT(b)
            f.SetReference(ref)
            b.Add(f)
            f.SetPosition(V(x, 5))
            s = k.PCB_SHAPE(f)
            s.SetShape(k.SHAPE_T_RECT)
            s.SetLayer(k.F_CrtYd)
            s.SetStart(V(x - 0.5, 4.5))
            s.SetEnd(V(x + 0.5, 5.5))
            s.SetWidth(50000)
            f.Add(s)
            if flip:
                f.Flip(f.GetPosition(), False)
            return f

        part("C1", 5, True)
        part("C2", 5.8, True)
        part("C3", 6.6, False)
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (3, 8), (8, 8), 0.2)],
            banks=[],
        )
        w = World(b, rules(), "sense", claim, [(5, 5)], 2.5, parts=("C1", "C2", "C3"))
        crt = {pm["id"]: pm for pm in w.prims if "CRT" in pm["layers"]}
        self.assertEqual(sorted(crt), ["crt:C1:B", "crt:C2:B", "crt:C3"])
        self.assertEqual(w.needs(crt["crt:C1:B"], crt["crt:C2:B"]), 0.0)
        self.assertIsNone(w.needs(crt["crt:C2:B"], crt["crt:C3"]))


class JustifiedSubwidthTest(unittest.TestCase):
    """gates.unjustified_subwidth judges each thin power track by the terminal it serves."""

    def judge(self, b, *tracks):
        from pnr.shove.gates import unjustified_subwidth

        b.BuildConnectivity()
        audit = dict(subwidth_tracks=[dict(uuid=t.m_Uuid.AsString()) for t in tracks])
        return set(unjustified_subwidth(b, power_rules(), audit))

    def test_source_bounded_neck_is_justified(self):
        b = power_board()
        neck = track(b, "rail", (5, 5), (5.3, 5), 0.3)
        track(b, "rail", (5.3, 5), (10, 5), 0.65)
        self.assertEqual(self.judge(b, neck), set())

    def test_stretched_neck_is_not(self):
        b = power_board()
        neck = track(b, "rail", (5, 5), (5.6, 5), 0.3)  # 0.6 mm > neck_max_length_mm 0.5
        track(b, "rail", (5.6, 5), (10, 5), 0.65)
        self.assertEqual(self.judge(b, neck), {neck.m_Uuid.AsString()})

    def test_thin_track_on_an_uncontracted_pad_is_not(self):
        b = power_board()
        thin = track(b, "rail", (10, 5), (10, 7), 0.3)  # merely touches the carrier land
        self.assertEqual(self.judge(b, thin), {thin.m_Uuid.AsString()})

    def test_sense_leaf_and_its_via_branch_are_justified(self):
        b = power_board()
        a = track(b, "rail", (5, 10), (5, 8), 0.2)  # U9.2's own leaf width
        v = via(b, "rail", (5, 8), 0.45, 0.3)
        c = track(b, "rail", (5, 8), (9, 8), 0.2, k.B_Cu)  # continues on B.Cu through the via
        self.assertEqual(self.judge(b, a, c), set())
        self.assertIsNotNone(v)

    def test_trunk_served_through_an_in_pad_via_under_its_collector(self):
        b = power_board()
        via(b, "rail", (5.05, 5), 0.3, 0.2)  # inside U8.1's land, not at a track end
        collector = track(b, "rail", (4.7, 5), (5.4, 5), 0.6, k.B_Cu)
        trunk = track(b, "rail", (5.4, 5), (9, 5), 0.6, k.B_Cu)
        self.assertEqual(self.judge(b, collector, trunk), set())


class ArrayRootTest(unittest.TestCase):
    def test_only_full_current_array_lands_root_a_branch(self):
        from pnr.electrical import net_policy, terminal_policy
        from pnr.native_electrical import array_roots

        b = power_board()
        r = power_rules()
        lands = {
            p.GetParentFootprint().GetReference(): p for f in b.GetFootprints() for p in f.Pads()
        }
        attached = {lands[ref].m_Uuid.AsString() for ref in ("U8", "U9")}
        trunk = net_policy("rail", r)
        leaf = terminal_policy("U9", ["2"], "rail", r)
        roots = array_roots(b, "rail", k.F_Cu, r, attached, set(), (trunk, leaf))
        self.assertEqual(
            [p.GetParentFootprint().GetReference() for p in roots], ["U8"]
        )  # never the sense leaf
        self.assertEqual(
            array_roots(b, "rail", k.F_Cu, r, attached, {lands["U8"].m_Uuid.AsString()}, (trunk,)),
            [],
        )


class PlacementFactsTest(unittest.TestCase):
    def test_locked_owned_and_cumulative_cap(self):
        from pnr.shove.placement import board_facts, poses

        before, after = power_board(), power_board()
        origin = poses(before)
        rules_ = dict(plane_access_intents=[dict(kind="power_array", ref="L1")])
        move = lambda b, ref, dx: b.FindFootprintByReference(ref).SetPosition(
            b.FindFootprintByReference(ref).GetPosition() + V(dx, 0)
        )
        move(after, "U9", 0.2)
        facts = board_facts(before, after, rules_, origin)
        self.assertEqual([m["ref"] for m in facts["moved"]], ["U9"])
        self.assertEqual(facts["problems"], [])
        # A later transaction may not add up past 0.5 mm from the ORIGIN pose.
        later = power_board()
        move(later, "U9", 0.55)
        self.assertEqual(
            [p["problem"] for p in board_facts(after, later, rules_, origin)["problems"]],
            ["cumulative_cap"],
        )
        owned = power_board()
        move(owned, "L1", 0.1)
        owned.FindFootprintByReference("U8").SetLocked(True)
        move(owned, "U8", 0.1)
        problems = sorted(
            p["problem"] for p in board_facts(before, owned, rules_, origin)["problems"]
        )
        self.assertEqual(problems, ["locked", "source_owned_array_or_keepout"])


@unittest.skipUnless(
    (CASE / "electrical/board.kicad_pcb").exists() and Path(CLI).exists(), "converter case absent"
)
class CaseTest(unittest.TestCase):
    """The converter block case (final board, case policy)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="shove-case-"))
        for name in ("board.kicad_pcb", "board.kicad_pro", "fp-lib-table"):
            shutil.copyfile(CASE / "electrical" / name, cls.tmp / name)
        cls.rules_path = CASE / "electrical/native-loop/policy/prepare.json"
        cls.rules = json.loads(cls.rules_path.read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_u513_forced_in_pad_escape(self):
        from pnr.fab_profile import load_board
        from pnr.native_electrical import Oracle, power_plan
        from pnr.pad_identity import resolve_pad

        b = load_board(self.tmp / "board.kicad_pcb")
        b.BuildConnectivity()
        source, target = resolve_pad(b, "U5.13"), resolve_pad(b, "R13.2")
        bounds = [48.7, 41.8, 57.8, 58.7]
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            oracle = Oracle(b, self.rules)
            first = power_plan(b, "p5v-hv", source, target, self.rules, oracle, bounds, 0.1)
            self.assertNotEqual(first["status"], "routed")  # every surface access is walled in
            self.assertTrue(oracle.__dict__.get("in_pad_skipped"))
            forced = power_plan(
                b,
                "p5v-hv",
                source,
                target,
                self.rules,
                Oracle(b, self.rules),
                bounds,
                0.1,
                _force_in_pad=True,
            )
        self.assertEqual(forced["status"], "routed")
        ((point, d, h),) = forced["in_pad_vias"]
        self.assertEqual((d, h), (0.35, 0.2))
        self.assertAlmostEqual(point[1], 45.025, delta=0.01)

    def gate(self, candidate, name):
        out = self.tmp / (name + ".json")
        env = dict(os.environ, PNR_SHOVE="1")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pnr.shove.gates",
                str(self.tmp / "board.kicad_pcb"),
                str(candidate),
                "--rules",
                str(self.rules_path),
                "--kicad-cli",
                CLI,
                "--out",
                str(out),
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        return json.loads(out.read_text())

    def edited(self, name, edit):
        from pnr.fab_profile import load_board

        b = load_board(self.tmp / "board.kicad_pcb")
        edit(b)
        path = self.tmp / name / "candidate.kicad_pcb"
        path.parent.mkdir()
        k.SaveBoard(str(path), b)
        shutil.copyfile(self.tmp / "board.kicad_pro", path.with_suffix(".kicad_pro"))
        shutil.copyfile(self.tmp / "fp-lib-table", path.parent / "fp-lib-table")
        return path

    def test_gate_rejects_identity(self):
        path = self.edited("same", lambda b: None)
        result = self.gate(path, "same")
        self.assertFalse(result["accepted"])  # opens must strictly fall
        self.assertEqual(result["new_violations"], [])

    def test_gate_rejects_bare_part_nudge(self):
        def nudge(b):
            f = b.FindFootprintByReference("C24")
            f.SetPosition(f.GetPosition() + V(0.5, 0))  # copper left behind

        result = self.gate(self.edited("nudge", nudge), "nudge")
        self.assertFalse(result["accepted"])
        self.assertTrue(
            result["new_violations"]
            or result["checks"]["lost_pad_entries"]
            or not result["checks"]["preserved"]
        )

    @unittest.skipUnless(Path(PNR_PYTHON).exists(), "PnR runtime absent")
    def test_placement_gate_rejects_a_nudge_out_of_the_hard_group(self):
        """The review scenario: C24 pushed outward from U5 by 0.45 mm stays inside the
        0.5 mm nudge cap but leaves the HARD 5 mm output_hf group: rejected."""
        from pnr.fab_profile import load_board
        from pnr.shove.placement import check

        def pushed(step):
            b = load_board(self.tmp / "board.kicad_pcb")
            u5 = b.FindFootprintByReference("U5").GetPosition()
            f = b.FindFootprintByReference("C24")
            p = f.GetPosition()
            d = (p - u5).EuclideanNorm() / 1e6
            f.SetPosition(p + V((p.x - u5.x) / 1e6 / d * step, (p.y - u5.y) / 1e6 / d * step))
            return b

        before = load_board(self.tmp / "board.kicad_pcb")
        out = check(
            before,
            pushed(0.45),
            self.rules,
            CASE / "constraints.yaml",
            PNR_PYTHON,
            self.tmp / "place-out",
        )
        self.assertFalse(out["accepted"])
        self.assertEqual(out["problems"], [])
        self.assertIn("C24", out["new_violations"].get("group_outside", []))
        inside = check(
            before,
            pushed(-0.1),
            self.rules,
            CASE / "constraints.yaml",
            PNR_PYTHON,
            self.tmp / "place-in",
        )
        self.assertTrue(inside["accepted"], inside)
        unverified = check(before, pushed(-0.1), self.rules, None, None, self.tmp / "place-none")
        self.assertFalse(unverified["accepted"])  # a nudge nobody can check is never accepted

    def test_gate_rejects_leftover_rule_area(self):
        def area(b):
            z = k.ZONE(b)
            z.SetIsRuleArea(True)
            z.SetZoneName("PNR shove:test")
            z.SetLayer(k.F_Cu)
            z.SetDoNotAllowTracks(True)
            outline = z.Outline()
            outline.NewOutline()
            for x, y in ((31, 31), (31.5, 31), (31.5, 31.5), (31, 31.5)):
                outline.Append(round(x * 1e6), round(y * 1e6))
            b.Add(z)

        result = self.gate(self.edited("area", area), "area")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["checks"]["leftover_rule_areas"], ["PNR shove:test"])


if __name__ == "__main__":
    unittest.main()
