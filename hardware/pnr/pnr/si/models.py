"""SI model library: ``si_models/*.json`` manifests, vendor fetch-by-sha256 cache, KIBIS.

Committed models (profiles, receiver, cable, connector, driver manifests, the part
table) live in ``hardware/pnr/si_models`` (``PNR_SI_MODELS`` overrides). Vendor files
are never committed: :func:`fetch_vendor` downloads the pinned, versioned URL into
``<cache>/vendor/<zip sha256>/<member>`` and checks both the archive and the member
sha256 (``PNR_SI_CACHE``, default ``~/.cache/pnr-si``). ``PNR_SI_VENDOR_SEED`` (a
directory list, ``os.pathsep``-separated) is searched first for a file with the
pinned hash, so an offline machine can be seeded without network access.

IBIS -> SPICE uses KiCad's KIBIS through the headless CLI only: a generated one-symbol
test-bench schematic is exported with ``kicad-cli sch export netlist --format spice``
(``PNR_KICAD_CLI``; a path inside ``/Applications/KiCad`` is refused because that
bundle registers a Dock app) with ``KICAD_CACHE_HOME`` set to a per-job directory, and
the ``ibis/<ref>.cache`` model it writes is copied to ``<cache>/kibis/<key>/driver.lib``.
The key hashes every input (vendor member sha, component, pin, model, corner,
stimulus, KiCad version, test-bench, decimation and post-processing versions). The
PWL stimulus tables (``Vku``/``Vkd``, one point per 0.1 ns) are decimated to a fixed
tolerance so ngspice is not forced to a breakpoint every 100 ps.

Corners KIBIS does not apply are applied here (post-processing ``ccomp-v1``):

* ``C_comp``: KIBIS always writes the typ column (``CCPOMP``). The manifest's
  per-corner ``c_comp`` (TYP/MIN/MAX column of the IBIS ``[Model] C_comp`` row) is
  substituted into the generated subcircuit and recorded in the meta.
* Package parasitics: when the component's ``[Pin]`` row gives R/L/C for the pin,
  KIBIS uses those at every corner (IBIS semantics: ``[Pin]`` overrides
  ``[Package]``), so a manifest ``rpin``/``lpin``/``cpin`` other than TYP would be a
  silent no-op; it is rejected (:class:`ModelError`) instead.

Every conversion is written into a private staging directory and renamed into
place in one step, so concurrent converters of the same key never see a partial
``driver.lib``/``meta.json`` pair. ``kicad-cli`` runs niced with its own deadline
(:func:`pnr.si.runner.bounded_cmd`) and holds a machine-wide SI slot
(:func:`pnr.si.runner.slot`).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[2] / 'si_models'
HEADLESS_CLI = Path.home() / 'Applications/KiCad-headless.app/Contents/MacOS/kicad-cli'
GUI_BUNDLE = '/Applications/KiCad/'
TB_VERSION = 'kibis-tb-v1'
POSTPROC_VERSION = 'ccomp-v1'
CORNER_VALUES = ('TYP', 'MIN', 'MAX')
PWL_TOL = 1e-9            # collinear points only: lossless (validated on p027: identical metrics and step counts at 0 and 2e-4)
DECIMATION_VERSION = 'swing-door-v1'
KINDS = ('profile', 'receiver', 'cable', 'connector', 'driver')


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def models_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get('PNR_SI_MODELS') or MODELS_DIR)


def cache_dir(env=None):
    env = os.environ if env is None else env
    p = Path(env.get('PNR_SI_CACHE') or (Path.home() / '.cache/pnr-si'))
    p.mkdir(parents=True, exist_ok=True)
    return p


class ModelError(RuntimeError):
    """A model, manifest or vendor file is missing or does not match its pinned hash."""


class Library:
    """Index of the committed ``si_models`` JSON files (id -> data, path, sha256)."""

    def __init__(self, root=None):
        self.root = Path(root) if root else models_dir()
        self._by = {}
        self.parts = None
        self.parts_sha = None
        if not self.root.is_dir():
            raise ModelError('SI model directory missing: %s' % self.root)
        for path in sorted(self.root.glob('*.json')):
            raw = path.read_bytes()
            data = json.loads(raw)
            schema = data.get('schema', '')
            if schema == 'pnr-si-parts-v1':
                self.parts, self.parts_sha = data, sha256_bytes(raw)
                self.parts_path = path
                continue
            m = re.match(r'pnr-si-(\w+)-v1$', schema)
            if not m or m.group(1) not in KINDS:
                raise ModelError('unknown SI model schema %r in %s' % (schema, path))
            key = (m.group(1), data['id'])
            if key in self._by:
                raise ModelError('duplicate SI model %s/%s' % key)
            self._by[key] = dict(data=data, path=str(path), sha256=sha256_bytes(raw))
        if self.parts is None:
            raise ModelError('si_models/parts.json missing')

    def get(self, kind, ident):
        try:
            return self._by[(kind, ident)]['data']
        except KeyError:
            raise ModelError('no SI %s model %r in %s' % (kind, ident, self.root)) from None

    def entry(self, kind, ident):
        self.get(kind, ident)
        return self._by[(kind, ident)]

    def ids(self, kind):
        return sorted(i for k, i in self._by if k == kind)

    def part(self, footprint_or_part):
        """Part table entry for a graph footprint ``'<PartDir>:<fp>'`` or a part dir name."""
        name = footprint_or_part.split(':', 1)[0]
        entry = self.parts['parts'].get(name)
        if entry is None:
            decoded = decode_resistor(name)
            if decoded:
                return dict(decoded, derived=True)
        return entry

    def default_pad_pf(self):
        return float(self.parts.get('default_pad_pf', 0.3))

    def hashes(self, profile_id):
        """sha256 of every committed file a profile's decks depend on."""
        prof = self.get('profile', profile_id)
        out = {'profile': self.entry('profile', profile_id)['sha256'], 'parts': self.parts_sha}
        for kind in ('receiver', 'cable', 'connector'):
            out[kind] = self.entry(kind, prof[kind])['sha256']
        return out


