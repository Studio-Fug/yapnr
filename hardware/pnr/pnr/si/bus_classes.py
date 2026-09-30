"""Bus-type annotation classes (``PNR_BUS_CLASSES=1``, default off).

Owner decision 2026-09-29: annotation classes for common bus types (USB-LS/FS/HS/SS,
...) map to signalling speed and impedance-control requirements, so the rules
generalise to other designs. The class data (every value cited: document, section,
table) lives in ``hardware/pnr/si_models/bus_classes.json`` (schema
``pnr-bus-classes`` v1; ``PNR_BUS_CLASSES_FILE`` overrides the path).

A ``@pnr-pair`` annotation gains an optional ``"class"`` (e.g. ``"usb2-fs"``). With the
flag set, :func:`apply_class` (called by :func:`pnr.electrical.resolve_pair_chains`)
derives numeric limits from the class plus the board stackup
(:func:`pnr.si.physics.stackup`: per-layer eps_eff and propagation delay from the
fab profile; via barrel delay from the board thickness and the dielectric) and
records each derived value in the pair's ``bus_class`` entry of rules.json with its
provenance (every ``derived`` entry carries ``class_id``, ``citation``, ``formula``,
``inputs`` and the ``stackup_inputs`` it used; the record adds the library path and
sha256, the physical stack and the per-routing-layer line models). EXPLICIT values
always win over derived ones:

* ``skew_mm``: annotation ``skew_mm`` (or ``skew_ps``) > the constraint file's
  ``skew_mm`` (explicit unless constraints.py marked it ``defaulted``) > class
  ``skew.max_ps`` / slowest routing-layer delay.
* ``max_uncoupled_mm``: annotation > ``uncoupled_k * t_rise_min`` / slowest delay.
* optional total length ``max_length_mm`` (class ``length``; report-only in v1, the
  pair router checks no total length): annotation > class.
* stub limit (intermediate terminals such as the ESD part, see
  :func:`pnr.native_electrical.pair_stub_metrics`): ``PNR_PAIR_STUB_MAX_MM`` (runtime,
  mm) > annotation ``stub_max_mm`` (mm) > annotation ``stub_delay_max_ps`` > class
  ``stub_k * t_rise_min``. A delay limit is checked on the stub's copper delay
  (per-layer ps/mm), the via barrel INCLUDED and measured to the pad CENTRE (owner
  decision 2026-09-29); an mm limit keeps the src13 reading (to where the copper
  enters the pad).

``t_rise_min`` comes from the annotation's ``t_rise_ns`` when given, else from the
class (a class whose document specifies no data-edge rate, e.g. USB 3.x, derives no
delay rule until the annotation gives ``t_rise_ns``).

With the flag off nothing here runs: the ``class`` key of an annotation is ignored
and rules.json is byte-identical to the unflagged engine. Stdlib only, Python 3.9
compatible (the KiCad-python ``prepare`` worker imports it).
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path

ENV = 'PNR_BUS_CLASSES'
SCHEMA = 'pnr-bus-classes'
SCHEMA_VERSION = 1
C_MM_PER_PS = 0.299792458          # speed of light, mm/ps
DEFAULT_FILE = Path(__file__).resolve().parents[2] / 'si_models' / 'bus_classes.json'
STUB_READING_DELAY = 'delay to the pad centre, via barrel included'
STUB_READING_MM = 'copper length to where the copper enters the pad (src13 reading)'
OVERRIDE_KEYS = ('t_rise_ns', 'stub_k', 'uncoupled_k', 'stub_delay_max_ps', 'stub_max_mm', 'skew_mm', 'skew_ps',
                 'max_uncoupled_mm', 'max_length_mm', 'params')


class BusClassError(ValueError):
    """A bus class that cannot be resolved (unknown id, schema, missing input)."""


def enabled(env=None):
    return ((os.environ if env is None else env).get(ENV) or '').strip() == '1'


def classes_path(env=None):
    env = os.environ if env is None else env
    if env.get('PNR_BUS_CLASSES_FILE'):
        return Path(env['PNR_BUS_CLASSES_FILE'])
    if env.get('PNR_SI_MODELS'):
        return Path(env['PNR_SI_MODELS']) / 'bus_classes.json'
    return DEFAULT_FILE


_CACHE = {}


def load(path=None, env=None):
    """The class library (validated) plus a ``_file`` record (path, sha256)."""
    path = Path(path) if path else classes_path(env)
    raw = path.read_bytes()
    key = (str(path), hashlib.sha256(raw).hexdigest())
    if key in _CACHE:
        return _CACHE[key]
    lib = json.loads(raw.decode('utf-8'))
    if lib.get('schema') != SCHEMA or lib.get('schema_version') != SCHEMA_VERSION:
        raise BusClassError('%s: schema %r v%r, expected %s v%d' % (path, lib.get('schema'), lib.get('schema_version'),
                                                                  SCHEMA, SCHEMA_VERSION))
    for cid, cls in lib.get('classes', {}).items():
        for part in ('signalling', 'impedance', 'skew'):
            if part not in cls:
                raise BusClassError('%s: class %s lacks %r' % (path, cid, part))
        unknown = sorted(_cited(cls) - set(lib.get('sources', {})))
        if unknown:
            raise BusClassError('%s: class %s cites unknown source(s) %s' % (path, cid, ', '.join(map(repr, unknown))))
    lib['_file'] = dict(path=str(path), sha256=key[1], schema=SCHEMA, schema_version=SCHEMA_VERSION)
    _CACHE[key] = lib
    return lib


def _r(x, n=6):
    return None if x is None else round(float(x), n)


def _eval(expr, names):
    """Arithmetic over ``names`` with min/max only (class ``expr`` fields)."""
    tree = ast.parse(expr, mode='eval')

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if names.get(node.id) is None:
                raise BusClassError('expression %r needs parameter %r' % (expr, node.id))
            return float(names[node.id])
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(node.left), ev(node.right)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Div: a / b if b else math.inf}[type(node.op)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('min', 'max') and not node.keywords:
            return (min if node.func.id == 'min' else max)(ev(a) for a in node.args)
        raise BusClassError('unsupported expression %r' % expr)
    return ev(tree)


def class_params(cls, annotation):
    """Class parameters: defaults, then the annotation's ``params`` (explicit)."""
    out = {k: (v or {}).get('default') for k, v in (cls.get('params') or {}).items()}
    given = dict(annotation.get('params') or {})
    if 't_rise_ns' in annotation and 't_rise_ns' in out:
        given.setdefault('t_rise_ns', annotation['t_rise_ns'])
    unknown = sorted(set(given) - set(out))
    if unknown:
        raise BusClassError('unknown class parameter(s) %s' % ', '.join(unknown))
    out.update(given)
    return out, sorted(given)


