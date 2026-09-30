"""SI orchestration and the ``pnr-si-report-v1`` result (compile-time check, post-route report).

Entry points for integration (all no-ops/None unless the caller enables ``PNR_SI=1``):

* :func:`resolve_intents` - parse + resolve only (no simulation; Python 3.9-safe, usable
  in a pcbnew worker).
* :func:`compile_rules` - rules compile sites: parse + resolve ``@pnr-si`` bindings,
  run the design-time check on the ideal board and add ``si_intents`` / ``si_design``
  to a *copy* of the rules. Raises :class:`SIDesignError` (fails the compile) when a
  requirement fails without a covering ``@pnr-si-waiver`` or cannot be evaluated.
  With the flag off it returns the very same rules object (byte-identical output).
* :func:`design_check` - the check itself (ideal geometry, every corner).
* :func:`post_route_report` - per candidate: extract the routed copper, simulate,
  classify every failing deck as ``layout`` (the ideal board passes the same corner)
  or ``design`` (it fails too), errors as ``error``; writes ``si.json``.
* :func:`candidate_side_fields` - flag-gated one-call hook for candidate evaluation.
* :func:`side_fields` - the named evaluation.json side fields (the 6-value objective
  vector is never touched): ``si_layout_failures``, ``si_design_failures``,
  ``si_errors``, ``si_summary``. Of these only ``si_layout_failures`` counts against a
  candidate: with ``PNR_SI=1`` it is a rank key right after the legality terms
  (violations, opens and, with ``PNR_POWER_FIRST``, open hot loops) in
  :func:`pnr.mc.halving._rank_key` and :func:`pnr.hier.synth_native.rank_key`; a
  missing or ``None`` value (no report, analysis crashed) ranks as the worst, like
  ``hot_loops_open``. Design failures and errors never change a ranking.

Report layout: ``requirements[].results[]`` = one record per driver corner x cable
length x cable impedance with metrics, gate failures, report-only violations,
pass/fail, class, deck sha256, model sha256s, runtime and the stored waveform path.

Simulation cache: ``<cache>/sims/<key>.json`` holds the metrics of one deck. The key
covers everything the stored metrics depend on: the deck text + driver model (deck
sha256), the metric code version, the ngspice build, the measurement thresholds and
windows (VIH/VIL/rail/edge times, which are not in the deck text) and the sha256 of
every committed model file of the profile (receiver, cable, connector, parts).

Planes (post-route): the stackup's reference planes are the rules' net-class
``plane_layer`` values plus every copper layer the routed board actually pours: a
layer whose filled zone copper (any net) covers at least ``PLANE_COVERAGE`` of the
board outline counts as a plane (so an outer-layer GND pour is a reference too).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from pnr.si import SCHEMA, enabled
from pnr.si import annotations as ann
from pnr.si import deck as deckmod
from pnr.si import extract, metrics, models, physics, runner

METRICS_VERSION = 'pnr-si-metrics-v1'
MAX_WORKERS = 3            # threads per report; the machine-wide cap is pnr.si.runner.slot (PNR_SI_SLOTS)
PLANE_COVERAGE = 0.5


class SIDesignError(RuntimeError):
    """Design-time SI check failed (unwaived design failure or an evaluation error)."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def _workers(env, workers):
    w = int(workers or (env.get('PNR_SI_WORKERS') or MAX_WORKERS))
    return max(1, min(MAX_WORKERS, w))


_LIB_SHA = {}


def engine_info(env=None):
    env = os.environ if env is None else env
    try:
        lib, cm = runner.find_lib(env)
    except Exception as error:
        return dict(error=repr(error))
    if lib not in _LIB_SHA:
        _LIB_SHA[lib] = models.sha256_file(lib)
    return dict(ngspice_lib=lib, ngspice_lib_sha256=_LIB_SHA[lib], codemodels=cm, metrics=METRICS_VERSION,
                deck=deckmod.DECK_VERSION)


