#!/usr/bin/env python3
"""Write the openEMS model files of `order0_em.py` from yapnr's own data (run with yapnr on the
path, at the repository root):

    python3 docs/rf/order0/openems/models.py [--out docs/rf/order0/openems/models]

- d1.json: the shipped D1 (`predictions/D1/runs/d1-star/footprint.kicad_mod`, re-rasterized as the
  validator does on its 0.10 mm grid) in its 12 x 15 mm window, ports W 0 / N 6.0 / S 6.0 mm;
- r1.json: R1 as drawn (`coupons.catalog.o_reference("M")`);
- line10.json, line30.json: the 0.40 mm line, 10 and 30 mm (run 0b's lengths), for openEMS's own
  α, β and Zc;
- a01.json, a04.json: the sticks A01 (thru) and A04 (ΔL 30 mm) of O0-M with the launch
  (`coupons.launch`, region M), the In1.Cu cut-out, the pour's channel and keep-away, and every
  via of the stick as the board file draws it (`examples/rf-coupons/order0/O0-M`).
"""

from __future__ import annotations

import argparse
import json
import os
import re

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
FEED_MM, MEAS_MM = 5.0, 2.5  # each port's feed outside the window, its probes' distance
BOARD = os.path.join(ROOT, "examples", "rf-coupons", "order0", "O0-M")


def runs_to_rects(mask, pitch, x0, y0):
    ni, nj = mask.shape
    xe = x0 + np.arange(ni + 1) * pitch
    ye = y0 + np.arange(nj + 1) * pitch
    rects, open_runs = [], {}
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
        new = {r: open_runs.pop(r, j) for r in row}
        for (i0, i1), j0 in open_runs.items():
            rects.append([round(xe[i0], 4), round(xe[i1], 4), round(ye[j0], 4), round(ye[j], 4)])
        open_runs = new
    return rects


def d1_model():
    from yapnr.rf import validate
    from yapnr.rf.export.kicad import read_footprint

    run = os.path.join(ROOT, "docs", "rf", "order0", "predictions", "D1", "runs", "d1-star")
    spec = validate.load_spec(run)
    fp = read_footprint(os.path.join(run, "footprint.kicad_mod"))
    pitch = spec.grid.pitch_mm
    mask = validate.footprint_mask(fp, spec, pitch)
    x0, x1, y0, y1 = spec.design_region
    widths = validate.pad_widths(fp, spec)
    ports = [[p.n, p.side, p.at_mm, round(widths[p.n] * pitch, 4)] for p in spec.ports]
    return dict(
        kind="msl",
        name="d1",
        source="predictions/D1/runs/d1-star/footprint.kicad_mod (O0 D1 divider-osh-m ad20e643)",
        window=[x0, x1, y0, y1],
        copper=runs_to_rects(mask, pitch, x0, y0),
        ports=ports,
        feed_mm=FEED_MM,
        meas_mm=MEAS_MM,
    )


def r1_model():
    from yapnr.rf.coupons import catalog

    ref = catalog.o_reference("M")
    w = catalog.O_REF_2D["M"]["w_out"]
    return dict(
        kind="msl",
        name="r1",
        source="yapnr.rf.coupons.catalog.o_reference('M') (R1 as drawn)",
        window=[0.0, ref["w"], -ref["h"] / 2, ref["h"] / 2],
        copper=[list(c) for c in ref["copper"]],
        ports=[[n, side, at, w] for n, (side, at) in enumerate(ref["ports"], 1)],
        feed_mm=FEED_MM,
        meas_mm=MEAS_MM,
    )


def line_model(length):
    return dict(
        kind="msl",
        name=f"line{length:g}",
        source=f"a straight 0.40 mm line, {length:g} mm (run 0b's loss lines)",
        window=[0.0, float(length), -1.5, 1.5],
        copper=[[0.0, float(length), -0.2, 0.2]],
        ports=[[1, "W", 0.0, 0.4], [2, "E", 0.0, 0.4]],
        feed_mm=FEED_MM,
        meas_mm=MEAS_MM,
    )


