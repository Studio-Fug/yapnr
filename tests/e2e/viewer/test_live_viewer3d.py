"""Live 3D export: a real board through the headless kicad-cli (about 20 to 40 s and 2 GB).

Manual only: YAPNR_V3D_LIVE=1, YAPNR_V3D_BOARD=<a .kicad_pcb> and optionally YAPNR_V3D_PARTS (the
atopile parts folder holding the 3D models) and YAPNR_KICAD_CLI (else the headless copy on macOS,
kicad-cli on PATH elsewhere; the GUI application is refused).
"""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from yapnr.viewer.services import viewer3d as v
from yapnr.viewer.testing import wait_for

BOARD = os.environ.get("YAPNR_V3D_BOARD")


@unittest.skipUnless(
    os.environ.get("YAPNR_V3D_LIVE") == "1" and BOARD,
    "set YAPNR_V3D_LIVE=1 and YAPNR_V3D_BOARD for a real headless KiCad export",
)
class LiveViewer3DTest(unittest.TestCase):
    def test_real_board(self):
        board = Path(BOARD)
        sha = hashlib.sha256(board.read_bytes()).hexdigest()
        parts = os.environ.get("YAPNR_V3D_PARTS")
        with tempfile.TemporaryDirectory() as t:
            s = v.Viewer3DService(t, parts=parts, timeout=300)
            self.assertIsNone(s.disabled)

            def settled():
                r = s.request(sha, [board])
                return r if r["status"] not in ("queued", "exporting") else None

            try:
                r = wait_for(settled, 320, 1)
            finally:
                s.shutdown()
        self.assertEqual(r["status"], "ready", r)
        self.assertTrue(r["meta"]["refs"])


if __name__ == "__main__":
    unittest.main()
