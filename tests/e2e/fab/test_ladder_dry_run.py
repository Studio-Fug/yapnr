"""``yapnr order stage --dry-run`` on routed regression-ladder boards (manual end to end).

``YAPNR_FAB_LADDER_RUNS`` lists ladder run directories (``hardware/pnr/regression/run.py
--fab-profile <vendor profile>``), separated by ``os.pathsep``. For every passed case the test
stages a dry-run order at that profile's vendor: KiCad's DRC under the vendor's rules must be
clean, the bundle complete, and nothing opened. ``YAPNR_FAB_E2E_OUT`` keeps the bundles (else a
temporary directory).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from yapnr import cli
from yapnr.fab import capability

RUNS = [Path(p) for p in os.environ.get("YAPNR_FAB_LADDER_RUNS", "").split(os.pathsep) if p]


def cases():
    for run in RUNS:
        profile = json.loads((run / "provenance.json").read_text())["fab_profile"]
        for result in sorted(run.glob("*/result.json")):
            doc = json.loads(result.read_text())
            if doc.get("passed"):
                yield profile, doc["case"], result.parent / "routed.kicad_pcb"


@unittest.skipUnless(RUNS, "YAPNR_FAB_LADDER_RUNS names no ladder run")
class LadderDryRunTest(unittest.TestCase):
    def test_every_passed_case_stages_a_dry_run_order(self):
        out_root = os.environ.get("YAPNR_FAB_E2E_OUT")
        tmp = None if out_root else tempfile.TemporaryDirectory()
        root = Path(out_root or tmp.name)
        staged = 0
        no_network = AssertionError("yapnr opened a network connection")
        with mock.patch(
            "webbrowser.open", side_effect=AssertionError("webbrowser.open")
        ), mock.patch("socket.socket.connect", side_effect=no_network), mock.patch(
            "socket.create_connection", side_effect=no_network
        ):
            for profile, case, board in cases():
                vendor = capability.profile(profile)["vendor"]
                with self.subTest(case=case, profile=profile):
                    out = io.StringIO()
                    argv = [
                        "order",
                        "stage",
                        str(board),
                        "--vendor",
                        vendor,
                        "--profile",
                        profile,
                        "--name",
                        case,
                        "--out",
                        str(root / profile),
                        "--dry-run",
                    ]
                    if capability.profile(profile)["status"] == "draft":
                        argv.append("--allow-draft")
                    with contextlib.redirect_stdout(out):
                        code = cli.main(argv)
                    text = out.getvalue()
                    self.assertEqual(code, 0, text)
                    self.assertIn("DRY RUN: would open https://", text)
                    self.assertIn("DRC 0 errors", text)
                    bundle = root / profile / f"{case}-{profile}"
                    card = json.loads((bundle / "order-card.staged.json").read_text())
                    with zipfile.ZipFile(bundle / card["files"]["upload"]["name"]) as zf:
                        self.assertGreaterEqual(len(zf.namelist()), 8)
                    self.assertFalse((bundle / "staged.jsonl").exists())
                    staged += 1
        self.assertGreater(staged, 0)
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
