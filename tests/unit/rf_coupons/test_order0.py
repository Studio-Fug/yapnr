"""Board O, the OSH Park 4-layer Order 0 uploads (Order 0 design §3-§6): stackups and priors,
line families, the catalogue, the frameless panel, the KiCad text, the tag's QR and the expected
S-parameters."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import tempfile
import unittest

import numpy as np

from yapnr.rf.coupons import (
    catalog,
    expected,
    fab,
    families,
    launch,
    layout,
    layout_o,
    models,
    qr,
    stackups,
    touchstone,
)

FR = "OSHPARK-4L-FR408HR"
EM = "OSHPARK-4L-EM528"
URL = "https://github.com/Studio-Fug/yapnr/tree/main/docs/rf/order0"


def _have_tables() -> bool:
    try:
        t = families.load(FR)
    except FileNotFoundError:
        return False
    return all(f in t for f in families.BOARD_FAMILIES[FR])


class StackupTest(unittest.TestCase):
    def test_osh_priors(self):
        """OSH Park's published values (design §3.1, §9 priors)."""
        fr, em = stackups.get(FR), stackups.get(EM)
        self.assertEqual((fr.board, em.board), ("O", "O"))
        p = fr.prior()
        self.assertEqual((p["pp.dk"].nominal, p["pp.dk"].sigma), (3.61, 0.10))
        self.assertEqual(p["core.dk"].nominal, 3.87)
        self.assertAlmostEqual(p["pp1.h"].sigma * 2 / 0.0254, 0.797, places=2)  # ±0.797 mil
        self.assertAlmostEqual(p["mask.scale"].sigma, 1 / 3, places=2)  # 0.6 ± 0.2 mil
        self.assertEqual(em.prior()["pp.dk"].nominal, 3.76)
        self.assertEqual(em.prior()["core.dk"].nominal, 4.11)
        # the JLC priors are untouched
        self.assertEqual(stackups.param("pp.dk").nominal, 4.4)

    def test_em528_inside_the_fr408hr_tables(self):
        """EM528 reads FR408HR's tables: its nominal point lies inside their ranges."""
        self.assertTrue(families.table_path(EM).endswith(f"{FR}.json"))
        fr = stackups.params_of(stackups.get(FR))
        for n, p in stackups.get(EM).prior().items():
            if fr[n].table:
                lo, hi = fr[n].table
                self.assertTrue(lo < p.nominal < hi, n)

    def test_board_b_is_the_product_stackup(self):
        st = stackups.get("JLC06161H-2116C")
        self.assertEqual(st.board, "B")
        self.assertEqual(st.prior()["pp1.h"].nominal, 0.2464)
        b = catalog.board("JLC06161H-2116C")
        self.assertEqual(b.launch.pad_gap, 0.435)  # 2D, L2 and L3 cut, L4 reference
        self.assertIn("B24", [s.id for s in b.sticks])  # the P-MO tie variant
        self.assertNotIn("B24", [s.id for s in catalog.board("JLC06161H-7628").sticks])


