"""atopile source index: resolver semantics, build-target entries, cache, LLM merge, path safety.

Offline (no network, no model calls). The synthetic designs are written by the tests or come from
tests/fixtures/viewer/design (an atopile project with an ato.yaml, symbols and a graph.json).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from yapnr.viewer import config
from yapnr.viewer.agent import net_llm
from yapnr.viewer.sources import open_sources, read_builds, resolve_entry
from yapnr.viewer.sources.service import SourceNotFound, SourceService
from yapnr.viewer.testing import fixture_copy

DESIGN = fixture_copy("design")
SRC = DESIGN / "elec/src"
GRAPH = DESIGN / "graph.json"


class FixtureDesign(unittest.TestCase):
    """The fixture project: build-target entries, the net partition, cache and LLM merge."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = SourceService(SRC, GRAPH, cache_dir=cls.tmp.name, entry=("demo.ato", "Demo"))
        cls.ix = cls.svc.index()
        cls.graph = json.loads(GRAPH.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_build_targets(self):
        self.assertEqual(set(read_builds(DESIGN / "ato.yaml")), {"default", "other"})
        self.assertEqual(resolve_entry(DESIGN, "default"), (SRC / "demo.ato", "Demo"))
        self.assertEqual(resolve_entry(DESIGN, "other")[1], "Board")
        with self.assertRaisesRegex(ValueError, "no build target 'nope'"):
            resolve_entry(DESIGN, "nope")
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ValueError, "no ato.yaml"):
                resolve_entry(Path(t), "default")

    def test_open_sources_from_config(self):
        with tempfile.TemporaryDirectory() as t:
            argv = ["--root", t + "/live", "--graph", str(GRAPH)]
            env = {"YAPNR_USER_CONFIG": ""}
            cfg = config.load(
                argv + ["--atopile-root", str(DESIGN), "--atopile-build", "default"], env
            )
            s = open_sources(cfg)
            self.assertEqual((s.reason, s.src, s.entry_label), (None, SRC, "demo.ato:Demo"))
            self.assertEqual(s.parts, SRC / "parts")
            self.assertEqual(s.service.index()["entry"], "demo.ato:Demo")
            # no atopile sources: no Source tab, with the reason
            s = open_sources(config.load(argv, env))
            self.assertIsNone(s.service)
            self.assertIn("no atopile sources", s.reason)
            # atopile sources without a netlist to join them to
            s = open_sources(config.load(["--root", t + "/live", "--atopile-src", str(SRC)], env))
            self.assertIsNone(s.service)
            self.assertIn("netlist", s.reason)
            s = open_sources(
                config.load(argv + ["--atopile-root", str(DESIGN), "--atopile-build", "x"], env)
            )
            self.assertIn("no build target", s.reason)

    def test_partition_matches_graph_exactly(self):
        v = self.ix["validation"]
        self.assertEqual((v["nets_matched"], v["nets_total"]), (4, 4))
        self.assertEqual(v["mismatches"], [])
        self.assertEqual(v["components_matched"], len(self.graph["components"]))
        self.assertEqual(self.ix["entry"], "demo.ato:Demo")
        self.assertEqual(set(self.ix["nets"]), {n["name"] for n in self.graph["nets"]})
        for n in self.graph["nets"]:
            self.assertEqual(
                {(p["ref"], p["pad"]) for p in self.ix["nets"][n["name"]]["pins"]},
                {(r, str(p)) for r, p in n["pins"]},
            )

    def test_component_instance_line(self):
        c = self.ix["components"]["C2"]
        self.assertEqual(
            (c["address"], c["instance"], c["type"]),
            ("board.supply.output_cap._p", "board.supply.output_cap", "Capacitor"),
        )
        self.assertEqual(c["part"], "Cap_package")
        self.assertEqual((c["file"], c["text"]), ("demo.ato", "output_cap = new Capacitor"))
        lines = (SRC / "demo.ato").read_text().splitlines()
        self.assertEqual(lines[c["line"] - 1].strip(), "output_cap = new Capacitor")
        self.assertEqual(
            [x["address"] for x in c["chain"]],
            ["board", "board.supply", "board.supply.output_cap", "board.supply.output_cap._p"],
        )
        self.assertEqual([x["module"] for x in c["chain"]][:3], ["Board", "Supply", "Capacitor"])
        self.assertIn("C2", self.svc.at("demo.ato", c["line"])["refs"])

    def test_net_semantics(self):
        vin, lv, vout, en = (self.ix["nets"][k] for k in ("vin-hv", "lv", "vout-hv", "EN"))
        self.assertEqual((vin["kind"], vin["voltage"]), ("power", "5V +/- 5%"))
        self.assertEqual(vin["aliases"][0]["path"], "board.supply.vin.hv")
        self.assertEqual(lv["kind"], "ground")
        self.assertIn("GND", lv["title"])
        self.assertEqual((vout["kind"], vout["voltage"]), ("power", "3.3V +/- 3%"))
        self.assertEqual(en["kind"], "unconnected")
        hit = [c for c in vout["currents"] if c["target"] == "ic"]
        self.assertEqual(
            [(c["ref"], c["pads"], c["rms_current_a"], c["peak_current_a"]) for c in hit],
            [("U1", ["3"], 0.5, 1)],
        )
        line = (SRC / hit[0]["file"]).read_text().splitlines()[hit[0]["line"] - 1]
        self.assertIn("@pnr-current", line)

    def test_cache_bytes_and_reload(self):
        self.assertIs(self.svc.index(), self.ix)
        b = self.svc.index_bytes()
        self.assertIs(b, self.svc.index_bytes())
        self.assertEqual(json.loads(b)["sha"], self.ix["sha"])
        self.assertTrue(list(Path(self.tmp.name).glob("source-index-*.json")))
        again = SourceService(SRC, GRAPH, cache_dir=self.tmp.name, entry=("demo.ato", "Demo"))
        self.assertEqual(
            (again.index()["sha"], again.index()["dossier_sha"]),
            (self.ix["sha"], self.ix["dossier_sha"]),
        )

    def test_llm_merge_requires_matching_dossier(self):
        svc = SourceService(SRC, GRAPH, cache_dir=self.tmp.name, entry=("demo.ato", "Demo"))
        sha, dos = svc.dossier()
        self.assertEqual(sha, self.ix["dossier_sha"])
        self.assertEqual(len(dos), 4)
        f = Path(self.tmp.name) / "llm.json"
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha="0" * 64,
                    model="x",
                    generated_at="t",
                    nets={"vin-hv": dict(label="bad", summary="bad")},
                )
            )
        )
        self.assertEqual(svc.merge_llm(f), 0)
        self.assertNotIn("llm", svc.net("vin-hv"))
        self.assertTrue(svc.index()["llm"]["stale"])
        # same mechanical dossier but an older prompt version: stale, never served as current
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha=sha,
                    source_sha=sha,
                    prompt_version=net_llm.PROMPT_VERSION - 1,
                    model="sonnet",
                    generated_at="t",
                    nets={"vin-hv": dict(label="old", summary="old")},
                )
            )
        )
        self.assertEqual(svc.merge_llm(), 0)
        self.assertTrue(svc.index()["llm"]["stale"])
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha="e" * 64,
                    source_sha=sha,
                    prompt_version=net_llm.PROMPT_VERSION,
                    model="sonnet",
                    generated_at="2026-09-29T00:00:00Z",
                    nets={
                        "vin-hv": dict(label="5 V input", summary="Input rail."),
                        "nope": dict(label="x"),
                    },
                )
            )
        )
        self.assertEqual(svc.merge_llm(), 1)
        self.assertEqual(svc.net("vin-hv")["llm"]["label"], "5 V input")
        self.assertEqual(json.loads(svc.index_bytes())["nets"]["vin-hv"]["llm"]["model"], "sonnet")
        svc.llm_model = "opus"  # a configured model rejects labels made by another one
        self.assertEqual(svc.merge_llm(), 0)
        self.assertTrue(svc.index()["llm"]["stale"])
        svc.llm_model = None
        f.unlink()
        self.assertEqual(svc.merge_llm(), 0)
        self.assertNotIn("llm", svc.net("vin-hv"))

    def test_file_and_rejections(self):
        f = self.svc.file("demo.ato")
        self.assertIn("output_cap = new Capacitor", f["text"])
        self.assertEqual(f["lines"], len(f["text"].splitlines()))
        self.assertIn("demo.ato", {x["path"] for x in self.ix["files"]})
        part = next(x["path"] for x in self.ix["files"] if x["kind"] == "part")
        self.assertTrue(self.svc.file(part)["text"])
        for bad in (
            "../demo.ato",
            "/etc/passwd",
            "/" + str(SRC / "demo.ato"),
            "parts/../../demo.ato",
            "parts/../demo.ato",
            "./demo.ato",
            "demo.ato.orig",
            "x.txt",
            "",
            "a\\b.ato",
            ".hidden.ato",
            "C:/x.ato",
            "~/x.ato",
            "parts//x.ato",
            "demo.ato\0.ato",
            None,
            5,
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.svc.file(bad)
        with self.assertRaises(SourceNotFound):
            self.svc.file("nope.ato")


PART = '''import is_atomic_part

component P_package:
    """Test part."""
    trait is_atomic_part<manufacturer="X", partnumber="P-1", footprint="f.kicad_mod", symbol="s.kicad_sym">
    signal p1 ~ pin 1
    signal p2 ~ pin 2
    p2 ~ pin 4
    pin 3
'''
TOP = '''import ElectricPower
from "parts/P/P.ato" import P_package

module Wrap:
    """Wrapper."""
    _p = new P_package
    signal p1 ~ _p.p1
    signal p2 ~ _p.p2

module Sub:
    power = new ElectricPower     # sub rail
    signal out ~ u.p1
    u = new P_package
    u.p2 ~ power.lv

module Core:
    """Core module."""
    rail = new ElectricPower   # main rail
    rail.voltage = 5V +/- 5%
    sub = new Sub
    sub.power ~ rail
    r1 = new Wrap
    r1.p1 ~ rail.hv
    r1.p2 ~ rail.lv
    sub.out ~ rail.hv
    # Test envelope.
    # @pnr-current {"target":"r1","pads":["1"],"scope":"net","rms_current_a":1,"peak_current_a":2}

module Top:
    board = new Core
'''


class Synthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        src = root / "src"
        (src / "parts/P").mkdir(parents=True)
        (src / "parts/P/P.ato").write_text(PART)
        (src / "top.ato").write_text(TOP)
        (root / "outside.ato").write_text("module Secret:\n    pass\n")
        os.symlink(root / "outside.ato", src / "link.ato")
        os.symlink(root, src / "up")
        nets = dict(
            hv=[["R1", "1"], ["U1", "1"]],
            lv=[["R1", "2"], ["R1", "4"], ["U1", "2"], ["U1", "4"]],
            r3=[["R1", "3"]],
        )
        nets["3"] = [["U1", "3"]]
        net_of = {(r, p): n for n, ps in nets.items() for r, p in ps}

        def comp(ref, addr):
            return dict(
                ref=ref,
                address=addr,
                footprint="P:f",
                pads=[dict(name=str(x), net=net_of[(ref, str(x))]) for x in (1, 2, 3, 4)],
            )

        g = dict(
            components=[comp("R1", "board.r1._p"), comp("U1", "board.sub.u")],
            nets=[dict(code=i, name=n, pins=ps) for i, (n, ps) in enumerate(nets.items(), 1)],
        )
        (root / "graph.json").write_text(json.dumps(g))
        self.svc = SourceService(src, root / "graph.json", cache_dir=root / "cache")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolver(self):
        ix = self.svc.index()
        self.assertEqual(ix["entry"], "top.ato:Top")
        self.assertEqual(
            (
                ix["validation"]["nets_matched"],
                ix["validation"]["mismatches"],
                ix["validation"]["notes"],
            ),
            (4, [], []),
        )
        hv, lv, n3 = (ix["nets"][k] for k in ("hv", "lv", "3"))
        self.assertEqual(
            [a["path"] for a in hv["aliases"]],
            ["board.rail.hv", "board.sub.out", "board.sub.power.hv"],
        )
        self.assertEqual((hv["kind"], hv["voltage"]), ("power", "5V +/- 5%"))
        self.assertTrue(hv["title"].startswith("rail 5V ±5% rail — main rail"), hv["title"])
        self.assertEqual((lv["kind"], n3["kind"]), ("ground", "unconnected"))
        self.assertEqual({p["pin"] for p in lv["pins"] if p["ref"] == "U1"}, {"p2"})
        self.assertEqual(
            [(c["target"], c["ref"], c["note"]) for c in hv["currents"]],
            [("r1", "R1", "Test envelope.")],
        )
        c = ix["components"]["R1"]
        self.assertEqual(
            (c["type"], c["part"], c["mpn"], c["text"]),
            ("Wrap", "P_package", "P-1", "r1 = new Wrap"),
        )
        self.assertEqual(ix["modules"]["Core"]["doc"], "Core module.")
        self.assertNotIn("link.ato", {f["path"] for f in ix["files"]})
        self.assertFalse(any(f["path"].startswith("up/") for f in ix["files"]))

    def test_symlink_escapes_rejected(self):
        self.assertIn("module Wrap", self.svc.file("top.ato")["text"])
        for bad in ("link.ato", "up/outside.ato", "up/src/top.ato"):
            with self.assertRaises(ValueError):
                self.svc.file(bad)

    def test_edit_rebuilds(self):
        a = self.svc.index()["sha"]
        p = Path(self.tmp.name) / "src/top.ato"
        p.write_text(TOP.replace("# main rail", "# primary rail"))
        self.svc._checked = float("-inf")
        ix = self.svc.index()
        self.assertNotEqual(ix["sha"], a)
        self.assertIn("primary rail", ix["nets"]["hv"]["title"])
        p.write_text(
            TOP.replace("module Top:\n    board = new Core\n", "")
        )  # no entry left: keep the last good index
        self.svc._checked = float("-inf")
        self.assertIs(self.svc.index(), ix)
        self.assertIn("no entry", self.svc.describe()["error"])


if __name__ == "__main__":
    unittest.main()
