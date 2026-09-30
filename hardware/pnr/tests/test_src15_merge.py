"""src15 merge: the conflict resolutions where src13 (PNR_PAIR_LANDING_RESERVE, landing
reserves) and N-0001 (PNR_MACRO_SHRINK / PNR_MACRO_HULL / PNR_LIBRARY_RANK_USED) and src14
(PNR_SI rank key) meet: graph JSON, placement_rects, the legalizer, macro collapse and
synth_native.rank_key. Each flag alone must reproduce its own line; both together compose.

  PYTHONPATH=$PWD/..:$PWD .../pnr-regression-runtime/bin/python -m unittest -v test_src15_merge
"""
import copy, json, math, os, unittest
from unittest import mock

try:
    import numpy  # noqa: F401
    from pnr.place.legalize import legalize
    from pnr.place import hull as H
    from pnr.hier import extent as E
    NUMPY = True
except Exception:  # KiCad Python: no numpy
    NUMPY = False

from pnr.graph import BoardGraph, Component, Pad

FLAGS = ('PNR_PAIR_LANDING_RESERVE', 'PNR_MACRO_SHRINK', 'PNR_MACRO_HULL', 'PNR_LIBRARY_RANK_USED', 'PNR_SI',
         'PNR_POWER_FIRST', 'PNR_PAD_EDGE_CLEARANCE')


def env(**on):
    values = {k: None for k in FLAGS}
    values.update(on)

    class _Env:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in values}
            for k, v in values.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = str(v)
        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
    return _Env()


RESERVE = dict(kind='pair_landing', pair='usb', pads=['U1.1', 'U1.2'], side='opposite',
               runs_mm={'-1': 0.8, '1': 0.8}, via_radius_mm=0.35, half_width_mm=0.6)
MOUNT = dict(kind='macro_mount', sides=['top'])


def terminal(ref='T1', pos=(5.0, 5.0), reserves=True):
    return Component(ref, 'QFN', pos, 0.0, 'top', (2.0, 2.0), (2.0, 2.0),
                     pads=[Pad('1', 'Dp', (-0.25, 0.9), (0.2, 0.4)), Pad('2', 'Dn', (0.25, 0.9), (0.2, 0.4))],
                     reserves=[dict(RESERVE, pads=['1', '2'])] if reserves else [])


class GraphJsonTest(unittest.TestCase):
    def test_default_graph_has_neither_key(self):
        g = BoardGraph('t', [terminal(reserves=False)])
        c = json.loads(g.to_json())['components'][0]
        self.assertNotIn('reserves', c); self.assertNotIn('hull', c)

    def test_both_fields_round_trip(self):
        comp = terminal(); comp.hull = {'version': 2, 'top': [[0, 0, 1, 1]]}
        back = BoardGraph.from_json(BoardGraph('t', [comp]).to_json()).components[0]
        self.assertEqual(back.reserves, comp.reserves); self.assertEqual(back.hull, comp.hull)


try:
    import yaml  # noqa: F401  (pnr.hier.synth_native imports it; KiCad Python lacks it)
    YAML = True
except ImportError:
    YAML = False


@unittest.skipUnless(YAML, 'needs yaml (PnR runtime)')
class RankKeyTest(unittest.TestCase):
    REC = dict(objective=[1, 2, 3, 4.5, 5, 6], si_layout_failures=7, used_band=8, used={'area_mm2': 9.0},
               port_debt_mm=10.0, area=11.0, hot_loops_open=12, power_quality={'q_band': 13, 'crossings': 14})

    def key(self, **on):
        from pnr.hier.synth_native import rank_key
        with env(**on):
            return rank_key(dict(self.REC))

    def test_each_flag_alone_is_its_line(self):
        self.assertEqual(self.key(), (6, 1, 2, 3, 4.5, 5, 10.0, 11.0))
        self.assertEqual(self.key(PNR_SI='1'), (6, 1, 7, 2, 3, 4.5, 5, 10.0, 11.0))                      # src14
        self.assertEqual(self.key(PNR_LIBRARY_RANK_USED='1'), (6, 1, 2, 3, 8, 4.5, 5, 10.0, 9.0, 11.0))   # src12n
        self.assertEqual(self.key(PNR_POWER_FIRST='1', PNR_LIBRARY_RANK_USED='1'),
                         (6, 1, 12, 13, 14, 2, 3, 8, 4.5, 5, 10.0, 9.0, 11.0))

    def test_si_and_used_band_compose(self):
        self.assertEqual(self.key(PNR_SI='1', PNR_LIBRARY_RANK_USED='1'), (6, 1, 7, 2, 3, 8, 4.5, 5, 10.0, 9.0, 11.0))
        self.assertEqual(self.key(PNR_SI='1', PNR_LIBRARY_RANK_USED='1', PNR_POWER_FIRST='1'),
                         (6, 1, 12, 7, 13, 14, 2, 3, 8, 4.5, 5, 10.0, 9.0, 11.0))