class FamilyTest(unittest.TestCase):
    def test_board_o_families(self):
        f = families.of(FR)
        self.assertEqual(f["M"].w, 0.40)  # 4 pixels of the 0.10 mm grid
        self.assertEqual(f["W"].w, 3.0)  # 6 pixels of the 0.50 mm grid
        self.assertEqual((f["M0.7"].w, f["M1.4"].w), (0.28, 0.56))
        self.assertTrue(f["M-MK"].mask and not f["M"].mask)
        self.assertEqual(f["W"].params, families.DEEP)
        self.assertEqual(set(f["W"].regions), {"pp", "core"})
        self.assertEqual(families.get("M", "JLC04161H-7628").w, 0.348)  # board A's M
        self.assertEqual(families.get("S", "JLC06161H-2116C").w, 0.279)

    def test_w_dispersion_substrate(self):
        v = stackups.with_values(stackups.get(FR), {})
        h, er = families.get("W", FR).dispersion_substrate(v)
        self.assertAlmostEqual(h, 2 * 0.1999 + 0.9906, places=4)
        self.assertTrue(3.61 < er < 3.87)

    @unittest.skipUnless(_have_tables(), "board O tables not built")
    def test_tables_at_nominal(self):
        """The shipped surrogates: M near 50 Ω, the masked line 1-2 Ω lower (design §4.1)."""
        m = models.Model(FR, stackups.with_values(stackups.get(FR), {}), np.array([5e9]))
        z = {f: float(m.line(f).zc[0].real) for f in ("M", "M0.7", "M1.4", "M-MK", "W")}
        self.assertAlmostEqual(z["M"], 50.3, delta=0.7)
        self.assertTrue(59.0 < z["M0.7"] < 63.0 and 39.5 < z["M1.4"] < 43.0)
        self.assertTrue(0.8 < z["M"] - z["M-MK"] < 2.5)
        self.assertAlmostEqual(z["W"], 48.6, delta=0.8)
        eps = float(m.line("M").eps_eff[0])
        self.assertAlmostEqual(eps, 2.685, delta=0.03)


class OptimizedWinnerTest(unittest.TestCase):
    """`catalog.o_optimized`: review findings 1, 4 and 5 (the fab DRC, the hash label computed
    from the winner's own result.json, and a loud failure instead of a silent placeholder)."""

    def test_missing_winner_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            catalog.o_optimized("D1", "/no/such/footprint.kicad_mod")

    def test_none_is_the_explicit_no_winner_case(self):
        win = catalog.o_optimized("D1", None)
        self.assertNotIn("islands", win)
        self.assertEqual(win, catalog.O_WINDOWS["D1"])

    def test_d1_hash_matches_its_result_json_and_fails_fab_drc(self):
        import hashlib
        import os

        path = os.path.join(catalog._repo_root(), catalog.D1_WINNER)
        if not os.path.isfile(path):
            self.skipTest("D1's winner footprint is not checked out")
        win = catalog.o_optimized("D1", path)
        result = os.path.join(os.path.dirname(path), "result.json")
        with open(result, "rb") as fh:
            want = hashlib.sha256(fh.read()).hexdigest()[:8]
        self.assertEqual(win["hash8"], want)
        # review finding 1: two 0.100 mm island-to-body gaps the raster-only check missed.
        self.assertFalse(win["drc_ok"])
        self.assertEqual({v["reason"] for v in win["drc_violations"]}, {"corner"})

    def test_d2_hash_matches_its_result_json_and_passes_fab_drc(self):
        import hashlib
        import os

        path = os.path.join(catalog._repo_root(), catalog.D2_WINNER)
        if not os.path.isfile(path):
            self.skipTest("D2's winner footprint is not checked out")
        win = catalog.o_optimized("D2", path)
        result = os.path.join(os.path.dirname(path), "result.json")
        with open(result, "rb") as fh:
            want = hashlib.sha256(fh.read()).hexdigest()[:8]
        self.assertEqual(win["hash8"], want)
        self.assertTrue(win["drc_ok"])
        self.assertEqual(win["drc_violations"], [])


