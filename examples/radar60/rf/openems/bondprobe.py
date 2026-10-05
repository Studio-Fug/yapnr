"""Plane-pair voltage probes for ant_sim.py (`--probe-bond`, stage 3b E1 cavity evidence).

Palace eigenmode is not available for the stage-3b models (its image is on hold and its config
writer has no Eigenmode yet), so the bank and triplet runs carry their own ring-down evidence:
time-domain voltage probes (they add no mesh lines, so a comparison keeps its mesh template):

- L3-L2 across the bondply under each bank's L2 copper: the column gaps at the patch pair's mid
  height (the bank's middle gap and both end gaps) and the cut-out's top margin;
- L3-L2 and L2-L1 in the open pour round the banks: above and beside each cut-out, and between
  the two banks.

The sites come from the model's cut-outs and phase centres only, so every variant of a
comparison (the D15 pours A/B/C, the K options) has the same sites. After the run each probe's
ring-down (from the end of the excitation pulse) is decomposed into damped sinusoids
(pp_sim.matrix_pencil: frequency, Q, amplitude relative to the probe's peak), and its spectrum is
divided by the driven port's incident voltage: the transfer from the port into the plane pair
(dB, the same DFT convention as the port voltages). OUT/probes.json holds the decimated time
series and the transfers. Evidence label [S]; nothing is measured.
"""

import json
import math
import os

import numpy as np

F_SPEC = np.linspace(54e9, 70e9, 321)  # 50 MHz steps: the transfer spectra
F_BAND = (60.3e9, 62.05e9, 63.8e9)


def points(m):
    """Probe sites: [{name, at, pair: bond|core, where: bank|pour}]. A site within 0.12 mm of a
    via's edge moves along +x in 0.05 mm steps."""
    vias = np.array([v[:3] for v in m["vias"]], float) if m["vias"] else np.zeros((0, 3))

    def clear(x, y):
        for _ in range(40):
            if not len(vias):
                break
            if np.min(np.hypot(vias[:, 0] - x, vias[:, 1] - y) - vias[:, 2] / 2) > 0.12:
                break
            x += 0.05
        return [round(x, 4), round(y, 4)]

    pts, boxes = [], {}
    for bank, (x0, y0, x1, y1) in sorted(m["cutouts"].items()):
        cols = sorted(
            (c[0], c[1])
            for c in m["phase_centres"].values()
            if x0 <= c[0] <= x1 and y0 <= c[1] <= y1
        )
        if len(cols) < 2:
            continue
        yc = sum(c[1] for c in cols) / len(cols)
        gaps = [(a[0] + b[0]) / 2 for a, b in zip(cols, cols[1:])]
        xm = min(gaps, key=lambda g: abs(g - (x0 + x1) / 2))
        boxes[bank] = (x0, y0, x1, y1, yc)
        seen = []
        for tag, (x, y) in (
            ("mid", (xm, yc)),
            ("end0", (gaps[0], yc)),
            ("end1", (gaps[-1], yc)),
            ("top", (xm, y1 - 0.35)),
        ):
            at = clear(x, y)
            if at in seen:  # a triplet's middle gap is also an end gap
                continue
            seen.append(at)
            pts.append(dict(name=f"pb_{bank}_{tag}", at=at, pair="bond", where="bank"))
        for tag, (x, y) in (("above", ((x0 + x1) / 2, y1 + 1.6)), ("side", (x0 - 1.6, yc))):
            at = clear(x, y)
            pts.append(dict(name=f"pb_{bank}_{tag}", at=at, pair="bond", where="pour"))
            pts.append(dict(name=f"pc_{bank}_{tag}", at=at, pair="core", where="pour"))
    if "RX" in boxes and "TX" in boxes:
        rx, tx = boxes["RX"], boxes["TX"]
        at = clear((rx[2] + tx[0]) / 2, (rx[4] + tx[4]) / 2)
        pts.append(dict(name="pb_between", at=at, pair="bond", where="pour"))
        pts.append(dict(name="pc_between", at=at, pair="core", where="pour"))
    return pts


def add(csx, pts, z_l2, z_l1):
    """The voltage probes (z lines: bond 0..z_l2, core z_l2..z_l1)."""
    for p in pts:
        za, zb = (0.0, z_l2) if p["pair"] == "bond" else (z_l2, z_l1)
        pr = csx.AddProbe(p["name"], p_type=0)
        pr.AddBox([p["at"][0], p["at"][1], za], [p["at"][0], p["at"][1], zb])


def sources(csx, pts, z_l2, h):
    """`--ringdown-ns`: z-directed soft E sources across the bondply at each bank's middle and
    end column gaps (the probe sites), 2 h wide."""
    for p in pts:
        if p["where"] == "bank" and p["pair"] == "bond" and p["name"].endswith(("_mid", "_end0")):
            x, y = p["at"]
            ex = csx.AddExcitation(f"src_{p['name']}", exc_type=0, exc_val=[0, 0, 1])
            ex.AddBox([x - h, y - h, 0.0], [x + h, y + h, z_l2])


MIN_WINDOW_S = 0.6e-9  # a shorter ring-down (a driven run stopped at -40 dB) gives no poles