@unittest.skipUnless(NUMPY, 'needs numpy (PnR runtime)')
class PlacementRectsTest(unittest.TestCase):
    def macro(self):
        geo = E.BlockGeometry(ok=True, extent=(-5.0, -3.0, 5.0, 3.0),
                              shapes=dict(top=dict(rect=[[-5.0, -3.0, -1.0, 3.0]], cap=[[-4.6, 1.5, 4.6, 1.5, 0.45]]),
                                          bottom=dict(rect=[], cap=[[3.0, 2.4, 3.0, 2.4, 0.575]])),
                              c_cu=0.35, margin=0.3, track_width=0.2)
        hull = E.build_hull(geo, (0.0, 0.0), (10.0, 6.0), 0.2)
        return Component('MB00', 'block:test', (15.125, 10.125), 0.0, 'top', (10.0, 6.0), (10.0, 6.0),
                         pads=[Pad('U1.1', 'Dp', (-3.25, 0.0), (0.2, 0.4)), Pad('U1.2', 'Dn', (-2.75, 0.0), (0.2, 0.4))],
                         address='block:test', hull=hull, reserves=[dict(RESERVE), dict(MOUNT)])

    def test_hull_alone_is_the_n0001_output(self):
        from pnr.place.geometry import placement_rects, MountedRect, ReserveRect
        m = self.macro()
        with env(PNR_MACRO_HULL='1'):
            got = placement_rects(m)
        self.assertEqual(got, H.hull_placement_rects(m))
        self.assertFalse(any(isinstance(r, (MountedRect, ReserveRect)) for _, r in got))

    def test_landing_alone_is_the_src13_output(self):
        from pnr.place.geometry import placement_rects, MountedRect, ReserveRect, courtyard_rect
        m = self.macro()
        with env(PNR_PAIR_LANDING_RESERVE='1'):
            got = placement_rects(m)
        bodies = [(s, r) for s, r in got if not isinstance(r, ReserveRect)]
        # a block macro's courtyard occupies both sides; both body rects carry its mount side
        cr = MountedRect(*[getattr(courtyard_rect(m), a) for a in ('cx', 'cy', 'w', 'h')], mount='top')
        self.assertEqual(sorted(bodies), [('bottom', cr), ('top', cr)])
        self.assertEqual(sum(isinstance(r, ReserveRect) for _, r in got), 2)

    def test_both_flags_tag_hull_rects_and_add_reserves(self):
        from pnr.place.geometry import placement_rects, MountedRect, ReserveRect
        m = self.macro()
        with env(PNR_PAIR_LANDING_RESERVE='1', PNR_MACRO_HULL='1'):
            got = placement_rects(m)
        hull = H.hull_placement_rects(m)
        bodies = [(s, r) for s, r in got if not isinstance(r, ReserveRect)]
        self.assertEqual([(s, (r.cx, r.cy, r.w, r.h)) for s, r in bodies], [(s, (r.cx, r.cy, r.w, r.h)) for s, r in hull])
        self.assertTrue(all(isinstance(r, MountedRect) and r.mount == 'top' for _, r in bodies))
        reserves = [r for _, r in got if isinstance(r, ReserveRect)]
        self.assertEqual(len(reserves), 2); self.assertTrue(all(r.side == 'bottom' for r in reserves))


@unittest.skipUnless(NUMPY, 'needs numpy (PnR runtime)')
class LegalizeCompositionTest(unittest.TestCase):
    """A top terminal part with bottom landing reserves, a bottom part aimed at them and a
    hull macro: each flag alone and both together legalize with no overlap."""

    def graph(self):
        macro = PlacementRectsTest.macro(self)
        macro.reserves = [dict(MOUNT)]
        return BoardGraph('t', [macro, terminal(pos=(25.0, 15.0)),
                                Component('B1', 'R0402', (25.0, 16.6), 0.0, 'bottom', (1.0, 0.6), (1.0, 0.6),
                                          pads=[Pad('1', 'x', (0.0, 0.0), (0.4, 0.4))])])

    def run_(self, **on):
        from pnr.place.metrics import overlap_pairs
        with env(**on):
            out = legalize(self.graph(), 30.0, 20.0, fixed={'T1': (25.0, 15.0)}, keepouts=[], clearance=0.2, grid_mm=0.25,
                           rotations={'MB00': 0.0})
            return out, overlap_pairs(out)

    def test_flags_off_bottom_part_stays_on_the_landing(self):
        out, pairs = self.run_()
        self.assertEqual(pairs, [])
        self.assertLess(math.dist(out.component('B1').pos, (25.0, 16.6)), 0.3)

    def test_landing_moves_the_bottom_part_off_the_reserve(self):
        from pnr.place.geometry import placement_rects, ReserveRect
        for on in (dict(PNR_PAIR_LANDING_RESERVE='1'), dict(PNR_PAIR_LANDING_RESERVE='1', PNR_MACRO_HULL='1')):
            out, pairs = self.run_(**on)
            self.assertEqual(pairs, [], on)
            with env(**on):
                res = [r for s, r in placement_rects(out.component('T1')) if isinstance(r, ReserveRect)]
                body = [r for s, r in placement_rects(out.component('B1')) if s == 'bottom']
            self.assertTrue(res and body)
            self.assertFalse(any(r.overlaps(b) for r in res for b in body), on)

    def test_hull_alone_and_with_landing_keep_the_macro_legal(self):
        for on in (dict(PNR_MACRO_HULL='1'), dict(PNR_MACRO_HULL='1', PNR_PAIR_LANDING_RESERVE='1')):
            out, pairs = self.run_(**on)
            self.assertEqual(pairs, [], on)


