"""Thickness-equivalent substrates for the zero-thickness RF solver (Order 0, D-O0-9).

`yapnr.rf` models the copper as a zero-thickness sheet on one uniform substrate over a ground
plane. A real strip has thickness (43 µm on OSH Park's outer layers, t/h = 0.22 over region M's
0.2 mm prepreg), which adds fringing capacitance between its side walls and the plane and puts
more of the field in air: on the nominal substrate the sheet reads about 3 Ω high and 3 % high in
εeff (`order0-design` §4.1). An *equivalent substrate* (εr', h') makes a zero-thickness strip of
the same drawn width have the real strip's quasi-static Z0 and εeff:

- h' from the vacuum capacitance: a zero-thickness strip on a uniform substrate has a C0 that
  depends on w/h' alone, so h' is the height at which C0(w, h') equals the real strip's C0
  (equivalently Z0·sqrt(εeff), its impedance in air);
- εr' from εeff: on a uniform substrate εeff = C(εr')/C0 rises monotonically with εr' at fixed
  geometry (C is concave in εr, so the solve iterates with the slope at each point).

Both sides are 2D quasi-static solves with the coupon generator's solver (`xsec`, scikit-fem P2 on
a gmsh mesh): the real strip with its copper on the real dielectric stack (`launch.section`: one
prepreg for region M; prepreg, core and prepreg for region W, inner layers removed), the sheet
as a line conductor on the substrate's surface (`sheet_spec`, `build_sheet`). There is no L1
ground and no mask: the optimizer's window has none within 5h (M) or 3h (W), the solver has none,
and Order 0's RF copper is mask-open. The match is exact at the matching width only; `residuals`
reports other widths (narrow features read high in Z0 on the equivalent substrate, wide ones
low, review R15).

Two targets for the real strip, both at 5.0 GHz:

- `geometric`: the lossless quasi-static line at the stackup rf block's εr (OSH Park's 1 GHz Dk
  scaled by Isola's 1 -> 5 GHz trend: prepreg 3.561, core 3.818), D-O0-9 as written;
- `coupon` (used by the Order 0 specs): the coupon forward model's line (`models.family_line`,
  the shipped stackup priors), which the coupon predictions and the stackup fit use: its
  Djordjevic-Sarkar εr at 5 GHz (3.61 at 1 GHz with Df 0.009 gives 3.578, 0.5 % above the rf
  block's 3.561) and the internal inductance of its rough-copper surface impedance (Huray,
  Rq 1 µm: about +0.9 % on L at 5 GHz). L = μ0ε0/C0 + L_int and C of the open strip at that εr;
  the sheet then needs C0 = μ0ε0/L and the same C. Without L_int the FDTD's lines would run
  about 0.5 % fast against the coupon model.

The loss tangent tan δ' gives the sheet the real line's dielectric conductance at 5 GHz
(G = ω Σ tan δ_r · εr_r ∂C/∂εr_r over the real stack; the sheet holds less of its field in the
substrate than the thick strip). The solver's substrate conductivity makes tan δ fall as 1/f
around f_ref, the laminate's stays about flat; run 0b's line comparison measures what is left.

What the equivalence leaves out: the variation over the band of the dielectric's dispersion and
of the internal inductance (matched at 5 GHz; about 0.1 % in εeff over 4.25–5.75 GHz), the
copper's loss (the solver's sheet is resistive at f_ref; run 0b corrects it), and microstrip
dispersion, which the FDTD has and the quasi-static match does not need (a property of the
line, about +0.2 % in εeff at 5 GHz on M; the coupon target leaves out the coupon model's own
Kirschning-Jansen term so that it is not counted twice).

    python -m yapnr.rf.coupons.equivalent [--out FILE]   # FEA environment; prints a table

writes `data/equivalent-oshpark-4l.json` (`yapnr-coupon-equivalent/1`).
"""

from __future__ import annotations

import argparse
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

C_LIGHT = 299792458.0
EPS0 = 8.8541878128e-12

