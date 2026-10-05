"""extract_footprints against real KiCad I/O (KiCad Python only: python3 -m unittest
tests.test_library_table_kicad). Builds a tiny board whose footprints carry a synthetic lib
nickname absent from any fp-lib-table (as radar60's atopile-generated parts do), saves them
out with extract_footprints, and checks that registering the resulting table with KiCad's own
DRC clears the ``lib_footprint_issues`` violation it raises without one."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pnr.library_table import extract_footprints, library_table

NATIVE = importlib.util.find_spec("pcbnew") is not None


def _board(tmp):
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(2)
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(k.VECTOR2I(0, 0))
    edge.SetEnd(k.VECTOR2I(10_000_000, 10_000_000))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50_000)
    b.Add(edge)
    net = k.NETINFO_ITEM(b, "N1")
    b.Add(net)

    def part(x, nickname, name, ref):
        fp = k.FOOTPRINT(b)
        fp.SetFPID(k.LIB_ID(nickname, name))
        fp.SetReference(ref)
        fp.SetPosition(k.VECTOR2I(x, 5_000_000))
        pad = k.PAD(fp)
        pad.SetNumber("1")
        pad.SetShape(k.PAD_SHAPE_RECTANGLE)
        pad.SetSize(k.VECTOR2I(500_000, 500_000))
        pad.SetAttribute(k.PAD_ATTRIB_SMD)
        pad.SetLayerSet(k.PAD.SMDMask())
        pad.SetNet(net)
        fp.Add(pad)
        b.Add(fp)
        return fp

    # Two placements of the same synthetic library part (dedup must save it once), plus a
    # second nickname and a board-local footprint (empty nickname: no library entry needed).
    part(2_000_000, "Radar60_R_10k_0402", "R_0402", "R1")
    part(4_000_000, "Radar60_R_10k_0402", "R_0402", "R2")
    part(6_000_000, "Radar60_C_100n_0402", "C_0402", "C1")
    part(8_000_000, "", "RadomeStandoffLand_3mm", "RL1")
    pcb = tmp / "board.kicad_pcb"
    k.SaveBoard(str(pcb), b)
    return pcb


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class ExtractFootprintsTest(unittest.TestCase):
    def test_dedup_and_skip_unnamed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            pcb = _board(tmp)
            written = extract_footprints(pcb, tmp / "libs")
            rel = sorted(str(p.relative_to(tmp / "libs")) for p in written)
            self.assertEqual(
                rel,
                [
                    "Radar60_C_100n_0402/C_0402.kicad_mod",
                    "Radar60_R_10k_0402/R_0402.kicad_mod",
                ],
            )
            for p in written:
                self.assertTrue(p.is_file())

    def test_clears_lib_footprint_issues_drc(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            pcb = _board(tmp)
            written = extract_footprints(pcb, tmp / "libs")
            drc_before = tmp / "before.json"
            subprocess.run(
                [
                    "kicad-cli",
                    "pcb",
                    "drc",
                    "--severity-all",
                    "--format",
                    "json",
                    "-o",
                    str(drc_before),
                    str(pcb),
                ],
                check=False,
                capture_output=True,
            )
            before = json.loads(drc_before.read_text())
            before_types = {v["type"] for v in before.get("violations", [])}
            self.assertIn("lib_footprint_issues", before_types)

            (pcb.parent / "fp-lib-table").write_text(library_table(written))
            drc_after = tmp / "after.json"
            subprocess.run(
                [
                    "kicad-cli",
                    "pcb",
                    "drc",
                    "--severity-all",
                    "--format",
                    "json",
                    "-o",
                    str(drc_after),
                    str(pcb),
                ],
                check=False,
                capture_output=True,
            )
            after = json.loads(drc_after.read_text())
            after_types = {v["type"] for v in after.get("violations", [])}
            self.assertNotIn("lib_footprint_issues", after_types)


if __name__ == "__main__":
    unittest.main()