class _Sim:
    """Driver-model conversion, deck building, cached parallel simulation."""

    def __init__(self, library, st, *, env, workers, timeout, cache, work_dir, save_waves, series_ohm):
        self.lib, self.st, self.env = library, st, env
        self.workers, self.timeout = workers, timeout
        self.cache = Path(cache) if cache else models.cache_dir(env)
        self.work_dir = Path(work_dir) if work_dir else None
        self.save_waves = save_waves
        self.series_ohm = series_ohm
        self._drivers = {}
        self._lock = threading.Lock()
        # results depend on the simulator build too: its sha256 is part of the sim cache key
        self.engine = engine_info(env)
        self.engine_key = self.engine.get('ngspice_lib_sha256') or self.engine.get('error', '')

    def driver(self, intent, corner, window, td):
        key = (intent['driver']['model'], intent['driver']['pad'], corner, window, td)
        if key not in self._drivers:
            manifest = self.lib.get('driver', intent['driver']['model'])
            rail = manifest['corners'][corner].get('rail_v')
            self._drivers[key] = models.kibis_driver(manifest, intent['driver']['pad'], corner, window, td,
                                                     rail_v=manifest.get('rail_v'), cache=self.cache, env=self.env)
            if rail is not None and abs(self._drivers[key]['rail_v'] - rail) > 1e-6:
                raise models.ModelError('KIBIS %s corner rail %.3f V != manifest %.3f V' % (
                    corner, self._drivers[key]['rail_v'], rail))
        return self._drivers[key]

    def plan(self, intent, geometry, corners):
        """Deck jobs (dicts) for a corner list of ``(driver corner, cable_m[, cable_z0_ohm])``."""
        prof = self.lib.get('profile', intent['profile'])
        cable = self.lib.get('cable', prof['cable'])
        hashes = self.lib.hashes(intent['profile'])
        nominal = deckmod.nominal_z0(cable)
        jobs = []
        for case in corners:
            corner, cable_m = case[0], float(case[1])
            z0 = float(case[2]) if len(case) > 2 and case[2] is not None else nominal
            w = deckmod.window_ns(prof, cable, cable_m)
            td = prof['stimulus']['td_ns']
            drv = self.driver(intent, corner, w, td)
            template, meta = deckmod.build(intent, geometry, corner=corner, cable_m=cable_m, driver=drv,
                                           library=self.lib, st=self.st, window=w, td_ns=td, series_ohm=self.series_ohm,
                                           cable_z0=z0)
            dh = deckmod.deck_hash(template, drv['sha256'])
            jobs.append(dict(intent=intent['name'], geometry=geometry['kind'], corner=corner, cable_m=cable_m,
                             cable_z0_ohm=z0, z0_tag='' if abs(z0 - nominal) < 1e-9 else '-z%g' % z0,
                             template=template, meta=meta, driver=drv, deck_sha256=dh, limits=prof['limits'],
                             models=dict(hashes, driver_lib=drv['sha256'], vendor_ibis=drv['vendor_sha256'],
                                         driver_manifest=self.lib.entry('driver', intent['driver']['model'])['sha256'])))
        return jobs

    def _cache_path(self, job):
        key = sim_key(job, self.engine_key)
        return self.cache / 'sims' / (key[:2]) / (key + '.json')

    def _one(self, job):
        cp = self._cache_path(job)
        if cp.exists():
            try:
                rec = json.loads(cp.read_text())
                rec['cached'] = True
                return rec
            except Exception:
                pass
        meta = job['meta']
        rail = meta['rail_v']

        def check(t, v):
            return metrics.sanity(t, v['rx'], v['drv'], rail=rail, t_rise=meta['t_rise_s'], t_fall=meta['t_fall_s'],
                                  t_end=meta['t_end_s'])
        wd = None
        if self.work_dir:
            wd = self.work_dir / 'decks' / job['intent'] / _case_name(job)
        deck_text = deckmod.materialize(job['template'], job['driver']['path'])
        res = runner.run_deck(deck_text, meta['nodes'], tstop=meta['tstop_s'], timeout=self.timeout,
                              work_dir=str(wd) if wd else None, check=check, env=self.env)
        rec = dict(status='error', error=res.get('error'), attempts=res['attempts'], runtime_s=res['runtime_s'], cached=False)
        if res['status'] == 'ok':
            t, v = res['t'], res['v']
            m = metrics.edge_metrics(t, v['rx'], v['drv'], rail=rail, vih=meta['vih_v'], vil=meta['vil_v'],
                                     t_rise=meta['t_rise_s'], t_fall=meta['t_fall_s'], t_end=meta['t_end_s'])
            m['conn_vmax_v'] = max(v['conn'])
            m['conn_rise_10_90_ns'] = metrics.edge_metrics(t, v['conn'], v['drv'], rail=rail, vih=meta['vih_v'],
                                                           vil=meta['vil_v'], t_rise=meta['t_rise_s'], t_fall=meta['t_fall_s'],
                                                           t_end=meta['t_end_s'])['rise_10_90_ns']
            wt, wy = metrics.decimate_wave(t, {'drv': v['drv'], 'conn': v['conn'], 'rx': v['rx']})
            rec = dict(status='ok', metrics=m, attempts=res['attempts'], runtime_s=res['runtime_s'], points=len(t),
                       wave=dict(t=[round(x, 13) for x in wt], **{k: [round(x, 5) for x in y] for k, y in wy.items()}),
                       cached=False)
            cp.parent.mkdir(parents=True, exist_ok=True)
            tmp = cp.with_suffix('.%d.%d.tmp' % (os.getpid(), threading.get_ident()))
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, cp)
        return rec

    def run(self, jobs):
        out = [None] * len(jobs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(self._one, j): i for i, j in enumerate(jobs)}
            for f in concurrent.futures.as_completed(futs):
                i = futs[f]
                try:
                    out[i] = f.result()
                except Exception as error:
                    out[i] = dict(status='error', error='runner crashed: %r' % (error,), attempts=[], runtime_s=0.0, cached=False)
        return out


