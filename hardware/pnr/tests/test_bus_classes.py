"""Bus-type annotation classes (PNR_BUS_CLASSES=1, src15): library, derivation,
derived numbers vs hand calculations (usb2-fs / usb2-hs on JLC04161H-7628),
explicit-over-derived precedence, provenance, rules compilation and the router's
stub delay.

The pure classes run under the PnR runtime and KiCad Python 3.9; the router class
needs pcbnew (KiCad Python) and skips elsewhere.

  runtime: PYTHONPATH=$PWD/..:$PWD .../pnr-regression-runtime/bin/python -m unittest -v test_bus_classes
  KiCad:   PYTHONPATH=$PWD/..:$PWD $PNR_KICAD_PYTHON -m unittest -v test_bus_classes
"""

import copy
import dataclasses
import json
import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pcbnew as k
except ImportError:
    k = None

from pnr.si import bus_classes as B

TMP = tempfile.TemporaryDirectory(prefix="pnr-bus-classes-")

RULES = {
    "fab": {"track_width_mm": 0.2, "clearance_mm": 0.15},
    "electrical_fab": {"board_thickness_mm": 1.6},
    "net_classes": [{"name": "gnd", "nets": ["GND"], "plane_layer": "In1.Cu"}],
    "diff_pairs": [
        {"name": "usb", "p": "Dpos", "n": "Dneg", "width_mm": 0.2, "gap_mm": 0.15, "skew_mm": 0.3}
    ],
}
ANN = {"name": "usb", "class": "usb2-fs", "max_uncoupled_mm": 2, "reference_layer": "In1.Cu"}


def env(**values):
    class _Env:
        def __enter__(self):
            self.saved = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(value)

        def __exit__(self, *exc):
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _Env()


def apply(ann=None, pair=None, rules=None, where=None):
    rules = copy.deepcopy(rules or RULES)
    pair = copy.deepcopy(pair or rules["diff_pairs"][0])
    rec = B.apply_class(pair, dict(ann or ANN), rules, where=where)
    return pair, rec


class LibraryTest(unittest.TestCase):
    def test_schema_and_every_cited_source_exists(self):
        lib = B.load()
        self.assertEqual((lib["schema"], lib["schema_version"]), (B.SCHEMA, B.SCHEMA_VERSION))
        for cid in (
            "usb2-ls",
            "usb2-fs",
            "usb2-hs",
            "usb3-ss-gen1",
            "usb3-ss-gen2",
            "i2c-standard",
            "i2c-fast",
            "i2c-fast-plus",
            "spi",
        ):
            self.assertIn(cid, lib["classes"])
        for cid, cls in lib["classes"].items():
            self.assertTrue(B._cited(cls) <= set(lib["sources"]), cid)
            sig = cls["signalling"]
            # every numeric requirement carries a citation (or is null with a note)
            for key in ("data_rate_bps", "t_rise_min_ps"):
                v = sig[key]
                self.assertTrue(v.get("cite") or v.get("note") or v.get("expr"), (cid, key))
            if cls["impedance"].get("differential_ohm") is not None:
                self.assertIn("cite", cls["impedance"])
            if cls["skew"].get("max_ps") is not None:
                self.assertIn("cite", cls["skew"])

    def test_spec_values(self):
        c = B.load()["classes"]
        self.assertEqual(c["usb2-ls"]["signalling"]["t_rise_min_ps"]["value"], 75000)
        self.assertEqual(c["usb2-fs"]["signalling"]["t_rise_min_ps"]["value"], 4000)
        self.assertEqual(c["usb2-fs"]["signalling"]["t_rise_max_ps"]["value"], 20000)
        self.assertEqual(c["usb2-hs"]["signalling"]["t_rise_min_ps"]["value"], 500)
        self.assertEqual(
            (
                c["usb2-hs"]["impedance"]["differential_ohm"],
                c["usb2-hs"]["impedance"]["tolerance_pct"],
            ),
            (90, 15),
        )
        self.assertEqual(c["usb2-fs"]["skew"]["max_ps"], 100)
        self.assertEqual(c["usb3-ss-gen1"]["skew"]["max_ps"], 15)
        self.assertIsNone(c["usb3-ss-gen1"]["signalling"]["t_rise_min_ps"]["value"])

    def test_bad_schema_is_refused(self):
        bad = Path(TMP.name) / "bad.json"
        bad.write_text(json.dumps({"schema": B.SCHEMA, "schema_version": 99, "classes": {}}))
        with self.assertRaises(B.BusClassError):
            B.load(bad)
        bad.write_text(
            json.dumps(
                {
                    "schema": B.SCHEMA,
                    "schema_version": 1,
                    "sources": {},
                    "classes": {
                        "x": {
                            "signalling": {
                                "t_rise_min_ps": {"value": 1, "cite": {"source": "nope"}}
                            },
                            "impedance": {},
                            "skew": {},
                        }
                    },
                }
            )
        )
        with self.assertRaises(B.BusClassError):
            B.load(bad)

    def test_si_model_library_skips_the_class_file(self):
        from pnr.si import models

        lib = models.Library(models.models_dir())
        self.assertTrue(Path(models.models_dir(), "bus_classes.json").exists())
        self.assertFalse(any("bus_classes" in v["path"] for v in lib._by.values()))

    def test_file_override(self):
        with env(PNR_BUS_CLASSES_FILE=str(Path(TMP.name) / "x.json")):
            self.assertEqual(B.classes_path(), Path(TMP.name) / "x.json")
        with env(PNR_BUS_CLASSES_FILE=None, PNR_SI_MODELS="/tmp/m"):
            self.assertEqual(B.classes_path(), Path("/tmp/m/bus_classes.json"))


