"""SI physics: stackup, trace L'/C', via and pad models against hand calculations."""
import math
import unittest

from pnr.si import physics as P


class StackupTest(unittest.TestCase):
    def setUp(self):
        self.st = P.stackup({})

    def test_jlc_stack_from_profile(self):
        self.assertEqual(P.copper_layers(self.st), ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])
        self.assertEqual(self.st['planes'], ['In1.Cu'])
        t = {x['name']: x['t_mm'] for x in self.st['layers']}
        # copper from pnr.fab_profile jlc-pofv: 35 um outer, 15.2 um inner
        self.assertAlmostEqual(t['F.Cu'], 0.035)
        self.assertAlmostEqual(t['In1.Cu'], 0.0152)
        self.assertAlmostEqual(P.thickness(self.st), 0.035 * 2 + 0.0152 * 2 + 0.2104 * 2 + 1.065, places=9)
        self.assertEqual(self.st['via'], dict(diameter_mm=0.45, drill_mm=0.30))

    def test_planes_follow_rules_and_override(self):
        st = P.stackup(dict(net_classes=[dict(name='gnd', plane_layer='In2.Cu', nets=['x'])]))
        self.assertEqual(st['planes'], ['In2.Cu'])
        custom = dict(layers=[dict(name='F.Cu', kind='copper', t_mm=0.035), dict(name='d', kind='dielectric', t_mm=1.5, er=4.0),
                              dict(name='B.Cu', kind='copper', t_mm=0.035)], planes=['B.Cu'])
        st = P.stackup(dict(stackup=custom))
        self.assertEqual(P.copper_layers(st), ['F.Cu', 'B.Cu'])

    def test_microstrip_anchor_f_cu(self):
        # design sec. 4 anchors: 0.20 mm over 0.2104 mm 7628 (er 4.4)
        p = P.line(self.st, 'F.Cu', 0.2)
        self.assertEqual(p['model'], 'microstrip_hammerstad_jensen')
        self.assertAlmostEqual(p['z0_ohm'], 67.9, delta=0.2)
        self.assertAlmostEqual(p['eps_eff'], 2.99, delta=0.01)
        self.assertAlmostEqual(p['td_ps_per_mm'], 5.77, delta=0.01)
        self.assertAlmostEqual(p['l_nh_per_mm'], 0.392, delta=0.002)
        self.assertAlmostEqual(p['c_pf_per_mm'], 0.085, delta=0.001)
        self.assertAlmostEqual(P.line(self.st, 'F.Cu', 0.127)['z0_ohm'], 81.2, delta=0.2)
        # L' C' consistency: Z0 = sqrt(L/C), v = 1/sqrt(LC)
        self.assertAlmostEqual(math.sqrt(p['l_nh_per_mm'] * 1e-9 / (p['c_pf_per_mm'] * 1e-12)), p['z0_ohm'], delta=0.01)

    def test_inner_and_far_layers(self):
        in2 = P.line(self.st, 'In2.Cu', 0.2)
        self.assertEqual(in2['model'], 'embedded_microstrip_ipc2141a')
        self.assertEqual(in2['reference'], ['In1.Cu'])
        # hand calc: er' = 4.6 (1 - exp(-1.55 (1.065 + 0.0152 + 0.2104)/1.065)); Z0 = 60/sqrt(er') ln(5.98 h/(0.8 w + t))
        er = 4.6 * (1 - math.exp(-1.55 * (1.065 + 0.0152 + 0.2104) / 1.065))
        z0 = 60 / math.sqrt(er) * math.log(5.98 * 1.065 / (0.16 + 0.0152))
        self.assertAlmostEqual(in2['z0_ohm'], z0, places=6)
        b = P.line(self.st, 'B.Cu', 0.2)
        self.assertEqual(b['model'], 'microstrip_hammerstad_jensen')
        self.assertAlmostEqual(b['h_mm'], 1.065 + 0.0152 + 0.2104, places=6)
        self.assertGreater(b['z0_ohm'], 115)
        self.assertLess(b['z0_ohm'], 140)
        with self.assertRaises(ValueError):
            P.line(self.st, 'In3.Cu', 0.2)

    def test_stripline_when_planes_on_both_sides(self):
        st = P.stackup(dict(net_classes=[dict(plane_layer='In1.Cu'), dict(plane_layer='B.Cu')]))
        p = P.line(st, 'In2.Cu', 0.2)
        self.assertEqual(p['model'], 'stripline_asym_ipc2141a')
        self.assertEqual(sorted(p['reference']), ['B.Cu', 'In1.Cu'])
        self.assertLess(p['z0_ohm'], P.line(self.st, 'In2.Cu', 0.2)['z0_ohm'])


