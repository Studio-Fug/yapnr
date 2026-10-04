"""openEMS model of a radiating piece of the regenerated macro (board_ant.py): one column with its
entry, or a bank of columns with terminated dummies.

Stack, boundaries, ports, mesh and far field follow stage 2's column model
(examples/radar60/rf/openems/column_sim.py) so that the results compare like for like:
- L3 PEC sheet at z 0, RO4450F 0.096 mm + L2 copper gap filled (bond) up to L2, L2 PEC sheet with
  the radiator windows (bond in the windows), RO4835 LoPro 0.1016 mm, L1 a zero-thickness
  conducting sheet with sigma/K^2 (Hammerstad K at 62 GHz); through vias are PEC posts
  (drill as a square) from L3 to L1; finite board (board_ant.py: 0.5 lambda0 beyond the copper);
  0.3 lambda0 of air and PML_8 on all six sides; NF2FF box 0.15 lambda0 outside the board.
- Ports: 50 ohm MSL ports at each fed column's Pg (lead 1.2 mm from the south, matched source
  Feed_R 50, measurement plane at Pg); every port not excited is a matched termination. Dummy
  loads: 50 ohm lumped ports (signal pad to L2), not excited.
- Mesh: fixed lines on every straight copper edge and window edge inside the fine box
  (1/3-2/3 pair at res/3), uniform res*1.6 fill inside it, graded x1.3 to lambda0/18 outside;
  4 + 4 cells across the bond and the core.

  python3 openems/ant_sim.py MODEL.json --excite TX2.Pg --out OUT [--threads 4] [--res 0.025]

Outputs OUT/result.json (S of every port, RL-10 band of the excited port, far field per band
frequency: broadside directivity and realized gain, radiation efficiency, HPBW, E- and H-plane
cuts) and OUT/s.csv.
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
from rfmacro.params import F_HI, F_LO, STACK  # noqa: E402

EPS0 = 8.8541878128e-12


def dedupe(v, tol):
    out = []
    for x in sorted(v):
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def model(m, args):
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    s = STACK
    z_l2 = s["h_bond"] + s["t_l2"]
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

    sx0, sy0, sx1, sy1 = m["sub"]
    bx0, by0, bx1, by1 = m["cbox"]
    cell_air = lam0 / 18
    pml = 8 * cell_air
    air = 0.3 * lam0 + pml
    ax0, ax1, ay0, ay1 = sx0 - air, sx1 + air, sy0 - air, sy1 + air
    az0, az1 = -(0.2 * lam0 + pml), z_l1 + air
    nfm = 0.15 * lam0

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
    for wx0, wy0, wx1, wy1 in m["windows"]:  # ... with the radiator windows
        bond.AddBox([wx0, wy0, z_l2], [wx1, wy1, z_l2], priority=20)
    polys = list(m["gnd"]) + [pp for v in m["nets"].values() for pp in v]
    for pp in polys:
        cu.AddPolygon(np.array(pp).T, "z", z_l1, priority=30)
    for vx, vy, dr in m["vias"]:
        pec.AddBox([vx - dr / 2, vy - dr / 2, 0.0], [vx + dr / 2, vy + dr / 2, z_l1], priority=50)
    for vx, vy, dr in m.get("shorts", []):  # shorted dummy loads: signal land to L3
        pec.AddBox([vx - dr / 2, vy - dr / 2, 0.0], [vx + dr / 2, vy + dr / 2, z_l1], priority=50)

    res = args.res
    fine = res / 3
    mx, my = set(), set()
    for pp in polys:
        n = len(pp)
        for i in range(n):
            (xa, ya), (xb, yb) = pp[i], pp[(i + 1) % n]
            if abs(xa - xb) < 1e-6 and abs(ya - yb) > 0.03 and bx0 <= xa <= bx1:
                mx.add(round(xa, 4))
            if abs(ya - yb) < 1e-6 and abs(xa - xb) > 0.03 and by0 <= ya <= by1:
                my.add(round(ya, 4))
    for wx0, wy0, wx1, wy1 in m["windows"]:
        mx.update(round(v, 4) for v in (wx0, wx1))
        my.update(round(v, 4) for v in (wy0, wy1))
    lx, ly = [], []
    for e in sorted(mx):
        lx += [e - fine, e + 2 * fine]
    for e in sorted(my):
        ly += [e - fine, e + 2 * fine]
    for pt in m["ports"]:
        lx += [pt["at"][0]]
        ly += [pt["start_y"], pt["at"][1]]
    for ld in m["loads"]:
        lx += [ld["at"][0] - ld["half"], ld["at"][0] + ld["half"]]
        ly += [ld["at"][1] - ld["half"], ld["at"][1] + ld["half"]]
    lx += [bx0, bx1, sx0, sx1, ax0, ax1]
    ly += [by0, by1, sy0, sy1, ay0, ay1]
    # x lines repeat at the column pitch over each bank's periodic span (review 2026-10-04): the
    # lines of every period are folded into one period, merged, and laid out again in each, and
    # the fill inside a span is a whole number of cells per period, so every column of a bank
    # sees the same x mesh (edge and interior columns compare without a mesh difference)
    spans = [tuple(v) for v in m.get("periods", {}).values()]
    lx = dedupe(lx, res / 2)
    fill_x = res * 1.6
    px = []
    for lo, hi, dd in spans:
        us = dedupe([(x - lo) % dd for x in lx if lo - 1e-9 <= x <= hi + 1e-9], res / 2)
        if us and us[-1] > dd - res / 2:  # wrap-around duplicate of u = 0
            us = us[:-1]
        nper = int(round((hi - lo) / dd))
        nf = int(math.ceil(dd / fill_x))
        uf = [dd * i / nf for i in range(nf)]
        uf = (
            [u for u in uf if min(min(abs(u - w), dd - abs(u - w)) for w in us) > res / 2]
            if us
            else uf
        )
        for k in range(nper + 1):
            px += [lo + k * dd + u for u in us + uf if lo + k * dd + u <= hi + 1e-9]
    inside = [(lo - res / 2, hi + res / 2) for lo, hi, _ in spans]
    lx = [x for x in lx if not any(a < x < b for a, b in inside)]
    lx = sorted(set(round(x, 6) for x in lx + px))
    ly = dedupe(ly, res / 2)
    grid.AddLine("x", lx)
    grid.AddLine("y", ly)
    zl = list(np.linspace(0, z_l2, 5)) + list(np.linspace(z_l2, z_l1, 5))
    zl += [az0, az1, z_l1 + 0.3, -0.3]
    grid.AddLine("z", sorted(set(round(v, 6) for v in zl)))
    fx = np.arange(bx0, bx1, fill_x)
    fy = np.arange(by0, by1, fill_x)
    fx = [
        v
        for v in fx
        if min(abs(v - u) for u in lx) > res / 2 and not any(a < v < b for a, b in inside)
    ]
    fy = [v for v in fy if min(abs(v - u) for u in ly) > res / 2]
    if fx:
        grid.AddLine("x", fx)
    if fy:
        grid.AddLine("y", fy)
    for axn in ("x", "y", "z"):
        grid.SmoothMeshLines(axn, cell_air, 1.3)

    w50 = 0.200
    ports = {}
    for k, pt in enumerate(m["ports"]):
        px, py = pt["at"]
        y_s = pt["start_y"]
        ports[pt["name"]] = fdtd.AddMSLPort(
            k + 1,
            pec,
            [px - w50 / 2, y_s, z_l1],
            [px + w50 / 2, py, z_l2],
            "y",
            "z",
            excite=-1 if pt["name"] == args.excite else 0,
            FeedShift=0.15,
            Feed_R=50.0,
            MeasPlaneShift=py - y_s,
            priority=40,
        )
    loads = {}
    for k, ld in enumerate(m["loads"]):
        cx, cy, h = ld["at"][0], ld["at"][1], ld["half"]
        loads[ld["ref"]] = fdtd.AddLumpedPort(
            100 + k,
            ld["R"],
            [cx - h, cy - h, z_l2],
            [cx + h, cy + h, z_l1],
            "z",
            excite=0,
            priority=45,
        )
    nf = fdtd.CreateNF2FFBox(
        start=[sx0 - nfm, sy0 - nfm, -nfm], stop=[sx1 + nfm, sy1 + nfm, z_l1 + nfm]
    )
    if args.dump_bond:  # E at mid-bondply (L2-L3 parallel plate), frequency domain
        zb = s["h_bond"] / 2
        dump = csx.AddDump(
            "e_bond", dump_type=10, frequency=[f * 1e9 for f in (60.3, 62.05, 63.8)], file_type=1
        )
        dump.AddBox([sx0, sy0, zb], [sx1, sy1, zb])
    gx, gy, gz = (np.array(grid.GetLines(dd)) for dd in "xyz")
    meta = dict(
        mesh=dict(nx=len(gx), ny=len(gy), nz=len(gz)),
        cells=int(len(gx) * len(gy) * len(gz)),
        min_cell_um=dict(
            x=float(np.min(np.diff(gx)) * 1e3),
            y=float(np.min(np.diff(gy)) * 1e3),
            z=float(np.min(np.diff(gz)) * 1e3),
        ),
        k_rough=k_r,
        substrate_mm=[sx1 - sx0, sy1 - sy0],
    )
    if args.setup_only:
        meta["x_lines"] = [round(float(v), 5) for v in gx]
    return fdtd, ports, loads, nf, meta


def hpbw(theta, cut):
    pk = int(np.argmax(cut))
    lvl = cut[pk] - 3
    lo = pk
    while lo > 0 and cut[lo] > lvl:
        lo -= 1
    hi = pk
    while hi < len(cut) - 1 and cut[hi] > lvl:
        hi += 1
    return float(theta[hi] - theta[lo])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--excite", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--res", type=float, default=0.025)
    ap.add_argument("--max-steps", type=int, default=150000)
    ap.add_argument("--end-db", type=float, default=1e-4)
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--dump-bond", action="store_true", help="E at mid-bondply (e_bond.h5)")
    a = ap.parse_args()
    m = json.load(open(a.model))
    out = os.path.abspath(a.out)
    sim = os.path.join(out, "sim")
    if os.path.exists(sim):
        shutil.rmtree(sim)
    os.makedirs(sim)
    fdtd, ports, loads, nf, meta = model(m, a)
    print(
        f"mesh {meta['mesh']} = {meta['cells'] / 1e6:.2f} M cells, min cell {meta['min_cell_um']}",
        flush=True,
    )
    if a.setup_only:
        json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1, default=float)
        shutil.rmtree(sim, ignore_errors=True)
        return
    t0 = time.time()
    fdtd.Run(sim, cleanup=True, numThreads=a.threads)
    wall = time.time() - t0
    f = np.linspace(58e9, 66e9, 321)
    pe = ports[a.excite]
    for p in list(ports.values()) + list(loads.values()):
        p.CalcPort(sim, f, ref_impedance=50)
    cols, hdr = [f / 1e9], ["f_ghz"]
    res = dict(
        model=m["model"],
        col=m.get("col"),
        board=m["board"],
        excite=a.excite,
        wall_s=wall,
        meta=meta,
        s={},
        loads={},
    )
    for name, p in ports.items():
        sv = p.uf_ref / pe.uf_inc
        db = 20 * np.log10(np.abs(sv))
        cols += [db, np.degrees(np.unwrap(np.angle(sv))), np.real(sv), np.imag(sv)]
        hdr += [f"s_{name}_db", f"s_{name}_deg", f"s_{name}_re", f"s_{name}_im"]
        res["s"][name] = dict(
            db_at={
                f"{x:.2f}": float(np.interp(x * 1e9, f, db)) for x in (58, 60.3, 62.05, 63.8, 66)
            },
            max_db=float(db.max()),
        )
    for name, p in loads.items():  # power into each dummy load, relative to the incident power
        frac = (np.abs(p.uf_ref) ** 2) / (np.abs(pe.uf_inc) ** 2)
        db = 10 * np.log10(frac)
        cols += [db]
        hdr += [f"load_{name}_db"]
        res["loads"][name] = dict(
            db_at={f"{x:.2f}": float(np.interp(x * 1e9, f, db)) for x in (60.3, 62.05, 63.8)},
            max_db=float(db.max()),
        )
    s11 = 20 * np.log10(np.abs(pe.uf_ref / pe.uf_inc))
    zin = pe.uf_tot / pe.if_tot
    cols += [np.real(zin), np.imag(zin)]
    hdr += ["re_zin", "im_zin"]
    ok = s11 <= -10
    band, i = [], 0
    while i < len(f):
        if ok[i]:
            j = i
            while j + 1 < len(f) and ok[j + 1]:
                j += 1
            band.append([f[i] / 1e9, f[j] / 1e9])
            i = j + 1
        else:
            i += 1
    imin = int(np.argmin(s11))
    res.update(
        s11_min_db=float(s11[imin]),
        f_s11_min_ghz=float(f[imin] / 1e9),
        rl10_bands_ghz=band,
        s11_db_at={f"{x / 1e9:.2f}": float(np.interp(x, f, s11)) for x in (F_LO, 62.05e9, F_HI)},
        far_field={},
    )
    np.savetxt(
        os.path.join(out, "s.csv"),
        np.array(cols).T,
        delimiter=",",
        header=",".join(hdr),
        fmt="%.6g",
    )
    pc = m["phase_centres"][a.excite.split(".")[0]]
    theta = np.arange(-180.0, 180.5, 1.0)
    for fx in (F_LO, 62.05e9, F_HI):
        pe.CalcPort(sim, [fx], ref_impedance=50)
        p_inc = float(np.real(pe.P_inc[0]))
        p_acc = float(np.real(pe.P_acc[0]))
        r_e = nf.CalcNF2FF(
            sim,
            fx,
            theta,
            [90.0],
            center=[pc[0] * 1e-3, pc[1] * 1e-3, 0],
            outfile=f"nf2ff_e_{fx / 1e9:.2f}.h5",
        )
        e_cut = 20 * np.log10(r_e.E_norm[0][:, 0] / np.max(r_e.E_norm[0])) + 10 * np.log10(
            r_e.Dmax[0]
        )
        r_h = nf.CalcNF2FF(
            sim,
            fx,
            theta,
            [0.0],
            center=[pc[0] * 1e-3, pc[1] * 1e-3, 0],
            outfile=f"nf2ff_h_{fx / 1e9:.2f}.h5",
        )
        h_cut = 20 * np.log10(r_h.E_norm[0][:, 0] / np.max(r_h.E_norm[0])) + 10 * np.log10(
            r_h.Dmax[0]
        )
        prad = float(r_e.Prad[0])
        dmax = float(r_e.Dmax[0])
        # complex cuts: E * r, normalised so |E|^2 / (2 eta0) integrates to P_rad / P_inc
        cplx = {}
        for tag, rr in (("e", r_e), ("h", r_h)):
            for comp in ("E_theta", "E_phi"):
                ev = np.asarray(getattr(rr, comp)[0])[:, 0] / math.sqrt(p_inc)
                cplx[f"{tag}_{comp}"] = [
                    [round(float(v.real), 6), round(float(v.imag), 6)] for v in ev[::2]
                ]
        i0 = int(np.argmin(np.abs(theta)))
        d_bs = float(e_cut[i0])
        res["far_field"][f"{fx / 1e9:.2f}"] = dict(
            dmax_dbi=10 * math.log10(dmax),
            d_broadside_dbi=d_bs,
            rad_efficiency=prad / p_acc,
            p_rad_over_p_inc=prad / p_inc,
            p_acc_over_p_inc=p_acc / p_inc,
            realized_gain_broadside_dbi=d_bs + 10 * math.log10(prad / p_inc),
            e_plane_hpbw_deg=hpbw(theta, e_cut),
            h_plane_hpbw_deg=hpbw(theta, h_cut),
            h_plane_gain_45_rel_db=float(np.interp(45, theta, h_cut) - d_bs),
            h_plane_gain_m45_rel_db=float(np.interp(-45, theta, h_cut) - d_bs),
            e_plane_peak_deg=float(theta[int(np.argmax(e_cut))]),
            h_plane_peak_deg=float(theta[int(np.argmax(h_cut))]),
            cut_theta_deg=theta[::2].tolist(),
            e_cut_dbi=[round(v, 2) for v in e_cut[::2].tolist()],
            h_cut_dbi=[round(v, 2) for v in h_cut[::2].tolist()],
            # the NF2FF phase reference, in metres: openEMS reads `center` in the dumps' unit (m);
            # runs before 2026-10-04 passed millimetres (the reference sat metres away: magnitudes
            # unaffected, phases re-referenced in post-processing)
            nf2ff_centre_m=[pc[0] * 1e-3, pc[1] * 1e-3, 0.0],
            complex_cuts=cplx,
        )
    with open(os.path.join(out, "result.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=float)
    if a.dump_bond and os.path.exists(os.path.join(sim, "e_bond.h5")):
        shutil.copy(os.path.join(sim, "e_bond.h5"), os.path.join(out, "e_bond.h5"))
    shutil.rmtree(sim, ignore_errors=True)
    print(
        json.dumps(
            {k: v for k, v in res.items() if k not in ("meta", "far_field")},
            indent=1,
            default=float,
        )
    )
    for k, v in res["far_field"].items():
        print(
            k,
            {
                kk: (round(vv, 3) if isinstance(vv, float) else None)
                for kk, vv in v.items()
                if not kk.endswith(("cut_dbi", "theta_deg"))
            },
        )


if __name__ == "__main__":
    main()