# The matched lines: the optimizer's port lines (order0-design §3.1 rf block: M 0.40 mm on a
# 0.10 mm grid, W 3.0 mm on a 0.50 mm grid).
MATCH_W = {"M": 0.40, "W": 3.0}
# The 2D box (mm): wide and tall enough that the walls move Z0 by well under 0.1 Ω (M within
# 0.05 Ω at 24 x 10 mm, launch.md §2); the same box on both sides of the match.
BOX = {"M": (24.0, 10.0), "W": (60.0, 30.0)}
# Mesh near the sheet's edges (field singular there), largest element, growth (mm): the M sizes;
# W scales by h. Against (0.0008, 0.6, 0.15) Z0 moves by 0.007 ohm on the M line; against
# Hammerstad-Jensen's zero-thickness forms the M line is within 0.06 % (Z0) and 0.02 % (εeff).
SHEET_MESH = (0.0004, 0.6, 0.10)
RESIDUAL_W = {"M": (0.20, 0.30, 0.40, 0.50, 0.70, 1.00, 1.40, 2.00), "W": (1.0, 2.0, 3.0, 4.0, 5.0)}
SCHEMA = "yapnr-coupon-equivalent/1"


def sheet_spec(w: float, h: float, width: float, air: float) -> dict:
    """A zero-thickness strip of width `w` on a uniform substrate of height `h` over the plane at
    z = 0, in a grounded box `width` wide and `air` above the substrate."""
    return dict(w=w, h=h, width=width, air=air)


def build_sheet(spec: dict, mesh: Tuple[float, float, float] = SHEET_MESH, scale: float = 1.0):
    """Mesh `sheet_spec`: the strip is a line on the substrate-air interface whose nodes are the
    signal conductor (`xsec.solve`'s input: points, tris, mat, nodesets)."""
    from yapnr.rf.coupons import xsec

    xsec._need()
    import gmsh

    lc_min, lc_max, grow = (mesh[0] * scale, mesh[1] * scale, mesh[2])
    w, h, W, air = spec["w"], spec["h"], spec["width"], spec["air"]
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.model.add("sheet")
        occ = gmsh.model.occ
        sub = occ.addRectangle(-W / 2, 0.0, 0, W, h)
        top = occ.addRectangle(-W / 2, h, 0, W, air)
        p0 = occ.addPoint(-w / 2, h, 0)
        p1 = occ.addPoint(w / 2, h, 0)
        strip = occ.addLine(p0, p1)
        _, omap = occ.fragment([(2, sub), (2, top)], [(1, strip)])
        occ.synchronize()
        regions = {
            "sub": {t for d, t in omap[0] if d == 2},
            "air": {t for d, t in omap[1] if d == 2},
        }
        strip_curves = {t for d, t in omap[2] if d == 1}
        surfs = sorted(regions["sub"] | regions["air"])
        outer = {
            abs(c)
            for _, c in gmsh.model.getBoundary(
                [(2, t) for t in surfs], combined=True, oriented=False
            )
        }
        bottom, rest = set(), set()
        for c in outer:
            b = gmsh.model.getBoundingBox(1, c)
            (bottom if abs(b[1]) < 1e-12 and abs(b[4]) < 1e-12 else rest).add(c)
        ends = []
        for c in strip_curves:
            ends += [abs(t) for d, t in gmsh.model.getBoundary([(1, c)], oriented=False) if d == 0]
        fd = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(fd, "CurvesList", sorted(strip_curves))
        gmsh.model.mesh.field.setNumber(fd, "Sampling", 800)
        fe = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(fe, "PointsList", sorted(set(ends)))
        f1 = gmsh.model.mesh.field.add("MathEval")
        gmsh.model.mesh.field.setString(
            f1, "F", f"Min({lc_max}, {4 * lc_min} + {grow}*F{fd})"
        )  # along the strip
        f2 = gmsh.model.mesh.field.add("MathEval")
        gmsh.model.mesh.field.setString(f2, "F", f"Min({lc_max}, {lc_min} + {grow}*F{fe})")
        fm = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(fm, "FieldsList", [f1, f2])
        gmsh.model.mesh.field.setAsBackgroundMesh(fm)
        for k in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
            gmsh.option.setNumber(f"Mesh.{k}", 0)
        gmsh.model.mesh.generate(2)
        ntags, coords, _ = gmsh.model.mesh.getNodes()
        idx = np.full(int(ntags.max()) + 1, -1)
        idx[ntags.astype(int)] = np.arange(len(ntags))
        pts = coords.reshape(-1, 3)[:, :2]
        tris, mat = [], []
        for name, ts in regions.items():
            for t in ts:
                _, _, en = gmsh.model.mesh.getElements(2, t)
                tri = idx[np.asarray(en[0], int)].reshape(-1, 3)
                tris.append(tri)
                mat.append(np.full(len(tri), name, dtype=object))
        nodesets = {}
        for name, cs in (("S", strip_curves), ("@ground", bottom), ("@outer", rest)):
            s = set()
            for c in cs:
                nt, _, _ = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
                s |= set(idx[nt.astype(int)].tolist())
            nodesets[name] = np.array(sorted(s), dtype=int)
    finally:
        gmsh.finalize()
    tris = np.vstack(tris)
    used = np.unique(tris)
    remap = np.full(len(pts), -1)
    remap[used] = np.arange(len(used))
    nodesets = {k: remap[v[remap[v] >= 0]] for k, v in nodesets.items()}
    return dict(points=pts[used], tris=remap[tris], mat=np.concatenate(mat), nodesets=nodesets)


