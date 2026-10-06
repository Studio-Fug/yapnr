"""``pnr.animate.plane_partition``: the WebP animation and the per-stage stills it renders
from a ``pnr.plane_partition.PlaneTrace`` (docs/plane-partition.md)."""

import io
import os
import tempfile
import unittest

from PIL import Image

from pnr.animate.plane_partition import (
    STAGE_ORDER,
    Renderer,
    make_frames,
    render_stills,
    render_webp,
)
from pnr.plane_partition import _CACHE, PlaneTrace, Terminal, partition

ENTRY = dict(
    layer="In2.Cu",
    nets=["A", "B"],
    order="current",
    split_gap_mm=0.3,
    min_width_mm=1.0,
    fill="GND",
    core_no_vias=True,
    terminal_reach_mm=0.8,
    currents={},
    budgets_mohm={},
    sources={},
    h_mm=0.1,
)


def pad(name, at):
    return Terminal(name, "pad", at, 0.8)


def via(name, at):
    return Terminal(name, "via", at, 0.175)


TERMS = {
    "A": [pad("A1", (2.0, 2.0)), pad("A2", (18.0, 2.0)), via("A3", (10.0, 5.0))],
    "B": [pad("B1", (2.0, 10.0)), pad("B2", (18.0, 10.0)), via("B3", (10.0, 7.0))],
}
FOREIGN = [((x, 6.0), 0.3) for x in (6.0, 8.0, 12.0, 14.0)]


def traced_partition():
    _CACHE.clear()
    trace = PlaneTrace(nets=list(ENTRY["nets"]))
    partition(
        ENTRY,
        width=20.0,
        height=12.0,
        terminals=TERMS,
        blocked=FOREIGN,
        currents={"A": 4.0, "B": 1.5},
        trace=trace,
    )
    return trace


class WebpTest(unittest.TestCase):
    def setUp(self):
        self.trace = traced_partition()

    def test_a_webp_is_written_within_budget_and_plays_every_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.webp")
            budget = 400_000
            settings = render_webp(self.trace, path, title="Unit test", budget_bytes=budget)
            self.assertLessEqual(os.path.getsize(path), budget)
            self.assertIn("quality", settings)
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(data[0:4], b"RIFF")
            self.assertEqual(data[8:12], b"WEBP")
            image = Image.open(io.BytesIO(data))
            self.assertTrue(image.is_animated)
            self.assertEqual(image.n_frames, len(self.trace.frames))
            for i in range(image.n_frames):  # every frame decodes
                image.seek(i)
                image.load()

    def test_an_impossible_budget_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.webp")
            with self.assertRaises(ValueError):
                render_webp(self.trace, path, budget_bytes=16)


class StillsTest(unittest.TestCase):
    def test_one_png_per_stage_in_order(self):
        trace = traced_partition()
        with tempfile.TemporaryDirectory() as tmp:
            names = render_stills(trace, tmp, prefix="case", title="Unit test")
            stages = [n.split("case-")[1].rsplit(".png")[0] for n in names]
            self.assertEqual(
                stages, sorted(set(f.stage for f in trace.frames), key=STAGE_ORDER.index)
            )
            for name in names:
                path = os.path.join(tmp, name)
                self.assertTrue(os.path.isfile(path))
                image = Image.open(path)
                image.load()
                self.assertEqual(image.format, "PNG")


class FramesTest(unittest.TestCase):
    def test_make_frames_holds_the_last_frame_longest(self):
        trace = traced_partition()
        frames = make_frames(trace)
        self.assertEqual(len(frames), len(trace.frames))
        durations = [ms for _f, ms in frames]
        self.assertEqual(max(durations), durations[-1])

    def test_renderer_frame_is_cached_by_identity(self):
        trace = traced_partition()
        renderer = Renderer(trace, width=320, title="t")
        a = renderer.frame(trace.frames[0])
        b = renderer.frame(trace.frames[0])
        self.assertIs(a, b)
        c = renderer.frame(trace.frames[1])
        self.assertIsNot(a, c)


if __name__ == "__main__":
    unittest.main()
