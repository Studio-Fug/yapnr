"""pnr.hier.assemble's E2 additions through KiCad (KiCad Python only, by hand:
``python3 -m unittest tests.test_hier_assemble_kicad``): ``--zones`` clones a
block's copper pours and rule areas by the same rigid transform as its tracks
and vias; ``--group``/``--anchor`` (the hier -> fixed_block bridge) puts the
assembled items and the block's non-anchor footprints into one new KiCad group
and reports its ``pnr.fixed_copper.block_digest``; neither flag changes the
default (undeclared) output.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

NATIVE = importlib.util.find_spec("pcbnew") is not None
OFFSET = 30.0
SIZE = (30.0, 20.0)


def _save(b, folder, name):
    import pcbnew as k

    path = Path(folder) / name
    k.SaveBoard(str(path), b)
    return path


def _block_board(rotate=0.0, shift=(0.0, 0.0)):
    """A 4-layer block board (S P P S): anchor U1 and member ANT, a CLK track
    between them, a GND zone on In1.Cu and a rule area barring tracks on B.Cu.
    ``rotate``/``shift`` pose the whole block (and its shared footprints) as it
    would sit on a routed block board, distinct from where it lands on ``full``."""
    import pcbnew as k
    from test_stack_kicad import board, smd

    b, nets = board("SPPS", size=SIZE, offset=OFFSET)
    nets["CLK"] = k.NETINFO_ITEM(b, "CLK")
    b.Add(nets["CLK"])
    u1 = smd(
        b, "U1", (OFFSET + 10.0, OFFSET + 10.0), [("1", "CLK", (-0.8, 0)), ("2", "GND", (0.8, 0))]
    )
    ant = smd(b, "ANT", (OFFSET + 21.0, OFFSET + 10.0), [("1", "CLK", (0, 0))], size=(0.6, 0.6))
    t = k.PCB_TRACK(b)
    t.SetStart(k.VECTOR2I(round((OFFSET + 10.8) * 1e6), round((OFFSET + 10.0) * 1e6)))
    t.SetEnd(k.VECTOR2I(round((OFFSET + 20.7) * 1e6), round((OFFSET + 10.0) * 1e6)))
    t.SetWidth(round(0.2 * 1e6))
    t.SetLayer(k.F_Cu)
    t.SetNet(nets["CLK"])
    b.Add(t)
    zone = k.ZONE(b)
    zone.SetLayer(b.GetLayerID("In1.Cu"))
    zone.SetNet(nets["GND"])
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((5, 5), (25, 5), (25, 15), (5, 15)):
        outline.Append(k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + y) * 1e6)))
    b.Add(zone)
    area = k.ZONE(b)
    area.SetIsRuleArea(True)
    lset = k.LSET()
    lset.AddLayer(k.B_Cu)
    area.SetLayerSet(lset)
    area.SetDoNotAllowTracks(True)
    outline = area.Outline()
    outline.NewOutline()
    for x, y in ((6, 6), (14, 6), (14, 14), (6, 14)):
        outline.Append(k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + y) * 1e6)))
    b.Add(area)
    if rotate or shift != (0.0, 0.0):
        centre = u1.GetPosition()
        for item in (u1, ant, t, zone, area):
            if rotate:
                item.Rotate(centre, k.EDA_ANGLE(rotate, k.DEGREES_T))
            if shift != (0.0, 0.0):
                item.Move(k.VECTOR2I(round(shift[0] * 1e6), round(-shift[1] * 1e6)))
    return b, nets


def _full_board():
    """The top board, after placement: U1 and ANT posed (rotated 90deg, shifted
    from where the block board drew them), as the macro's expand would leave
    them -- footprints only, no copper of its own yet."""
    import pcbnew as k
    from test_stack_kicad import board, smd

    b, nets = board("SPPS", size=SIZE, offset=OFFSET)
    nets["CLK"] = k.NETINFO_ITEM(b, "CLK")
    b.Add(nets["CLK"])
    u1 = smd(
        b, "U1", (OFFSET + 10.0, OFFSET + 10.0), [("1", "CLK", (-0.8, 0)), ("2", "GND", (0.8, 0))]
    )
    ant = smd(b, "ANT", (OFFSET + 21.0, OFFSET + 10.0), [("1", "CLK", (0, 0))], size=(0.6, 0.6))
    centre, rotate, shift = u1.GetPosition(), 90.0, (5.0, -3.0)
    for fp in (u1, ant):
        fp.Rotate(centre, k.EDA_ANGLE(rotate, k.DEGREES_T))
        fp.Move(k.VECTOR2I(round(shift[0] * 1e6), round(-shift[1] * 1e6)))
    return b


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class AssembleZonesAndGroup(unittest.TestCase):
    def _run(self, full_path, out_path, *extra):
        import os

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pnr.hier.assemble",
                str(full_path),
                "--block",
                str(self.block_path),
                "--out",
                str(out_path),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        block, _ = _block_board()
        self.block_path = _save(block, self.tmp.name, "block.kicad_pcb")
        self.full_path = _save(_full_board(), self.tmp.name, "full.kicad_pcb")

    def test_default_run_still_drops_zones_and_makes_no_group(self):
        import pcbnew as k

        out = Path(self.tmp.name) / "out.kicad_pcb"
        report = self._run(self.full_path, out)
        self.assertEqual(sorted(report[0]), ["block", "footprints", "items", "rigid_error_nm"])
        b = k.LoadBoard(str(out))
        self.assertEqual(list(b.Zones()), [])
        self.assertEqual(list(b.Groups()), [])
        self.assertEqual(len(list(b.GetTracks())), 1)  # the CLK track only

    def test_zones_opt_in_clones_the_pour_and_the_rule_area(self):
        import pcbnew as k

        out = Path(self.tmp.name) / "out-zones.kicad_pcb"
        report = self._run(self.full_path, out, "--zones")
        self.assertEqual(report[0]["zones"], 2)
        b = k.LoadBoard(str(out))
        zones = list(b.Zones())
        self.assertEqual(len(zones), 2)
        pour = next(z for z in zones if not z.GetIsRuleArea())
        self.assertEqual(pour.GetNetname(), "GND")
        self.assertTrue(pour.IsOnLayer(b.GetLayerID("In1.Cu")))
        area = next(z for z in zones if z.GetIsRuleArea())
        self.assertTrue(area.GetDoNotAllowTracks())
        self.assertTrue(area.IsOnLayer(b.GetLayerID("B.Cu")))

    def test_group_bridges_to_a_fixed_block_with_a_digest(self):
        import pcbnew as k

        from pnr.fixed_copper import block_digest

        out = Path(self.tmp.name) / "out-group.kicad_pcb"
        report = self._run(
            self.full_path, out, "--zones", "--group", "POWER_STAGE", "--anchor", "U1"
        )
        self.assertEqual(
            (report["group"], report["anchor"], report["refs"]), ("POWER_STAGE", "U1", ["ANT"])
        )
        self.assertEqual(len(report["sha256"]), 64)
        b = k.LoadBoard(str(out))
        (group,) = [g for g in b.Groups() if g.GetName() == "POWER_STAGE"]
        from pnr.writeback import group_name

        members = {fp.GetReference() for fp in b.GetFootprints() if group_name(fp) == "POWER_STAGE"}
        self.assertEqual(members, {"ANT"})  # the anchor U1 is not a member
        self.assertEqual(
            sorted(group_name(t) for t in b.GetTracks()),
            ["POWER_STAGE"],
        )
        self.assertEqual(report["sha256"], block_digest(b, "POWER_STAGE", "U1"))

    def _run_expect_failure(self, full_path, out_path, *extra):
        import os

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pnr.hier.assemble",
                str(full_path),
                "--block",
                str(self.block_path),
                "--out",
                str(out_path),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_group_refuses_a_name_already_on_the_board(self):
        import pcbnew as k

        out = Path(self.tmp.name) / "out-dup.kicad_pcb"
        full = k.LoadBoard(str(self.full_path))
        existing = k.PCB_GROUP(full)
        existing.SetName("POWER_STAGE")
        full.Add(existing)
        dup_full = _save(full, self.tmp.name, "full-dup.kicad_pcb")
        proc = self._run_expect_failure(dup_full, out, "--group", "POWER_STAGE")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already exists", proc.stderr)

    def test_anchor_without_group_is_refused(self):
        out = Path(self.tmp.name) / "out-bad.kicad_pcb"
        proc = self._run_expect_failure(self.full_path, out, "--anchor", "U1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--anchor needs --group", proc.stderr)


if __name__ == "__main__":
    unittest.main()