_MESHES: Dict[tuple, dict] = {}


def sheet_mesh(w: float, h: float, region: str, mesh: Optional[tuple] = None) -> dict:
    """The mesh of a zero-thickness strip (cached per geometry: the εr solves reuse it)."""
    width, air = BOX[region]
    mesh = mesh or SHEET_MESH
    key = (round(w, 9), round(h, 9), region, tuple(mesh))
    if key not in _MESHES:
        scale = 1.0 if region == "M" else h / 0.2
        _MESHES[key] = build_sheet(sheet_spec(w, h, width, air), mesh, scale=scale)
    return _MESHES[key]


def sheet_solve(w: float, h: float, er: float, region: str, mesh: Optional[tuple] = None) -> dict:
    """C (at `er`), C0 and ∂C/∂εr (at `er`, Hellmann-Feynman) per metre of a zero-thickness
    strip on a uniform substrate. C is concave in εr: the derivative holds at `er` only."""
    from yapnr.rf.coupons import xsec

    r = xsec.solve(sheet_mesh(w, h, region, mesh), {"sub": er, "air": 1.0}, ["S"])
    return dict(c=float(r["C"][0, 0]), c0=float(r["C0"][0, 0]), dc=float(r["dC"]["sub"][0, 0]))


def sheet_line(
    w: float, h: float, er: float, region: str, mesh: Optional[tuple] = None
) -> Tuple[float, float]:
    """(Z0, εeff) of a zero-thickness strip on a uniform substrate (2D quasi-static)."""
    r = sheet_solve(w, h, er, region, mesh)
    return 1.0 / (C_LIGHT * math.sqrt(r["c"] * r["c0"])), r["c"] / r["c0"]


def real_solve(bd, region: str, w: float, mesh: str = "fit") -> dict:
    """The real strip (copper thickness, the region's dielectric stack, no L1 ground, no mask)
    in the region's box: C, C0, and each dielectric's εr ∂C/∂εr (the part of C it holds)."""
    from yapnr.rf.coupons import launch, xsec

    width, air = BOX[region]
    spec, er = launch.section(bd, region, w, None, width=width, air=air)
    r = xsec.solve(xsec.build(spec, mesh), er, ["S"])
    held = {n: er[n] * float(d[0, 0]) for n, d in r["dC"].items() if n in ("pp", "core")}
    return dict(c=float(r["C"][0, 0]), c0=float(r["C0"][0, 0]), held=held)


def real_line(bd, region: str, w: float, mesh: str = "fit") -> Tuple[float, float, float]:
    """(Z0, εeff, C0) of the real strip (`real_solve`)."""
    r = real_solve(bd, region, w, mesh)
    c, c0 = r["c"], r["c0"]
    return 1.0 / (C_LIGHT * math.sqrt(c * c0)), c / c0, c0