def t_rise(cls, annotation, params):
    """(t_rise_min in ps or None, provenance)."""
    if annotation.get('t_rise_ns') is not None:
        v = float(annotation['t_rise_ns']) * 1000.0
        if not v > 0:
            raise BusClassError('t_rise_ns must be positive')
        return v, dict(value=_r(v), unit='ps', source='explicit', annotation_key='t_rise_ns')
    spec = cls['signalling'].get('t_rise_min_ps') or {}
    value = spec.get('value')
    how = 'class'
    if spec.get('expr'):
        try:
            value = _eval(spec['expr'], params)
            how = 'class expression %s with %s' % (spec['expr'], json.dumps(params, sort_keys=True))
        except BusClassError:
            if value is None:
                return None, dict(value=None, unit='ps', source='class', note=spec.get('note'), cite=spec.get('cite'))
    if value is None:
        return None, dict(value=None, unit='ps', source='class', note=spec.get('note'), cite=spec.get('cite'))
    return float(value), dict(value=_r(value), unit='ps', source=how, cite=spec.get('cite'), note=spec.get('note'))


def _value(spec, params):
    """(value or None, provenance) of a class field {value, expr?, cite?, note?}."""
    if not isinstance(spec, dict):
        return None, None
    value, how = spec.get('value'), 'class'
    if spec.get('expr'):
        try:
            value = _eval(spec['expr'], params)
            how = 'class expression %s with %s' % (spec['expr'], json.dumps(params, sort_keys=True))
        except BusClassError:
            pass
    if value is None:
        return None, dict(value=None, source='class', note=spec.get('note'), cite=spec.get('cite'))
    return float(value), dict(value=_r(value), source=how, cite=spec.get('cite'), note=spec.get('note'))


