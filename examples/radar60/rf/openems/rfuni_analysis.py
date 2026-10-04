"""Analysis of the RF-uniformity bank, cell and feed runs (review fixes, 2026-10-04).

  python3 openems/rfuni_analysis.py RUNS_DIR MODELS_DIR OUT_DIR

RUNS_DIR/<id>/{result.json, s.csv} as collected by tools/exp/openems_plan.py: the bank runs
rf-s2-e<port> (dummies) and rf-s0-e<port> (none), rf-s2short/open-eTX1[-m], cell-n-r<mesh>,
txa-all-r<mesh>[-nz<n>] and txa-ll-r<mesh> (lossless). MODELS_DIR holds rf-s2.json and rf-s0.json.
Writes OUT_DIR/summary.json: complex reflection differences edge vs interior (RMS over 60.3-63.8
GHz and the best constant rotation), coupling, TX-RX isolation, active reflection, embedded
co-polar patterns and fitted phase centres, the load variants, the cell and feed mesh series.
Evidence label [S] (openEMS); nothing is measured.
"""

import json
import math
import os
import sys

import numpy as np

RUNS, MODELS, OUT = sys.argv[1:4]
BEFORE = sys.argv[4] if len(sys.argv) > 4 else None
os.makedirs(OUT, exist_ok=True)
F3 = (60.3, 62.05, 63.8)
C0 = 299792458.0
ETA0 = 376.730313668


def load_csv(path):
    hdr = open(path).readline().lstrip("# ").strip().split(",")
    a = np.loadtxt(path, delimiter=",")
    return {h: a[:, i] for i, h in enumerate(hdr)}


def run(name):
    d = os.path.join(RUNS, name)
    if not os.path.exists(os.path.join(d, "s.csv")):
        return None
    r = json.load(open(os.path.join(d, "result.json")))
    for fk, v in r.get("far_field", {}).items():
        v["_f_ghz"] = float(fk)
    return dict(s=load_csv(os.path.join(d, "s.csv")), r=r)


def r3(x, n=3):
    return round(float(x), n)


def cs(d, port):
    """Complex S of `port` in an ant_sim run (re/im columns)."""
    return d[f"s_{port}_re"] + 1j * d[f"s_{port}_im"]


S = {}

# ============================================================== banks (both banks, one board)
RXP = ["RX1", "RX2", "RX3", "RX4"]
TXP = ["TX1", "TX2", "TX3"]
PORTS = RXP + TXP


def bank(tag):
    runs = {p: run(f"rf-{tag}-e{p}") for p in PORTS}
    if any(v is None for v in runs.values()):
        missing = [p for p, v in runs.items() if v is None]
        print(tag, "missing", missing)
        if all(v is None for v in runs.values()):
            return None
    f = next(v for v in runs.values() if v is not None)["s"]["f_ghz"]
    n = len(PORTS)
    M = np.full((len(f), n, n), np.nan + 0j)
    loads = {}
    for j, pj in enumerate(PORTS):
        v = runs[pj]
        if v is None:
            continue
        for i, pi in enumerate(PORTS):
            M[:, i, j] = cs(v["s"], f"{pi}.Pg")
        loads[pj] = {k: v["r"]["loads"][k]["db_at"] for k in v["r"].get("loads", {})}
    for j in range(n):  # a column not driven: reciprocity fills its off-diagonal entries
        for i in range(n):
            if i != j and np.all(np.isnan(M[:, i, j])) and not np.all(np.isnan(M[:, j, i])):
                M[:, i, j] = M[:, j, i]
    model = json.load(open(os.path.join(MODELS, f"rf-{tag}.json")))
    return dict(f=f, M=M, runs=runs, loads=loads, model=model)


def band_mask(f):
    return (f >= 60.3 - 1e-9) & (f <= 63.8 + 1e-9)