class CatalogTest(unittest.TestCase):
    def test_uploads(self):
        m = catalog.board(FR, "M")
        ids = [s.id for s in m.sticks]
        for sid in (
            "A01",
            "A05",
            "A06",
            "A07",
            "A10",
            "A11",
            "A12",
            "A14",
            "A15",
            "A16",
            "A04R",
            "R1",
        ):
            self.assertIn(sid, ids)
        self.assertNotIn("D1", ids)  # D1 moved to O0-D (D-O0-10)
        for s in m.sticks:
            if s.kind in ("thru", "line", "verify", "reflect", "variant", "switch"):
                self.assertEqual(s.height, 12.0, s.id)  # review: 12 mm M sticks
        self.assertEqual(m.stick("A04R").geometry["rot"], 90)
        self.assertEqual(sorted(m.trl["M"]["dl"]), [0.0, 4.5, 13.0, 30.0, 70.0])
        w = catalog.board(FR, "W")
        self.assertEqual(
            [s.id for s in w.sticks if s.trl == "W" and s.kind != "demo"][:5],
            ["B01", "B02", "B03", "B04", "B05"],
        )
        self.assertTrue(all(s.height == 16.0 for s in w.sticks if s.kind in ("thru", "line")))
        # D2 (d2-star-sched, a near-miss) and D1 (d1-star) both carry real exported copper now
        # (catalog.o_optimized's winner paths), so neither window is a placeholder any more.
        self.assertEqual(w.stick("D2").kind, "demo")
        d = catalog.board(FR, "D")
        self.assertEqual(sorted(s.id for s in d.sticks), ["A01", "A04", "A20", "D1", "R1"])
        self.assertEqual(d.trl["M"]["dl"], [0.0, 9.0, 30.0])  # review M2: O0-D's own lines
        for u, sid in (("W", "D2"), ("D", "D1")):
            self.assertNotIn("placeholder", catalog.board(FR, u).stick(sid).geometry)
        self.assertNotIn("placeholder", m.stick("R1").geometry)
        # D1 ships with a known fab-DRC miss (review finding 1/2: re-optimization pending);
        # D2's near-miss design has no such width/space violation.
        self.assertFalse(d.stick("D1").geometry["window"]["drc_ok"])
        self.assertTrue(w.stick("D2").geometry["window"]["drc_ok"])
        self.assertEqual(len(d.stick("D1").geometry["window"]["hash8"]), 8)
        self.assertEqual(len(w.stick("D2").geometry["window"]["hash8"]), 8)

    def test_conditioning(self):
        """Design §4.2/§4.3: the M set >= 0.926 over 0.5-6 GHz for εeff 2.60-2.95, the W set
        >= 0.929 over 1-6 GHz for 2.85-3.25."""
        for eps in (2.60, 2.75, 2.95):
            c = catalog.conditioning(catalog.O_TRL_M, eps, np.linspace(0.5e9, 6e9, 2001))
            self.assertGreater(c.min(), 0.925, eps)
        for eps in (2.85, 3.05, 3.25):
            c = catalog.conditioning(catalog.O_TRL_W, eps, np.linspace(1e9, 6e9, 2001))
            self.assertGreater(c.min(), 0.928, eps)
        # O0-D (review M2): >= 0.95 over D1's band, >= 0.80 over 1-6 GHz
        for eps in (2.60, 2.70, 2.80, 2.95):
            c = catalog.conditioning(catalog.O_TRL_D, eps, np.linspace(4.25e9, 5.75e9, 601))
            self.assertGreater(c.min(), 0.95, eps)
            c = catalog.conditioning(catalog.O_TRL_D, eps, np.linspace(1e9, 6e9, 2001))
            self.assertGreater(c.min(), 0.80, eps)

    def test_references(self):
        """R1 / R1t: the λ/4 arm counted from Hammerstad's branch reference plane (review M1),
        every edge on the refine-2 grid the FDTD runs them at (review H2: 0.70 mm is 14 cells of
        0.05 mm about a node, not 7 of 0.10 mm)."""
        for reg, (w_arm, d2) in (("M", (0.70, 0.37)), ("W", (5.0, 2.18))):
            r = catalog.o_reference(reg)
            (ax0, ax1, ay0, ay1), (ox0, ox1, oy0, _) = r["copper"]
            self.assertAlmostEqual(ay1 - ay0, w_arm)
            arm = r["arm"]
            self.assertAlmostEqual(arm["junction_d_branch_mm"], d2, delta=0.01)
            self.assertAlmostEqual((ox0 + ox1) / 2, arm["l_to_centre"])
            # λ/4 from the branch plane within half a grid step; f0 within 1 % of 5 GHz
            pitch = r["fdtd_pitch_mm"]
            self.assertLess(abs(arm["l_to_centre"] - d2 - arm["quarter_wave_mm"]), pitch / 2 + 0.01)
            self.assertAlmostEqual(arm["f0_ghz_est"], 5.0, delta=0.05)
            for v in (ax1, ay0, ay1, ox0, ox1, oy0, r["w"], r["h"]):
                self.assertAlmostEqual(v / pitch, round(v / pitch), places=6)

    def test_tee_offsets(self):
        """Hammerstad's T-junction: equal 50 Ω arms on M put the branch plane 0.31 D off the
        main line's centre (0.28 mm), the main planes 0.055 D (0.05 mm)."""
        d_main, d_branch = models.tee_offsets(50.6, 2.70, 50.6, 2.70, 0.1999, 5.8e9)
        D = models.ETA0 * 0.1999 / (50.6 * math.sqrt(2.70))
        self.assertAlmostEqual(d_branch / D, 0.309, delta=0.002)
        self.assertAlmostEqual(d_main / D, 0.055, delta=0.001)

    def test_demo_geometry(self):
        """Each demo port: the standard launch half (10 mm to its RP) and L_f to the window."""
        for upload, sid in (("M", "R1"), ("W", "R1t"), ("W", "D2"), ("D", "D1")):
            s = catalog.board(FR, upload).stick(sid)
            g = s.geometry
            win = g["window"]
            self.assertAlmostEqual(win["x0"], catalog.LAUNCH_MM + win["feed"])
            self.assertAlmostEqual(s.height, win["h"] + 2 * (catalog.LAUNCH_MM + win["feed"]))
            self.assertEqual([p["side"] for p in g["ports"]], ["W", "N", "S"])

    def test_tuned_resonators(self):
        b = fab.tuned_board(FR, "M") if _have_tables() else None
        if b is None:
            self.skipTest("board O tables not built")
        r = b.stick("A11").geometry["radius"]
        self.assertAlmostEqual(r, 15.1, delta=0.3)  # design: n = 3 at 5.79 GHz
        # review H1: the notch at 5.5 GHz from the branch plane, 0.28 mm off the line's centre
        g = b.stick("A12").geometry
        self.assertAlmostEqual(g["junction"]["d_branch"], 0.28, delta=0.01)
        self.assertAlmostEqual(g["stub_mm"], 8.29, delta=0.1)
        m = models.Model(FR, stackups.with_values(stackups.get(FR), {}), expected.GRID_O_CPAD)
        s12 = b.stick("A12")
        ref = m.line("M").zc[0]
        (f_notch,) = expected.notches(m, s12.elements, ref, 4.5e9, 6.0e9)
        self.assertAlmostEqual(f_notch / 1e9, 5.5, delta=0.01)
        f_ring = expected.notches(m, b.stick("A11").elements, ref, 0.5e9, 6.0e9)
        self.assertEqual(len(f_ring), 2)  # n = 1 and 3; n = 2 passes
        self.assertAlmostEqual(f_ring[1] / 1e9, 5.8, delta=0.02)


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        board = fab.tuned_board if _have_tables() else catalog.board  # the shipped geometry
        cls.panels = {u: layout_o.pack(board(FR, u).sticks) for u in "MWD"}

    def test_size_and_cost(self):
        """Near the design's estimates (§16.7) at $10 per square inch: O0-M 24.2 sq in grows by
        tabs off the launch edges (review F5) and two per stick (F4); O0-D by its 9 mm line."""
        limits = {"M": 27.5, "W": 12.6, "D": 7.5}
        for u, p in self.panels.items():
            self.assertLess(p.sq_in, limits[u], u)

    def test_one_outline_and_slots(self):
        for u, p in self.panels.items():
            loops = layout_o.outline_loops(p)
            boxes = [
                (
                    min(x for x, _ in lp),
                    min(y for _, y in lp),
                    max(x for x, _ in lp),
                    max(y for _, y in lp),
                )
                for lp in loops
            ]
            outer = [
                b
                for b in boxes
                if not any(
                    c != b and c[0] <= b[0] and c[1] <= b[1] and b[2] <= c[2] and b[3] <= c[3]
                    for c in boxes
                )
            ]
            self.assertEqual(len(outer), 1, u)  # frameless: one outline, the rest cut-outs
            its = p.items
            for i, a in enumerate(its):
                for b in its[i + 1 :]:
                    gap = max(
                        b.x - (a.x + a.w), a.x - (b.x + b.w), b.y - (a.y + a.h), a.y - (b.y + b.h)
                    )
                    self.assertGreaterEqual(gap, layout_o.SLOT - 1e-6, (u, a.stick.id, b.stick.id))

    def test_tabs_on_port_free_edges(self):
        """Every tab bridges two edges that carry no launch (2-port sticks: the long sides; the
        demos: their east edge; review F5), TAB_CLEAR from a launch end; every stick has at
        least two tabs (F4)."""
        for u, p in self.panels.items():
            self.assertGreaterEqual(min(p.tabs_per_item()), layout_o.TABS_MIN, u)
            for (x0, x1, y0, y1), (orient, a, b) in zip(p.tabs, p.tab_meta):
                c = 0.5 * (x0 + x1) if orient == "h" else 0.5 * (y0 + y1)
                for k in (a, b):
                    ok = False
                    for o, cc, ivs in layout_o._panel_edges(p.items[k]):
                        on = o == orient and (
                            abs(cc - y0) < 1e-6 or abs(cc - y1) < 1e-6
                            if o == "h"
                            else abs(cc - x0) < 1e-6 or abs(cc - x1) < 1e-6
                        )
                        if on and any(
                            lo - 1e-6 <= c - layout_o.TAB_W / 2
                            and c + layout_o.TAB_W / 2 <= hi + 1e-6
                            for lo, hi in ivs
                        ):
                            ok = True
                    self.assertTrue(ok, (u, p.items[k].stick.id, orient, c))

    def test_bites_on_the_tabbed_edges(self):
        """Review F1: three bites on each of a tab's two stick edges, centred on the edge line,
        spread along it inside the tab, the outer ones 0.04 mm clear of its milled sides."""
        r = layout_o.BITE_D / 2
        for u, p in self.panels.items():
            self.assertEqual(len(p.bites), 6 * len(p.tabs), u)
            for t, ((x0, x1, y0, y1), (orient, _, _)) in enumerate(zip(p.tabs, p.tab_meta)):
                bites = p.bites[6 * t : 6 * t + 6]
                for bx, by in bites:
                    if orient == "h":  # edges y0, y1; the tab spans x0..x1
                        self.assertTrue(min(abs(by - y0), abs(by - y1)) < 1e-9, (u, t))
                        self.assertGreaterEqual(min(bx - r - x0, x1 - bx - r), 0.04 - 1e-9)
                    else:
                        self.assertTrue(min(abs(bx - x0), abs(bx - x1)) < 1e-9, (u, t))
                        self.assertGreaterEqual(min(by - r - y0, y1 - by - r), 0.04 - 1e-9)
        pitch_in = layout_o.BITE_PITCH / 25.4
        self.assertTrue(0.035 <= pitch_in <= 0.045)  # OSH Park's hole spacing [O-panel]

    def test_board_text(self):
        b = catalog.board(FR, "M")
        text, panel = layout.board_text(FR, b, "A", dict(title="t", git="g"))
        depth = 0
        for ch in text:
            depth += (ch == "(") - (ch == ")")
            self.assertGreaterEqual(depth, 0)
        self.assertEqual(depth, 0)
        self.assertNotRegex(text, r"[ \t]+\n")
        nets = re.findall(r'\(net \d+ "([^"]*)"\)', text)
        self.assertEqual(len(nets), len(set(nets)))
        self.assertIn('(color "Purple") (thickness 0.0152)', text)
        self.assertIn("(epsilon_r 3.61)", text)
        self.assertIn('"Reference_R1"', text)
        self.assertIn('"R_0402_1005Metric"', text)
        self.assertEqual(
            text.count('(footprint "SMA_EdgeLaunch_Cinch_142-0701-851"'), 33
        )  # design §16.7: 33 SMAs per O0-M copy
        # D1 and D2 both carry real exported copper now (no upload has a placeholder window).
        self.assertNotIn(
            '(footprint "Placeholder', layout.board_text(FR, catalog.board(FR, "D"))[0]
        )
        dru = layout.dru_text(panel.mask_rules, bites=True)
        self.assertIn("memberOfFootprint('MB1')", dru)
        self.assertIn("physical_hole_clearance (min 0.005mm)", dru)
        self.assertNotIn('severity ignore))\n(rule "MO', dru.split("MB1")[1][:200])

    def test_upload_ids_and_placeholders(self):
        """Review F2: every stick's back silkscreen names its upload; F3: an empty window is a
        placeholder footprint that `yapnr fab check` stops on."""
        for u in "MWD":
            b = catalog.board(FR, u)
            text, panel = layout.board_text(FR, b, "A", dict(title="t", git="g"))
            back = re.findall(r'\(gr_text "([^"]*)" \(at [^)]*\) \(layer "B\.SilkS"\)', text)
            # finding 5: an optimizer winner's hash label (8 hex of its result.json sha256)
            hashes = {t for t in back if re.fullmatch(r"[0-9a-f]{8}", t)}
            back = [t for t in back if t not in hashes]
            want = {
                s.geometry["window"]["hash8"]
                for s in b.sticks
                if s.kind == "demo" and "hash8" in s.geometry.get("window", {})
            }
            self.assertEqual(hashes, want, u)
            self.assertEqual(len(want), 0 if u == "M" else 1, u)
            ids = {s.id for s in b.sticks}
            self.assertEqual({t.split(" ", 1)[1] for t in back}, ids, u)
            self.assertTrue(all(t.startswith(f"O0-{u} ") for t in back), u)
            ph = re.findall(r'\(property "yapnr_placeholder" "([^"]*)"', text)
            # D1 and D2 both carry real exported copper now: no placeholder anywhere.
            self.assertEqual(len(ph), 0, u)

    def test_no_stitching_under_silk(self):
        """Review F6: no plane-stitching via within 0.1 mm of a silkscreen item."""
        st = stackups.get(FR)
        for u in "MW":
            b = catalog.board(FR, u)
            wr = layout.Writer(st, b)
            layout_o.build(wr, layout_o.pack(b.sticks), dict(title="t", git="g"))
            r = wr.via_size[0] / 2 + 0.1
            for p, key in wr.via_log:
                if key[1] != "st":
                    continue
                for _, (x0, x1, y0, y1) in wr.silk_boxes:
                    inside = x0 - r <= p[0] <= x1 + r and y0 - r <= p[1] <= y1 + r
                    self.assertFalse(inside, (u, key))