# ------------------------------------------------------------------ stackup side

def stackup_inputs(rules):
    """Stackup, per-layer lines and via barrel inputs from pnr.si.physics."""
    from pnr.si import physics
    st = physics.stackup(rules)
    thickness = float((rules.get('electrical_fab') or {}).get('board_thickness_mm') or physics.thickness(st))
    via = physics.via(st, physics.copper_layers(st)[0], physics.copper_layers(st)[-1])
    barrel_td = math.sqrt(via['er']) / C_MM_PER_PS
    return st, dict(
        stackup=st.get('name'), fab_profile=st.get('fab_profile'),
        stack=[dict(name=x['name'], kind=x['kind'], t_mm=_r(x['t_mm']), **({'er': x['er']} if 'er' in x else {}))
               for x in st['layers']],
        stack_source=st.get('source'),
        planes=list(st.get('planes', [])),
        board_thickness_mm=_r(thickness), stack_thickness_mm=_r(physics.thickness(st)),
        barrel=dict(length_mm=_r(thickness), er=_r(via['er']), td_ps_per_mm=_r(barrel_td), ps=_r(thickness * barrel_td),
                    formula='td = sqrt(er_dielectric) / c (first-order TEM delay through the dielectric the barrel '
                            'crosses; thickness-weighted er of pnr.si.physics.via); length = electrical_fab '
                            'board_thickness_mm (the full barrel, F.Cu to B.Cu)'))


def routing_layers(st):
    from pnr.si import physics
    planes = set(st.get('planes', []))
    return [n for n in physics.copper_layers(st) if n not in planes]


def diff_impedance(rec, width, gap, b=None):
    """Edge-coupled differential impedance estimate (report-only) from a line record.

    ``b``: plane-to-plane spacing (stripline only)."""
    if gap is None or rec['z0_ohm'] is None:
        return None, None
    if rec['model'].startswith('stripline') and b:
        return 2 * rec['z0_ohm'] * (1 - 0.347 * math.exp(-2.9 * gap / b)), 'Zdiff = 2 Z0 (1 - 0.347 exp(-2.9 s/b))'
    return 2 * rec['z0_ohm'] * (1 - 0.48 * math.exp(-0.96 * gap / rec['h_mm'])), 'Zdiff = 2 Z0 (1 - 0.48 exp(-0.96 s/h))'


def layer_table(st, width, gap):
    from pnr.si import physics
    out = {}
    zt = physics.z_table(st)
    for name in physics.copper_layers(st):
        rec = physics.line(st, name, width)
        ref = rec.get('reference') or []
        b = zt[ref[1]][0] - zt[ref[0]][1] if len(ref) == 2 else None
        z, formula = diff_impedance(rec, width, gap, b)
        out[name] = dict(td_ps_per_mm=_r(rec['td_ps_per_mm']), eps_eff=_r(rec['eps_eff']), z0_ohm=_r(rec['z0_ohm'], 3),
                         zdiff_ohm=_r(z, 3), zdiff_formula=formula, model=rec['model'], reference=rec['reference'],
                         h_mm=_r(rec['h_mm']), er=_r(rec['er']))
    return out


# ------------------------------------------------------------------ derivation