def sim_key(job, engine_key):
    """Simulation cache key: every input the cached metrics depend on (see the module doc)."""
    meta = job['meta']
    measure = {k: meta.get(k) for k in ('vih_v', 'vil_v', 'rail_v', 't_rise_s', 't_fall_s', 't_end_s', 'nodes')}
    blob = json.dumps(dict(deck=job['deck_sha256'], metrics=METRICS_VERSION, engine=engine_key, measure=measure,
                           models={k: v for k, v in sorted(job['models'].items())}), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _case_name(job):
    return '%s-%s-%gm%s' % (job['geometry'], job['corner'], job['cable_m'], job.get('z0_tag', ''))


def _corners(profile, z0_sweep=True):
    """``(driver corner, cable_m, cable_z0_ohm)`` of the profile's sweep.

    Lengths above 0 m also sweep ``cable_z0_ohm`` (nominal only with
    ``z0_sweep=False`` or when the profile lists none; ``None`` = the cable's nominal).
    """
    c = profile['corners']
    z0s = [float(z) for z in c.get('cable_z0_ohm') or []] or [None]
    out = []
    for d in c['driver']:
        for m in c['cable_m']:
            for z in (z0s if (z0_sweep and float(m) > 0) else z0s[:1]):
                out.append((d, float(m), z))
    return out


def _record(job, sim):
    rec = dict(corner=job['corner'], cable_m=job['cable_m'], cable_z0_ohm=job.get('cable_z0_ohm'),
               cable_r_ohm=job['meta'].get('cable_r_ohm'), geometry=job['geometry'], deck_sha256=job['deck_sha256'],
               models=job['models'], window_ns=job['meta']['window_ns'], rail_v=job['meta']['rail_v'],
               vih_v=round(job['meta']['vih_v'], 4), vil_v=round(job['meta']['vil_v'], 4),
               series=job['meta']['series'], runtime_s=sim.get('runtime_s'), attempts=len(sim.get('attempts') or []),
               sim_s=((sim.get('attempts') or [{}])[-1] or {}).get('run_s'), points=sim.get('points'),
               cached=sim.get('cached', False))
    if sim['status'] != 'ok':
        rec.update(status='error', cls='error', error=sim.get('error'))
        return rec
    judged = metrics.judge(sim['metrics'], job['limits'])
    rec.update(status='pass' if judged['passed'] else 'fail', metrics=_round(sim['metrics']),
               gate_failures=_round(judged['gate_failures']), report_violations=_round(judged['report_violations']),
               cls=None)
    return rec


def _round(x):
    if isinstance(x, float):
        return round(x, 5)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v) for v in x]
    return x


def _worst(results):
    cands = []
    for r in results:
        for g in (r.get('gate_failures') or []):
            cands.append((0, g.get('margin') if g.get('margin') is not None else -1e9, r, g))
        for g in (r.get('report_violations') or []):
            cands.append((1, g.get('margin') if g.get('margin') is not None else -1e9, r, g))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    _, _, r, g = cands[0]
    return dict(metric=g['metric'], corner=r['corner'], cable_m=r['cable_m'], cable_z0_ohm=r.get('cable_z0_ohm'),
                value=g['value'], limit=g['limit'], margin=g['margin'], gate=g['gate'])


def _summarise(req):
    res = req['results']
    classes = [r.get('cls') for r in res if r['status'] != 'pass']
    req['decks'] = len(res)
    req['errors'] = sum(r['status'] == 'error' for r in res)
    req['failures'] = sum(r['status'] == 'fail' for r in res)
    if any(c == 'layout' for c in classes):
        req['cause'] = 'layout'
    elif any(c == 'design' for c in classes):
        req['cause'] = 'design'
    elif req['errors']:
        req['cause'] = 'error'
    else:
        req['cause'] = None
    req['status'] = 'fail' if req['failures'] else ('error' if req['errors'] else 'pass')
    req['worst'] = _worst(res)
    return req


def _save_waves(waves_dir, req_name, job, sim):
    if not waves_dir or sim.get('status') != 'ok' or 'wave' not in sim:
        return None
    p = Path(waves_dir) / req_name / (_case_name(job) + '.json')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(sim['wave'], vih_v=job['meta']['vih_v'], vil_v=job['meta']['vil_v'],
                                 rail_v=job['meta']['rail_v'], t_rise_s=job['meta']['t_rise_s'], t_fall_s=job['meta']['t_fall_s'])))
    return str(p)