def decode_resistor(part):
    """Value of well-known 0402 resistor part numbers (UNI-ROYAL 0402WGFxxxxTCE, YAGEO RC0402FR-07xxxRL).

    Only used when a part is not in ``parts.json``; the result is marked ``derived``.
    """
    m = re.match(r'UNI_ROYAL_0402WGF(\d{3})(\d)TCE$', part)
    if m:
        return dict(kind='R', ohm=float(int(m.group(1)) * 10 ** int(m.group(2))), esl_nh=0.4,
                    source='decoded from UNI-ROYAL part number %s' % part)
    m = re.match(r'YAGEO_RC0402FR_07(\d+)R(\d*)L$', part)
    if m:
        whole, frac = m.group(1), m.group(2)
        return dict(kind='R', ohm=float(whole + ('.' + frac if frac else '')), esl_nh=0.4,
                    source='decoded from YAGEO part number %s' % part)
    return None


# ------------------------------------------------------------------ vendor files

MAX_DOWNLOAD_BYTES = 64 << 20


def _download(url, timeout):
    """GET ``url`` within ``timeout`` seconds of wall clock in total (not per socket op).

    The socket timeout bounds each connect/read; the total deadline is checked
    between 64 KiB reads, so a slow-drip server cannot stretch the fetch past
    ``timeout`` + one socket timeout. Bodies over 64 MiB are refused.
    """
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (pnr-si model fetch)'})
    chunks, size = [], 0
    with urllib.request.urlopen(req, timeout=min(timeout, 30.0)) as r:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('download of %s exceeded %.0f s' % (url, timeout))
            block = r.read(1 << 16)
            if not block:
                break
            size += len(block)
            if size > MAX_DOWNLOAD_BYTES:
                raise ValueError('download of %s exceeds %d bytes' % (url, MAX_DOWNLOAD_BYTES))
            chunks.append(block)
    return b''.join(chunks)