def derive(class_id, annotation, pair, rules, lib=None):
    """Derived limits for ``class_id`` on this board (no pair mutation).

    Returns the provenance record stored as ``pair['bus_class']`` (without
    ``applied``, which :func:`apply_class` adds)."""
    lib = lib or load()
    cls = lib['classes'].get(class_id)
    if cls is None:
        raise BusClassError('unknown bus class %r (known: %s)' % (class_id, ', '.join(sorted(lib['classes']))))
    params, explicit_params = class_params(cls, annotation)
    tr, tr_prov = t_rise(cls, annotation, params)
    width = pair.get('width_mm') or (rules.get('fab') or {}).get('track_width_mm') or 0.2
    gap = pair.get('gap_mm')
    st, inputs = stackup_inputs(rules)
    layers = layer_table(st, width, gap)
    routing = routing_layers(st) or list(layers)
    # line models of the routing layers only (a plane layer carries no pair copper)
    layers = {n: layers[n] for n in routing}
    slow = max(routing, key=lambda n: layers[n]['td_ps_per_mm'])
    fast = min(routing, key=lambda n: layers[n]['td_ps_per_mm'])
    td_slow, td_fast = layers[slow]['td_ps_per_mm'], layers[fast]['td_ps_per_mm']
    defaults = lib.get('defaults', {})

    def k_of(name):
        if annotation.get(name) is not None:
            return float(annotation[name]), dict(source='explicit', annotation_key=name)
        if cls.get(name) is not None:
            return float(cls[name]), dict(source='class')
        return float(defaults[name]['value']), dict(source='library default', basis=defaults[name].get('basis'))

    derived = {}
    stub_k, stub_k_prov = k_of('stub_k')
    unc_k, unc_k_prov = k_of('uncoupled_k')

    def layer_inputs(names):
        """The stackup inputs a derived value used (per-layer line model + where it came from)."""
        return dict(stackup=inputs['stackup'], fab_profile=inputs['fab_profile'], planes=inputs['planes'],
                    width_mm=width, layers={n: {k: layers[n][k] for k in ('td_ps_per_mm', 'eps_eff', 'model', 'reference',
                                                                            'h_mm', 'er')} for n in names})

    tr_cite = [tr_prov['cite']] if tr_prov.get('cite') else []
    if tr is not None:
        v = stub_k * tr
        derived['stub_delay_max_ps'] = dict(
            value=_r(v, 3), unit='ps', formula='stub_k * t_rise_min_ps',
            inputs=dict(stub_k=dict(value=stub_k, **stub_k_prov), t_rise_min_ps=tr_prov),
            reading=STUB_READING_DELAY, class_id=class_id, citation=tr_cite,
            # the delay limit itself needs no stackup; the check reads copper with these
            stackup_inputs=dict(layer_inputs(routing), barrel=inputs['barrel']))
        derived['stub_max_mm_equivalent'] = dict(
            unit='mm', formula='(stub_delay_max_ps - n_barrels * barrel_ps) / td_ps_per_mm(layer) (information only; '
                               'the check uses the delay)',
            per_layer={n: dict(no_via=_r(v / layers[n]['td_ps_per_mm'], 3),
                               one_via=_r(max(0.0, v - inputs['barrel']['ps']) / layers[n]['td_ps_per_mm'], 3))
                       for n in routing},
            class_id=class_id, citation=tr_cite, stackup_inputs=dict(layer_inputs(routing), barrel=inputs['barrel']))
        u = unc_k * tr
        derived['max_uncoupled_mm'] = dict(
            value=_r(u / td_slow, 3), unit='mm', formula='uncoupled_k * t_rise_min_ps / td_ps_per_mm(slowest routing layer)',
            inputs=dict(uncoupled_k=dict(value=unc_k, **unc_k_prov), t_rise_min_ps=tr_prov,
                        layer=slow, td_ps_per_mm=td_slow),
            delay_ps=_r(u, 3), basis=(defaults.get('uncoupled_k') or {}).get('basis'), class_id=class_id,
            citation=tr_cite, stackup_inputs=layer_inputs([slow]))
    skew = cls.get('skew') or {}
    if skew.get('max_ps') is not None:
        derived['skew_mm'] = dict(
            value=_r(skew['max_ps'] / td_slow, 3), unit='mm', formula='skew.max_ps / td_ps_per_mm(slowest routing layer)',
            inputs=dict(skew_max_ps=skew['max_ps'], layer=slow, td_ps_per_mm=td_slow),
            cite=skew.get('cite'), applies_to=skew.get('applies_to'), class_id=class_id,
            citation=[skew['cite']] if skew.get('cite') else [], stackup_inputs=layer_inputs([slow]))
    length = cls.get('length') or {}
    lmm, lps = _value(length.get('max_mm'), params), _value(length.get('max_delay_ps'), params)
    if lmm[0] is not None or lps[0] is not None:
        # optional total-length rule (report-only in v1: the pair router checks no
        # total length; recorded so the owner sees it)
        if lmm[0] is not None:
            lv, lf, li, spec = lmm[0], 'length.max_mm', dict(max_mm=lmm[1]), length['max_mm']
        else:
            lv = lps[0] / td_slow
            lf, spec = 'length.max_delay_ps / td_ps_per_mm(slowest routing layer)', length['max_delay_ps']
            li = dict(max_delay_ps=lps[1], layer=slow, td_ps_per_mm=td_slow)
        derived['max_length_mm'] = dict(
            value=_r(lv, 3), unit='mm', formula=lf, inputs=li, class_id=class_id,
            delay_ps=None if lps[0] is None else _r(lps[0], 3), applies_to=length.get('applies_to'),
            citation=[spec['cite']] if spec.get('cite') else [], stackup_inputs=layer_inputs([slow]),
            enforced=False, note='report-only in v1 (the pair router checks no total length)')
    imp = cls.get('impedance') or {}
    check = None
    if imp.get('differential_ohm') is not None:
        target = float(imp['differential_ohm'])
        tol = target * float(imp['tolerance_pct']) / 100 if imp.get('tolerance_pct') is not None else float(imp.get('tolerance_ohm') or 0)
        check = dict(target_ohm=target, min_ohm=_r(target - tol, 3), max_ohm=_r(target + tol, 3),
                     required=bool(imp.get('required')), width_mm=width, gap_mm=gap,
                     per_layer={n: dict(zdiff_ohm=layers[n]['zdiff_ohm'],
                                        within=None if layers[n]['zdiff_ohm'] is None else
                                        (target - tol - 1e-9 <= layers[n]['zdiff_ohm'] <= target + tol + 1e-9))
                                for n in routing},
                     basis='report-only estimate: pnr.si.physics single-ended Z0 with the edge-coupled approximation '
                           'commonly attributed to IPC-2141; not a field solve')
    return dict(
        id=class_id, title=cls.get('title'), kind=cls.get('kind'),
        library=dict(lib['_file']), sources={k: lib['sources'][k] for k in sorted(_cited(cls)) if k in lib.get('sources', {})},
        signalling=cls.get('signalling'), impedance=imp, skew=skew,
        params=dict(values=params, explicit=explicit_params),
        t_rise_min_ps=tr_prov,
        stackup=dict(inputs, width_mm=width, gap_mm=gap, routing_layers=routing, slowest_layer=slow, fastest_layer=fast,
                     layers=layers, source='pnr.si.physics.stackup(rules) + physics.line / physics.via'),
        derived=derived, impedance_check=check, flag=ENV)