def ringdown(t, v, t0, fmin=54e9, fmax=72e9):
    """Damped sinusoids of v after t0, [(f, Q, |a|/peak)] by amplitude, the late levels (dB
    below the peak 0.5/1/2 ns after t0) and the analysed window (s). No poles when the record
    after t0 is shorter than MIN_WINDOW_S; undamped or growing poles (Q = inf) are dropped."""
    from pp_sim import matrix_pencil

    pk = float(np.max(np.abs(v))) or 1.0
    late = {}
    for dt_ns in (0.5, 1.0, 2.0):
        sel = t > t0 + dt_ns * 1e-9
        late[f"{dt_ns}ns"] = (
            round(float(20 * np.log10(np.max(np.abs(v[sel])) / pk + 1e-30)), 1)
            if sel.any()
            else None
        )
    sel = t > t0
    ts, vs = t[sel], v[sel]
    window = float(ts[-1] - ts[0]) if len(ts) > 1 else 0.0
    if window < MIN_WINDOW_S:
        return [], late, pk, window
    step = max(1, int(round((1 / (4 * 80e9)) / (t[1] - t[0]))))
    ts, vs = ts[::step][:1200], vs[::step][:1200]
    poles = matrix_pencil(ts - ts[0], vs, fmin, fmax)
    return [(f, q, a / pk) for f, q, a in poles if math.isfinite(q)], late, pk, window


def ringdown_only(sim, out, pts, fc):
    """`--ringdown-ns`: poles of every probe from the end of the sources' pulse (no port is
    driven, so no transfer); writes OUT/probes.json, returns the summary for result.json."""
    t_pulse = 2 * 9 / (2 * math.pi * fc)
    summary, store = {}, {}
    for p in pts:
        fn = os.path.join(sim, p["name"])
        if not os.path.exists(fn):
            continue
        tv = np.loadtxt(fn, comments="%")
        if tv.ndim != 2 or len(tv) < 10:
            continue
        t, v = tv[:, 0], tv[:, 1]
        poles, late, pk, window = ringdown(t, v, t_pulse + 0.05e-9)
        summary[p["name"]] = dict(
            at=p["at"],
            pair=p["pair"],
            where=p["where"],
            peak_v=pk,
            late_db=late,
            window_ns=round(window * 1e9, 3),
            poles=[
                dict(f_ghz=round(f / 1e9, 3), q=round(q, 1), amp_rel=round(a, 5))
                for f, q, a in poles[:12]
            ],
            q20_57_70=[
                dict(f_ghz=round(f / 1e9, 3), q=round(q, 1), amp_rel=round(a, 5))
                for f, q, a in poles
                if 57e9 <= f <= 70e9 and q >= 20 and a >= 1e-3
            ],
        )
        step = max(1, int(round(1e-12 / (t[1] - t[0]))))  # about 1 ps
        store[p["name"]] = dict(
            dt_s=float(t[step] - t[0]), v=[float(f"{x:.4g}") for x in v[::step]]
        )
    with open(os.path.join(out, "probes.json"), "w") as fh:
        json.dump(store, fh)
    return summary


def analyse(sim, out, pts, port, fc):
    """Ring-down poles and port-to-probe transfers of every probe; writes OUT/probes.json and
    returns the summary for result.json. `port` is the driven port (openEMS port object); `fc`
    the Gaussian's 20 dB half-bandwidth (openEMS: the pulse lasts 2 x 9 / (2 pi fc))."""
    from openEMS.utilities import DFT_time2freq

    t_pulse = 2 * 9 / (2 * math.pi * fc)
    port.CalcPort(sim, F_SPEC, ref_impedance=50)
    u_inc = np.asarray(port.uf_inc)
    summary, store = {}, dict(f_ghz=[round(float(x) / 1e9, 3) for x in F_SPEC])
    for p in pts:
        fn = os.path.join(sim, p["name"])
        if not os.path.exists(fn):
            continue
        tv = np.loadtxt(fn, comments="%")
        if tv.ndim != 2 or len(tv) < 10:
            continue
        t, v = tv[:, 0], tv[:, 1]
        poles, late, pk, window = ringdown(t, v, t_pulse + 0.1e-9)
        tr = 20 * np.log10(np.abs(DFT_time2freq(t, v, F_SPEC)) / np.abs(u_inc) + 1e-30)
        ib = (F_SPEC >= 57e9) & (F_SPEC <= 70e9)
        k = int(np.argmax(np.where(ib, tr, -999)))
        summary[p["name"]] = dict(
            at=p["at"],
            pair=p["pair"],
            where=p["where"],
            peak_v=pk,
            late_db=late,
            window_ns=round(window * 1e9, 3),
            transfer_db_at={
                f"{x / 1e9:.2f}": round(float(np.interp(x, F_SPEC, tr)), 2) for x in F_BAND
            },
            transfer_max_57_70=dict(
                f_ghz=round(float(F_SPEC[k]) / 1e9, 3), db=round(float(tr[k]), 2)
            ),
            poles=[
                dict(f_ghz=round(f / 1e9, 3), q=round(q, 1), amp_rel=round(a, 5))
                for f, q, a in poles[:12]
            ],
            q20_57_70=[
                dict(f_ghz=round(f / 1e9, 3), q=round(q, 1), amp_rel=round(a, 5))
                for f, q, a in poles
                if 57e9 <= f <= 70e9 and q >= 20 and a >= 1e-3
            ],
        )
        step = max(1, int(round(1e-12 / (t[1] - t[0]))))  # about 1 ps
        store[p["name"]] = dict(
            dt_s=float(t[step] - t[0]),
            v=[float(f"{x:.4g}") for x in v[::step]],
            transfer_db=[round(float(x), 2) for x in tr],
        )
    with open(os.path.join(out, "probes.json"), "w") as fh:
        json.dump(store, fh)
    return summary
