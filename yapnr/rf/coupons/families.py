"""Line families (cross-sections) and their 2D surrogate tables (design §4.3, §8.5, §11.1).

A family is one cross-section: layer, type, drawn width, gap, mask. Its quasi-static quantities
depend on a handful of fab parameters (heights, Dk, etch, copper thickness, mask). The table tool
(`python -m yapnr.rf.coupons.families build`, FEA environment) solves the 2D model at about 65
sample points per family and fits, per output, a polynomial in the normalized parameters
(full quadratic plus diagonal cubic terms). The fit and the synthetic sessions read only the
shipped tables (`data/<stackup>.json`), so they need numpy alone.

Outputs per mode ("" for a single line, "e"/"o" for the even and odd modes of a pair):
`lnC` and `lnC0` (F/m), `lng` (Wheeler's loss factor, 1/m) and `q:<region>`, the filling
factor εr ∂C/∂εr / C of each dielectric region (the rest is air).
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import SCHEMA_TABLES, jsonfmt, stackups

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RECEDE_MM = 0.002  # Wheeler wall recession

OUTER_MASKED = ("pp.dk", "pp1.h", "L1.etch", "L1.t", "mask.scale", "mask.dk")
OUTER_BARE = ("pp.dk", "pp1.h", "L1.etch", "L1.t")
STRIP = ("core.dk", "pp.dk", "core.h", "pp3.h", "L3.etch", "L3.t")


@dataclass(frozen=True)
class Family:
    id: str
    kind: str  # "outer" or "strip"
    w: float  # drawn width (mm)
    gap: Optional[float]  # coplanar gap (outer only)
    mask: bool
    pair_gap: Optional[float] = None
    title: str = ""

    @property
    def layer(self) -> str:
        return "L1" if self.kind == "outer" else "L3"

    @property
    def params(self) -> Tuple[str, ...]:
        if self.kind == "strip":
            return STRIP
        return OUTER_MASKED if self.mask else OUTER_BARE

    @property
    def regions(self) -> Dict[str, Tuple[str, str]]:
        """Dielectric region -> (Dk parameter, Df parameter)."""
        if self.kind == "strip":
            return {"lo": ("pp.dk", "pp.df"), "hi": ("core.dk", "core.df")}
        r = {"sub": ("pp.dk", "pp.df")}
        if self.mask:
            r["mask"] = ("mask.dk", "mask.df")
        return r

    @property
    def modes(self) -> Tuple[str, ...]:
        return ("e", "o") if self.pair_gap is not None else ("",)

    @property
    def outputs(self) -> List[str]:
        out = []
        for m in self.modes:
            p = f"{m}:" if m else ""
            out += [p + "lnC", p + "lnC0", p + "lng"] + [p + "q:" + r for r in self.regions]
        return out

    @property
    def etch_param(self) -> str:
        return f"{self.layer}.etch"

    @property
    def t_param(self) -> str:
        return f"{self.layer}.t"

    @property
    def height_param(self) -> str:
        return "pp1.h" if self.kind == "outer" else "pp3.h"

    @property
    def rough_param(self) -> str:
        return f"{self.layer}.rough"


# Drawn dimensions: design §4.3 (2D quasi-static, 50 Ω at nominal).
FAMILIES: Dict[str, Family] = {
    f.id: f
    for f in [
        Family("P", "outer", 0.291, 0.200, True, title="L1 GCPW under mask (primary)"),
        Family("P0.7", "outer", 0.204, 0.200, True, title="P at 0.7 w"),
        Family("P1.4", "outer", 0.407, 0.200, True, title="P at 1.4 w"),
        Family("P-MO", "outer", 0.291, 0.200, False, title="P with the mask opened"),
        Family("M", "outer", 0.348, None, True, title="L1 microstrip under mask"),
        Family("M-MO", "outer", 0.348, None, False, title="M with the mask opened"),
        Family("CPL20", "outer", 0.348, None, True, 0.20, "two M lines, gap 0.20"),
        Family("S", "strip", 0.214, None, False, title="L3 stripline"),
        Family("S0.7", "strip", 0.150, None, False, title="S at 0.7 w"),
        Family("S1.4", "strip", 0.300, None, False, title="S at 1.4 w"),
    ]
}

# Copper-free distances from the trace edge assumed by the 2D models (no side copper): L1
# microstrip (no L1 ground) and L3 stripline (no L3 copper).
M_CLEAR = 2.0
S_CLEAR = 1.5


def channel_halfwidth(fam_id: str) -> float:
    """Half width (mm) of the copper-free channel around a family's trace."""
    f = FAMILIES[fam_id]
    if f.kind == "strip":
        return f.w / 2 + S_CLEAR
    return f.w / 2 + (f.gap if f.gap is not None else M_CLEAR)