def _cited(node):
    out = set()
    if isinstance(node, dict):
        if isinstance(node.get('cite'), dict) and node['cite'].get('source'):
            out.add(node['cite']['source'])
        for v in node.values():
            out |= _cited(v)
    elif isinstance(node, list):
        for v in node:
            out |= _cited(v)
    return out


def _explicit(annotation, key):
    return annotation.get(key) is not None


def apply_class(pair, annotation, rules, lib=None, where=None):
    """Resolve ``annotation['class']`` onto ``pair`` (in place) and return the record.

    Sets ``skew_mm`` / ``max_uncoupled_mm`` (explicit values win) and ``stub_limit``
    (the limit the pair router checks), and stores the provenance in
    ``pair['bus_class']``."""
    cid = annotation['class']
    rec = derive(cid, annotation, pair, rules, lib)
    if rec['kind'] != 'differential':
        raise BusClassError('%s: class %s is %s and cannot bind a @pnr-pair' % (where or pair.get('name'), cid, rec['kind']))
    d = rec['derived']
    applied = {}
    ann = dict(source='@pnr-pair annotation', **(where or {}))

    # skew
    if _explicit(annotation, 'skew_mm'):
        applied['skew_mm'] = dict(value=float(annotation['skew_mm']), source='explicit', where=ann)
    elif _explicit(annotation, 'skew_ps'):
        slow = rec['stackup']['slowest_layer']
        v = float(annotation['skew_ps']) / rec['stackup']['layers'][slow]['td_ps_per_mm']
        applied['skew_mm'] = dict(value=_r(v, 3), source='explicit skew_ps converted on the slowest routing layer', where=ann)
    elif pair.get('skew_mm') is not None and 'skew_mm' not in (pair.get('defaulted') or ()):
        applied['skew_mm'] = dict(value=float(pair['skew_mm']), source='explicit', where=dict(source='constraints diff_pair (rules.json)'))
    elif 'skew_mm' in d:
        applied['skew_mm'] = dict(value=d['skew_mm']['value'], source='class')
    if 'skew_mm' in applied:
        applied['skew_mm']['class_derived'] = (d.get('skew_mm') or {}).get('value')
        pair['skew_mm'] = applied['skew_mm']['value']

    # continuous uncoupled run
    if _explicit(annotation, 'max_uncoupled_mm'):
        applied['max_uncoupled_mm'] = dict(value=float(annotation['max_uncoupled_mm']), source='explicit', where=ann)
    elif 'max_uncoupled_mm' in d:
        applied['max_uncoupled_mm'] = dict(value=d['max_uncoupled_mm']['value'], source='class')
    else:
        raise BusClassError('%s: no max_uncoupled_mm in the annotation and class %s derives none (no t_rise); add '
                            '"max_uncoupled_mm" or "t_rise_ns"' % (pair.get('name'), cid))
    applied['max_uncoupled_mm']['class_derived'] = (d.get('max_uncoupled_mm') or {}).get('value')
    pair['max_uncoupled_mm'] = applied['max_uncoupled_mm']['value']

    # intermediate-terminal stub (PNR_PAIR_STUB_MAX_MM still overrides at run time)
    st = rec['stackup']
    if _explicit(annotation, 'stub_max_mm'):
        limit = dict(kind='mm', max_mm=float(annotation['stub_max_mm']), reading=STUB_READING_MM, source='explicit', where=ann)
    elif _explicit(annotation, 'stub_delay_max_ps'):
        limit = dict(kind='delay', max_ps=float(annotation['stub_delay_max_ps']), reading=STUB_READING_DELAY,
                     source='explicit', where=ann)
    elif 'stub_delay_max_ps' in d:
        limit = dict(kind='delay', max_ps=d['stub_delay_max_ps']['value'], reading=STUB_READING_DELAY, source='class')
    else:
        limit = None
    if limit is not None and limit['kind'] == 'delay':
        # routing layers only: a plane layer carries no pair copper (its line model is
        # meaningless), and a layer missing from the table reads as the slowest one
        td = {n: st['layers'][n]['td_ps_per_mm'] for n in st['routing_layers']}
        limit.update(td_ps_per_mm=td,
                     barrel_ps_per_mm=st['barrel']['td_ps_per_mm'], barrel_mm=st['barrel']['length_mm'],
                     # search-side pruning must never reject a stub the final delay check
                     # accepts: the largest planar length any routing layer allows
                     search_cap_mm=_r(limit['max_ps'] / min(td.values()), 3))
    if limit is not None:
        limit['class_derived_ps'] = (d.get('stub_delay_max_ps') or {}).get('value')
        pair['stub_limit'] = limit
        applied['stub'] = limit

    # optional total length (report-only in v1; recorded, not set on the pair)
    if _explicit(annotation, 'max_length_mm'):
        applied['max_length_mm'] = dict(value=float(annotation['max_length_mm']), source='explicit', where=ann)
    elif 'max_length_mm' in d:
        applied['max_length_mm'] = dict(value=d['max_length_mm']['value'], source='class')
    if 'max_length_mm' in applied:
        applied['max_length_mm'].update(class_derived=(d.get('max_length_mm') or {}).get('value'), enforced=False)
    rec['applied'] = applied
    pair['bus_class'] = rec
    return rec


