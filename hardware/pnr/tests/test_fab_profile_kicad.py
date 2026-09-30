"""kicad-cli enforces the generated fab-profile custom rules (KiCad Python only).

A probe board places one deliberate near-miss per rule plus passing controls,
then native DRC (which writes <board>.kicad_dru) must report exactly the misses.
"""

import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CLI = os.environ.get("KICAD_CLI") or os.environ.get(
    "PNR_KICAD_CLI", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
)  # PNR_KICAD_CLI (src15)
NATIVE = importlib.util.find_spec("pcbnew") is not None and Path(CLI).exists()


def build_probe(path):
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    mm = lambda v: round(v * 1e6)
    V = lambda x, y: k.VECTOR2I(mm(x), mm(y))
    nets = {}

    def net(n):
        if n not in nets:
            nets[n] = k.NETINFO_ITEM(b, n)
            b.Add(nets[n])
        return nets[n].GetNetCode()

    for a, z in [((0, 0), (30, 0)), ((30, 0), (30, 20)), ((30, 20), (0, 20)), ((0, 20), (0, 0))]:
        s = k.PCB_SHAPE(b)
        s.SetShape(k.SHAPE_T_SEGMENT)
        s.SetStart(V(*a))
        s.SetEnd(V(*z))
        s.SetLayer(k.Edge_Cuts)
        s.SetWidth(mm(0.15))
        b.Add(s)

    def via(x, y, n, d=0.45, h=0.30):
        v = k.PCB_VIA(b)
        v.SetPosition(V(x, y))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        v.SetFrontWidth(mm(d))
        v.SetDrill(mm(h))
        v.SetNetCode(net(n))
        b.Add(v)

    def track(a, z, n, w=0.2):
        t = k.PCB_TRACK(b)
        t.SetStart(V(*a))
        t.SetEnd(V(*z))
        t.SetWidth(mm(w))
        t.SetLayer(k.F_Cu)
        t.SetNetCode(net(n))
        b.Add(t)

    def footprint(ref, x, y):
        f = k.FOOTPRINT(b)
        f.SetReference(ref)
        f.SetPosition(V(x, y))
        b.Add(f)
        return f

    def pad(f, num, x, y, kind, size, drill=0, n=None, h=None):
        p = k.PAD(f)
        p.SetNumber(num)
        attr, mask, shape = {
            "pth": (k.PAD_ATTRIB_PTH, k.PAD.PTHMask(), k.PAD_SHAPE_CIRCLE),
            "npth": (k.PAD_ATTRIB_NPTH, k.PAD.UnplatedHoleMask(), k.PAD_SHAPE_CIRCLE),
            "smd": (k.PAD_ATTRIB_SMD, k.PAD.SMDMask(), k.PAD_SHAPE_RECT),
        }[kind]
        p.SetAttribute(attr)
        p.SetShape(shape)
        p.SetSize(k.VECTOR2I(mm(size), mm(h or size)))
        if drill:
            p.SetDrillSize(k.VECTOR2I(mm(drill), mm(drill)))
        p.SetLayerSet(mask)
        f.Add(p)
        p.SetPosition(V(x, y))
        if n:
            p.SetNetCode(net(n))

    # Misses (one per custom rule) ...
    via(0.575, 2, "e1")  # hole 0.425 from the outline centreline (< 0.50), copper 0.35 (>= 0.30)
    via(5, 2, "s2")
    via(5.52, 2, "s2")  # via drills 0.22 apart (< 0.25)
    f = footprint("J1", 10, 5)
    pad(f, "1", 10, 5, "pth", 1.2, 1.0, "p3")
    track((9, 5.9), (11, 5.9), "t3")  # 0.30 from the PTH drill wall (< 0.35), 0.20 from its land
    f = footprint("H1", 15, 5)
    pad(f, "", 15, 5, "npth", 0.6, 0.6)
    via(15.85, 5, "v4")  # 0.40 to an NPTH drill (< 0.45)
    f = footprint("J2", 20, 5)
    pad(f, "1", 20, 5, "pth", 1.2, 1.0, "p5")
    via(21.12, 5, "v5")  # 0.47 to a component PTH drill (< 0.50)
    f = footprint("U1", 5, 10)
    pad(f, "1", 5, 10, "smd", 0.3, n="a6", h=0.8)
    pad(f, "2", 5.44, 10, "smd", 0.3, n="b6", h=0.8)
    f = footprint("H2", 20, 10)
    pad(f, "", 20, 10, "npth", 0.4, 0.4)  # NPTH drill 0.40 (< 0.50)
    # A footprint's 0.20/0.45 thermal via (via class: under 5A Component PTH hole
    # 0.30) 0.40 from an NPTH drill (< 0.45 filled via to NPTH) ...
    f = footprint("U3", 14.2, 5)
    pad(f, "9", 14.2, 5, "pth", 0.45, 0.20, "q9")
    # ... and a 0.30/0.50 plated pad (0.30 is a component PTH hole) 0.30 from a track.
    f = footprint("U4", 12, 15)
    pad(f, "9", 12, 15, "pth", 0.50, 0.30, "q10")
    track((11, 15.55), (13, 15.55), "t10")
    # ... and controls that must pass.
    f = footprint("U2", 8, 10)
    pad(f, "1", 8, 10, "smd", 0.3, n="a7", h=0.8)
    pad(f, "2", 8.46, 10, "smd", 0.3, n="b7", h=0.8)
    track((12, 10), (14, 10), "a8")
    track((12, 10.33), (14, 10.33), "b8")  # 0.13 track gap
    f = footprint("J3", 25, 10)
    pad(f, "1", 25, 10, "pth", 1.2, 1.0, "p13")
    track((25, 10), (28, 10), "p13")
    via(25, 15, "r14", 0.35, 0.20)  # in-pad via class: 0.075 ring
    via(29.3, 18, "e16")  # hole 0.55 from the outline centreline
    # Via-class footprint hole: via rules, not PTH ones. A foreign track 0.30 from
    # its drill wall (>= 0.20, < 0.35) and a via drill 0.36 away (>= 0.25, < 0.50).
    f = footprint("U5", 5, 15)
    pad(f, "24", 5, 15, "pth", 0.45, 0.20, "q17")
    track((4, 15.5), (6, 15.5), "u17")
    via(5.61, 15, "v17")
    # 5B filled via-in-pad: a 0.20/0.35 via centred in its own 0.38 x 1.43 land
    # passes; a default 0.30/0.45 via inside its own SMD pad breaks 5A "via copper
    # to SMD pad 0.127" (only the 0.20-drill in-pad class may enter a pad).
    f = footprint("U6", 15, 15)
    pad(f, "25", 15, 15, "smd", 0.38, n="w18", h=1.43)
    via(15, 15, "w18", 0.35, 0.20)
    f = footprint("U7", 18, 15)
    pad(f, "1", 18, 15, "smd", 1.0, n="w19", h=1.0)
    via(18, 15, "w19")
    k.SaveBoard(str(path), b)


