"""openEMS ring-down of a plane-pair window (board_pp.py): are there resonances of the stitched L1
pour over L2 (the 4 mil core) or of the L2-L3 bondply in 57-70 GHz with Q >= 20? (D15 hard gate;
Palace eigenmode is the cross-check once it is ready.)

Stack as ant_sim.py: L3 the PEC floor, RO4450F 0.096 mm (+ the L2 copper gap), L2 a PEC sheet
with the board's apertures in the window, RO4835 LoPro 0.1016 mm, L1 a conducting sheet (GND and
any line in the window), through vias PEC posts L3 to L1, air 1.2 mm above. The substrate and
every line run into PML_8 on the four sides (energy that is not trapped leaves the window), PML
on top. Two soft E-field sources (z) drive both pairs at the window's centre at once (a solid L2
keeps them apart; an aperture couples them, which is what a real board does); voltage probes
L2-L1 and L3-L2 at three points record the fields. After the pulse (Gaussian 54-71 GHz) the
probe signals are decomposed into damped sinusoids (matrix pencil): frequency, Q and amplitude of
every pole in 54-72 GHz; the late-time level (dB below the probe's peak at 0.5, 1 and 2 ns after
the pulse) is reported as well.

  python3 openems/pp_sim.py MODEL.json --out OUT [--threads 8] [--res 0.027] [--t-ns 4]

Outputs OUT/result.json (poles per probe, Q >= 20 in 57-70 GHz flagged, late-time levels, mesh)
and OUT/probes.npz (time series, decimated).
"""

import argparse
import json
import math
import os
import shutil
import sys
import time

import numpy as np