# ------------------------------------------------------------------ stub delay (router side)

def stub_delay_limit(pair, env=None):
    """The pair's delay-kind stub limit (dict) when the flag is on, else None."""
    if not enabled(env):
        return None
    limit = pair.get('stub_limit')
    return limit if limit and limit.get('kind') == 'delay' else None


def stub_mm_limit(pair, env=None):
    """An explicit annotation mm stub cap (flag on), else None."""
    if not enabled(env):
        return None
    limit = pair.get('stub_limit')
    return float(limit['max_mm']) if limit and limit.get('kind') == 'mm' else None


def router_stub_rule(pair, env_cap_mm, env=None):
    """The stub rule the pair router applies: (search cap mm or None, delay limit or None, source).

    Precedence: ``PNR_PAIR_STUB_MAX_MM`` (``env_cap_mm``, explicit mm) > annotation
    ``stub_max_mm`` (kind 'mm') > annotation ``stub_delay_max_ps`` / class
    ``stub_k * t_rise_min`` (kind 'delay': searched with ``search_cap_mm``, checked as
    delay to the pad centre with the barrel). Flag off: only the env cap."""
    if env_cap_mm is not None:
        return float(env_cap_mm), None, 'PNR_PAIR_STUB_MAX_MM'
    mm = stub_mm_limit(pair, env)
    if mm is not None:
        return mm, None, 'annotation stub_max_mm'
    delay = stub_delay_limit(pair, env)
    if delay is not None:
        return float(delay['search_cap_mm']), delay, 'delay (%s)' % delay.get('source')
    return None, None, None