BOARD_FAMILIES = {
    "JLC04161H-7628": ("P", "P0.7", "P1.4", "P-MO", "M", "M-MO", "CPL20"),
    "JLC06161H-7628": ("S", "S0.7", "S1.4", "P"),
}


# --- geometry (needs xsec only when solving) ----------------------------------------------------


def spec_for(fam: Family, v: Dict[str, float], recede: float = 0.0):
    from yapnr.rf.coupons import xsec

    if fam.kind == "outer":
        return xsec.outer_spec(
            fam.w,
            h=v["pp1.h"],
            t=v["L1.t"],
            etch=v["L1.etch"],
            gap=fam.gap,
            mask=fam.mask,
            mask_scale=v.get("mask.scale", 1.0),
            pair_gap=fam.pair_gap,
            recede=recede,
        )
    return xsec.strip_spec(
        fam.w,
        h_below=v["pp3.h"],
        h_above=v["core.h"],
        t=v["L3.t"],
        etch=v["L3.etch"],
        pair_gap=fam.pair_gap,
        recede=recede,
    )


def solve_point(
    fam_id: str, values: Dict[str, float], mesh: str = "fit", parts: str = "all"
) -> Dict[str, float]:
    """Direct 2D solve of one family at one parameter point: the surrogate outputs. parts:
    "all", "c" (no Wheeler solve) or "g" (the Wheeler factor only)."""
    from yapnr.rf.coupons import xsec

    fam = FAMILIES[fam_id]
    v = dict(stackups.nominal(fam.params))
    v.update(values)
    spec, sig = spec_for(fam, v)
    er_of = {r: v[dk] for r, (dk, _) in fam.regions.items()}
    er_of["air"] = 1.0
    res = xsec.solve(xsec.build(spec, mesh), er_of, sig, vacuum_only=(parts == "g"))
    res_r = None
    if parts != "c":
        spec_r, sig_r = spec_for(fam, v, recede=RECEDE_MM)
        res_r = xsec.solve(xsec.build(spec_r, mesh), er_of, sig_r, vacuum_only=True)
    out: Dict[str, float] = {}
    for m in fam.modes:
        p = f"{m}:" if m else ""

        def pick(M):
            M = np.asarray(M)
            if not m:
                return float(M[0, 0])
            e, o = xsec.modal(M)
            return e if m == "e" else o

        c0 = pick(res["C0"])
        if res_r is not None:
            out[p + "lng"] = math.log(float(xsec.wheeler_g(c0, pick(res_r["C0"]), RECEDE_MM)))
        if parts == "g":
            continue
        c = pick(res["C"])
        out[p + "lnC"] = math.log(c)
        out[p + "lnC0"] = math.log(c0)
        for r in fam.regions:
            out[p + "q:" + r] = er_of[r] * pick(res["dC"][r]) / c
    return out


# --- polynomial surrogate --------------------------------------------------------------------
#
# lnC, lnC0 and the filling factors: a full cubic in the normalized parameters plus quartic
# diagonal terms and a quintic etch term (the coplanar gap makes C strongly nonlinear in the
# etch: over ±2.5 σ a cubic in the etch alone leaves 2.6e-3 in ln C, a quintic 1e-4, the level
# of the mesh noise). The Wheeler factor g: a full quadratic plus diagonal cubic terms.
# Outside the table range the surrogate extrapolates linearly from the range's edge.

BASIS_C = "cubic+quartic(etch x one)+quartic-diag+etch5;log-etch"
BASIS_G = "quadratic+cubic-diag"


def _monomials(d: int, order: int) -> List[Tuple[int, ...]]:
    out: List[Tuple[int, ...]] = [()]
    for k in range(1, order + 1):
        out += list(itertools.combinations_with_replacement(range(d), k))
    return out


