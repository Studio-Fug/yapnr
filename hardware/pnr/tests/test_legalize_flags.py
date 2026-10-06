"""The legalizer and global-placement switches (pnr.legalize_flags; docs/design/
compact-placement.md section 11): parsing, the runner's options, the trace header, and the
flag-off identity on two ladder cases (the goldens of testdata/compact/identity.json, which the
parent commit reproduces) with every new function patched to fail.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "regression"))

import compact_fixture as fixture  # noqa: E402

from pnr import legalize_flags  # noqa: E402

COMPACT = ("PNR_COMPACT", "PNR_SHRINK")


@contextlib.contextmanager
def flags(**values):
    """Every legalizer, polish and compact switch unset, then ``values`` set."""
    keys = legalize_flags.FLAGS + COMPACT
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ParseTest(unittest.TestCase):
    def test_unset_is_off(self):
        with flags():
            self.assertFalse(legalize_flags.gp_polish())
            self.assertIsNone(legalize_flags.gp_channels())
            self.assertFalse(legalize_flags.pool_source_clamp())
            self.assertIsNone(legalize_flags.legalize_hpwl())
            self.assertFalse(legalize_flags.legalize_reorient())
            self.assertEqual(legalize_flags.active(), {})
        with flags(PNR_LEGALIZE_HPWL="0", PNR_GP_CHANNELS="", PNR_GP_POLISH="0"):
            self.assertEqual(legalize_flags.active(), {})

    def test_values_and_implication(self):
        with flags(PNR_GP_CHANNELS="0.5", PNR_LEGALIZE_HPWL="4", PNR_LEGALIZE_REORIENT="1"):
            self.assertTrue(legalize_flags.gp_polish())  # the channel term implies the polish
            self.assertEqual(
                legalize_flags.active(),
                dict(
                    GP_POLISH=200, GP_CHANNELS=0.5, LEGALIZE_HPWL=4.0, LEGALIZE_REORIENT="guarded"
                ),
            )
        with flags(PNR_LEGALIZE_REORIENT="wire"):
            self.assertEqual(legalize_flags.legalize_reorient(), "wire")
        with flags(PNR_LEGALIZE_REORIENT="yes"), self.assertRaises(ValueError):
            legalize_flags.legalize_reorient()
        for bad in ("-1", "nan", "inf", "four"):
            with flags(PNR_LEGALIZE_HPWL=bad), self.assertRaises(ValueError):
                legalize_flags.legalize_hpwl()

    def test_compact_parts_and_explicit_values(self):
        """WIRE, TURN and SATELLITES are PNR_COMPACT parts; a set variable (0 included) wins
        over its part, and PNR_COMPACT_<PART>=0 drops it."""
        with flags(PNR_COMPACT="1"):
            self.assertEqual(legalize_flags.legalize_hpwl(), legalize_flags.COMPACT_WIRE_WEIGHT)
            self.assertEqual(legalize_flags.legalize_reorient(), "wire")
            self.assertTrue(legalize_flags.line_satellites())
            self.assertEqual(
                legalize_flags.active(),
                dict(LEGALIZE_HPWL=4.0, LEGALIZE_REORIENT="wire", LINE_SATELLITES=True),
            )
        with flags(
            PNR_COMPACT="1",
            PNR_LEGALIZE_HPWL="16",
            PNR_LEGALIZE_REORIENT="0",
            PNR_LINE_SATELLITES="0",
        ):
            self.assertEqual(legalize_flags.legalize_hpwl(), 16.0)
            self.assertIsNone(legalize_flags.legalize_reorient())
            self.assertFalse(legalize_flags.line_satellites())
        with flags(PNR_COMPACT="1", PNR_LEGALIZE_HPWL="0"):
            self.assertIsNone(legalize_flags.legalize_hpwl())
        saved = {k: os.environ.get(k) for k in ("PNR_COMPACT_WIRE", "PNR_COMPACT_TURN")}
        try:
            os.environ.update(PNR_COMPACT_WIRE="0", PNR_COMPACT_TURN="0")
            with flags(PNR_COMPACT="1"):
                self.assertIsNone(legalize_flags.legalize_hpwl())
                self.assertIsNone(legalize_flags.legalize_reorient())
                self.assertTrue(legalize_flags.line_satellites())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_channel_clearance_and_satellites(self):
        with flags():
            self.assertIsNone(legalize_flags.legalize_channel_clearance())
            self.assertFalse(legalize_flags.line_satellites())
        with flags(PNR_LEGALIZE_CHANNEL_CLEARANCE="fab", PNR_LINE_SATELLITES="1"):
            self.assertEqual(
                legalize_flags.active(),
                dict(LEGALIZE_CHANNEL_CLEARANCE="fab", LINE_SATELLITES=True),
            )
        with flags(PNR_LEGALIZE_CHANNEL_CLEARANCE="0.2"), self.assertRaises(ValueError):
            legalize_flags.legalize_channel_clearance()
        import run

        self.assertEqual(
            run.legalize_environment(channel_clearance="fab", line_satellites=True),
            dict(PNR_LEGALIZE_CHANNEL_CLEARANCE="fab", PNR_LINE_SATELLITES="1"),
        )
        args = run.parser().parse_args(
            ["--out", "x", "--legalize-channel-clearance", "fab", "--line-satellites"]
        )
        self.assertEqual((args.legalize_channel_clearance, args.line_satellites), ("fab", True))

    def test_channel_layers(self):
        from pnr.graph import BoardGraph
        from pnr.place.channels import ChannelModel

        graph = BoardGraph("empty", [], [])
        with flags():
            self.assertFalse(legalize_flags.channel_layers())
            self.assertEqual(ChannelModel(graph, {"layers": 4}).share, 1.0)
        with flags(PNR_CHANNEL_LAYERS="1"):
            self.assertEqual(legalize_flags.active(), dict(CHANNEL_LAYERS=True))
            self.assertEqual(ChannelModel(graph, {"layers": 4}).share, 0.25)
            self.assertEqual(ChannelModel(graph, {"layers": 4}, layers=False).share, 1.0)
        import run

        self.assertEqual(
            run.legalize_environment(channel_layers=True), dict(PNR_CHANNEL_LAYERS="1")
        )
        self.assertTrue(run.parser().parse_args(["--out", "x", "--channel-layers"]).channel_layers)

    def test_channel_clearance_reaches_the_legalizer_model(self):
        from pnr.place import placer
        from pnr.place.channels import ChannelModel

        graph, _constraints, rules = fixture.load("04-inverter-leds-8")
        with flags():
            self.assertEqual(placer._channel_kwargs(rules), {})
        with flags(PNR_LEGALIZE_CHANNEL_CLEARANCE="fab"):
            kwargs = placer._channel_kwargs(rules)
        self.assertEqual(kwargs, dict(clearance=rules["fab"]["clearance_mm"]))
        self.assertEqual(ChannelModel(graph, rules).clearance, rules["default_clearance_mm"])
        self.assertEqual(ChannelModel(graph, rules, **kwargs).clearance, kwargs["clearance"])

    def test_runner_options(self):
        import run

        self.assertEqual(run.legalize_environment(), {})
        self.assertEqual(
            run.legalize_environment(True, 1, True, 4, "1"),
            dict(
                PNR_GP_POLISH="1",
                PNR_GP_CHANNELS="1.0",
                PNR_POOL_SOURCE_CLAMP="1",
                PNR_LEGALIZE_HPWL="4.0",
                PNR_LEGALIZE_REORIENT="1",
            ),
        )
        self.assertEqual(
            run.legalize_environment(reorient="wire"), dict(PNR_LEGALIZE_REORIENT="wire")
        )
        with self.assertRaises(ValueError):
            run.legalize_environment(legalize_hpwl=0.0)
        args = run.parser().parse_args(
            ["--out", "x", "--gp-channels", "0.5", "--legalize-hpwl", "16"]
        )
        self.assertEqual((args.gp_channels, args.legalize_hpwl), (0.5, 16.0))
        self.assertFalse(args.gp_polish or args.legalize_reorient or args.pool_source_clamp)
        parse = run.parser().parse_args
        self.assertEqual(parse(["--out", "x", "--legalize-reorient"]).legalize_reorient, "1")
        self.assertEqual(
            parse(["--out", "x", "--legalize-reorient", "wire"]).legalize_reorient, "wire"
        )

    def test_trace_header_records_active_switches_only(self):
        from pnr.trace import board_header

        graph = fixture.load("04-inverter-leds-8")[0]
        with flags():
            self.assertNotIn("placement_switches", board_header(graph))
        with flags(PNR_LEGALIZE_HPWL="4"):
            self.assertEqual(board_header(graph)["placement_switches"], dict(LEGALIZE_HPWL=4.0))


class SameSeedTest(unittest.TestCase):
    def test_every_switch_on_reruns_to_the_same_board(self):
        from pnr.place.placer import place

        graph, constraints, rules = fixture.load("07-chaser-20")
        switches = dict(
            PNR_COMPACT="1",
            PNR_GP_CHANNELS="1",
            PNR_POOL_SOURCE_CLAMP="1",
            PNR_LEGALIZE_HPWL="4",
            PNR_LEGALIZE_REORIENT="1",
        )
        boards = []
        with flags(**switches):
            for _ in range(2):
                placed, report = place(
                    graph, constraints, seed=3, iters=80, spread=1.3, channel_rules=rules
                )
                self.assertTrue(report.legal, report.summary())
                boards.append(placed.to_json())
        self.assertEqual(boards[0], boards[1])


class FlagOffIdentityTest(unittest.TestCase):
    """With the switches unset the placer, the legalizer and the initial pool never call a
    new function, and reproduce the parent commit's digests on 04-inverter-leds-8 and
    07-chaser-20 (the legalizer and the pool's starts on every platform, the whole placer on
    every arm64 platform with a golden: global placement is platform-independent there,
    pnr.place.portable_math)."""

    golden = json.loads((fixture.DATA / "identity.json").read_text())

    def test_flag_off_paths_and_digests(self):
        from pnr.place import gp_polish, initial_pool, legalize, line_group, model, reorient

        def refuse(name):
            def call(*args, **kwargs):
                raise AssertionError("%s called with the switches unset" % name)

            return call

        guarded = [
            (legalize, "wire_cost"),
            (legalize, "wire_turns"),
            (legalize, "plane_nets"),
            (legalize, "_wire_column"),
            (reorient, "reorient"),
            (reorient, "matched_refs"),
            (line_group, "satellites"),
            (line_group, "satellite_layout"),
            (gp_polish, "Polish"),
            (model, "_freeze"),
            (initial_pool, "_clamp_start"),
        ]
        key = fixture.platform_key()
        with flags(), contextlib.ExitStack() as stack:
            for module, name in guarded:
                stack.enter_context(mock.patch.object(module, name, refuse(name)))
            for case in fixture.CASES:
                with self.subTest(case=case):
                    self.assertEqual(fixture.legal_digest(case), self.golden[case]["legal"])
                    digest = fixture.place_digest(case)  # runs the placer either way
                    if key in self.golden[case]["place"]:
                        self.assertEqual(digest, self.golden[case]["place"][key])


if __name__ == "__main__":
    unittest.main()
