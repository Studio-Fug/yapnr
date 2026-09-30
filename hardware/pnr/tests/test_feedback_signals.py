"""pnr.feedback.signals on real (trimmed) nb5 converter rounds.

Fixtures (testdata/feedback/rounds/) are the same converter layout s1-30x30
evaluated without and with PNR_SHOVE, plus layout s1-22.25x33.25 without shove:
feedback.json targets and shove event statuses, evaluated refs/addresses, the
objective, the native budget and the fab profile, as the runs wrote them.
"""

import json
import tempfile
import unittest
from pathlib import Path

from pnr.feedback import signals
from pnr.feedback.blocks import block_key
from pnr.hier.blocks import Block

ROUNDS = Path(__file__).resolve().parents[1] / "testdata" / "feedback" / "rounds"
CONVERTER = Block(name="board.converter", refs=[], prefix="board.converter")


def read(name, key=True):
    return signals.read_round(ROUNDS / name, key=block_key(CONVERTER) if key else None)


class ReadRoundTest(unittest.TestCase):
    def test_same_part_targets_are_counted_not_acted_on(self):
        fb = read("noshove-s1-30x30")
        self.assertEqual(fb["opens"], 3)
        self.assertEqual(fb["same_part"], dict(count=1, ids=["ic.21|ic.25"]))  # U5.21 - U5.25
        self.assertEqual(
            [c["id"] for c in fb["conns"]], ["bootstrap2._p.2|inductor.2", "ic.23|inductor.1"]
        )
        self.assertEqual(fb["endpoints"], {"bootstrap2._p": 1, "ic": 1, "inductor": 2})
        self.assertEqual(fb["objective"], [0, 0, 0, 42, 0, 3])
        self.assertEqual(fb["pose_source"], "evaluated")

    def test_keys_are_block_local_and_ids_canonical_across_routers(self):
        plain, shove = read("noshove-s1-30x30"), read("shove-s1-30x30")
        both = {c["id"] for c in plain["conns"]} & {c["id"] for c in shove["conns"]}
        self.assertEqual(both, {"ic.23|inductor.1"})  # U5.23 - L2.1 in both runs
        for fb in (plain, shove):
            for c in fb["conns"]:
                self.assertEqual(c["id"], signals.conn_id(c["a"], c["b"]))
                self.assertLess("%s.%s" % tuple(c["a"]), "%s.%s" % tuple(c["b"]))
                self.assertFalse(
                    c["a"][0][:1].isupper() and c["a"][0][1:].isdigit()
                )  # not a designator
        refs = read("noshove-s1-30x30", key=False)
        self.assertEqual([c["id"] for c in refs["conns"]], ["C9.2|L2.2", "L2.1|U5.23"])

    def test_router_marker_budget_and_fab(self):
        plain, shove = read("noshove-s1-30x30"), read("shove-s1-30x30")
        self.assertEqual((plain["router"], shove["router"]), ("plain", "shove"))
        self.assertEqual((plain["budget_seconds"], plain["fab_profile"]), (600.0, "jlc-pofv"))

    def test_no_room_marks_shove_no_make_room_targets(self):
        fb = read("shove-s1-30x30")
        flags = {c["id"]: c["no_room"] for c in fb["conns"]}
        self.assertTrue(flags["ic.23|inductor.1"])  # U5.23 -> L2.1: no_make_room events
        self.assertFalse(flags["enable_top._p.2|ic.4"])  # U5.4 -> R6.2: never attempted
        self.assertEqual(sum(flags.values()), 1)
        self.assertEqual(fb["same_part"]["count"], 0)
        self.assertTrue(all(not c["no_room"] for c in read("noshove-s1-30x30")["conns"]))

    def test_missing_or_broken_round_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(signals.read_round(d), dict(missing=True, dir=str(Path(d))))
            (Path(d) / "feedback.json").write_text("{not json")
            self.assertTrue(signals.read_round(d)["missing"])
            (Path(d) / "feedback.json").write_text(
                json.dumps(dict(targets=[dict(source="A.1", target="B.2")]))
            )
            fb = signals.read_round(d)
            self.assertEqual([c["id"] for c in fb["conns"]], ["A.1|B.2"])
            self.assertIsNone(fb["pose_source"])

    def test_merge_over_instances(self):
        a, b = read("noshove-s1-30x30"), read("noshove-s1-22.25x33.25")
        m = signals.merge([a, b])
        self.assertEqual(
            {c["id"] for c in m["conns"]},
            {c["id"] for c in a["conns"]} | {c["id"] for c in b["conns"]},
        )
        self.assertEqual(m["opens"], 5)
        self.assertTrue(signals.merge([a, dict(missing=True)])["missing"])
        self.assertIs(signals.merge([a]), a)