def path_delay(nodes, layer_name, td_ps_per_mm, barrel_ps, lead=None):
    """Delay (ps) and breakdown of a copper path.

    ``nodes``: (layer, x_nm, y_nm) from the start to the junction (as
    pnr.route.detail.coupled.branch_lengths returns). A hop between two nodes at the
    same xy on different layers is a via barrel (``barrel_ps`` each). ``lead``:
    optional (layer, mm) planar copper before the first node (the pad centre to the
    entry vertex). A layer without a delay uses the slowest known one (conservative).
    """
    worst = max(td_ps_per_mm.values())
    planar, barrels = {}, 0
    if lead and lead[1] > 0:
        planar[layer_name(lead[0])] = planar.get(layer_name(lead[0]), 0.0) + lead[1]
    for a, b in zip(nodes, nodes[1:]):
        if a[0] != b[0] and (a[1], a[2]) == (b[1], b[2]):
            barrels += 1
            continue
        name = layer_name(a[0])
        planar[name] = planar.get(name, 0.0) + math.hypot(b[1] - a[1], b[2] - a[2]) / 1e6
    ps = sum(mm * td_ps_per_mm.get(n, worst) for n, mm in planar.items()) + barrels * barrel_ps
    return ps, dict(planar_mm={n: _r(v) for n, v in sorted(planar.items())}, barrels=barrels)