class DeriveTest(unittest.TestCase):
    def test_usb2_fs_owner_decision_400ps_stub_with_barrel(self):
        pair, rec = apply()
        d = rec["derived"]
        self.assertEqual(rec["t_rise_min_ps"]["value"], 4000.0)
        self.assertEqual(d["stub_delay_max_ps"]["value"], 400.0)
        self.assertEqual(d["stub_delay_max_ps"]["formula"], "stub_k * t_rise_min_ps")
        self.assertEqual(pair["stub_limit"]["kind"], "delay")
        self.assertEqual(pair["stub_limit"]["max_ps"], 400.0)
        self.assertIn("pad centre", pair["stub_limit"]["reading"])
        self.assertIn("barrel", pair["stub_limit"]["reading"])
        st = rec["stackup"]
        self.assertEqual(st["pair_layers"], ["F.Cu", "B.Cu"])  # the pair router's layers
        # F.Cu microstrip over In1 (the si physics anchor: eps_eff 2.99)
        self.assertAlmostEqual(st["layers"]["F.Cu"]["eps_eff"], 2.99, places=2)
        self.assertAlmostEqual(
            st["layers"]["F.Cu"]["td_ps_per_mm"],
            math.sqrt(st["layers"]["F.Cu"]["eps_eff"]) / B.C_MM_PER_PS,
            places=4,
        )
        barrel = st["barrel"]
        self.assertEqual(barrel["length_mm"], 1.6)
        self.assertAlmostEqual(
            barrel["ps"], 1.6 * math.sqrt(barrel["er"]) / B.C_MM_PER_PS, places=4
        )
        # the search cap never rejects a stub within the delay (fastest layer)
        fastest = min(v["td_ps_per_mm"] for v in st["layers"].values())
        self.assertAlmostEqual(pair["stub_limit"]["search_cap_mm"], 400 / fastest, places=2)

    def test_explicit_values_override_and_the_class_value_is_reported(self):
        pair, rec = apply()
        a = rec["applied"]
        self.assertEqual((pair["skew_mm"], a["skew_mm"]["source"]), (0.3, "explicit"))
        self.assertEqual(
            (pair["max_uncoupled_mm"], a["max_uncoupled_mm"]["source"]), (2.0, "explicit")
        )
        slow = rec["stackup"]["slowest_layer"]
        td = rec["stackup"]["layers"][slow]["td_ps_per_mm"]
        self.assertAlmostEqual(a["skew_mm"]["class_derived"], 100 / td, places=2)
        self.assertAlmostEqual(a["max_uncoupled_mm"]["class_derived"], 0.1 * 4000 / td, places=2)

    def test_annotation_overrides(self):
        pair, rec = apply(dict(ANN, skew_mm=0.1, t_rise_ns=2, stub_k=0.2))
        self.assertEqual(pair["skew_mm"], 0.1)
        self.assertEqual(rec["t_rise_min_ps"]["source"], "explicit")
        self.assertEqual(
            rec["t_rise_min_ps"]["class_value"], 4000.0
        )  # recorded beside the explicit value
        self.assertEqual(pair["stub_limit"]["max_ps"], 400.0)  # 0.2 * 2000
        pair, _ = apply(dict(ANN, stub_delay_max_ps=250))
        self.assertEqual(
            (pair["stub_limit"]["max_ps"], pair["stub_limit"]["source"]), (250.0, "explicit")
        )
        pair, _ = apply(dict(ANN, stub_max_mm=1.0))
        self.assertEqual(pair["stub_limit"], dict(pair["stub_limit"], kind="mm", max_mm=1.0))
        pair, _ = apply(dict(ANN, skew_ps=50))
        self.assertLess(pair["skew_mm"], 10)

    def test_defaulted_constraint_skew_takes_the_class_value(self):
        rules = copy.deepcopy(RULES)
        rules["diff_pairs"][0]["defaulted"] = ["skew_mm"]
        pair, rec = apply(pair=rules["diff_pairs"][0], rules=rules)
        self.assertEqual(rec["applied"]["skew_mm"]["source"], "class")
        self.assertAlmostEqual(pair["skew_mm"], rec["derived"]["skew_mm"]["value"])

    def test_class_derives_max_uncoupled_when_not_explicit(self):
        ann = dict(ANN)
        ann.pop("max_uncoupled_mm")
        pair, rec = apply(ann)
        self.assertEqual(rec["applied"]["max_uncoupled_mm"]["source"], "class")
        self.assertGreater(pair["max_uncoupled_mm"], 50)

    def test_usb3_needs_a_rise_time_for_delay_rules(self):
        ann = dict(ANN, **{"class": "usb3-ss-gen1"})
        pair, rec = apply(ann)
        self.assertNotIn("stub_limit", pair)
        self.assertNotIn("stub_delay_max_ps", rec["derived"])
        ann.pop("max_uncoupled_mm")
        with self.assertRaises(B.BusClassError):
            apply(ann)
        pair, rec = apply(dict(ann, t_rise_ns=0.03))
        self.assertAlmostEqual(pair["stub_limit"]["max_ps"], 3.0)

    def test_errors(self):
        with self.assertRaises(B.BusClassError):
            apply(dict(ANN, **{"class": "no-such-bus"}))
        with self.assertRaises(B.BusClassError):
            apply(dict(ANN, **{"class": "i2c-fast"}))  # single-ended class on a pair
        with self.assertRaises(B.BusClassError):
            B.derive("i2c-fast", {"params": {"nope": 1}}, RULES["diff_pairs"][0], RULES)

    def test_i2c_fall_time_parameter(self):
        r = B.derive("i2c-fast", {}, RULES["diff_pairs"][0], RULES)
        self.assertAlmostEqual(r["t_rise_min_ps"]["value"], 12000.0)  # 20 ns x 3.3/5.5
        r = B.derive("i2c-fast", {"params": {"vdd_v": 5.5}}, RULES["diff_pairs"][0], RULES)
        self.assertAlmostEqual(r["t_rise_min_ps"]["value"], 20000.0)
        r = B.derive("i2c-standard", {}, RULES["diff_pairs"][0], RULES)
        self.assertIsNone(r["t_rise_min_ps"]["value"])
        r = B.derive("spi", {"params": {"t_rise_ns": 2}}, RULES["diff_pairs"][0], RULES)
        self.assertAlmostEqual(r["t_rise_min_ps"]["value"], 2000.0)

    def test_expression_evaluator_is_restricted(self):
        self.assertEqual(B._eval("min(2, 3 * x)", {"x": 0.5}), 1.5)
        with self.assertRaises(B.BusClassError):
            B._eval('__import__("os")', {})

    def test_record_is_json_and_deterministic(self):
        _, a = apply()
        _, b = apply()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


# ---------------------------------------------------------------- hand calculations
#
# Independent of pnr.si.physics: the JLC04161H-7628 stack typed in from
# docs/fab-comparison.md (+ the jlc-pofv copper: 35 um outer, 15.2 um inner), In1.Cu the
# only plane (the net classes' plane_layer), 0.20 mm traces, and the published closed
# forms written out again here. c = 0.299792458 mm/ps.
C = 0.299792458
PP, CORE, ER_PP, ER_CORE, T_OUT, T_IN, W = 0.2104, 1.065, 4.4, 4.6, 0.035, 0.0152, 0.2