def build_zone_board(path):
    """An In1.Cu pour on net a around a component PTH (0.30 drill, 0.20 ring) on net b.

    The netclass clearance (0.127) from the land leaves the drill wall 0.327 from
    the pour, so only the custom 0.35 PTH rule pushes the fill back.
    """
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    mm = lambda v: round(v * 1e6)
    V = lambda x, y: k.VECTOR2I(mm(x), mm(y))
    nets = {}
    for n in ("a", "b"):
        nets[n] = k.NETINFO_ITEM(b, n)
        b.Add(nets[n])
    for s0, s1 in [((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (0, 20)), ((0, 20), (0, 0))]:
        s = k.PCB_SHAPE(b)
        s.SetShape(k.SHAPE_T_SEGMENT)
        s.SetStart(V(*s0))
        s.SetEnd(V(*s1))
        s.SetLayer(k.Edge_Cuts)
        s.SetWidth(mm(0.15))
        b.Add(s)
    f = k.FOOTPRINT(b)
    f.SetReference("J1")
    f.SetPosition(V(10, 10))
    b.Add(f)
    p = k.PAD(f)
    p.SetNumber("1")
    p.SetAttribute(k.PAD_ATTRIB_PTH)
    p.SetShape(k.PAD_SHAPE_CIRCLE)
    p.SetSize(k.VECTOR2I(mm(0.7), mm(0.7)))
    p.SetDrillSize(k.VECTOR2I(mm(0.3), mm(0.3)))
    p.SetLayerSet(k.PAD.PTHMask())
    f.Add(p)
    p.SetPosition(V(10, 10))
    p.SetNetCode(nets["b"].GetNetCode())
    z = k.ZONE(b)
    z.SetLayer(k.In1_Cu)
    z.SetNetCode(nets["a"].GetNetCode())
    z.SetLocalClearance(mm(0.127))  # the engine's pours use the copper clearance
    outline = z.Outline()
    outline.NewOutline()
    for x, y in [(2, 2), (18, 2), (18, 18), (2, 18)]:
        outline.Append(mm(x), mm(y))
    b.Add(z)
    k.SaveBoard(str(path), b)


@unittest.skipUnless(NATIVE, "requires KiCad Python and kicad-cli")
class ZoneRefillTest(unittest.TestCase):
    """A refill must apply the custom rules even where the board was written
    without its .kicad_dru (pnr.merge_additive / trial folders): KiCad reads the
    rules when the board is loaded, so pnr.planes writes them first.

    Fills run in a fresh KiCad Python process, as in the pipeline (an in-process
    LoadBoard can reuse the project settings this process already holds)."""

    PLAIN = (
        "import sys, pcbnew; b = pcbnew.LoadBoard(sys.argv[1]); "
        "pcbnew.ZONE_FILLER(b).Fill(b.Zones()); pcbnew.SaveBoard(sys.argv[1], b)"
    )

    def pth_findings(self, folder, command):
        import subprocess
        import sys
        from pnr import fab_profile as fp
        from pnr.native_drc import run_drc
        from pnr.writeback import patch_project_rules

        folder.mkdir()
        board = folder / "merged.kicad_pcb"
        build_zone_board(board)
        rules = fp.apply_rules({"fab": dict(fp.LEGACY_FAB), "net_classes": [], "diff_pairs": []})
        patch_project_rules(str(board.with_suffix(".kicad_pro")), rules)
        fp.dru_path(board).unlink()  # as merge_additive left it before the fix
        (folder / "rules.json").write_text(json.dumps(rules))
        root = str(Path(__file__).resolve().parents[1])
        subprocess.run(
            [sys.executable] + command(board, folder / "rules.json"),
            check=True,
            capture_output=True,
            timeout=120,
            env=dict(os.environ, PYTHONPATH=root),
        )
        report = run_drc(CLI, board, folder / "merged.drc.json", timeout=120)
        return [
            v["description"]
            for v in report["violations"]
            if "pth_hole_clearance" in v["description"]
        ]

    def test_planes_refill_applies_the_pth_rule(self):
        from pnr import fab_profile as fp

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        with patch.dict(os.environ, {fp.ENV: "jlc-pofv"}):
            # Control: the pre-fix refill (board loaded before its rules exist).
            control = self.pth_findings(
                d / "plain", lambda board, rules: ["-c", self.PLAIN, str(board)]
            )
            fixed = self.pth_findings(
                d / "planes",
                lambda board, rules: [
                    "-m",
                    "pnr.planes",
                    str(board),
                    "--rules",
                    str(rules),
                    "--refill-only",
                ],
            )
        self.assertTrue(control, "the probe must reproduce the fill-without-rules failure")
        self.assertEqual(fixed, [])


@unittest.skipUnless(NATIVE, "requires KiCad Python and kicad-cli")
class KiCadEnforcementTest(unittest.TestCase):
    def drc(self, profile_name):
        import pcbnew
        from pnr import fab_profile as fp
        from pnr.native_drc import run_drc
        from pnr.writeback import patch_project_rules

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        board = d / "probe.kicad_pcb"
        build_probe(board)
        rules = {"fab": dict(fp.LEGACY_FAB), "net_classes": [], "diff_pairs": []}
        with patch.dict(os.environ, {fp.ENV: profile_name}):
            patch_project_rules(str(board.with_suffix(".kicad_pro")), rules)
            report = run_drc(CLI, board, d / "probe.drc.json", timeout=120)
        # Distinct item pairs per rule: KiCad reports a via-to-pad hole_to_hole
        # once from each side.
        pairs = {}
        for v in report["violations"]:
            m = re.search(r"rule '([^']+)'", v["description"])
            if m:
                pairs.setdefault(m.group(1), set()).add(
                    frozenset(i["description"] for i in v["items"])
                )
        found = {rule: len(p) for rule, p in pairs.items()}
        other = sorted(
            {v["type"] for v in report["violations"]}
            - {"via_dangling", "track_dangling", "silk_over_copper", "silk_overlap"}
        )
        return found, other, board

    def test_jlc_pofv_rules_are_enforced(self):
        found, other, board = self.drc("jlc-pofv")
        self.assertTrue(board.with_suffix(".kicad_dru").exists())
        expected = {
            "hole_to_edge",
            "via_hole_to_hole",
            "pth_hole_clearance",
            "filled_via_to_pad_hole",
            "component_pth_hole_to_hole",
            "smd_pad_to_pad",
            "npth_min_drill",
            "via_to_smd_pad",
        }
        self.assertEqual({k.split("_", 1)[1] for k in found}, expected)
        self.assertEqual(found["jlc-pofv_hole_to_edge"], 1)  # the 0.55 control passes
        self.assertEqual(found["jlc-pofv_smd_pad_to_pad"], 1)  # the 0.16 control passes
        # Same-net entry and the via-class footprint hole pass; the 0.30 pad does not.
        self.assertEqual(found["jlc-pofv_pth_hole_clearance"], 2)
        self.assertEqual(
            found["jlc-pofv_filled_via_to_pad_hole"], 2
        )  # via and via-class pad to NPTH
        self.assertEqual(
            found["jlc-pofv_via_hole_to_hole"], 1
        )  # the via-class pad to via 0.36 passes
        self.assertEqual(found["jlc-pofv_component_pth_hole_to_hole"], 1)
        self.assertEqual(found["jlc-pofv_via_to_smd_pad"], 1)  # the in-pad 0.20/0.35 via passes
        self.assertEqual(
            sorted(
                set(other) - {"hole_clearance", "hole_to_hole", "clearance", "drill_out_of_range"}
            ),
            [],
        )

    def test_legacy_reports_none_of_them(self):
        found, other, board = self.drc("legacy")
        self.assertFalse(board.with_suffix(".kicad_dru").exists())
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()
