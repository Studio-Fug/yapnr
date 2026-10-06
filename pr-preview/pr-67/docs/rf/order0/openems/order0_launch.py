"""The ``launch`` model of `order0_em.py`: a region-M stick (A01, A04) with a Cinch
142-0701-851 edge SMA on each end, as a tier-1 (coax) calibration sees it.

Geometry (written into the model JSON by `models.py` from yapnr.rf.coupons.launch and the board
file, so it is the drawn board): x along the stick from the west milled edge (0 to L), y across
(the axis at 0), z up from the board's bottom face.

- Board: L4 (B.Cu) a PEC slab 0-43 µm under the whole stick less the 0.381 mm keep-back; prepreg
  0.1999 mm; In2.Cu and In1.Cu as conducting (or PEC) sheets on their faces toward L1 and L4
  (the 17 µm of each is filled with core so every dielectric keeps its thickness); core 0.9906 mm;
  prepreg 0.1999 mm; L1 43.18 µm: the pin pad and taper, the 0.40 mm line, the ground pour less
  the launch's channel and the line's keep-away, all to the keep-back. In1.Cu has the launch's
  cut-out under the pad and taper. Every via of the stick (board file) is a PEC post of the
  0.30 mm drill's area from L4 to L1. No solder mask, no solder fillets.
- Connector (Cinch drawing, launch.CINCH_142_0701_851): the flange (9.525 × 7.925 × 1.651 mm,
  PEC) against the milled edge with its coax bore (PTFE εr 2.05, Ø4.20 mm; centre conductor
  Ø1.27 mm, 50.1 Ω), the coax continued as a PEC barrel to the port, the 0.51 × 0.25 mm tab from
  inside the bore to 1.905 mm on the pad (its centre on the coax axis), the top legs on the L1
  leg pads and the bottom legs on B.Cu (1.016 mm wide and thick, 4.75 mm long, flush with the
  flange's sides).
- Ports: a coax TEM waveguide port (analytic mode, ZL = η0/√2.05) in each barrel, 3.35 mm
  behind the flange; the waves are moved to the flange's back face. The connector's own coax
  between its mating plane and the flange (which the tier-1 SOLT includes) is not modelled: it
  adds phase and a little loss, not reflection, to a good connector.
"""

from __future__ import annotations

import math

import numpy as np

ER_PTFE = 2.05
R_PIN = 0.635
R_BORE = 2.10
R_BARREL = 3.20
ETA0 = 376.730313668


def _graded(a, b, c, cl, cr, ratio):
    """Lines in (a, b]: cells growing by `ratio` from the left neighbour's cl and the right
    neighbour's cr up to c, c in the middle, scaled to fit exactly."""
    g = b - a

    def ramp(d):
        out = []
        while True:
            d = min(d * ratio, c)
            if d >= c - 1e-12:
                return out
            out.append(d)

    left, right = ramp(cl), ramp(cr)
    rem = g - sum(left) - sum(right)
    if rem < 0:
        n = max(1, int(math.ceil(g / (ratio * min(cl, cr)) - 1e-9)))
        return list(np.linspace(a, b, n + 1)[1:])
    n = int(round(rem / c))
    cells = left + ([rem / n] * n if n else []) + right[::-1]
    if not cells:
        return [b]
    pos = a + np.cumsum(np.array(cells) * (g / sum(cells)))
    pos[-1] = b
    return list(pos)


def _lines(fixed, regions, dmax_far, lo, hi, ratio=1.5):
    """Mesh lines from lo to hi through every `fixed` line and region bound: each gap between
    them split evenly into cells of at most 1.2 x its cap (the smallest region dmax covering it,
    else dmax_far); a gap whose cells exceed `ratio` x a neighbour's grows its cells
    geometrically from that neighbour instead."""

    def cap(x):
        d = dmax_far
        for a, b, dm in regions:
            if a - 1e-9 <= x <= b + 1e-9:
                d = min(d, dm)
        return d

    pts = list(fixed) + [lo, hi] + [v for a, b, _ in regions for v in (a, b)]
    pts = sorted(set(round(v, 6) for v in pts if lo - 1e-9 <= v <= hi + 1e-9))
    merged = [pts[0]]
    for v in pts[1:]:
        if v - merged[-1] > 2e-3:
            merged.append(v)
    gaps = list(zip(merged, merged[1:]))
    cell = []
    for a, b in gaps:
        n = max(1, int(math.ceil((b - a) / (1.2 * cap(0.5 * (a + b))) - 1e-9)))
        cell.append((b - a) / n)
    out = [merged[0]]
    for i, (a, b) in enumerate(gaps):
        c = cell[i]
        cl = cell[i - 1] if i > 0 else c
        cr = cell[i + 1] if i + 1 < len(cell) else c
        if c > ratio * min(cl, cr) * 1.0001:
            out += _graded(a, b, c, min(cl, c), min(cr, c), ratio)
        else:
            n = int(round((b - a) / c))
            out += list(np.linspace(a, b, n + 1)[1:])
    return [round(float(v), 6) for v in out]


