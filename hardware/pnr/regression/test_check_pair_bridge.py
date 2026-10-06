"""The constraint checker's ``pair_bridge`` check: a diff pair's two series parts
(R1, R2) stay geometrically coupled across them (KiCad's Python only)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None


def bridge_board(path, *, r2_offset=(0.0, 2.5), r2_rot=0.0):
    """A 30 x 20 mm board: R1 at (10, 10), pads 1/2 at x -0.5/+0.5 mm; R2 at
    ``(10, 10) + r2_offset``, turned ``r2_rot``, same pad layout. Pad 1 is the
    "near" (pair) side, pad 2 the "far" side, as the inferred line group's members
    (one per diff_pair leg) would be laid out. KiCad y down."""
    import pcbnew as k

    b = k.BOARD()
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(k.VECTOR2I(0, 0))
    edge.SetEnd(k.VECTOR2I(30_000_000, 20_000_000))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(0)
    b.Add(edge)
    r1 = (10.0, 10.0)
    r2 = (r1[0] + r2_offset[0], r1[1] + r2_offset[1])
    for ref, (x, y), rot in (("R1", r1, 0.0), ("R2", r2, r2_rot)):
        fp = k.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(k.VECTOR2I(round(x * 1_000_000), round(y * 1_000_000)))
        for number, dx in (("1", -0.5), ("2", 0.5)):
            pad = k.PAD(fp)
            pad.SetNumber(number)
            pad.SetSize(k.VECTOR2I(400_000, 400_000))
            pad.SetFPRelativePosition(k.VECTOR2I(round(dx * 1_000_000), 0))
            fp.Add(pad)
        fp.SetOrientationDegrees(rot)
        b.Add(fp)
    k.SaveBoard(str(path), b)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class PairBridgeCheck(unittest.TestCase):
    def check(self, **board_kw):
        from check_constraints import Board, check_pair_bridge

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            bridge_board(path, **board_kw)
            spec = dict(
                id="pair-bridge",
                kind="pair_bridge",
                refs=["R1", "R2"],
                near_pad="1",
                far_pad="2",
            )
            return check_pair_bridge(Board(path), spec)

    def test_r2_translated_only_stays_coupled(self):
        # R2 is a pure translation of R1 (the inferred line group's own layout): the
        # near-pad and far-pad vectors between them are identical.
        ok, measured, _expected = self.check(r2_offset=(0.0, 2.5), r2_rot=0.0)
        self.assertTrue(ok)
        self.assertEqual(measured["mismatch_mm"], 0.0)
        self.assertTrue(measured["same_side"])

    def test_r2_rotated_relative_to_r1_is_not_coupled(self):
        # R2 turned 90 degrees from R1: its pads no longer run parallel, so the two
        # legs could not be routed as a constant-width corridor through both.
        ok, measured, _expected = self.check(r2_offset=(0.0, 2.5), r2_rot=90.0)
        self.assertFalse(ok)
        self.assertGreater(measured["mismatch_mm"], 0.5)

    def test_small_mismatch_within_tolerance_passes(self):
        ok, _measured, _expected = self.check(r2_offset=(0.02, 2.5), r2_rot=0.0)
        self.assertTrue(ok)

    def test_without_max_pitch_a_distant_but_parallel_pair_still_passes(self):
        # Parallel, matching legs are not by themselves "side by side": without
        # max_pitch_mm, a pair translated far apart (but still a pure translation)
        # is not caught -- the gap an owner review (2026-10-06) found.
        ok, measured, _expected = self.check(r2_offset=(0.0, 15.0), r2_rot=0.0)
        self.assertTrue(ok)
        self.assertEqual(measured["mismatch_mm"], 0.0)
        self.assertEqual(measured["pitch_mm"], 15.0)

    def test_max_pitch_mm_catches_the_same_distant_pair(self):
        spec = dict(
            id="pair-bridge",
            kind="pair_bridge",
            refs=["R1", "R2"],
            near_pad="1",
            far_pad="2",
            max_pitch_mm=6.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            from check_constraints import Board, check_pair_bridge

            path = Path(tmp) / "b.kicad_pcb"
            bridge_board(path, r2_offset=(0.0, 15.0), r2_rot=0.0)
            ok, measured, expected = check_pair_bridge(Board(path), spec)
        self.assertFalse(ok)
        self.assertEqual(measured["pitch_mm"], 15.0)
        self.assertEqual(expected["max_pitch_mm"], 6.0)

    def test_max_pitch_mm_still_passes_a_pair_within_it(self):
        spec = dict(
            id="pair-bridge",
            kind="pair_bridge",
            refs=["R1", "R2"],
            near_pad="1",
            far_pad="2",
            max_pitch_mm=6.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            from check_constraints import Board, check_pair_bridge

            path = Path(tmp) / "b.kicad_pcb"
            bridge_board(path, r2_offset=(0.0, 2.5), r2_rot=0.0)
            ok, measured, _expected = check_pair_bridge(Board(path), spec)
        self.assertTrue(ok)
        self.assertEqual(measured["pitch_mm"], 2.5)


if __name__ == "__main__":
    unittest.main()