class ViaPadTest(unittest.TestCase):
    def setUp(self):
        self.st = P.stackup({})

    def test_hsdd_via_inductance_anchors(self):
        self.assertAlmostEqual(P.via_l_nh(0.245, 0.3), 0.107, delta=0.001)
        self.assertAlmostEqual(P.via_l_nh(1.6, 0.3), 1.30, delta=0.01)
        # hand calc: 5.08 h (ln(4h/d) + 1), inches
        h, d = 1.0 / 25.4, 0.2 / 25.4
        self.assertAlmostEqual(P.via_l_nh(1.0, 0.2), 5.08 * h * (math.log(4 * h / d) + 1), places=9)
        self.assertEqual(P.via_l_nh(0, 0.3), 0.0)

    def test_via_spans_and_capacitance(self):
        self.assertAlmostEqual(P.via_span_mm(self.st, 'F.Cu', 'In1.Cu'), 0.2454, places=4)
        self.assertAlmostEqual(P.via_span_mm(self.st, 'In1.Cu', 'F.Cu'), 0.2454, places=4)
        self.assertAlmostEqual(P.via_span_mm(self.st, 'F.Cu', 'B.Cu'), P.thickness(self.st), places=9)
        v = P.via(self.st, 'F.Cu', 'In2.Cu')
        self.assertAlmostEqual(v['l_nh'], P.via_l_nh(v['span_mm'], 0.3), places=9)
        # HSDD: 1.41 er T D1/(D2 - D1), T = 1.586 mm, D1 = 0.45, D2 = 0.85 mm
        er = v['er']
        self.assertAlmostEqual(v['c_pf'], 1.41 * er * (P.thickness(self.st) / 25.4) * 0.45 / 0.40, places=9)
        self.assertGreater(v['c_pf'], 0.3)
        self.assertLess(v['c_pf'], 0.7)
        with self.assertRaises(ValueError):
            P.via_c_pf(1.6, 0.6, 0.5, 4.4)

    def test_pad_capacitance_parallel_plate(self):
        c = P.pad_c_pf(self.st, 'F.Cu', 0.5 * 0.55)
        self.assertAlmostEqual(c, 8.8541878128e-12 * 4.4 * 0.275e-6 / 0.2104e-3 * 1e12, places=9)
        self.assertLess(P.pad_c_pf(self.st, 'B.Cu', 0.275), c)
        self.assertEqual(P.pad_c_pf(self.st, 'F.Cu', 0), 0.0)

    def test_cable_per_metre(self):
        lc = P.cable_lc(120, 0.68)
        self.assertAlmostEqual(lc['td_ns_per_m'], 1 / (0.68 * 0.299792458), places=6)
        self.assertAlmostEqual(lc['l_nh_per_m'], 588.6, delta=0.1)
        self.assertAlmostEqual(lc['c_pf_per_m'], 40.88, delta=0.01)

    def test_cable_skin_effect(self):
        import math
        loss = dict(conductor_d_mm=0.511, conductors=2, rho_ohm_m=1.72e-8, f_ref_mhz=300, spacing_mm=1.1)
        self.assertAlmostEqual(P.skin_depth_m(1.72e-8, 300e6) * 1e6, 3.81, delta=0.01)
        self.assertAlmostEqual(P.cable_r_ohm_per_m(loss), 6.35, delta=0.01)           # 2 x 2.81 x 1.13
        self.assertAlmostEqual(P.cable_r_ohm_per_m(dict(loss, f_ref_mhz=None)), 0.1677, delta=1e-4)   # 2 x 83.9 mOhm/m DC
        lad = P.skin_ladder(loss)
        self.assertLess(lad['fit'], 0.06)
        self.assertTrue(all(r > 0 and l > 0 for r, l in lad['stages']))

        def r(f):
            return lad['r_dc'] + sum(rk * (f / (rk / (2 * math.pi * lk))) ** 2 / (1 + (f / (rk / (2 * math.pi * lk))) ** 2)
                                     for rk, lk in lad['stages'])
        self.assertAlmostEqual(r(0.0), lad['r_dc'])                                   # DC: no skin effect
        for f in (1e7, 3.5e7, 1e8, 3e8, 9e8):                                         # sqrt(f) within 5 %
            self.assertAlmostEqual(r(f) / (6.3497 * math.sqrt(f / 3e8)), 1.0, delta=0.05)


if __name__ == '__main__':
    unittest.main()