class QrTest(unittest.TestCase):
    def test_reed_solomon_and_format(self):
        # the "HELLO WORLD" 1-M example (thonky.com QR tutorial)
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr.rs_ecc(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])
        self.assertEqual(format(qr._format_bits(0), "015b"), "101010000010010")

    def test_tag_url(self):
        """Version 4-M (33 x 33) as the design says; the module grid matches python-qrcode's
        (checked once, 2026-10-02, for all eight masks)."""
        m = qr.matrix(URL)
        self.assertEqual(len(m), 33)
        bits = "".join("".join("1" if v else "0" for v in r) for r in m)
        self.assertEqual(
            hashlib.sha256(bits.encode()).hexdigest(),
            "0b4b4c4aa0faef56e50533aabbd3b4ca25adf943e0ad3cdb200998b3cb068064",
        )
        self.assertEqual(qr.capacity(4), 62)
        with self.assertRaises(ValueError):
            qr.matrix("x" * 200)


@unittest.skipUnless(_have_tables(), "board O tables not built")
class ExpectedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_both_substrates(self):
        paths = expected.write_o("M", self.dir)
        for sub in (FR, EM):
            self.assertIn(f"{sub}:A04", paths)
            self.assertIn(f"{sub}:A16:2", paths)
        f, s_fr, _ = touchstone.read(paths[f"{FR}:A04"])
        _, s_em, _ = touchstone.read(paths[f"{EM}:A04"])
        k = int(np.argmin(abs(f - 5e9)))
        loss = -20 * math.log10(abs(s_fr[k, 1, 0]))
        self.assertTrue(0.3 < loss < 0.7, loss)  # 3 cm at 0.13-0.16 dB/cm (design §4.1)
        # EM528's higher εeff: more phase over the 30 mm line
        ph_fr = np.unwrap(np.angle(s_fr[:, 1, 0]))[k]
        ph_em = np.unwrap(np.angle(s_em[:, 1, 0]))[k]
        self.assertLess(ph_em, ph_fr)
        # the C-pad is a capacitor at 30-300 MHz
        f1, s1, _ = touchstone.read(paths[f"{FR}:A16:1"])
        z = 50 * (1 + s1[:, 0, 0]) / (1 - s1[:, 0, 0])
        k1 = int(np.argmin(abs(f1 - 100e6)))
        c = -1 / (2 * math.pi * f1[k1] * z[k1].imag)
        c_pp = 8.854e-12 * 3.6 * 36e-6 / 0.1999e-3
        self.assertTrue(1.0 < c / c_pp < 1.4, c / c_pp)  # parallel plate plus fringing
        # review L1: the held-out scalars to 10 kHz and the lines on the 6 MHz grid
        import json

        with open(paths[f"{FR}:scalars"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertAlmostEqual(doc["notches_ghz"]["A12"][0], 5.5, delta=0.01)
        self.assertEqual(len(doc["notches_ghz"]["A11"]), 2)
        with open(paths[f"{FR}:lines"], encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        self.assertEqual(len(rows), 1 + 1000)
        self.assertTrue(rows[0].startswith("f_ghz,M.eps_eff,M.alpha_db_per_cm"))


class LaunchFrameTest(unittest.TestCase):
    def test_frames_map_launch_axes(self):
        """A launch on each edge: x into the stick, the footprint angle turns its x axis."""
        s = catalog.board(FR, "M").stick("R1")
        pl = layout.Placed(s, 0.0, 0.0)
        for side, at, into in (
            ("W", 0.0, (1, 0)),
            ("E", 0.0, (-1, 0)),
            ("N", 20.0, (0, 1)),
            ("S", 20.0, (0, -1)),
        ):
            fr = layout.Frame(pl, side, at)
            p0, p1 = fr.p(0.0, 0.0), fr.p(1.0, 0.0)
            self.assertAlmostEqual(p1[0] - p0[0], into[0])
            self.assertAlmostEqual(p1[1] - p0[1], into[1])
            a = math.radians(fr.angle)  # KiCad: local (1, 0) -> (cos a, -sin a) on screen
            self.assertAlmostEqual(math.cos(a), into[0])
            self.assertAlmostEqual(-math.sin(a), into[1])
        rot = layout.Placed(s, 10.0, 5.0, rot=90)
        self.assertEqual(rot.p(1.0, 0.0), (10.0, 6.0))
        self.assertEqual(rot.angle, -90.0)
        self.assertLess(launch.CINCH_142_0701_851.lay_e, layout_o.TAB_CLEAR)


if __name__ == "__main__":
    unittest.main()