def _terms(d: int, kind: str, etch: Optional[int]) -> List[Tuple[int, ...]]:
    if kind == "g":
        return _monomials(d, 2) + [(i, i, i) for i in range(d)]
    t = _monomials(d, 3)
    if etch is None:
        return t + [(i,) * 4 for i in range(d)]
    # quartic terms in the etch and one other parameter, quartic diagonal, quintic etch: the
    # leave-one-out error of ln C0 of P drops from 8.5e-4 (cubic + quartic diagonal) to 1.4e-4
    t += [
        m
        for m in itertools.combinations_with_replacement(range(d), 4)
        if len(set(m)) == 2 and etch in m
    ]
    t += [(i,) * 4 for i in range(d)] + [(etch,) * 5]
    return t


def basis(x: np.ndarray, terms: Sequence[Tuple[int, ...]]) -> np.ndarray:
    x = np.atleast_2d(np.asarray(x, float))
    cols = [np.prod(x[:, list(m)], axis=1) if m else np.ones(len(x)) for m in terms]
    return np.stack(cols, axis=1)


def design_points(d: int, n_lhs: int, seed: int = 7) -> np.ndarray:
    """Centre, axial points at ±1/3, ±2/3 and ±1, and a Latin hypercube in [-1, 1]^d."""
    pts = [np.zeros(d)]
    for i in range(d):
        for a in (-1.0, -2 / 3, -1 / 3, 1 / 3, 2 / 3, 1.0):
            e = np.zeros(d)
            e[i] = a
            pts.append(e)
    rng = np.random.default_rng(seed)
    lhs = np.stack([(rng.permutation(n_lhs) + rng.random(n_lhs)) / n_lhs for _ in range(d)], 1)
    pts += list(2.0 * lhs - 1.0)
    return np.array(pts)


def transforms(fam: Family) -> Dict[str, list]:
    """Feature transforms of the surrogate's coordinates. The etch enters through the
    logarithm of the etched gap-to-width ratio where the line has a gap (coplanar or coupled),
    else of the etched width: C is nearly linear in those (a cubic in the 1D etch sweep of P-MO
    leaves 6e-4 in ln C against 2.6e-3 in the raw etch)."""
    g = fam.gap if fam.gap is not None else fam.pair_gap
    return {fam.etch_param: ["ratio", fam.w, g] if g is not None else ["width", fam.w]}


def _fwd(tr, x: float) -> float:
    if tr[0] == "ratio":
        return math.log(max(tr[2] + 2 * x, 1e-6) / max(tr[1] - 2 * x, 1e-6))
    return math.log(max(tr[1] - 2 * x, 1e-6))


def _inv(tr, u: float) -> float:
    if tr[0] == "ratio":
        return (math.exp(u) * tr[1] - tr[2]) / (2 * (1 + math.exp(u)))
    return (tr[1] - math.exp(u)) / 2


@dataclass
class Surrogate:
    family: str
    params: Tuple[str, ...]
    lo: np.ndarray
    hi: np.ndarray
    coef: Dict[str, np.ndarray]
    terms_c: List[Tuple[int, ...]]
    terms_g: List[Tuple[int, ...]]
    tr: Dict[str, list] = field(default_factory=dict)

    def __post_init__(self):
        self._ulo = np.array([self._u(p, a) for p, a in zip(self.params, self.lo)])
        self._uhi = np.array([self._u(p, b) for p, b in zip(self.params, self.hi)])

    def _u(self, p: str, x: float) -> float:
        return _fwd(self.tr[p], x) if p in self.tr else float(x)

    def normalize(self, values: Dict[str, float]) -> np.ndarray:
        u = np.array([self._u(p, values[p]) for p in self.params], float)
        return 2.0 * (u - self._ulo) / (self._uhi - self._ulo) - 1.0

    def denormalize(self, x: np.ndarray) -> Dict[str, float]:
        u = self._ulo + (np.asarray(x) + 1.0) * 0.5 * (self._uhi - self._ulo)
        return {
            p: float(_inv(self.tr[p], ui) if p in self.tr else ui) for p, ui in zip(self.params, u)
        }

    def _eval(self, x: np.ndarray) -> Dict[str, float]:
        bc = basis(x, self.terms_c)[0]
        bg = basis(x, self.terms_g)[0]
        return {k: float((bg if k.endswith("lng") else bc) @ c) for k, c in self.coef.items()}

    def __call__(self, values: Dict[str, float]) -> Dict[str, float]:
        x = self.normalize(values)
        xc = np.clip(x, -1.0, 1.0)
        out = self._eval(xc)
        out_dims = np.nonzero(x != xc)[0]
        if len(out_dims):  # linear extrapolation from the edge of the range
            h = 1e-3
            for i in out_dims:
                xh = xc.copy()
                xh[i] -= h * np.sign(x[i])
                lower = self._eval(xh)
                for k in out:
                    out[k] = float(out[k] + (out[k] - lower[k]) / h * abs(x[i] - xc[i]))
        return out

    def outside(self, values: Dict[str, float]) -> List[str]:
        x = self.normalize(values)
        return [p for p, xi in zip(self.params, x) if abs(xi) > 1.0 + 1e-9]


