"""PNR_GLOSS wiring in pnr.native_loop (PnR runtime python; KiCad workers and DRC stubbed).

stub_run() drives the real native_loop controller with pnr.proc.run_status and
pnr.native_drc.run_drc replaced: workers write canned reports, the gloss pass writes
a canned result. It also serves as the harness of a flag-off golden comparison: run
it against two trees (with and without the gloss hunks) and compare the normalized
progress.json, worker calls and outputs.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

GLOSS_ENV = [k for k in os.environ if k.startswith("PNR_")]


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def stub_run(
    root,
    *,
    electrical=True,
    gate_fail=False,
    nested=False,
    only_mode=None,
    argv_extra=(),
    rules=None,
):
    """Run native_loop.main on a stub board; returns (result, progress or None, calls).
    rules: a rules file whose content the run uses (default: the fab profile only)."""
    import pnr.native_drc
    import pnr.proc
    from pnr import native_loop
    from pnr.fab_profile import active_name

    root = Path(root)
    (root / "in").mkdir(parents=True)
    board = root / "in" / "board.kicad_pcb"
    board.write_text("(kicad_pcb (version 1))\n")
    board.with_suffix(".kicad_pro").write_text("{}\n")
    content = json.loads(Path(rules).read_text()) if rules else {}
    rules = root / "in" / "rules.json"
    save(rules, dict(content, fab_profile=active_name()))
    repo = root / "repo"
    (repo / "hardware/tools").mkdir(parents=True)
    (repo / "hardware/tools/keyhole_region.py").write_text("")
    constraints = root / "in" / "constraints.yaml"
    constraints.write_text("{}\n")
    fab = root / "in" / "fab.json"
    fab.write_text("{}\n")
    calls = []

    def fake_run_status(cmd, timeout=None, session=True, **kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        if "--worker" in cmd:
            mode = cmd[cmd.index("--worker") + 1]
            report = Path(cmd[cmd.index("--report") + 1])
            if mode == "prepare":
                save(report, json.loads(rules.read_text()))
            elif mode == "inspect":
                save(
                    report,
                    dict(
                        item_nets={},
                        footprint_poses={},
                        excluded=[],
                        targets=[],
                        owners={},
                        partition=[],
                        entries={},
                    ),
                )
            elif mode == "check":
                save(
                    report,
                    dict(
                        preserved=True,
                        lost_pad_entries=[],
                        lost_connections=[],
                        reference_failures=[],
                    ),
                )
            else:
                save(report, {})
        elif "-m" in cmd and cmd[cmd.index("-m") + 1] == "pnr.gloss":
            out = Path(cmd[cmd.index("--out") + 1])
            shutil.copyfile(cmd[cmd.index("-m") + 2], out)
            out.with_suffix(".kicad_pro").write_text("{}\n")
            label = cmd[cmd.index("--label") + 1]
            save(
                cmd[cmd.index("--report") + 1],
                dict(
                    label=label,
                    status="ok",
                    accepted_transactions=1,
                    proposed_transactions=1,
                    transactions=[],
                    steps_report={},
                    inverse_specs=[],
                    created_uuids=[],
                ),
            )
        return 0, False

    def fake_drc(cli, board, report, **kw):
        board = Path(board)
        env = kw.get("env") or {}
        calls.append(
            [
                "drc",
                str(board),
                str(report),
                "final=%s" % kw.get("final"),
                "PYTHONPATH=%s" % env.get("PYTHONPATH"),
            ]
        )
        opens = (
            1
            if gate_fail
            and board.parent.name.endswith("-gloss")
            and board.name == "candidate.kicad_pcb"
            else 0
        )
        result = dict(violations=[], unconnected_items=[dict(items=[])] * opens)
        Path(report).write_text(json.dumps(result))
        return result

    argv = [
        str(board),
        "--repo",
        str(repo),
        "--rules",
        str(rules),
        "--constraints",
        str(constraints),
        "--out-dir",
        str(root / "out"),
        "--kicad-python",
        "kicad-python",
        "--kicad-cli",
        "kicad-cli",
        "--route-only",
        "--cycles",
        "1",
    ]
    if electrical:
        argv += ["--electrical-fab", str(fab)]
    if only_mode:
        argv += ["--only-mode", only_mode]
    argv += list(argv_extra)
    cwd = os.getcwd()
    try:
        with mock.patch.object(pnr.proc, "run_status", fake_run_status), mock.patch.object(
            pnr.native_drc, "run_drc", fake_drc
        ):
            result = native_loop.main(argv, nested=nested)
    finally:
        os.chdir(cwd)
    progress = root / "out" / "progress.json"
    return result, (json.loads(progress.read_text()) if progress.exists() else None), calls


def normalize(value, root):
    """Strip run-specific paths and clocks (golden comparisons across trees)."""
    text = (
        json.dumps(value, sort_keys=True)
        .replace(str(Path(root).resolve()), "<root>")
        .replace(str(root), "<root>")
    )
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("elapsed_seconds", "prerequisite_seconds"):
            if key in data:
                data[key] = 0
    return data


class Env:
    """Run with exactly these PNR_* variables (everything else PNR_* removed)."""

    def __init__(self, **flags):
        self.flags = flags

    def __enter__(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("PNR_")}
        os.environ.update(self.flags)

    def __exit__(self, *exc):
        for k in [k for k in os.environ if k.startswith("PNR_")]:
            del os.environ[k]
        os.environ.update(self.saved)


class GlossLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_off_never_imports_gloss_and_keeps_labels(self):
        sys.modules.pop("pnr.gloss", None)
        from pnr import native_loop

        self.assertEqual(
            native_loop.PHASE_LABELS,
            (
                "02-usb-pairs",
                "03-early-power",
                "04-early-plane",
                "05-early-power-refine",
                "06-signals",
                "06b-power-bank-consolidation",
                "07-native-refinement",
                "08b-power-bank-consolidation",
            ),
        )
        self.assertFalse(set(native_loop.GLOSS_LABELS) & set(native_loop.PHASE_LABELS))
        with Env(
            PNR_GLOSS_STEPS="normalize", PNR_GLOSS_PASSES="06g-gloss"
        ):  # sub-flags alone do nothing
            result, progress, calls = stub_run(self.root / "off")
        self.assertNotIn("pnr.gloss", sys.modules)
        self.assertNotIn("gloss", progress)
        self.assertFalse(any("pnr.gloss" in c for cmd in calls for c in cmd))
        self.assertFalse((self.root / "off/out/06g-gloss").exists())
        self.assertEqual(
            sorted(progress),
            sorted(
                [
                    "source",
                    "source_sha256",
                    "best",
                    "best_sha256",
                    "initial_opens",
                    "opens",
                    "rounds",
                    "events",
                    "component_scores",
                    "termination",
                    "elapsed_seconds",
                    "prerequisite_seconds",
                    "native_open_nets",
                    "protected_open_nets",
                    "electrical_modes_enabled",
                    "budgets",
                ]
            ),
        )
        self.assertEqual(progress["events"], [])
        with Env(PNR_STOP_AFTER_PHASE="06g-gloss"), self.assertRaises(SystemExit):
            stub_run(self.root / "stop-off")
        self.assertNotIn("pnr.gloss", sys.modules)

    def test_gloss_modules_are_in_the_code_key_closure_only_with_the_flag(self):
        # native_loop and full_iteration import pnr.gloss only under PNR_GLOSS=1, as the loop
        # imports pnr.shove only under PNR_SHOVE=1: flag-off code keys never change with gloss
        # code; with the flag the key covers the pass and the one shove module it runs.
        from pnr.feedback import signals

        for router in ("plain", "shove"):
            off = signals.eval_code_modules(signals.PNR_ROOT, router, gloss=False)
            on = signals.eval_code_modules(signals.PNR_ROOT, router, gloss=True)
            self.assertFalse({"pnr.gloss", "pnr.gloss_geometry"} & set(off))
            self.assertLessEqual(
                {"pnr.gloss", "pnr.gloss_geometry", "pnr.shove", "pnr.shove.gates"}, set(on)
            )
            extra = set(on) - set(off)
            if router == "plain":
                self.assertEqual(
                    extra, {"pnr.gloss", "pnr.gloss_geometry", "pnr.shove", "pnr.shove.gates"}
                )
            else:
                self.assertEqual(extra, {"pnr.gloss", "pnr.gloss_geometry"})
        with Env():
            self.assertEqual(
                signals.code_key(signals.PNR_ROOT, "plain"),
                signals.code_key(signals.PNR_ROOT, "plain", gloss=False),
            )
            off_key = signals.TreeCode(signals.PNR_ROOT, "plain").key()
        with Env(PNR_GLOSS="1"):
            on_key = signals.TreeCode(signals.PNR_ROOT, "plain").key()
            self.assertEqual(on_key, signals.code_key(signals.PNR_ROOT, "plain", gloss=True))
        self.assertNotEqual(off_key, on_key)

    def test_router_key_and_imports_separate_gloss_arms(self):
        # the two arms of a gloss A/B (and two gloss configurations) never share a router key,
        # and an evaluation of one arm is never imported into the other
        from pnr import gloss
        from pnr.feedback import signals

        with Env():
            off = signals.current_key("native", 900)
        self.assertIsNone(off["gloss"])
        self.assertNotIn("gloss=", signals.key_string(off))  # flag-off strings as before
        self.assertTrue(signals.key_string(off).endswith("|%s|2" % off["code"]))
        with Env(PNR_GLOSS="1"):
            on = signals.current_key("native", 900)
        with Env(PNR_GLOSS="1", PNR_GLOSS_STEPS="dekink"):
            dekink = signals.current_key("native", 900)
        self.assertEqual(on["gloss"], gloss.settings_key(gloss.settings({})))
        self.assertNotEqual(on["gloss"], dekink["gloss"])
        self.assertIn("|gloss=%s|%s|2" % (on["gloss"], on["code"]), signals.key_string(on))
        self.assertNotEqual(signals.key_string(on), signals.key_string(dekink))
        # a groups file is keyed by its content, not its path
        a, b = self.root / "a.json", self.root / "b" / "a.json"
        b.parent.mkdir()
        for f in (a, b):
            f.write_text(json.dumps({"classes": {"g": ["N1", "N2"]}}))
        keys = [gloss.settings_key(gloss.settings(dict(PNR_GLOSS_CLASSES=str(f)))) for f in (a, b)]
        self.assertEqual(keys[0], keys[1])
        b.write_text(json.dumps({"classes": {"g": ["N1", "N3"]}}))
        self.assertNotEqual(
            keys[0], gloss.settings_key(gloss.settings(dict(PNR_GLOSS_CLASSES=str(b))))
        )
        # imports: the record's gloss key (progress.json) must equal this run's
        fb = dict(router="plain", budget_seconds=900, fab_profile=on["fab_profile"])
        for record, current, refused in (
            (None, off, False),
            (None, on, True),
            (on["gloss"], off, True),
            (on["gloss"], on, False),
            (dekink["gloss"], on, True),
        ):
            errors, _ = signals.check_import(
                signals.observed_key(dict(fb, gloss=record), stage="native", power_first=False),
                current,
            )
            self.assertEqual(any(e.startswith("gloss ") for e in errors), refused, errors)

    def test_flag_on_runs_both_passes_and_records_them(self):
        with Env(PNR_GLOSS="1"):
            result, progress, calls = stub_run(self.root / "on")
        gloss = [c for c in calls if "-m" in c and c[c.index("-m") + 1] == "pnr.gloss"]
        self.assertEqual([c[c.index("--label") + 1] for c in gloss], ["06g-gloss", "07g-gloss"])
        self.assertTrue(all(c[0] == sys.executable and "--kicad-python" in c for c in gloss))
        self.assertEqual(sorted(progress["gloss"]), ["06g-gloss", "07g-gloss"])
        from pnr import gloss as gloss_module

        self.assertEqual(
            progress["gloss_key"], gloss_module.settings_key(gloss_module.settings({}))
        )
        events = [e for e in progress["events"] if e["stage"] == "gloss_pass"]
        self.assertEqual([e["phase"] for e in events], ["06g-gloss", "07g-gloss"])
        self.assertTrue(all(e["accepted"] for e in events))
        # each pass reports its cost (wall and CPU of its workers and the outer gate)
        for label, row in progress["gloss"].items():
            self.assertEqual(sorted(row["cost"]), ["cpu_seconds", "wall_seconds"])
            self.assertGreaterEqual(row["cost"]["wall_seconds"], 0)

    def test_passes_subflag_and_skips(self):
        with Env(PNR_GLOSS="1", PNR_GLOSS_PASSES="07g-gloss"):
            _, progress, calls = stub_run(self.root / "late")
        self.assertEqual(sorted(progress["gloss"]), ["07g-gloss"])
        for name, kw in (
            ("nested", dict(nested=True)),
            ("only", dict(only_mode="signal")),
            ("nofab", dict(electrical=False)),
        ):
            with Env(PNR_GLOSS="1"):
                _, progress, calls = stub_run(self.root / name, **kw)
            self.assertFalse(any("pnr.gloss" in c for cmd in calls for c in cmd), name)

    def test_failing_outer_gate_keeps_input_board(self):
        with Env(PNR_GLOSS="1"):
            result, progress, calls = stub_run(self.root / "gate", gate_fail=True)
        events = [e for e in progress["events"] if e["stage"] == "gloss_pass"]
        self.assertEqual(
            [(e["accepted"], e.get("status")) for e in events],
            [(False, "outer_gate"), (False, "outer_gate")],
        )
        self.assertEqual(
            Path(result).read_bytes(), (self.root / "gate/in/board.kicad_pcb").read_bytes()
        )

    def test_stop_after_gloss_label_with_flag(self):
        with Env(PNR_GLOSS="1", PNR_STOP_AFTER_PHASE="06g-gloss"):
            result, progress, calls = stub_run(self.root / "stop")
        self.assertEqual(progress["termination"], "stopped_after_phase")
        self.assertEqual(progress["stopped_after_phase"], "06g-gloss")
        # an unknown label: the error lists the gloss labels only with the flag
        for flags, listed in ((dict(PNR_GLOSS="1"), True), ({}, False)):
            err = io.StringIO()
            with Env(PNR_STOP_AFTER_PHASE="07x", **flags), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    stub_run(self.root / ("stop-bad-%s" % listed))
            self.assertEqual("07g-gloss" in err.getvalue(), listed, err.getvalue())

    def test_settings(self):
        from pnr import gloss

        conf = gloss.settings({})
        self.assertEqual(conf.steps, ("normalize", "dekink", "gloss", "corridor"))
        self.assertEqual(conf.passes, ("06g-gloss", "07g-gloss"))
        self.assertEqual(
            (conf.seconds, conf.max_transactions, conf.batch, conf.si, conf.cycle),
            (240, 48, 16, False, False),
        )
        conf = gloss.settings(
            dict(PNR_GLOSS_STEPS="corridor,dekink", PNR_GLOSS_SI="1", PNR_GLOSS_BATCH="4")
        )
        self.assertEqual(conf.steps, ("dekink", "corridor"))  # fixed order
        self.assertEqual((conf.si, conf.batch), (True, 4))
        with self.assertRaises(ValueError):
            gloss.settings(dict(PNR_GLOSS_STEPS="polish"))
        self.assertFalse(gloss.enabled({}))
        self.assertTrue(gloss.enabled(dict(PNR_GLOSS="1")))
        self.assertEqual((conf.hug, conf.sweeps, conf.classes), (True, 2, None))
        conf = gloss.settings(dict(PNR_GLOSS_HUG="0", PNR_GLOSS_SWEEPS="1"))
        self.assertEqual((conf.hug, conf.sweeps), (False, 1))
        for bad in (
            dict(PNR_GLOSS_CYCLE="1"),
            dict(PNR_GLOSS_SECONDS="0"),
            dict(PNR_GLOSS_SECONDS="abc"),
            dict(PNR_GLOSS_BATCH="2.5"),
            dict(PNR_GLOSS_SI="yes"),
            dict(PNR_GLOSS_SWEEPS="3"),
            dict(PNR_GLOSS_CLASSES="/nonexistent/classes.json"),
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                gloss.settings(bad)
        self.assertFalse(gloss.settings(dict(PNR_GLOSS_CYCLE="0")).cycle)

    def test_cross_group_settings(self):
        from pnr import gloss

        groups = Path(tempfile.mkdtemp()) / "groups.json"
        groups.write_text(
            json.dumps(
                {
                    "classes": {
                        "bus": {"nets": ["bus_d", "bus_c"], "provenance": []},
                        "ctl": ["ctl_en"],
                    }
                }
            )
        )
        self.assertEqual(gloss.settings({}).cross_group_mm, 10.0)
        conf = gloss.settings(dict(PNR_GLOSS_CLASSES=str(groups)))
        self.assertEqual((conf.classes, conf.cross_group_mm), (str(groups.resolve()), 10.0))
        self.assertEqual(
            gloss.settings(
                dict(PNR_GLOSS_CLASSES=str(groups), PNR_GLOSS_CROSS_GROUP_MM="4.5")
            ).cross_group_mm,
            4.5,
        )
        self.assertEqual(
            gloss.settings(
                dict(PNR_GLOSS_CLASSES=str(groups), PNR_GLOSS_CROSS_GROUP_MM="0")
            ).cross_group_mm,
            0.0,
        )
        self.assertEqual(
            gloss.load_class_tags(str(groups)), {"bus_d": "bus", "bus_c": "bus", "ctl_en": "ctl"}
        )
        twice = groups.with_name("twice.json")
        twice.write_text(json.dumps({"classes": {"a": ["x"], "b": ["x"]}}))
        broken = groups.with_name("broken.json")
        broken.write_text('{"classes": ')
        for bad in (
            dict(PNR_GLOSS_CROSS_GROUP_MM="10"),  # a cap without groups
            dict(PNR_GLOSS_CLASSES=str(groups), PNR_GLOSS_CROSS_GROUP_MM="-1"),
            dict(PNR_GLOSS_CLASSES=str(groups), PNR_GLOSS_CROSS_GROUP_MM="inf"),
            dict(PNR_GLOSS_CLASSES=str(groups), PNR_GLOSS_CROSS_GROUP_MM="ten"),
            dict(PNR_GLOSS_CLASSES=str(twice)),
            dict(PNR_GLOSS_CLASSES=str(broken)),
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                gloss.settings(bad)
        # the controller hands the groups file and the cap to every worker
        a = mock.Mock(
            annotation_source=[],
            guard_open_nets=False,
            work_dir=groups.parent / "w",
            rules=groups,
            seconds=600,
            kicad_python="kp",
        )
        import dataclasses

        p = gloss.GlossPass(a, dataclasses.replace(conf, cross_group_mm=4.5))
        with mock.patch("pnr.proc.run_status", return_value=(0, False)) as run, mock.patch.object(
            gloss, "read", return_value={}
        ):
            p.run_worker("inventory", "b.kicad_pcb", groups.parent / "r.json")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--classes") + 1], str(groups.resolve()))
        self.assertEqual(cmd[cmd.index("--cross-group-mm") + 1], "4.5")

    def test_derived_groups(self):
        from pnr import gloss

        rules = dict(
            net_classes=[
                dict(name="power", nets=["VCC", "GND"]),
                dict(name="usb", nets=["DP", "DN"]),
                dict(name="empty", nets=[]),
            ],
            diff_pairs=[dict(name="usb", p="DP", n="DN")],
            length_match=[dict(name="bus", nets=["D0", "D1", "D2"], tolerance_mm=0.5)],
        )
        intents = [dict(name="led", nets=["DIN", "DOUT"])]
        tags = gloss.derive_class_tags(rules, gloss.CLASS_SOURCES, intents)
        self.assertEqual(
            tags,
            {
                "VCC": "netclass:power",
                "GND": "netclass:power",
                "DP": "netclass:usb",  # the pair is also its own net class: one group
                "DN": "netclass:usb",
                "D0": "length_match:bus",
                "D1": "length_match:bus",
                "D2": "length_match:bus",
                "DIN": "si:led",
                "DOUT": "si:led",
            },
        )
        self.assertEqual(
            gloss.derive_class_tags(rules, ("pairs",)), {"DP": "pair:usb", "DN": "pair:usb"}
        )
        self.assertEqual(gloss.derive_class_tags(rules, ("si",)), {})  # no intents given
        # a net derived into groups with different members joins the most specific one: a pair
        # inside a wider net class is its own group, the class keeps its other nets
        wide = dict(rules, net_classes=[dict(name="hs", nets=["DP", "DN", "CLK", "STB"])])
        self.assertEqual(
            gloss.derive_class_tags(wide, ("netclasses", "pairs")),
            {"CLK": "netclass:hs", "DN": "pair:usb", "DP": "pair:usb", "STB": "netclass:hs"},
        )
        self.assertEqual(
            gloss.derive_class_tags(wide, ("pairs", "netclasses")),
            gloss.derive_class_tags(wide, ("netclasses", "pairs")),
        )
        # equal sizes: the first source (CLASS_SOURCES order) wins
        clash = dict(rules, length_match=[dict(name="bus", nets=["DP", "D0"])])
        self.assertEqual(
            gloss.derive_class_tags(clash, ("length_match", "pairs")),
            {"D0": "length_match:bus", "DN": "pair:usb", "DP": "pair:usb"},
        )
        # the controller start validates the groups against the rules (si needs the board)
        conf = gloss.settings(dict(PNR_GLOSS_CLASSES_FROM="netclasses,pairs,si"))
        self.assertEqual(gloss.check_groups(conf, wide)["DP"], "pair:usb")
        self.assertIsNone(gloss.check_groups(gloss.settings({}), wide))
        with self.assertRaises(ValueError):
            gloss.check_groups(conf, dict(net_classes=[dict(name="x", nets="DP")]))
        # a groups file entry wins for its net; neither source: no groups (no cap)
        groups = Path(tempfile.mkdtemp()) / "groups.json"
        groups.write_text(json.dumps({"classes": {"sense": ["DP"]}}))
        tags = gloss.class_tags(str(groups), ("pairs",), rules)
        self.assertEqual(tags, {"DP": "sense", "DN": "pair:usb"})
        self.assertIsNone(gloss.class_tags(None, (), rules))
        self.assertEqual(gloss.class_tags(None, ("pairs",), {}), {})  # every net a singleton
        # settings: the source list (fixed order), the cap with derived groups only
        conf = gloss.settings(dict(PNR_GLOSS_CLASSES_FROM="si,netclasses"))
        self.assertEqual(conf.classes_from, ("netclasses", "si"))
        self.assertEqual(gloss.settings({}).classes_from, ())
        conf = gloss.settings(dict(PNR_GLOSS_CLASSES_FROM="pairs", PNR_GLOSS_CROSS_GROUP_MM="5"))
        self.assertEqual(conf.cross_group_mm, 5.0)
        with self.assertRaises(ValueError):
            gloss.settings(dict(PNR_GLOSS_CLASSES_FROM="netclass"))
        # the controller hands the sources to every worker
        a = mock.Mock(
            annotation_source=[],
            guard_open_nets=False,
            work_dir=groups.parent / "w",
            rules=groups,
            seconds=600,
            kicad_python="kp",
        )
        p = gloss.GlossPass(a, gloss.settings(dict(PNR_GLOSS_CLASSES_FROM="netclasses,pairs")))
        with mock.patch("pnr.proc.run_status", return_value=(0, False)) as run, mock.patch.object(
            gloss, "read", return_value={}
        ):
            p.run_worker("inventory", "b.kicad_pcb", groups.parent / "r.json")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--classes-from") + 1], "netclasses,pairs")
        self.assertEqual(cmd[cmd.index("--cross-group-mm") + 1], "10.0")
        self.assertNotIn("--classes", cmd)

    def test_malformed_subflag_rejected_before_any_phase(self):
        for flags in (
            dict(PNR_GLOSS_STEPS="dekinks"),
            dict(PNR_GLOSS_SECONDS="0"),
            dict(PNR_GLOSS_CYCLE="1"),
        ):
            with Env(PNR_GLOSS="1", **flags), self.assertRaises(SystemExit):
                stub_run(self.root / ("bad-%d" % len(list(self.root.glob("bad-*")))))
        # groups the rules cannot give (a net class listing a string) stop it before any phase
        rules = self.root / "bad-rules.json"
        rules.write_text(json.dumps(dict(net_classes=[dict(name="x", nets="DP")])))
        with Env(PNR_GLOSS="1", PNR_GLOSS_CLASSES_FROM="netclasses"), self.assertRaises(SystemExit):
            stub_run(self.root / "bad-groups", rules=rules)
        # the same typo without the master flag is never read
        with Env(PNR_GLOSS_STEPS="dekinks"):
            result, progress, calls = stub_run(self.root / "typo-off")
        self.assertNotIn("gloss", progress)

    def test_bad_settings_inside_the_pass_keep_the_board(self):
        import pnr.gloss

        real = pnr.gloss.settings
        state = dict(n=0)

        def flaky(env=None):
            state["n"] += 1
            if state["n"] > 1:  # valid at controller start, broken later
                raise ValueError("PNR_GLOSS_STEPS: unknown x")
            return real(env)

        with Env(PNR_GLOSS="1"), mock.patch.object(pnr.gloss, "settings", flaky):
            result, progress, calls = stub_run(self.root / "flaky")
        events = [e for e in progress["events"] if e["stage"] == "gloss_pass"]
        self.assertEqual(
            [(e["phase"], e["status"]) for e in events],
            [("06g-gloss", "bad_settings"), ("07g-gloss", "bad_settings")],
        )
        self.assertFalse(any("-m" in c and c[c.index("-m") + 1] == "pnr.gloss" for c in calls))

    def test_open_net_guard_only_before_refinement(self):
        with Env(PNR_GLOSS="1"):
            _, _, calls = stub_run(self.root / "guard")
        gloss = {
            c[c.index("--label") + 1]: c
            for c in calls
            if "-m" in c and c[c.index("-m") + 1] == "pnr.gloss"
        }
        self.assertIn("--guard-open-nets", gloss["06g-gloss"])
        self.assertNotIn("--guard-open-nets", gloss["07g-gloss"])

    def test_every_worker_of_a_pass_reads_the_b0_drc_report(self):
        from pnr import gloss

        root = self.root / "b0"
        (root / "in").mkdir(parents=True)
        board = root / "in" / "board.kicad_pcb"
        board.write_text("(kicad_pcb (version 1))\n")
        board.with_suffix(".kicad_pro").write_text("{}\n")
        facts = dict(
            partition=[],
            entries={},
            entry_nets={},
            bad_entries=0,
            reference=[],
            subwidth=0,
            unjustified=[],
            forbidden=[],
            unqualified_pairs=0,
            skews={},
        )
        seen = []

        class Stub(gloss.GlossPass):
            def drc(self, path):
                result = dict(violations=[], unconnected_items=[dict(items=[dict(uuid=str(path))])])
                Path(path).with_suffix(".drc.json").write_text(json.dumps(result))
                return result

            def run_worker(self, mode, path, report, extra=()):
                extra = [str(x) for x in extra]
                drc = extra[extra.index("--drc") + 1] if "--drc" in extra else None
                seen.append((mode, drc))
                if mode == "facts":
                    return dict(facts)
                if mode == "inventory":
                    step = extra[extra.index("--step") + 1]
                    n = sum(1 for m, _ in seen if m == "inventory")
                    specs = (
                        [
                            dict(
                                id="%s:%d" % (step, n),
                                step=step,
                                nets=["n%d" % n],
                                layer="F.Cu",
                                gain=1.0,
                                box=[0, 0, 1, 1],
                            )
                        ]
                        if step == "dekink" and n <= 2
                        else []
                    )
                    return dict(specs=specs, stats={}, si_status="rules", seconds=0.1)
                spec = json.loads(Path(extra[extra.index("--spec") + 1]).read_text())
                out = Path(extra[extra.index("--out") + 1])
                out.write_text(Path(path).read_text())
                ids = [s["id"] for s in spec["specs"]]
                return dict(
                    dropped={},
                    applied={i: dict(created=[]) for i in ids},
                    accepted_specs=ids,
                    facts=dict(facts),
                )

        a = mock.Mock(
            board=board,
            rules=root / "in" / "rules.json",
            annotation_source=[],
            out=root / "out" / "c.kicad_pcb",
            work_dir=root / "work",
            report=root / "out" / "result.json",
            kicad_cli="cli",
            kicad_python="kp",
            label="t",
            seconds=600,
            metrics=False,
            guard_open_nets=False,
        )
        (root / "in" / "rules.json").write_text("{}")
        conf = gloss.Settings(
            steps=("dekink",),
            passes=gloss.LABELS,
            si=False,
            cycle=False,
            seconds=600,
            max_transactions=48,
            batch=16,
        )
        result = Stub(a, conf).run()
        self.assertEqual(result["accepted_transactions"], 2)  # the checkpoint moved twice
        b0 = str((root / "work" / "checkpoint-000" / "board.drc.json").resolve())
        workers = [(m, d) for m, d in seen if m in ("inventory", "trial")]
        self.assertGreaterEqual(len(workers), 5)
        self.assertEqual({d for _, d in workers}, {b0})
        self.assertIn("planning", result)
        self.assertEqual(result["planning"]["deadline_hits"], 0)

    def test_evaluation_side_fields_measure_the_groups_of_the_passes(self):
        # full_iteration's gloss-metrics worker gets the same functional groups as the passes
        import subprocess

        from pnr.full_iteration import gloss_side_fields

        metrics = dict(
            classes=dict(eligible=dict(length_mm=1.0, bends_all=2)),
            E_mm2=0.0,
            E_bbox_mm2=0.0,
            DS_mm2=0.5,
            US_mm2=0.0,
            layers={},
            cross_group=dict(max_mm=3.0),
        )
        calls = []

        def run(cmd, name, fail=False):
            calls.append([str(c) for c in cmd])
            if fail:
                raise subprocess.CalledProcessError(1, cmd)
            report = Path(calls[-1][calls[-1].index("--report") + 1])
            report.write_text(json.dumps(dict(metrics=metrics)))

        progress = dict(gloss={"07g-gloss": dict(accepted=True)})
        flags = dict(
            PNR_GLOSS="1", PNR_GLOSS_CLASSES_FROM="netclasses", PNR_GLOSS_CROSS_GROUP_MM="4"
        )
        with Env(**flags):
            fields = gloss_side_fields("b.kicad_pcb", "r.json", self.root, [], run, progress)
        cmd = calls[-1]
        self.assertEqual(cmd[cmd.index("--classes-from") + 1], "netclasses")
        self.assertEqual(cmd[cmd.index("--cross-group-mm") + 1], "4.0")
        self.assertEqual(fields["gloss_metrics"]["cross_group"], dict(max_mm=3.0))
        self.assertEqual(fields["gloss_summary"], progress["gloss"])
        with Env(PNR_GLOSS="1"):
            gloss_side_fields("b.kicad_pcb", "r.json", self.root, [], run, progress)
        self.assertNotIn("--cross-group-mm", calls[-1])
        with Env(PNR_GLOSS="1"):
            fields = gloss_side_fields(
                "b.kicad_pcb", "r.json", self.root, [], lambda c, n: run(c, n, True), {}
            )
        self.assertIn("CalledProcessError", fields["gloss_metrics"]["error"])
        self.assertIsNone(fields["gloss_summary"])

    def test_gloss_module_has_no_kicad_import_at_module_level(self):
        import ast

        import pnr.gloss

        tree = ast.parse(Path(pnr.gloss.__file__).read_text())
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {a.name for n in top for a in n.names} | {getattr(n, "module", None) for n in top}
        self.assertNotIn("pcbnew", names)

    def test_compare_gate(self):
        from pnr.gloss import compare

        facts = dict(
            partition=[["a", "b"]],
            entries={"p:0": True},
            entry_nets={"p:0": "n"},
            bad_entries=0,
            reference=[],
            subwidth=1,
            unjustified=[],
            forbidden=[],
            unqualified_pairs=0,
            skews={"pair0": 0.1},
        )
        drc = dict(
            violations=[dict(type="clearance", items=[dict(uuid="x")])], unconnected_items=[{}]
        )
        self.assertEqual(compare(drc, facts, drc, facts)[:2], (True, []))
        worse = dict(
            drc, violations=drc["violations"] + [dict(type="clearance", items=[dict(uuid="new")])]
        )
        ok, reasons, keys = compare(drc, facts, worse, facts)
        self.assertFalse(ok)
        self.assertIn("drc:new_violation", reasons)
        self.assertIn("objective", reasons)
        dangling = dict(
            drc,
            violations=drc["violations"] + [dict(type="track_dangling", items=[dict(uuid="t")])],
        )
        self.assertIn("drc:dangling", compare(drc, facts, dangling, facts)[1])
        split = dict(facts, partition=[["a"], ["b"]])
        self.assertIn("partition", compare(drc, facts, drc, split)[1])
        bad = dict(facts, entries={"p:0": False}, bad_entries=1)
        self.assertIn("lost_pad_entries", compare(drc, facts, drc, bad)[1])
        skew = dict(facts, skews={"pair0": 0.2})
        self.assertIn("skew:pair0", compare(drc, facts, drc, skew)[1])
        more_open = dict(drc, unconnected_items=[{}, {}])
        self.assertIn("drc:opens", compare(drc, facts, more_open, facts)[1])
        # functional-group cap (facts 'cross_group' rows {pair: [mm, allowed]}, only with a groups file)
        rows = {"a|x": [12.0, 10.0], "b|y": [4.0, 10.0], "c|z": [30.0, None]}
        cg = dict(facts, cross_group=rows)
        self.assertEqual(compare(drc, cg, drc, cg)[:2], (True, []))
        for after, ok in (
            ({"a|x": [12.0, 10.0]}, True),
            ({"a|x": [11.0, 10.0]}, True),  # over: may fall
            ({"a|x": [12.5, 10.0]}, False),  # ... never rise
            ({"b|y": [10.0, 10.0]}, True),
            ({"b|y": [10.1, 10.0]}, False),  # under: up to it
            ({"c|z": [99.0, None]}, True),
            ({"n|m": [10.5, 10.0]}, False),
        ):  # unlimited / new
            reasons = compare(drc, cg, drc, dict(facts, cross_group=dict(rows, **after)))[1]
            self.assertEqual("cross_group_cap" not in reasons, ok, after)
        self.assertEqual(compare(drc, facts, drc, facts)[:2], (True, []))  # no groups file


if __name__ == "__main__":
    unittest.main()
