"""Schematic view: symbol parsing, the mechanical builder, lane scoping and the HTTP routes.

Runs on the synthetic atopile project in tests/fixtures/viewer/design (a regulator, two
capacitors without a symbol library and a pull-up resistor) with the engine runtime on the path.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from yapnr.viewer import runtime
from yapnr.viewer.services.schematic import SchematicService
from yapnr.viewer.services.schematic_sym import cached_part_symbol, generic_symbol
from yapnr.viewer.testing import (
    child_env,
    fixture_copy,
    module_argv,
    start_viewer,
    stop,
    wait_for,
)

DESIGN = fixture_copy("design")
GRAPH = DESIGN / "graph.json"
CONSTRAINTS = DESIGN / "constraints.yaml"
SRC = DESIGN / "elec/src"
PARTS = SRC / "parts"
RUNTIME = runtime.imported_runtime()
SUPPLY = ["C1", "C2", "U1"]


def no_engine_env():
    """A child environment whose import path has no `pnr` package."""
    env = child_env()
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in env["PYTHONPATH"].split(os.pathsep) if not (Path(p) / "pnr").is_dir()
    )
    return env


def build(refs, parts=PARTS, cache=None, env=None):
    with tempfile.TemporaryDirectory(prefix="schematic-test-") as tmp:
        req, out = Path(tmp) / "req.json", Path(tmp) / "out.json"
        req.write_text(
            json.dumps(
                dict(
                    graph=str(GRAPH),
                    rules=str(DESIGN / "rules.json"),
                    constraints=str(CONSTRAINTS),
                    parts=str(parts),
                    ato_src=str(SRC),
                    symbol_cache=cache,
                    refs=refs,
                )
            )
        )
        subprocess.run(
            module_argv("yapnr.viewer.services.schematic_build", str(req), str(out)),
            env=env or child_env(),
            check=False,
            timeout=120,
        )
        return json.loads(out.read_text())


class SymbolTest(unittest.TestCase):
    def test_part_libraries(self):
        with tempfile.TemporaryDirectory() as cache:
            got = {
                lib: cached_part_symbol(PARTS, lib, cache) for lib in ("Regulator", "Res", "Cap")
            }
            self.assertEqual(
                [p["number"] for p in got["Regulator"][0]["units"][0]["pins"]], ["1", "2", "3"]
            )
            self.assertEqual(got["Regulator"][1], {"1": "VIN", "2": "GND", "3": "VOUT"})
            self.assertEqual(got["Res"][0]["ref_prefix"], "R")
            self.assertIsNone(got["Cap"][0])  # no .kicad_sym: the builder draws a generic box
            self.assertIn("no unique .kicad_sym", got["Cap"][2])
            # parsed libraries are cached per library
            self.assertEqual(len(list(Path(cache).glob("*.json"))), 2)
            self.assertEqual(cached_part_symbol(PARTS, "../x", cache)[0], None)

    def test_generic_symbol_names_pins_from_atopile(self):
        sym = generic_symbol("X", ["1", "2", "3", "3", ""], {"1": "VIN", "2": "GND"})
        pins = sym["units"][0]["pins"]
        self.assertEqual([p["number"] for p in pins], ["1", "2", "3"])
        self.assertEqual(pins[0]["name"], "VIN")
        self.assertTrue(sym["generic"])


class BuilderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = build(None)

    def test_whole_board(self):
        d = self.board
        self.assertNotIn("error", d, d.get("trace"))
        self.assertEqual(d["scope"]["kind"], "board")
        self.assertEqual(sorted(c["ref"] for c in d["components"]), ["C1", "C2", "R1", "U1"])
        u1 = next(c for c in d["components"] if c["ref"] == "U1")
        self.assertEqual(next(p["net"] for p in u1["pins"] if p["number"] == "3"), "vout-hv")
        self.assertEqual((u1["lib"], u1["value"]), ("Regulator", "REG-33"))
        self.assertEqual({m["id"]: m["type"] for m in d["modules"]}, {"board.supply": "Supply"})
        c1 = next(c for c in d["components"] if c["ref"] == "C1")
        self.assertTrue(d["symbols"][c1["lib"]]["generic"])
        self.assertTrue(any("Cap: generic symbol" in w for w in d["warnings"]))
        self.assertIsNotNone(d["power"])
        self.assertEqual({r for u in d["units"] for r in u["refs"]}, {"C1", "C2", "R1", "U1"})

    def test_block_scope(self):
        d = build(SUPPLY)
        self.assertEqual(
            (d["scope"]["kind"], d["scope"]["name"], d["scope"]["match"]),
            ("block", "board.supply", "exact"),
        )
        self.assertEqual(sorted(c["ref"] for c in d["components"]), SUPPLY)

    def test_subset_and_unknown_parts_do_not_fail(self):
        d = build(["R1", "U1", "NOPE1"])
        self.assertNotIn("error", d, d.get("trace"))
        self.assertEqual(sorted(c["ref"] for c in d["components"]), ["R1", "U1"])
        self.assertTrue(any("NOPE1" in w for w in d["warnings"]))

    def test_broken_symbols_fall_back_to_boxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = Path(tmp) / "parts"
            shutil.copytree(PARTS, parts)
            (parts / "Regulator/Regulator.kicad_sym").write_text('(kicad_symbol_lib (symbol "x"')
            d = build(None, parts=parts)
        self.assertNotIn("error", d, d.get("trace"))
        u1 = next(c for c in d["components"] if c["ref"] == "U1")
        sym = d["symbols"][u1["lib"]]
        self.assertTrue(sym["generic"])
        self.assertEqual([p["name"] for p in sym["units"][0]["pins"]], ["VIN", "GND", "VOUT"])

    def test_without_runtime_degrades_to_modules_only(self):
        d = build(None, env=no_engine_env())
        self.assertNotIn("error", d, d.get("trace"))
        self.assertIsNone(d["power"])
        self.assertEqual(len(d["components"]), 4)
        self.assertEqual({m["id"] for m in d["modules"]}, {"board.supply"})
        self.assertTrue(any("runtime unavailable" in w for w in d["warnings"]))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="schematic-service-")
        self.experiment = Path(self.tmp.name)
        root = self.experiment / "live"
        root.mkdir()
        # a block trial run: blocks/<run>/<block>/native/<tag>/keep.json plus its library.json
        trial = self.experiment / "blocks/r1/board_supply/native/s1"
        trial.mkdir(parents=True)
        (trial / "keep.json").write_text(json.dumps(SUPPLY))
        (trial.parent.parent / "library.json").write_text(json.dumps(dict(template_id="t0123")))
        self.svc = SchematicService(
            root,
            graph=GRAPH,
            constraints=CONSTRAINTS,
            parts=PARTS,
            runtime=RUNTIME,
            experiment=self.experiment,
        )

    def tearDown(self):
        self.svc.pool.shutdown(wait=True)
        self.tmp.cleanup()

    def wait(self, *args):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            r = self.svc.request(*args)
            if r["status"] not in ("pending", "busy"):
                return r
            time.sleep(0.05)
        self.fail("schematic build did not finish")

    def test_blocks_lane_resolves_from_keep_json(self):
        r = self.wait("blocks/s1", [], None)
        self.assertEqual(r["status"], "ready", r)
        self.assertEqual(r["scope"], "block")
        self.assertEqual(r["overlay"]["template"], "t0123")
        doc = json.loads(self.svc.payload(r["key"]))
        self.assertEqual(doc["scope"]["name"], "board.supply")
        # same parts, another trial: the same content-addressed payload
        again = self.svc.request("blocks/s1", sorted(doc["scope"]["refs"]), None)
        self.assertEqual(again["key"], r["key"])
        board = self.wait("blocks/s1", [], None, "board")
        self.assertEqual(board["scope"], "board")
        self.assertEqual(board["overlay"]["lane_refs"], doc["scope"]["refs"])

    def test_whole_board_lane_and_path_confinement(self):
        refs = [c["ref"] for c in json.loads(GRAPH.read_text())["components"]]
        r = self.wait("h1/p001/rung1", refs, "/etc/passwd")
        self.assertEqual(
            (r["status"], r["scope"], r["overlay"]["run_dir"]), ("ready", "board", None)
        )
        self.assertIsNone(self.svc.run_dir("../../x/y", None))
        with self.assertRaises(ValueError):
            self.svc.payload("../" + "a" * 61)

    def test_unconfigured(self):
        svc = SchematicService(self.experiment / "live")
        self.assertEqual(svc.request("x", [], None)["status"], "unavailable")


class HttpTest(unittest.TestCase):
    def test_routes(self):
        graph = json.loads(GRAPH.read_text())
        layout = dict(graph, components=[c for c in graph["components"] if c["ref"] in SUPPLY])
        with tempfile.TemporaryDirectory(prefix="viewer-schematic-") as tmp:
            root = Path(tmp) / "live"
            (root / "events").mkdir(parents=True)
            event = dict(
                schema="pnr-live-event-v1",
                id="1-aa",
                time=1,
                kind="signal_start",
                candidate="t1/board_supply-x",
                iteration=None,
                layout=layout,
                source=str(Path(tmp) / "blocks/nope"),
                data={},
            )
            (root / "events/1-aa.json").write_text(json.dumps(event))
            p, base = start_viewer(
                root,
                "--graph",
                str(GRAPH),
                "--constraints",
                str(CONSTRAINTS),
                "--atopile-root",
                str(DESIGN),
                "--atopile-build",
                "default",
                "--viewer3d",
                "off",
            )
            try:

                def get(path):
                    return json.load(urlopen(base + path, timeout=10))

                wait_for(lambda: get("/api/state")["revision"], 10)
                state = get("/api/state")
                self.assertNotIn("source", state["lanes"]["t1/board_supply-x"])
                with self.assertRaises(HTTPError) as err:
                    get("/api/schematic?lane=missing")
                self.assertEqual(err.exception.code, 404)

                def ready():
                    r = get("/api/schematic?lane=t1/board_supply-x")
                    return r if r["status"] not in ("pending", "busy") else None

                r = wait_for(ready, 60, 0.1)
                self.assertEqual((r["status"], r["scope"]), ("ready", "block"), r)
                with urlopen(base + r["url"], timeout=10) as resp:
                    self.assertIn("immutable", resp.headers["Cache-Control"])
                    doc = json.load(resp)
                self.assertEqual(sorted(doc["scope"]["refs"]), SUPPLY)
                with urlopen(base + "/schematic.js", timeout=10) as resp:
                    self.assertEqual(resp.headers["Content-Type"], "application/javascript")
                    self.assertEqual(resp.headers["Cache-Control"], "no-store")
                if not get("/api/about")["missing_assets"]:  # the assembled dist
                    with urlopen(base + "/elk.bundled.js", timeout=10) as resp:
                        self.assertIn("max-age", resp.headers["Cache-Control"])
            finally:
                stop(p)


if __name__ == "__main__":
    unittest.main()