def _etch_index(params: Sequence[str]) -> Optional[int]:
    for i, p in enumerate(params):
        if p.endswith(".etch"):
            return i
    return None


def _lstsq(B: np.ndarray, y: np.ndarray, ridge: float = 1e-10) -> np.ndarray:
    reg = ridge * np.eye(B.shape[1])
    return np.linalg.solve(B.T @ B + reg, B.T @ y)


def _ranges(fam: Family) -> Tuple[np.ndarray, np.ndarray]:
    r = [stackups.PARAMS[p].table for p in fam.params]
    return np.array([a for a, _ in r]), np.array([b for _, b in r])


def _blank(fam: Family) -> Surrogate:
    lo, hi = _ranges(fam)
    d = len(fam.params)
    return Surrogate(
        fam.id,
        fam.params,
        lo,
        hi,
        {},
        _terms(d, "c", _etch_index(fam.params)),
        _terms(d, "g", None),
        transforms(fam),
    )


def points_to_values(fam: Family, X: np.ndarray) -> List[Dict[str, float]]:
    sur = _blank(fam)
    return [sur.denormalize(x) for x in X]


# --- tables ----------------------------------------------------------------------------------


def table_path(stackup_id: str, data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or DATA_DIR, f"{stackup_id}.json")


_CACHE: Dict[str, Dict[str, Surrogate]] = {}


def load(stackup_id: str, path: Optional[str] = None) -> Dict[str, Surrogate]:
    """The surrogate of every family of a stackup's coupon board."""
    key = path or stackup_id
    if key in _CACHE:
        return _CACHE[key]
    p = path or table_path(stackup_id)
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"no 2D tables for {stackup_id} ({p}); build them with "
            f"`python -m yapnr.rf.coupons.families build --stackup {stackup_id}` "
            "in the FEA environment"
        )
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schema") != SCHEMA_TABLES:
        raise ValueError(f"{p}: not a {SCHEMA_TABLES} file")
    legacy = "basis" not in doc  # first tables: quadratic + cubic diagonal, g diagonal quadratic
    if not legacy and doc["basis"] != [BASIS_C, BASIS_G]:
        raise ValueError(f"{p}: tables built with another surrogate basis; rebuild them")
    out = {}
    for fid, t in doc["families"].items():
        params = tuple(t["params"])
        d = len(params)
        out[fid] = Surrogate(
            fid,
            params,
            np.array(t["lo"], float),
            np.array(t["hi"], float),
            {k: np.array(c, float) for k, c in t["coef"].items()},
            (
                (_monomials(d, 2) + [(i,) * 3 for i in range(d)])
                if legacy
                else _terms(d, "c", _etch_index(params))
            ),
            (
                ([()] + [(i,) for i in range(d)] + [(i, i) for i in range(d)])
                if legacy
                else _terms(d, "g", None)
            ),
            {} if legacy else t.get("transforms", {}),
        )
    _CACHE[key] = out
    return out


def load_doc(stackup_id: str) -> dict:
    with open(table_path(stackup_id), encoding="utf-8") as f:
        return json.load(f)


# --- the table tool (FEA environment) --------------------------------------------------------


def _work(args):
    fam_id, values, mesh, parts = args
    t0 = time.time()
    out = solve_point(fam_id, values, mesh, parts)
    return out, time.time() - t0