sys.path.insert(
    0, os.environ.get("RFMACRO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
from rfmacro import closedform as cf  # noqa: E402
from rfmacro.params import STACK  # noqa: E402

EPS0 = 8.8541878128e-12
C0 = 299792458.0
F0, FC = 62.5e9, 8.5e9


def matrix_pencil(t, y, fmin, fmax, rel=1e-3):
    """Damped complex exponentials of y(t) (uniform t): returns [(f, Q, |a|)] in [fmin, fmax]."""
    n = len(y)
    if n < 20:
        return []
    L = n // 3
    Y = np.array([y[i : i + L + 1] for i in range(n - L)])
    U, S, Vh = np.linalg.svd(Y, full_matrices=False)
    m = int(np.sum(S > rel * S[0]))
    m = max(2, min(m, L - 1, 80))
    V = Vh.conj().T[:, :m]
    V1, V2 = V[:-1, :], V[1:, :]
    z = np.linalg.eigvals(np.linalg.pinv(V1) @ V2)
    dt = t[1] - t[0]
    s = np.log(z.astype(complex)) / dt
    # amplitudes by least squares
    Z = np.vander(z, n, increasing=True).T
    amp = np.linalg.lstsq(Z, y.astype(complex), rcond=None)[0]
    out = []
    for sk, ak in zip(s, amp):
        f = sk.imag / (2 * math.pi)
        alpha = -sk.real
        if not (fmin <= f <= fmax):
            continue
        q = math.pi * f / alpha if alpha > 0 else float("inf")
        out.append((float(f), float(q), float(abs(ak))))
    out.sort(key=lambda r: -r[2])
    return out


def build(m, a):
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    s = STACK
    z_l2 = s["h_bond"] + s["t_l2"]
    z_l1 = z_l2 + s["h_core"]
    x0, y0, x1, y1 = m["window"]
    cell_air = C0 / 72e9 * 1e3 / 18
    res = a.res
    dt_est = res * 1e-3 / (C0 * math.sqrt(3)) * 0.5  # rough, for the step count
    nts = int(a.t_ns * 1e-9 / dt_est)
    fdtd = openEMS(NrTS=nts, EndCriteria=1e-12)
    fdtd.SetGaussExcite(F0, FC)
    fdtd.SetBoundaryCond(["PML_8"] * 4 + ["PEC", "PML_8"])
    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    grid = csx.GetGrid()
    grid.SetDeltaUnit(1e-3)
    w = 2 * math.pi * 62e9
    core = csx.AddMaterial(
        "RO4835", epsilon=s["dk_core"], kappa=w * EPS0 * s["dk_core"] * s["df_core"]
    )
    bond = csx.AddMaterial(
        "RO4450F", epsilon=s["dk_bond"], kappa=w * EPS0 * s["dk_bond"] * s["df_bond"]
    )
    k_r = cf.roughness_factor(s["rq_l1_um"], 62e9)
    cu = csx.AddConductingSheet("L1", conductivity=5.8e7 / k_r**2, thickness=s["t_l1"] * 1e-3)
    pec = csx.AddMetal("PEC")
    pad = 1.2  # graded margin (x1.3 to 0.12 mm) holding the 8 PML cells
    X0, X1, Y0, Y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    bond.AddBox([X0, Y0, 0.0], [X1, Y1, z_l2], priority=1)
    core.AddBox([X0, Y0, z_l2], [X1, Y1, z_l1], priority=1)
    pec.AddBox([X0, Y0, z_l2], [X1, Y1, z_l2], priority=10)  # L2 ...
    for pp in m["l2_holes"]:  # ... with the board's apertures
        bond.AddPolygon(np.array(pp).T, "z", z_l2, priority=20)
    for pp in list(m["gnd"]) + [q for v in m["nets"].values() for q in v]:
        cu.AddPolygon(np.array(pp).T, "z", z_l1, priority=30)
    for vx, vy, dr in m["vias"]:
        pec.AddBox([vx - dr / 2, vy - dr / 2, 0.0], [vx + dr / 2, vy + dr / 2, z_l1], priority=50)
    # sources: z-directed soft E boxes, one per pair, at the window's centre
    sx, sy = m["source"]
    hh = res
    for name, za, zb in (("core", z_l2, z_l1), ("bond", 0.0, z_l2)):
        ex = csx.AddExcitation(f"src_{name}", exc_type=0, exc_val=[0, 0, 1])
        ex.AddBox([sx - hh, sy - hh, za], [sx + hh, sy + hh, zb])
    probes = []
    for k, (px, py) in enumerate(m["probes"]):
        for name, za, zb in (("core", z_l2, z_l1), ("bond", 0.0, z_l2)):
            nm = f"v_{name}_{k}"
            pr = csx.AddProbe(nm, p_type=0)
            pr.AddBox([px, py, za], [px, py, zb])
            probes.append(dict(name=nm, at=[px, py], pair=name))
    # mesh: uniform res over the window, graded (x1.3) to 0.12 mm in the margin that holds the PML;
    # 4 + 4 cells across the bond and the core
    lx = list(np.arange(x0, x1 + res / 2, res)) + [X0, X1]
    ly = list(np.arange(y0, y1 + res / 2, res)) + [Y0, Y1]
    grid.AddLine("x", lx)
    grid.AddLine("y", ly)
    grid.SmoothMeshLines("x", 0.12, 1.3)
    grid.SmoothMeshLines("y", 0.12, 1.3)
    zl = list(np.linspace(0, z_l2, 5)) + list(np.linspace(z_l2, z_l1, 5)) + [z_l1 + a.air]
    grid.AddLine("z", sorted(set(round(v, 6) for v in zl)))
    grid.SmoothMeshLines("z", cell_air, 1.3)
    gx, gy, gz = (np.array(grid.GetLines(d)) for d in "xyz")
    meta = dict(
        mesh=dict(nx=len(gx), ny=len(gy), nz=len(gz)),
        cells=int(len(gx) * len(gy) * len(gz)),
        res_um=res * 1e3,
        steps_max=nts,
        probes=probes,
    )
    return fdtd, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--res", type=float, default=0.027)
    ap.add_argument("--t-ns", type=float, default=4.0)
    ap.add_argument("--air", type=float, default=1.2)
    ap.add_argument("--setup-only", action="store_true")
    a = ap.parse_args()
    m = json.load(open(a.model))
    out = os.path.abspath(a.out)
    sim = os.path.join(out, "sim")
    if os.path.exists(sim):
        shutil.rmtree(sim)
    os.makedirs(sim)
    fdtd, meta = build(m, a)
    print(f"mesh {meta['mesh']} = {meta['cells'] / 1e6:.2f} M cells", flush=True)
    if a.setup_only:
        json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
        shutil.rmtree(sim, ignore_errors=True)
        return
    t0 = time.time()
    fdtd.Run(sim, cleanup=True, numThreads=a.threads)
    wall = time.time() - t0
    t_pulse = 2 * 9 / (2 * math.pi * FC) * 1.0  # openEMS Gaussian: 9 / (2 pi fc) either side
    res = dict(
        model=m["model"],
        board=m["board"],
        window=m["window"],
        wall_s=wall,
        meta=meta,
        probes={},
        gate=dict(q_min=20, band_ghz=[57, 70]),
    )
    store = {}
    worst = None
    for pr in meta["probes"]:
        fn = os.path.join(sim, pr["name"])
        if not os.path.exists(fn):
            continue
        tv = np.loadtxt(fn, comments="%")
        t, v = tv[:, 0], tv[:, 1]
        pk = float(np.max(np.abs(v))) or 1.0
        late = {}
        for dt_ns in (0.5, 1.0, 2.0):
            sel = t > t_pulse + dt_ns * 1e-9
            late[f"{dt_ns}ns"] = (
                float(20 * np.log10(np.max(np.abs(v[sel])) / pk + 1e-30)) if sel.any() else None
            )
        sel = t > t_pulse + 0.05e-9
        ts, vs = t[sel], v[sel]
        step = max(1, int(round((1 / (4 * 80e9)) / (t[1] - t[0]))))
        ts, vs = ts[::step], vs[::step]
        if len(vs) > 1200:
            ts, vs = ts[:1200], vs[:1200]
        poles = matrix_pencil(ts - ts[0], vs, 54e9, 72e9)
        trapped = [
            dict(f_ghz=round(f / 1e9, 3), q=round(q, 1), amp_rel=round(amp / pk, 5))
            for f, q, amp in poles
            if 57e9 <= f <= 70e9 and q >= 20 and amp / pk > 1e-4
        ]
        res["probes"][pr["name"]] = dict(
            at=pr["at"],
            pair=pr["pair"],
            peak_v=pk,
            late_db=late,
            poles=[
                dict(f_ghz=round(f / 1e9, 3), q=round(q, 1), amp_rel=round(amp / pk, 5))
                for f, q, amp in poles[:12]
            ],
            trapped_q20=trapped,
        )
        store[pr["name"]] = v[::4]
        store["t"] = t[::4]
        for tr in trapped:
            if worst is None or tr["q"] > worst["q"]:
                worst = dict(tr, probe=pr["name"])
    res["worst_trapped"] = worst
    res["gate_pass"] = worst is None
    np.savez(os.path.join(out, "probes.npz"), **store)
    json.dump(res, open(os.path.join(out, "result.json"), "w"), indent=1)
    shutil.rmtree(sim, ignore_errors=True)
    print(json.dumps({k: v for k, v in res.items() if k != "meta"}, indent=1)[:4000])


if __name__ == "__main__":
    main()