def _solve_h(w: float, c0_target: float, region: str, h0: float) -> float:
    """h' with C0_sheet(w, h') = c0_target (secant on log h; C0 falls as h grows)."""

    def f(h):
        return math.log(sheet_solve(w, h, 1.0, region)["c0"] / c0_target)

    a, b = h0 * 0.95, h0 * 0.85
    fa, fb = f(a), f(b)
    for _ in range(20):
        if abs(fb) < 1e-9 or fb == fa:  # converged (C0 to 1e-9), or a step below rounding
            break
        nb = math.exp(math.log(b) - fb * (math.log(b) - math.log(a)) / (fb - fa))
        a, fa = b, fb
        b = nb
        fb = f(b)
    return b


def _solve_er(w: float, h: float, eps_target: float, region: str, er0: float) -> float:
    """εr' with εeff_sheet(w, h, εr') = eps_target (Newton with the Hellmann-Feynman slope)."""
    er = er0
    for _ in range(20):
        r = sheet_solve(w, h, er, region)
        err = r["c"] / r["c0"] - eps_target
        if abs(err) < 1e-9:
            break
        step = err / (r["dc"] / r["c0"])
        er -= step
        if abs(step) < 1e-12:
            break
    return er


MU0 = 4.0e-7 * math.pi
F_MATCH = 5.0e9
# Board 4L id -> the coupon stackup whose forward model is the `coupon` target.
COUPON_STACKUP = {
    "oshpark-4l-fr408hr": "OSHPARK-4L-FR408HR",
    "oshpark-4l-em528": "OSHPARK-4L-EM528",
}


def coupon_terms(bd, region: str, f: float = F_MATCH) -> dict:
    """The coupon forward model's εr of each dielectric at `f` (Djordjevic-Sarkar, the
    stackup's nominal Dk and Df) and the internal inductance (H/m) of the region's line family
    (M or W): Im(Z)/ω of its series impedance less μ0ε0/C0 of its 2D table."""
    from yapnr.rf.coupons import families, models, stackups

    sid = COUPON_STACKUP[bd.id]
    st = stackups.get(sid)
    v = stackups.with_values(st, {})
    tables = families.load(sid)
    fam = MATCH_FAMILY[region]
    line = models.family_line(fam, v, np.array([f]), tables, dispersion=False, stackup_id=sid)
    w = 2 * math.pi * f
    z_series = complex(line.gamma[0] * line.zc[0])
    c0_fam = math.exp(tables[fam](v)["lnC0"])
    l_int = z_series.imag / w - MU0 * EPS0 / c0_fam
    eps = {
        n: complex(models.ds_eps(v[n + ".dk"], v[n + ".df"], np.array([f]))[0])
        for n in ("pp", "core")
    }
    return dict(
        stackup=sid,
        family=fam,
        er={n: e.real for n, e in eps.items()},
        tan_delta={n: -e.imag / e.real for n, e in eps.items()},
        l_int_h_per_m=l_int,
        dk_df_1ghz={n: (v[n + ".dk"], v[n + ".df"]) for n in ("pp", "core")},
    )


MATCH_FAMILY = {"M": "M", "W": "W"}
# The stackups' rf block loss tangent at 5 GHz (order0-design §3.1): FR408HR 0.0095, EM528 0.0053.
RF_TAN_DELTA = {"oshpark-4l-fr408hr": 0.0095, "oshpark-4l-em528": 0.0053}


def tan_deltas(bd, terms: Optional[dict]) -> Dict[str, float]:
    """Each dielectric's tan δ at 5 GHz: the coupon model's (Djordjevic-Sarkar ε''/ε' from the
    stackup's Df at 1 GHz) for the coupon target, the rf block's otherwise."""
    if terms is None:
        return {"pp": RF_TAN_DELTA[bd.id], "core": RF_TAN_DELTA[bd.id]}
    return dict(terms["tan_delta"])