def build_family(
    fam_id: str, pool, n_test: int = 10, log=print, samples_dir: Optional[str] = None
) -> dict:
    """Solve the design points of one family and fit its surrogate.

    About twice as many samples as basis terms; the Wheeler factor (two vacuum solves per point)
    on the centre, the axial points and the first LHS points only. Held-out points on the fine
    mesh, the nominal point among them, measure the surrogate error including the mesh error."""
    fam = FAMILIES[fam_id]
    d = len(fam.params)
    terms_c = _terms(d, "c", _etch_index(fam.params))
    terms_g = _terms(d, "g", None)
    n_axial = 1 + 6 * d
    X = design_points(d, n_lhs=max(2 * len(terms_c) - n_axial, 10))
    vals = points_to_values(fam, X)
    t0 = time.time()
    res = pool.map(_work, [(fam_id, v, "fit", "c") for v in vals])
    n_g = min(len(X), n_axial + len(terms_g))
    rg = pool.map(_work, [(fam_id, v, "fit", "g") for v in vals[:n_g]])
    rng = np.random.default_rng(1000 + len(fam_id))
    Xt = rng.uniform(-0.8, 0.8, size=(n_test, d))
    Xt[0] = _blank(fam).normalize(stackups.nominal(fam.params))
    vt = points_to_values(fam, Xt)
    rt = pool.map(_work, [(fam_id, v, "fine", "all") for v in vt])
    tests = [dict(values=v, fine=direct) for v, (direct, _) in zip(vt, rt)]
    c, g = [r[0] for r in res], [r[0] for r in rg]
    entry = _table_entry(fam, X, c, g, tests)
    err, err0 = entry["test"]["max_err"], entry["test"]["nominal_err"]
    cpu = sum(r[1] for r in res) + sum(r[1] for r in rg) + sum(r[1] for r in rt)
    log(
        f"{fam_id}: {len(vals)} + {len(rg)} + {len(vt)} solves, {cpu:.0f} s CPU, "
        f"{time.time() - t0:.0f} s wall; test |dZ0| max {err['z0_ohm']:.3f} ohm, "
        f"|d eps_eff| max {err['eps_eff']:.4f}, |d g|/g max {err['g_rel']:.3f}; at nominal "
        f"{err0['z0_ohm']:.3f} ohm, {err0['eps_eff']:.4f}, {err0['g_rel']:.3f}"
    )
    if samples_dir:
        os.makedirs(samples_dir, exist_ok=True)
        jsonfmt.dump(
            dict(family=fam_id, X=X.tolist(), values=vals, c=c, g=g, tests=_round(tests)),
            os.path.join(samples_dir, f"{fam_id}.json"),
        )
    return entry


def _round(obj):
    if isinstance(obj, float):
        return float(f"{obj:.8g}")
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


def _test_errors(fam: Family, tests: List[dict]) -> dict:
    dz, de, dg = 0.0, 0.0, 0.0
    for t in tests:
        for m in fam.modes:
            p = f"{m}:" if m else ""
            c1, c01 = math.exp(t["fine"][p + "lnC"]), math.exp(t["fine"][p + "lnC0"])
            c2, c02 = math.exp(t["surrogate"][p + "lnC"]), math.exp(t["surrogate"][p + "lnC0"])
            z1, e1 = 1 / (299792458.0 * math.sqrt(c1 * c01)), c1 / c01
            z2, e2 = 1 / (299792458.0 * math.sqrt(c2 * c02)), c2 / c02
            dz, de = max(dz, abs(z1 - z2)), max(de, abs(e1 - e2))
            dg = max(dg, abs(math.exp(t["surrogate"][p + "lng"] - t["fine"][p + "lng"]) - 1))
    return dict(z0_ohm=dz, eps_eff=de, g_rel=dg)


