"""SI geometry extraction: frozen p027 LED copper, synthetic T-junctions/stubs/vias/opens, estimates."""
import json
import os
import unittest
from pathlib import Path

from pnr.si import annotations as A
from pnr.si import extract as X
from pnr.si import models, physics
from tests.test_si_annotations import fixture_components

TESTDATA = Path(__file__).resolve().parents[1] / 'testdata/si'
ST = physics.stackup({})


def p027_intents():
    reqs, waivers = A.parse([TESTDATA / 'p027-si.ato'])
    return A.resolve(reqs, waivers, fixture_components(), models.Library(), env={})


def total_copper(leg):
    per = dict(leg['copper_mm'])
    for a in leg['attach']:
        if a['kind'] == 'stub':
            per[a['layer']] = per.get(a['layer'], 0.0) + a['length_mm']
    return per


class P027ExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dump = json.loads((TESTDATA / 'p027-led-dump.json').read_text())
        cls.intents = {i['name']: i for i in p027_intents()}

    def test_fixture_provenance(self):
        src = json.loads((TESTDATA / 'SOURCE.json').read_text())
        self.assertEqual(self.dump['board_sha256'], src['board_sha256'])

    def test_led0_matches_measured_copper(self):
        geo = X.routed_geometry(self.dump, self.intents['led0_data'], ST)
        self.assertEqual(geo['status'], 'ok')
        b, out = geo['legs']
        self.assertEqual((b['source'], b['target']), ('U9.4', 'R21.1'))
        # design sec. 11 WP10 anchors (+-0.05 mm): board.led0-B F 4.89 / In2 7.79 / B 3.35, 3 vias
        tot = total_copper(b)
        for layer, mm in (('F.Cu', 4.89), ('In2.Cu', 7.79), ('B.Cu', 3.35)):
            self.assertAlmostEqual(tot[layer], mm, delta=0.05, msg=layer)
        self.assertEqual(b['vias'], 3)
        self.assertEqual([e['kind'] for e in b['elements']], ['seg', 'via', 'seg', 'via', 'seg', 'via', 'seg'])
        self.assertEqual([(e['from'], e['to']) for e in b['elements'] if e['kind'] == 'via'],
                         [('F.Cu', 'B.Cu'), ('B.Cu', 'In2.Cu'), ('In2.Cu', 'F.Cu')])
        self.assertAlmostEqual(total_copper(out)['F.Cu'], 7.96, delta=0.05)
        self.assertEqual(out['vias'], 0)
        self.assertEqual(out['target'], 'CN1.2')
        self.assertEqual(b['warnings'] + out['warnings'], [])

    def test_led1_matches_measured_copper(self):
        geo = X.routed_geometry(self.dump, self.intents['led1_data'], ST)
        b, out = geo['legs']
        tb, to = total_copper(b), total_copper(out)
        self.assertAlmostEqual(tb['F.Cu'], 2.07, delta=0.05)
        self.assertAlmostEqual(tb['In2.Cu'], 2.88, delta=0.05)
        self.assertEqual(b['vias'], 2)
        self.assertAlmostEqual(to['F.Cu'], 2.38, delta=0.05)
        self.assertAlmostEqual(to['In2.Cu'], 6.84, delta=0.05)
        self.assertEqual(out['vias'], 1)

    def test_ideal_and_estimate(self):
        it = self.intents['led0_data']
        ideal = X.ideal_geometry(it)
        self.assertEqual([l['elements'] for l in ideal['legs']], [[], []])
        est = X.estimate_geometry(it, fixture_components())
        # U9.4 -> R21.1 and R21.2 -> CN1.2: Manhattan x 1.1, routed/Manhattan was 0.98-1.10 on p027
        routed = X.routed_geometry(self.dump, it, ST)
        for e, r in zip(est['legs'], routed['legs']):
            self.assertGreater(e['length_mm'], 0.5 * r['length_mm'])
            self.assertLess(e['length_mm'], 2.0 * r['length_mm'] + 2.0)
        pes = X.estimate_geometry(it, fixture_components(), variant='pessimistic')
        self.assertEqual(pes['legs'][0]['vias'], 2)
        self.assertAlmostEqual(sum(x['length_mm'] for x in pes['legs'][0]['elements'] if x['kind'] == 'seg'),
                               est['legs'][0]['length_mm'], places=6)