class MacroCollapseTest(unittest.TestCase):
    """collapse() carries member landing reserves onto shaped (shrink/hull) macros too."""

    @unittest.skipUnless(NUMPY, 'needs numpy (PnR runtime)')
    def test_shaped_macro_keeps_reserves_and_mount(self):
        from pnr.hier.macro import collapse
        from pnr.constraints import compile_constraints
        member = terminal('U1', pos=(3.0, 3.0))
        sub = BoardGraph('blk', [member])
        flat = BoardGraph('top', [copy.deepcopy(member), Component('R9', 'R', (1, 1), 0, 'top', (1, 1), (1, 1))])
        cons = compile_constraints({'board': {'outline': {'w': 30, 'h': 20}}}, flat.refs)
        geo = E.BlockGeometry(ok=True, extent=(1.0, 1.0, 5.0, 5.0), shapes=dict(top=dict(rect=[[1.0, 1.0, 5.0, 5.0]], cap=[]),
                                                                              bottom=dict(rect=[], cap=[])),
                              c_cu=0.35, margin=0.3, track_width=0.2)
        block = type('B', (), {'name': 'blk'})()
        for on in (dict(PNR_PAIR_LANDING_RESERVE='1'), dict(PNR_PAIR_LANDING_RESERVE='1', PNR_MACRO_SHRINK='1'),
                   dict(PNR_PAIR_LANDING_RESERVE='1', PNR_MACRO_SHRINK='1', PNR_MACRO_HULL='1')):
            with env(**on):
                mg, *_, plan = collapse(flat, cons, {'fab': {'edge_clearance_mm': 0.3}}, [(block, sub, 6.0, 6.0)], geometry={'blk': geo})
            mb = mg.component('MB00')
            kinds = sorted(r['kind'] for r in mb.reserves)
            self.assertEqual(kinds, ['macro_mount', 'pair_landing'], on)
            self.assertEqual(next(r for r in mb.reserves if r['kind'] == 'pair_landing')['pads'], ['U1.1', 'U1.2'])
        with env():
            mg, *_ = collapse(flat, cons, {'fab': {'edge_clearance_mm': 0.3}}, [(block, sub, 6.0, 6.0)], geometry={'blk': geo})
        self.assertEqual([r['kind'] for r in mg.component('MB00').reserves], ['pair_landing'])  # member recipe only


class KiCadDefaultsTest(unittest.TestCase):
    """Every defaulted KiCad cli/python path honours PNR_KICAD_CLI / PNR_KICAD_PYTHON."""
    ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
    # Not a cli/python default: SI refusal constants (they name the GUI bundle to refuse it),
    # halving.KI (unused), the GUI-bound warm-DRC host (pcbnew.app; off, documented).
    EXEMPT = {'pnr/si/runner.py', 'pnr/si/models.py', 'pnr/si/extract.py', 'pnr/mc/halving.py', 'pnr/drc_warm/launch_host.py'}

    def test_no_bare_applications_default(self):
        import re
        bad = []
        for path in sorted(self.ROOT.glob('pnr/**/*.py')) + [self.ROOT / 'regression/run.py']:
            rel = str(path.relative_to(self.ROOT))
            if rel in self.EXEMPT:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for m in re.finditer(r"'/Applications/KiCad[^']*'", line):
                    before = line[:m.start()]
                    if not re.search(r"os\.environ\.get\('PNR_KICAD_(CLI|PYTHON)',\s*$", before) and 'KI=' not in line.replace(' ', ''):
                        bad.append('%s:%d' % (rel, n))
                if re.search(r"default='kicad-cli'|default=sys\.executable", line):
                    bad.append('%s:%d' % (rel, n))
        self.assertEqual(bad, [])

    def test_kicad_path_constants_follow_the_environment(self):
        text = (self.ROOT / 'regression/run.py').read_text()
        self.assertIn("os.environ.get('PNR_KICAD_PYTHON',KI+", text)
        self.assertIn("os.environ.get('PNR_KICAD_CLI',KI+", text)
        bzl = (self.ROOT / 'pnr.bzl').read_text()
        self.assertIn('${PNR_KICAD_CLI:-', bzl)
        self.assertIn('${PNR_KICAD_PYTHON:-', bzl)


if __name__ == '__main__':
    unittest.main()