def _geometry_summary(geo):
    return [dict(net=l['net'], source=l['source'], target=l['target'], status=l['status'],
                 copper_mm=l.get('copper_mm', {}), length_mm=l.get('length_mm'), vias=l.get('vias'),
                 stubs_mm=round(sum(a.get('length_mm', 0) for a in l.get('attach', []) if a['kind'] == 'stub'), 4),
                 warnings=l.get('warnings', [])) for l in geo['legs']]


def _base(mode, env, overrides):
    return dict(schema=SCHEMA, mode=mode, created=time.strftime('%Y-%m-%dT%H:%M:%S'), engine=engine_info(env),
                overrides=overrides, requirements=[])


def _finish(report, t0):
    reqs = report['requirements']
    report['summary'] = dict(
        requirements=len(reqs), passed=sum(r['status'] == 'pass' for r in reqs),
        failed=sum(r['status'] == 'fail' for r in reqs),
        layout_failures=sum(r['cause'] == 'layout' for r in reqs),
        design_failures=sum(r['cause'] == 'design' for r in reqs),
        errors=sum(bool(r.get('errors')) or r['status'] == 'error' for r in reqs),
        decks=sum(r.get('decks', 0) for r in reqs),
        cached_decks=sum(bool(x.get('cached')) for r in reqs for x in r['results']),
        runtime_s=round(time.monotonic() - t0, 2))
    return report


def _estimate(sim, intent, geometry, cases, waves_dir=None):
    """Report-only simulation of one pre-layout placement estimate (never gates)."""
    if geometry.get('error'):
        return dict(kind=geometry.get('kind', 'estimate'), gates=False, status='error', error=geometry['error'],
                    failures=0, errors=1, worst=None, results=[])
    jobs = sim.plan(intent, geometry, cases)
    sims = sim.run(jobs)
    recs = []
    for job, s in zip(jobs, sims):
        rec = _record(job, s)
        rec['wave'] = _save_waves(waves_dir, intent['name'], job, s)
        recs.append(rec)
    fails = sum(r['status'] == 'fail' for r in recs)
    errs = sum(r['status'] == 'error' for r in recs)
    return dict(kind=geometry['kind'], detour=geometry.get('detour'), gates=False,
                geometry=_geometry_summary(geometry), status='fail' if fails else ('error' if errs else 'pass'),
                failures=fails, errors=errs, worst=_worst(recs), results=recs)


def _estimate_all(sim, library, intents, estimates, corners=None, waves_dir=None):
    """``{name: estimate section}`` for every intent with a placement estimate geometry."""
    out = {}
    for intent in intents:
        geo = (estimates or {}).get(intent['name'])
        if geo is None:
            continue
        try:
            prof = library.get('profile', intent['profile'])
            out[intent['name']] = _estimate(sim, intent, geo, corners or _corners(prof, z0_sweep=False), waves_dir)
        except Exception as error:
            out[intent['name']] = dict(kind=geo.get('kind', 'estimate'), gates=False, status='error', error=repr(error),
                                       failures=0, errors=1, worst=None, results=[])
    return out


def _estimate_summary(report):
    ests = [r['estimate'] for r in report['requirements'] if r.get('estimate')]
    return dict(requirements=len(ests), failed=sum(e['status'] == 'fail' for e in ests),
                errors=sum(e['status'] == 'error' for e in ests), gates=False)


def _fmt_limit(limit):
    return ' '.join('%s %s' % (k, v) for k, v in sorted((limit or {}).items())) or 'n/a'