def board_vias(sid):
    """The stick's vias (x from its west edge, y from its axis, mm) from the board file."""
    with open(os.path.join(BOARD, "catalog.json"), encoding="utf-8") as fh:
        cat = json.load(fh)
    with open(os.path.join(BOARD, "O0-M.kicad_pcb"), encoding="utf-8") as fh:
        pcb = fh.read()
    st = next(s for s in cat["sticks"] if s["id"] == sid)
    pl = cat["placement"][sid]
    if pl["rot"] != 0:
        raise SystemExit(f"{sid} is turned in O0-M; read it from another upload")
    pat = r'\(via \(at ([-\d.]+) ([-\d.]+)\) \(size [\d.]+\) \(drill ([\d.]+)\)[^)]*\) \(net "GND_%s"\)'
    vias = [(float(x), float(y), float(d)) for x, y, d in re.findall(pat % sid, pcb)]
    # the stick's frame in the board: west edge and axis (the vias are symmetric about the axis)
    xs, ys = [v[0] for v in vias], [v[1] for v in vias]
    y_axis = 0.5 * (min(ys) + max(ys))
    x_west = pl["x"] + 20.0  # the panel's origin in the board file
    if not (x_west < min(xs) and max(xs) < x_west + st["length"]):
        raise SystemExit(f"{sid}: vias outside the stick's frame")
    drills = sorted(set(v[2] for v in vias))
    out = [[round(x - x_west, 4), round(-(y - y_axis), 4)] for x, y, _ in vias]
    return out, drills, st


def launch_model(sid):
    from yapnr.rf.coupons import catalog, launch

    d = launch.design("oshpark-4l-fr408hr:M")
    vias, drills, st = board_vias(sid)
    L = float(st["length"])
    hw = catalog.O_STICK_W["M"] / 2
    kb = d.board.keepback
    sig = [[round(x, 4), round(y, 4)] for x, y in d.signal_outline()]
    sig_r = [[round(L - x, 4), y] for x, y in reversed(sig)]
    x_te = d.x_te
    line = [
        [x_te, -d.line_w / 2],
        [L - x_te, -d.line_w / 2],
        [L - x_te, d.line_w / 2],
        [x_te, d.line_w / 2],
    ]
    prof = d.channel(x_to=L / 2)
    # the pour's inner edge: the channel from the left launch to the middle, mirrored after it
    left = [(x, h) for x, h in prof if x >= kb - 1e-9]
    if left[0][0] > kb:
        left.insert(0, (kb, d.channel_at(kb)))
    edge = left + [(L - x, h) for x, h in reversed(left)]
    edge = [
        p
        for k, p in enumerate(edge)
        if k == 0 or abs(p[0] - edge[k - 1][0]) > 1e-9 or abs(p[1] - edge[k - 1][1]) > 1e-9
    ]
    top = [[round(x, 4), round(h, 4)] for x, h in edge] + [[L - kb, hw - kb], [kb, hw - kb]]
    bot = [[x, -y] for x, y in reversed(top)]
    cut = d.cut_profile()
    cl = [(max(x, kb), h) for x, h in cut]
    cedge = cl + [(L - x, h) for x, h in reversed(cl)]
    in1_top = [[round(x, 4), round(h, 4)] for x, h in cedge] + [[L - kb, hw - kb], [kb, hw - kb]]
    in1_bot = [[x, -y] for x, y in reversed(in1_top)]
    conn = d.conn
    lx = sorted(set([d.x0, d.x_tab, d.x_pe, d.x_te, d.x_end]))
    yfix = sorted(
        set(
            [
                d.line_w / 2,
                d.pad_w / 2,
                round(d.pad_w / 2 + d.gap_tab, 4),
                round(d.pad_w / 2 + d.gap_bare, 4),
                d.cut_pad,
                d.line_w / 2 + d.keepaway,
                conn.gnd_pad_y[0],
                conn.gnd_pad_y[1],
                conn.tab_w / 2,
            ]
        )
    )
    return dict(
        kind="launch",
        name=sid.lower(),
        source=f"O0-M {sid}: yapnr.rf.coupons.launch (oshpark-4l-fr408hr:M) and the board file's"
        " vias",
        length=L,
        stick_w=2 * hw,
        keepback=kb,
        line_w=d.line_w,
        line_x=[x_te, L - x_te],
        signal=[sig, line, sig_r],
        pour=[top, bot],
        in1=[in1_top, in1_bot],
        vias=vias,
        via_drill=drills[0],
        launch_x=lx,
        y_fixed=yfix,
        conn=dict(
            tab_t=conn.tab_t,
            tab_w=conn.tab_w,
            tab_len=conn.tab_len,
            flange_h=conn.flange_h,
            flange_t=conn.flange_t,
            body_w=conn.body_w,
            leg_w=conn.leg_w,
            leg_len=conn.leg_len,
            leg_t_top=conn.leg_t_top,
            lay_e=conn.lay_e,
        ),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(HERE, "models"))
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    models = [d1_model(), r1_model(), line_model(10), line_model(30)]
    models += [launch_model("A01"), launch_model("A04")]
    for m in models:
        with open(os.path.join(a.out, m["name"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1)
            fh.write("\n")
        print(m["name"], m["kind"], len(m.get("copper", m.get("vias", []))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
