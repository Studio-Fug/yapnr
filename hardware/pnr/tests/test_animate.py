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


def make_trace(root, result=None, starts=("start-00", "start-01"), shortlist=None):
    root = Path(root)
    shortlist = list(shortlist or starts)
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
    for index, start in enumerate(starts):
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
        list(starts),
        shortlist,
        "capacity-proxy",
        {s: 1.0 + i for i, s in enumerate(starts)},
    )
    for start in shortlist:
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
        [s + "-route" for s in shortlist],
        shortlist[0] + "-route",
        "route-objective",
        {s + "-route": [0, 0, 2, 8.0 + i] for i, s in enumerate(shortlist)},
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


def board_text(r1_x, segments):
    """A two-resistor KiCad board (outline 12 x 8 mm at KiCad (10, 20)) with ``segments``."""

    def part(ref, x):
        pads = "".join(
            '(pad "%s" smd roundrect (at %s 0) (size 0.9 1.2) (layers "F.Cu") '
            '(roundrect_rratio 0.25) (net "%s"))' % (n, dx, net)
            for n, dx, net in (("1", -0.9, "A"), ("2", 0.9, "B"))
        )
        return (
            '(footprint "R_0805" (layer "F.Cu") (at %s 24 0) (property "Reference" "%s") '
            '(property "Value" "1k") %s)' % (x, ref, pads)
        )

    tracks = "".join(
        '(segment (start %s %s) (end %s %s) (width 0.25) (layer "F.Cu") (net "%s"))' % seg
        for seg in segments
    )
    return (
        '(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal) '
        '(25 "Edge.Cuts" user)) (gr_rect (start 10 20) (end 22 28) (layer "Edge.Cuts")) '
        + part("R1", 10 + r1_x)
        + part("R2", 18)
        + tracks
        + ")"
    )


