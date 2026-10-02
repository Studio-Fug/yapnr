"""The density-evolution animation (design §10.5) on a synthetic run directory (no solver)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import numpy as np
from PIL import Image

from yapnr.rf.animate import animate, durations, figure, frame_indices
from yapnr.rf.export.kicad import Footprint, PortPad, write_footprint
from yapnr.rf.testing import tiny_spec


def _run_dir(path: str, n: int = 14) -> None:
    spec = tiny_spec()
    with open(os.path.join(path, "spec.json"), "w") as fh:
        json.dump(spec.to_dict(), fh)
    rng = np.random.default_rng(0)
    hist, frames = [], []
    rho = np.full((6, 6), 0.5)
    target = np.zeros((6, 6))
    target[:, 2:4] = 1.0
    for k in range(n):
        rho = 0.7 * rho + 0.3 * target + 0.02 * rng.standard_normal((6, 6))
        rho = np.clip(rho, 0, 1)
        frames.append(np.round(255 * rho).astype(np.uint8))
        hist.append(
            {
                "iteration": k,
                "beta": 8.0 * 2 ** (k // 5),
                "t": 0.3 - 0.05 * k,
                "gray": float(4 * np.mean(rho * (1 - rho))),
                "keys": [["x1", 8.0], ["x1", 10.0], ["x1", 12.0]],
                "s_db": {"S11": [-10 - k, -11 - k, -12 - k], "S21": [-0.5, -0.4, -0.3]},
                "phi": {"1: a": [0.1 - 0.02 * k] * 3, "2: b": [None, 0.2 - 0.03 * k, -0.1]},
            }
        )
    with open(os.path.join(path, "history.json"), "w") as fh:
        json.dump({"iterations": hist}, fh)
    np.savez_compressed(os.path.join(path, "frames.npz"), rho_bar=np.array(frames))


def _validated(path: str) -> None:
    """A footprint (a straight line between the pads) and a validation report for `figure`."""
    spec = tiny_spec()
    x0, x1, y0, y1 = spec.design_region
    line = np.array([[x0, -0.4], [x1, -0.4], [x1, 0.4], [x0, 0.4]])
    fp = Footprint(
        "RF_tiny",
        (0.5 * (x0 + x1), 0.5 * (y0 + y1)),
        spec.design_region,
        [PortPad(1, (x0 + 0.4, 0.0), (0.8, 0.8)), PortPad(2, (x1 - 0.4, 0.0), (0.8, 0.8))],
        [(line, [1, 2])],
        seed="t",
    )
    write_footprint(fp, os.path.join(path, "footprint.kicad_mod"))
    ghz = [8.0, 9.0, 10.0, 11.0, 12.0]
    table = {
        "ghz": ghz,
        "S11": [-30.0] * 5,
        "S21": [-0.2] * 5,
        "S12": [-0.2] * 5,
        "S22": [-30.0] * 5,
    }
    report = {"case": "tiny", "ok": True}
    for level in ("coarse", "fine"):
        report[level] = {"ok": True, "checks": [], "table": table}
    with open(os.path.join(path, "validation.json"), "w") as fh:
        json.dump(report, fh)


class AnimateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rf-anim-")
        _run_dir(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_frame_indices(self):
        self.assertEqual(frame_indices(3, 10), [0, 1, 2])
        self.assertEqual(durations(3), [80, 80, 1500])
        picks = frame_indices(500, 120)
        self.assertEqual(len(picks), 120)
        self.assertEqual((picks[0], picks[-1]), (0, 499))

    def test_webp_and_gif(self):
        for ext in ("webp", "gif"):
            a = os.path.join(self.tmp, f"a.{ext}")
            b = os.path.join(self.tmp, f"b.{ext}")
            info = animate(self.tmp, a)
            animate(self.tmp, b)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read(), f"{ext} is not deterministic")
            self.assertLessEqual(info["bytes"], 2.5 * 1024 * 1024)
            with Image.open(a) as im:
                self.assertEqual(im.n_frames, 14)
                self.assertEqual(im.size[0], 800 if ext == "webp" else 640)
                if ext == "gif":  # Pillow reports per-frame durations for GIF
                    im.seek(13)
                    self.assertEqual(im.info["duration"], 1500)

    def test_bad_extension(self):
        with self.assertRaises(ValueError):
            animate(self.tmp, os.path.join(self.tmp, "x.png"))

    def test_figure(self):
        _validated(self.tmp)
        out = os.path.join(self.tmp, "fig.png")
        info = figure(self.tmp, out)
        again = os.path.join(self.tmp, "fig2.png")
        figure(self.tmp, again)
        with open(out, "rb") as fa, open(again, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        with Image.open(out) as im:
            self.assertEqual(im.size, (1000, 540))
            # Copper (the straight line between the pads) is drawn on the left half.
            rgb = np.array(im.convert("RGB"))
        self.assertGreater(info["bytes"], 1000)
        copper = np.all(np.abs(rgb[:, :460].astype(int) - [0xE8, 0x9A, 0x73]) < 3, axis=-1)
        self.assertGreater(copper.sum(), 1000)


if __name__ == "__main__":
    unittest.main()
