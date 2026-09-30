"""Schematic view: symbol parsing, the mechanical builder, lane scoping and the HTTP routes.

Run with the viewer's python:  python -m unittest test_schematic
Paths default to the splanc hier layout next to this file; override with
PNR_SCHEMATIC_TEST_HIER (experiment output root) and PNR_SCHEMATIC_TEST_RUNTIME
(frozen pnr runtime holding pnr.power_topology and pnr.hier).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
HIER = Path(os.environ.get("PNR_SCHEMATIC_TEST_HIER", HERE.parent))
RUNTIME = Path(os.environ.get("PNR_SCHEMATIC_TEST_RUNTIME", HIER / "src11.frozen/hardware/pnr"))
DESIGN = RUNTIME.parent / "splanc_dev"
GRAPH = HIER / "inputs10b/graph.json"
PARTS = DESIGN / "elec/src/parts"
sys.path.insert(0, str(HERE))
from schematic_sym import cached_part_symbol, generic_symbol  # noqa: E402
from schematic_service import SchematicService  # noqa: E402

AVAILABLE = GRAPH.is_file() and PARTS.is_dir() and (RUNTIME / "pnr/power_topology.py").is_file()


def build(refs, runtime=RUNTIME, parts=PARTS, cache=None):
    with tempfile.TemporaryDirectory(prefix="schematic-test-") as tmp:
        req, out = Path(tmp) / "req.json", Path(tmp) / "out.json"
        req.write_text(json.dumps(dict(graph=str(GRAPH), rules=str(GRAPH.parent / "rules.json"),
                                       constraints=str(DESIGN / "mini-constraints.yaml"), parts=str(parts),
                                       ato_src=str(PARTS.parent), symbol_cache=cache, refs=refs)))
        env = dict(os.environ, PYTHONPATH=str(runtime))
        subprocess.run([sys.executable, str(HERE / "schematic_build.py"), str(req), str(out)], env=env, check=False,
                       timeout=120)
        return json.loads(out.read_text())


def block_refs(name):
    blocks = json.loads((HIER / "blocks/nb6-fb/blocks.json").read_text())
    return next(b["refs"] for b in blocks if b["name"] == name)


@unittest.skipUnless(AVAILABLE, "splanc design inputs not present")
class SymbolTest(unittest.TestCase):
    def test_every_part_library_parses(self):
        libs = sorted(p.name for p in PARTS.iterdir() if p.is_dir())
        self.assertGreaterEqual(len(libs), 90)
        with tempfile.TemporaryDirectory() as cache:
            failed = [(lib, why) for lib in libs for sym, _, why in [cached_part_symbol(PARTS, lib, cache)] if sym is None]
            self.assertEqual(failed, [])
            # second pass is served from the per-library cache
            self.assertEqual(len(list(Path(cache).glob("*.json"))), len(libs))

    def test_generic_symbol_names_pins_from_atopile(self):
        sym = generic_symbol("X", ["1", "2", "3", "3", ""], {"1": "VIN", "2": "GND"})
        pins = sym["units"][0]["pins"]
        self.assertEqual([p["number"] for p in pins], ["1", "2", "3"])
        self.assertEqual(pins[0]["name"], "VIN")
        self.assertTrue(sym["generic"])


@unittest.skipUnless(AVAILABLE, "splanc design inputs not present")
class BuilderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = build(None)

    def test_whole_board(self):
        d = self.board
        self.assertNotIn("error", d, d.get("trace"))
        self.assertEqual(d["scope"]["kind"], "board")
        self.assertEqual(len(d["components"]), 137)
        u5 = next(c for c in d["components"] if c["ref"] == "U5")
        self.assertEqual(next(p["net"] for p in u5["pins"] if p["number"] == "23"), "board.converter-1")
        self.assertEqual(next(p["pads"] for p in u5["pins"] if p["number"] == "24"), 2)  # stacked pads, one pin
        for c in d["components"]:  # only unnamed mechanical pads stay unmapped
            self.assertTrue(all(u["pad"] == "" and not u["net"] for u in c["unmapped_pads"]), c["ref"])
        self.assertEqual({m["id"]: m["type"] for m in d["modules"]},
                         {"board.converter": "System5V", "board.led0": "LedChannel", "board.led1": "LedChannel",
                          "board.pd": "PdInput"})
        self.assertEqual(next(m for m in d["modules"] if m["id"] == "board.led0")["twins"], ["board.led1"])
        self.assertEqual(len([h for h in d["highlights"] if h["style"] == "block"]), 13)
        self.assertEqual(next(n for n in d["nets"] if n["name"] == "lv")["draw"], "rail")
        self.assertEqual(next(n for n in d["nets"] if n["name"] == "20")["alias"], "U5.BOOT2")
        units = {r for u in d["units"] for r in u["refs"]}
        self.assertEqual(len(units), 137)

    def test_converter_block(self):
        d = build(block_refs("board.converter"))
        self.assertEqual((d["scope"]["kind"], d["scope"]["name"], d["scope"]["match"]), ("block", "board.converter", "exact"))
        self.assertEqual(len(d["components"]), 33)
        loops = [h for h in d["highlights"] if h["style"] in ("hot-loop", "power-path")]
        self.assertEqual(len(loops), 3)
        self.assertEqual(sum(h["style"] == "hot-loop" for h in loops), 2)
        tiers = {h["id"]: len(h["refs"]) for h in d["highlights"] if h["style"] == "tier"}
        self.assertEqual(sum(tiers.values()), 33)
        self.assertIn(["C16", "C17", "C18", "C19", "C20", "C21", "C22", "C23"], [a["refs"] for a in d["arrays"]])
        c19 = next(c for c in d["components"] if c["ref"] == "C19")
        self.assertEqual((c19["value"], c19["local"]), ("22 uF 0805", "output_cap3"))

    def test_subset_and_unknown_parts_do_not_fail(self):
        d = build(["C49", "U19", "NOPE1"])
        self.assertEqual(d["scope"]["match"], "none")
        self.assertEqual(len(d["components"]), 2)
        self.assertTrue(any("NOPE1" in w for w in d["warnings"]))

    def test_missing_and_broken_symbols_fall_back_to_boxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = Path(tmp) / "parts"
            shutil.copytree(PARTS, parts, ignore=lambda d, names: [n for n in names if (Path(d) / n).is_file() and not n.endswith((".ato", ".kicad_sym"))])
            shutil.rmtree(parts / "Texas_Instruments_TPS552882RPMR")
            for f in (parts / "Samsung_CL21A226MOQNNNE").glob("*.kicad_sym"):
                f.write_text("(kicad_symbol_lib (symbol \"broken\"")
            d = build(block_refs("board.converter"), parts=parts)
            self.assertNotIn("error", d, d.get("trace"))
            u5 = next(c for c in d["components"] if c["ref"] == "U5")
            sym = d["symbols"][u5["lib"]]
            self.assertTrue(sym["generic"])
            self.assertEqual(len(sym["units"][0]["pins"]), 26)
            c16 = next(c for c in d["components"] if c["ref"] == "C16")
            self.assertTrue(d["symbols"][c16["lib"]]["generic"])
            self.assertTrue(any("generic" in w for w in d["warnings"]))

    def test_without_runtime_degrades_to_modules_only(self):
        with tempfile.TemporaryDirectory() as empty:
            d = build(None, runtime=Path(empty))
        self.assertNotIn("error", d, d.get("trace"))
        self.assertIsNone(d["power"])
        self.assertEqual(len(d["components"]), 137)
        self.assertEqual({m["id"] for m in d["modules"]}, {"board.converter", "board.led0", "board.led1", "board.pd"})
        self.assertTrue(any("runtime unavailable" in w for w in d["warnings"]))


@unittest.skipUnless(AVAILABLE, "splanc design inputs not present")
class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="schematic-service-")
        root = Path(self.tmp.name) / "live"
        root.mkdir()
        self.svc = SchematicService(root, graph=GRAPH, constraints=DESIGN / "mini-constraints.yaml", parts=PARTS,
                                    runtime=RUNTIME, hier=HIER, assets=HERE)

    def tearDown(self):
        self.svc.pool.shutdown(wait=True)
        self.tmp.cleanup()

    def wait(self, *args):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            r = self.svc.request(*args)
            if r["status"] not in ("pending", "busy"):
                return r
            time.sleep(.05)
        self.fail("schematic build did not finish")

    def test_old_blocks_lane_resolves_from_keep_json(self):
        r = self.wait("blocks/board_pd-s1-18.5x18.5", [], None)
        self.assertEqual(r["status"], "ready", r)
        self.assertEqual(r["scope"], "block")
        self.assertEqual(r["overlay"]["template"], "eb028a7b17aa")
        doc = json.loads(self.svc.payload(r["key"]))
        self.assertEqual(doc["scope"]["name"], "board.pd")
        # same parts, other trial: same content-addressed payload
        again = self.svc.request("blocks/board_pd-s1-18.5x18.5", sorted(doc["scope"]["refs"]), None)
        self.assertEqual(again["key"], r["key"])
        board = self.wait("blocks/board_pd-s1-18.5x18.5", [], None, "board")
        self.assertEqual(board["scope"], "board")
        self.assertEqual(board["overlay"]["lane_refs"], doc["scope"]["refs"])

    def test_whole_board_lane_and_path_confinement(self):
        refs = [c["ref"] for c in json.loads(GRAPH.read_text())["components"]]
        r = self.wait("h9/p001/rung1", refs, "/etc/passwd")
        self.assertEqual((r["status"], r["scope"], r["overlay"]["run_dir"]), ("ready", "board", None))
        self.assertIsNone(self.svc.run_dir("../../x/y", None))
        with self.assertRaises(ValueError):
            self.svc.payload("../" + "a" * 61)


@unittest.skipUnless(AVAILABLE, "splanc design inputs not present")
class HttpTest(unittest.TestCase):
    def test_routes(self):
        repo = Path(os.environ.get("PNR_VIEWER_TEST_REPO", HERE.parents[2]))
        graph = json.loads(GRAPH.read_text())
        refs = set(block_refs("board.pd"))
        layout = dict(graph, components=[c for c in graph["components"] if c["ref"] in refs])
        with tempfile.TemporaryDirectory(prefix="viewer-schematic-") as tmp:
            root = Path(tmp) / "live"
            (root / "events").mkdir(parents=True)
            event = dict(schema="pnr-live-event-v1", id="1-aa", time=1, kind="signal_start", candidate="t1/board_pd-x",
                         iteration=None, layout=layout, source=str(HIER / "blocks/nope"), data={})
            (root / "events/1-aa.json").write_text(json.dumps(event))
            p = subprocess.Popen([sys.executable, str(HERE / "server.py"), str(root), "--port", "0", "--repo", str(repo),
                                  "--cost-runtime", str(RUNTIME), "--schematic-graph", str(GRAPH),
                                  "--schematic-hier", str(HIER), "--net-summaries", "off"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                base = p.stdout.readline().strip().removeprefix("Live PnR: ")
                get = lambda path: json.load(urlopen(base + path, timeout=10))
                end = time.monotonic() + 10
                while not get("/api/state")["revision"] and time.monotonic() < end:
                    time.sleep(.05)
                state = get("/api/state")
                self.assertNotIn("source", state["lanes"]["t1/board_pd-x"])  # /api/state unchanged
                with self.assertRaises(HTTPError) as err:
                    get("/api/schematic?lane=missing")
                self.assertEqual(err.exception.code, 404)
                end = time.monotonic() + 60
                while (r := get("/api/schematic?lane=t1/board_pd-x"))["status"] in ("pending", "busy") and time.monotonic() < end:
                    time.sleep(.1)
                self.assertEqual((r["status"], r["scope"]), ("ready", "block"), r)
                with urlopen(base + r["url"], timeout=10) as resp:
                    self.assertIn("immutable", resp.headers["Cache-Control"])
                    doc = json.load(resp)
                self.assertEqual(sorted(doc["scope"]["refs"]), sorted(refs))
                with urlopen(base + "/schematic.js", timeout=10) as resp:
                    self.assertEqual(resp.headers["Content-Type"], "application/javascript")
                    self.assertEqual(resp.headers["Cache-Control"], "no-store")
                with urlopen(base + "/elk.bundled.js", timeout=10) as resp:
                    self.assertIn("max-age", resp.headers["Cache-Control"])
            finally:
                p.terminate()
                p.wait(10)


if __name__ == "__main__":
    unittest.main()