def gamma_compare(B, a, b):
    """Complex reflection of ports a and b: |dGamma| RMS/max over 60.3-63.8 GHz, and the best
    constant rotation (angle, residual RMS) mapping b onto a."""
    f, M = B["f"], B["M"]
    ia, ib = PORTS.index(a), PORTS.index(b)
    ga, gb = M[:, ia, ia], M[:, ib, ib]
    if np.any(np.isnan(ga)) or np.any(np.isnan(gb)):
        return None
    m = band_mask(f)
    dg = np.abs(ga - gb)[m]
    # best rotation: argmin |ga - gb e^{j t}| -> t = angle(sum ga conj(gb))
    t = np.angle(np.sum(ga[m] * np.conj(gb[m])))
    res = np.abs(ga[m] - gb[m] * np.exp(1j * t))
    return dict(
        dgamma_rms=r3(np.sqrt(np.mean(dg**2))),
        dgamma_max=r3(dg.max()),
        rotation_deg=r3(np.degrees(t), 1),
        residual_after_rotation_rms=r3(np.sqrt(np.mean(res**2))),
        s11_db={
            a: {
                str(x): r3(
                    20 * np.log10(abs(np.interp(x, f, ga.real) + 1j * np.interp(x, f, ga.imag))), 2
                )
                for x in F3
            },
            b: {
                str(x): r3(
                    20 * np.log10(abs(np.interp(x, f, gb.real) + 1j * np.interp(x, f, gb.imag))), 2
                )
                for x in F3
            },
        },
    )


def at_c(f, z, x):
    return np.interp(x, f, z.real) + 1j * np.interp(x, f, z.imag)


def active(B, bank_ports, thetas=(0, 15, 30, 45, -15, -30, -45)):
    """Active reflection of each element of a bank, uniform amplitude, progressive phase for a
    scan angle theta along the array axis (x, from the phase centres), over 60.3-63.8 GHz."""
    f, M = B["f"], B["M"]
    pc = B["model"]["phase_centres"]
    idx = [PORTS.index(p) for p in bank_ports]
    m = band_mask(f)
    out = {}
    for th in thetas:
        row = {}
        for i, pi in zip(idx, bank_ports):
            worst = 0.0
            for k in np.nonzero(m)[0]:
                kk = 2 * math.pi * f[k] * 1e9 / C0 * 1e-3  # rad/mm
                a = {
                    p: np.exp(-1j * kk * pc[p][0] * math.sin(math.radians(th))) for p in bank_ports
                }
                g = sum(M[k, i, PORTS.index(p)] * a[p] for p in bank_ports) / a[pi]
                worst = max(worst, abs(g)) if not np.isnan(g) else float("nan")
            row[pi] = r3(20 * math.log10(worst), 2) if worst == worst and worst > 0 else None
        out[str(th)] = row
    return out


def coupling(B, pairs):
    f, M = B["f"], B["M"]
    m = band_mask(f)
    out = {}
    for a, b in pairs:
        s = M[:, PORTS.index(b), PORTS.index(a)]
        if np.all(np.isnan(s)):
            continue
        out[f"{a}->{b}"] = dict(
            max_db=r3(20 * np.log10(np.abs(s[m]).max()), 1),
            at_db={str(x): r3(20 * np.log10(abs(at_c(f, s, x))), 1) for x in F3},
        )
    return out


def isolation(B):
    f, M = B["f"], B["M"]
    m = band_mask(f)
    worst = (-999, None)
    rows = {}
    for t in TXP:
        for r in RXP:
            s = M[:, PORTS.index(r), PORTS.index(t)]
            if np.all(np.isnan(s)):
                continue
            v = 20 * np.log10(np.abs(s[m]).max())
            rows[f"{t}->{r}"] = r3(v, 1)
            if v > worst[0]:
                worst = (v, f"{t}->{r}")
    return dict(max_db=r3(worst[0], 1), pair=worst[1], all=rows)


