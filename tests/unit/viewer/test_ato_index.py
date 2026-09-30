"""Source index regression: exact 93/93 net partition, semantics, cache, LLM merge, path safety.

Run: ../pnr-regression-runtime/bin/python test_ato_index.py  (stdlib unittest; no network, no LLM).
"""

import glob
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ato_index  # noqa: E402
import net_llm  # noqa: E402
from source_service import SourceNotFound, SourceService  # noqa: E402

HIER = HERE.parent
SRC = Path(os.environ.get("SPLANC_ATO_SRC", HIER / "src11.frozen/hardware/splanc_dev/elec/src"))
GRAPH = Path(os.environ.get("SPLANC_GRAPH", HIER / "inputs10b/graph.json"))
PINOUT = Path(
    os.environ.get(
        "SPLANC_PINOUT", HIER.parent.parent / "hardware/splanc_dev/build/builds/mini/pinout"
    )
)


@unittest.skipUnless(SRC.is_dir() and GRAPH.is_file(), "design inputs missing")
class RealDesign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = SourceService(SRC, GRAPH, cache_dir=cls.tmp.name)
        cls.ix = cls.svc.index()
        cls.graph = json.loads(GRAPH.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_partition_matches_graph_exactly(self):
        v = self.ix["validation"]
        self.assertEqual(v["nets_total"], len(self.graph["nets"]))
        self.assertEqual((v["nets_matched"], v["nets_total"]), (93, 93))
        self.assertEqual(v["mismatches"], [])
        self.assertEqual(v["components_matched"], len(self.graph["components"]))
        self.assertEqual(self.ix["entry"], "splanc_mini.ato:SplancMini")
        self.assertEqual(set(self.ix["nets"]), {n["name"] for n in self.graph["nets"]})
        for n in self.graph["nets"]:
            self.assertEqual(
                {(p["ref"], p["pad"]) for p in self.ix["nets"][n["name"]]["pins"]},
                {(r, str(p)) for r, p in n["pins"]},
            )

    def test_c17_instance_line(self):
        c = self.ix["components"]["C17"]
        self.assertEqual(
            (c["address"], c["instance"], c["type"]),
            ("board.converter.output_cap1._p", "board.converter.output_cap1", "C22u"),
        )
        self.assertEqual(c["part"], "Samsung_CL21A226MOQNNNE_package")
        self.assertEqual((c["file"], c["text"]), ("system_5v.ato", "output_cap1 = new C22u"))
        lines = (SRC / "system_5v.ato").read_text().splitlines()
        self.assertEqual(lines[c["line"] - 1].strip(), "output_cap1 = new C22u")
        self.assertEqual(
            [x["address"] for x in c["chain"]],
            [
                "board",
                "board.converter",
                "board.converter.output_cap1",
                "board.converter.output_cap1._p",
            ],
        )
        self.assertEqual([x["module"] for x in c["chain"]][:3], ["MiniCore", "System5V", "C22u"])
        self.assertEqual(c["pins"]["1"], dict(pin="p1", net="board.converter-1-3"))
        self.assertIn("output_cap1.p1 ~ ic.VOUT", [s["text"] for s in c["statements"]])
        self.assertIn("C17", self.svc.at("system_5v.ato", c["line"])["refs"])

    def test_hv_lv_titles_and_kind(self):
        hv, lv = self.ix["nets"]["hv"], self.ix["nets"]["lv"]
        self.assertEqual(lv["kind"], "ground")
        self.assertIn("GND", lv["title"])
        self.assertTrue(lv["low_info"] and hv["low_info"])
        self.assertEqual(hv["kind"], "power")
        self.assertIn("p3v3a", hv["title"])
        self.assertIn("3.3V", hv["title"])
        self.assertEqual(hv["voltage"], "3.3V +/- 3%")
        self.assertEqual(hv["aliases"][0]["path"], "board.p3v3a.hv")
        self.assertIn("board.vbus.lv", [a["path"] for a in lv["aliases"]])
        self.assertTrue(all(a.get("member") != "hv" for a in lv["aliases"]))
        p5 = self.ix["nets"]["p5v-hv"]
        self.assertEqual(
            (p5["kind"], p5["voltage"], p5["aliases"][0]["path"]),
            ("power", "5V +/- 5%", "board.p5v.hv"),
        )
        self.assertIn("converter.output ~ p5v", [s["text"] for s in p5["statements"]])
        for n in self.ix["nets"].values():
            self.assertTrue(
                n["title"]
                and n["summary"]
                and n["kind"] in ("ground", "power", "signal", "unconnected"),
                n["name"],
            )
            self.assertEqual(n["kind"] == "unconnected", len(n["pins"]) == 1, n["name"])

    def test_comp_net_has_tps552882_comp(self):
        comp = self.ix["nets"]["COMP"]
        pins = {(p["ref"], p["pad"]): p for p in comp["pins"]}
        self.assertEqual(pins[("U5", "18")]["pin"], "COMP")
        self.assertIn("TPS552882", pins[("U5", "18")]["part"])
        self.assertIn("TPS552882", comp["title"])
        self.assertIn("compensation_r.p1 ~ ic.COMP", [s["text"] for s in comp["statements"]])
        self.assertIn("TPS552882RPMR COMP", comp["summary"])

    def test_pnr_current_lands_on_net(self):
        sw1 = self.ix["nets"]["board.converter-1"]["currents"]
        hit = [c for c in sw1 if c["target"] == "converter.inductor" and c["pads"] == ["1"]]
        self.assertEqual(len(hit), 1)
        self.assertEqual(
            (hit[0]["ref"], hit[0]["scope"], hit[0]["rms_current_a"], hit[0]["peak_current_a"]),
            ("L2", "net", 5, 8),
        )
        line = (SRC / hit[0]["file"]).read_text().splitlines()[hit[0]["line"] - 1]
        self.assertIn("@pnr-current", line)
        self.assertIn('"converter.inductor"', line)
        plane = [c for c in self.ix["nets"]["lv"]["currents"] if c["scope"] == "plane_access"]
        self.assertEqual([(c["target"], c["ref"]) for c in plane], [("converter.low_fet", "Q2")])
        rules = GRAPH.parent / "rules.json"
        if rules.is_file():  # every current intent the PnR compiled lands on the same net here
            got = {
                (c["target"], tuple(c["pads"]), c["scope"]): (n, c["ref"])
                for n, v in self.ix["nets"].items()
                for c in v["currents"]
            }
            for c in json.loads(rules.read_text())["current_intents"]:
                self.assertEqual(
                    got.get((c["target"], tuple(c["pads"]), c["scope"])), (c["net"], c["ref"]), c
                )
            self.assertEqual(self.ix["nets"]["lv"].get("net_class"), "gnd")

    def test_net_comments_are_relevant_paragraphs(self):
        drain = "DRAIN pins/pad share an isolated copper island, never ground or VBUS."
        for n in (
            "lv",
            "raw_usb-hv",
        ):  # a comment that says 'never ground or VBUS' describes neither net
            self.assertFalse(
                [c for c in self.ix["nets"][n]["comments"] if c.startswith("DRAIN")], n
            )
        self.assertIn(
            "Unused SBU paths and USB ESD channels. USB data has its own local clamp.",
            self.ix["nets"]["lv"]["comments"],
        )
        vbus = self.ix["nets"]["raw_usb-hv"]["comments"]
        self.assertTrue(
            all(c.rstrip().endswith((".", "…", "V")) for c in vbus), vbus
        )  # whole paragraphs, never a half line
        self.assertTrue(
            any(
                c.startswith("User decision 2026-09-26: VBUS pads") and "full-current trace" in c
                for c in vbus
            ),
            vbus,
        )
        self.assertEqual(
            self.ix["nets"]["DRAIN"]["comments"][0], drain
        )  # other pins' sentences are dropped from a shared paragraph
        self.assertEqual(
            self.ix["nets"]["DBG_ACC"]["comments"],
            ["Unused open-drain indicators PLUG_EVENT/PLUG_FLIP/DBG_ACC are left open."],
        )
        self.assertIn(
            "Enable connector-side dead-battery Rd", " ".join(self.ix["nets"]["A5"]["comments"])
        )

    def test_signal_semantics(self):
        sda = self.ix["nets"]["sda"]
        self.assertEqual(sda["aliases"][0]["path"], "board.i2c_sda")
        self.assertIn("board.pd.sda", [a["path"] for a in sda["aliases"]])
        self.assertTrue(self.ix["nets"]["board-1-4"]["title"].startswith("led0.chan_out"))
        self.assertEqual({p["polarity"] for p in self.ix["nets"]["Dpos"]["pairs"]}, {"p"})
        self.assertIn(
            "DRAIN pins/pad share an isolated copper island",
            " ".join(self.ix["nets"]["DRAIN"]["comments"]),
        )
        self.assertTrue(
            all(
                not re.fullmatch(r".*\._p\.p\d", a["path"]) and ".pin" not in a["path"]
                for n in self.ix["nets"].values()
                for a in n["aliases"]
            )
        )

    @unittest.skipUnless(PINOUT.is_dir(), "stale pinout build output missing")
    def test_chains_match_stale_pinout(self):
        by = {c["address"]: c for c in self.ix["components"].values()}
        n = 0
        for f in sorted(glob.glob(str(PINOUT / "*.json"))):
            d = json.loads(Path(f).read_text())
            c = by.get(d["atoAddress"])
            if not c:
                continue
            segs = [s.split("::", 1) for s in d["typeName"].split("|")]
            parent, mine = "SplancMini", []
            for ch in c["chain"]:
                mine.append((ch["file"], f"{parent}.{ch['address'].rsplit('.', 1)[-1]}"))
                parent = ch["module"]
            self.assertEqual(mine, [tuple(s) for s in segs[:-1]], d["atoAddress"])
            self.assertEqual(parent, segs[-1][1])
            n += 1
        self.assertGreater(n, 100)

    def test_from_defaults(self):
        d = SourceService.from_defaults(runtime=SRC.parents[2] / "pnr", graph=GRAPH)
        self.assertEqual((d.src, d.index()["validation"]["nets_matched"]), (SRC.resolve(), 93))

    def test_cache_bytes_and_reload(self):
        self.assertIs(self.svc.index(), self.ix)
        b = self.svc.index_bytes()
        self.assertIs(b, self.svc.index_bytes())
        self.assertEqual(json.loads(b)["sha"], self.ix["sha"])
        self.assertTrue(list(Path(self.tmp.name).glob("source-index-*.json")))
        again = SourceService(SRC, GRAPH, cache_dir=self.tmp.name).index()
        self.assertEqual(
            (again["sha"], again["dossier_sha"]), (self.ix["sha"], self.ix["dossier_sha"])
        )

    def test_llm_merge_requires_matching_dossier(self):
        sha, dos = self.svc.dossier()
        self.assertEqual(sha, self.ix["dossier_sha"])
        self.assertEqual(len(dos), 93)
        f = Path(self.tmp.name) / "llm.json"
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha="0" * 64,
                    model="x",
                    generated_at="t",
                    nets=dict(hv=dict(label="bad", summary="bad")),
                )
            )
        )
        self.assertEqual(self.svc.merge_llm(f), 0)
        self.assertNotIn("llm", self.svc.net("hv"))
        self.assertTrue(self.svc.index()["llm"]["stale"])
        # same mechanical dossier but an older prompt version (or none): stale, never served as current
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha=sha,
                    source_sha=sha,
                    prompt_version=net_llm.PROMPT_VERSION - 1,
                    model="sonnet",
                    generated_at="t",
                    nets=dict(hv=dict(label="old", summary="old")),
                )
            )
        )
        self.assertEqual(self.svc.merge_llm(), 0)
        self.assertTrue(self.svc.index()["llm"]["stale"])
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha="e" * 64,
                    source_sha=sha,
                    prompt_version=net_llm.PROMPT_VERSION,
                    model="sonnet",
                    generated_at="2026-09-29T00:00:00Z",
                    nets=dict(
                        hv=dict(label="3V3 analog", summary="Quiet sensor rail."),
                        nope=dict(label="x"),
                    ),
                )
            )
        )
        self.assertEqual(self.svc.merge_llm(), 1)
        self.assertEqual(self.svc.net("hv")["llm"]["label"], "3V3 analog")
        self.assertEqual(json.loads(self.svc.index_bytes())["nets"]["hv"]["llm"]["model"], "sonnet")
        f.write_text(
            json.dumps(
                dict(
                    dossier_sha="f" * 64,
                    source_sha=sha,
                    prompt_version=net_llm.PROMPT_VERSION,
                    model="opus",
                    generated_at="t",
                    nets=dict(lv=dict(label="Ground", summary="Return.")),
                )
            )
        )
        self.assertEqual(self.svc.merge_llm(), 1)
        self.assertEqual(
            (self.svc.net("lv")["llm"]["model"], "llm" in self.svc.net("hv")), ("opus", False)
        )
        self.svc.llm_model = "sonnet"  # a configured model rejects labels made by another one
        self.assertEqual(self.svc.merge_llm(), 0)
        self.assertTrue(self.svc.index()["llm"]["stale"])
        self.svc.llm_model = None
        f.unlink()
        self.assertEqual(self.svc.merge_llm(), 0)
        self.assertNotIn("llm", self.svc.net("hv"))

    def test_file_and_rejections(self):
        f = self.svc.file("system_5v.ato")
        self.assertIn("output_cap1 = new C22u", f["text"])
        self.assertEqual(f["lines"], len(f["text"].splitlines()))
        self.assertIn("system_5v.ato", {x["path"] for x in self.ix["files"]})
        part = next(x["path"] for x in self.ix["files"] if x["kind"] == "part")
        self.assertTrue(self.svc.file(part)["text"])
        for bad in (
            "../system_5v.ato",
            "/etc/passwd",
            "/" + str(SRC / "system_5v.ato"),
            "parts/../../system_5v.ato",
            "parts/../system_5v.ato",
            "./system_5v.ato",
            "system_5v.ato.orig",
            "splanc_mini.ato.orig",
            "x.txt",
            "",
            "a\\b.ato",
            ".hidden.ato",
            "C:/x.ato",
            "~/x.ato",
            "parts//x.ato",
            "system_5v.ato\0.ato",
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
        comp = lambda ref, addr: dict(
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
        self.svc._checked = 0.0
        ix = self.svc.index()
        self.assertNotEqual(ix["sha"], a)
        self.assertIn("primary rail", ix["nets"]["hv"]["title"])
        p.write_text(
            TOP.replace("module Top:\n    board = new Core\n", "")
        )  # no entry left: keep the last good index
        self.svc._checked = 0.0
        self.assertIs(self.svc.index(), ix)
        self.assertIn("no entry", self.svc.describe()["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
