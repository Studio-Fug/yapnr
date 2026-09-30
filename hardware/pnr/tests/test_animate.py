"""pnr.animate on a tiny synthetic trace: storyboard, frames, deterministic bytes (also from a
moved trace), size budgets, no metadata, the overlay text validator and the command line."""

import io
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageColor
from pnr import trace
from pnr.animate import encode
from pnr.animate import render as render_mod
from pnr.animate import storyboard
from pnr.animate.cli import main, render_animation
from pnr.animate.render import Renderer, safe_text
from pnr.animate.timeline import Timeline
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.provenance import Trace


def graph():
    def part(ref, x, a, b):
        pads = [Pad("1", a, (-0.9, 0.0), (0.9, 1.2)), Pad("2", b, (0.9, 0.0), (0.9, 1.2))]
        return Component(ref, "R_0805", (x, 20.0), 0.0, "top", (3.2, 1.8), (3.2, 1.8), pads=pads)

    parts = [part("R1", 16.0, "A", "B"), part("R2", 20.0, "A", "B")]
    nets = [Net("A", 1, [("R1", "1"), ("R2", "1")]), Net("B", 2, [("R1", "2"), ("R2", "2")])]
    return BoardGraph("tiny", parts, nets, BoardOutline(12.0, 8.0))


def copper(net_y):
    return dict(
        tracks=[[0, 3100, net_y, 7100, net_y, 250]], vias=[[5000, net_y, 600, 300]], zones=[]
    )


def make_trace(root, result=None):
    root = Path(root)
    trace.write_run(
        root,
        dict(
            subject=dict(
                case="00-tiny", seed=0, description="Two resistors in parallel.", parts=2, layers=2
            ),
            config=dict(initial_pool=True),
        ),
    )
    rec = trace.Recorder(root)
    rec.begin_board(graph(), None, {"fab": {"track_width_mm": 0.25}})
    rec.section("round-01", "round")
    rec.enter("initial-pool", "pool")
    for index, start in enumerate(("start-00", "start-01")):
        rec.enter(start, "start", kind="global")
        for step in (0, 5, 9):
            x = 3000 + 400 * step + index * 500
            rec.poses(
                "global",
                [["R1", x, 4000, 0.0, "top"], ["R2", x + 3000, 4000, 90.0, "top"]],
                iter=step,
                iters=10,
                phase="global-placement",
            )
        rec.event(
            "legal",
            order=[["R1", 4000, 4000, 0.0, "top"], ["R2", 8000, 4000, 0.0, "top"]],
            backtracks=0,
        )
        rec.leave()
    rec.select(
        "shortlist",
        ["start-00", "start-01"],
        ["start-00", "start-01"],
        "capacity-proxy",
        {"start-00": 1.0, "start-01": 2.0},
    )
    for start in ("start-00", "start-01"):
        rec.enter(start + "-route", "route", start=start)
        rec.event("route_begin", progress=dict(done=0, total=2, source="router"))
        a, b = rec.blob(copper(3000)), rec.blob(copper(5000))
        rec.event(
            "net",
            net="A",
            op="add",
            provisional=True,
            copper=a,
            groups=[[0, 1]],
            progress=dict(done=1, total=2, source="router-provisional"),
        )
        rec.event(
            "net",
            net="A",
            op="commit",
            provisional=False,
            copper=a,
            groups=[[0, 1]],
            progress=dict(done=1, total=2, source="router"),
        )
        rec.event(
            "net",
            net="B",
            op="commit",
            provisional=False,
            copper=b,
            groups=[[0, 1]],
            progress=dict(done=2, total=2, source="router"),
        )
        rec.event(
            "route_end",
            nets={"A": a, "B": b},
            groups={"A": [[0, 1]], "B": [[0, 1]]},
            unrouted=[],
            progress=dict(done=2, total=2, source="router"),
        )
        rec.leave()
    rec.select(
        "chosen",
        ["start-00-route", "start-01-route"],
        "start-00-route",
        "route-objective",
        {"start-00-route": [0, 0, 2, 8.0], "start-01-route": [0, 0, 2, 9.0]},
    )
    rec.leave(type="pool")
    with_route = rec.enter("route", "route", reused=True)
    rec.leave(with_route)
    rec.leave(type="round")
    rec.select("best-round", ["round-01"], "round-01", "missing-connections", {"round-01": 0})
    rec.close()
    native = trace.Recorder(root, lane="native")
    board = dict(
        copper=dict(
            copper(3000), zones=[[0, "B", [[[0, 0], [12000, 0], [12000, 8000], [0, 8000]]]]]
        ),
        poses=[["R1", 4000, 4000, 0.0, "top"], ["R2", 8000, 4000, 0.0, "top"]],
        frame=(0.0, 0.0),
    )
    for stage in ("writeback", "planes", "refill"):
        native.board(stage, board, dict(unconnected_items=[], violations=[]), 2)
    native.result(
        **(result or dict(passed=True, opens=0, violations={}, vias=2, copper_length_mm=8.0))
    )
    native.close()


