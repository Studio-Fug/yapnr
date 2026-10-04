"""The command line (`python -m yapnr.rf.coupons`): synthetic session -> check -> extract, and
the expected Touchstone files."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from yapnr.rf.coupons import touchstone
from yapnr.rf.coupons.cli import main


def _run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_synthetic_check_extract(self):
        ses, out = os.path.join(self.dir, "ses"), os.path.join(self.dir, "out")
        st = ["--stackup", "JLC04161H-7628"]
        code, text = _run(["synthetic", *st, "--out", ses, "--fmax", "4", "--step", "40"])
        self.assertEqual(code, 0, text)
        code, text = _run(["check-session", *st, ses])
        self.assertEqual(code, 0, text)
        code, text = _run(
            [
                "extract",
                *st,
                "--measured",
                ses,
                "--out",
                out,
                "--bootstrap",
                "0",
                "--no-systematics",
            ]
        )
        self.assertEqual(code, 0, text)
        self.assertIn("pp.dk", text)
        with open(os.path.join(out, "fit.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        # a later lot fitted with this fit as the prior (design §5.4)
        code, text = _run(
            [
                "extract",
                *st,
                "--measured",
                ses,
                "--out",
                out + "2",
                "--bootstrap",
                "0",
                "--no-systematics",
                "--prior",
                os.path.join(out, "fit.json"),
            ]
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(rec["stackup"]["id"], "JLC04161H-7628")

    def test_expected(self):
        code, text = _run(["expected", "--stackup", "JLC06161H-7628", "--out", self.dir])
        self.assertEqual(code, 0, text)
        files = sorted(os.listdir(self.dir))
        self.assertIn("B06-S-line.s2p", files)
        self.assertIn("B11-S-ring.s2p", files)
        f, s, ref = touchstone.read(os.path.join(self.dir, "B06-S-line.s2p"))
        self.assertEqual(len(f), 180)
        self.assertLess(abs(s[57, 1, 0]), 1.0)  # 5.8 GHz: 100 mm of stripline loses about 3 dB
        self.assertGreater(abs(s[57, 1, 0]), 0.5)

    def test_unknown_stackup(self):
        with self.assertRaises(KeyError):
            _run(["expected", "--stackup", "NOPE", "--out", self.dir])


if __name__ == "__main__":
    unittest.main()