def equivalent(bd, region: str, w: Optional[float] = None, target: str = "coupon") -> dict:
    """The equivalent substrate of `bd`'s region for a strip of width `w` (default the region's
    port line): the real strip's Z0 and εeff (`target` "geometric" or "coupon", see the module
    doc), then h' and εr'."""
    from dataclasses import replace

    w = MATCH_W[region] if w is None else w
    l_int = 0.0
    terms = None
    if target == "coupon":
        terms = coupon_terms(bd, region)
        bd = replace(bd, er_pp=terms["er"]["pp"], er_core=terms["er"]["core"])
        l_int = terms["l_int_h_per_m"]
    elif target != "geometric":
        raise ValueError(f"unknown target {target!r}")
    rs = real_solve(bd, region, w)
    c0_real, c_real = rs["c0"], rs["c"]
    z_q, e_q = 1.0 / (C_LIGHT * math.sqrt(c_real * c0_real)), c_real / c0_real
    l_tot = MU0 * EPS0 / c0_real + l_int
    z_real = math.sqrt(l_tot / c_real)
    e_real = C_LIGHT**2 * l_tot * c_real
    c0_sheet = MU0 * EPS0 / l_tot
    h_nom = bd.h_m if region == "M" else bd.h_w
    h_eq = _solve_h(w, c0_sheet, region, h_nom)
    er_eq = _solve_er(w, h_eq, c_real / c0_sheet, region, nominal_er(bd, region))
    z_chk, e_chk = sheet_line(w, h_eq, er_eq, region)
    # The loss tangent that gives the sheet the real line's dielectric conductance at 5 GHz:
    # G = ω Σ tanδ_r εr_r ∂C/∂εr_r over the real stack, ω tanδ' εr' ∂C/∂εr' on the sheet.
    tan_d = tan_deltas(bd, terms)
    g_real = sum(tan_d[n] * held for n, held in rs["held"].items())
    sh = sheet_solve(w, h_eq, er_eq, region)
    tan_eq = g_real / (er_eq * sh["dc"])
    out = dict(
        w_mm=w,
        target=target,
        tan_delta=tan_eq,
        tan_delta_real=tan_d,
        real=dict(z0_ohm=z_real, eps_eff=e_real, z0_quasi_static=z_q, eps_eff_quasi_static=e_q),
        er=er_eq,
        h_mm=h_eq,
        check=dict(z0_ohm=z_chk, eps_eff=e_chk),
    )
    if terms is not None:
        out["coupon_model"] = terms
    return out


def nominal_er(bd, region: str) -> float:
    """The region's single εr at 5 GHz: the prepreg (M), or the series combination of the
    stack (W), as the stackup's rf block gives it."""
    if region == "M":
        return bd.er_pp
    hs = (bd.h_pp, bd.h_core, bd.h_pp)
    es = (bd.er_pp, bd.er_core, bd.er_pp)
    return sum(hs) / sum(h / e for h, e in zip(hs, es))


def residuals(bd, region: str, eq: dict, widths: Sequence[float]) -> List[dict]:
    """Per width: the real strip (the target's εr; with the coupon target its internal
    inductance scaled from the matched width by the strip's perimeter, est.), the sheet on the
    equivalent substrate and the sheet on the nominal substrate, as [Z0, εeff]."""
    from dataclasses import replace

    h_nom = bd.h_m if region == "M" else bd.h_w
    er_nom = nominal_er(bd, region)
    l_int0, w0 = 0.0, eq["w_mm"]
    if eq["target"] == "coupon":
        terms = eq["coupon_model"]
        bd = replace(bd, er_pp=terms["er"]["pp"], er_core=terms["er"]["core"])
        l_int0 = terms["l_int_h_per_m"]
    out = []
    for w in widths:
        _, e_q, c0 = real_line(bd, region, w)
        l_int = l_int0 * (w0 + 2 * bd.t_out) / (w + 2 * bd.t_out)
        l_tot = MU0 * EPS0 / c0 + l_int
        c = e_q * c0
        z_r, e_r = math.sqrt(l_tot / c), C_LIGHT**2 * l_tot * c
        z_e, e_e = sheet_line(w, eq["h_mm"], eq["er"], region)
        z_n, e_n = sheet_line(w, h_nom, er_nom, region)
        out.append(
            dict(
                w_mm=w,
                real=[round(z_r, 3), round(e_r, 5)],
                equivalent=[round(z_e, 3), round(e_e, 5)],
                nominal_sheet=[round(z_n, 3), round(e_n, 5)],
            )
        )
    return out


def hammerstad_check(w: float, h: float, er: float) -> Tuple[float, float]:
    """Hammerstad-Jensen's zero-thickness (Z0, εeff), the closed-form check of `sheet_line`."""
    from yapnr.rf.stackup import hammerstad_jensen

    return hammerstad_jensen(w * 1e-3, h * 1e-3, er)