def failure_message(report, path=None):
    """Human-readable reason a design check failed, with a waiver template."""
    lines = []
    unwaived = [(r, x) for r in report['requirements'] for x in r['results'] if x['status'] == 'fail' and not x.get('waived')]
    errors = [r for r in report['requirements'] if r['status'] == 'error' or r.get('errors')]
    if unwaived:
        names = sorted({r['name'] for r, _ in unwaived})
        lines.append('SI design check FAILED (PNR_SI=1): %d requirement(s) fail on the ideal (zero-length) board, '
                     'which no layout can fix: %s' % (len(names), ', '.join(names)))
        for name in names:
            cases = [(r, x) for r, x in unwaived if r['name'] == name]
            r, x = cases[0]
            g = sorted(x['gate_failures'], key=lambda g: g['margin'] if g.get('margin') is not None else -1e9)[0]
            val = g.get('value')
            lines.append('  %s [%s] %s:%s: %d failing deck(s), e.g. driver %s / cable %g m%s: %s = %s (limit %s)' % (
                name, r['profile'], r['source']['path'], r['source']['line'], len(cases), x['corner'], x['cable_m'],
                (' Z0 %g ohm' % x['cable_z0_ohm']) if x.get('cable_m') and x.get('cable_z0_ohm') else '',
                g['metric'], ('%.4g' % val) if isinstance(val, (int, float)) else val, _fmt_limit(g.get('limit'))))
    if errors:
        lines.append('SI design check could not evaluate %d requirement(s) (errors fail closed): %s' % (
            len(errors), '; '.join('%s: %s' % (r['name'], r.get('error') or next(
                (x.get('error') for x in r['results'] if x['status'] == 'error'), 'error')) for r in errors)))
    if path:
        lines.append('Report: %s' % path)
    if unwaived:
        r, x = unwaived[0]
        metrics_ = sorted({g['metric'] for rr, y in unwaived if rr['name'] == r['name'] for g in y['gate_failures']})
        cables = sorted({y['cable_m'] for rr, y in unwaived if rr['name'] == r['name']})
        lines.append('Fix the design (termination, driver, or the profile limits in hardware/pnr/si_models) or waive it '
                     'with one line in the .ato next to the @pnr-si binding, e.g.:')
        lines.append('  # @pnr-si-waiver ' + json.dumps(dict(name=r['name'], reason='<why this is acceptable>',
                                                              metrics=metrics_, cable_m=cables), separators=(',', ':')))
    return '\n'.join(lines)


def design_check(intents, library=None, st=None, *, rules=None, env=None, workers=None, timeout=None, cache=None,
                 work_dir=None, out_dir=None, raise_on_fail=True, series_ohm=None, corners=None, estimates=None,
                 save_waves=True, report_name='si-design.json'):
    """Simulate every intent on the ideal (zero-length) board at every corner.

    Returns a ``pnr-si-report-v1`` report (mode ``design``). Each failing deck is
    class ``design``; ``waived`` names the covering ``@pnr-si-waiver``. Raises
    :class:`SIDesignError` when ``raise_on_fail`` and any requirement has an unwaived
    failure or an error (fail closed); its message is :func:`failure_message`.

    ``estimates``: optional ``{name: geometry}`` pre-layout placement estimates
    (:func:`pnr.si.extract.estimate_geometries`); simulated at the same corners and
    stored as ``requirements[].estimate`` - report only, they never gate (a
    placement-dependent failure is a layout matter for the post-route report).
    ``out_dir``: writes ``<out_dir>/<report_name>`` (and waveforms under
    ``<out_dir>/si-design-waves`` when ``save_waves``).
    """
    env = os.environ if env is None else env
    t0 = time.monotonic()
    library = library or models.Library(models.models_dir(env))
    st = st or physics.stackup(rules)
    waves_dir = (Path(out_dir) / 'si-design-waves') if (out_dir and save_waves) else None
    sim = _Sim(library, st, env=env, workers=_workers(env, workers), timeout=timeout, cache=cache,
               work_dir=work_dir, save_waves=bool(waves_dir), series_ohm=series_ohm)
    report = _base('design', env, dict(series_ohm=series_ohm) if series_ohm is not None else {})
    report['reference'] = 'ideal board: zero-length legs, no vias, design shunts only'
    report['stackup'] = dict(name=st.get('name'), planes=st.get('planes'), fab_profile=st.get('fab_profile'))
    for intent in intents:
        req = dict(name=intent['name'], profile=intent['profile'], source=intent['source'],
                   geometry=_geometry_summary(extract.ideal_geometry(intent)), results=[], waived=[])
        try:
            prof = library.get('profile', intent['profile'])
            jobs = sim.plan(intent, extract.ideal_geometry(intent), corners or _corners(prof))
            sims = sim.run(jobs)
        except Exception as error:
            req.update(results=[], status='error', cause='error', error=repr(error), decks=0, errors=1, failures=0, worst=None)
            report['requirements'].append(req)
            continue
        for job, s in zip(jobs, sims):
            rec = _record(job, s)
            rec['wave'] = _save_waves(waves_dir, intent['name'], job, s)
            if rec['status'] == 'fail':
                rec['cls'] = 'design'
                w = ann.waived(intent, dict(corner=job['corner'], cable_m=job['cable_m'], cable_z0_ohm=job['cable_z0_ohm'],
                                            metrics=[g['metric'] for g in rec['gate_failures']]))
                if w:
                    rec['waived'] = dict(reason=w['reason'], source=w['source'])
                    req['waived'].append(dict(corner=job['corner'], cable_m=job['cable_m'], cable_z0_ohm=job['cable_z0_ohm']))
            req['results'].append(rec)
        report['requirements'].append(_summarise(req))
    if estimates:
        est = _estimate_all(sim, library, intents, estimates, corners,
                            (Path(out_dir) / 'si-estimate-waves') if (out_dir and save_waves) else None)
        for req in report['requirements']:
            if req['name'] in est:
                req['estimate'] = est[req['name']]
    _finish(report, t0)
    unwaived = [(r['name'], x['corner'], x['cable_m']) for r in report['requirements'] for x in r['results']
                if x['status'] == 'fail' and not x.get('waived')]
    errors = [r['name'] for r in report['requirements'] if r['status'] == 'error' or r.get('errors')]
    report['summary']['unwaived_failures'] = len(unwaived)
    if estimates:
        report['summary']['estimate'] = _estimate_summary(report)
    report['status'] = 'fail' if unwaived else ('error' if errors else ('waived' if report['summary']['failed'] else 'pass'))
    path = None
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        path = Path(out_dir) / report_name
        path.write_text(json.dumps(report, indent=1))
        report['path'] = str(path)
    if raise_on_fail and (unwaived or errors):
        raise SIDesignError(failure_message(report, path), report)
    return report