def synth(tracks, vias=(), pads=()):
    base = dict(pads1=[], pads2=[], arc=False, w=0.2, net='n')
    ts = []
    for t in tracks:
        t = dict(base, **t)
        t.setdefault('length', ((t['x2'] - t['x1']) ** 2 + (t['y2'] - t['y1']) ** 2) ** 0.5)
        ts.append(t)
    return dict(tracks=ts, vias=[dict(dict(net='n', d=0.45, drill=0.3), **v) for v in vias],
                pads=[dict(dict(net='n', smd=True, layers=['F.Cu']), **p) for p in pads],
                copper_layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])


def pad(ref, name, x, y, layers=('F.Cu',), smd=True):
    return dict(ref=ref, name=name, x=x, y=y, bbox=[x - 0.3, y - 0.3, x + 0.3, y + 0.3], layers=list(layers), smd=smd)


class EstimateLayersAndPythonTest(unittest.TestCase):
    def test_estimate_layers_follow_the_stackup(self):
        st = physics.stackup({})                                           # planes: In1.Cu
        self.assertEqual(X.estimate_layers(st), ('F.Cu', 'In2.Cu'))
        self.assertEqual(X.estimate_layers(dict(st, planes=['In1.Cu', 'In2.Cu'])), ('F.Cu', 'B.Cu'))
        two = dict(st, layers=[l for l in st['layers'] if l['name'] in ('F.Cu', 'core', 'B.Cu')], planes=['B.Cu'])
        self.assertEqual(X.estimate_layers(two), ('F.Cu', 'B.Cu'))
        it = {i['name']: i for i in p027_intents()}['led0_data']
        pes = X.estimate_geometry(it, fixture_components(), variant='pessimistic',
                                  st=dict(st, planes=['In1.Cu', 'In2.Cu']))
        self.assertEqual([e.get('to') for e in pes['legs'][0]['elements'] if e['kind'] == 'via'], ['B.Cu', 'F.Cu'])

    def test_gui_python_fallback_warns_or_refuses(self):
        from unittest import mock
        gui = X.GUI_PY
        with mock.patch('os.access', side_effect=lambda p, m: p == gui), mock.patch('sys.stderr'):
            py, warning = X.kicad_python({})
            self.assertEqual(py, gui)
            self.assertIn('GUI bundle', warning)
            with self.assertRaises(FileNotFoundError):
                X.kicad_python({'PNR_SI_NO_GUI_PYTHON': '1'})
        with mock.patch('os.access', return_value=True):
            self.assertEqual(X.kicad_python({'PNR_SI_KICAD_PYTHON': '/x/python3'}), ('/x/python3', None))
            # src15: PNR_KICAD_PYTHON (the engine-wide KiCad python) after the SI override
            self.assertEqual(X.kicad_python({'PNR_KICAD_PYTHON': '/k/python3'}), ('/k/python3', None))
            self.assertEqual(X.kicad_python({'PNR_SI_KICAD_PYTHON': '/x/python3', 'PNR_KICAD_PYTHON': '/k/python3'}),
                             ('/x/python3', None))
        # a set PNR_KICAD_PYTHON never falls back to the GUI bundle python
        with mock.patch('os.access', side_effect=lambda p, m: p == gui), mock.patch('sys.stderr'):
            with self.assertRaises(FileNotFoundError):
                X.kicad_python({'PNR_KICAD_PYTHON': '/missing/python3'})