def copol(ff, plane):
    """Co-polar complex far field of a cut (E-plane: E_theta, H-plane: E_phi for the y-polarised
    patches), and the realized gain dBi (per unit incident power, r = 1 m)."""
    c = ff["complex_cuts"]
    th = np.array(ff["cut_theta_deg"], float)
    et = np.array([complex(*v) for v in c[f"{plane}_E_theta"]])
    ep = np.array([complex(*v) for v in c[f"{plane}_E_phi"]])
    e = et if np.sum(np.abs(et) ** 2) >= np.sum(np.abs(ep) ** 2) else ep
    if "nf2ff_centre_m" not in ff:  # runs that passed the centre in mm, read by openEMS as m
        cen = np.array(ff["nf2ff_centre"])
        k = 2 * math.pi * float(ff.get("_f_ghz", 0) or 0) * 1e9 / C0
        e = e * np.exp(
            1j * k * (cen[0] if plane == "h" else cen[1]) * (1 - 1e-3) * np.sin(np.radians(th))
        )
    g = 4 * math.pi * np.abs(e) ** 2 / (2 * ETA0)
    return th, e, 10 * np.log10(np.maximum(g, 1e-12))


def phase_centre(th, e, f_ghz, span=40.0):
    """Least-squares phase centre offset (mm) along the cut's axis and in z from the co-pol phase
    over |theta| <= span: psi = psi0 + k (d sin(theta) + dz cos(theta))."""
    m = np.abs(th) <= span
    psi = np.unwrap(np.angle(e[m]))
    k = 2 * math.pi * f_ghz * 1e9 / C0 * 1e-3
    t = np.radians(th[m])
    A = np.stack([np.ones_like(t), k * np.sin(t), k * np.cos(t)], 1)
    sol, *_ = np.linalg.lstsq(A, psi, rcond=None)
    fit = A @ sol
    return dict(
        d_mm=r3(sol[1], 4),
        dz_mm=r3(sol[2], 4),
        rms_deg=r3(np.degrees(np.sqrt(np.mean((psi - fit) ** 2))), 2),
    )


def patterns(B, a, b, span=45.0):
    """Embedded co-pol patterns of ports a and b (each about its own design phase centre):
    worst realized-gain and phase difference within +-span, both cuts, at the three
    frequencies; fitted phase centres."""
    if B["runs"].get(a) is None or B["runs"].get(b) is None:
        return None
    ra, rb = B["runs"][a]["r"], B["runs"][b]["r"]
    out = {}
    for fk in ra["far_field"]:
        row = {}
        for plane, axis in (("h", "x"), ("e", "y")):
            th, ea, ga = copol(ra["far_field"][fk], plane)
            _, eb, gb = copol(rb["far_field"][fk], plane)
            m = np.abs(th) <= span
            dph = np.degrees(np.angle(ea[m] * np.conj(eb[m])))
            dph0 = dph - dph[np.argmin(np.abs(th[m]))]  # relative to broadside
            row[plane] = dict(
                gain_diff_max_db=r3(np.max(np.abs(ga[m] - gb[m])), 2),
                broadside_gain_dbi={
                    a: r3(ga[np.argmin(np.abs(th))], 2),
                    b: r3(gb[np.argmin(np.abs(th))], 2),
                },
                phase_diff_pp_deg=r3(np.ptp(dph0), 1),
                phase_centre={
                    a: phase_centre(th, ea, float(fk)),
                    b: phase_centre(th, eb, float(fk)),
                },
            )
        out[fk] = row
    return out


