"""openEMS run of the PA-corner model (board_pa.py, D14 / E1-PA): does the macro's VOUT_PA feed
change the TX1 and RX4 launches?

Stack down to L4: L1 a conducting sheet (GND fill, the kept lines and lands, the PA copper, the
dummy load's run-in), RO4835 LoPro 0.1016 mm, L2 a PEC sheet with the board's apertures (launch
cut-outs, the PA anti-pads), RO4450F 0.096 mm, L3 a PEC sheet with the PA anti-pads, FR-4 0.51 mm
(Dk 4.3, Df 0.015), L4 the PEC floor (the 1V0 plane, an RF ground through its decoupling). GND
vias are PEC posts L3 to L1; the PA vias run from L1 through the anti-pads and end
- `--pa short`: on L4 (the bottom caps as an RF short),
- `--pa open`: 0.1 mm above L4,
- `--pa port`: 0.05 mm above L4 on a 50 ohm lumped port each (the island's termination; the power
  it takes is the coupling into the PA feed).
Ports: 50 ohm lumped ports at the ball lands (L1 land to L3 through the L2 cut-out: the vendor
boundary Pb), MSL ports on the synthetic leads (from the north edge, measurement plane at the
cut, Pf); a dummy load in the box is a 50 ohm lumped termination. PML_8 on the sides and the top,
PEC below L4.

  python3 openems/pa_sim.py MODEL.json --out OUT --excite TX1.Pb|RX4.Pb|all [--pa short|open|port]
      [--threads 8] [--res 0.020] [--lossless] [--mesh-ref OTHER.json]

`--excite all` drives both lands at once (S of each line relative to its own land's incident
wave; their mutual coupling is part of what is compared). Outputs OUT/result.json (S at the
band frequencies, the largest in-band change is computed against another run by the analysis),
OUT/s.csv and OUT/waves.npz.
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
H_FR4, DK_FR4, DF_FR4, T_L3 = 0.51, 4.3, 0.015, 0.0175


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
    z_l3 = 0.0
    z_l2 = s["h_bond"] + s["t_l2"]
    z_l1 = z_l2 + s["h_core"]
    z_l4 = -(T_L3 + H_FR4)
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
    zone = 0.9  # PML zone inside the box edges
    z_top = z_l1 + a.air + 8 * cell_air
    w = 2 * math.pi * f0
    ll = 0.0 if a.lossless else 1.0
    core = csx.AddMaterial(
        "RO4835", epsilon=s["dk_core"], kappa=ll * w * EPS0 * s["dk_core"] * s["df_core"]
    )
    bond = csx.AddMaterial(
        "RO4450F", epsilon=s["dk_bond"], kappa=ll * w * EPS0 * s["dk_bond"] * s["df_bond"]
    )
    fr4 = csx.AddMaterial("FR4", epsilon=DK_FR4, kappa=ll * w * EPS0 * DK_FR4 * DF_FR4)
    pec = csx.AddMetal("PEC")
    k_r = cf.roughness_factor(s["rq_l1_um"], f0)
    cu = (
        pec
        if a.lossless
        else csx.AddConductingSheet("L1", conductivity=5.8e7 / k_r**2, thickness=s["t_l1"] * 1e-3)
    )
    fr4.AddBox([x0, y0, z_l4], [x1, y1, z_l3], priority=1)
    bond.AddBox([x0, y0, z_l3], [x1, y1, z_l2], priority=1)
    core.AddBox([x0, y0, z_l2], [x1, y1, z_l1], priority=1)
    pec.AddBox([x0, y0, z_l3], [x1, y1, z_l3], priority=10)  # L3
    pec.AddBox([x0, y0, z_l2], [x1, y1, z_l2], priority=10)  # L2
    for pp in m["l2_holes"]:
        bond.AddPolygon(np.array(pp).T, "z", z_l2, priority=20)
    ar = m.get("pa_antipad_r") or 0.35
    for vx, vy, dr, pad in m["pa_vias"]:  # L3 anti-pads (L2's are holes of the In1 fill)
        n = 16
        ring = [
            (vx + ar * math.cos(2 * math.pi * k / n), vy + ar * math.sin(2 * math.pi * k / n))
            for k in range(n)
        ]
        fr4.AddPolygon(np.array(ring).T, "z", z_l3, priority=20)
    polys = list(m["gnd"]) + [pp for v in m["nets"].values() for pp in v]
    for pp in polys:
        cu.AddPolygon(np.array(pp).T, "z", z_l1, priority=30)
    for vx, vy, dr in m["vias"]:
        pec.AddBox([vx - dr / 2, vy - dr / 2, z_l3], [vx + dr / 2, vy + dr / 2, z_l1], priority=50)
    z_end = {"short": z_l4, "open": z_l4 + 0.1, "port": z_l4 + 0.05}[a.pa]
    for vx, vy, dr, pad in m["pa_vias"]:
        pec.AddBox([vx - dr / 2, vy - dr / 2, z_end], [vx + dr / 2, vy + dr / 2, z_l1], priority=50)

    # mesh: fixed lines on axis-parallel copper edges in the inner box (1/3-2/3 pair), uniform
    # res*1.6 fill, graded x1.3 to 0.12 mm in the PML zones; 4 cells across the core and the bond
    res, fine = a.res, a.res / 3
    ix0, ix1, iy0, iy1 = x0 + zone, x1 - zone, y0 + zone, y1 - zone
    mx, my = set(), set()
    edge_polys = list(polys)
    ref_vias = []
    for ref in [r for r in (a.mesh_ref or "").split(",") if r]:  # one mesh template (A/B)
        o = json.load(open(ref))
        edge_polys += list(o["gnd"]) + [pp for v in o["nets"].values() for pp in v]
        ref_vias += list(o["pa_vias"])
    for pp in edge_polys:
        n = len(pp)
        for i in range(n):
            (ax_, ay_), (bx_, by_) = pp[i], pp[(i + 1) % n]
            if abs(ax_ - bx_) < 1e-6 and abs(ay_ - by_) > 0.03 and ix0 <= ax_ <= ix1:
                mx.add(round(ax_, 4))
            if abs(ay_ - by_) < 1e-6 and abs(ax_ - bx_) > 0.03 and iy0 <= ay_ <= iy1:
                my.add(round(ay_, 4))
    lx, ly = [], []
    for e in mx:
        lx += [e - fine, e + 2 * fine]
    for e in my:
        ly += [e - fine, e + 2 * fine]
    for pt in m["land_ports"] + m.get("loads", []):
        lx += [pt["at"][0] - pt["half"], pt["at"][0] + pt["half"]]
        ly += [pt["at"][1] - pt["half"], pt["at"][1] + pt["half"]]
    for pt in m["ports"]:
        lx += [pt["at"][0]]
        ly += [pt["at"][1]]
    for vx, vy, dr, pad in list(m["pa_vias"]) + ref_vias:
        lx += [vx - dr / 2, vx + dr / 2]
        ly += [vy - dr / 2, vy + dr / 2]
    lx = dedupe(lx + [ix0, ix1], res / 2)
    ly = dedupe(ly + [iy0, iy1], res / 2)
    fx = [v for v in np.arange(ix0, ix1, res * 1.6) if min(abs(v - u) for u in lx) > res / 2]
    fy = [v for v in np.arange(iy0, iy1, res * 1.6) if min(abs(v - u) for u in ly) > res / 2]
    grid.AddLine("x", sorted(lx + fx + [x0, x1]))
    grid.AddLine("y", sorted(ly + fy + [y0, y1]))
    grid.SmoothMeshLines("x", 0.12, 1.3)
    grid.SmoothMeshLines("y", 0.12, 1.3)
    zl = list(np.linspace(z_l2, z_l1, 5)) + list(np.linspace(z_l3, z_l2, 5))
    zl += list(np.linspace(z_l4, z_l3, 6)) + [z_end, z_top]
    grid.AddLine("z", sorted(set(round(v, 6) for v in zl)))
    grid.SmoothMeshLines("z", cell_air, 1.3)
    gx, gy, gz = (np.array(grid.GetLines(d)) for d in "xyz")

    w50 = 0.200
    ports = {}
    exc_all = a.excite == "all"
    for pt in m["land_ports"]:
        cx, cy, h = pt["at"][0], pt["at"][1], pt["half"]
        ex = -1 if (pt["name"] == a.excite or exc_all) else 0
        ports[pt["name"]] = fdtd.AddLumpedPort(
            len(ports) + 1,
            50.0,
            [cx - h, cy - h, z_l3],
            [cx + h, cy + h, z_l1],
            "z",
            excite=ex,
            priority=45,
        )
    for pt in m["ports"]:  # leads from the north edge, wave into -y
        px, py = pt["at"]
        feed = float(y1 - gy[-11])
        ports[pt["name"]] = fdtd.AddMSLPort(
            len(ports) + 1,
            pec,
            [px - w50 / 2, y1, z_l1],
            [px + w50 / 2, py, z_l2],
            "y",
            "z",
            excite=0,
            FeedShift=feed,
            MeasPlaneShift=y1 - py,
            priority=40,
        )
    loads = {}
    for k, ld in enumerate(m.get("loads", [])):
        cx, cy, h = ld["at"][0], ld["at"][1], ld["half"]
        loads[ld["ref"]] = fdtd.AddLumpedPort(
            100 + k,
            50.0,
            [cx - h, cy - h, z_l2],
            [cx + h, cy + h, z_l1],
            "z",
            excite=0,
            priority=45,
        )
    pa_ports = {}
    if a.pa == "port":
        for k, (vx, vy, dr, pad) in enumerate(m["pa_vias"]):
            pa_ports[f"PA{k}"] = fdtd.AddLumpedPort(
                200 + k,
                50.0,
                [vx - dr / 2, vy - dr / 2, z_l4],
                [vx + dr / 2, vy + dr / 2, z_end],
                "z",
                excite=0,
                priority=45,
            )
    meta = dict(
        mesh=dict(nx=len(gx), ny=len(gy), nz=len(gz)),
        cells=int(len(gx) * len(gy) * len(gz)),
        min_cell_um=dict(
            x=float(np.min(np.diff(gx)) * 1e3),
            y=float(np.min(np.diff(gy)) * 1e3),
            z=float(np.min(np.diff(gz)) * 1e3),
        ),
        z=dict(l1=z_l1, l2=z_l2, l3=z_l3, l4=z_l4, pa_via_end=z_end),
        pa=a.pa,
    )
    return fdtd, ports, loads, pa_ports, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--excite", required=True)
    ap.add_argument("--pa", default="short", choices=["short", "open", "port"])
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--res", type=float, default=0.020)
    ap.add_argument("--air", type=float, default=1.2)
    ap.add_argument("--max-steps", type=int, default=200000)
    ap.add_argument("--end-db", type=float, default=1e-4)
    ap.add_argument("--lossless", action="store_true")
    ap.add_argument(
        "--mesh-ref", help="comma-separated models whose copper edges join the mesh (A/B pairs)"
    )
    ap.add_argument("--setup-only", action="store_true")
    a = ap.parse_args()
    m = json.load(open(a.model))
    out = os.path.abspath(a.out)
    sim = os.path.join(out, "sim")
    if os.path.exists(sim):
        shutil.rmtree(sim)
    os.makedirs(sim)
    fdtd, ports, loads, pa_ports, meta = build(m, a)
    print(
        f"mesh {meta['mesh']} = {meta['cells'] / 1e6:.2f} M cells, min {meta['min_cell_um']}",
        flush=True,
    )
    if a.setup_only:
        json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
        shutil.rmtree(sim, ignore_errors=True)
        return
    t0 = time.time()
    fdtd.Run(sim, cleanup=True, numThreads=a.threads)
    wall = time.time() - t0
    f = np.arange(54.0, 70.0001, 0.025) * 1e9
    for p in list(ports.values()) + list(loads.values()) + list(pa_ports.values()):
        p.CalcPort(sim, f, ref_impedance=50)
    multi = a.excite == "all"
    res = dict(
        model=m["model"],
        board=m["board"],
        pa=a.pa,
        excite=a.excite,
        lossless=a.lossless,
        wall_s=wall,
        meta=meta,
        s={},
        pa_coupling_db=None,
    )
    cols, hdr = [f / 1e9], ["f_ghz"]
    store = dict(f=f)
    for name, p in ports.items():
        line = name.split(".")[0]
        pe = ports[f"{line}.Pb"] if multi else ports[a.excite]
        sv = p.uf_ref / pe.uf_inc
        db = 20 * np.log10(np.abs(sv))
        cols += [db, np.degrees(np.unwrap(np.angle(sv)))]
        hdr += [f"s_{name}_db", f"s_{name}_deg"]
        store[f"s_{name}"] = sv
        res["s"][name] = dict(
            relative_to=f"{line}.Pb" if multi else a.excite,
            db_at={
                f"{x:.2f}": float(np.interp(x * 1e9, f, db))
                for x in (54, 57, 60.3, 62.05, 63.8, 66, 70)
            },
            max_db=float(db.max()),
        )
    if pa_ports:
        pe = ports[a.excite] if not multi else list(ports.values())[0]
        pw = sum(np.abs(p.uf_ref) ** 2 for p in pa_ports.values()) / np.abs(pe.uf_inc) ** 2
        db = 10 * np.log10(pw)
        cols += [db]
        hdr += ["pa_coupling_db"]
        store["pa_coupling"] = pw
        res["pa_coupling_db"] = dict(
            max_54_70=float(db.max()),
            db_at={f"{x:.2f}": float(np.interp(x * 1e9, f, db)) for x in (60.3, 62.05, 63.8)},
        )
    for name, p in loads.items():
        pe = ports[a.excite] if not multi else list(ports.values())[0]
        store[f"load_{name}"] = np.abs(p.uf_ref) ** 2 / np.abs(pe.uf_inc) ** 2
    np.savetxt(
        os.path.join(out, "s.csv"),
        np.array(cols).T,
        delimiter=",",
        header=",".join(hdr),
        fmt="%.6g",
    )
    np.savez(os.path.join(out, "waves.npz"), **store)
    json.dump(res, open(os.path.join(out, "result.json"), "w"), indent=1, default=float)
    shutil.rmtree(sim, ignore_errors=True)
    print(json.dumps({k: v for k, v in res.items() if k != "meta"}, indent=1, default=float)[:4000])


if __name__ == "__main__":
    main()
