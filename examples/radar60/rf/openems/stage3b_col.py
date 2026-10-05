"""Stage-3b column and feeds analysis: the C1 retune, the D5 series-fed points, the TX feed options
and the D14 PA corner (openEMS runs of `stage3b_em.py`'s models; campaign files in the notes).

  python3 openems/stage3b_col.py RUNS_DIR OUT_DIR [--figs FIG_DIR]

RUNS_DIR/<id>/{result.json, s.csv} as collected by tools/exp/openems_plan.py:
- column cells (ant_sim.py, one port T1.Pg): cell-c1a-NN-rMM[-nz8], cell-c1w-NNN-rMM,
  cell-ser-N-rMM, cell-c1b-*-rMM (rMM = fill 27/20/15 um);
- TX feeds (feed_sim.py, all three driven): txa-<option>[-ll]-r20nz8 / -r15nz8;
- PA corner (pa_sim.py): pa-<pa|pa0>-<tx1|rx4>-<short|open|port>.

Every column point is reported twice:
- in openEMS's own frequency frame (sheet copper, the run's mesh);
- "Palace-referred": the frequency axis scaled by k(mesh), the single-patch offset of the Palace
  validation (palace/validation.md sections 4 and 10.5: the calibrated patch's |S11| dip at 62.36 /
  62.54 / 62.68 GHz for openEMS 27 um, 20 um and 20 um with 8 z-cells, 63.30 GHz for Palace order 3
  with sheet copper, +0.27 GHz with solid copper). k is [D] derived from [S]; it is a single
  patch's offset applied to a column, so it brackets the frequency, it does not replace a
  converged solve. 15 um takes the 20 um 8-z-cell value (the dip rises ~0.03 GHz per um of fill).

Writes OUT_DIR/column.json (every metric below) and the figures. Labels: [S] openEMS, [D] derived.
"""

import argparse
import json
import math
import os
import re

import numpy as np

BAND = (60.3, 63.8)
F3 = (60.3, 62.05, 63.8)
F_TRUE = 63.30 + 0.27  # Palace order 3 sheet + solid-copper shift (single patch) [S]
K_MESH = {  # Palace-referred frequency factor per (fill um, z-cells) [D]
    (27, 4): F_TRUE / 62.36,
    (20, 4): F_TRUE / 62.54,
    (20, 8): F_TRUE / 62.68,
    (15, 4): F_TRUE / 62.68,
    (15, 8): F_TRUE / 62.82,
}
GAIN_MIN = 5.0  # ANT-02 realized broadside gain [dBi]


def load_csv(path):
    hdr = open(path).readline().lstrip("# ").strip().split(",")
    a = np.loadtxt(path, delimiter=",")
    return {h: a[:, i] for i, h in enumerate(hdr)}


def r(x, n=2):
    return None if x is None else round(float(x), n)


def bands(f, s11, lim=-10.0):
    ok = s11 <= lim
    out, i = [], 0
    while i < len(f):
        if ok[i]:
            j = i
            while j + 1 < len(f) and ok[j + 1]:
                j += 1
            out.append([float(f[i]), float(f[j])])
            i = j + 1
        else:
            i += 1
    return out


def main_band(bl, lo=BAND[0], hi=BAND[1]):
    """The RL-10 band with the largest overlap with [lo, hi] (else the nearest one)."""
    if not bl:
        return None
    ov = [max(0.0, min(b[1], hi) - max(b[0], lo)) for b in bl]
    if max(ov) > 0:
        return bl[int(np.argmax(ov))]
    c = (lo + hi) / 2
    return min(bl, key=lambda b: min(abs(b[0] - c), abs(b[1] - c)))


def parse_id(rid):
    m = re.match(r"(cell-[a-z0-9]+-[0-9]+)-r(\d+)(?:-nz(\d+))?$", rid)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 4)