def compute(boards: Optional[Dict[str, object]] = None) -> dict:
    """Every Order 0 region on FR408HR and EM528, for both targets: the equivalent substrate,
    its check, the residuals over widths (coupon target) and the closed-form check of the sheet
    solve."""
    from yapnr.rf.coupons import launch

    boards = boards or {"fr408hr": launch.OSH_FR408HR, "em528": launch.OSH_EM528}
    doc: dict = {
        "schema": SCHEMA,
        "method": "2D quasi-static (xsec, P2): the real strip (43 um copper, the region's"
        " dielectric stack, no L1 ground, no mask) against a zero-thickness strip on a uniform"
        " substrate; h' from C0, er' from eps_eff; box M 24 x 10 mm, W 60 x 30 mm; targets"
        " 'geometric' (rf-block er, lossless) and 'coupon' (the coupon model's"
        " Djordjevic-Sarkar er and internal inductance at 5 GHz, no Kirschning-Jansen term)",
        "f_ghz": F_MATCH / 1e9,
        "use": "coupon",
        "substrates": {},
    }
    for name, bd in boards.items():
        for region in ("M", "W"):
            h_nom = bd.h_m if region == "M" else bd.h_w
            er_nom = nominal_er(bd, region)
            entry: dict = {"matched_width_mm": MATCH_W[region]}
            for target in ("geometric", "coupon"):
                eq = equivalent(bd, region, target=target)
                hj = hammerstad_check(eq["w_mm"], eq["h_mm"], eq["er"])
                item = {
                    "real": {k: round(v, 5) for k, v in eq["real"].items()},
                    "equivalent": {
                        "er": round(eq["er"], 4),
                        "h_mm": round(eq["h_mm"], 5),
                        "tan_delta": round(eq["tan_delta"], 5),
                    },
                    "tan_delta_real": {k: round(v, 5) for k, v in eq["tan_delta_real"].items()},
                    "check": {k: round(v, 5) for k, v in eq["check"].items()},
                    "hammerstad_jensen_on_equivalent": [round(hj[0], 4), round(hj[1], 5)],
                }
                if target == "coupon":
                    t = eq["coupon_model"]
                    item["coupon_model"] = {
                        "stackup": t["stackup"],
                        "family": t["family"],
                        "er_5ghz": {k: round(v, 4) for k, v in t["er"].items()},
                        "dk_df_1ghz": t["dk_df_1ghz"],
                        "l_int_nh_per_m": round(t["l_int_h_per_m"] * 1e9, 4),
                    }
                    item["residuals"] = residuals(bd, region, eq, RESIDUAL_W[region])
                entry[target] = item
            z_n, e_n = sheet_line(MATCH_W[region], h_nom, er_nom, region)
            entry["nominal"] = {
                "er": round(er_nom, 4),
                "h_mm": round(h_nom, 5),
                "sheet_z0_ohm": round(z_n, 4),
                "sheet_eps_eff": round(e_n, 5),
            }
            doc["substrates"][f"{bd.id}:{region}"] = entry
    return doc


def data_path() -> str:
    return os.path.join(os.path.dirname(__file__), "data", "equivalent-oshpark-4l.json")


def main(argv=None) -> int:
    from yapnr.rf.coupons import jsonfmt

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=data_path())
    args = ap.parse_args(argv)
    doc = compute()
    jsonfmt.dump(doc, args.out)
    for key, v in doc["substrates"].items():
        for target in ("geometric", "coupon"):
            t = v[target]
            e, r, c = t["equivalent"], t["real"], t["check"]
            print(
                f"{key} {target}: real {r['z0_ohm']:.3f} ohm / {r['eps_eff']:.4f};"
                f" er' {e['er']:.4f} h' {e['h_mm']:.4f} mm tan_d' {e['tan_delta']:.5f} (check"
                f" {c['z0_ohm']:.3f} / {c['eps_eff']:.4f})"
            )
        n = v["nominal"]
        print(f"{key} nominal sheet: {n['sheet_z0_ohm']:.3f} ohm / {n['sheet_eps_eff']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