def design_summary(report):
    """The ``rules['si_design']`` record of a design-check report."""
    out = dict(status='pass' if not report['summary']['failed'] else 'waived', summary=report['summary'],
               waived=[dict(name=r['name'], cases=r['waived']) for r in report['requirements'] if r.get('waived')])
    if report.get('path'):
        out['report'] = report['path']
    return out


def resolve_intents(annotation_sources, components, *, env=None, library=None):
    """Parse + resolve ``@pnr-si`` / ``@pnr-si-waiver`` lines into ``si_intents`` (no simulation).

    Pure Python 3.9-compatible: usable inside a pcbnew worker (e.g. native_loop
    ``prepare``) where the graph is built; run :func:`design_check` from the pnr runtime.
    """
    library = library or models.Library(models.models_dir(env))
    reqs, waivers = ann.parse(annotation_sources)
    return ann.resolve(reqs, waivers, components, library, env=os.environ if env is None else env)


def compile_rules(rules, annotation_sources, components, *, env=None, check=True, library=None, **kw):
    """Rules compile hook. ``PNR_SI`` off: returns ``rules`` itself, untouched.

    On: returns a copy with ``si_intents`` (resolved bindings incl. waivers) and
    ``si_design`` (summary of the design-time check); raises :class:`SIDesignError`
    / :class:`pnr.si.annotations.AnnotationError` to fail the compile.
    """
    env = os.environ if env is None else env
    if not enabled(env):
        return rules
    library = library or models.Library(models.models_dir(env))
    intents = resolve_intents(annotation_sources, components, env=env, library=library)
    out = json.loads(json.dumps(rules))
    out['si_intents'] = intents
    if check:
        rep = design_check(intents, library, physics.stackup(rules), rules=rules, env=env, **kw)
        out['si_design'] = design_summary(rep)
    return out


def _dump_rules(path, rules):
    # the format of native_loop.save (prepare.json)
    Path(path).write_text(json.dumps(rules, indent=2) + '\n')


def gate_rules_file(path, *, out_dir=None, estimates=None, env=None, library=None, **kw):
    """Compile-time check at the native_loop ``prepare`` site (pnr runtime, ``PNR_SI=1``).

    ``path``: the rules the pcbnew ``prepare`` worker wrote with ``si_intents``
    (:func:`resolve_intents`). Runs :func:`design_check` on the ideal board (plus the
    report-only placement ``estimates`` file the worker wrote, if any), writes
    ``<out_dir>/si-design.json`` and adds ``si_design`` to the rules file. Raises
    :class:`SIDesignError` (clear message, see :func:`failure_message`) to fail the
    compile. ``PNR_SI`` off: no-op, returns None.
    """
    env = os.environ if env is None else env
    if not enabled(env):
        return None
    path = Path(path)
    rules = json.loads(path.read_text())
    if 'si_intents' not in rules:
        raise SIDesignError('SI design check: %s has no si_intents (the prepare worker ran without PNR_SI=1)' % path, None)
    est = None
    if estimates and Path(estimates).exists():
        est = json.loads(Path(estimates).read_text())
    rep = design_check(rules["si_intents"], library or models.Library(models.models_dir(env)), physics.stackup(rules), rules=rules, env=env,
                       out_dir=out_dir or path.parent, estimates=est, **kw)
    rules['si_design'] = design_summary(rep)
    _dump_rules(path, rules)
    return rep