def column_point(d, rid):
    model, fill, nz = parse_id(rid)
    res = json.load(open(os.path.join(d, "result.json")))
    s = load_csv(os.path.join(d, "s.csv"))
    f = s["f_ghz"]
    port = res["excite"]
    s11 = s[f"s_{port}_db"]
    k = K_MESH.get((fill, nz), F_TRUE / 62.68)
    out = dict(model=model, fill_um=fill, nz=nz, k_palace=r(k, 4), cells=res["meta"]["cells"])
    out["wall_s"] = r(res.get("wall_s"), 0)
    for frame, kk in (("oems", 1.0), ("palace_ref", k)):
        fr = f * kk
        inb = (fr >= BAND[0]) & (fr <= BAND[1])
        bl = bands(fr, s11)
        mb = main_band(bl)
        imin = int(np.argmin(np.where((fr > 55) & (fr < 69), s11, 0)))
        out[frame] = dict(
            worst_inband_s11_db=r(s11[inb].max()),
            s11_db_at={str(x): r(np.interp(x, fr, s11)) for x in F3},
            rl10_bands_ghz=[[r(a), r(b)] for a, b in bl],
            main_band_ghz=None if mb is None else [r(mb[0]), r(mb[1])],
            main_band_centre_ghz=None if mb is None else r((mb[0] + mb[1]) / 2),
            main_band_width_ghz=None if mb is None else r(mb[1] - mb[0]),
            covers_band=bool(mb is not None and mb[0] <= BAND[0] and mb[1] >= BAND[1]),
            margin_lo_ghz=None if mb is None else r(BAND[0] - mb[0]),
            margin_hi_ghz=None if mb is None else r(mb[1] - BAND[1]),
            f_s11_min_ghz=r(fr[imin]),
            s11_min_db=r(s11[imin]),
        )
    ff = res.get("far_field", {})
    out["realized_gain_dbi"] = {k2: r(v["realized_gain_broadside_dbi"]) for k2, v in ff.items()}
    out["rad_efficiency"] = {k2: r(v["rad_efficiency"], 3) for k2, v in ff.items()}
    out["gain_ok"] = bool(ff) and all(
        v["realized_gain_broadside_dbi"] >= GAIN_MIN for v in ff.values()
    )
    out["_f"], out["_s11"] = f, s11
    return out


def l_solve(points, frame, target=62.05):
    """L scale that puts the main band's centre on `target` (linear fit over the L sweep)."""
    xs, ys = [], []
    for p in points:
        c = p[frame]["main_band_centre_ghz"]
        if c is not None:
            xs.append(p["params"]["fullwave_l_scale"])
            ys.append(c)
    if len(set(xs)) < 2:
        return None
    a, b = np.polyfit(xs, ys, 1)
    return dict(
        slope_ghz_per_unit=r(a, 2),
        l_scale=r((target - b) / a, 4),
        fit=[[r(x, 3), r(y)] for x, y in zip(xs, ys)],
    )


def cavity_modes(model, f_lo=57.0, f_hi=67.0, dk=3.52):
    """[D] TM_mn0 modes of the L2-L3 bondply under the cell's cut-out: a parallel plate bounded
    by the nearest via rows either side of the cut-out centre (PEC walls at the via centres; the
    windows' loading and the fence's effective wall are ignored, so a mode sits within about 1 %
    of these). Empty for K1 (no windows: the bondply is not excited)."""
    if not model.get("windows"):
        return dict(walls_mm=None, modes=[])
    (x0, y0, x1, y1) = next(iter(model["cutouts"].values()))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    vs = model["vias"]
    left = max(x for x, y, d in vs if abs(y - cy) < 0.4 and x < cx)
    right = min(x for x, y, d in vs if abs(y - cy) < 0.4 and x > cx)
    bot = max(y for x, y, d in vs if abs(x - cx) < 0.4 and y < cy)
    top = min(y for x, y, d in vs if abs(x - cx) < 0.4 and y > cy)
    a, b = right - left, top - bot
    c = 299.792458 / (2 * math.sqrt(dk))
    modes = sorted(
        (c * math.hypot(p / a, q / b), p, q) for p in range(16) for q in range(16) if p + q
    )
    return dict(
        walls_mm=[r(a, 3), r(b, 3)],
        modes=[[r(f), p, q] for f, p, q in modes if f_lo <= f <= f_hi],
    )


# ------------------------------------------------------------------------------- TX feeds
TXP = ("TX1", "TX2", "TX3")


def feed_run(d):
    s = load_csv(os.path.join(d, "s.csv"))
    f = s["f_ghz"]
    row = {}
    for n in TXP:
        if f"s_{n}.P1_db" not in s:
            continue
        db = s[f"s_{n}.P1_db"]
        s21 = 10 ** (db / 20) * np.exp(1j * np.radians(s[f"s_{n}.P1_deg"]))
        s11 = 10 ** (s[f"s_{n}.P0_db"] / 20)
        lost = 1 - np.abs(s21) ** 2 - s11**2
        # the largest narrow dip in 54-70 GHz: |S21| dB below its own quadratic trend
        trend = np.polyval(np.polyfit(f, db, 2), f)
        row[n] = dict(
            s21_db={str(x): r(np.interp(x, f, db), 3) for x in F3},
            lost_pct={str(x): r(100 * np.interp(x, f, lost), 2) for x in F3},
            s11_max_db=r(s[f"s_{n}.P0_db"][(f >= BAND[0]) & (f <= BAND[1])].max()),
            dip_db=r((trend - db).max(), 3),
            _s21=s21,
        )
    for a, b in (("TX1", "TX3"), ("TX2", "TX3"), ("TX1", "TX2")):
        if a in row and b in row:
            za = np.interp(62.05, f, row[a]["_s21"].real) + 1j * np.interp(
                62.05, f, row[a]["_s21"].imag
            )
            zb = np.interp(62.05, f, row[b]["_s21"].real) + 1j * np.interp(
                62.05, f, row[b]["_s21"].imag
            )
            dph = math.degrees(np.angle(za * np.conj(zb)))
            row[f"skew_{a}-{b}_ps"] = r(dph / 360 / 62.05 * 1e3, 3)
    for n in TXP:
        if n in row:
            row[n].pop("_s21")
    return row, f, s