class SyntheticExtractTest(unittest.TestCase):
    def leg(self, dump):
        cu = X.Copper(dump, 'n', physics.copper_layers(ST), lambda a, b: physics.via_span_mm(ST, a, b))
        return X._leg(cu, ('U1', '1'), ('R1', '1'), ST)

    def test_t_junction_stub_and_extra_pad(self):
        d = synth([dict(layer='F.Cu', x1=0, y1=0, x2=10, y2=0, pads1=[['U1', '1']], pads2=[['R1', '1']]),
                   dict(layer='F.Cu', x1=4, y1=0, x2=4, y2=3, pads2=[['TP', '1']])],
                  pads=[pad('U1', '1', 0, 0), pad('R1', '1', 10, 0), pad('TP', '1', 4, 3)])
        leg = self.leg(d)
        self.assertEqual(leg['status'], 'ok')
        self.assertEqual([e['kind'] for e in leg['elements']], ['seg'])
        self.assertAlmostEqual(leg['elements'][0]['length_mm'], 10.0)
        self.assertEqual(leg['elements'][0]['n_merged'], 2)            # split at the junction, merged back
        stubs = [a for a in leg['attach'] if a['kind'] == 'stub']
        self.assertEqual(len(stubs), 1)
        self.assertAlmostEqual(stubs[0]['length_mm'], 3.0)
        self.assertIn(dict(at=1, kind='pad', ref='TP', pad='1'), leg['attach'])

    def test_layer_change_through_via_and_via_stub(self):
        d = synth([dict(layer='F.Cu', x1=0, y1=0, x2=5, y2=0, pads1=[['U1', '1']]),
                   dict(layer='In2.Cu', x1=5, y1=0, x2=9, y2=0),
                   dict(layer='In2.Cu', x1=9, y1=0, x2=9, y2=0.001 + 3),
                   dict(layer='F.Cu', x1=9, y1=3.001, x2=12, y2=3.001, pads2=[['R1', '1']]),
                   dict(layer='F.Cu', x1=2, y1=0, x2=2, y2=1)],
                  vias=[dict(x=5, y=0, top='F.Cu', bottom='B.Cu'), dict(x=9, y=3.001, top='F.Cu', bottom='B.Cu'),
                        dict(x=2, y=1, top='F.Cu', bottom='B.Cu')],
                  pads=[pad('U1', '1', 0, 0), pad('R1', '1', 12, 3.001)])
        leg = self.leg(d)
        self.assertEqual([e['kind'] for e in leg['elements']], ['seg', 'via', 'seg', 'via', 'seg'])
        self.assertEqual(leg['copper_mm'], {'F.Cu': 8.0, 'In2.Cu': 7.001})
        self.assertEqual(leg['vias'], 2)
        vs = [e for e in leg['elements'] if e['kind'] == 'via']
        self.assertEqual((vs[0]['from'], vs[0]['to']), ('F.Cu', 'In2.Cu'))
        kinds = sorted(a['kind'] for a in leg['attach'])
        self.assertEqual(kinds, ['stub', 'via_stub'])

    def test_open_leg(self):
        d = synth([dict(layer='F.Cu', x1=0, y1=0, x2=4, y2=0, pads1=[['U1', '1']])],
                  pads=[pad('U1', '1', 0, 0), pad('R1', '1', 10, 0)])
        self.assertEqual(self.leg(d)['status'], 'open')

    def test_via_in_pad_and_pth(self):
        d = synth([dict(layer='B.Cu', x1=0, y1=0, x2=6, y2=0, pads2=[['R1', '1']])],
                  vias=[dict(x=0, y=0, top='F.Cu', bottom='B.Cu')],
                  pads=[pad('U1', '1', 0, 0), pad('R1', '1', 6, 0, layers=('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'), smd=False)])
        leg = self.leg(d)
        self.assertEqual(leg['status'], 'ok')
        self.assertEqual([e['kind'] for e in leg['elements']], ['via', 'seg'])

    def test_routed_geometry_rejects_unknown_layers(self):
        d = synth([])
        d['copper_layers'] = ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']
        with self.assertRaises(ValueError):
            X.routed_geometry(d, p027_intents()[0], ST)


@unittest.skipUnless(os.environ.get('PNR_SI_LIVE') == '1', 'live pcbnew read (PNR_SI_LIVE=1)')
class LiveDumpTest(unittest.TestCase):
    def test_pcbnew_dump_matches_frozen_fixture(self):
        src = json.loads((TESTDATA / 'SOURCE.json').read_text())
        board = os.environ.get('PNR_SI_E2E_BOARD') or str(Path(__file__).resolve().parents[4] / src['board'].split('output/hier/', 1)[1])
        if not Path(board).exists():
            self.skipTest('board not found: %s' % board)
        frozen = json.loads((TESTDATA / 'p027-led-dump.json').read_text())
        d = X.read_board(board, frozen['nets'])
        if d['board_sha256'] != frozen['board_sha256']:
            self.skipTest('board changed since the fixture was frozen')
        self.assertEqual(sorted(json.dumps(t, sort_keys=True) for t in d['tracks']),
                         sorted(json.dumps(t, sort_keys=True) for t in frozen['tracks']))
        self.assertEqual(len(d['vias']), len(frozen['vias']))


if __name__ == '__main__':
    unittest.main()
