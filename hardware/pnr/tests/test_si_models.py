"""SI model library: manifests, part values, vendor fetch-by-sha256 cache, KIBIS test bench, PWL decimation."""
import hashlib
import io
import json
import os
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pnr.si import models as M


def zip_bytes(member, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(member, data)
    return buf.getvalue()


def physics_r(cable):
    from pnr.si import physics
    return physics.cable_r_ohm_per_m(cable['loss'])


IBIS_SNIPPET = """[Component]  PART_DBV
[Package]
|        typ   min   max
R_pkg    25m   19m   37m
[Pin]  signal_name  model_name  R_pin  L_pin  C_pin
|
1  GND   GND       37.0m  1.751nH  0.263pF
4  B     IO_B      1.900e-02 1.620e-09 2.640e-13
[Component]  PART_DCK
[Pin]  signal_name  model_name  R_pin  L_pin  C_pin
4  B     IO_B      NA NA NA
[Model]     IO_B_50
Model_type  I/O
C_comp      6.40pF      5.43pF      7.92pF
[Model]     IO_B_33
C_comp      5.0pF      NA      NA
"""


class IbisTest(unittest.TestCase):
    def test_numbers_and_tables(self):
        self.assertAlmostEqual(M.ibis_number('6.40pF'), 6.4e-12)
        self.assertAlmostEqual(M.ibis_number('37.0m'), 0.037)
        self.assertAlmostEqual(M.ibis_number('1.751nH'), 1.751e-9)
        self.assertAlmostEqual(M.ibis_number('2.640e-13'), 2.64e-13)
        self.assertIsNone(M.ibis_number('NA'))
        self.assertEqual(M.ibis_pin_parasitics(IBIS_SNIPPET, 'PART_DBV', 4), (0.019, 1.62e-09, 2.64e-13))
        self.assertIsNone(M.ibis_pin_parasitics(IBIS_SNIPPET, 'PART_DCK', 4))          # NA: [Package] applies
        self.assertIsNone(M.ibis_pin_parasitics(IBIS_SNIPPET, 'PART_DBV', 6))
        c = M.ibis_c_comp(IBIS_SNIPPET, 'IO_B_50')
        self.assertEqual({k: round(v * 1e12, 3) for k, v in c.items()}, {'TYP': 6.4, 'MIN': 5.43, 'MAX': 7.92})
        self.assertEqual(M.ibis_c_comp(IBIS_SNIPPET, 'IO_B_33')['MAX'], 5e-12)          # NA -> typ
        with self.assertRaises(M.ModelError):
            M.ibis_c_comp(IBIS_SNIPPET, 'nope')

    def test_apply_c_comp(self):
        text = '.SUBCKT X GND PIN\nCPIN PIN GND 2.640000e-13\nCCPOMP DIE0 GND 6.400000e-12\n.ENDS\n'
        out = M.apply_c_comp(text, 7.92e-12)
        self.assertIn('CCPOMP DIE0 GND 7.920000e-12\n', out)
        self.assertIn('CPIN PIN GND 2.640000e-13', out)
        with self.assertRaises(M.ModelError):
            M.apply_c_comp(text.replace('CCPOMP', 'CX'), 1e-12)

    def fake_cli(self, d):
        cli = Path(d) / 'kicad-cli'
        cli.write_text('#!/bin/sh\necho 10.0.6\n')
        cli.chmod(0o755)
        return str(cli)

    def test_pin_parasitic_corners_rejected_when_pin_row_overrides(self):
        drv = json.loads(json.dumps(M.Library().get('driver', 'sn74lvc1t45')))
        drv['ibis_component'] = 'PART_DBV'
        drv['pins']['4']['by_rail']['5.0'] = 'IO_B_50'
        drv['corners']['min']['rpin'] = 'MIN'
        with tempfile.TemporaryDirectory() as d:
            ibs = Path(d) / 'x.ibs'
            ibs.write_text(IBIS_SNIPPET)
            env = {'PNR_KICAD_CLI': self.fake_cli(d)}
            with self.assertRaises(M.ModelError) as ctx:
                M.kibis_driver(drv, 4, 'min', 40.0, 2.0, cache=d, env=env, fetch=lambda *a, **k: ibs)
            self.assertIn('[Pin]', str(ctx.exception))

    def test_publish_is_atomic_and_first_writer_wins(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'kibis' / 'k1'
            a = M._publish_kibis(out, 'A\n', dict(key='k1'))
            b = M._publish_kibis(out, 'B\n', dict(key='k1'))              # loser: returns the published model
            self.assertEqual(Path(a['path']).read_text(), 'A\n')
            self.assertEqual(b['sha256'], a['sha256'])
            self.assertEqual(sorted(p.name for p in (Path(d) / 'kibis').iterdir()), ['k1'])   # no staging left
            partial = Path(d) / 'kibis' / 'k2'
            partial.mkdir()
            (partial / 'driver.lib').write_text('half')                   # legacy partial write, no meta.json
            c = M._publish_kibis(partial, 'C\n', dict(key='k2'))
            self.assertEqual(Path(c['path']).read_text(), 'C\n')
            self.assertTrue(M._read_kibis(partial / 'driver.lib', partial / 'meta.json'))

    def test_download_has_a_total_deadline(self):
        class Slow:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                time.sleep(0.2)
                return b'x' * 10
        with mock.patch('urllib.request.urlopen', return_value=Slow()):
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                M._download('https://example.invalid/x.zip', 0.5)
            self.assertLess(time.monotonic() - t0, 2.0)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.lib = M.Library()

    def test_committed_models(self):
        self.assertEqual(self.lib.ids('profile'), ['ws2812b_din'])
        prof = self.lib.get('profile', 'ws2812b_din')
        self.assertEqual(prof['corners'], {'driver': ['typ', 'min', 'max'], 'cable_m': [0.0, 1.0, 3.0],
                                           'cable_z0_ohm': [120.0, 100.0, 150.0]})
        self.assertTrue(prof['limits']['rise_10_90_ns']['gate'])
        self.assertFalse(prof['limits']['overshoot_v']['gate'])          # report-only until calibrated
        rx = self.lib.get('receiver', prof['receiver'])
        self.assertEqual((rx['vih_frac_vdd'], rx['vil_frac_vdd'], rx['c_in_pf']), (0.7, 0.3, 15.0))
        self.assertTrue(rx['clamps']['assumed'])
        cable = self.lib.get('cable', prof['cable'])
        self.assertEqual(cable['per_metre']['z0_ohm'], 120.0)
        self.assertTrue(cable['citations'])
        drv = self.lib.get('driver', 'sn74lvc1t45')
        v = drv['vendor_files'][0]
        self.assertTrue(v['url'].endswith('/scem401c'))                   # versioned URL
        self.assertIn('no redistribution', v['license'].lower())
        self.assertEqual({c: drv['corners'][c]['vcc'] for c in drv['corners']}, {'typ': 'TYP', 'min': 'MIN', 'max': 'MAX'})
        # slow-weak gets the largest die capacitance, fast-strong the smallest; the [Pin] row fixes R/L/C
        self.assertEqual({c: drv['corners'][c]['c_comp'] for c in drv['corners']}, {'typ': 'TYP', 'min': 'MAX', 'max': 'MIN'})
        self.assertEqual({drv['corners'][c][k] for c in drv['corners'] for k in ('rpin', 'lpin', 'cpin')}, {'TYP'})
        self.assertAlmostEqual(physics_r(cable), 6.35, places=2)
        self.assertEqual(set(self.lib.hashes('ws2812b_din')), {'profile', 'parts', 'receiver', 'cable', 'connector'})
        with self.assertRaises(M.ModelError):
            self.lib.get('profile', 'nope')

    def test_part_table(self):
        self.assertEqual(self.lib.part('Texas_Instruments_SN74LVC1T45DBVR:SOT-23-6')['kind'], 'driver')
        self.assertEqual(self.lib.part('JST_B3B_PH_K_S_LF__SN:CONN-TH_B3B-PH-K-S')['model'], 'jst-ph')
        self.assertEqual(self.lib.part('YAGEO_RC0402FR_0733RL:R0402')['ohm'], 33.0)
        self.assertEqual(self.lib.part('UNI_ROYAL_0402WGF1000TCE:R0402')['ohm'], 100.0)
        self.assertIsNone(self.lib.part('Samsung_CL05B104KO5NNNC:C0402'))

    def test_decode_resistor(self):
        self.assertEqual(M.decode_resistor('UNI_ROYAL_0402WGF1000TCE')['ohm'], 100.0)
        self.assertEqual(M.decode_resistor('UNI_ROYAL_0402WGF3162TCE')['ohm'], 31600.0)
        self.assertEqual(M.decode_resistor('UNI_ROYAL_0402WGF0000TCE')['ohm'], 0.0)
        self.assertEqual(M.decode_resistor('YAGEO_RC0402FR_0733RL')['ohm'], 33.0)
        self.assertEqual(M.decode_resistor('YAGEO_RC0402FR_074R7L')['ohm'], 4.7)
        self.assertIsNone(M.decode_resistor('Samsung_CL05B104KO5NNNC'))

    def test_bad_model_dir(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'x.json').write_text(json.dumps(dict(schema='pnr-si-thing-v1', id='x')))
            with self.assertRaises(M.ModelError):
                M.Library(d)


class FetchTest(unittest.TestCase):
    def entry(self, data=b'IBIS DATA', member='part.ibs'):
        z = zip_bytes(member, data)
        return z, dict(id='ibis', url='https://example.invalid/x', zip_sha256=hashlib.sha256(z).hexdigest(), member=member,
                       member_sha256=hashlib.sha256(data).hexdigest(), license='test')

    def test_fetch_verify_and_cache(self):
        z, e = self.entry()
        calls = []

        def dl(url, timeout):
            calls.append(url)
            return z
        with tempfile.TemporaryDirectory() as cache:
            p = M.fetch_vendor(e, cache=cache, env={}, download=dl)
            self.assertEqual(p.read_bytes(), b'IBIS DATA')
            self.assertEqual(p.parent.name, e['zip_sha256'])
            self.assertEqual(json.loads((p.parent / 'SOURCE.json').read_text())['license'], 'test')
            M.fetch_vendor(e, cache=cache, env={}, download=dl)
            self.assertEqual(len(calls), 1)                              # cache hit, no second download
            p.write_bytes(b'tampered')
            M.fetch_vendor(e, cache=cache, env={}, download=dl)          # re-fetched on hash mismatch
            self.assertEqual(p.read_bytes(), b'IBIS DATA')

    def test_hash_mismatch_and_unreachable(self):
        z, e = self.entry()
        with tempfile.TemporaryDirectory() as cache:
            with self.assertRaises(M.ModelError):
                M.fetch_vendor(dict(e, zip_sha256='0' * 64), cache=cache, env={}, download=lambda u, t: z)
            with self.assertRaises(M.ModelError):
                M.fetch_vendor(dict(e, member_sha256='0' * 64), cache=cache, env={}, download=lambda u, t: z)

            def boom(u, t):
                raise OSError('offline')
            with self.assertRaises(M.ModelError):
                M.fetch_vendor(e, cache=cache, env={}, download=boom)

    def test_offline_seed(self):
        z, e = self.entry()
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as seed:
            Path(seed, 'anything.ibs').write_bytes(b'IBIS DATA')
            p = M.fetch_vendor(e, cache=cache, env={'PNR_SI_VENDOR_SEED': seed},
                               download=lambda u, t: self.fail('must not download'))
            self.assertEqual(p.read_bytes(), b'IBIS DATA')


class KibisTest(unittest.TestCase):
    def test_cli_guard(self):
        with self.assertRaises(M.ModelError):
            M.kicad_cli({'PNR_KICAD_CLI': '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli'})
        with self.assertRaises(M.ModelError):
            M.kicad_cli({'PNR_KICAD_CLI': '/nonexistent/kicad-cli'})

    def test_testbench_is_deterministic(self):
        f = {'Sim.Device': 'IBIS', 'Sim.Type': 'RECTDRIVER', 'Sim.Pins': '1=GND 2=IN/OUT', 'Sim.Params': 'vcc=MIN ton=40n'}
        a, b = M.testbench_sch(f, 'k1'), M.testbench_sch(f, 'k1')
        self.assertEqual(a, b)
        self.assertNotEqual(a, M.testbench_sch(f, 'k2'))
        self.assertIn('(property "Sim.Params" "vcc=MIN ton=40n"', a)
        self.assertEqual(a.count('(label "0"'), 1)                      # ground net must be named 0
        self.assertEqual(a.count('(lib_id "tb:IBIS2")'), 1)             # one symbol

    def test_lowercase_corner_rejected(self):
        drv = M.Library().get('driver', 'sn74lvc1t45')
        bad = json.loads(json.dumps(drv))
        bad['corners']['min']['vcc'] = 'min'
        with self.assertRaises(M.ModelError):
            M.kibis_driver(bad, 4, 'min', 40.0, 2.0, env={'PNR_KICAD_CLI': '/nonexistent'})
        with self.assertRaises(M.ModelError):
            M.kibis_driver(drv, 4, 'typ', 20.0, 2.0)                    # window shorter than the IBIS tables
        with self.assertRaises(M.ModelError):
            M.kibis_driver(drv, 1, 'typ', 40.0, 2.0)                    # VCCA pin has no driver model


class DecimateTest(unittest.TestCase):
    def test_collinear_points_removed_losslessly(self):
        ts = [i * 1e-10 for i in range(1000)]
        vs = [0.0] * 300 + [(i - 300) / 100 for i in range(300, 400)] + [1.0] * 600
        keep = M.decimate(ts, vs, 1e-9)
        self.assertEqual(keep, [0, 300, 400, 999])          # the ramp corners only

    def test_tolerance_bound_on_curve(self):
        import math
        ts = [i * 1e-11 for i in range(2000)]
        vs = [1 - math.exp(-t / 3e-9) for t in ts]
        tol = 1e-3
        keep = M.decimate(ts, vs, tol)
        self.assertLess(len(keep), 200)
        for a, b in zip(keep, keep[1:]):
            for k in range(a + 1, b):
                u = (ts[k] - ts[a]) / (ts[b] - ts[a])
                self.assertLessEqual(abs(vs[a] + u * (vs[b] - vs[a]) - vs[k]), tol + 1e-12)

    def test_model_text(self):
        text = ('.SUBCKT U1.X GND PIN\nVku KU GND pwl ( 0 0 1e-12 0 2e-12 0 3e-12 0.5 4e-12 1 5e-12 1 )\n'
                'a1 %vd(DIE0 GC_GND0) %id(DIE0 GC_GND0) GC0\n.ENDS DRIVER\n')
        out, stats = M.decimate_model(text)
        self.assertEqual(stats, [dict(source='Vku', before=6, after=4)])
        self.assertIn('a1 %vd(DIE0 GC_GND0) %id(DIE0 GC_GND0) GC0', out)
        pts = re.search(r'pwl \( (.*) \)', out).group(1).split()
        self.assertEqual([float(x) for x in pts], [0, 0, 2e-12, 0, 4e-12, 1, 5e-12, 1])


@unittest.skipUnless(os.environ.get('PNR_SI_LIVE') == '1', 'live KIBIS via headless kicad-cli (PNR_SI_LIVE=1)')
class LiveKibisTest(unittest.TestCase):
    def test_corners_convert_with_their_rails(self):
        lib = M.Library()
        drv = lib.get('driver', 'sn74lvc1t45')
        with tempfile.TemporaryDirectory() as cache:
            seen = {}
            for corner, rail in (('typ', 5.0), ('min', 4.5), ('max', 5.5)):
                r = M.kibis_driver(drv, 4, corner, 40.0, 2.0, cache=cache)
                self.assertFalse(r['cached'])
                self.assertEqual(r['rail_v'], rail)
                self.assertEqual(r['subckt'], 'U1.SN74LVC1T45_DBV')
                self.assertEqual(r['pins'], ['GND', 'PIN'])
                self.assertEqual(r['model'], 'LVC1T45_IO_B_50')
                text = Path(r['path']).read_text()
                # structure golden: package parasitics, K-factor PWL sources, I-V tables
                for pat in (r'^RPIN ', r'^LPIN ', r'^CPIN ', r'^Vku KU GND pwl', r'^Vkd KD GND pwl', r'^\.model GC0 pwl',
                            r'^\.model PD0 pwl', r'^\.model PU0 pwl', r'^\.ENDS'):
                    self.assertTrue(re.search(pat, text, re.M), pat)
                seen[corner] = r['sha256']
            self.assertEqual(len(set(seen.values())), 3)
            again = M.kibis_driver(drv, 4, 'min', 40.0, 2.0, cache=cache)
            self.assertTrue(again['cached'])


if __name__ == '__main__':
    unittest.main()
