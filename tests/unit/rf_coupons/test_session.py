"""Measurement sessions, file names and Touchstone files (design §7.6)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import numpy as np

from yapnr.rf.coupons import SCHEMA_SESSION, catalog, jsonfmt, session, touchstone


class NameTest(unittest.TestCase):
    def test_roundtrip(self):
        name = session.file_name("A", "03", "A05", "P-MO", "L40", 2)
        self.assertEqual(name, "A-03_A05-PMO-L40_r2.s2p")
        d = session.parse_name(name)
        self.assertEqual(
            (d["board"], d["serial"], d["stick"], d["what"], d["repeat"]),
            ("A", "03", "A05", "L40", 2),
        )

    def test_rejects(self):
        for bad in (
            "A05.s2p",
            "A-03_A5-P-L40_r1.s2p",
            "A-03_A05-P-L40.s2p",
            "a-03_A05-P-L40_r1.s2p",
        ):
            self.assertIsNone(session.parse_name(bad), bad)


class TouchstoneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip_formats(self):
        f = np.linspace(0.1e9, 6e9, 7)
        rng = np.random.default_rng(1)
        s = 0.5 * (rng.standard_normal((7, 2, 2)) + 1j * rng.standard_normal((7, 2, 2)))
        for fmt in ("ri", "ma", "db"):
            p = os.path.join(self.dir, f"x_{fmt}.s2p")
            touchstone.write(p, f, s, ["comment"], fmt=fmt, digits=10)
            f2, s2, ref = touchstone.read(p)
            np.testing.assert_allclose(f2, f, rtol=1e-9)
            np.testing.assert_allclose(s2, s, atol=1e-6)
            self.assertEqual(ref, 50.0)

    def test_two_port_order(self):
        """Touchstone 1.0 orders two-port data S11 S21 S12 S22."""
        p = os.path.join(self.dir, "o.s2p")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("# GHz S RI R 50\n1 0.1 0 0.2 0 0.3 0 0.4 0\n")
        _, s, _ = touchstone.read(p)
        self.assertEqual((s[0, 1, 0].real, s[0, 0, 1].real), (0.2, 0.3))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_load_and_check(self):
        session.write_manifest(self.dir, {"stackup": "JLC04161H-7628", "lot": "test"})
        f = np.linspace(0.1e9, 1e9, 5)
        s = np.zeros((5, 2, 2), complex)
        s[:, 0, 1] = s[:, 1, 0] = 1.0
        touchstone.write(
            os.path.join(self.dir, session.file_name("A", "01", "A01", "P", "THRU", 1)),
            f,
            s,
            fmt="ri",
        )
        with open(os.path.join(self.dir, "misnamed.s2p"), "w", encoding="utf-8") as fh:
            fh.write("# GHz S RI R 50\n")
        session.write_dc(
            self.dir,
            [
                dict(
                    serial="01",
                    stick="A21",
                    layer=1,
                    width_mm=0.2,
                    current_a=0.1,
                    voltage_v=0.05,
                    temp_c=23.0,
                )
            ],
        )
        with open(os.path.join(self.dir, "microsection.json"), "w", encoding="utf-8") as fh:
            json.dump({"pp1.h": [0.21, 0.005]}, fh)
        ses = session.load(self.dir)
        self.assertEqual(ses.manifest["schema"], SCHEMA_SESSION)
        self.assertEqual(list(ses.meas), ["A01"])
        self.assertEqual(ses.dc[0]["layer"], 1)
        self.assertEqual(ses.microsection["pp1.h"], (0.21, 0.005))
        problems = session.check(ses, catalog.board("JLC04161H-7628"))
        self.assertIn("unparsed file name: misnamed.s2p", problems)
        self.assertTrue(any(p.startswith("no measurement of A06") for p in problems))
        self.assertFalse(any("A01" in p for p in problems))

    def test_schema_required(self):
        with open(os.path.join(self.dir, "session.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": "something-else"}, fh)
        with self.assertRaises(ValueError):
            session.load(self.dir)


class JsonFormatTest(unittest.TestCase):
    def test_compact_lists(self):
        text = jsonfmt.dumps({"a": [1, 2.5, "x"], "b": {"c": [[1, 2], [3]]}, "d": []})
        self.assertIn('"a": [1, 2.5, "x"]', text)
        self.assertEqual(json.loads(text)["b"]["c"], [[1, 2], [3]])


if __name__ == "__main__":
    unittest.main()