for tag in ("s2", "s0", "s2short", "s2open"):
    if tag in ("s2short", "s2open"):
        continue
    B = bank(tag)
    if B is None:
        continue
    S[f"bank_{tag}"] = dict(
        gamma=dict(
            TX1_vs_TX2=gamma_compare(B, "TX1", "TX2"),
            TX3_vs_TX2=gamma_compare(B, "TX3", "TX2"),
            RX1_vs_RX2=gamma_compare(B, "RX1", "RX2"),
            RX4_vs_RX3=gamma_compare(B, "RX4", "RX3"),
            RX4_vs_RX2=gamma_compare(B, "RX4", "RX2"),
            RX2_vs_RX3=gamma_compare(B, "RX2", "RX3"),
        ),
        coupling=coupling(
            B,
            [
                ("TX1", "TX2"),
                ("TX2", "TX3"),
                ("TX1", "TX3"),
                ("TX2", "TX1"),
                ("TX3", "TX2"),
                ("RX1", "RX2"),
                ("RX2", "RX3"),
                ("RX3", "RX4"),
                ("RX1", "RX3"),
                ("RX1", "RX4"),
            ],
        ),
        loads=B["loads"],
        isolation=isolation(B),
        active_tx=active(B, TXP),
        active_rx=active(B, RXP),
        patterns=dict(
            TX1_vs_TX2=patterns(B, "TX1", "TX2"),
            TX3_vs_TX2=patterns(B, "TX3", "TX2"),
            RX1_vs_RX2=patterns(B, "RX1", "RX2"),
            RX4_vs_RX3=patterns(B, "RX4", "RX3"),
            RX4_vs_RX2=patterns(B, "RX4", "RX2"),
            RX2_vs_RX3=patterns(B, "RX2", "RX3"),
        ),
        far_field={
            p: {
                fk: dict(rg=r3(v["realized_gain_broadside_dbi"], 2), eff=r3(v["rad_efficiency"], 3))
                for fk, v in B["runs"][p]["r"]["far_field"].items()
            }
            for p in PORTS
            if B["runs"][p]
        },
        rl10={p: B["runs"][p]["r"]["rl10_bands_ghz"] for p in PORTS if B["runs"][p]},
        cells=next((v["r"]["meta"]["cells"] for v in B["runs"].values() if v), None),
        runs=sorted(p for p in PORTS if B["runs"].get(p)),
    )
    S[f"_bank_{tag}"] = B

# dummy loads shorted / open (TX1 driven) against 50 ohm
for mode in ("short", "open"):
    v = run(f"rf-s2{mode}-eTX1-m") or run(f"rf-s2{mode}-eTX1")  # -m: the 50 ohm run's mesh
    B = S.get("_bank_s2")
    if v is None or B is None:
        continue
    f = v["s"]["f_ghz"]
    g = cs(v["s"], "TX1.Pg")
    g50 = B["M"][:, PORTS.index("TX1"), PORTS.index("TX1")]
    m = band_mask(f)
    dg = np.abs(g - g50)[m]
    ff = v["r"]["far_field"]
    ff50 = B["runs"]["TX1"]["r"]["far_field"]
    pat = {}
    for fk in ff:
        th, e1, g1 = copol(ff[fk], "h")
        _, e0, g0 = copol(ff50[fk], "h")
        mm = np.abs(th) <= 45
        pat[fk] = r3(np.max(np.abs(g1[mm] - g0[mm])), 2)
    S[f"loads_{mode}_unmatched_mesh"] = None
    w = run(f"rf-s2{mode}-eTX1")
    if w is not None and v is not w:
        gw = cs(w["s"], "TX1.Pg")
        S[f"loads_{mode}_unmatched_mesh"] = dict(
            mesh=w["r"]["meta"]["mesh"],
            dgamma_vs_50_rms=r3(np.sqrt(np.mean(np.abs(gw - g50)[m] ** 2))),
        )
    S[f"loads_{mode}"] = dict(
        mesh=v["r"]["meta"]["mesh"],
        dgamma_vs_50_rms=r3(np.sqrt(np.mean(dg**2))),
        dgamma_vs_50_max=r3(dg.max()),
        s11_db={str(x): r3(20 * np.log10(abs(at_c(f, g, x))), 2) for x in F3},
        broadside_rg={
            fk: r3(
                ff[fk]["realized_gain_broadside_dbi"] - ff50[fk]["realized_gain_broadside_dbi"], 2
            )
            for fk in ff
        },
        h_plane_gain_diff_max_45=pat,
        coupling_tx2_db={
            str(x): r3(20 * np.log10(abs(at_c(f, cs(v["s"], "TX2.Pg"), x))), 1) for x in F3
        },
    )