def placement_estimate(rules, components, *, out_dir, env=None, library=None, report_name='si-design.json', **kw):
    """Report-only pre-layout estimate after placement (route/__main__), merged into si-design.json.

    Never raises: problems are recorded as the estimate's ``error``.
    """
    env = os.environ if env is None else env
    if not enabled(env) or not rules.get('si_intents'):
        return None
    try:
        library = library or models.Library(models.models_dir(env))
        st = physics.stackup(rules)
        est_geo = extract.estimate_geometries(rules['si_intents'], components, st=st)
        sim = _Sim(library, st, env=env, workers=_workers(env, kw.get('workers')), timeout=kw.get('timeout'),
                   cache=kw.get('cache'), work_dir=None, save_waves=True, series_ohm=kw.get('series_ohm'))
        est = _estimate_all(sim, library, rules['si_intents'], est_geo, kw.get('corners'),
                            Path(out_dir) / 'si-estimate-waves')
    except Exception as error:
        est = {i['name']: dict(kind='estimate', gates=False, status='error', error=repr(error), failures=0, errors=1,
                               worst=None, results=[]) for i in rules['si_intents']}
    path = Path(out_dir) / report_name
    if path.exists():
        report = json.loads(path.read_text())
        for req in report['requirements']:
            if req['name'] in est:
                req['estimate'] = est[req['name']]
        report['summary']['estimate'] = _estimate_summary(report)
        path.write_text(json.dumps(report, indent=1))
    return est


def post_route_report(board, rules, *, out_dir=None, intents=None, annotation_sources=None, components=None,
                      env=None, workers=None, timeout=None, cache=None, series_ohm=None, library=None,
                      dump=None, save_decks=False, corners=None, report_name='si.json', waves_dir=None):
    """Post-route SI report of one routed board; writes ``<out_dir>/<report_name>``.

    Waveforms go to ``waves_dir`` (default ``<out_dir>/waves``).

    Intents come from ``intents``, else ``rules['si_intents']``, else are resolved
    from ``annotation_sources`` + ``components``. ``series_ohm`` overrides every series
    resistor (what-if; recorded in ``overrides``). ``corners``: optional
    ``[(driver_corner, cable_m)]`` subset (default: the profile's full sweep).
    Never raises for simulation or extraction problems: they become ``error``
    results (fail closed).
    """
    env = os.environ if env is None else env
    t0 = time.monotonic()
    library = library or models.Library(models.models_dir(env))
    rules = rules if isinstance(rules, dict) else json.loads(Path(rules).read_text())
    if intents is None:
        intents = rules.get('si_intents')
    if intents is None:
        reqs, waivers = ann.parse(annotation_sources or [])
        intents = ann.resolve(reqs, waivers, components or [], library, env=env)
    report = _base('post-route', env, dict(series_ohm=series_ohm) if series_ohm is not None else {})
    report['board'] = str(board)
    t_dump = time.monotonic()
    try:
        if dump is None:
            dump = extract.read_board(board, sorted({n for i in intents for n in i['nets']}), env=env) if intents else {}
        report['board_sha256'] = dump.get('board_sha256')
        dump_error = None
    except Exception as error:
        dump_error = repr(error)
    report['extract_s'] = round(time.monotonic() - t_dump, 2)
    st = physics.stackup(rules)
    rule_planes = list(st.get('planes') or [])
    poured = board_planes(dump) if not dump_error else {}
    st['planes'] = sorted(set(rule_planes) | {l for l, c in poured.items() if c >= PLANE_COVERAGE},
                          key=lambda l: physics.copper_layers(st).index(l) if l in physics.copper_layers(st) else 99)
    work = Path(out_dir) if out_dir else None
    waves_dir = Path(waves_dir) if waves_dir else ((work / 'waves') if work else None)
    sim = _Sim(library, st, env=env, workers=_workers(env, workers), timeout=timeout, cache=cache,
               work_dir=(work / 'work') if (work and save_decks) else None, save_waves=bool(out_dir), series_ohm=series_ohm)
    report['stackup'] = dict(name=st.get('name'), planes=st.get('planes'), fab_profile=st.get('fab_profile'),
                             planes_from_rules=rule_planes, pour_coverage={k: round(v, 4) for k, v in sorted(poured.items())},
                             plane_coverage_min=PLANE_COVERAGE)
    if dump and dump.get('warnings'):
        report['warnings'] = list(dump['warnings'])
    for intent in intents:
        req = dict(name=intent['name'], profile=intent['profile'], source=intent['source'], results=[])
        if dump_error:
            req.update(status='error', cause='error', error='extraction: ' + dump_error, decks=0, errors=1, failures=0, worst=None)
            report['requirements'].append(req)
            continue
        try:
            prof = library.get('profile', intent['profile'])
            cases = corners or _corners(prof)
            geo = extract.routed_geometry(dump, intent, st)
            req['geometry'] = _geometry_summary(geo)
            if geo['status'] == 'open':
                opened = [l['net'] for l in geo['legs'] if l['status'] == 'open']
                req['results'] = [dict(corner=c[0], cable_m=c[1], cable_z0_ohm=c[2] if len(c) > 2 else None,
                                       geometry='routed', status='fail', cls='layout',
                                       gate_failures=[dict(metric='copper_path', value=None, limit={}, gate=True, ok=False,
                                                           margin=None, reason='no copper path on %s' % opened)])
                                  for c in cases]
                report['requirements'].append(_summarise(req))
                continue
            jobs = sim.plan(intent, geo, cases)
            sims = sim.run(jobs)
            recs = [_record(j, s) for j, s in zip(jobs, sims)]
            for j, s, r in zip(jobs, sims, recs):
                r['wave'] = _save_waves(waves_dir, intent['name'], j, s)
            failing = [i for i, r in enumerate(recs) if r['status'] == 'fail']
            if failing:
                ideal = extract.ideal_geometry(intent)
                ref_jobs = sim.plan(intent, ideal, [(recs[i]['corner'], recs[i]['cable_m'], recs[i]['cable_z0_ohm'])
                                                    for i in failing])
                ref_sims = sim.run(ref_jobs)
                for i, rj, rs in zip(failing, ref_jobs, ref_sims):
                    ref = _record(rj, rs)
                    recs[i]['reference'] = dict(status=ref['status'], deck_sha256=ref['deck_sha256'],
                                                gate_failures=ref.get('gate_failures'), error=ref.get('error'))
                    if ref['status'] == 'error':
                        recs[i]['cls'] = 'error'
                    elif ref['status'] == 'fail':
                        recs[i]['cls'] = 'design'
                    else:
                        recs[i]['cls'] = 'layout'
            req['results'] = recs
        except Exception as error:
            req.update(status='error', cause='error', error=repr(error), decks=0, errors=1, failures=0, worst=None)
            report['requirements'].append(req)
            continue
        report['requirements'].append(_summarise(req))
    _finish(report, t0)
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        p = Path(out_dir) / report_name
        p.write_text(json.dumps(report, indent=1))
        report['path'] = str(p)
    return report