def rounds_trace(root):
    """A baseline trace: round 1 has a failed attempt and an incomplete route, round 2 wins."""
    rec = trace.Recorder(root)
    rec.begin_board(graph(), None, {"fab": {"track_width_mm": 0.25}})
    poses = [["R1", 4000, 4000, 0.0, "top"], ["R2", 8000, 4000, 0.0, "top"]]
    a = rec.blob(copper(3000))
    for index, (legal, complete) in enumerate(((False, False), (True, False), (True, True))):
        if index < 2:
            rec.section("round-01", "round") if index == 0 else None
        else:
            rec.section("round-02", "round")
        attempt = rec.enter("attempt-%d" % index, "attempt", seed=index)
        rec.poses("global", poses, iter=9, iters=10)
        if legal:
            rec.event("legal", order=poses, backtracks=0)
        rec.leave(attempt, status=None if legal else "illegal")
        if not legal:
            continue
        rec.poses("round", poses)
        route = rec.enter("route", "route")
        rec.event(
            "net",
            net="A",
            op="commit",
            provisional=False,
            copper=a,
            groups=[[0, 1]],
            progress=dict(done=1, total=2, source="router"),
        )
        nets = {"A": a, "B": rec.blob(copper(5000))} if complete else {"A": a}
        rec.event(
            "route_end",
            nets=nets,
            groups={},
            unrouted=[] if complete else ["B"],
            progress=dict(done=2 if complete else 1, total=2, source="router"),
        )
        rec.leave(route)
        if not complete:
            rec.congestion([[0, 1], [3, 0]], 2.5, {"R2": 1.6})
    rec.leave(type="round")
    rec.select(
        "best-round",
        ["round-01", "round-02"],
        "round-02",
        "missing-connections",
        {"round-01": 1, "round-02": 0},
    )
    rec.close()


def riff_chunks(data):
    chunks, pos = [], 12
    while pos + 8 <= len(data):
        tag, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        chunks.append(tag)
        pos += 8 + size + (size & 1)
    return chunks


class AnimateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "case" / "trace"
        make_trace(cls.root)
        cls.trace = Trace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_storyboard(self):
        board = storyboard.build(self.trace, title="Tiny")
        types = [s["type"] for s in board["scenes"]]
        self.assertEqual(
            types,
            [
                "title",
                "source",
                "montage",
                "montage",
                "placement",
                "route",
                "native",
                "native",
                "native",
                "end",
            ],
        )
        self.assertEqual(board["subject"]["title"], "Tiny")
        self.assertEqual(board["subject"]["connections"], 2)
        montage = board["scenes"][3]
        self.assertEqual(
            [t["label"] for t in montage["tiles"]], ["start-00-route", "start-01-route"]
        )
        self.assertEqual([t["chosen"] for t in montage["tiles"]], [True, False])
        self.assertEqual(board["scenes"][-1]["result"]["vias"], 2)
        self.assertNotIn(self.tmp.name, json.dumps(board))

    def test_rounds_attempts_and_congestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            rounds_trace(Path(tmp) / "t")
            loaded = Trace(Path(tmp) / "t")
            board = storyboard.build(loaded)
            types = [s["type"] for s in board["scenes"]]
            self.assertEqual(
                types,
                [
                    "title",
                    "source",
                    "attempts",
                    "placement",
                    "route",
                    "congestion",
                    "placement",
                    "route",
                    "end",
                ],
            )
            self.assertEqual(board["scenes"][2]["scopes"], ["round-01/attempt-0"])
            frames = Timeline(loaded, board, max_seconds=6).frames
            phases = [v.phase for v, _ms in frames]
            self.assertIn("attempts", phases)
            self.assertIn("congestion", phases)
            self.assertTrue(any(v.heat for v, _ms in frames))
            renderer = Renderer(loaded.header, board["subject"], width=240)
            data = encode.webp_bytes(renderer, frames)
            self.assertGreater(Image.open(io.BytesIO(data)).n_frames, 5)
            self.assertEqual(board["scenes"][-1]["rejected"], {})

    def test_timeline_and_budget_scaling(self):
        board = storyboard.build(self.trace)
        long = Timeline(self.trace, board, frame_ms=60, max_seconds=60).frames
        short = Timeline(self.trace, board, frame_ms=60, max_seconds=5).frames
        seconds = sum(ms for _v, ms in short) / 1000.0
        self.assertLess(len(short), len(long))
        self.assertLess(seconds, 5.6)
        last = short[-1][0]
        self.assertEqual((last.card["kind"], last.progress[:2]), ("end", (2, 2)))
        self.assertTrue(all(ms >= 10 for _v, ms in short))

    def test_deterministic_bytes_and_moved_trace(self):
        board = storyboard.build(self.trace)
        frames = Timeline(self.trace, board, max_seconds=4).frames
        one = encode.webp_bytes(Renderer(self.trace.header, board["subject"], width=320), frames)
        two = encode.webp_bytes(Renderer(self.trace.header, board["subject"], width=320), frames)
        self.assertEqual(one, two)
        with tempfile.TemporaryDirectory() as other:
            moved = Path(other) / "elsewhere" / "t"
            shutil.copytree(self.root, moved)
            copy = Trace(moved)
            again = storyboard.build(copy)
            frames = Timeline(copy, again, max_seconds=4).frames
            three = encode.webp_bytes(Renderer(copy.header, again["subject"], width=320), frames)
        self.assertEqual(one, three)
        self.assertEqual(riff_chunks(one)[0], b"VP8X")
        self.assertFalse({b"EXIF", b"XMP ", b"ICCP"} & set(riff_chunks(one)))
        image = Image.open(io.BytesIO(one))
        self.assertEqual((image.format, image.size[0]), ("WEBP", 320))
        self.assertGreater(image.n_frames, 10)

    def test_end_card_names_the_failing_rules(self):
        failed = dict(passed=False, opens=0, violations={"clearance": 3}, vias=2)
        failed.update(copper_length_mm=8.0, rules={"fab_via_to_smd_pad": 3})
        with tempfile.TemporaryDirectory() as tmp:
            make_trace(Path(tmp) / "t", failed)
            loaded = Trace(Path(tmp) / "t")
            board = storyboard.build(loaded)
            self.assertEqual(board["scenes"][-1]["result"]["rules"], {"fab_via_to_smd_pad": 3})
            frames = Timeline(loaded, board, max_seconds=4).frames
            renderer = Renderer(loaded.header, board["subject"], width=480)
            renderer.frame(frames[-1][0])
        self.assertIn(
            "KiCad DRC: 0 unconnected · 3 violations (fab_via_to_smd_pad)", renderer.strings
        )
        self.assertEqual(render_mod._rule_names({"b": 1, "a": 4, "c": 1}), "a 4, b 1, 1 more")
        self.assertEqual(render_mod._rule_names({"bad/name": 2}), "")

    def test_gif_keeps_the_signal_colours(self):
        board = storyboard.build(self.trace)
        frames = Timeline(self.trace, board, frame_ms=80, max_seconds=3).frames
        renderer = Renderer(self.trace.header, board["subject"], width=240)
        palette = encode.gif_palette(renderer, frames, 32).getpalette()
        entries = {tuple(palette[i : i + 3]) for i in range(0, len(palette), 3)}
        for colour in encode.SIGNAL_COLOURS:
            self.assertIn(ImageColor.getrgb(colour), entries)

    def test_gif_has_one_palette_and_no_comment(self):
        board = storyboard.build(self.trace)
        frames = Timeline(self.trace, board, frame_ms=80, max_seconds=3).frames
        renderer = Renderer(self.trace.header, board["subject"], width=240)
        data = encode.gif_bytes(renderer, frames, colors=32)
        self.assertEqual(data, encode.gif_bytes(renderer, frames, colors=32))
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.format, "GIF")
        self.assertNotIn("comment", image.info)
        self.assertLessEqual(len(image.getpalette()) // 3, 256)

    def test_encode_budget(self):
        board = storyboard.build(self.trace)

        def make(width, frame_ms):
            renderer = Renderer(self.trace.header, board["subject"], width=min(width, 240))
            return renderer, Timeline(self.trace, board, frame_ms=frame_ms, max_seconds=3).frames

        data, settings, _frames, _renderer = encode.encode("webp", make, 10 * 1024 * 1024)
        self.assertEqual(settings, encode.WEBP_STEPS[0])
        with self.assertRaises(ValueError):
            encode.encode("webp", make, 100, steps=encode.WEBP_STEPS[:2])

    def test_overlay_text_validator(self):
        for bad in ("a/b", "~/x", "C:\\x", "someone@example.com", "https://x"):
            with self.assertRaises(ValueError):
                safe_text(bad)
        for good in (
            "TLC555 + CD4017B five-stage chaser",
            "10 parts \u00b7 9 nets",
            "3.3V, GND, input",
        ):
            self.assertEqual(safe_text(good), good)
        with self.assertRaises(ValueError):
            storyboard_title = "run/dir"
            Renderer(
                self.trace.header,
                dict(storyboard.build(self.trace)["subject"], title=storyboard_title),
            )

    def test_manifest_entry_and_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            entry = render_animation(
                self.trace, "webp", out / "tiny.webp", width=320, max_seconds=3
            )
            self.assertEqual(entry["file"], "tiny.webp")
            self.assertEqual(entry["bytes"], (out / "tiny.webp").stat().st_size)
            self.assertEqual(entry["result"]["passed"], True)
            self.assertIn("2 parts \u00b7 2 nets \u00b7 2 layers", entry["captions"])
            self.assertNotIn(tmp, json.dumps(entry))
            manifest = out / "manifest.json"
            code = main(
                [
                    str(self.root.parent),
                    "--out",
                    str(out),
                    "--format",
                    "webp,gif",
                    "--width",
                    "320",
                    "--gif-width",
                    "240",
                    "--max-seconds",
                    "3",
                    "--manifest",
                    str(manifest),
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue((out / "00-tiny.webp").is_file() and (out / "00-tiny.gif").is_file())
            doc = json.loads(manifest.read_text())
            self.assertEqual(
                [a["file"] for a in doc["animations"]], ["00-tiny.gif", "00-tiny.webp"]
            )
            with self.assertRaises(SystemExit):
                main([str(self.root.parent), "--out", str(self.root.parent / "x.webp")])
            story = out / "story.json"
            main([str(self.root), "--storyboard", str(story)])
            self.assertEqual(json.loads(story.read_text())["schema"], "pnr-storyboard-v1")


if __name__ == "__main__":
    unittest.main()
