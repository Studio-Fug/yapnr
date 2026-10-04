#!/usr/bin/env python3
"""openEMS models of Order 0's D1, R1, the M line and the sticks A01/A04 (D-O0-13a).

An independent 3D prediction next to yapnr.rf's: real 43 µm copper on the nominal FR408HR
stackup instead of a zero-thickness sheet on the thickness-equivalent substrate, another FDTD code
(openEMS 0.37), another mesher and other ports.

    python3 order0_em.py MODEL.json --out OUT [--excite N] [--threads T] [--res MM]
        [--nz-sub N] [--sheets] [--setup-only]

MODEL.json is one of two kinds (written by `models.py`):

- ``msl``: copper rectangles on L1 over In1.Cu (region M), the demo window's ports W/N/S/E as
  straight feeds of the port width running ``feed_mm`` out of the window. Each feed ends in a
  50 Ω lumped resistor (and, on the excited port, an E-field source) and carries the port's
  voltage and current probes ``meas_mm`` outside the window: three voltage probes from the ground
  to the strip and two current probes on loops around the whole thick strip (openEMS's MSLPort
  puts its current probe on the strip plane, which on a 43 µm strip encloses only part of its
  current). From them the line's own Zc and β (MSLPort's telegrapher formulas), and the waves are
  moved to the window edge with Re β: the solver's port plane, as yapnr.rf reports it.
- ``launch``: a 2-port stick of region M with the Cinch 142-0701-851 edge SMA on both ends
  (coax ports in the connectors' PTFE, the flange, the legs, the tab on the pin pad), the
  4-layer stack with the In1.Cu cut-out under the pad, every via of the stick, the L1 and B.Cu
  pours to the keep-back: the stick as a tier-1 (coax) calibration sees it.

Copper and loss: L1 copper is 43.18 µm thick, PEC (lossless); the conductor loss is added
afterwards from the coupon model, as for yapnr.rf's predictions (`compare.py`). ``--sheets``
(experimental, not used for the predictions) puts openEMS conducting sheets (σ 5.8e7 S/m) on
every face of the copper and on the ground: on a 0.40 mm line at 5 GHz they added only about
0.02 dB/cm, about half the smooth-copper loss a 2D model gives, so they are not trusted here.
The dielectrics are nominal FR408HR at 5 GHz as the coupon model has it (prepreg εr 3.5767,
core 3.8343, tan δ 0.00906, a conductivity fixed at 5 GHz as yapnr.rf's substrate), no solder
mask (open over RF copper on the boards).

OUT/result.json: per port, at the frequencies of the run (1-8 GHz, 10 MHz), the voltage and
current at the reference plane (V, I), the line impedance Z and β, and the waves a, b (yapnr's
convention, e^{+jωt}); the excited port; the cells, steps and wall time. The S-matrix needs every
excitation (or the model's symmetry): `post.py` assembles it, S = B A⁻¹ as yapnr.rf does.
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

C0 = 299792458.0
EPS0 = 8.8541878128e-12
F_REF = 5.0e9
SIGMA_CU = 5.8e7
# OSH Park 4-layer FR408HR (order0-design §3.1): copper and dielectric thicknesses (mm); the
# coupon model's (Djordjevic-Sarkar) εr at 5 GHz and tan δ (equivalent-oshpark-4l.json).
STACK = dict(
    t_out=0.04318,
    t_in=0.01727,
    h_pp=0.1999,
    h_core=0.9906,
    er_pp=3.5767,
    er_core=3.8343,
    tand=0.00906,
)
FREQS = np.linspace(1.0e9, 8.0e9, 701)
F0, FC = 4.5e9, 3.5e9  # Gaussian pulse: 1-8 GHz above -20 dB


def dc_free_pulse(f0: float = F0, fc: float = FC) -> str:
    """openEMS's Gaussian pulse (the same f0 and -20 dB bandwidth) less its DC content, as an
    fparser string of t (s). The launch models have no DC path from the centre conductor to
    ground (their coax ports are not resistive), so the Gaussian's DC part (2 % of its peak
    spectrum here) stayed behind as a static field and the energy never fell below -31 dB."""
    tau = 1.5174 / (math.pi * fc)  # exp(-(pi tau fc)^2) = 0.1: the -20 dB half-bandwidth
    t0 = 3.3 * tau
    k = math.exp(-((math.pi * f0 * tau) ** 2))  # cos(...) - k integrates to zero
    return (
        f"(cos({2 * math.pi * f0:.10e}*(t-{t0:.10e}))-{k:.10e})"
        f"*exp(-((t-{t0:.10e})/{tau:.10e})^2)"
    )


def kappa(er: float) -> float:
    return 2 * math.pi * F_REF * EPS0 * er * STACK["tand"]


# --- mesh helpers -------------------------------------------------------------------------------


def uniform(a: float, b: float, d: float) -> list:
    n = max(1, int(round((b - a) / d)))
    return list(np.linspace(a, b, n + 1))


def grade(lines: list, lo: float, hi: float, d0: float, dmax: float, ratio: float = 1.3) -> list:
    """`lines` (sorted, the fine region) extended to `lo` and `hi` with cells growing from d0
    by `ratio` up to `dmax`."""
    out = list(lines)
    d = d0
    x = out[0]
    while x > lo + 1e-9:
        d = min(d * ratio, dmax)
        x = max(x - d, lo)
        out.insert(0, x)
    d = d0
    x = out[-1]
    while x < hi - 1e-9:
        d = min(d * ratio, dmax)
        x = min(x + d, hi)
        out.append(x)
    return out


def merge(lines, tol=1e-6) -> list:
    out = []
    for v in sorted(lines):
        if not out or v - out[-1] > tol:
            out.append(float(v))
    return out


def smooth(fixed: list, dmax: float, ratio: float = 1.4) -> list:
    """Fill the gaps between `fixed` lines so no cell exceeds dmax and neighbours grow by at
    most `ratio` (a simple two-sided grading between fixed lines)."""
    fixed = merge(fixed)
    out = [fixed[0]]
    for a, b in zip(fixed, fixed[1:]):
        gap = b - a
        if gap <= dmax + 1e-9:
            out.append(b)
            continue
        da = out[-1] - out[-2] if len(out) > 1 else dmax
        n = max(1, int(math.ceil(gap / dmax)))
        # grow from the left neighbour's cell, at most ratio per step
        cells = []
        d = min(da * ratio, dmax)
        x = a
        while b - x > 1e-9:
            d = min(d, dmax)
            if x + d > b - 1e-9 or b - (x + d) < 0.5 * d:
                cells.append(b - x)
                break
            cells.append(d)
            x += d
            d *= ratio
        # make it symmetric enough: if the last cell is much smaller than its neighbour, re-split
        if len(cells) < n:
            cells = [gap / n] * n
        x = a
        for c in cells:
            x += c
            out.append(x)
        out[-1] = b
    return merge(out)


# --- copper as rectangles -----------------------------------------------------------------------


def runs_to_rects(mask: np.ndarray, xe: np.ndarray, ye: np.ndarray) -> list:
    """Rectangles (x0, x1, y0, y1) covering the True cells of `mask` (cells between the edges
    xe, ye): runs along x merged across rows with the same run."""
    rects = []
    open_runs: dict = {}
    ni, nj = mask.shape
    for j in range(nj + 1):
        row = []
        if j < nj:
            i = 0
            while i < ni:
                if mask[i, j]:
                    k = i
                    while k < ni and mask[k, j]:
                        k += 1
                    row.append((i, k))
                    i = k
                else:
                    i += 1
        new_open = {}
        for run in row:
            new_open[run] = open_runs.pop(run, j)
        for (i0, i1), j0 in open_runs.items():
            rects.append((xe[i0], xe[i1], ye[j0], ye[j]))
        open_runs = new_open
    return rects


def faces(mask: np.ndarray, xe: np.ndarray, ye: np.ndarray):
    """Side faces of the copper: (x, y0, y1) where the mask changes along x, (y, x0, x1) along y,
    merged into runs."""
    ni, nj = mask.shape
    pad = np.zeros((ni + 2, nj + 2), bool)
    pad[1:-1, 1:-1] = mask
    xf = []
    for i in range(ni + 1):
        diff = pad[i, 1:-1] != pad[i + 1, 1:-1]
        j = 0
        while j < nj:
            if diff[j]:
                k = j
                while k < nj and diff[k]:
                    k += 1
                xf.append((xe[i], ye[j], ye[k]))
                j = k
            else:
                j += 1
    yf = []
    for j in range(nj + 1):
        diff = pad[1:-1, j] != pad[1:-1, j + 1]
        i = 0
        while i < ni:
            if diff[i]:
                k = i
                while k < ni and diff[k]:
                    k += 1
                yf.append((ye[j], xe[i], xe[k]))
                i = k
            else:
                i += 1
    return xf, yf


# --- the thick-strip line port --------------------------------------------------------------------


def thick_msl_port(CSX, mesh, n, start, stop, prop, t_cu, excite, feed_r, meas_shift, feed_shift):
    """MSLPort (openEMS.ports) with its current probes on loops around the whole thick strip
    (y across the strip with two cells to spare, z from half the substrate to two cells above
    the copper) instead of a line on the strip plane. start: (x, y, z_strip_bottom) at the
    port's outer end; stop: the inner end at z = 0; prop 'x' or 'y'; exc 'z'."""
    from openEMS.ports import MSLPort, Port

    port = MSLPort.__new__(MSLPort)
    Port.__init__(port, CSX, port_nr=n, start=start, stop=stop, excite=excite)
    ny = {"x": 0, "y": 1}[prop]
    port.exc_ny, port.prop_ny = 2, ny
    port.direction = np.sign(stop[ny] - start[ny])
    port.upside_down = np.sign(stop[2] - start[2])
    port.feed_shift = feed_shift
    port.measplane_shift = meas_shift
    port.measplane_pos = port.start[ny] + meas_shift * port.direction
    port.feed_R = feed_r
    lines = np.array(mesh[ny])
    k = int(np.argmin(np.abs(lines - port.measplane_pos)))
    port.measplane_shift = abs(port.start[ny] - lines[k])
    idx = np.array([k - 1, k, k + 1], int)
    if port.direction < 0:
        idx = idx[::-1]
    upos = lines[idx]
    port.U_filenames, port.I_filenames = [], []
    port.U_delta = np.diff(upos)
    for m, suf in enumerate("ABC"):
        a = 0.5 * (port.start + port.stop)
        b = 0.5 * (port.start + port.stop)
        a[ny] = b[ny] = upos[m]
        a[2], b[2] = port.start[2], port.stop[2]
        name = port.lbl_temp.format("ut") + suf
        port.U_filenames.append(name)
        pr = port._AddProbe(CSX, name, p_type=0)
        pr.AddBox(a, b)
        port.port_props.append(pr)
    ipos = upos[0:2] + np.diff(upos) / 2.0
    port.I_delta = np.diff(ipos)
    tr = 1 - ny  # the transverse in-plane axis
    lo_t, hi_t = sorted((port.start[tr], port.stop[tr]))
    zl = np.array(mesh[2])
    zc = zl[zl > 0]
    dz = zc[1] - zc[0] if zc.size > 1 else 0.02
    tl = np.array(mesh[tr])
    dt = np.min(np.diff(tl[(tl >= lo_t - 1e-9) & (tl <= hi_t + 1e-9)]))
    h = abs(port.start[2] - port.stop[2])
    for m, suf in enumerate("AB"):
        a = np.zeros(3)
        b = np.zeros(3)
        a[ny] = b[ny] = ipos[m]
        a[tr], b[tr] = lo_t - 2.5 * dt, hi_t + 2.5 * dt
        a[2], b[2] = 0.5 * h, h + t_cu + 2.5 * dz
        name = port.lbl_temp.format("it") + suf
        port.I_filenames.append(name)
        pr = port._AddProbe(CSX, name, p_type=1, weight=port.direction, norm_dir=ny)
        pr.AddBox(a, b)
        port.port_props.append(pr)
    if excite:
        e = int(np.argmin(np.abs(lines - (port.start[ny] + feed_shift * port.direction))))
        a, b = np.array(port.start), np.array(port.stop)
        a[ny] = b[ny] = lines[e]
        vec = np.zeros(3)
        vec[2] = -1 * port.upside_down * excite
        exc = CSX.AddExcitation(port.lbl_temp.format("excite"), exc_type=0, exc_val=vec)
        exc.AddBox(a, b, priority=0)
        port.port_props.append(exc)
    a, b = np.array(port.start), np.array(port.stop)
    b[ny] = a[ny]
    res = CSX.AddLumpedElement(port.lbl_temp.format("resist"), ny=2, caps=True, R=feed_r)
    res.AddBox(a, b)
    port.port_props.append(res)
    return port


