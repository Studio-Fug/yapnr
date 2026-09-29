"""Read one evaluated round directory into placement feedback (JSON-safe).

A round directory is what :mod:`pnr.full_iteration` leaves behind: a block
instance dir (``<synth out>/<tid>/native/<tag>``) or a halving stage dir
(``cand/<id>/native``). Only these files are read:

* ``feedback.json``: ``targets`` (the connections still open after the final
  refill), ``native_opens`` and, with PNR_SHOVE=1, ``shove`` (its presence marks
  the router, its ``no_make_room`` events mark connections nudges could not
  open);
* ``evaluation.json``: the objective;
* ``evaluated-placed.json`` (else ``placed.json``): ref -> address, so
  connections are keyed by a caller key (block-local path for templates, ref at
  top level) and pool across layouts and instances;
* ``electrical/native-loop/progress.json`` and ``rules.json``: the native budget
  and fab profile, for the router key;
* ``electrical/coalesce.json``: the source tree that ran the evaluation, whose
  evaluation modules give the code part of the router key (:func:`observed_code`).

Per-part scores (``component_scores``, ``routing_failure_scores``) and static
blockers are copied only as diagnostics: the signal study found them dominated
by the block IC and moving their parts goes with more opens, so no move uses
them.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

POWER_MODES = ('power', 'plane')


def enabled() -> bool:
    return os.environ.get('PNR_FEEDBACK') == '1'


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def split_endpoint(endpoint):
    """'REF.pad' -> (REF, pad); refs never contain '.', pad names may not either."""
    ref, _, pad = str(endpoint).partition('.')
    return ref, pad


def conn_id(a, b):
    """Canonical id of the connection between pads ``a`` and ``b`` ((key, pad) each).

    Endpoints only: a pad has one net, so the pad pair names the connection, and
    template instances (whose flat net names differ) share ids.
    """
    return '|'.join(sorted('%s.%s' % tuple(e) for e in (a, b)))


def _ends(a, b):
    return sorted([list(a), list(b)], key=lambda e: '%s.%s' % tuple(e))


def read_round(round_dir, key=None):
    """Feedback record of one evaluated round, or ``{'missing': True, 'dir': ...}``.

    ``key(component dict) -> str`` names parts (default: the ref). Never raises
    on missing or unreadable files.
    """
    d = Path(round_dir)
    feedback = _load(d / 'feedback.json')
    if not isinstance(feedback, dict) or not isinstance(feedback.get('targets'), list):
        return dict(missing=True, dir=str(d))
    comps = (_load(d / 'evaluated-placed.json') or {}).get('components') or []
    pose_source = 'evaluated' if comps else None
    if not comps:
        comps = (_load(d / 'placed.json') or {}).get('components') or []
        pose_source = 'placed' if comps else None
    keyf = key or (lambda c: c['ref'])
    key_of = {}
    for c in comps:
        try:
            key_of[c['ref']] = keyf(c)
        except (KeyError, TypeError):
            continue

    def k(ref):
        return key_of.get(ref, ref)

    no_room = set()
    for e in (feedback.get('shove') or {}).get('events') or []:
        if e.get('status') == 'no_make_room':
            t = e.get('target') or {}
            no_room.add((t.get('net'), frozenset((t.get('source'), t.get('target')))))
    conns, same = {}, []
    for t in feedback['targets']:
        try:
            (ra, pa), (rb, pb) = split_endpoint(t['source']), split_endpoint(t['target'])
        except (KeyError, TypeError):
            continue
        a, b = (k(ra), pa), (k(rb), pb)
        cid = conn_id(a, b)
        if ra == rb:
            same.append(cid)
            continue
        if cid in conns:
            continue
        ends = _ends(a, b)
        conns[cid] = dict(id=cid, net=t.get('net'), a=ends[0], b=ends[1], mode=t.get('mode'),
                          distance=round(float(t.get('distance') or 0.0), 3),
                          no_room=(t.get('net'), frozenset((t.get('source'), t.get('target')))) in no_room)
    endpoints = {}
    for c in conns.values():
        for key_, _ in (c['a'], c['b']):
            endpoints[key_] = endpoints.get(key_, 0) + 1
    evaluation = _load(d / 'evaluation.json') or {}
    progress = _load(d / 'electrical' / 'native-loop' / 'progress.json') or {}
    rules = _load(d / 'rules.json') or {}
    scores = feedback.get('routing_failure_scores') or {}
    top = sorted(((k(r), round(float(s), 3)) for r, s in scores.items() if isinstance(s, (int, float))),
                 key=lambda x: (-x[1], x[0]))[:5]
    return dict(
        dir=str(d), opens=feedback.get('native_opens'), objective=evaluation.get('objective'),
        router='shove' if 'shove' in feedback else 'plain',
        budget_seconds=(progress.get('budgets') or {}).get('seconds'),
        fab_profile=rules.get('fab_profile', 'legacy') if rules else None,
        conns=[conns[c] for c in sorted(conns)],
        same_part=dict(count=len(same), ids=sorted(same)),
        endpoints={k_: endpoints[k_] for k_ in sorted(endpoints)},
        pose_source=pose_source,
        diag=dict(routing_failure_top=[list(x) for x in top]))


def merge(fbs):
    """One layout's feedback over its template instances (a connection fails in
    the layout when it fails in any instance). Missing instance feedback makes the
    merged record missing."""
    fbs = list(fbs)
    if not fbs or any(f.get('missing') for f in fbs):
        return dict(missing=True, dirs=[f.get('dir') for f in fbs])
    if len(fbs) == 1:
        return fbs[0]
    conns = {}
    for f in fbs:
        for c in f['conns']:
            if c['id'] not in conns:
                conns[c['id']] = dict(c)
            else:
                conns[c['id']]['no_room'] = conns[c['id']]['no_room'] or c['no_room']
    endpoints = {}
    for c in conns.values():
        for key_, _ in (c['a'], c['b']):
            endpoints[key_] = endpoints.get(key_, 0) + 1
    routers = sorted({f.get('router') for f in fbs})
    opens = [f.get('opens') for f in fbs]
    return dict(dirs=[f.get('dir') for f in fbs], opens=sum(opens) if all(o is not None for o in opens) else None,
                router=routers[0] if len(routers) == 1 else 'mixed',
                budget_seconds=fbs[0].get('budget_seconds'), fab_profile=fbs[0].get('fab_profile'),
                conns=[conns[c] for c in sorted(conns)],
                same_part=dict(count=sum(f['same_part']['count'] for f in fbs),
                               ids=sorted({i for f in fbs for i in f['same_part']['ids']})),
                endpoints={k_: endpoints[k_] for k_ in sorted(endpoints)},
                pose_source=fbs[0].get('pose_source'))


# ------------------------------------------------------------------ router keys

def file_sha(path, n=8):
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:n]
    except OSError:
        return None


# ---- code identity of the evaluation (router) code
#
# Two runs with the same router name, budget and flags can still route
# differently when the evaluation code differs (e.g. src10.frozen's shove capped
# a moved power line at min(.5, .05 * len); src10b/src11 restore the 0.15 mm
# floor). The code key is a hash of every module an evaluation can execute: the
# static import closure of the evaluation entry points (function-local imports,
# ``-m pnr.x`` subprocess targets and package ``__main__`` included), stopping at
# driver-only modules that launch evaluations but never run inside one. The shove
# package is part of the key only for the shove router (every import of it
# outside the package is gated on PNR_SHOVE=1).

PNR_ROOT = Path(__file__).resolve().parents[2]          # .../hardware/pnr of this tree
EVAL_ENTRIES = ('pnr.full_iteration', 'pnr.hier.native_block', 'pnr.hier.synth', 'pnr.hier.blocks')
DRIVER_MODULES = ('pnr.feedback', 'pnr.mc', 'pnr.hier.synth_native', 'pnr.hier.top')
_MODULE_RE = None


def _excluded(module, router):
    prefixes = DRIVER_MODULES + (('pnr.shove',) if router == 'plain' else ())
    return any(module == p or module.startswith(p + '.') for p in prefixes)


def eval_code_files(pnr_root, router):
    """{relative path: sha1} of the modules an evaluation under ``router`` can run."""
    import ast
    import re
    global _MODULE_RE
    _MODULE_RE = _MODULE_RE or re.compile(r'^pnr(\.[A-Za-z_]\w*)+$')
    root = Path(pnr_root)

    def path_of(module):
        p = root.joinpath(*module.split('.'))
        if (p / '__init__.py').exists():
            return p / '__init__.py'
        return p.with_suffix('.py') if p.with_suffix('.py').exists() else None

    todo, seen = list(EVAL_ENTRIES), {}
    while todo:
        m = todo.pop()
        if m in seen or _excluded(m, router):
            continue
        f = path_of(m)
        if f is None:
            continue
        seen[m] = f
        parts = m.split('.')
        todo += ['.'.join(parts[:i]) for i in range(1, len(parts))]
        is_pkg = f.name == '__init__.py'
        if is_pkg:
            todo.append(m + '.__main__')        # ``python -m <package>``
        pkg = m if is_pkg else m.rpartition('.')[0]
        try:
            tree = ast.parse(f.read_text())
        except (OSError, SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                todo += [a.name for a in n.names if a.name.startswith('pnr')]
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base = pkg.split('.')
                    base = base[:len(base) - (n.level - 1)]
                    mod = '.'.join(base + ([n.module] if n.module else []))
                else:
                    mod = n.module or ''
                if mod.startswith('pnr'):
                    todo.append(mod)
                    todo += [mod + '.' + a.name for a in n.names]
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and _MODULE_RE.match(n.value):
                todo.append(n.value)
    out = {}
    for f in seen.values():
        try:
            out[str(f.relative_to(root))] = hashlib.sha1(f.read_bytes()).hexdigest()
        except OSError:
            continue
    return {k: out[k] for k in sorted(out)}


def code_sha(files):
    return hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()[:10]


def code_key(pnr_root=None, router=None):
    """Code key of the evaluation code of a tree (default: this tree, this process's router)."""
    router = router or ('shove' if os.environ.get('PNR_SHOVE') == '1' else 'plain')
    return code_sha(eval_code_files(pnr_root or PNR_ROOT, router))


def evaluation_tree(round_dir):
    """Source tree (the dir holding hardware/) an evaluated round was produced by, or None.

    Both drivers pass ``--annotation-source <tree>/hardware/splanc_dev/elec/src/...``
    (the tree of the running code, not of the inputs); native_loop snapshots it
    with its origin (``source-inputs/origins.json``), the coalesce phase records
    it for templates with protected intents. ``fp-lib-table`` and ``rules.json``
    are not used: they come from the inputs directory."""
    import re
    pattern = re.compile(r'^(/.*?)/hardware/splanc_dev/elec/src/[^/]*\.ato$')
    d = Path(round_dir) / 'electrical'
    origins = _load(d / 'native-loop' / 'source-inputs' / 'origins.json')
    if isinstance(origins, list):
        trees = {m.group(1) for o in origins if isinstance(o, dict)
                 for m in [pattern.match(str(o.get('original') or ''))] if m}
        if len(trees) == 1:
            return Path(trees.pop())
    try:
        text = (d / 'coalesce.json').read_text()
    except OSError:
        return None
    trees = set(re.findall(r'"(/[^"]*?)/hardware/splanc_dev/elec/src/[^"/]*\.ato"', text))
    return Path(trees.pop()) if len(trees) == 1 else None


def _started(round_dir):
    d = Path(round_dir)
    try:
        st = d.stat()
    except OSError:
        return None
    start = getattr(st, 'st_birthtime', None)
    if start:
        return start
    try:
        return (d / 'evaluation.json').stat().st_mtime
    except OSError:
        return None


def observed_code(round_dir, router, stamped=None, cache=None):
    """dict(code, tree, files, reason) of the code that evaluated ``round_dir``.

    A code key stamped into the record at evaluation time wins. Otherwise the
    tree is read from the round (:func:`evaluation_tree`) and hashed now; if any
    of its evaluation modules is newer than the round's start the tree changed
    after the evaluation and the code is unknown (``code`` None). ``cache`` (a
    dict) reuses one tree's module hashes across rounds of one import."""
    if stamped:
        return dict(code=stamped, tree=None, files=None, reason='stamped')
    tree = evaluation_tree(round_dir)
    if tree is None:
        return dict(code=None, tree=None, files=None, reason='evaluation tree not recorded')
    root = tree / 'hardware' / 'pnr'
    if not (root / 'pnr').is_dir():
        return dict(code=None, tree=str(tree), files=None, reason='evaluation tree %s is gone' % tree)
    if cache is not None and (str(root), router) in cache:
        files = cache[str(root), router]
    else:
        files = eval_code_files(root, router)
        if cache is not None:
            cache[str(root), router] = files
    start = _started(round_dir)
    newer = []
    if start is not None:
        for rel in files:
            try:
                if (root / rel).stat().st_mtime > start + 1.0:
                    newer.append(rel)
            except OSError:
                newer.append(rel)
    if newer:
        return dict(code=None, tree=str(tree), files=files,
                    reason='%s changed after the evaluation (%s)' % (tree.name, ', '.join(newer[:4])))
    return dict(code=code_sha(files), tree=str(tree), files=files, reason='tree')


def code_diff(files_a, files_b):
    """Modules whose content differs between two eval_code_files maps."""
    if not files_a or not files_b:
        return []
    return sorted(k for k in set(files_a) | set(files_b) if files_a.get(k) != files_b.get(k))


def check_code(observed, current_code, current_files=None, policy='error'):
    """(errors, warnings, stale) for an import's code identity against this run's.

    ``policy`` 'error': a different or unknown code refuses the import; 'warn':
    imported as is, with a warning naming the differing modules; 'rebase': the
    import is kept only as a layout source and is re-evaluated under this code
    before any feedback generation (``stale`` True)."""
    if observed.get('code') == current_code:
        return [], [], False
    if observed.get('code') is None:
        why = 'code unknown (%s)' % observed.get('reason')
    else:
        diff = code_diff(observed.get('files'), current_files)
        why = 'code %s != this run %s%s' % (observed['code'], current_code,
                                             (' (differs: %s)' % ', '.join(diff[:6])) if diff else '')
    if policy == 'error':
        return [why], [], False
    if policy == 'warn':
        return [], [why], False
    return [], [why + ': re-evaluated under this code (rebase)'], True


def current_key(stage, budget_seconds, inputs=None):
    """Router key of evaluations this process will run (environment + CLI + evaluation code)."""
    router = 'shove' if os.environ.get('PNR_SHOVE') == '1' else 'plain'
    return dict(router=router, stage=stage,
                budget_seconds=float(budget_seconds) if budget_seconds is not None else None,
                power_first=os.environ.get('PNR_POWER_FIRST') == '1',
                fanout_reserve=os.environ.get('PNR_FANOUT_RESERVE') == '1',
                fab_profile=os.environ.get('PNR_FAB_PROFILE') or 'jlc-pofv',
                inputs=file_sha(Path(inputs) / 'source.kicad_pcb') if inputs else None,
                code=code_key(PNR_ROOT, router))


def key_string(key):
    return '|'.join(str(key.get(f)) for f in ('router', 'stage', 'budget_seconds', 'power_first',
                                                'fanout_reserve', 'fab_profile', 'inputs', 'code'))


def observed_key(fb, *, stage=None, power_first=None):
    """What a record's feedback says about the evaluation that produced it."""
    return dict(router=fb.get('router'), stage=stage,
                budget_seconds=float(fb['budget_seconds']) if fb.get('budget_seconds') is not None else None,
                power_first=power_first, fab_profile=fb.get('fab_profile'))


def check_import(observed, current, budget_policy='error'):
    """(errors, warnings) comparing an imported evaluation's key with this run's.

    Router, power-first and fab profile must match (failure statistics and flags
    depend on the router). A different native budget is an error unless
    ``budget_policy`` is 'warn'. Fields the import cannot show are warnings."""
    errors, warnings = [], []
    for field in ('router', 'power_first', 'fab_profile', 'stage'):
        o, c = observed.get(field), current.get(field)
        if o is None or c is None:
            if field != 'stage':
                warnings.append('%s unknown for the import' % field)
        elif o != c:
            errors.append('%s %r != this run %r' % (field, o, c))
    o, c = observed.get('budget_seconds'), current.get('budget_seconds')
    if o is None:
        warnings.append('native budget unknown for the import')
    elif c is not None and abs(o - c) > 1e-6:
        (errors if budget_policy == 'error' else warnings).append('budget %gs != this run %gs' % (o, c))
    return errors, warnings