def _atomic_write(path, data):
    """Write ``data`` (bytes or str) to ``path`` through a unique temp file + rename."""
    path = Path(path)
    tmp = path.with_name('.%s.%d.%d.tmp' % (path.name, os.getpid(), threading.get_ident()))
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def fetch_vendor(entry, *, cache=None, timeout=60.0, env=None, download=_download):
    """Path of the pinned vendor member in the cache; fetch and verify it if needed.

    ``entry``: a ``vendor_files`` item (url, zip_sha256, member, member_sha256, license).
    Raises :class:`ModelError` on any hash mismatch or when no source is reachable.
    """
    env = os.environ if env is None else env
    cache = Path(cache) if cache else cache_dir(env)
    zsha, member, msha = entry['zip_sha256'], entry['member'], entry['member_sha256']
    dest = cache / 'vendor' / zsha / member
    if dest.exists() and sha256_file(dest) == msha:
        return dest
    blob = None
    for seed in [s for s in (env.get('PNR_SI_VENDOR_SEED') or '').split(os.pathsep) if s]:
        for cand in sorted(Path(seed).rglob('*')):
            if not cand.is_file() or cand.stat().st_size > 64 << 20:
                continue
            h = sha256_file(cand)
            if h == msha:
                blob = ('member', cand.read_bytes())
                break
            if h == zsha:
                blob = ('zip', cand.read_bytes())
                break
        if blob:
            break
    if blob is None:
        try:
            blob = ('zip', download(entry['url'], timeout))
        except Exception as error:
            raise ModelError('cannot fetch %s: %r' % (entry['url'], error)) from None
    kind, data = blob
    if kind == 'zip':
        if sha256_bytes(data) != zsha:
            raise ModelError('vendor archive %s: sha256 %s != pinned %s' % (entry['url'], sha256_bytes(data), zsha))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(member)
    if sha256_bytes(data) != msha:
        raise ModelError('vendor member %s: sha256 %s != pinned %s' % (member, sha256_bytes(data), msha))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest, data)
    _atomic_write(dest.parent / 'SOURCE.json', json.dumps(dict(
        url=entry['url'], zip_sha256=zsha, member=member, member_sha256=msha,
        license=entry.get('license'), fetched=time.strftime('%Y-%m-%dT%H:%M:%S')), indent=1))
    return dest


# ------------------------------------------------------------------ kicad-cli

def kicad_cli(env=None):
    """The headless kicad-cli (``PNR_KICAD_CLI``); never the GUI bundle."""
    env = os.environ if env is None else env
    cli = env.get('PNR_KICAD_CLI') or str(HEADLESS_CLI)
    if os.path.realpath(cli).startswith(GUI_BUNDLE) or cli.startswith(GUI_BUNDLE):
        raise ModelError('refusing kicad-cli inside %s (GUI bundle registers a Dock app); '
                         'set PNR_KICAD_CLI to the headless copy' % GUI_BUNDLE)
    if not os.access(cli, os.X_OK):
        raise ModelError('kicad-cli not found/executable: %s' % cli)
    return cli


_VERSION = {}
_LOCK = threading.Lock()


def kicad_version(cli, timeout=60.0):
    from pnr.proc import run
    from pnr.si.runner import bounded_cmd
    with _LOCK:
        if cli in _VERSION:
            return _VERSION[cli]
    with tempfile.TemporaryFile() as out:
        code = run(bounded_cmd([cli, 'version'], timeout + 5.0), timeout=timeout, stdout=out, stderr=out)
        out.seek(0)
        text = out.read().decode(errors='replace').strip()
    if code != 0:
        raise ModelError('kicad-cli version failed (%s): %s' % (code, text[-200:]))
    with _LOCK:
        _VERSION[cli] = text.splitlines()[-1] if text else 'unknown'
    return _VERSION[cli]


def _prop(k, v, x, y, hide=True):
    return '(property "%s" "%s" (at %g %g 0) (effects (font (size 1.27 1.27))%s))' % (
        k, v, x, y, ' (hide yes)' if hide else '')


