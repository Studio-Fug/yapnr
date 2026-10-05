"""Figures of the stage-3b EM bank and ground runs (s3b_bank_analysis.py inputs and summary).

  python3 openems/s3b_bank_plots.py RUNS_DIR SUMMARY_JSON OUT_DIR

Writes OUT_DIR/em-bank-d15.png (isolation, active reflection, plane-pair poles for the pours
A/B/C), em-bank-cavity.png (the triplets K0/K2/K1) and em-bank-s1-s2.png (dummies). Evidence
label [S] (openEMS); nothing is measured.
"""

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s3b_bank_analysis as A  # noqa: E402

# reference palette (dataviz skill, light mode): categorical slots 1-3, text and grid tokens
SERIES = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def style(ax, title, xl, yl):
    ax.set_title(title, fontsize=9, color=INK, loc="left")
    ax.set_xlabel(xl, fontsize=8, color=INK2)
    ax.set_ylabel(yl, fontsize=8, color=INK2)
    ax.tick_params(labelsize=7, colors=INK2)
    ax.grid(True, color=GRID, lw=0.6)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.set_facecolor(SURF)


def band_shade(ax):
    ax.axvspan(60.3, 63.8, color="#f0efec", zorder=0)


def worst_coupling(B):
    f, M = B["f"], B["M"]
    w = np.full(len(f), -200.0)
    for t in A.TXP:
        for rx in A.RXP:
            s = M[:, A.PORTS.index(rx), A.PORTS.index(t)]
            if not np.any(np.isnan(s)):
                w = np.maximum(w, 20 * np.log10(np.abs(s) + 1e-30))
    return f, w