def tx_summary(runs):
    out = {}
    for rid in sorted(os.listdir(runs)):
        m = re.match(r"txa-([A-Za-z0-9]+)(-ll)?-(r\d+nz\d+)$", rid)
        d = os.path.join(runs, rid)
        if not m or not os.path.exists(os.path.join(d, "s.csv")):
            continue
        opt, ll, mesh = m.group(1), bool(m.group(2)), m.group(3)
        row, _, _ = feed_run(d)
        out.setdefault(mesh, {}).setdefault(opt, {})["lossless" if ll else "lossy"] = row
    for mesh, opts in out.items():
        for opt, v in opts.items():
            if "lossy" in v and "lossless" in v:
                v["split"] = {}
                for n in TXP:
                    if n in v["lossy"] and n in v["lossless"]:
                        lo = -v["lossy"][n]["s21_db"]["62.05"]
                        ll = -v["lossless"][n]["s21_db"]["62.05"]
                        v["split"][n] = dict(
                            total_db=r(lo, 3), radiated_db=r(ll, 3), dissipated_db=r(lo - ll, 3)
                        )
    return out


# ------------------------------------------------------------------------------- PA corner
def pa_summary(runs):
    out = {}
    for line in ("tx1", "rx4"):
        base = os.path.join(runs, f"pa-pa0-{line}-short")
        if not os.path.exists(os.path.join(base, "s.csv")):
            continue
        b = load_csv(os.path.join(base, "s.csv"))
        f = b["f_ghz"]
        port = line.upper()
        thru = next(
            (
                k[2:-3]
                for k in b
                if k.startswith(f"s_{port}.P")
                and k.endswith("_db")
                and not k.startswith(f"s_{port}.Pb")
            ),
            None,
        )
        res = {"through_port": thru}
        inb = (f >= BAND[0]) & (f <= BAND[1])
        for mode in ("short", "open", "port"):
            d = os.path.join(runs, f"pa-pa-{line}-{mode}")
            if not os.path.exists(os.path.join(d, "s.csv")):
                continue
            s = load_csv(os.path.join(d, "s.csv"))
            js = json.load(open(os.path.join(d, "result.json")))
            row = {}
            for pn in [k[2:-3] for k in s if k.startswith("s_") and k.endswith("_db")]:
                if f"s_{pn}_db" not in b:
                    continue
                dd = s[f"s_{pn}_db"] - b[f"s_{pn}_db"]
                dp = (s[f"s_{pn}_deg"] - b[f"s_{pn}_deg"] + 180) % 360 - 180
                row[pn] = dict(
                    d_db_inband_max=r(np.abs(dd[inb]).max(), 3),
                    d_deg_inband_max=r(np.abs(dp[inb]).max(), 2),
                    d_db_54_70_max=r(np.abs(dd).max(), 3),
                    db_at={str(x): r(np.interp(x, f, s[f"s_{pn}_db"]), 3) for x in F3},
                )
            if thru:
                db = s[f"s_{thru}_db"]
                trend = np.polyval(np.polyfit(f, db, 2), f)
                row["through_dip_db"] = r((trend - db).max(), 3)
            row["pa_coupling_db"] = js.get("pa_coupling_db")
            res[mode] = row
        out[line] = res
    return out