def testbench_sch(fields, seed):
    """One IBIS symbol (pin 1 = GND on net ``0``, pin 2 = OUT) as a KiCad schematic.

    ``fields``: Sim.* properties. UUIDs derive from ``seed`` so the text is deterministic.
    """
    ns = uuid.UUID('6f1c2b1e-5b7a-4e21-9a55-5e3e1f0c9a11')
    n = [0]

    def U():
        n[0] += 1
        return str(uuid.uuid5(ns, '%s/%d' % (seed, n[0])))
    root = U()
    font = '(effects (font (size 1.27 1.27)))'
    pins = ' '.join('(pin passive line (at %g 0 %d) (length 2.54) (name "%s" %s) (number "%s" %s))' % (
        x, a, nm, font, k, font) for k, nm, x, a in (('1', 'A', -5.08, 0), ('2', 'B', 5.08, 180)))
    libsym = ('(symbol "tb:IBIS2" (pin_names (offset 0)) (exclude_from_sim no) (in_bom yes) (on_board yes) '
              + ' '.join([_prop('Reference', 'U', 0, 2.54, False), _prop('Value', 'IBIS2', 0, -2.54, False),
                          _prop('Footprint', '', 0, 0), _prop('Datasheet', '', 0, 0), _prop('Description', '', 0, 0)])
              + ' (symbol "IBIS2_0_1" (rectangle (start -2.54 1.27) (end 2.54 -1.27) (stroke (width 0) (type default)) (fill (type none))))'
              + ' (symbol "IBIS2_1_1" %s))' % pins)
    x, y = 50, 50
    extra = ' '.join(_prop(k, v, x, y + 5) for k, v in fields.items())
    sym = ('(symbol (lib_id "tb:IBIS2") (at %g %g 0) (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) '
           '(uuid "%s") %s %s %s %s %s %s (pin "1" (uuid "%s")) (pin "2" (uuid "%s")) '
           '(instances (project "tb" (path "/%s" (reference "U1") (unit 1)))))') % (
        x, y, U(), _prop('Reference', 'U1', x, y - 3, False), _prop('Value', 'X', x, y + 3, False),
        _prop('Footprint', '', x, y), _prop('Datasheet', '', x, y), _prop('Description', '', x, y), extra, U(), U(), root)
    labels = ' '.join('(label "%s" (at %g %g 0) (fields_autoplaced yes) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "%s"))' % (
        net, xx, y, U()) for net, xx in (('0', x - 5.08), ('drv', x + 5.08)))
    return ('(kicad_sch (version 20231120) (generator "eeschema") (generator_version "8.0") (uuid "%s") (paper "A4") '
            '(lib_symbols %s) %s %s (sheet_instances (path "/" (page "1"))))\n') % (root, libsym, sym, labels)


def _fmt_ns(v):
    return ('%.6g' % v) + 'n'