def hand_microstrip(w, h, t, er):
    """Hammerstad & Jensen (IEEE MTT-S 1980) eps_eff with the finite-thickness correction."""
    u, tn = w / h, t / h
    du1 = tn / math.pi * math.log(1 + 4 * math.e / (tn / math.tanh(math.sqrt(6.517 * u)) ** 2))
    dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(er - 1))) * du1
    z = lambda x: 60 * math.log(
        (6 + (2 * math.pi - 6) * math.exp(-((30.666 / x) ** 0.7528))) / x + math.sqrt(1 + 4 / x**2)
    )
    a = (
        lambda x: 1
        + math.log((x**4 + (x / 52) ** 2) / (x**4 + 0.432)) / 49
        + math.log(1 + (x / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    e0 = lambda x: (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / x) ** (-a(x) * b)
    return e0(u + dur) * (z(u + du1) / z(u + dur)) ** 2


def hand_stack():
    """{layer: td ps/mm} and the F.Cu->B.Cu barrel (er, ps/mm, ps) by hand."""
    ee = {
        "F.Cu": hand_microstrip(W, PP, T_OUT, ER_PP),
        # In2.Cu: embedded microstrip over In1 (IPC-2141A): er' = er (1 - exp(-1.55 H1/h)),
        # h = core, H1 = core + In2 copper + prepreg-2 (plane to the covering surface)
        "In2.Cu": ER_CORE * (1 - math.exp(-1.55 * (CORE + T_IN + PP) / CORE)),
        # B.Cu: microstrip over In1 across core + etched In2 + prepreg-2, er thickness-weighted
        "B.Cu": hand_microstrip(
            W, CORE + T_IN + PP, T_OUT, (CORE * ER_CORE + PP * ER_PP) / (CORE + PP)
        ),
    }
    td = {n: math.sqrt(e) / C for n, e in ee.items()}
    er_via = (2 * PP * ER_PP + CORE * ER_CORE) / (2 * PP + CORE)
    return ee, td, dict(er=er_via, td=math.sqrt(er_via) / C, ps=1.6 * math.sqrt(er_via) / C)


class HandCalcTest(unittest.TestCase):
    """Derived numbers for usb2-fs / usb2-hs on the JLC stack vs hand calculations."""

    def setUp(self):
        self.ee, self.td, self.barrel = hand_stack()

    def test_hand_stack_literals(self):
        # the hand numbers themselves (4 significant figures), so both sides cannot drift together
        self.assertEqual(
            {n: round(v, 4) for n, v in self.td.items()},
            {"F.Cu": 5.7706, "In2.Cu": 6.5848, "B.Cu": 5.6903},
        )
        self.assertEqual(
            {n: round(v, 3) for n, v in self.ee.items()},
            {"F.Cu": 2.993, "In2.Cu": 3.897, "B.Cu": 2.910},
        )
        self.assertEqual(
            (round(self.barrel["er"], 4), round(self.barrel["td"], 4), round(self.barrel["ps"], 3)),
            (4.5434, 7.11, 11.376),
        )

    def test_stackup_matches_hand(self):
        _, rec = apply()
        st = rec["stackup"]
        self.assertEqual(
            (st["stackup"], st["fab_profile"], st["planes"], st["pair_layers"]),
            ("JLC04161H-7628", "jlc-pofv", ["In1.Cu"], ["F.Cu", "B.Cu"]),
        )
        self.assertEqual(
            set(st["layers"]), {"F.Cu", "B.Cu"}
        )  # the pair's layers only (no plane, no In2)
        for n in ("F.Cu", "B.Cu"):
            self.assertAlmostEqual(st["layers"][n]["eps_eff"], self.ee[n], places=5)
            self.assertAlmostEqual(st["layers"][n]["td_ps_per_mm"], self.td[n], places=5)
        # the full stack table (every signal layer) still matches the hand In2.Cu model
        from pnr.si import physics

        full = B.layer_table(physics.stackup(RULES), W, 0.15)
        self.assertAlmostEqual(full["In2.Cu"]["td_ps_per_mm"], self.td["In2.Cu"], places=5)
        self.assertEqual(
            [(x["name"], x["t_mm"], x.get("er")) for x in st["stack"]],
            [
                ("F.Cu", T_OUT, None),
                ("prepreg-1", PP, ER_PP),
                ("In1.Cu", T_IN, None),
                ("core", CORE, ER_CORE),
                ("In2.Cu", T_IN, None),
                ("prepreg-2", PP, ER_PP),
                ("B.Cu", T_OUT, None),
            ],
        )
        self.assertAlmostEqual(st["barrel"]["er"], self.barrel["er"], places=5)
        self.assertAlmostEqual(st["barrel"]["td_ps_per_mm"], self.barrel["td"], places=5)
        self.assertAlmostEqual(st["barrel"]["ps"], self.barrel["ps"], places=5)
        self.assertEqual((st["slowest_layer"], st["fastest_layer"]), ("F.Cu", "B.Cu"))

    def check_class(self, cid, t_rise_ps, literals):
        pair, rec = apply(dict(ANN, **{"class": cid}))
        d, slow = rec["derived"], self.td["F.Cu"]  # slowest of the pair's layers (F.Cu, B.Cu)
        stub = 0.1 * t_rise_ps
        self.assertEqual(rec["t_rise_min_ps"]["value"], t_rise_ps)
        self.assertAlmostEqual(d["stub_delay_max_ps"]["value"], stub, places=6)
        self.assertEqual(pair["stub_limit"]["max_ps"], stub)
        eq = d["stub_max_mm_equivalent"]["per_layer"]
        self.assertEqual(set(eq), {"F.Cu", "B.Cu"})
        self.assertAlmostEqual(
            d["stub_max_mm_equivalent"]["value"], stub / self.td["B.Cu"], places=3
        )
        for n, td in ((n, self.td[n]) for n in ("F.Cu", "B.Cu")):
            self.assertAlmostEqual(eq[n]["no_via"], stub / td, places=3)
            self.assertAlmostEqual(eq[n]["one_via"], (stub - self.barrel["ps"]) / td, places=3)
        self.assertAlmostEqual(d["max_uncoupled_mm"]["value"], 0.1 * t_rise_ps / slow, places=3)
        self.assertAlmostEqual(d["skew_mm"]["value"], 100 / slow, places=3)
        self.assertAlmostEqual(d["max_length_mm"]["value"], 1000 / slow, places=3)
        self.assertAlmostEqual(
            pair["stub_limit"]["search_cap_mm"], stub / self.td["B.Cu"], places=3
        )
        self.assertEqual(pair["stub_limit"]["barrel_mm"], 1.6)
        self.assertAlmostEqual(pair["stub_limit"]["barrel_ps_per_mm"], self.barrel["td"], places=5)
        # the literal numbers reported in WORKLOG-src15 section 4.4
        got = dict(
            stub_ps=d["stub_delay_max_ps"]["value"],
            fcu=eq["F.Cu"]["no_via"],
            fcu_via=eq["F.Cu"]["one_via"],
            bcu=eq["B.Cu"]["no_via"],
            bcu_via=eq["B.Cu"]["one_via"],
            uncoupled=d["max_uncoupled_mm"]["value"],
            skew=d["skew_mm"]["value"],
            length=d["max_length_mm"]["value"],
            cap=pair["stub_limit"]["search_cap_mm"],
        )
        self.assertEqual(got, literals)
        return pair, rec

    def test_usb2_fs(self):
        pair, rec = self.check_class(
            "usb2-fs",
            4000.0,
            dict(
                stub_ps=400.0,
                fcu=69.317,
                fcu_via=67.345,
                bcu=70.295,
                bcu_via=68.296,
                uncoupled=69.317,
                skew=17.329,
                length=173.291,
                cap=70.295,
            ),
        )
        imp = rec["impedance_check"]
        self.assertEqual(
            (imp["target_ohm"], imp["min_ohm"], imp["max_ohm"], imp["required"]),
            (90.0, 76.5, 103.5, False),
        )

    def test_usb2_hs(self):
        pair, rec = self.check_class(
            "usb2-hs",
            500.0,
            dict(
                stub_ps=50.0,
                fcu=8.665,
                fcu_via=6.693,
                bcu=8.787,
                bcu_via=6.788,
                uncoupled=8.665,
                skew=17.329,
                length=173.291,
                cap=8.787,
            ),
        )
        imp = rec["impedance_check"]
        self.assertTrue(imp["required"])
        self.assertEqual(imp["spec_basis"], "interpretation")  # USB 2.0 sets no PCB impedance
        self.assertEqual(
            {n: v["within"] for n, v in imp["per_layer"].items()}, {"F.Cu": True, "B.Cu": False}
        )

    def test_host_port_allocation(self):
        # USB 2.0 7.1.16: 3 ns from a host/hub downstream-facing transceiver to its connector
        _, rec = apply(dict(ANN, params={"board_delay_ns": 3}))
        self.assertAlmostEqual(
            rec["derived"]["max_length_mm"]["value"], 3000 / self.td["F.Cu"], places=3
        )


class PrecedenceTest(unittest.TestCase):
    """Explicit values override class-derived ones, in the documented order."""

    PAIR = dict(RULES["diff_pairs"][0])

    def test_skew_order(self):
        slow = hand_stack()[1]["F.Cu"]
        undef = dict(self.PAIR, defaulted=["skew_mm"])
        cases = [
            (dict(ANN, skew_mm=0.1, skew_ps=50), self.PAIR, 0.1, "explicit"),  # annotation mm
            (
                dict(ANN, skew_ps=50),
                self.PAIR,
                round(50 / slow, 3),
                "explicit skew_ps",
            ),  # annotation ps
            (ANN, self.PAIR, 0.3, "explicit"),  # constraint file
            (ANN, undef, round(100 / slow, 3), "class"),
        ]  # class (constraint defaulted)
        for ann, pair, value, source in cases:
            got, rec = apply(ann, pair)
            self.assertEqual(got["skew_mm"], value, source)
            self.assertTrue(
                rec["applied"]["skew_mm"]["source"].startswith(source), rec["applied"]["skew_mm"]
            )
            self.assertEqual(rec["applied"]["skew_mm"]["class_derived"], round(100 / slow, 3))

    def test_uncoupled_and_length_order(self):
        pair, rec = apply(dict(ANN, max_length_mm=80))
        self.assertEqual(
            (pair["max_uncoupled_mm"], rec["applied"]["max_uncoupled_mm"]["source"]),
            (2.0, "explicit"),
        )
        self.assertEqual(
            (rec["applied"]["max_length_mm"]["value"], rec["applied"]["max_length_mm"]["source"]),
            (80.0, "explicit"),
        )
        self.assertFalse(rec["applied"]["max_length_mm"]["enforced"])
        self.assertNotIn("max_length_mm", pair)  # report-only: nothing the router reads
        ann = {k: v for k, v in ANN.items() if k != "max_uncoupled_mm"}
        pair, rec = apply(ann)
        self.assertEqual(rec["applied"]["max_uncoupled_mm"]["source"], "class")
        self.assertEqual(rec["applied"]["max_length_mm"]["source"], "class")
        # explicit uncoupled_k / t_rise_ns feed the class rule
        pair, rec = apply(dict(ann, uncoupled_k=0.05, t_rise_ns=8))
        self.assertAlmostEqual(
            pair["max_uncoupled_mm"], round(0.05 * 8000 / hand_stack()[1]["F.Cu"], 3)
        )
        self.assertEqual(
            rec["derived"]["max_uncoupled_mm"]["inputs"]["uncoupled_k"]["source"], "explicit"
        )

    def test_stub_order(self):
        both = dict(ANN, stub_max_mm=1.5, stub_delay_max_ps=250, t_rise_ns=2, stub_k=0.2)
        for ann, expect in (
            (both, dict(kind="mm", max_mm=1.5, source="explicit")),
            (
                dict(ANN, stub_delay_max_ps=250, t_rise_ns=2),
                dict(kind="delay", max_ps=250.0, source="explicit"),
            ),
            (dict(ANN, t_rise_ns=2, stub_k=0.2), dict(kind="delay", max_ps=400.0, source="class")),
            (dict(ANN, t_rise_ns=2), dict(kind="delay", max_ps=200.0, source="class")),
            (ANN, dict(kind="delay", max_ps=400.0, source="class")),
        ):
            pair, rec = apply(ann)
            self.assertEqual({k: pair["stub_limit"].get(k) for k in expect}, expect, ann)
        # the router: PNR_PAIR_STUB_MAX_MM > annotation mm > delay (searched with its cap)
        delay_pair, _ = apply()
        mm_pair, _ = apply(dict(ANN, stub_max_mm=1.5))
        with env(PNR_BUS_CLASSES="1"):
            self.assertEqual(
                B.router_stub_rule(delay_pair, 1.0), (1.0, None, "PNR_PAIR_STUB_MAX_MM")
            )
            self.assertEqual(B.router_stub_rule(mm_pair, 1.0)[0], 1.0)
            self.assertEqual(
                B.router_stub_rule(mm_pair, None), (1.5, None, "annotation stub_max_mm")
            )
            cap, delay, source = B.router_stub_rule(delay_pair, None)
            self.assertEqual(
                (cap, delay["max_ps"], source),
                (delay_pair["stub_limit"]["search_cap_mm"], 400.0, "delay (class)"),
            )
            self.assertEqual(B.router_stub_rule(dict(self.PAIR), None), (None, None, None))
        with env(PNR_BUS_CLASSES=None):
            self.assertEqual(B.router_stub_rule(delay_pair, None), (None, None, None))

    def test_params_and_class_k(self):
        lib = copy.deepcopy(B.load())
        lib["classes"]["usb2-fs"]["stub_k"] = 0.05  # class-level k beats the library default
        rules = copy.deepcopy(RULES)
        p = copy.deepcopy(rules["diff_pairs"][0])
        rec = B.apply_class(p, dict(ANN), rules, lib=lib)
        self.assertEqual(
            (
                p["stub_limit"]["max_ps"],
                rec["derived"]["stub_delay_max_ps"]["inputs"]["stub_k"]["source"],
            ),
            (200.0, "class"),
        )
        rec = B.apply_class(
            copy.deepcopy(rules["diff_pairs"][0]), dict(ANN, stub_k=0.1), rules, lib=lib
        )
        self.assertEqual(
            rec["derived"]["stub_delay_max_ps"]["value"], 400.0
        )  # annotation beats the class
        self.assertEqual(rec["params"], dict(values={"board_delay_ns": 1}, explicit=[]))
        _, rec = apply(dict(ANN, params={"board_delay_ns": 3}))
        self.assertEqual(
            rec["params"], dict(values={"board_delay_ns": 3}, explicit=["board_delay_ns"])
        )


class ProvenanceTest(unittest.TestCase):
    """rules.json records every derived value with class id, citation, formula and stackup inputs."""

    def test_every_derived_value(self):
        lib = B.load()
        for cid in ("usb2-fs", "usb2-hs"):
            _, rec = apply(dict(ANN, **{"class": cid}))
            self.assertEqual(
                set(rec["derived"]),
                {
                    "stub_delay_max_ps",
                    "stub_max_mm_equivalent",
                    "max_uncoupled_mm",
                    "skew_mm",
                    "max_length_mm",
                },
            )
            for key, v in rec["derived"].items():
                self.assertEqual(v["class_id"], cid, key)
                self.assertTrue(v["formula"], key)
                self.assertIsNotNone(v.get("value"), key)  # every entry: value + inputs
                self.assertTrue(v.get("inputs"), key)
                self.assertTrue(v["citation"], key)
                for cite in v["citation"]:
                    self.assertIn(cite["source"], lib["sources"], key)
                    self.assertTrue(cite["ref"], key)
                si = v["stackup_inputs"]
                self.assertEqual(
                    (si["stackup"], si["fab_profile"], si["planes"]),
                    ("JLC04161H-7628", "jlc-pofv", ["In1.Cu"]),
                    key,
                )
                self.assertEqual(si["line_models"], "bus_class.stackup.layers", key)
                for name, td in si["td_ps_per_mm"].items():
                    model = rec["stackup"]["layers"][name]  # the full line model it points to
                    self.assertEqual(model["td_ps_per_mm"], td, key)
                    self.assertTrue(
                        {"td_ps_per_mm", "eps_eff", "model", "h_mm", "er"} <= set(model), key
                    )
            d = rec["derived"]
            self.assertEqual(
                d["stub_delay_max_ps"]["citation"][0]["ref"],
                lib["classes"][cid]["signalling"]["t_rise_min_ps"]["cite"]["ref"],
            )
            self.assertEqual(
                d["skew_mm"]["citation"][0]["ref"], "7.1.3 Cable Skew; Table 7-12 TSKEW"
            )
            self.assertEqual(
                d["max_length_mm"]["citation"][0]["ref"], "7.1.16 Cable Delay; Figure 7-39"
            )
            self.assertEqual(
                d["stub_delay_max_ps"]["stackup_inputs"]["barrel_ps"],
                rec["stackup"]["barrel"]["ps"],
            )
            self.assertEqual(
                d["skew_mm"]["spec_basis"], "interpretation"
            )  # cable TSKEW applied to the board
            self.assertIn("cable", d["skew_mm"]["interpretation"])
            self.assertEqual(d["max_length_mm"]["spec_basis"], "spec")  # 7.1.16 board allocation
            self.assertEqual(
                d["stub_delay_max_ps"]["inputs"]["stub_k"]["source"], "library default"
            )
            self.assertIn("owner decision", d["stub_delay_max_ps"]["inputs"]["stub_k"]["basis"])
            # the library file itself
            raw = B.DEFAULT_FILE.read_bytes()
            import hashlib

            self.assertEqual(rec["library"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(
                rec["library"]["file"], "si_models/bus_classes.json"
            )  # no run-directory path
            self.assertEqual(set(rec["sources"]), {"usb2"})
            self.assertIn("formula", rec["stackup"]["barrel"])

    def test_applied_records_where_explicit_values_came_from(self):
        _, rec = apply(dict(ANN), where=dict(file="x.ato", line=223))
        a = rec["applied"]
        self.assertEqual(
            a["max_uncoupled_mm"]["where"],
            dict(source="@pnr-pair annotation", file="x.ato", line=223),
        )
        self.assertEqual(a["skew_mm"]["where"], dict(source="constraints diff_pair (rules.json)"))
        self.assertEqual((a["stub"]["source"], a["stub"]["class_derived_ps"]), ("class", 400.0))
        self.assertEqual(
            (a["max_uncoupled_mm"]["class_derived"], a["skew_mm"]["class_derived"]),
            (69.317, 17.329),
        )

    def test_record_has_no_absolute_paths_and_is_compact(self):
        _, rec = apply(dict(ANN, t_rise_ns=4), where=dict(file="x.ato", line=223))
        strings = []

        def walk(x):
            if isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, str):
                strings.append(x)

        walk(rec)
        self.assertFalse([s for s in strings if s.startswith("/")])
        self.assertFalse(any("quote" in c for v in rec["derived"].values() for c in v["citation"]))
        self.assertLess(len(json.dumps(rec)), 11000)


class PathDelayTest(unittest.TestCase):
    def test_planar_legs_and_barrel_hops(self):
        td = {"F.Cu": 5.0, "B.Cu": 6.0}
        name = {0: "F.Cu", 1: "B.Cu"}.get
        nodes = [(0, 0, 0), (0, 2_000_000, 0), (1, 2_000_000, 0), (1, 2_000_000, 3_000_000)]
        ps, parts = B.path_delay(nodes, name, td, 11.0)
        self.assertAlmostEqual(ps, 2 * 5.0 + 11.0 + 3 * 6.0)
        self.assertEqual(parts, {"planar_mm": {"B.Cu": 3.0, "F.Cu": 2.0}, "barrels": 1})
        ps, _ = B.path_delay(nodes, name, td, 11.0, lead=(0, 0.5))
        self.assertAlmostEqual(ps, 2.5 * 5.0 + 11.0 + 3 * 6.0)
        ps, _ = B.path_delay([(7, 0, 0), (7, 1_000_000, 0)], name, td, 0)  # unknown layer: slowest
        self.assertAlmostEqual(ps, 6.0)

    def test_router_limits_are_gated(self):
        pair, _ = apply()
        with env(PNR_BUS_CLASSES=None):
            self.assertIsNone(B.stub_delay_limit(pair))
            self.assertIsNone(B.stub_mm_limit(pair))
        with env(PNR_BUS_CLASSES="1"):
            self.assertEqual(B.stub_delay_limit(pair)["max_ps"], 400.0)
            self.assertIsNone(B.stub_mm_limit(pair))
            mm, _ = apply(dict(ANN, stub_max_mm=1.0))
            self.assertEqual(B.stub_mm_limit(mm), 1.0)
            self.assertIsNone(B.stub_delay_limit(mm))


class StackupCheckTest(unittest.TestCase):
    """Review fixes: the stackup must match the board; the pair's reference layer is a plane;
    time -> length conversions use the pair's layers."""

    def test_layer_count_mismatch_is_refused(self):
        for n in (2, 6):
            rules = dict(copy.deepcopy(RULES), layers=n)
            with self.assertRaises(B.BusClassError) as cm:
                apply(rules=rules)
            self.assertIn("%d copper layers" % n, str(cm.exception))
        _, rec = apply(rules=dict(copy.deepcopy(RULES), layers=4))
        self.assertEqual(rec["stackup"]["layer_count_check"], "match (4 copper layers)")
        _, rec = apply()  # RULES has no count: recorded as unverified
        self.assertIn("unverified", rec["stackup"]["layer_count_check"])

    def test_declared_two_layer_stackup(self):
        st = {
            "name": "two-layer-1.6",
            "layers": [
                {"name": "F.Cu", "kind": "copper", "t_mm": 0.035},
                {"name": "core", "kind": "dielectric", "t_mm": 1.53, "er": 4.5},
                {"name": "B.Cu", "kind": "copper", "t_mm": 0.035},
            ],
            "planes": ["B.Cu"],
        }
        rules = dict(copy.deepcopy(RULES), layers=2, stackup=st, net_classes=[])
        pair = dict(rules["diff_pairs"][0])
        rec = B.apply_class(pair, dict(ANN, reference_layer="B.Cu"), rules)
        s = rec["stackup"]
        self.assertEqual(
            (s["stack_source"], s["pair_layers"], s["planes"]),
            ("rules.stackup", ["F.Cu"], ["B.Cu"]),
        )
        self.assertEqual(s["layers"]["F.Cu"]["model"], "microstrip_hammerstad_jensen")
        self.assertAlmostEqual(s["layers"]["F.Cu"]["h_mm"], 1.53, places=6)

    def test_reference_layer_is_a_plane(self):
        rules = copy.deepcopy(RULES)
        rules["net_classes"] = [{"name": "gnd", "nets": ["GND"], "plane_layer": "In1.Cu"}]
        pair = dict(rules["diff_pairs"][0])
        rec = B.apply_class(pair, dict(ANN, reference_layer="In2.Cu"), rules)
        s = rec["stackup"]
        self.assertEqual(s["planes"], ["In1.Cu", "In2.Cu"])
        self.assertIn("pair reference_layer In2.Cu", s["planes_source"])
        # B.Cu now references In2 across prepreg-2 (0.2104 mm), like F.Cu over In1: same line
        self.assertAlmostEqual(s["layers"]["B.Cu"]["h_mm"], PP, places=6)
        self.assertAlmostEqual(
            s["layers"]["B.Cu"]["td_ps_per_mm"], s["layers"]["F.Cu"]["td_ps_per_mm"], places=6
        )
        with self.assertRaises(B.BusClassError):
            B.apply_class(dict(rules["diff_pairs"][0]), dict(ANN, reference_layer="In9.Cu"), rules)

    def test_pair_layers_override(self):
        rules = copy.deepcopy(RULES)
        pair = dict(rules["diff_pairs"][0], layers=["F.Cu", "In2.Cu"])
        rec = B.apply_class(pair, dict(ANN), rules)
        s = rec["stackup"]
        self.assertEqual(
            (s["pair_layers"], s["pair_layers_source"], s["slowest_layer"]),
            (["F.Cu", "In2.Cu"], "pair layers (rules.json)", "In2.Cu"),
        )
        self.assertAlmostEqual(
            rec["derived"]["skew_mm"]["value"], round(100 / hand_stack()[1]["In2.Cu"], 3), places=3
        )
        with self.assertRaises(B.BusClassError):  # a plane cannot carry the pair
            B.apply_class(dict(rules["diff_pairs"][0], layers=["In1.Cu"]), dict(ANN), rules)


class BasisTest(unittest.TestCase):
    """Every applied impedance / skew / length number says whether the document requires it of
    the board pair ('spec') or this library applies a cable/connector figure ('interpretation')."""

    def test_library_marks_every_value(self):
        lib = B.load()
        c = lib["classes"]
        for cid in ("usb2-fs", "usb2-hs", "usb3-ss-gen1", "usb3-ss-gen2"):
            self.assertEqual(c[cid]["skew"]["basis"], "interpretation", cid)
            self.assertEqual(c[cid]["impedance"]["basis"], "interpretation", cid)
            self.assertTrue(c[cid]["impedance"]["interpretation"], cid)
        for cid in ("usb2-fs", "usb2-hs"):
            self.assertEqual(c[cid]["length"]["basis"], "spec", cid)

    def test_load_refuses_a_value_without_basis(self):
        lib = json.loads(B.DEFAULT_FILE.read_text())
        del lib["classes"]["usb2-fs"]["skew"]["basis"]
        bad = Path(TMP.name) / "nobasis.json"
        bad.write_text(json.dumps(lib))
        with self.assertRaises(B.BusClassError):
            B.load(bad)
        lib = json.loads(B.DEFAULT_FILE.read_text())
        del lib["classes"]["usb2-hs"]["impedance"]["interpretation"]
        bad.write_text(json.dumps(lib))
        with self.assertRaises(B.BusClassError):
            B.load(bad)

    def test_override_file_is_recorded_by_name(self):
        lib = json.loads(B.DEFAULT_FILE.read_text())
        f = Path(TMP.name) / "my-classes.json"
        f.write_text(json.dumps(lib))
        with env(PNR_BUS_CLASSES_FILE=str(f)):
            rec = B.derive("usb2-fs", dict(ANN), RULES["diff_pairs"][0], RULES, lib=B.load())
        self.assertEqual(
            (rec["library"]["file"], rec["library"]["override"]),
            ("my-classes.json", "PNR_BUS_CLASSES_FILE"),
        )


class OrderBridgeCandidatesTest(unittest.TestCase):
    """Review fix: under a delay limit the bridge candidates keep a short-stub preference (src order
    interleaved with short-stub-first), which the 70 mm search cap alone had lost."""

    def setUp(self):
        try:
            from pnr.native_electrical import order_bridge_candidates
        except Exception as error:  # pragma: no cover
            self.skipTest("native_electrical import: %s" % error)
        self.order = order_bridge_candidates
        # src (shift, length) order: target ports with 3.0, 2.0, 1.5, 0.4, 0.9, 80.0 mm stubs
        self.ports = [dict(id=i, stub=s) for i, s in enumerate((3.0, 2.0, 1.5, 0.4, 0.9, 80.0))]
        self.cands = [("a", z) for z in self.ports]
        self.stub_of = lambda port: port["stub"]
        self.delay = (lambda mm: mm * 5.77, 400.0)

    def ids(self, got):
        return [z["id"] for _, z in got]

    def test_mm_cap_is_the_src13_partition(self):
        self.assertEqual(self.ids(self.order(self.cands, 1.0, self.stub_of)), [3, 4, 0, 1, 2, 5])
        # the search cap alone is (almost) no order at all: the src15 regression
        self.assertEqual(self.ids(self.order(self.cands, 70.3, self.stub_of)), [0, 1, 2, 3, 4, 5])

    def test_delay_limit_interleaves_src_order_and_short_stub_first(self):
        # src 0,1,2,3,4 / short-stub-first 3,4,2,1,0 -> 0,3,1,4,2; 80 mm (462 ps > 400 ps, > cap) last
        self.assertEqual(
            self.ids(self.order(self.cands, 70.3, self.stub_of, self.delay)), [0, 3, 1, 4, 2, 5]
        )
        # a src-order winner at position k stays within the first 2k+1; a short-stub winner likewise
        got = self.ids(self.order(self.cands, 70.3, self.stub_of, self.delay))
        self.assertLessEqual(got.index(3), 2 * 0 + 1)

    def test_inline_hunt_cap_zero(self):
        inline = self.cands + [("a", dict(id=6, stub=0.0))]
        # every stub port is over the in-line hunt's 0 mm cap: in-line first, the rest in src order (src13)
        self.assertEqual(
            self.ids(self.order(inline, 0.0, self.stub_of, self.delay)), [6, 0, 1, 2, 3, 4, 5]
        )

    def test_stable_within_a_target_port(self):
        z = self.ports[3]
        cands = [("a2", z), ("a1", z), ("a0", self.ports[0])]
        got = self.order(cands, 70.3, self.stub_of, self.delay)
        self.assertEqual([a for a, _ in got], ["a2", "a1", "a0"])


def fake_components():
    from pnr.graph import Component, Pad

    def comp(ref, address, pads):
        return Component(
            ref=ref,
            address=address,
            footprint="x",
            pos=(0, 0),
            rot=0,
            side="top",
            courtyard=(1, 1),
            bbox=(1, 1),
            pads=[Pad(name=n, net=net, offset=(0, 0)) for n, net in pads],
        )

    return [
        comp("USB1", "board.usbc", [("A6", "Dpos"), ("A7", "Dneg")]),
        comp("D2", "board.usb_data_esd", [("1", "Dpos"), ("2", "Dneg")]),
        comp("U6", "board.esp", [("14", "Dpos"), ("13", "Dneg")]),
    ]


PAIR_LINE = (
    '# @pnr-pair {"name":"usb",%s"terminal_chain":[{"p":"usbc:A6","n":"usbc:A7"},{"p":"usb_data_esd:1","n":"usb_data_esd:2"},'
    '{"p":"esp:14","n":"esp:13"}],"max_uncoupled_mm":2,"reference_layer":"In1.Cu"}\n'
)


class ResolvePairChainsTest(unittest.TestCase):
    def compile(self, cls, flag):
        from pnr.electrical import resolve_pair_chains

        path = Path(TMP.name) / ("mini-%s.ato" % (cls or "none"))
        path.write_text("module X:\n    " + PAIR_LINE % ('"class":"%s",' % cls if cls else ""))
        with env(PNR_BUS_CLASSES="1" if flag else None):
            return resolve_pair_chains(RULES, [path], fake_components())

    def test_flag_off_ignores_the_class_key(self):
        with_class, without = self.compile("usb2-fs", False), self.compile(None, False)
        for r in (with_class, without):
            r["diff_pairs"][0]["source"].pop("sha256")
            r["diff_pairs"][0]["source"].pop("path")
        self.assertEqual(with_class, without)
        self.assertNotIn("bus_class", with_class["diff_pairs"][0])

    def test_flag_on_records_provenance_and_limits(self):
        r = self.compile("usb2-fs", True)
        pair = r["diff_pairs"][0]
        self.assertEqual(pair["terminal_chain"][1], {"p": "D2.1", "n": "D2.2"})
        self.assertEqual(pair["bus_class"]["id"], "usb2-fs")
        self.assertEqual(pair["bus_class"]["library"]["schema"], B.SCHEMA)
        self.assertEqual(len(pair["bus_class"]["library"]["sha256"]), 64)
        self.assertEqual(pair["stub_limit"]["max_ps"], 400.0)
        self.assertEqual(pair["max_uncoupled_mm"], 2.0)
        self.assertEqual(pair["bus_class"]["applied"]["max_uncoupled_mm"]["where"]["line"], 2)
        self.assertEqual(
            pair["bus_class"]["applied"]["max_uncoupled_mm"]["where"]["file"], "mini-usb2-fs.ato"
        )
        self.assertNotIn("path", pair["bus_class"]["applied"]["max_uncoupled_mm"]["where"])
        json.dumps(r)
        # a pair without a class is untouched by the flag
        self.assertNotIn("bus_class", self.compile(None, True)["diff_pairs"][0])

    def test_flag_on_without_a_class_equals_flags_off(self):
        """Review fix: the constraints "defaulted" marker (flag on) never reaches the policy."""
        from pnr.electrical import resolve_pair_chains

        path = Path(TMP.name) / "mini-noclass.ato"
        path.write_text("module X:\n    " + PAIR_LINE % "")
        marked = copy.deepcopy(RULES)
        marked["diff_pairs"][0]["defaulted"] = ["skew_mm"]
        with env(PNR_BUS_CLASSES="1"):
            on = resolve_pair_chains(marked, [path], fake_components())
        with env(PNR_BUS_CLASSES=None):
            off = resolve_pair_chains(RULES, [path], fake_components())
        self.assertEqual(json.dumps(on, sort_keys=True), json.dumps(off, sort_keys=True))
        # with a class the marker still selects the class skew, then is dropped
        cpath = Path(TMP.name) / "mini-classed.ato"
        cpath.write_text("module X:\n    " + PAIR_LINE % '"class":"usb2-fs",')
        with env(PNR_BUS_CLASSES="1"):
            r = resolve_pair_chains(marked, [cpath], fake_components())
        self.assertNotIn("defaulted", r["diff_pairs"][0])
        self.assertEqual(r["diff_pairs"][0]["bus_class"]["applied"]["skew_mm"]["source"], "class")


try:
    import yaml  # noqa: F401  (pnr.constraints needs it; KiCad Python lacks it)

    YAML = True
except ImportError:
    YAML = False


@unittest.skipUnless(YAML, "needs yaml (PnR runtime)")
class ConstraintsDefaultedTest(unittest.TestCase):
    DOC = {
        "board": {"outline": {"w": 10, "h": 10}},
        "diff_pair": [{"name": "usb", "p": "Dpos", "n": "Dneg", "width_mm": 0.2}],
    }

    def rules(self, flag):
        from pnr.constraints import compile_constraints, compile_routing_rules

        with env(PNR_BUS_CLASSES="1" if flag else None):
            cc = compile_constraints(copy.deepcopy(self.DOC), [])
        return cc, compile_routing_rules(cc, ["Dpos", "Dneg"])

    def test_marker_only_with_the_flag(self):
        cc, r = self.rules(False)
        self.assertNotIn("defaulted", r["diff_pairs"][0])
        self.assertNotIn("defaulted", dataclasses.asdict(cc.diff_pairs[0]))
        cc, r = self.rules(True)
        self.assertEqual(r["diff_pairs"][0]["defaulted"], ["skew_mm"])
        self.assertNotIn("defaulted", dataclasses.asdict(cc.diff_pairs[0]))
        self.assertNotIn("_defaulted", dataclasses.asdict(cc.diff_pairs[0]))


@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class RouterStubDelayTest(unittest.TestCase):
    """The real _pair_plan_order on the src13 J -> D -> U chain, with a class stub limit."""

    def plan(self, stub_limit, extra_env=None):
        import test_pair_post_bridge as tpb
        from test_pair_post_bridge import CROSSING, chain_board
        from test_pair_src13 import ChainRunAndStubTest

        real = tpb.chain_board

        def classed():
            b, pair, r = real()
            if stub_limit is not None:
                pair["stub_limit"] = stub_limit
                pair["bus_class"] = {"id": "usb2-fs"}
            return b, pair, r

        t = ChainRunAndStubTest("test_flags_off_report_nothing_new")
        with patch.object(tpb, "chain_board", classed), env(
            PNR_BUS_CLASSES="1", **(extra_env or {})
        ):
            result, calls, bridges, pair = t.plan(
                dict({"PNR_PAIR_POST_BRIDGE_SURFACE": "1"}, **(extra_env or {})), CROSSING
            )
        return result

    LIMIT = dict(
        kind="delay",
        max_ps=400.0,
        reading=B.STUB_READING_DELAY,
        source="class",
        td_ps_per_mm={"F.Cu": 5.77, "B.Cu": 5.69},
        barrel_ps_per_mm=7.11,
        barrel_mm=1.6,
        search_cap_mm=70.3,
    )

    def test_delay_limit_routes_and_reports_delay_to_the_pad_centre(self):
        from test_pair_src13 import ChainRunAndStubTest as C

        result = self.plan(self.LIMIT)
        self.assertEqual(result["status"], "routed", result.get("stub_skips"))
        for net in ("p", "n"):
            v = result["stub_metrics"]["D.1/D.2"][net]
            parts = v["stub_delay_path"]
            expected = (
                sum(
                    mm * self.LIMIT["td_ps_per_mm"][layer]
                    for layer, mm in parts["planar_mm"].items()
                )
                + parts["barrels"] * 1.6 * 7.11
            )
            self.assertAlmostEqual(v["stub_delay_ps"], expected, places=2)
            # pad-centre reading: the planar copper equals the centre length (+ barrel hops)
            self.assertAlmostEqual(
                sum(parts["planar_mm"].values()) + parts["barrels"] * 1.6,
                v["stub_centre_mm"],
                places=4,
            )
            self.assertAlmostEqual(v["stub_centre_mm"], C.STUB_CENTRE, places=5)
            self.assertEqual(v["stub_delay_limit_ps"], 400.0)

    def test_delay_above_the_limit_is_pair_stub_limit(self):
        # 6.3 ps: the search estimate of the via-start leg (1.024 mm to the pad entry x 5.77 =
        # 5.91 ps) passes, the final check reads 1.166 mm to the pad centre = 6.73 ps and rejects
        result = self.plan(dict(self.LIMIT, max_ps=6.3))
        self.assertEqual(result["status"], "pair_stub_limit")
        self.assertGreater(result["stub_metrics"]["D.1/D.2"]["p"]["stub_delay_ps"], 6.3)
        self.assertEqual(
            result["stub_skips"], []
        )  # the via-start leg's estimate passed; it routed first

    def test_stub_legs_over_the_delay_are_pruned_in_the_search(self):
        """Review fix: under a delay limit the via-start / via-reuse legs are pruned by their
        estimated stub delay (planar fanout at the leg layer's delay + barrel), as the mm cap
        prunes them; the estimate reads to the pad entry, so it never exceeds the final check."""
        result = self.plan(dict(self.LIMIT, max_ps=5.0))
        self.assertEqual(result["status"], "pair_no_matched_layer_bridge")
        skips = result["stub_skips"]
        self.assertEqual([s["start"] for s in skips], ["bridge_vias", "via_reuse"])
        self.assertAlmostEqual(skips[0]["stub_ps"], skips[0]["stub_mm"] * 5.77, places=2)
        self.assertAlmostEqual(skips[1]["stub_ps"] - skips[0]["stub_ps"], 1.6 * 7.11, places=2)
        self.assertTrue(all(s["stub_ps"] > 5.0 for s in skips))

    def test_explicit_mm_override_wins(self):
        result = self.plan(self.LIMIT, {"PNR_PAIR_STUB_MAX_MM": "1.0"})
        # src13 behaviour at 1.0 mm (edge reading): the stub legs are skipped
        self.assertEqual(result["status"], "pair_no_matched_layer_bridge")
        self.assertEqual([s["start"] for s in result["stub_skips"]], ["bridge_vias", "via_reuse"])

    def test_annotation_mm_limit_behaves_like_the_env_cap(self):
        result = self.plan(
            dict(kind="mm", max_mm=1.0, reading=B.STUB_READING_MM, source="explicit")
        )
        self.assertEqual(result["status"], "pair_no_matched_layer_bridge")
        self.assertEqual([s["start"] for s in result["stub_skips"]], ["bridge_vias", "via_reuse"])

    def test_flag_off_ignores_the_limit(self):
        import test_pair_post_bridge as tpb
        from test_pair_post_bridge import CROSSING
        from test_pair_src13 import ChainRunAndStubTest

        real = tpb.chain_board

        def classed():
            b, pair, r = real()
            pair["stub_limit"] = dict(self.LIMIT, max_ps=5.0)
            return b, pair, r

        t = ChainRunAndStubTest("test_flags_off_report_nothing_new")
        with patch.object(tpb, "chain_board", classed), env(PNR_BUS_CLASSES=None):
            result, *_ = t.plan({"PNR_PAIR_POST_BRIDGE_SURFACE": "1"}, CROSSING)
        self.assertEqual(result["status"], "routed")
        self.assertNotIn("stub_metrics", result)


@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class StubLayerTest(unittest.TestCase):
    """Review fix: the stub reading uses the terminal pad's own copper layer (a bottom-side ESD
    part is measured on B.Cu, the lead to the centre charged at the B.Cu delay)."""

    TD = {"F.Cu": 5.77, "B.Cu": 5.69}
    DELAY = dict(
        max_ps=400.0,
        td_ps_per_mm=TD,
        barrel_ps_per_mm=7.11,
        barrel_mm=1.6,
        reading=B.STUB_READING_DELAY,
    )

    def chain(self, d_layer):
        from test_native_electrical import board, pad

        b = board()
        by = {}
        for ref, x in (("J", 3.0), ("U", 15.0)):
            for num, net, y in (("1", "p", 5.2), ("2", "n", 4.8)):
                by[ref + "." + num] = pad(b, ref, num, net, (x, y), (0.2, 0.2))
        # D sits off the trunk: F.Cu spur to a via, B.Cu to the pad (on B.Cu, or on F.Cu over the via end)
        tracks, vias = [], []
        for num, net, y, via_y, pad_y in (("1", "p", 5.2, 5.6, 6.6), ("2", "n", 4.8, 4.4, 3.4)):
            dp = pad(b, "D", num, net, (8.0, pad_y), (0.2, 0.2))
            ls = k.LSET()
            ls.AddLayer(d_layer)
            dp.SetLayerSet(ls)
            by["D." + num] = dp
            tracks += [
                (net, k.F_Cu, (3.0, y), (15.0, y), 0.2),
                (net, k.F_Cu, (8.0, y), (8.0, via_y), 0.2),
            ]
            vias.append((net, (8.0, via_y)))
            tracks.append(
                (net, k.B_Cu if d_layer == k.B_Cu else k.F_Cu, (8.0, via_y), (8.0, pad_y), 0.2)
            )
        pair = dict(
            name="usb",
            p="p",
            n="n",
            terminal_chain=[
                {"p": "J.1", "n": "J.2"},
                {"p": "D.1", "n": "D.2"},
                {"p": "U.1", "n": "U.2"},
            ],
        )
        return pair, by, tracks, vias

    def test_bottom_side_terminal_is_measured_on_its_layer(self):
        from pnr.native_electrical import pair_stub_metrics

        pair, by, tracks, vias = self.chain(k.B_Cu)
        m = pair_stub_metrics(pair, by, tracks, vias, 1.6, barrel=1.6, delay=self.DELAY)["D.1/D.2"]
        for net in ("p", "n"):
            v = m[net]
            self.assertEqual(v["pad_layers"], ["B.Cu"])
            self.assertAlmostEqual(
                v["stub_centre_mm"], 0.4 + 1.6 + 1.0, places=5
            )  # spur + barrel + B.Cu to the centre
            self.assertAlmostEqual(
                v["stub_mm"], 0.4 + 1.6 + 0.9, places=5
            )  # to where it enters the 0.2 mm pad
            self.assertEqual(
                v["stub_delay_path"], {"planar_mm": {"B.Cu": 1.0, "F.Cu": 0.4}, "barrels": 1}
            )
            self.assertAlmostEqual(
                v["stub_delay_ps"], 0.4 * 5.77 + 1.6 * 7.11 + 1.0 * 5.69, places=2
            )

    def test_top_side_terminal_reading_is_unchanged(self):
        # an F.Cu pad keeps the src13/src15 reading (no pad_layers key, F.Cu lead)
        from pnr.native_electrical import pair_stub_metrics

        pair, by, tracks, vias = self.chain(k.F_Cu)
        m = pair_stub_metrics(pair, by, tracks, vias, 1.6, barrel=1.6, delay=self.DELAY)["D.1/D.2"]
        for net in ("p", "n"):
            self.assertNotIn("pad_layers", m[net])
            self.assertAlmostEqual(
                m[net]["stub_centre_mm"], 0.4 + 1.0, places=5
            )  # F.Cu spur + F.Cu to the pad (via unused)
            self.assertEqual(m[net]["stub_delay_path"]["barrels"], 0)


if __name__ == "__main__":
    unittest.main()