# --- the msl model --------------------------------------------------------------------------------


def build_msl(m: dict, a, CSX, FDTD):
    s = STACK
    h, t = s["h_pp"], s["t_out"]
    res = a.res
    x0, x1, y0, y1 = m["window"]
    feed, meas = m["feed_mm"], m["meas_mm"]
    margin = m.get("margin_mm", 1.5)
    ports = m["ports"]
    sides = {p[1] for p in ports}
    # copper raster on the fine grid over the window and the feeds
    xa = x0 - (feed if "W" in sides else 0.0)
    xb = x1 + (feed if "E" in sides else 0.0)
    ya = y0 - (feed if "S" in sides else 0.0)
    yb = y1 + (feed if "N" in sides else 0.0)
    xe = np.round(np.linspace(xa, xb, int(round((xb - xa) / res)) + 1), 6)
    ye = np.round(np.linspace(ya, yb, int(round((yb - ya) / res)) + 1), 6)
    xc, yc = 0.5 * (xe[1:] + xe[:-1]), 0.5 * (ye[1:] + ye[:-1])
    X, Y = np.meshgrid(xc, yc, indexing="ij")
    mask = np.zeros(X.shape, bool)
    for r in m["copper"]:
        mask |= (X > r[0]) & (X < r[1]) & (Y > r[2]) & (Y < r[3])
    port_specs = []
    for n, side, at, w in ports:
        if side == "W":
            r = (xa, x0, at - w / 2, at + w / 2)
            start, stop, prop = [xa, at - w / 2, h], [x0, at + w / 2, 0.0], "x"
        elif side == "E":
            r = (x1, xb, at - w / 2, at + w / 2)
            start, stop, prop = [xb, at - w / 2, h], [x1, at + w / 2, 0.0], "x"
        elif side == "S":
            r = (at - w / 2, at + w / 2, ya, y0)
            start, stop, prop = [at - w / 2, ya, h], [at + w / 2, y0, 0.0], "y"
        else:
            r = (at - w / 2, at + w / 2, y1, yb)
            start, stop, prop = [at - w / 2, yb, h], [at + w / 2, y1, 0.0], "y"
        mask |= (X > r[0]) & (X < r[1]) & (Y > r[2]) & (Y < r[3])
        port_specs.append((n, start, stop, prop))
    # mesh: fine (res) over the copper raster's box, graded out to the margin and the PML
    pml = 8
    dmax = 0.4
    xl = grade(list(xe), xa - margin, xb + margin, res, dmax)
    yl = grade(list(ye), ya - margin, yb + margin, res, dmax)
    xl = grade(xl, xl[0] - pml * dmax, xl[-1] + pml * dmax, dmax, dmax)
    yl = grade(yl, yl[0] - pml * dmax, yl[-1] + pml * dmax, dmax, dmax)
    nz_sub, nz_cu = a.nz_sub, 2
    z_gnd = -0.02 if not a.lossless else 0.0
    zl = ([z_gnd] if z_gnd < 0 else []) + uniform(0.0, h, h / nz_sub)[:-1]
    zl += uniform(h, h + t, t / nz_cu)
    air = m.get("air_mm", 3.0)
    zl = grade(zl, zl[0], h + t + air, t / nz_cu, 0.4)
    zl = grade(zl, zl[0], zl[-1] + pml * 0.4, 0.4, 0.4)
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(1e-3)
    mesh.SetLines("x", merge(xl))
    mesh.SetLines("y", merge(yl))
    mesh.SetLines("z", merge(zl))
    X0, X1, Y0, Y1 = xl[0], xl[-1], yl[0], yl[-1]
    sub = CSX.AddMaterial("prepreg", epsilon=s["er_pp"], kappa=kappa(s["er_pp"]))
    sub.AddBox([X0, Y0, 0.0], [X1, Y1, h], priority=1)
    pec = CSX.AddMetal("PEC")
    rects = runs_to_rects(mask, xe, ye)
    for r in rects:
        pec.AddBox([r[0], r[2], h], [r[1], r[3], h + t], priority=10)
    sheets = 0
    if not a.lossless:
        cu = CSX.AddConductingSheet("cu", conductivity=SIGMA_CU, thickness=t * 1e-3)
        gnd = CSX.AddConductingSheet("gnd", conductivity=SIGMA_CU, thickness=s["t_in"] * 1e-3)
        pec.AddBox([X0, Y0, z_gnd], [X1, Y1, 0.0], priority=10)
        gnd.AddBox([X0, Y0, 0.0], [X1, Y1, 0.0], priority=20)
        for r in rects:
            for z in (h, h + t):
                cu.AddBox([r[0], r[2], z], [r[1], r[3], z], priority=20)
                sheets += 1
        xf, yf = faces(mask, xe, ye)
        for x, ya_, yb_ in xf:
            cu.AddBox([x, ya_, h], [x, yb_, h + t], priority=20)
            sheets += 1
        for y, xa_, xb_ in yf:
            cu.AddBox([xa_, y, h], [xb_, y, h + t], priority=20)
            sheets += 1
    lines = [mesh.GetLines(d) for d in ("x", "y", "z")]
    plist = []
    for n, start, stop, prop in port_specs:
        p = thick_msl_port(
            CSX,
            lines,
            n,
            start,
            stop,
            prop,
            t,
            1.0 if n == a.excite else 0.0,
            50.0,
            feed - meas,
            0.5,
        )
        plist.append((n, p, feed))
    bc = ["PML_8"] * 4 + ["PEC", "PML_8"]
    cells = (len(lines[0]) - 1) * (len(lines[1]) - 1) * (len(lines[2]) - 1)
    small = []
    for v in lines:
        dv = np.diff(v)
        k = int(np.argmin(dv))
        small.append([round(float(dv[k]), 5), round(float(v[k]), 4)])
    info = dict(
        rects=len(rects),
        sheets=sheets,
        cells=int(cells),
        mesh=[len(v) for v in lines],
        min_cell_at=small,
    )
    return plist, bc, info