# ============================================================== cell mesh series
cell = {}
ref = None
for tag, h in (("r40", 40), ("r27", 27), ("r20", 20), ("r15", 15)):
    v = run(f"cell-n-{tag}")
    if v is None:
        continue
    f = v["s"]["f_ghz"]
    g = cs(v["s"], "TX2.Pg")
    row = dict(
        fill_um=h,
        cells=v["r"]["meta"]["cells"],
        wall_s=round(v["r"]["wall_s"]),
        s11_db={str(x): r3(20 * np.log10(abs(at_c(f, g, x))), 2) for x in F3},
        s11_deg={str(x): r3(np.degrees(np.angle(at_c(f, g, x))), 1) for x in F3},
        rl10=v["r"]["rl10_bands_ghz"],
        rg={fk: r3(ff["realized_gain_broadside_dbi"], 2) for fk, ff in v["r"]["far_field"].items()},
    )
    cell[tag] = row
    cell[f"_g_{tag}"] = (f, g)
S["cell"] = {k: v for k, v in cell.items() if not k.startswith("_")}
tags = [t for t in ("r40", "r27", "r20", "r15") if t in S["cell"]]
for a, b in zip(tags, tags[1:]):
    fa, ga = cell[f"_g_{a}"]
    fb, gb = cell[f"_g_{b}"]
    m = band_mask(fa)
    S["cell"][f"dgamma_{a}_{b}"] = dict(
        rms=r3(np.sqrt(np.mean(np.abs(ga[m] - gb[m]) ** 2))),
        dphase_62_deg=r3(
            np.degrees(np.angle(at_c(fa, ga, 62.05) * np.conj(at_c(fb, gb, 62.05)))), 1
        ),
    )

# ============================================================== TX feeds (all three driven)
feeds = {}


def feed(name):
    p = os.path.join(RUNS, name, "s.csv")
    return load_csv(p) if os.path.exists(p) else None


for tag, h, nz, lossless in (
    ("txa-all-r40", 40, 4, False),
    ("txa-all-r27", 27, 4, False),
    ("txa-all-r20", 20, 4, False),
    ("txa-all-r15", 15, 4, False),
    ("txa-all-r12", 12, 4, False),
    ("txa-all-r20-nz6", 20, 6, False),
    ("txa-all-r20-nz8", 20, 8, False),
    ("txa-ll-r40", 40, 4, True),
    ("txa-ll-r27", 27, 4, True),
    ("txa-ll-r20", 20, 4, True),
):
    d = feed(tag)
    if d is None:
        continue
    f = d["f_ghz"]
    row = dict(fill_um=h, nz_core=nz, lossless=lossless)
    for n in TXP:
        s21 = 10 ** (d[f"s_{n}.P1_db"] / 20) * np.exp(1j * np.radians(d[f"s_{n}.P1_deg"]))
        s11 = 10 ** (d[f"s_{n}.P0_db"] / 20)
        lost = 1 - np.abs(s21) ** 2 - s11**2
        row[n] = dict(
            s21_db={str(x): r3(np.interp(x, f, d[f"s_{n}.P1_db"]), 3) for x in F3},
            lost_pct={str(x): r3(100 * np.interp(x, f, lost), 2) for x in F3},
            phase_62_deg=r3(np.interp(62.05, f, d[f"s_{n}.P1_deg"]), 2),
        )
        row[f"_s21_{n}"] = s21
    for a, b in (("TX1", "TX3"), ("TX2", "TX3"), ("TX1", "TX2")):
        dph = np.degrees(
            np.angle(at_c(f, row[f"_s21_{a}"], 62.05) * np.conj(at_c(f, row[f"_s21_{b}"], 62.05)))
        )
        row[f"skew_{a}-{b}_62_deg"] = r3(dph, 1)
        row[f"skew_{a}-{b}_62_ps"] = r3(dph / 360 / 62.05 * 1e3, 3)
    feeds[tag] = {k: v for k, v in row.items() if not k.startswith("_")}
S["feeds"] = feeds

json.dump(
    {k: v for k, v in S.items() if not k.startswith("_")},
    open(os.path.join(OUT, "summary.json"), "w"),
    indent=1,
)
print(
    json.dumps(
        {k: v for k, v in S.items() if not k.startswith("_") and k not in ("feeds",)}, indent=1
    )[:6000]
)