def build(
    stackup_ids: Sequence[str],
    workers: int = 2,
    families: Optional[Sequence[str]] = None,
    force: bool = False,
    data_dir: Optional[str] = None,
    samples_dir: Optional[str] = None,
):
    """Build (or complete) the tables of the given stackups; families already in a table with
    the current basis are kept unless `force`."""
    import multiprocessing as mp

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    done: Dict[str, dict] = {}
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers) as pool:
        for sid in stackup_ids:
            path = table_path(sid, data_dir)
            doc = {"schema": SCHEMA_TABLES, "stackup": sid, "families": {}}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
            if doc.get("basis") != [BASIS_C, BASIS_G]:
                doc["families"] = {}
            doc["basis"] = [BASIS_C, BASIS_G]
            doc["mesh"] = {"fit": list(_mesh("fit")), "fine": list(_mesh("fine"))}
            doc["recede_mm"] = RECEDE_MM
            doc["tool"] = "python -m yapnr.rf.coupons.families build"
            for fid in BOARD_FAMILIES[sid]:
                if families and fid not in families:
                    continue
                if fid in doc["families"] and not force:
                    done.setdefault(fid, doc["families"][fid])
                if fid not in done:
                    done[fid] = build_family(
                        fid, pool, log=lambda m: print(m, flush=True), samples_dir=samples_dir
                    )
                doc["families"][fid] = done[fid]
                os.makedirs(os.path.dirname(path), exist_ok=True)
                jsonfmt.dump(doc, path)
            print(f"wrote {path}", flush=True)


def launch_gap(stackup_id: str, pad_w: float = 1.27, cut: Sequence[int] = (2,), z0: float = 50.0):
    """The coplanar gap that makes the edge-SMA pad `z0` ohms (2D, masked GCPW), with the planes
    `cut` removed under it, so the next plane down is the reference and the dielectrics between
    are lumped at their thickness-weighted εr (design §6.2). FEA environment."""
    from yapnr.rf.coupons import xsec

    st = stackups.get(stackup_id)
    h, eh, n_cu, ref = 0.0, 0.0, 0, None
    for x in st.layers[1:]:
        if x.kind == "copper":
            n_cu += 1
            if n_cu + 1 not in cut:
                ref = n_cu + 1
                break
        h += x.t_mm
        eh += x.t_mm * (x.er if x.er else 4.4)
    er = eh / h

    def zc(g):
        spec, sig = xsec.outer_spec(pad_w, h, 0.035, 0.0, g, True, 1.0, gnd_w=2.0, width=14.0)
        r = xsec.solve(xsec.build(spec, "fit"), {"sub": er, "mask": 3.8, "air": 1.0}, sig)
        c, c0 = r["C"][0, 0], r["C0"][0, 0]
        return 1 / (299792458.0 * math.sqrt(c * c0))

    lo, hi = 0.05, 2.0
    for _ in range(12):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if zc(mid) < z0 else (lo, mid)
    return dict(gap_mm=round(0.5 * (lo + hi), 3), reference=f"L{ref}", h_mm=h, er=round(er, 3))


