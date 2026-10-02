"""2D quasi-static cross-section solver (design §8.5, FEA design §5.3 and §7.2).

The energy method with scikit-fem P2 elements on a gmsh mesh, as in the FEA spike, extended with
dielectric blocks (the conformal solder mask), per-region filling factors and Wheeler's
incremental-inductance loss factor:

- `C` (F/m): the Maxwell capacitance matrix of the signal conductors at the real permittivities;
- `C0`: the same with every εr = 1, so L = μ0 ε0 C0⁻¹ (exact for quasi-TEM lines);
- `dC[region]` = ∂C/∂εr of each dielectric region, exact from the stationary energy
  (Hellmann-Feynman: C_ij = U_iᵀ K U_j with K linear in εr), so C = Σ εr dC + dC[air];
- `g` (1/m): Wheeler's ∂L/∂n / μ0 from a second, vacuum solve with every metal wall receded by
  δ (traces narrower and thinner by 2δ, gaps wider by 2δ, planes farther by δ) [Wheeler42];
  the conductor resistance is R = R_s g.

scikit-fem (BSD-3) and gmsh (GPL-2.0-or-later) are optional: they run in the FEA worker
environment and in the table tool (`yapnr.rf.coupons.families`), never in the fit, which reads
the shipped tables. Units: mm for geometry.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

EPS0 = 8.8541878128e-12
MU0 = 4.0e-7 * math.pi
C_LIGHT = 299792458.0

MASK_SUB, MASK_CU = 0.030, 0.015  # mm, conformal mask over substrate and over copper

# Mesh settings: (smallest element near metal, largest, growth with distance), mm.
# "fit" builds the shipped tables; "fine" checks them (and is the truth of the slow synthetic
# study). At P's nominal point the two differ by 0.005 Ω in Z0 and 4e-5 in εeff, and "fine"
# differs from a (0.0012, 0.4, 0.10) mesh by 0.003 Ω.
MESH = {"fit": (0.003, 0.6, 0.25), "fine": (0.002, 0.6, 0.15)}


def _need():
    try:
        import gmsh  # noqa: F401
        import skfem  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "the 2D cross-section solver needs scikit-fem and gmsh (the FEA worker environment); "
            "the fit itself only needs the shipped tables in yapnr/rf/coupons/data"
        ) from exc


def build(spec: dict, mesh: str = "fit") -> dict:
    """Mesh a cross-section. spec: width, slabs [(name, z0, z1)], blocks [(name, x0, x1, z0,
    z1)] (later entries override earlier ones and slabs), conductors [(name, x0, x1, z0, z1)].
    Conductors are holes; the bottom edge is the ground plane, the rest of the box is grounded."""
    _need()
    import gmsh

    lc_min, lc_max, grow = MESH[mesh]
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.model.add("xsec")
        occ = gmsh.model.occ
        W = spec["width"]
        diel = [(occ.addRectangle(-W / 2, z0, 0, W, z1 - z0), n) for n, z0, z1 in spec["slabs"]]
        diel += [
            (occ.addRectangle(x0, z0, 0, x1 - x0, z1 - z0), n)
            for n, x0, x1, z0, z1 in spec.get("blocks", [])
        ]
        cond = [
            ((2, occ.addRectangle(x0, z0, 0, x1 - x0, z1 - z0)), n)
            for n, x0, x1, z0, z1 in spec["conductors"]
        ]
        obj = [(2, t) for t, _ in diel]
        _, omap = occ.fragment(obj, [e for e, _ in cond])
        occ.synchronize()
        cond_surfs, cond_of = set(), {}
        for k, (_, n) in enumerate(cond):
            for d, t in omap[len(obj) + k]:
                cond_of[(d, t)] = n
                cond_surfs.add(t)
        owner = {}
        for k, (_, n) in enumerate(diel):  # later entries win
            for _, tt in omap[k]:
                if tt not in cond_surfs:
                    owner[tt] = n
        regions: Dict[str, set] = {}
        for tt, n in owner.items():
            regions.setdefault(n, set()).add(tt)
        cond_curves: Dict[str, set] = {}
        for (_, t), n in cond_of.items():
            for _, c in gmsh.model.getBoundary([(2, t)], oriented=False):
                cond_curves.setdefault(n, set()).add(abs(c))
        occ.remove([(2, t) for t in cond_surfs])
        occ.synchronize()
        surfs = sorted(owner)
        outer = {
            abs(c)
            for _, c in gmsh.model.getBoundary(
                [(2, t) for t in surfs], combined=True, oriented=False
            )
        }
        outer -= set().union(*cond_curves.values())
        zmin = min(s[1] for s in spec["slabs"])
        bottom, rest = set(), set()
        for c in outer:
            b = gmsh.model.getBoundingBox(1, c)
            (bottom if abs(b[1] - zmin) < 1e-9 and abs(b[4] - zmin) < 1e-9 else rest).add(c)
        fd = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(
            fd, "CurvesList", sorted(set().union(*cond_curves.values()))
        )
        gmsh.model.mesh.field.setNumber(fd, "Sampling", 400)
        fm = gmsh.model.mesh.field.add("MathEval")
        gmsh.model.mesh.field.setString(fm, "F", f"Min({lc_max}, {lc_min} + {grow}*F{fd})")
        gmsh.model.mesh.field.setAsBackgroundMesh(fm)
        for k in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
            gmsh.option.setNumber(f"Mesh.{k}", 0)
        gmsh.model.mesh.generate(2)
        ntags, coords, _ = gmsh.model.mesh.getNodes()
        idx = np.full(int(ntags.max()) + 1, -1)
        idx[ntags.astype(int)] = np.arange(len(ntags))
        pts = coords.reshape(-1, 3)[:, :2]
        tris, mat = [], []
        for n, ts in regions.items():
            for t in ts:
                _, _, en = gmsh.model.mesh.getElements(2, t)
                tri = idx[np.asarray(en[0], int)].reshape(-1, 3)
                tris.append(tri)
                mat.append(np.full(len(tri), n, dtype=object))
        nodesets = {}
        for n, cs in list(cond_curves.items()) + [("@ground", bottom), ("@outer", rest)]:
            s = set()
            for c in cs:
                nt, _, _ = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
                s |= set(idx[nt.astype(int)].tolist())
            nodesets[n] = np.array(sorted(s), dtype=int)
    finally:
        gmsh.finalize()
    tris = np.vstack(tris)
    used = np.unique(tris)
    remap = np.full(len(pts), -1)
    remap[used] = np.arange(len(used))
    nodesets = {k: remap[v[remap[v] >= 0]] if len(v) else v for k, v in nodesets.items()}
    return dict(points=pts[used], tris=remap[tris], mat=np.concatenate(mat), nodesets=nodesets)


def solve(
    m: dict, er_of: Dict[str, float], signals: Sequence[str], vacuum_only: bool = False
) -> dict:
    """C, C0 and ∂C/∂εr per region for the signal conductors; every other conductor, the bottom
    plane and the box are at 0 V."""
    _need()
    from skfem import Basis, BilinearForm, ElementTriP2, MeshTri, asm, condense
    from skfem import solve as fsolve
    from skfem.helpers import dot, grad

    @BilinearForm
    def lap(u, v, w):
        return w.k * dot(grad(u), grad(v))

    mesh = MeshTri(m["points"].T * 1e-3, m["tris"].T)
    b = Basis(mesh, ElementTriP2())
    names = sorted(set(m["mat"]))
    Kr = {}
    for n in names:
        ind = (m["mat"] == n).astype(float)
        Kr[n] = asm(lap, b, k=np.repeat(ind[:, None], b.X.shape[1], axis=1))
    f2v = mesh.facets

    def dofs(nodes):
        s = np.zeros(mesh.p.shape[1], bool)
        s[nodes] = True
        fac = np.where(s[f2v[0]] & s[f2v[1]])[0]
        d = set(b.get_dofs(fac).all().tolist()) if len(fac) else set()
        d |= set(b.nodal_dofs[0, nodes].tolist())
        return np.array(sorted(d), dtype=int)

    sig = {n: dofs(m["nodesets"][n]) for n in signals}
    others = [dofs(v) for k, v in m["nodesets"].items() if k not in signals and len(v)]
    fixed = np.unique(np.concatenate(list(sig.values()) + others))

    def run(eps: Dict[str, float]):
        K = sum(EPS0 * eps[n] * Kr[n] for n in names)
        U = []
        for n in signals:
            x = np.zeros(b.N)
            x[sig[n]] = 1.0
            U.append(fsolve(*condense(K, x=x, D=fixed)))
        U = np.array(U)
        return U, U @ (K @ U.T)

    U0, C0 = run({n: 1.0 for n in names})
    out = dict(C0=C0, ndof=int(b.N))
    if vacuum_only:
        return out
    U, C = run({n: er_of.get(n, 1.0) for n in names})
    out["C"] = C
    out["dC"] = {n: EPS0 * (U @ (Kr[n] @ U.T)) for n in names}
    return out


# --- cross-sections of the coupon families --------------------------------------------------


def outer_spec(
    w: float,
    h: float,
    t: float,
    etch: float,
    gap: Optional[float],
    mask: bool,
    mask_scale: float,
    pair_gap: Optional[float] = None,
    recede: float = 0.0,
    gnd_w: float = 1.5,
    width: float = 10.0,
    air: float = 5.0,
) -> Tuple[dict, List[str]]:
    """L1 microstrip (gap None) or GCPW, optionally an edge-coupled pair, over the plane at z=0.
    Etch: each copper edge recedes by `etch` (traces narrower, gaps wider). `recede` moves every
    metal wall by δ for Wheeler's loss factor (the mask and dielectrics stay where they were).
    Returns the spec and the signal conductor names."""
    d = recede
    we = w - 2 * etch
    z0 = h  # copper foot (on the substrate)
    zc0, zc1 = h + d, h + t - d  # receded copper
    if pair_gap is None:
        strips = [("S", -we / 2, we / 2)]
    else:
        g = pair_gap + 2 * etch
        strips = [("S1", -g / 2 - we, -g / 2), ("S2", g / 2, g / 2 + we)]
    xl, xr = strips[0][1], strips[-1][2]
    full = [(n, a, b_) for n, a, b_ in strips]
    if gap is not None:
        ge = gap + 2 * etch
        full += [("G1", xl - ge - gnd_w, xl - ge), ("G2", xr + ge, xr + ge + gnd_w)]
    conds = [(n, a + d, b_ - d, zc0, zc1) for n, a, b_ in full]
    ms, mc = MASK_SUB * mask_scale, MASK_CU * mask_scale
    slabs = [("sub", -d, z0)]
    blocks = []
    if mask:
        slabs.append(("mask", z0, z0 + ms))
        blocks = [("mask", a - mc, b_ + mc, z0, z0 + t + mc) for _, a, b_ in full]
        slabs.append(("air", z0 + ms, z0 + t + mc + air))
        # the copper volume itself, minus the receded conductor, is air (the wall moved)
        blocks += [("air", a, b_, z0, z0 + t) for _, a, b_ in full] if d > 0 else []
    else:
        slabs.append(("air", z0, z0 + t + air))
    return dict(width=width, slabs=slabs, blocks=blocks, conductors=conds), [n for n, *_ in strips]


def strip_spec(
    w: float,
    h_below: float,
    h_above: float,
    t: float,
    etch: float,
    pair_gap: Optional[float] = None,
    recede: float = 0.0,
    width: float = 8.0,
) -> Tuple[dict, List[str]]:
    """Stripline: plane at z=0, the prepreg `lo` (the trace embedded at its top, as pressed
    prepreg flows round it), the core `hi` above to the upper plane (the box top)."""
    d = recede
    we = w - 2 * etch
    z1 = h_below + t
    slabs = [("lo", -d, z1), ("hi", z1, z1 + h_above + d)]
    if pair_gap is None:
        strips = [("S", -we / 2, we / 2)]
    else:
        g = pair_gap + 2 * etch
        strips = [("S1", -g / 2 - we, -g / 2), ("S2", g / 2, g / 2 + we)]
    conds = [(n, a + d, b_ - d, h_below + d, z1 - d) for n, a, b_ in strips]
    blocks = [("lo", a, b_, h_below, z1) for _, a, b_ in strips] if d > 0 else []
    return dict(width=width, slabs=slabs, blocks=blocks, conductors=conds), [n for n, *_ in strips]


def wheeler_g(c0: np.ndarray, c0_receded: np.ndarray, recede_mm: float) -> np.ndarray:
    """Wheeler's ∂L/∂n / μ0 (1/m) per mode from the vacuum C of the nominal and receded
    geometries (L = μ0 ε0 / C0 per mode)."""
    lin = MU0 * EPS0 / np.asarray(c0)
    lre = MU0 * EPS0 / np.asarray(c0_receded)
    return (lre - lin) / (MU0 * recede_mm * 1e-3)


def modal(C: np.ndarray) -> Tuple[float, float]:
    """Even and odd per-line capacitances of a symmetric pair."""
    return float(C[0, 0] + C[0, 1]), float(C[0, 0] - C[0, 1])


def z_eps(c: float, c0: float) -> Tuple[float, float]:
    return 1.0 / (C_LIGHT * math.sqrt(c * c0)), c / c0