def board_planes(dump):
    """``{copper layer: filled zone area / board outline area}`` from a board dump (0..1+)."""
    area = dump.get('board_area_mm2') or 0.0
    if area <= 0:
        return {}
    per = {}
    for z in dump.get('pours') or []:
        per[z['layer']] = per.get(z['layer'], 0.0) + float(z.get('filled_mm2') or 0.0)
    return {k: v / area for k, v in per.items()}


def candidate_side_fields(board, rules, *, out_dir, env=None, **kw):
    """One-call hook for candidate evaluation (``hot_loops_open`` precedent).

    ``PNR_SI`` off: ``{}`` - evaluation records stay byte-identical. On: runs
    :func:`post_route_report` into ``out_dir`` and returns :func:`side_fields`; never
    raises (a crash yields ``si_errors`` = 1, ``si_layout_failures`` = None).
    """
    env = os.environ if env is None else env
    if not enabled(env):
        return {}
    try:
        rep = post_route_report(board, rules, out_dir=out_dir, env=env, **kw)
        return side_fields(rep)
    except Exception as error:
        out = side_fields(None)
        out['si_error'] = repr(error)
        return out


SIDE_FIELDS = ('si_layout_failures', 'si_design_failures', 'si_errors', 'si_summary')


def side_fields(report, path=None):
    """Named evaluation.json side fields (the objective vector is unchanged).

    ``si_layout_failures`` (the only field that counts against a candidate: a rank
    key with ``PNR_SI=1``, see the module doc), ``si_design_failures``, ``si_errors``
    and ``si_summary`` (requirement counts, per-requirement status/cause/worst metric,
    report path, runtime).
    ``None`` report (analysis crashed before producing one): ``si_layout_failures``
    stays None, ``si_errors`` = 1 - never a pass.
    """
    if report is None:
        return dict(si_layout_failures=None, si_design_failures=None, si_errors=1,
                    si_summary=dict(requirements=None, report=str(path) if path else None))
    s = report['summary']
    summary = dict(requirements=s['requirements'], passed=s.get('passed'), failed=s.get('failed'),
                   decks=s.get('decks'), cached_decks=s.get('cached_decks'), runtime_s=s.get('runtime_s'),
                   report=str(path) if path else report.get('path'))
    reqs = report.get('requirements') or []
    if reqs:
        summary['by_requirement'] = {r['name']: dict(status=r.get('status'), cause=r.get('cause'), worst=r.get('worst'))
                                     for r in reqs}
    return dict(si_layout_failures=s['layout_failures'], si_design_failures=s['design_failures'], si_errors=s['errors'],
                si_summary=summary)