def build_launch(m: dict, a, CSX, FDTD):
    import order0_em as em

    s = em.STACK
    L = float(m["length"])
    hw = float(m["stick_w"]) / 2
    kb = float(m["keepback"])
    t1, tin = s["t_out"], s["t_in"]
    z_l4 = t1  # L4's top face
    z_in2 = z_l4 + s["h_pp"]
    z_in1 = z_in2 + tin + s["h_core"] + tin
    z_l1b = z_in1 + s["h_pp"]
    z_l1t = z_l1b + t1
    c = m["conn"]
    tab_t, tab_w, tab_len = c["tab_t"], c["tab_w"], c["tab_len"]
    z_ax = z_l1t + tab_t / 2
    fh, ft, bw = c["flange_h"], c["flange_t"], c["body_w"]
    leg_y0, leg_y1 = bw / 2 - c["leg_w"], bw / 2
    leg_len, leg_t = c["leg_len"], c["leg_t_top"]
    port_back = 5.0  # the coax port, behind the milled edge
    x_end = port_back + 1.5  # the coax runs on into the PML
    # --- mesh ---------------------------------------------------------------------------------
    lossy = not a.lossless
    fine = a.res if a.res < 0.05 else 0.04
    xfix = [-x_end, -port_back, -port_back + 0.4, -ft, 0.0, kb, -0.3]
    for xs in m["launch_x"]:
        xfix += [xs, L - xs]
    xfix += [L, L + ft, L + port_back - 0.4, L + port_back, L + x_end, L + 0.3]
    xfix += [leg_len, L - leg_len, c["lay_e"], L - c["lay_e"]]
    xreg = [(-0.4, m["launch_x"][-1] + 0.6, fine), (L - m["launch_x"][-1] - 0.6, L + 0.4, fine)]
    xreg += [(-x_end, 0.0, 0.1), (L, L + x_end, 0.1), (0.0, L, 0.2)]
    k = getattr(a, "mesh_scale", 1.0)  # > 1: a coarse functional test only
    xreg = [(u, v, d * k) for u, v, d in xreg]
    xl = _lines(xfix, xreg, 0.2 * k, -x_end, L + x_end)
    yfix = [0.0] + [sg * v for v in m["y_fixed"] for sg in (-1, 1)]
    yfix += [
        sg * v
        for v in (R_PIN, R_BORE, R_BARREL, leg_y0, leg_y1, hw, hw - kb, bw / 2)
        for sg in (-1, 1)
    ]
    ymax = hw + 1.5
    yreg = [(-0.9, 0.9, 0.035), (-2.2, 2.2, 0.05), (-hw, hw, 0.1)]
    yreg = [(u, v, d * k) for u, v, d in yreg]
    yl = _lines(yfix, yreg, 0.4 * k, -ymax, ymax)
    z_bot = z_ax - fh / 2 - 1.0
    z_top = z_ax + fh / 2 + 1.0
    zfix = [0.0, z_l4, z_in2, z_in1, z_l1b, z_l1t, z_l1t + tab_t, z_l1t + leg_t, -leg_t]
    zfix += [z_ax + v for v in (-R_PIN, R_PIN, -R_BORE, R_BORE, -R_BARREL, R_BARREL)]
    zfix += [z_ax - fh / 2, z_ax + fh / 2]
    zreg = [
        (z_in1, z_l1b, s["h_pp"] / 6.5),
        (z_l1b, z_l1t, t1 / 2),
        (z_l1t, z_l1t + tab_t, tab_t / 5),
        (z_in2, z_in1, 0.12),
        (0.0, z_in2, 0.07),
        (z_ax - R_BORE, z_ax + R_BORE, 0.1),
    ]
    zreg = [(u, v, d * (k if d > 0.05 else 1.0)) for u, v, d in zreg]
    zl = _lines(zfix, zreg, 0.4 * k, z_bot, z_top)
    # PML cells beyond
    pml = 8
    xl = list(xl) + [xl[-1] + 0.2 * (k + 1) for k in range(pml)]
    xl = [xl[0] - 0.2 * (pml - k) for k in range(pml)] + list(xl)
    yl = (
        [yl[0] - 0.4 * (pml - k) for k in range(pml)]
        + list(yl)
        + [yl[-1] + 0.4 * (k + 1) for k in range(pml)]
    )
    zl = (
        [zl[0] - 0.4 * (pml - k) for k in range(pml)]
        + list(zl)
        + [zl[-1] + 0.4 * (k + 1) for k in range(pml)]
    )
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(1e-3)
    mesh.SetLines("x", xl)
    mesh.SetLines("y", yl)
    mesh.SetLines("z", zl)
    # --- materials -------------------------------------------------------------------------------
    pp = CSX.AddMaterial("prepreg", epsilon=s["er_pp"], kappa=em.kappa(s["er_pp"]))
    core = CSX.AddMaterial("core", epsilon=s["er_core"], kappa=em.kappa(s["er_core"]))
    ptfe = CSX.AddMaterial("ptfe", epsilon=ER_PTFE)
    pec = CSX.AddMetal("PEC")
    pp.AddBox([0.0, -hw, z_l4], [L, hw, z_in2], priority=1)
    core.AddBox([0.0, -hw, z_in2], [L, hw, z_in1], priority=1)
    pp.AddBox([0.0, -hw, z_in1], [L, hw, z_l1b], priority=1)
    # L4 slab and the inner planes
    pec.AddBox([kb, -hw + kb, 0.0], [L - kb, hw - kb, z_l4], priority=10)
    if lossy:
        cu = CSX.AddConductingSheet("cu", conductivity=em.SIGMA_CU, thickness=t1 * 1e-3)
        cin = CSX.AddConductingSheet("cu_in", conductivity=em.SIGMA_CU, thickness=tin * 1e-3)
        plane = cin
    else:
        cu = None
        plane = pec
    plane.AddBox([kb, -hw + kb, z_in2], [L - kb, hw - kb, z_in2], priority=12)
    for poly in m["in1"]:
        pts = np.array(poly).T
        plane.AddPolygon(pts, "z", z_in1, priority=12)
    # L1: signal and pour, PEC bodies; lossy: the signal's faces as sheets
    for poly in m["signal"] + m["pour"]:
        pts = np.array(poly).T
        pec.AddLinPoly(pts, "z", z_l1b, t1, priority=10)
    if lossy:
        for poly in m["signal"]:
            pts = np.array(poly).T
            for z in (z_l1b, z_l1t):
                cu.AddPolygon(pts, "z", z, priority=20)
        lw = m["line_w"]
        x0s, x1s = m["line_x"]
        for sg in (-1, 1):
            cu.AddBox([x0s, sg * lw / 2, z_l1b], [x1s, sg * lw / 2, z_l1t], priority=20)
    # vias: posts of the drill's area
    side = m["via_drill"] * math.sqrt(math.pi) / 2
    for x, y in m["vias"]:
        pec.AddBox(
            [x - side / 2, y - side / 2, 0.0], [x + side / 2, y + side / 2, z_l1t], priority=11
        )
    # --- connectors --------------------------------------------------------------------------------
    for xo, sgn in ((0.0, -1), (L, 1)):
        xf = xo + sgn * ft
        pec.AddBox([xo, -bw / 2, z_ax - fh / 2], [xf, bw / 2, z_ax + fh / 2], priority=10)
        xb = xo + sgn * (x_end + 2.0)
        pec.AddBox([xf, -R_BARREL, z_ax - R_BARREL], [xb, R_BARREL, z_ax + R_BARREL], priority=10)
        ptfe.AddCylinder([xo, 0.0, z_ax], [xb, 0.0, z_ax], R_BORE, priority=20)
        xp = xo + sgn * 0.3  # the pin becomes the tab just inside the bore
        pec.AddCylinder([xp, 0.0, z_ax], [xb, 0.0, z_ax], R_PIN, priority=30)
        xt = xo - sgn * tab_len
        pec.AddBox([xp, -tab_w / 2, z_l1t], [xt, tab_w / 2, z_l1t + tab_t], priority=30)
        xl_ = xo - sgn * leg_len
        for ys in (-1, 1):
            ya, yb = sorted((ys * leg_y0, ys * leg_y1))
            pec.AddBox([xo, ya, z_l1t], [xl_, yb, z_l1t + leg_t], priority=10)
            pec.AddBox([xo, ya, -leg_t], [xl_, yb, 0.0], priority=10)
    # --- ports ---------------------------------------------------------------------------------------
    from openEMS.ports import WaveguidePort

    a2, b2 = (R_PIN + 0.01) ** 2, (R_BORE - 0.01) ** 2
    rr = "(y*y+z*z)"
    inside = f"({rr}>{a2:.6f})*({rr}<{b2:.6f})"
    e_func = ["0", f"{inside}*y/{rr}", f"{inside}*z/{rr}"]
    h_func = ["0", f"-{inside}*z/{rr}", f"{inside}*y/{rr}"]
    plist = []
    for n, xo, sgn in ((1, 0.0, -1), (2, L, 1)):
        xs = xo + sgn * port_back
        start = [xs, -R_BORE, z_ax - R_BORE]
        stop = [xs - sgn * 0.4, R_BORE, z_ax + R_BORE]
        p = WaveguidePort(
            CSX,
            n,
            start,
            stop,
            "x",
            e_func,
            h_func,
            0.0,
            excite=1.0 if n == a.excite else 0.0,
            local_origin=[xs, 0.0, z_ax],
        )
        p.ref_index = math.sqrt(ER_PTFE)
        p.order0_zl = ETA0 / math.sqrt(ER_PTFE)
        plist.append((n, p, port_back - ft))
    bc = ["PML_8"] * 6
    lines = [xl, yl, zl]
    cells = (len(xl) - 1) * (len(yl) - 1) * (len(zl) - 1)
    small = []
    for v in lines:
        dv = np.diff(v)
        k = int(np.argmin(dv))
        small.append([round(float(dv[k]), 5), round(float(v[k]), 4)])
    info = dict(
        cells=int(cells),
        mesh=[len(v) for v in lines],
        vias=len(m["vias"]),
        z_axis=round(z_ax, 5),
        min_cell_at=small,
    )
    return plist, bc, info