def refit(stackup_ids: Sequence[str], samples_dir: str, data_dir: Optional[str] = None):
    """Rebuild the tables from saved 2D samples (`build --samples-dir`) with the current basis,
    without solving again."""
    done: Dict[str, dict] = {}
    for sid in stackup_ids:
        doc = {"schema": SCHEMA_TABLES, "stackup": sid, "families": {}, "basis": [BASIS_C, BASIS_G]}
        doc["mesh"] = {"fit": list(_mesh("fit")), "fine": list(_mesh("fine"))}
        doc["recede_mm"] = RECEDE_MM
        doc["tool"] = "python -m yapnr.rf.coupons.families build (refit from saved samples)"
        for fid in BOARD_FAMILIES[sid]:
            if fid not in done:
                with open(os.path.join(samples_dir, f"{fid}.json"), encoding="utf-8") as f:
                    smp = json.load(f)
                done[fid] = _table_entry(
                    FAMILIES[fid], np.array(smp["X"]), smp["c"], smp["g"], smp["tests"]
                )
                e = done[fid]["test"]
                print(f"{fid}: test max {e['max_err']}, nominal {e['nominal_err']}", flush=True)
            doc["families"][fid] = done[fid]
        path = table_path(sid, data_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        jsonfmt.dump(doc, path)
        print(f"wrote {path}", flush=True)


def _table_entry(fam: Family, X: np.ndarray, c: List[dict], g: List[dict], tests: List[dict]):
    """Fit the surrogate of one family to its samples and evaluate it on the held-out tests."""
    d = len(fam.params)
    terms_c = _terms(d, "c", _etch_index(fam.params))
    terms_g = _terms(d, "g", None)
    Bc = basis(X, terms_c)
    Y = {k: np.array([r[k] for r in c]) for k in fam.outputs if not k.endswith("lng")}
    coef = {k: _lstsq(Bc, y) for k, y in Y.items()}
    Bg = basis(X[: len(g)], terms_g)
    Yg = {k: np.array([r[k] for r in g]) for k in fam.outputs if k.endswith("lng")}
    coef.update({k: _lstsq(Bg, y) for k, y in Yg.items()})
    sur = _blank(fam)
    sur.coef = coef
    fit_rms = {k: float(np.sqrt(np.mean((Bc @ coef[k] - y) ** 2))) for k, y in Y.items()}
    fit_rms.update({k: float(np.sqrt(np.mean((Bg @ coef[k] - y) ** 2))) for k, y in Yg.items()})
    tests = [dict(values=t["values"], fine=t["fine"], surrogate=sur(t["values"])) for t in tests]
    return dict(
        family=asdict(fam),
        params=list(fam.params),
        lo=sur.lo.tolist(),
        hi=sur.hi.tolist(),
        transforms=transforms(fam),
        outputs=fam.outputs,
        coef={k: [float(f"{x:.10g}") for x in v] for k, v in coef.items()},
        samples=dict(c=len(c), g=len(g)),
        fit_rms={k: float(f"{x:.3g}") for k, x in fit_rms.items()},
        test=dict(
            max_err={k: float(f"{x:.3g}") for k, x in _test_errors(fam, tests).items()},
            nominal_err={k: float(f"{x:.3g}") for k, x in _test_errors(fam, tests[:1]).items()},
            points=_round(tests),
        ),
    )


def _mesh(name):
    from yapnr.rf.coupons import xsec

    return xsec.MESH[name]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m yapnr.rf.coupons.families")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="solve the 2D tables (FEA environment: scikit-fem, gmsh)")
    b.add_argument("--stackup", action="append", default=None)
    b.add_argument("--family", action="append", default=None)
    b.add_argument("--workers", type=int, default=2)
    b.add_argument("--force", action="store_true", help="rebuild families already present")
    b.add_argument("--data-dir", default=None, help="output directory (default: the package's)")
    b.add_argument("--samples-dir", default=None, help="also save the raw 2D samples there")
    lp = sub.add_parser("pad", help="the 50 ohm coplanar gap of the edge-SMA pad (2D)")
    lp.add_argument("--stackup", required=True)
    lp.add_argument("--cut", default="2", help="planes removed under the pad, e.g. 2 or 2,3")
    r = sub.add_parser("refit", help="rebuild the tables from saved samples (numpy only)")
    r.add_argument("--stackup", action="append", default=None)
    r.add_argument("--samples-dir", required=True)
    r.add_argument("--data-dir", default=None)
    p = sub.add_parser("point", help="one direct 2D solve (prints Z0, eps_eff per mode)")
    p.add_argument("family")
    p.add_argument("--set", action="append", default=[], help="param=value")
    p.add_argument("--mesh", default="fit")
    a = ap.parse_args(argv)
    if a.cmd == "pad":
        print(launch_gap(a.stackup, cut=[int(x) for x in a.cut.split(",")]))
    elif a.cmd == "refit":
        refit(a.stackup or list(BOARD_FAMILIES), a.samples_dir, a.data_dir)
    elif a.cmd == "build":
        build(
            a.stackup or list(BOARD_FAMILIES),
            a.workers,
            a.family,
            a.force,
            a.data_dir,
            a.samples_dir,
        )
    else:
        vals = {k: float(x) for k, x in (s.split("=", 1) for s in a.set)}
        t0 = time.time()
        out = solve_point(a.family, vals, a.mesh)
        fam = FAMILIES[a.family]
        for m in fam.modes:
            pre = f"{m}:" if m else ""
            c, c0 = math.exp(out[pre + "lnC"]), math.exp(out[pre + "lnC0"])
            print(
                f"{a.family}{('/' + m) if m else ''}: Z0 {1 / (299792458.0 * math.sqrt(c * c0)):.3f}"
                f" eps_eff {c / c0:.4f} g {math.exp(out[pre + 'lng']):.1f}/m",
                {k: round(v, 4) for k, v in out.items() if k.startswith(pre + "q:")},
            )
        print(f"{time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