def fig_d15(runs, S, out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(2, 2, figsize=(11, 7.4), facecolor=SURF)
    ax = axs[0, 0]
    band_shade(ax)
    for v in ("A", "B", "C"):
        B = A.bank(runs, MODELS, v)
        if B is None:
            continue
        f, w = worst_coupling(B)
        iso = S["banks"][v]["isolation"]["isolation_db"]
        ax.plot(f, w, color=SERIES[v], lw=2, label=f"{v}: {iso:.1f} dB in band")
    ax.axhline(-35, color=INK2, lw=1, ls="--")
    ax.axhline(-27, color=INK2, lw=1, ls=":")
    ax.text(58.1, -34.5, "35 dB design", fontsize=7, color=INK2)
    ax.text(58.1, -26.5, "27 dB required", fontsize=7, color=INK2)
    style(ax, "Worst TX-to-RX coupling (12 pairs, both banks, Pg)", "GHz", "dB")
    ax.legend(fontsize=7, frameon=False)
    for k, (bk, key) in enumerate((("TX", "active_tx"), ("RX", "active_rx"))):
        ax = axs[0, 1] if k == 0 else axs[1, 0]
        for v in ("A", "B", "C"):
            b = S["banks"].get(v)
            if b is None:
                continue
            pc = b[key]["per_code"]
            th = sorted(int(x) for x in pc)
            y = [max(x for x in pc[str(t)].values() if x is not None) for t in th]
            ax.plot(th, y, "-o", color=SERIES[v], lw=2, ms=5, label=v)
        ax.axhline(-10, color=INK2, lw=1, ls="--")
        q = " (codes in 5.625 deg steps)" if bk == "TX" else " (ideal phases)"
        style(
            ax,
            f"{bk} bank: worst active reflection over 60.3-63.8 GHz{q}",
            "scan angle (deg)",
            "dB",
        )
        ax.legend(fontsize=7, frameon=False)
    ax = axs[1, 1]
    ax.axvspan(57, 70, color="#f0efec", zorder=0)
    ax.axhline(20, color=INK2, lw=1, ls="--")
    mk = {"core": "o", "bond": "^"}
    for v in ("A", "B", "C"):
        pp = S["pp"].get(v)
        if not pp:
            continue
        for tag, alpha in (("r27", 0.35), ("r20", 0.6), ("r15", 1.0)):
            row = pp["meshes"].get(tag)
            if not row:
                continue
            for pr in row["probes"].values():
                for t in pr["trapped"]:
                    ax.scatter(
                        t["f_ghz"],
                        t["q"],
                        s=40,
                        marker=mk[pr["pair"]],
                        color=SERIES[v],
                        alpha=alpha,
                        edgecolors=SURF,
                        linewidths=1,
                    )
    for v in ("A", "B", "C"):
        ax.scatter([], [], color=SERIES[v], label=v, s=30)
    ax.scatter([], [], marker="o", color=INK2, label="L1-L2 core", s=30)
    ax.scatter([], [], marker="^", color=INK2, label="L2-L3 bond", s=30)
    ax.set_xlim(54, 72)
    style(
        ax,
        "Plane-pair ring-down poles, Q >= 20 (27/20/15 um: light to dark)",
        "GHz",
        "Q",
    )
    ax.legend(fontsize=7, frameon=False, ncol=2)
    fig.suptitle(
        "radar60 stage 3b, D15 open-pour variants [S] openEMS, 40 um shared mesh (banks), "
        "27/20/15 um (plane pair)",
        fontsize=10,
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(os.path.join(out, "em-bank-d15.png"), dpi=150)


def fig_cavity(runs, S, out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    col = {"K0": SERIES["A"], "K2": SERIES["B"], "K1": SERIES["C"]}
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.8), facecolor=SURF)
    for k in ("K0", "K2", "K1"):
        for mesh, ls in (("r27", "-"), ("r20", "--")):
            v = A.run(runs, f"tri-{k}-{mesh}")
            if v is None:
                continue
            f, d = v["s"]["f_ghz"], v["s"]
            g = A.cs(d, "T1.Pg")
            axs[0].plot(
                f, 20 * np.log10(np.abs(g)), ls=ls, color=col[k], lw=2, label=f"{k} {mesh[1:]} um"
            )
            pj = os.path.join(runs, f"tri-{k}-{mesh}", "probes.json")
            if os.path.exists(pj):
                P = json.load(open(pj))
                fs = np.array(P["f_ghz"])
                tr = [np.array(P[n]["transfer_db"]) for n in P if n.startswith("pb_RX_m")]
                if tr:
                    axs[1].plot(fs, tr[0], ls=ls, color=col[k], lw=2, label=f"{k} {mesh[1:]} um")
    for ax in axs:
        band_shade(ax)
        ax.legend(fontsize=7, frameon=False)
    axs[0].axhline(-10, color=INK2, lw=1, ls="--")
    style(axs[0], "Centre column |S11| at Pg (triplet)", "GHz", "dB")
    style(axs[1], "Port to bondply (L3-L2, middle column gap) transfer", "GHz", "dB")
    fig.suptitle(
        "radar60 stage 3b, L2-L3 cavity options: K0 ring only, K2 posts, K1 solid L2 [S]",
        fontsize=10,
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(os.path.join(out, "em-bank-cavity.png"), dpi=150)


def fig_s1(runs, S, out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if "S1" not in S["banks"] or "A" not in S["banks"]:
        return
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.8), facecolor=SURF)
    cols = {"S2": SERIES["A"], "S1": SERIES["B"]}
    for tag, v in (("S2", "A"), ("S1", "S1")):
        b = S["banks"][v]
        for p, key, ls in (("TX1", "active_tx", "-"), ("RX4", "active_rx", "--")):
            pc = b[key]["per_code"]
            th = sorted(int(x) for x in pc)
            axs[0].plot(
                th,
                [pc[str(t)][p] for t in th],
                ls=ls,
                marker="o",
                ms=5,
                color=cols[tag],
                lw=2,
                label=f"{p} {tag}",
            )
        B = A.bank(runs, MODELS, v)
        ff = B["runs"]["TX1"]["r"]["far_field"]["62.05"]
        th, _, g = A.copol(ff, "h")
        axs[1].plot(th, g, color=cols[tag], lw=2, label=f"TX1 {tag}")
        ff2 = B["runs"]["TX2"]["r"]["far_field"]["62.05"]
        th, _, g2 = A.copol(ff2, "h")
        axs[1].plot(th, g2, color=cols[tag], lw=1, ls=":", label=f"TX2 {tag}")
    axs[0].axhline(-10, color=INK2, lw=1, ls="--")
    style(axs[0], "Edge elements: worst active reflection per scan code", "scan angle (deg)", "dB")
    axs[1].set_xlim(-90, 90)
    axs[1].set_ylim(-15, 10)
    style(axs[1], "Embedded H-plane realized gain at 62.05 GHz", "theta (deg)", "dBi")
    for ax in axs:
        ax.legend(fontsize=7, frameon=False, ncol=2)
    fig.suptitle(
        "radar60 stage 3b, dummies: S2 (all four) against S1 (open-end only), pour A [S]",
        fontsize=10,
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(os.path.join(out, "em-bank-s1-s2.png"), dpi=150)


if __name__ == "__main__":
    RUNS, SUMMARY, OUT = sys.argv[1:4]
    MODELS = sys.argv[4] if len(sys.argv) > 4 else os.path.join(os.path.dirname(RUNS), "models")
    os.makedirs(OUT, exist_ok=True)
    S = json.load(open(SUMMARY))
    if S["banks"]:
        fig_d15(RUNS, S, OUT)
    if S["triplets"]:
        fig_cavity(RUNS, S, OUT)
    fig_s1(RUNS, S, OUT)
    print("figures in", OUT)