# --- run --------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--excite", type=int, default=1)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--sheets", action="store_true", help="conducting sheets (experimental)")
    ap.add_argument("--nz-sub", type=int, default=6, help="cells across the prepreg (msl)")
    ap.add_argument("--mesh-scale", type=float, default=1.0, help="launch: coarser (tests only)")
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--res", type=float, default=0.05, help="fine cell (mm)")
    ap.add_argument("--max-steps", type=int, default=600000)
    ap.add_argument("--end", type=float, default=1e-5, help="energy end criterion")
    a = ap.parse_args(argv)
    a.lossless = not a.sheets
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    with open(a.model, encoding="utf-8") as fh:
        m = json.load(fh)
    t0 = time.time()
    a.out = os.path.abspath(a.out)  # openEMS's Run changes into the simulation directory
    os.makedirs(a.out, exist_ok=True)
    sim = os.path.join(a.out, "sim")
    FDTD = openEMS(NrTS=a.max_steps, EndCriteria=a.end)
    FDTD.SetGaussExcite(F0, FC)
    CSX = ContinuousStructure()
    FDTD.SetCSX(CSX)
    if m["kind"] == "msl":
        plist, bc, info = build_msl(m, a, CSX, FDTD)
    elif m["kind"] == "launch":
        from order0_launch import build_launch  # noqa: E402 (the launch geometry)

        plist, bc, info = build_launch(m, a, CSX, FDTD)
    else:
        raise SystemExit(f"unknown model kind {m['kind']!r}")
    if m["kind"] == "launch":
        FDTD.SetCustomExcite(dc_free_pulse(), F0, 8.5e9)
        info["excitation"] = "openEMS Gaussian (f0 4.5 GHz, fc 3.5 GHz) less its DC content"
    FDTD.SetBoundaryCond(bc)
    print(json.dumps(dict(model=m["name"], excite=a.excite, **info)), flush=True)
    if a.setup_only:  # build openEMS's operator (catches geometry errors), no time steps
        return int(FDTD.Run(sim, cleanup=True, setup_only=True, numThreads=a.threads) or 0)
    FDTD.Run(sim, cleanup=True, numThreads=a.threads)
    out = dict(
        schema="yapnr-order0-openems/1",
        model=m["name"],
        kind=m["kind"],
        excite=a.excite,
        lossless=bool(a.lossless),
        res_mm=a.res,
        nz_sub=a.nz_sub,
        stack=STACK,
        pulse_hz=[F0, FC],
        f_hz=FREQS.tolist(),
        info=info,
        ports={},
    )
    for n, p, shift in plist:
        if hasattr(p, "order0_zl"):  # a TEM waveguide port in a dielectric: its wave impedance
            p.CalcPort(sim, FREQS, ref_plane_shift=shift, ZL=p.order0_zl)
        else:
            p.CalcPort(sim, FREQS, ref_plane_shift=shift)
        z = np.asarray(p.Z_ref, complex)
        v, i = np.asarray(p.uf_tot, complex), np.asarray(p.if_tot, complex)
        # openEMS uses e^{+jωt} already (DFT_time2freq with exp(-jωt))
        root = 2.0 * np.sqrt(np.real(z))
        aw, bw = (v + z * i) / root, (v - z * i) / root
        out["ports"][str(n)] = {
            k: [np.real(x).tolist(), np.imag(x).tolist()]
            for k, x in dict(v=v, i=i, z=z, beta=np.asarray(p.beta, complex), a=aw, b=bw).items()
        }
    out["wall_s"] = round(time.time() - t0, 1)
    with open(os.path.join(a.out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    shutil.rmtree(sim, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