def halving_run(root):
    """A successive-halving run: 4 starts (one illegal), 3 in rung 1, 2 native; p001 wins."""
    root = Path(root)
    records = []
    for i in range(4):
        record = dict(id="p%03d" % i, stage="place", status="legal" if i < 3 else "failed")
        if i < 3:
            record.update(
                proxy_score=10.0 - i,
                poses={"R1": [4.0 + i * 0.5, 4.0, 0.0, "top"], "R2": [8.0, 4.0, 0.0, "top"]},
            )
        records.append(record)
    for i, opens in ((0, 2), (1, 1), (2, 3)):
        records.append(
            dict(id="p%03d" % i, stage="rung1", status="ok", objective=[0, 0, 0, 0, 0, opens])
        )
    for i, opens in ((0, 1), (1, 0)):
        records.append(
            dict(id="p%03d" % i, stage="native", status="ok", objective=[0, 0, 0, 0, 0, opens])
        )
    (root / "cand").mkdir(parents=True)
    (root / "dataset.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    for i in range(3):
        cand = root / "cand" / ("p%03d" % i)
        cand.mkdir()
        (cand / "placed.json").write_text(graph().to_json())
        if i > 1:
            continue
        native = cand / "native"
        steps = (
            ("00-placement", [], 2),
            ("01-signals", [(14, 24, 18, 24, "A")], 1),
            ("02-final", [(14, 24, 18, 24, "A"), (16, 24, 20, 24, "B")], 1 - i),
        )
        for name, segments, opens in steps:
            phase = native / "phases" / name
            phase.mkdir(parents=True)
            (phase / "diagnostic.kicad_pcb").write_text(board_text(4.0 + i * 0.5, segments))
            unconnected = [dict(items=[dict(pos=dict(x=14, y=24)), dict(pos=dict(x=18, y=24))])]
            (phase / "diagnostic.drc.json").write_text(
                json.dumps(dict(unconnected_items=unconnected * opens, violations=[]))
            )
        (native / "source.kicad_pcb").write_text(board_text(1.0, []))
    return root


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
        # Each montage follows the winner's own replay up to the state its tiles show.
        self.assertEqual(
            types,
            [
                "title",
                "source",
                "placement",
                "montage",
                "route",
                "montage",
                "native",
                "native",
                "native",
                "end",
            ],
        )
        self.assertEqual(board["subject"]["title"], "Tiny")
        self.assertEqual(board["subject"]["connections"], 2)
        self.assertEqual(board["scenes"][3]["criterion"], "capacity-proxy")
        montage = board["scenes"][5]
        self.assertEqual(
            [t["label"] for t in montage["tiles"]], ["start-00-route", "start-01-route"]
        )
        self.assertEqual([t["chosen"] for t in montage["tiles"]], [True, False])
        self.assertEqual(board["scenes"][-1]["result"]["vias"], 2)
        # Both starts were shortlisted: the proxy set none aside; the route objective one.
        self.assertEqual(board["scenes"][-1]["rejected"], {"route-objective": 1})
        self.assertNotIn(self.tmp.name, json.dumps(board))

    def test_rivals_are_counted_once_per_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            starts = ("start-00", "start-01", "start-02", "start-03")
            make_trace(Path(tmp) / "t", starts=starts, shortlist=["start-02", "start-00"])
            board = storyboard.build(Trace(Path(tmp) / "t"))
        # 4 legal starts, 2 shortlisted (2 set aside by the proxy); 1 finalist lost the route.
        self.assertEqual(
            board["scenes"][-1]["rejected"], {"capacity-proxy": 2, "route-objective": 1}
        )

    def test_the_timeline_runs_straight_from_zero_to_routed(self):
        board = storyboard.build(self.trace)
        frames = [v for v, _ms in Timeline(self.trace, board, max_seconds=30).frames]
        title = frames[0]
        self.assertEqual(title.card["kind"], "title")
        backdrop = title.card["backdrop"]
        self.assertFalse(backdrop.committed or backdrop.native)  # the unplaced board
        done = [v.progress[0] for v in frames]
        self.assertEqual(done, sorted(done))  # one round: the bar never goes back
        self.assertEqual(done[-1], 2)
        first_copper = next(i for i, v in enumerate(frames) if v.committed or v.provisional)
        self.assertTrue(all(v.progress[0] == 0 for v in frames[:first_copper]))
        tiles = [t for v in frames[:first_copper] if v.montage for t in v.montage["tiles"]]
        self.assertTrue(tiles and not any(t["view"].committed for t in tiles))
        # Negotiation fills the lighter bar only; the number counts committed connections.
        negotiation = [v for v in frames if v.phase == "negotiation"]
        self.assertTrue(negotiation)
        self.assertTrue(all(v.progress[0] == 0 and v.ghost[0] == 1 for v in negotiation))
        routed_montage = [
            v for v in frames if v.montage and v.montage["criterion"] == "route-objective"
        ]
        self.assertTrue(routed_montage and all(v.progress[0] == 2 for v in routed_montage))

    def test_vias_are_drawn_over_pads_and_findings_are_marked(self):
        board = storyboard.build(self.trace)
        renderer = Renderer(self.trace.header, board["subject"], width=480)
        poses = {"R1": (4000, 4000, 0.0, "top"), "R2": (8000, 4000, 0.0, "top")}
        via = dict(tracks=[], vias=[[3100, 4000, 600, 300]], zones=[])  # on R1's pad 1
        view = (
            Timeline(self.trace, board, max_seconds=3)
            .frames[-1][0]
            .copy(
                poses=poses,
                committed={"A": via},
                native=None,
                native_mix=0.0,
                card=None,
                findings=(),
                open_pairs=[],
                flash={},
                ripped={},
                provisional={},
                camera=(0, 0, 12000, 8000),
            )
        )
        image = renderer.board(view, 480, 320)
        x, y = 3100 * 480 / 12000.0, 320 - 4000 * 320 / 8000.0
        pixel = image.getpixel((int(x), int(y)))
        hole, pad = ImageColor.getrgb(render_mod.theme.VIA_HOLE), ImageColor.getrgb(
            render_mod.theme.PAD
        )
        near = lambda c: sum((a - b) ** 2 for a, b in zip(pixel, c))  # noqa: E731
        self.assertLess(near(hole), near(pad))
        marked = renderer.board(view.copy(findings=((3100, 4000),)), 480, 320)
        self.assertNotEqual(image.tobytes(), marked.tobytes())

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
            with Image.open(out / "tiny.webp") as encoded:
                self.assertEqual(entry["frames"], encoded.n_frames)  # as a player sees it
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


class CoarseRunTest(unittest.TestCase):
    def test_a_halving_run_is_animated_along_its_winner(self):
        from pnr import provenance

        with tempfile.TemporaryDirectory() as tmp:
            run = halving_run(Path(tmp) / "h1")
            before = {p: p.stat().st_mtime_ns for p in run.rglob("*")}
            loaded = provenance.halving_trace(run)
            board = storyboard.build(loaded, title="Halving")
            types = [s["type"] for s in board["scenes"]]
            self.assertEqual(
                types,
                ["title", "source", "move", "montage", "montage"]
                + ["native"] * 3
                + ["montage", "end"],
            )
            self.assertEqual(
                board["path"][1:3], ["halving:p001/place", "halving:promote-rung1[p001]"]
            )
            self.assertEqual(
                [s["criterion"] for s in board["scenes"] if s["type"] == "montage"],
                ["place-objective", "rung1-objective", "native-objective"],
            )
            end = board["scenes"][-1]
            self.assertEqual((end["result"]["passed"], end["result"]["opens"]), (True, 0))
            # 3 legal starts all went on (none set aside), rung 1 dropped p002, native p000.
            self.assertEqual(end["rejected"], {"native-objective": 1, "rung1-objective": 1})
            frames = Timeline(loaded, board, max_seconds=8).frames
            done = [v.progress[0] for v, _ms in frames]
            self.assertEqual(done, sorted(done))
            self.assertEqual(frames[-1][0].progress[:2], (2, 2))
            out = Path(tmp) / "out"
            code = main(
                [str(run), "--out", str(out / "h1.webp"), "--width", "240", "--max-seconds", "3"]
            )
            self.assertEqual(code, 0)
            self.assertTrue((out / "h1.webp").is_file())
            self.assertEqual(before, {p: p.stat().st_mtime_ns for p in run.rglob("*")})

    def test_a_synthesis_library_gives_its_critical_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            for template in ("ldo", "usb"):
                (root / template).mkdir(parents=True)
                ranked = [dict(tag="%s-%d" % (template, k), objective=[0, k]) for k in range(3)]
                (root / template / "library.json").write_text(
                    json.dumps(dict(template_id=template, ranked=ranked))
                )
            story = Path(tmp) / "story.json"
            self.assertEqual(main([str(root), "--storyboard", str(story)]), 0)
            doc = json.loads(story.read_text())
            with self.assertRaises(SystemExit):
                main([str(root), "--out", str(Path(tmp) / "x.webp")])
        self.assertEqual(doc["kind"], "synthesis")
        self.assertIn("block:usb/usb-0", doc["path"])
        self.assertEqual(doc["rejected"], {"native-rank": 4})


if __name__ == "__main__":
    unittest.main()