# ------------------------------------------------------------------------------- figures
COL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def figures(S, pts, figs):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(figs, exist_ok=True)

    def style(ax, title):
        ax.axvspan(*BAND, color="#d9d9d4", alpha=0.45, lw=0)
        ax.axhline(-10, color="#8a8a85", lw=0.8, ls="--")
        ax.set_title(title, fontsize=9, loc="left")
        ax.set_xlim(55, 69)
        ax.set_ylim(-35, 0)
        ax.grid(color="#ececea", lw=0.6)
        ax.tick_params(labelsize=8)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)

    # C1 L sweep per mesh, openEMS frame and Palace-referred
    ls = sorted({p["params"]["fullwave_l_scale"] for p in pts if p["group"] == "L"})
    meshes = sorted({(p["fill_um"], p["nz"]) for p in pts if p["group"] == "L"}, reverse=True)
    if meshes:
        fig, axs = plt.subplots(2, len(meshes), figsize=(3.4 * len(meshes), 5.6), squeeze=False)
        for j, (fill, nz) in enumerate(meshes):
            for row, frame in enumerate(("oems", "palace_ref")):
                ax = axs[row][j]
                for i, L in enumerate(ls):
                    p = next(
                        (
                            q
                            for q in pts
                            if q["group"] == "L"
                            and q["fill_um"] == fill
                            and q["nz"] == nz
                            and q["params"]["fullwave_l_scale"] == L
                        ),
                        None,
                    )
                    if p is None:
                        continue
                    k = 1.0 if frame == "oems" else p["k_palace"]
                    ax.plot(p["_f"] * k, p["_s11"], color=COL[i], lw=1.4, label=f"L x{L}")
                lab = "openEMS frame" if frame == "oems" else "Palace-referred"
                style(ax, f"{fill} um, {nz} z-cells: {lab}")
                if j == 0:
                    ax.set_ylabel("|S11| at Pg (dB)", fontsize=8)
                if row == 1:
                    ax.set_xlabel("GHz", fontsize=8)
        axs[0][0].legend(fontsize=7, frameon=False, loc="lower left")
        fig.suptitle(
            "C1 corporate column (centre cell, inset 0.325, w35 0.353) [S]; grey = 60.3-63.8 GHz",
            fontsize=9,
        )
        fig.tight_layout()
        fig.savefig(os.path.join(figs, "c1-l-sweep-mesh.png"), dpi=150)
        plt.close(fig)
    # C1 inset / w35 at 20 um round L 1.025, plus D5 series at 27 um
    groups = [
        (
            "c1-inset-w35-r20.png",
            [
                p
                for p in pts
                if p["fill_um"] == 20
                and p["nz"] == 4
                and p["group"] in ("L", "inset", "w35")
                and p["params"]["fullwave_l_scale"] == 1.025
            ]
            + [p for p in pts if p["group"] == "c1b" and p["fill_um"] == 20],
            "C1 at 20 um, L x1.025: inset / w35 / t_y [S], Palace-referred",
        ),
        (
            "d5-series-vs-corporate-r20.png",
            [p for p in pts if p["fill_um"] == 20 and p["nz"] == 4 and p["group"] in ("L", "ser")],
            "D5 at 20 um: corporate (L sweep) vs series-fed [S], Palace-referred",
        ),
        (
            "c1-k1-vs-k0-r20.png",
            [p for p in pts if p["fill_um"] == 20 and p["nz"] == 4 and p["group"] in ("L", "K1")],
            "C1 at 20 um: windows over the bondply (K0) vs solid L2 (K1) [S], Palace-referred",
        ),
    ]
    for name, sel, title in groups:
        if not sel:
            continue
        fig, ax = plt.subplots(figsize=(6.4, 3.8))
        for i, p in enumerate(sel[: len(COL)]):
            pr = p["params"]
            lab = (
                f"{pr['column'][:4]} L x{pr['fullwave_l_scale']} i {pr['inset']}"
                + (
                    f" w35 {pr['w35']}"
                    if pr["column"] == "corporate"
                    else f" link x{pr['ser_link_scale']}"
                )
                + (f" t_y {pr['t_y']}" if p["group"] == "c1b" else "")
            )
            ax.plot(p["_f"] * p["k_palace"], p["_s11"], color=COL[i], lw=1.3, label=lab)
        cav = next(
            (p["cavity_d"] for p in sel if p.get("cavity_d") and p["cavity_d"]["modes"]), None
        )
        style(ax, title)
        if cav:  # [D] bondply modes, on the openEMS axis scaled like the curves
            k = sel[0]["k_palace"]
            for fm, _, _ in cav["modes"]:
                ax.plot([fm * k] * 2, [-35, -31], color="#4a4a46", lw=1.0)
            ax.text(55.2, -33.5, "ticks: L2-L3 cavity modes [D]", fontsize=6.5, color="#4a4a46")
        ax.set_xlabel("GHz", fontsize=8)
        ax.set_ylabel("|S11| at Pg (dB)", fontsize=8)
        ax.legend(fontsize=6.5, frameon=False, loc="lower left")
        fig.tight_layout()
        fig.savefig(os.path.join(figs, name), dpi=150)
        plt.close(fig)
    # TX feed options: TX1 loss split per option (stacked: radiated + dissipated)
    for mesh, opts in S.get("tx", {}).items():
        names = [o for o in ("T0", "S1", "T2", "T4", "B", "C") if o in opts]
        if not names:
            continue
        fig, ax = plt.subplots(figsize=(6.4, 3.4))
        x = np.arange(len(names))
        for j, n in enumerate(TXP):
            tot, rad = [], []
            for o in names:
                v = opts[o]
                lo = v.get("lossy", {}).get(n, {}).get("s21_db", {}).get("62.05")
                ll = v.get("lossless", {}).get(n, {}).get("s21_db", {}).get("62.05")
                tot.append(-lo if lo is not None else np.nan)
                rad.append(-ll if ll is not None else 0.0)
            w = 0.26
            ax.bar(x + (j - 1) * w, tot, w - 0.02, color=COL[j], alpha=0.35, label=f"{n} total")
            ax.bar(x + (j - 1) * w, rad, w - 0.02, color=COL[j], label=f"{n} lossless (radiated)")
        ax.axhline(1.5, color="#8a8a85", lw=0.8, ls="--")
        ax.set_xticks(x, names, fontsize=8)
        ax.set_ylabel("P0->P1 loss at 62.05 GHz (dB)", fontsize=8)
        ax.set_title(
            f"TX feeds, {mesh} [S]: total vs lossless (radiated/leaked)", fontsize=9, loc="left"
        )
        ax.grid(axis="y", color="#ececea", lw=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.legend(fontsize=6.5, frameon=False, ncol=3, loc="upper left")
        fig.tight_layout()
        fig.savefig(os.path.join(figs, f"tx-options-{mesh}.png"), dpi=150)
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("runs")
    ap.add_argument("out")
    ap.add_argument("--models", help="model dir (column parameters); default RUNS/../models")
    ap.add_argument("--figs")
    a = ap.parse_args()
    models = a.models or os.path.join(os.path.dirname(os.path.abspath(a.runs)), "models")
    pts = []
    for rid in sorted(os.listdir(a.runs)):
        d = os.path.join(a.runs, rid)
        if not parse_id(rid) or not os.path.exists(os.path.join(d, "s.csv")):
            continue
        p = column_point(d, rid)
        mp = os.path.join(models, f"{p['model']}.json")
        mj = json.load(open(mp)) if os.path.exists(mp) else {}
        p["params"] = mj.get("params", {})
        p["cavity_d"] = cavity_modes(mj) if "vias" in mj else None
        pr = p["params"]
        if pr.get("column") == "series":
            p["group"] = "ser"
        elif pr.get("l23_cavity") == "K1":
            p["group"] = "K1"
        elif p["model"].startswith("cell-c1b"):
            p["group"] = "c1b"
        elif abs(pr.get("w35", 0.353) - 0.353) > 1e-6:
            p["group"] = "w35"
        elif abs(pr.get("inset", 0.325) - 0.325) > 1e-6:
            p["group"] = "inset"
        else:
            p["group"] = "L"
        p["id"] = rid
        pts.append(p)
    S = dict(k_palace={f"{k[0]}um-nz{k[1]}": r(v, 4) for k, v in K_MESH.items()}, column={})
    for p in pts:
        S["column"][p["id"]] = {k: v for k, v in p.items() if not k.startswith("_")}
    S["l_solve"] = {}
    for fill, nz in sorted({(p["fill_um"], p["nz"]) for p in pts}):
        grp = [p for p in pts if p["group"] == "L" and p["fill_um"] == fill and p["nz"] == nz]
        for frame in ("oems", "palace_ref"):
            sol = l_solve(grp, frame)
            if sol:
                S["l_solve"][f"{fill}um-nz{nz}-{frame}"] = sol
    S["tx"] = tx_summary(a.runs)
    S["pa"] = pa_summary(a.runs)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "column.json"), "w") as fh:
        json.dump(S, fh, indent=1)
        fh.write("\n")
    if a.figs:
        figures(S, pts, a.figs)
    for p in pts:
        o, q = p["oems"], p["palace_ref"]
        print(
            f"{p['id']:24s} {p['group']:5s} band {o['main_band_ghz']} worst {o['worst_inband_s11_db']}"
            f" | ref {q['main_band_ghz']} worst {q['worst_inband_s11_db']} | G {p['realized_gain_dbi']}"
        )
    print(json.dumps(S["l_solve"], indent=1))


if __name__ == "__main__":
    main()
