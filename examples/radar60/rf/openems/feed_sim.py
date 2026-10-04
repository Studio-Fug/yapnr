"""openEMS model of the feeds of the Board A macro cut from its zone-filled KiCad board
(board_export.py, then board_feeds.py).

Stack as in stage 2 (openems/column_sim.py): RO4835 LoPro 0.1016 mm (Dk 3.56, Df 0.0037) over
L2; L1 copper (feeds, GND pour as filled by KiCad, via pads) a zero-thickness conducting sheet
with sigma/K^2 (Hammerstad K at 62 GHz); through vias PEC posts (drill as a square). Feed models
(no windows): L2 is the PEC bottom boundary. Column models (windows): RO4450F 0.096 mm + L2
copper gap filled under the windows, L2 PEC sheet with the windows, L3 the PEC bottom boundary.
The model box is truncated by PML_8 on the sides and the top (air 1.2 mm), the substrate and
every line run into it (matched line ends); inside the PML zones L1 copper is PEC. Ports: 50 ohm
MSL ports (openEMS MSLPort) at the macro ports P0 (lines heading east, lead from the west edge)
and P1 (column inputs, lead from the north edge), measurement planes at P0/P1.

  python3 openems/feed_sim.py MODEL.json --out OUT --excite TX1.P0|all [--threads 4] [--res 0.025] [--dump]

`--excite all` drives every P0 at once; S of each line is then taken relative to its own P0's
incident wave (valid where the feed-to-feed coupling is small, as it is here: <= -39 dB [S]).
Ports from the south (`ys`) are P0s heading north (RX). Dummy loads are 50 ohm lumped
terminations from the load's signal pad to L2.

Outputs OUT/result.json, OUT/s.csv (|S| dB and phase of every port for this excitation), and with
--dump the frequency-domain E field at mid-substrate (OUT/efield_mid.h5).
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
from openEMS.utilities import DFT_time2freq  # noqa: E402
from rfmacro import closedform as cf  # noqa: E402
from rfmacro.params import STACK  # noqa: E402

EPS0 = 8.8541878128e-12
C0 = 299792458.0


def dedupe(v, tol):
    out = []
    for x in sorted(v):
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def build(m, a):
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    s = STACK
    layered = bool(m["l2_windows"])
    z_l2 = s["h_bond"] + s["t_l2"] if layered else 0.0
    z_l1 = z_l2 + s["h_core"]
    f0, fc = 62.0e9, 9.0e9
    cell_air = C0 / 66e9 * 1e3 / 20
    fdtd = openEMS(NrTS=a.max_steps, EndCriteria=a.end_db)
    fdtd.SetGaussExcite(f0, fc)
    fdtd.SetBoundaryCond(["PML_8"] * 4 + ["PEC", "PML_8"])
    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    grid = csx.GetGrid()
    grid.SetDeltaUnit(1e-3)
    x0, x1, y0, y1 = m["box"]
    ix0, iy0, ix1, iy1 = m["inner"]
    z_top = z_l1 + a.air + 8 * cell_air

    w = 2 * math.pi * f0
    lossy = 0.0 if a.lossless else 1.0  # --lossless: what is still lost is radiated or leaked
    core = csx.AddMaterial(
        "RO4835", epsilon=s["dk_core"], kappa=lossy * w * EPS0 * s["dk_core"] * s["df_core"]
    )
    k_r = cf.roughness_factor(s["rq_l1_um"], f0)
    pec = csx.AddMetal("PEC")
    if a.lossless:
        cu = pec
    else:
        cu = csx.AddConductingSheet("L1", conductivity=5.8e7 / k_r**2, thickness=s["t_l1"] * 1e-3)
    core.AddBox([x0, y0, z_l2], [x1, y1, z_l1], priority=1)
    if layered:
        bond = csx.AddMaterial(
            "RO4450F", epsilon=s["dk_bond"], kappa=lossy * w * EPS0 * s["dk_bond"] * s["df_bond"]
        )
        bond.AddBox([x0, y0, 0.0], [x1, y1, z_l2], priority=1)
        pec.AddBox([x0, y0, z_l2], [x1, y1, z_l2], priority=10)
        for wx0, wy0, wx1, wy1 in m["l2_windows"]:
            bond.AddBox([wx0, wy0, z_l2], [wx1, wy1, z_l2], priority=20)
    polys = []
    for sheet, zone in [m["gnd"]] + list(m["nets"].values()):
        for pp in sheet:
            cu.AddPolygon(np.array(pp).T, "z", z_l1, priority=30)
            polys.append(pp)
        for pp in zone:
            pec.AddPolygon(np.array(pp).T, "z", z_l1, priority=31)
            polys.append(pp)
    for vx, vy, dr in m["vias"]:
        pec.AddBox([vx - dr / 2, vy - dr / 2, 0.0], [vx + dr / 2, vy + dr / 2, z_l1], priority=50)

    # mesh: fixed lines on straight axis-parallel copper and window edges inside the inner box
    # (1/3-2/3 pair), uniform res*1.6 fill in the inner box, graded (x1.3) to 0.12 mm in the PML
    # zones and to lambda0/20 in the air
    res, fine = a.res, a.res / 3
    mx, my = set(), set()
    if m.get("mesh_ref"):  # mesh from a reference geometry (same grid for a variant pair)
        mr = json.load(open(m["mesh_ref"]))
        polys = [
            pp for sheet, zone in [mr["gnd"]] + list(mr["nets"].values()) for pp in sheet + zone
        ]
    for pp in polys:
        n = len(pp)
        for i in range(n):
            (ax_, ay_), (bx_, by_) = pp[i], pp[(i + 1) % n]
            if abs(ax_ - bx_) < 1e-6 and abs(ay_ - by_) > 0.03 and ix0 <= ax_ <= ix1:
                mx.add(round(ax_, 4))
            if abs(ay_ - by_) < 1e-6 and abs(ax_ - bx_) > 0.03 and iy0 <= ay_ <= iy1:
                my.add(round(ay_, 4))
    for sl in m.get("slivers", []):  # same lines with and without the slivers (A/B, C/D)
        bx0, by0, bx1, by1 = sl["bounds"]
        if bx1 - bx0 > by1 - by0:
            my.update(round(v, 4) for v in (by0, by1))
        else:
            mx.update(round(v, 4) for v in (bx0, bx1))
    for wx0, wy0, wx1, wy1 in m["l2_windows"]:
        mx.update(v for v in (wx0, wx1) if ix0 <= v <= ix1)
        my.update(v for v in (wy0, wy1) if iy0 <= v <= iy1)
    lx, ly = [], []
    for e in mx:
        lx += [e - fine, e + 2 * fine]
    for e in my:
        ly += [e - fine, e + 2 * fine]
    for pt in m["ports"]:
        if pt["dir"] == "x":
            ly += [pt["at"][1]]
        else:
            lx += [pt["at"][0]]
    for ld in m.get("loads", []):  # lumped 50 ohm terminations of the dummy loads
        lx += [ld["at"][0] - ld["half"], ld["at"][0] + ld["half"]]
        ly += [ld["at"][1] - ld["half"], ld["at"][1] + ld["half"]]
    lx = dedupe(lx + [ix0, ix1], res / 2)
    ly = dedupe(ly + [iy0, iy1], res / 2)
    fx = [v for v in np.arange(ix0, ix1, res * 1.6) if min(abs(v - u) for u in lx) > res / 2]
    fy = [v for v in np.arange(iy0, iy1, res * 1.6) if min(abs(v - u) for u in ly) > res / 2]
    grid.AddLine("x", sorted(lx + fx + [x0, x1]))
    grid.AddLine("y", sorted(ly + fy + [y0, y1]))
    grid.SmoothMeshLines("x", a.zone_cell, 1.3)
    grid.SmoothMeshLines("y", a.zone_cell, 1.3)
    zl = list(np.linspace(z_l2, z_l1, a.nz_core + 1))
    if layered:
        zl += list(np.linspace(0.0, z_l2, 5))
    zl += [z_top]
    grid.AddLine("z", sorted(set(round(v, 6) for v in zl)))
    grid.SmoothMeshLines("z", cell_air, 1.3)
    gx, gy, gz = (np.array(grid.GetLines(d)) for d in "xyz")
    # the PML is the outer 8 cells: it has to stay inside the zones
    pml = dict(w=gx[8], e=gx[-9], s=gy[8], n=gy[-9], top=gz[-9])
    assert pml["w"] <= ix0 + 1e-6 and pml["e"] >= ix1 - 1e-6, pml
    assert pml["s"] <= iy0 + 1e-6 and pml["n"] >= iy1 - 1e-6, pml

    w50 = 0.200
    ports = {}
    p0s = [pt["name"] for pt in m["ports"] if pt["name"].endswith(".P0")]
    for pt in m["ports"]:
        px, py = pt["at"]
        exc = -1 if (pt["name"] == a.excite or (a.excite == "all" and pt["name"] in p0s)) else 0
        if pt["dir"] == "x":  # P0: lead from the west edge, wave into +x
            feed = float(gx[10] - x0)
            assert x0 + feed < px - 0.3, (feed, px)
            ports[pt["name"]] = fdtd.AddMSLPort(
                len(ports) + 1,
                pec,
                [x0, py - w50 / 2, z_l1],
                [px, py + w50 / 2, z_l2],
                "x",
                "z",
                excite=exc,
                FeedShift=feed,
                MeasPlaneShift=px - x0,
                priority=40,
            )
        elif pt["dir"] == "ys":  # P0 heading north: lead from the south edge, wave into +y
            feed = float(gy[10] - y0)
            assert y0 + feed < py - 0.3, (feed, py)
            ports[pt["name"]] = fdtd.AddMSLPort(
                len(ports) + 1,
                pec,
                [px - w50 / 2, y0, z_l1],
                [px + w50 / 2, py, z_l2],
                "y",
                "z",
                excite=exc,
                FeedShift=feed,
                MeasPlaneShift=py - y0,
                priority=40,
            )
        else:  # P1: lead from the north edge, wave into -y
            feed = float(y1 - gy[-11])
            ports[pt["name"]] = fdtd.AddMSLPort(
                len(ports) + 1,
                pec,
                [px - w50 / 2, y1, z_l1],
                [px + w50 / 2, py, z_l2],
                "y",
                "z",
                excite=exc,
                FeedShift=feed,
                MeasPlaneShift=y1 - py,
                priority=40,
            )
    loads = []
    for k, ld in enumerate(m.get("loads", [])):
        cx, cy, h = ld["at"][0], ld["at"][1], ld["half"]
        loads.append(
            fdtd.AddLumpedPort(
                100 + k,
                ld["R"],
                [cx - h, cy - h, z_l2],
                [cx + h, cy - h + 2 * h, z_l1],
                "z",
                excite=0,
                priority=45,
            )
        )
    # E-field point probes at mid-substrate along each sliver (both ends and the middle): the
    # sliver voltage spectrum, normalized to the incident port voltage, shows its resonances
    probes = []
    zm = 0.5 * (z_l2 + z_l1)
    for k, sl in enumerate(m.get("slivers", [])):
        bx0, by0, bx1, by1 = sl["bounds"]
        cx, cy = sl["centroid"]
        if bx1 - bx0 > by1 - by0:
            pts = [(bx0 + 0.04, cy), (cx, cy), (bx1 - 0.04, cy)]
        else:
            pts = [(cx, by0 + 0.04), (cx, cy), (cx, by1 - 0.04)]
        for j, (px, py) in enumerate(pts):
            nm = f"slv{k}_{j}"
            pr = csx.AddProbe(nm, p_type=2)
            pr.AddBox([px, py, zm], [px, py, zm])
            probes.append(dict(name=nm, at=[px, py], sliver=k))
    dump = None
    if a.dump:
        zm = 0.5 * (z_l2 + z_l1)
        dump = csx.AddDump(
            "efield_mid", dump_type=10, frequency=[f * 1e9 for f in a.dump_f], file_type=1
        )
        dump.AddBox([ix0, iy0, zm], [ix1, iy1, zm])
    meta = dict(
        mesh=dict(nx=len(gx), ny=len(gy), nz=len(gz)),
        cells=int(len(gx) * len(gy) * len(gz)),
        min_cell_um=dict(
            x=float(np.min(np.diff(gx)) * 1e3),
            y=float(np.min(np.diff(gy)) * 1e3),
            z=float(np.min(np.diff(gz)) * 1e3),
        ),
        pml=pml,
        z=dict(l2=z_l2, l1=z_l1, top=z_top),
        probes=probes,
        k_rough=k_r,
    )
    return fdtd, ports, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--excite", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--res", type=float, default=0.025)
    ap.add_argument("--zone-cell", type=float, default=0.12)
    ap.add_argument("--nz-core", type=int, default=4, help="cells across the 4 mil core")
    ap.add_argument("--air", type=float, default=1.2)
    ap.add_argument("--max-steps", type=int, default=150000)
    ap.add_argument("--end-db", type=float, default=1e-4)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--dump-f", type=float, nargs="+", default=[58.0, 60.0, 62.0, 64.0, 66.0])
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument(
        "--lossless", action="store_true", help="PEC L1, lossless dielectrics (radiation only)"
    )
    ap.add_argument("--post-only", action="store_true", help="post-process an existing OUT/sim")
    a = ap.parse_args()
    m = json.load(open(a.model))
    out = os.path.abspath(a.out)
    sim = os.path.join(out, "sim")
    if not a.post_only:
        if os.path.exists(sim):
            shutil.rmtree(sim)
        os.makedirs(sim)
    fdtd, ports, meta = build(m, a)
    print(
        f"mesh {meta['mesh']} = {meta['cells'] / 1e6:.2f} M cells, min cell {meta['min_cell_um']}",
        flush=True,
    )
    if a.setup_only:
        json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1, default=float)
        return
    t0 = time.time()
    if not a.post_only:
        fdtd.Run(sim, cleanup=True, numThreads=a.threads)
    wall = time.time() - t0
    f = np.arange(54.0, 70.0001, 0.025) * 1e9  # excitation 53-71 GHz; the report uses 58-66
    zline = {}
    for name, p in ports.items():
        p.CalcPort(sim, f)  # line impedance and beta from the port's own fields
        zline[name] = np.array(p.Z_ref)
        p.CalcPort(sim, f, ref_impedance=50)
    multi = a.excite == "all"
    pe = ports[a.excite] if not multi else None
    cols, hdr = [f / 1e9], ["f_ghz"]
    res = dict(
        model=m["model"],
        variant=m["variant"],
        excite=a.excite,
        lossless=a.lossless,
        wall_s=wall,
        meta=meta,
        ports={},
    )
    for name, p in ports.items():
        if multi:  # every line driven at its P0: S of line n relative to n.P0's incident wave
            pe = ports[name.split(".")[0] + ".P0"]
        s = p.uf_ref / pe.uf_inc
        cols += [20 * np.log10(np.abs(s)), np.degrees(np.unwrap(np.angle(s)))]
        hdr += [f"s_{name}_db", f"s_{name}_deg"]
        inc = 20 * np.log10(np.abs(p.uf_inc / pe.uf_inc)) if p is not pe else np.zeros_like(f)
        cols += [inc]
        hdr += [f"a_{name}_db"]
        res["ports"][name] = dict(
            measplane_shift_mm=float(p.measplane_shift),
            z_line_62=[
                float(np.interp(62e9, f, np.real(zline[name]))),
                float(np.interp(62e9, f, np.imag(zline[name]))),
            ],
            eeff_62=float(np.interp(62e9, f, (np.real(p.beta) * C0 / (2 * np.pi * f)) ** 2)),
            s_db_at={
                f"{x:.1f}": float(np.interp(x * 1e9, f, 20 * np.log10(np.abs(s))))
                for x in (58, 60.3, 62.05, 63.8, 66)
            },
            s_db_max=float(np.max(20 * np.log10(np.abs(s)))),
            incident_max_db=float(np.max(inc)) if p is not pe else None,
        )
    np.savetxt(
        os.path.join(out, "s.csv"),
        np.array(cols).T,
        delimiter=",",
        header=",".join(hdr),
        fmt="%.6g",
    )
    np.savez(
        os.path.join(out, "waves.npz"),
        f=f,
        **{f"inc_{k}": v.uf_inc for k, v in ports.items()},
        **{f"ref_{k}": v.uf_ref for k, v in ports.items()},
        **{f"zline_{k}": v for k, v in zline.items()},
        **{f"beta_{k}": v.beta for k, v in ports.items()},
    )
    vin = (ports[a.excite] if not multi else list(ports.values())[0]).uf_inc
    hcore = meta["z"]["l1"] - meta["z"]["l2"]
    pspec = {}
    for pr in meta["probes"]:
        fn = os.path.join(sim, pr["name"])
        if not os.path.exists(fn):
            continue
        tz = np.loadtxt(fn, comments="%")
        t, ez = tz[:, 0], tz[:, 3]
        ezf = DFT_time2freq(t, ez, f)  # the convention of the port voltages
        pspec[pr["name"]] = ezf * hcore * 1e-3 / vin
    if pspec:
        np.savez(os.path.join(out, "probes.npz"), f=f, **pspec)
        res["probes"] = {
            k: dict(
                at=[pr["at"] for pr in meta["probes"] if pr["name"] == k][0],
                max_db=float(np.max(20 * np.log10(np.abs(v)))),
                f_max=float(f[np.argmax(np.abs(v))] / 1e9),
            )
            for k, v in pspec.items()
        }
    if a.dump:
        src = os.path.join(sim, "efield_mid.h5")
        if os.path.exists(src):
            shutil.copy(src, os.path.join(out, "efield_mid.h5"))
    json.dump(res, open(os.path.join(out, "result.json"), "w"), indent=1, default=float)
    shutil.rmtree(sim, ignore_errors=True)
    print(json.dumps({k: v for k, v in res.items() if k != "meta"}, indent=1, default=float))


if __name__ == "__main__":
    main()