_IBIS_SCALE = {'T': 1e12, 'G': 1e9, 'M': 1e6, 'k': 1e3, 'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15}
_IBIS_NUM = re.compile(r'^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([TGMkmunpf]?)')


def ibis_number(token):
    """IBIS number with an optional scale suffix (``6.40pF`` -> 6.4e-12); None for NA."""
    m = _IBIS_NUM.match(token.strip())
    if not m:
        return None
    return float(m.group(1)) * _IBIS_SCALE.get(m.group(2), 1.0)


def _ibis_section(text, keyword, name):
    """Lines of the ``[keyword] name`` section up to the next keyword of the same kind."""
    lines = text.splitlines()
    head = re.compile(r'^\[%s\]\s+(\S+)' % re.escape(keyword), re.I)
    out, inside = [], False
    for line in lines:
        m = head.match(line)
        if m:
            if inside:
                break
            inside = m.group(1).lower() == name.lower()
            continue
        if inside:
            out.append(line)
    return out


def ibis_pin_parasitics(text, component, pin):
    """``(R, L, C)`` of ``pin`` from the component's ``[Pin]`` table, or None when not given."""
    rows = _ibis_section(text, 'Component', component)
    inside = False
    for line in rows:
        if re.match(r'^\[Pin\]', line, re.I):
            inside = True
            continue
        if inside and line.startswith('['):
            break
        if not inside or not line.strip() or line.lstrip().startswith('|'):
            continue
        cols = line.split()
        if cols[0] == str(pin):
            vals = [ibis_number(c) for c in cols[3:6]]
            return tuple(vals) if len(vals) == 3 and all(v is not None for v in vals) else None
    return None


def ibis_c_comp(text, model):
    """``{'TYP': F, 'MIN': F, 'MAX': F}`` from the ``[Model]`` section's ``C_comp`` row."""
    for line in _ibis_section(text, 'Model', model):
        cols = line.split()
        if cols and cols[0].lower() == 'c_comp':
            vals = [ibis_number(c) for c in cols[1:4]]
            typ = vals[0]
            if typ is None:
                break
            return {'TYP': typ, 'MIN': vals[1] if vals[1] is not None else typ, 'MAX': vals[2] if vals[2] is not None else typ}
    raise ModelError('IBIS model %s has no C_comp row' % model)


_CCOMP_LINE = re.compile(r'^(CCPOMP\s+\S+\s+\S+\s+)(\S+)(.*)$', re.M)


def apply_c_comp(model_text, farad):
    """Replace the KIBIS die capacitance (``CCPOMP``, always the typ column) with ``farad``."""
    lines = _CCOMP_LINE.findall(model_text)
    if len(lines) != 1:
        raise ModelError('KIBIS model has %d CCPOMP lines (expected 1); C_comp corner not applicable' % len(lines))
    return _CCOMP_LINE.sub(lambda m: m.group(1) + ('%.6e' % farad) + m.group(3), model_text, count=1)


def decimate(ts, vs, tol):
    """Indices of a PWL subset whose linear interpolation stays within ``tol`` of every point.

    Greedy swing-door: from each kept anchor, extend the segment while one slope still
    passes within ``tol`` of all intermediate points; the first and last points are kept.
    """
    n = len(ts)
    if n <= 2:
        return list(range(n))
    keep = [0]
    a = 0
    while a < n - 1:
        lo, hi = float('-inf'), float('inf')
        best = a + 1
        for k in range(a + 1, n):
            dt = ts[k] - ts[a]
            if dt <= 0:
                break
            s = (vs[k] - vs[a]) / dt
            if lo <= s <= hi:
                best = k
            lo = max(lo, (vs[k] - tol - vs[a]) / dt)
            hi = min(hi, (vs[k] + tol - vs[a]) / dt)
            if lo > hi:
                break
        keep.append(best)
        a = best
    return keep


_PWL = re.compile(r'^(V\w+\s+\S+\s+\S+\s+pwl\s*\()\s*([^)]*)\)\s*$', re.I)


def decimate_model(text, tol=PWL_TOL):
    """Decimate the independent-source PWL lines of a KIBIS model. Returns (text, stats)."""
    out, stats = [], []
    for line in text.splitlines():
        m = _PWL.match(line)
        if not m:
            out.append(line)
            continue
        vals = [float(x) for x in m.group(2).split()]
        ts, vs = vals[0::2], vals[1::2]
        idx = decimate(ts, vs, tol)
        stats.append(dict(source=line.split()[0], before=len(ts), after=len(idx)))
        out.append(m.group(1) + ' ' + ' '.join('%.9e %.9e' % (ts[i], vs[i]) for i in idx) + ' )')
    return '\n'.join(out) + '\n', stats


def kibis_driver(manifest, pin, corner, window_ns, td_ns, *, rail_v=None, cache=None, env=None,
                 timeout=120.0, fetch=fetch_vendor):
    """KIBIS RECTDRIVER model for one driver corner and stimulus; cached by input hash.

    Returns dict(path, sha256, subckt, pins, rail_v, key, cached, vendor_sha256, kicad,
    decimation). The stimulus is one pulse: low until ``td_ns``, high for ``window_ns``,
    low for ``window_ns`` (the rising and the falling edge window).
    """
    env = os.environ if env is None else env
    cache = Path(cache) if cache else cache_dir(env)
    pins = manifest['pins']
    if str(pin) not in pins:
        raise ModelError('driver %s has no pin %s' % (manifest['id'], pin))
    rail = rail_v if rail_v is not None else manifest.get('rail_v', 5.0)
    by_rail = pins[str(pin)]['by_rail']
    model = by_rail.get('%.1f' % float(rail))
    if not model:
        raise ModelError('driver %s pin %s has no IBIS model for a %.1f V rail' % (manifest['id'], pin, rail))
    cspec = dict(manifest['corners'][corner])
    cspec.setdefault('c_comp', 'TYP')
    bad = {k: cspec.get(k) for k in ('vcc', 'rpin', 'lpin', 'cpin', 'c_comp') if cspec.get(k) not in CORNER_VALUES}
    if bad:
        # KIBIS maps anything but the exact strings MIN/MAX to TYP without a warning.
        raise ModelError('driver %s corner %s: KIBIS corner values must be TYP/MIN/MAX, got %s' % (manifest['id'], corner, bad))
    if window_ns < float(manifest.get('kibis', {}).get('min_window_ns', 30.0)):
        raise ModelError('KIBIS window %.1f ns shorter than the IBIS waveform tables' % window_ns)
    vendor = next(v for v in manifest['vendor_files'] if v['id'] == 'ibis')
    cli = kicad_cli(env)
    kver = kicad_version(cli)
    params = 'vcc=%s rpin=%s lpin=%s cpin=%s ton=%s toff=%s td=%s n=1' % (
        cspec['vcc'], cspec['rpin'], cspec['lpin'], cspec['cpin'], _fmt_ns(window_ns), _fmt_ns(window_ns), _fmt_ns(td_ns))
    fields = {'Sim.Library': vendor['member'], 'Sim.Name': manifest['ibis_component'], 'Sim.Device': 'IBIS',
              'Sim.Type': manifest.get('kibis', {}).get('type', 'RECTDRIVER'), 'Sim.Ibis.Pin': str(pin),
              'Sim.Ibis.Model': model, 'Sim.Pins': '1=GND 2=IN/OUT', 'Sim.Params': params}
    key = sha256_bytes(json.dumps(dict(vendor=vendor['member_sha256'], fields=fields, kicad=kver, tb=TB_VERSION,
                                       decimation=[DECIMATION_VERSION, PWL_TOL], c_comp=cspec['c_comp'],
                                       postproc=POSTPROC_VERSION), sort_keys=True).encode())[:24]
    out_dir = cache / 'kibis' / key
    lib, meta_path = out_dir / 'driver.lib', out_dir / 'meta.json'
    cached = _read_kibis(lib, meta_path)
    if cached:
        return cached
    ibs = fetch(vendor, cache=cache, env=env)
    ibs_text = Path(ibs).read_text(errors='replace')
    pin_rlc = ibis_pin_parasitics(ibs_text, manifest['ibis_component'], pin)
    if pin_rlc and any(cspec[k] != 'TYP' for k in ('rpin', 'lpin', 'cpin')):
        raise ModelError('driver %s corner %s: rpin/lpin/cpin=%s would be ignored: the IBIS [Pin] row of pin %s gives '
                         'R/L/C %s, which KIBIS applies at every corner ([Pin] overrides [Package]); set them to TYP' % (
                             manifest['id'], corner, [cspec[k] for k in ('rpin', 'lpin', 'cpin')], pin, pin_rlc))
    c_comp = ibis_c_comp(ibs_text, model)
    from pnr.proc import run
    from pnr.si.runner import bounded_cmd, slot
    with tempfile.TemporaryDirectory(prefix='pnr-si-kibis-') as tmp:
        tmp = Path(tmp)
        shutil.copy2(ibs, tmp / vendor['member'])
        (tmp / 'tb.kicad_sch').write_text(testbench_sch(fields, key))
        (tmp / 'tb.kicad_pro').write_text('{"meta": {"filename": "tb.kicad_pro", "version": 1}}')
        job_env = dict(os.environ, KICAD_CACHE_HOME=str(tmp / 'cache'), LC_ALL='C', LANG='C')
        cmd = bounded_cmd([cli, 'sch', 'export', 'netlist', '--format', 'spice', '-o', str(tmp / 'tb.cir'),
                           str(tmp / 'tb.kicad_sch')], timeout + 5.0, env)
        with slot(env, what='KIBIS export'):
            t0 = time.monotonic()
            with open(tmp / 'cli.log', 'wb') as log:
                code = run(cmd, timeout=timeout, env=job_env, stdout=log, stderr=log, cwd=str(tmp))
            seconds = time.monotonic() - t0
        found = sorted((tmp / 'cache').rglob('ibis/*.cache'))
        log_text = (tmp / 'cli.log').read_text(errors='replace')[-600:]
        if code != 0 or not found:
            raise ModelError('KIBIS export failed (exit %s, %.1fs): %s' % (code, seconds, log_text))
        raw = found[0].read_text(errors='replace')
    m = re.search(r'^\.SUBCKT\s+(\S+)\s+(.+)$', raw, re.M | re.I)
    vp = re.search(r'^VPWR\s+POWER\s+GND\s+(\S+)', raw, re.M | re.I)
    if not m or not vp or '.ends' not in raw.lower():
        raise ModelError('KIBIS output has no subcircuit/VPWR (KiCad %s)' % kver)
    kibis_ccomp = re.search(r'^CCPOMP\s+\S+\s+\S+\s+(\S+)', raw, re.M)
    text, stats = decimate_model(apply_c_comp(raw, c_comp[cspec['c_comp']]))
    meta = dict(key=key, raw_sha256=sha256_bytes(raw.encode()), subckt=m.group(1),
                pins=m.group(2).split(), rail_v=float(vp.group(1)), model=model, corner=corner, params=params,
                window_ns=window_ns, td_ns=td_ns, vendor_sha256=vendor['member_sha256'], vendor_url=vendor['url'],
                kicad=kver, export_s=round(seconds, 2), decimation=dict(version=DECIMATION_VERSION, tol=PWL_TOL, lines=stats),
                c_comp=dict(column=cspec['c_comp'], farad=c_comp[cspec['c_comp']], ibis=c_comp,
                            kibis_farad=float(kibis_ccomp.group(1)) if kibis_ccomp else None, postproc=POSTPROC_VERSION),
                pin_parasitics=dict(source='IBIS [Pin] row (every corner)', r_l_c=list(pin_rlc)) if pin_rlc else
                dict(source='IBIS [Package] (%s/%s/%s)' % (cspec['rpin'], cspec['lpin'], cspec['cpin'])))
    return _publish_kibis(out_dir, text, meta)


def _read_kibis(lib, meta_path):
    """A complete cached conversion (meta sha256 matches the model file), else None."""
    if lib.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            return None
        if meta.get('sha256') == sha256_file(lib):
            return dict(meta, path=str(lib), cached=True)
    return None


def _publish_kibis(out_dir, text, meta):
    """Stage ``driver.lib`` + ``meta.json`` privately, then rename the directory into place.

    A concurrent converter that got there first wins; its (complete) result is used.
    """
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.%s.' % out_dir.name, dir=str(out_dir.parent)))
    try:
        (stage / 'driver.lib').write_text(text)
        meta = dict(meta, sha256=sha256_file(stage / 'driver.lib'))
        (stage / 'meta.json').write_text(json.dumps(meta, indent=1))
        try:
            os.rename(stage, out_dir)
        except OSError:
            # out_dir exists: another process published first, or a stale/partial legacy dir
            done = _read_kibis(out_dir / 'driver.lib', out_dir / 'meta.json')
            if done:
                return dict(done, cached=False)
            broken = out_dir.with_name('.%s.broken.%d.%d' % (out_dir.name, os.getpid(), threading.get_ident()))
            try:
                os.rename(out_dir, broken)
                shutil.rmtree(broken, ignore_errors=True)
            except OSError:
                pass
            os.rename(stage, out_dir)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return dict(meta, path=str(out_dir / 'driver.lib'), cached=False)
