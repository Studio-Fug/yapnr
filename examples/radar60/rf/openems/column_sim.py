"""openEMS model of one Board A column (two patches, corporate divider, gap input) at 60-64 GHz.

The copper comes from the same generator as the KiCad macro (`rfmacro.macro.build_column`), in
the column frame (phase centre at the origin, +y along the column, +z up). Stack, from L3 up:
RO4450F bondply (filling the removed L2 copper under the windows), the L2 GND sheet with the
radiator windows, the RO4835 LoPro core, and L1. L1 copper is a zero-thickness conducting sheet
(openEMS' surface-impedance sheet model) with σ/K² for the Hammerstad roughness factor K, so
the run includes conductor and dielectric loss; L2 and L3 are PEC. The port is a 50 Ω
microstrip port (openEMS MSLPort) on the input line, de-embedded to P1. The substrate and ground
are finite (about λ0/2 beyond the copper) with PML_8 on every side; the NF2FF box encloses them.

Run inside the image built from ./Dockerfile:

  python3 openems/column_sim.py --out OUT [--threads 4] [--res 0.025] [--variant 0]

Outputs OUT/result.json (S11, RL-10 dB band, directivity, radiation efficiency, realized gain,
E- and H-plane cuts at 60.3/62.05/63.8 GHz) and OUT/s11.csv. Sources: openEMS
(https://www.openems.de, GPL-3.0), the MSL port and NF2FF of its Python interface.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rfmacro import closedform as cf  # noqa: E402
from rfmacro import dims as dims_mod  # noqa: E402
from rfmacro.geom import outline  # noqa: E402
from rfmacro.macro import build_column  # noqa: E402
from rfmacro.params import F_HI, F_LO, STACK, resolve  # noqa: E402

EPS0 = 8.8541878128e-12


def model(args):
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    p = resolve({"variant": args.variant})
    for kv in args.set:
        k, v = kv.split("=", 1)
        p[k] = json.loads(v)
    d = dims_mod.compute(p)
    col = build_column("COL", "RF", (0.0, 0.0), False, p, d)

    s = STACK
    z_l2 = s["h_bond"] + s["t_l2"]  # top of the L2 copper (the 4 mil lines' reference)
    z_l1 = z_l2 + s["h_core"]
    f0, fc = 62.0e9, 9.0e9
    lam0 = 299792458.0 / f0 * 1e3

    fdtd = openEMS(NrTS=args.max_steps, EndCriteria=args.end_db)
    fdtd.SetGaussExcite(f0, fc)
    fdtd.SetBoundaryCond(["PML_8"] * 6)
    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    grid = csx.GetGrid()
    grid.SetDeltaUnit(1e-3)

    # copper outline in the column frame
    polys = [list(pp) for pp in col.patches]
    w50 = float(p["w50"])
    for pth in col.paths:
        polys.append(outline(pth, step=0.01))
    ty, w35 = float(p["t_y"]), float(p["w35"])
    polys.append(
        [
            (-w50 / 2, ty - w35 / 2),
            (w50 / 2, ty - w35 / 2),
            (w50 / 2, ty + w35 / 2),
            (-w50 / 2, ty + w35 / 2),
        ]
    )
    # arms reach 20 um into the patch at the notch bottom (overlap, not touch)
    for pth in col.paths[:2]:
        end = pth.pos
        sgn = 1 if pth.segs[-1].p1[1] > 0 else -1
        polys.append(
            [
                (end[0] - w50 / 2, end[1]),
                (end[0] + w50 / 2, end[1]),
                (end[0] + w50 / 2, end[1] + sgn * 0.02),
                (end[0] - w50 / 2, end[1] + sgn * 0.02),
            ]
        )
    xs = [q[0] for pp in polys for q in pp]
    ys = [q[1] for pp in polys for q in pp]
    xin, p1y = float(p["in_x"]), float(p["p1_y"])
    lport = 1.2
    y_port0 = p1y - lport
    bx0, bx1 = min(xs), max(xs)
    by0, by1 = min(min(ys), y_port0), max(ys)
    m_sub = 0.5 * lam0  # substrate and ground beyond the copper
    sx0, sx1, sy0, sy1 = bx0 - m_sub, bx1 + m_sub, by0 - m_sub, by1 + m_sub
    cell_air = lam0 / 18
    pml = 8 * cell_air
    air = 0.3 * lam0 + pml  # 0.3 λ0 of free air between the substrate and the PML
    ax0, ax1, ay0, ay1 = sx0 - air, sx1 + air, sy0 - air, sy1 + air
    az0, az1 = -(0.2 * lam0 + pml), z_l1 + air
    nfm = 0.15 * lam0  # NF2FF box this far outside the substrate (in free air, outside the PML)

    # materials
    w = 2 * math.pi * f0
    core = csx.AddMaterial(
        "RO4835", epsilon=s["dk_core"], kappa=w * EPS0 * s["dk_core"] * s["df_core"]
    )
    bond = csx.AddMaterial(
        "RO4450F", epsilon=s["dk_bond"], kappa=w * EPS0 * s["dk_bond"] * s["df_bond"]
    )
    k_r = cf.roughness_factor(s["rq_l1_um"], f0)
    cu = csx.AddConductingSheet("L1", conductivity=5.8e7 / k_r**2, thickness=s["t_l1"] * 1e-3)
    pec = csx.AddMetal("PEC")
    bond.AddBox([sx0, sy0, 0.0], [sx1, sy1, z_l2], priority=1)
    core.AddBox([sx0, sy0, z_l2], [sx1, sy1, z_l1], priority=1)
    pec.AddBox([sx0, sy0, 0.0], [sx1, sy1, 0.0], priority=10)  # L3
    pec.AddBox([sx0, sy0, z_l2], [sx1, sy1, z_l2], priority=10)  # L2 ...
    for win in col.windows:  # ... with the radiator windows (bondply wins on the sheet)
        wx = [q[0] for q in win]
        wy = [q[1] for q in win]
        bond.AddBox([min(wx), min(wy), z_l2], [max(wx), max(wy), z_l2], priority=20)
    names = ["patch_lo", "patch_hi", "arm_n", "arm_s", "input", "tee", "lap_n", "lap_s"]
    for nm, pp in zip(names, polys):
        prop = (
            cu
            if not args.debug_names
            else csx.AddConductingSheet(nm, conductivity=5.8e7 / k_r**2, thickness=s["t_l1"] * 1e-3)
        )
        prop.AddPolygon(np.array(pp).T, "z", z_l1, priority=30)

    stitch = []
    if (
        args.stitch > 0
    ):  # through GND vias (0.15 drill as a square post, 0.30 L1 pad) round each window
        for win in col.windows:
            wx = [q[0] for q in win]
            wy = [q[1] for q in win]
            off = 0.15 + 0.05  # pad radius + 50 um from the window edge
            x0, x1, y0, y1 = min(wx) - off, max(wx) + off, min(wy) - off, max(wy) + off
            per = 2 * (x1 - x0 + y1 - y0)
            n = max(4, int(round(per / args.stitch)))
            for i in range(n):
                t = i * per / n
                for L, a, b in (
                    (x1 - x0, (x0, y0), (1, 0)),
                    (y1 - y0, (x1, y0), (0, 1)),
                    (x1 - x0, (x1, y1), (-1, 0)),
                    (y1 - y0, (x0, y1), (0, -1)),
                ):
                    if t <= L:
                        stitch.append((a[0] + b[0] * t, a[1] + b[1] * t))
                        break
                    t -= L
        # skip posts that would land on or next to the divider and input copper
        feed = polys[2:]
        stitch = [
            q
            for q in stitch
            if all(
                not (
                    min(x for x, _ in pp) - 0.35 < q[0] < max(x for x, _ in pp) + 0.35
                    and min(y for _, y in pp) - 0.35 < q[1] < max(y for _, y in pp) + 0.35
                )
                for pp in feed
            )
        ]
        for q in stitch:
            pec.AddBox(
                [q[0] - 0.075, q[1] - 0.075, 0.0], [q[0] + 0.075, q[1] + 0.075, z_l1], priority=50
            )
            cu.AddPolygon(
                np.array(
                    [
                        [q[0] - 0.15, q[0] + 0.15, q[0] + 0.15, q[0] - 0.15],
                        [q[1] - 0.15, q[1] - 0.15, q[1] + 0.15, q[1] + 0.15],
                    ]
                ),
                "z",
                z_l1,
                priority=30,
            )

    # mesh: fixed lines on every copper and window edge (1/3-2/3 around the edge), smoothed
    res = args.res
    fine = res / 3
    mx, my = set(), set()
    # polygon vertices of arcs give many near-duplicate edges; keep straight-edge coordinates
    for pp in polys:
        n = len(pp)
        for i in range(n):
            a, b = pp[i], pp[(i + 1) % n]
            if abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) > 0.03:
                mx.add(round(a[0], 4))
            if abs(a[1] - b[1]) < 1e-6 and abs(a[0] - b[0]) > 0.03:
                my.add(round(a[1], 4))
    for win in col.windows:
        mx.update(round(q[0], 4) for q in win)
        my.update(round(q[1], 4) for q in win)
    lines_x, lines_y = [], []
    for e in sorted(mx):
        lines_x += [e - fine, e + 2 * fine]
    for e in sorted(my):
        lines_y += [e - fine, e + 2 * fine]
    lines_x += [xin, bx0 - 0.2, bx1 + 0.2, sx0, sx1, ax0, ax1]
    lines_y += [y_port0, p1y, by0 - 0.2, by1 + 0.2, sy0, sy1, ay0, ay1]
    lines_x = _dedupe(sorted(lines_x), res / 2)
    lines_y = _dedupe(sorted(lines_y), res / 2)
    grid.AddLine("x", lines_x)
    grid.AddLine("y", lines_y)
    nz_b, nz_c = 4, 4
    zl = list(np.linspace(0, z_l2, nz_b + 1)) + list(np.linspace(z_l2, z_l1, nz_c + 1))
    zl += [az0, az1, z_l1 + 0.3, -0.3]
    grid.AddLine("z", sorted(set(round(v, 6) for v in zl)))
    # uniform fill at res*1.6 inside the copper box, graded (x1.3) to λ0/18 in the air
    fx = np.arange(bx0 - 0.2, bx1 + 0.2, res * 1.6)
    fy = np.arange(by0 - 0.2, by1 + 0.2, res * 1.6)
    grid.AddLine("x", [v for v in fx if min(abs(v - u) for u in lines_x) > res / 2])
    grid.AddLine("y", [v for v in fy if min(abs(v - u) for u in lines_y) > res / 2])
    for ax in ("x", "y", "z"):
        grid.SmoothMeshLines(ax, cell_air, 1.3)
    port = fdtd.AddMSLPort(
        1,
        pec,
        [xin - w50 / 2, y_port0, z_l1],
        [xin + w50 / 2, p1y, z_l2],
        "y",
        "z",
        excite=-1,
        FeedShift=0.15,
        Feed_R=50.0,  # matched source: the open line end behind the feed must not reflect
        MeasPlaneShift=lport,
        priority=40,
    )
    nf = fdtd.CreateNF2FFBox(
        start=[sx0 - nfm, sy0 - nfm, -nfm], stop=[sx1 + nfm, sy1 + nfm, z_l1 + nfm]
    )
    meta = dict(
        params={k: v for k, v in p.items()},
        dims=d,
        arms=col.arm_lengths,
        mesh=dict(
            nx=len(grid.GetLines("x")), ny=len(grid.GetLines("y")), nz=len(grid.GetLines("z"))
        ),
        k_rough=k_r,
        stitch_posts=len(stitch),
        substrate_mm=[sx1 - sx0, sy1 - sy0],
    )
    return fdtd, csx, port, nf, meta


def _dedupe(v, tol):
    out = []
    for x in v:
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument(
        "--res", type=float, default=0.025, help="mm, finest in-plane cell near copper x 3"
    )
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=120000)
    ap.add_argument("--end-db", type=float, default=1e-4)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--debug-names", action="store_true")
    ap.add_argument(
        "--stitch",
        type=float,
        default=0.0,
        help="L2-L3 via ring pitch round each window (mm); 0 = none",
    )
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    sim = os.path.join(out, "sim")
    if os.path.exists(sim):
        shutil.rmtree(sim)
    os.makedirs(sim)
    fdtd, csx, port, nf, meta = model(a)
    m = meta["mesh"]
    cells = m["nx"] * m["ny"] * m["nz"]
    print(f"mesh {m['nx']} x {m['ny']} x {m['nz']} = {cells / 1e6:.2f} M cells", flush=True)
    t0 = time.time()
    fdtd.Run(sim, cleanup=True, numThreads=a.threads, setup_only=a.setup_only)
    wall = time.time() - t0
    if a.setup_only:
        return
    f = np.linspace(58e9, 66e9, 161)
    port.CalcPort(sim, f, ref_impedance=50)
    s11 = port.uf_ref / port.uf_inc
    s11db = 20 * np.log10(np.abs(s11))
    zin = port.uf_tot / port.if_tot
    ok = s11db <= -10
    band = []
    i = 0
    while i < len(f):
        if ok[i]:
            j = i
            while j + 1 < len(f) and ok[j + 1]:
                j += 1
            band.append([f[i] / 1e9, f[j] / 1e9])
            i = j + 1
        else:
            i += 1
    imin = int(np.argmin(s11db))
    res = dict(
        wall_s=wall,
        cells=cells,
        s11_min_db=float(s11db[imin]),
        f_s11_min_ghz=float(f[imin] / 1e9),
        rl10_bands_ghz=band,
        s11_db_at={f"{x / 1e9:.2f}": float(np.interp(x, f, s11db)) for x in (F_LO, 62.05e9, F_HI)},
        far_field={},
    )
    theta = np.arange(-180.0, 180.5, 1.0)
    for fx in (F_LO, 62.05e9, F_HI):
        port.CalcPort(sim, [fx], ref_impedance=50)
        p_inc = float(np.real(port.P_inc[0]))
        p_acc = float(np.real(port.P_acc[0]))
        r_e = nf.CalcNF2FF(
            sim, fx, theta, [90.0], center=[0, 0, 0], outfile=f"nf2ff_e_{fx / 1e9:.2f}.h5"
        )
        e_cut = 20 * np.log10(r_e.E_norm[0][:, 0] / np.max(r_e.E_norm[0])) + 10 * np.log10(
            r_e.Dmax[0]
        )
        r_h = nf.CalcNF2FF(
            sim, fx, theta, [0.0], center=[0, 0, 0], outfile=f"nf2ff_h_{fx / 1e9:.2f}.h5"
        )
        h_cut = 20 * np.log10(r_h.E_norm[0][:, 0] / np.max(r_h.E_norm[0])) + 10 * np.log10(
            r_h.Dmax[0]
        )
        prad = float(r_e.Prad[0])
        dmax = float(r_e.Dmax[0])
        i0 = int(np.argmin(np.abs(theta)))
        d_bs = float(e_cut[i0])  # broadside directivity (dBi)
        eta = prad / p_acc
        res["far_field"][f"{fx / 1e9:.2f}"] = dict(
            dmax_dbi=10 * math.log10(dmax),
            d_broadside_dbi=d_bs,
            rad_efficiency=eta,
            p_rad_over_p_inc=prad / p_inc,
            realized_gain_broadside_dbi=d_bs + 10 * math.log10(prad / p_inc),
            e_plane_hpbw_deg=_hpbw(theta, e_cut),
            h_plane_hpbw_deg=_hpbw(theta, h_cut),
            h_plane_gain_45_rel_db=float(np.interp(45, theta, h_cut) - d_bs),
            e_plane_peak_deg=float(theta[int(np.argmax(e_cut))]),
            cut_theta_deg=theta[::5].tolist(),
            e_cut_dbi=[round(v, 2) for v in e_cut[::5].tolist()],
            h_cut_dbi=[round(v, 2) for v in h_cut[::5].tolist()],
        )
    res["meta"] = meta
    with open(os.path.join(out, "result.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=float)
    np.savetxt(
        os.path.join(out, "s11.csv"),
        np.c_[f / 1e9, s11db, np.real(zin), np.imag(zin)],
        delimiter=",",
        header="f_ghz,s11_db,re_zin,im_zin",
        fmt="%.5g",
    )
    shutil.rmtree(sim, ignore_errors=True)
    print(json.dumps({k: v for k, v in res.items() if k not in ("meta", "far_field")}, indent=1))
    for k, v in res["far_field"].items():
        print(
            k,
            {
                kk: (round(vv, 3) if isinstance(vv, float) else None)
                for kk, vv in v.items()
                if not kk.endswith(("cut_dbi", "theta_deg"))
            },
        )


def _hpbw(theta, cut):
    pk = int(np.argmax(cut))
    lvl = cut[pk] - 3
    lo = pk
    while lo > 0 and cut[lo] > lvl:
        lo -= 1
    hi = pk
    while hi < len(cut) - 1 and cut[hi] > lvl:
        hi += 1
    return float(theta[hi] - theta[lo])


if __name__ == "__main__":
    main()