class RouterKeyTest(unittest.TestCase):
    def test_import_check(self):
        current = dict(
            router="plain",
            stage="block",
            budget_seconds=600.0,
            power_first=True,
            fab_profile="jlc-pofv",
        )
        fb = read("shove-s1-30x30")
        errors, _ = signals.check_import(
            signals.observed_key(fb, stage="block", power_first=True), current
        )
        self.assertEqual(errors, ["router 'shove' != this run 'plain'"])
        fb = read("noshove-s1-30x30")
        self.assertEqual(
            signals.check_import(
                signals.observed_key(fb, stage="block", power_first=True), current
            ),
            ([], []),
        )
        errors, warnings = signals.check_import(
            signals.observed_key(fb, stage="block", power_first=True),
            dict(current, budget_seconds=300.0),
        )
        self.assertEqual(len(errors), 1)
        errors, warnings = signals.check_import(
            signals.observed_key(fb, stage="block", power_first=True),
            dict(current, budget_seconds=300.0),
            "warn",
        )
        self.assertEqual((errors, len(warnings)), ([], 1))
        errors, warnings = signals.check_import(signals.observed_key(fb, stage="block"), current)
        self.assertEqual((errors, warnings), ([], ["power_first unknown for the import"]))

    def test_current_key_from_environment(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1", "PNR_POWER_FIRST": "1"}, clear=False):
            k = signals.current_key("native", 900)
        self.assertEqual(
            (k["router"], k["power_first"], k["budget_seconds"]), ("shove", True, 900.0)
        )
        self.assertIn("shove|native|900.0|True", signals.key_string(k))


MINI_TREE = {
    "pnr/__init__.py": "",
    "pnr/full_iteration.py": "import os\nfrom pnr.native_loop import run\nCMD = ['-m', 'pnr.shove']\n",
    "pnr/native_loop.py": "def run():\n    if os.environ.get('PNR_SHOVE') == '1':\n        from pnr.shove.targets import t\n"
    "    from .helpers import h\n",
    "pnr/helpers.py": "h = 1\n",
    "pnr/unused.py": "x = 1\n",
    "pnr/shove/__init__.py": "",
    "pnr/shove/__main__.py": "from pnr.shove.ladder import run\n",
    "pnr/shove/ladder.py": "from pnr.shove.world import line_length_limit\n",
    "pnr/shove/world.py": "def line_length_limit(n):\n    return min(.5, max(.05 * n, .15))\n",
    "pnr/shove/targets.py": "t = 1\n",
    "pnr/hier/__init__.py": "",
    "pnr/hier/native_block.py": "from pnr.hier import blocks\n",
    "pnr/hier/blocks.py": "",
    "pnr/hier/synth.py": "def f():\n    from pnr.hier.synth_native import main\n    from pnr.mc.halving import _load\n",
    "pnr/hier/synth_native.py": "from pnr.feedback import signals\nfrom pnr import unused\n",
    "pnr/hier/top.py": "",
    "pnr/mc/__init__.py": "",
    "pnr/mc/halving.py": "",
    "pnr/feedback/__init__.py": "",
    "pnr/feedback/signals.py": "",
}


def make_tree(root):
    for rel, text in MINI_TREE.items():
        f = Path(root) / "hardware" / "pnr" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return Path(root) / "hardware" / "pnr"


def make_round(d, tree, via="origins"):
    """A round dir whose evaluation 'started' now: an evaluation.json is written as well, because
    Linux has no st_birthtime and observed_code then falls back to that file's mtime (see #10)."""
    ato = str(Path(tree) / "hardware/splanc_dev/elec/src/splanc_mini.ato")
    Path(d).mkdir(parents=True, exist_ok=True)
    (Path(d) / "evaluation.json").write_text("{}")
    if via == "origins":
        o = Path(d) / "electrical" / "native-loop" / "source-inputs"
        o.mkdir(parents=True)
        (o / "origins.json").write_text(json.dumps([dict(original=ato, snapshot="s", sha256="x")]))
    else:
        (Path(d) / "electrical").mkdir(parents=True)
        (Path(d) / "electrical" / "coalesce.json").write_text(
            json.dumps(dict(protected_intents=[dict(source=dict(path=ato))]))
        )


class CodeKeyTest(unittest.TestCase):
    def test_closure_follows_evaluation_imports_and_stops_at_drivers(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_tree(d)
            plain = set(signals.eval_code_files(root, "plain"))
            self.assertEqual(
                plain,
                {
                    "pnr",
                    "pnr.full_iteration",
                    "pnr.native_loop",
                    "pnr.helpers",
                    "pnr.hier",
                    "pnr.hier.native_block",
                    "pnr.hier.blocks",
                    "pnr.hier.synth",
                },
            )
            shove = set(signals.eval_code_files(root, "shove"))
            # ``-m pnr.shove`` runs the package __main__, which reaches world.py through the ladder
            self.assertEqual(
                shove - plain,
                {
                    "pnr.shove",
                    "pnr.shove.__main__",
                    "pnr.shove.ladder",
                    "pnr.shove.world",
                    "pnr.shove.targets",
                },
            )
            before = (signals.code_key(root, "plain"), signals.code_key(root, "shove"))
            files = signals.eval_code_files(root, "shove")
            # src10.frozen's world.py: the shove key changes, the plain key does not
            (root / "pnr/shove/world.py").write_text(
                "def line_length_limit(n):\n    return min(.5, .05 * n)\n"
            )
            self.assertEqual(signals.code_key(root, "plain"), before[0])
            self.assertNotEqual(signals.code_key(root, "shove"), before[1])
            self.assertEqual(
                signals.code_diff(files, signals.eval_code_files(root, "shove")),
                ["pnr.shove.world"],
            )
            # a driver-only edit changes neither key
            (root / "pnr/hier/synth_native.py").write_text("# edited\n")
            (root / "pnr/feedback/signals.py").write_text("# edited\n")
            self.assertEqual(signals.code_key(root, "plain"), before[0])

    def test_real_tree(self):
        shove = signals.eval_code_files(signals.PNR_ROOT, "shove")
        plain = signals.eval_code_files(signals.PNR_ROOT, "plain")
        for module in (
            "pnr.full_iteration",
            "pnr.native_loop",
            "pnr.native_electrical",
            "pnr.hier.native_block",
        ):
            self.assertIn(module, plain)
        self.assertIn("pnr.shove.world", shove)
        self.assertFalse(any(k.split(".")[:2] == ["pnr", "shove"] for k in plain))
        for f in (shove, plain):
            self.assertFalse(
                any(k.split(".")[:2] in (["pnr", "feedback"], ["pnr", "mc"]) for k in f)
            )
            self.assertNotIn("pnr.hier.synth_native", f)
            self.assertNotIn("pnr.hier.top", f)
        k = signals.current_key("block", 600)
        self.assertEqual(k["code"], signals.code_key(signals.PNR_ROOT, k["router"]))
        self.assertTrue(signals.key_string(k).endswith("|%s|2" % k["code"]))

    def test_observed_code_from_the_round(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as d:
            tree = Path(d) / "srcX"
            root = make_tree(tree)
            for via in ("origins", "coalesce"):
                rnd = Path(d) / ("round-" + via)
                make_round(rnd, tree, via)
                o = signals.observed_code(rnd, "shove")
                self.assertEqual(
                    (o["code"], o["tree"], o["reason"]),
                    (signals.code_key(root, "shove"), str(tree), "tree"),
                )
            self.assertEqual(signals.observed_code(rnd, "plain", stamped="abc")["code"], "abc")
            # the tree changed after the evaluation started: its code now says nothing about the round
            later = time.time() + 30
            os.utime(root / "pnr/shove/world.py", (later, later))
            o = signals.observed_code(Path(d) / "round-origins", "shove")
            self.assertIsNone(o["code"])
            self.assertIn("changed after the evaluation (pnr/shove/world.py)", o["reason"])
            self.assertIsNotNone(
                signals.observed_code(Path(d) / "round-origins", "plain")["code"]
            )  # not in plain
            # nothing recorded / tree gone
            self.assertIn("not recorded", signals.observed_code(d, "plain")["reason"])
            gone = Path(d) / "round-gone"
            make_round(gone, Path(d) / "nowhere")
            self.assertIn("is gone", signals.observed_code(gone, "plain")["reason"])

    def test_check_code_policies(self):
        same = dict(code="abc")
        self.assertEqual(signals.check_code(same, "abc"), ([], [], False))
        other = dict(code="old", code_key_scheme=2, files={"pnr.a": "1", "pnr.shove.world": "2"})
        cur = {"pnr.a": "1", "pnr.shove.world": "3"}
        errors, _, stale = signals.check_code(other, "new", cur)
        self.assertEqual(errors, ["code old != this run new (differs: pnr.shove.world)"])
        self.assertFalse(stale)
        errors, warnings, stale = signals.check_code(other, "new", cur, "warn")
        self.assertEqual((errors, len(warnings), stale), ([], 1, False))
        errors, warnings, stale = signals.check_code(
            dict(code=None, reason="x"), "new", cur, "rebase"
        )
        self.assertEqual((errors, stale), ([], True))
        self.assertIn("code unknown (x)", warnings[0])


if __name__ == "__main__":
    unittest.main()
