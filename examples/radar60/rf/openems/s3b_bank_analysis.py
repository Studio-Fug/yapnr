"""Stage-3b EM bank and ground analysis: the D15 pours A/B/C, the dummies S1 against S2 (pour A),
the L2-L3 cavity options K0/K2/K1 (triplets) and the plane-pair ring-downs, with the plan's
decision rules applied mechanically.

  python3 openems/s3b_bank_analysis.py RUNS_DIR MODELS_DIR OUT_DIR [VARIANTS_JSON]

RUNS_DIR/<id>/{result.json, s.csv, probes.json} as collected by tools/exp/openems_plan.py:
bank-<V>-e<port> (ant_sim.py --probe-bond, V in A/B/C/S1), tri-<K>-r<mesh> (K0/K2/K1, 27/20 um)
and pp-<V>-r<mesh> (pp_sim.py, 27/20/15 um). MODELS_DIR holds bank-<V>.json and tri-<K>.json.
VARIANTS_JSON (results/stage3b/variants.json) gives the drill counts and G3 status.

Writes OUT_DIR/summary.json:
- per bank variant: TX-RX isolation (worst pair over 60.3-63.8 GHz; reciprocity check), active
  reflection of both banks for fixed codes set at 62.05 GHz (TX phases quantized to the 5.625
  degree phase-shifter step; scan 0, +-15, +-30, +-45 degrees), embedded co-polar patterns (edge
  against interior within +-45 degrees, both cuts), phased-array pointing and scan loss
  (H-plane, embedded complex patterns), the plane-pair probes (ring-down poles with Q >= 20 in
  57-70 GHz, port-to-probe transfers);
- the triplets: centre-column match, gain, neighbour coupling and bondply transfer per K option;
- the plane-pair windows: trapped modes per mesh and their trend;
- decisions: D15, S1/S2, K, by the rules of stage3b-rf/plan.md section 3.
Evidence label [S] (openEMS); nothing is measured.
"""

import json
import math
import os
import sys

import numpy as np

C0 = 299792458.0
ETA0 = 376.730313668
F3 = (60.3, 62.05, 63.8)
FC = 62.05
RXP = ["RX1", "RX2", "RX3", "RX4"]
TXP = ["TX1", "TX2", "TX3"]
PORTS = RXP + TXP
THETAS = (0, 15, -15, 30, -30, 45, -45)
TX_STEP_DEG = 5.625  # 6-bit TX phase shifter


def r(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def load_csv(path):
    hdr = open(path).readline().lstrip("# ").strip().split(",")
    a = np.loadtxt(path, delimiter=",")
    return {h: a[:, i] for i, h in enumerate(hdr)}


def run(runs, name):
    d = os.path.join(runs, name)
    if not os.path.exists(os.path.join(d, "result.json")):
        return None
    res = json.load(open(os.path.join(d, "result.json")))
    out = dict(r=res, dir=d)
    if os.path.exists(os.path.join(d, "s.csv")):
        out["s"] = load_csv(os.path.join(d, "s.csv"))
    for fk, v in res.get("far_field", {}).items():
        v["_f_ghz"] = float(fk)
    return out


def cs(d, port):
    return d[f"s_{port}_re"] + 1j * d[f"s_{port}_im"]


def band(f):
    return (f >= 60.3 - 1e-9) & (f <= 63.8 + 1e-9)


def at_c(f, z, x):
    return np.interp(x, f, z.real) + 1j * np.interp(x, f, z.imag)


def copol(ff, plane):
    """Co-polar complex far field of a cut, normalised so that |e|^2 / (2 eta0) integrates to
    P_rad / P_inc (r = 1 m), and its realized gain (dBi); phase referenced to the run's NF2FF
    centre (the element's phase centre)."""
    c = ff["complex_cuts"]
    th = np.array(ff["cut_theta_deg"], float)
    et = np.array([complex(*v) for v in c[f"{plane}_E_theta"]])
    ep = np.array([complex(*v) for v in c[f"{plane}_E_phi"]])
    e = et if np.sum(np.abs(et) ** 2) >= np.sum(np.abs(ep) ** 2) else ep
    g = 4 * math.pi * np.abs(e) ** 2 / (2 * ETA0)
    return th, e, 10 * np.log10(np.maximum(g, 1e-12))


# ============================================================== probe ring-downs
T_PULSE = 2 * 9 / (2 * math.pi * 9e9)  # ant_sim's Gaussian (f0 62 GHz, fc 9 GHz)
_STORE = {}


def probe_q20(d, name, t0):
    """Ring-down poles with Q >= 20 in 57-70 GHz (amplitude >= 1e-3 of the probe's peak) from
    OUT/probes.json, recomputed with bondprobe.ringdown's window guard (a driven run stopped at
    -40 dB leaves too short a ring-down for a triplet: no poles then). Returns (poles, window_ns)."""
    import bondprobe

    if d not in _STORE:
        fn = os.path.join(d, "probes.json")
        _STORE[d] = json.load(open(fn)) if os.path.exists(fn) else {}
    st = _STORE[d].get(name)
    if not st:
        return [], None
    v = np.array(st["v"], float)
    t = np.arange(len(v)) * st["dt_s"]
    poles, _, _, window = bondprobe.ringdown(t, v, t0)
    q = [
        dict(f_ghz=r(f / 1e9, 3), q=r(qq, 1), amp_rel=r(a, 4))
        for f, qq, a in poles
        if 57e9 <= f <= 70e9 and qq >= 20 and a >= 1e-3
    ]
    return sorted(q, key=lambda x: -x["amp_rel"]), r(window * 1e9, 2)


def ringdown_run(runs, name):
    """A `--ringdown-ns` run: per probe the poles with Q >= 20 in 57-70 GHz."""
    v = run(runs, name)
    if v is None:
        return None
    out = dict(cells=v["r"]["meta"]["cells"], wall_min=r(v["r"]["wall_s"] / 60, 1), probes={})
    for k, p in v["r"]["probes"].items():
        q, w = probe_q20(v["dir"], k, T_PULSE + 0.05e-9)
        out["probes"][k] = dict(where=p["where"], pair=p["pair"], window_ns=w, q20=q[:5])
    bank_q = [x for p in out["probes"].values() if p["where"] == "bank" for x in p["q20"]]
    out["bank_bond_q20_max"] = max((x["q"] for x in bank_q if x["amp_rel"] >= 0.05), default=None)
    out["bank_bond_modes"] = sorted(
        {(x["f_ghz"], x["q"]) for x in bank_q if x["amp_rel"] >= 0.05}, key=lambda m: m[0]
    )
    return out


# ============================================================== banks
def bank(runs, models, v):
    drives = {p: run(runs, f"bank-{v}-e{p}") for p in PORTS}
    have = {p: x for p, x in drives.items() if x is not None and "s" in x}
    if not have:
        return None
    f = next(iter(have.values()))["s"]["f_ghz"]
    n = len(PORTS)
    M = np.full((len(f), n, n), np.nan + 0j)
    for j, pj in enumerate(PORTS):
        if pj in have:
            for i, pi in enumerate(PORTS):
                M[:, i, j] = cs(have[pj]["s"], f"{pi}.Pg")
    recip = {}
    for i in range(n):  # reciprocity: the check where both are driven, the fill where one is
        for j in range(n):
            if i == j:
                continue
            a, b = M[:, i, j], M[:, j, i]
            if not np.any(np.isnan(a)) and not np.any(np.isnan(b)) and i < j:
                m = band(f)
                da = 20 * np.log10(np.abs(a[m]) + 1e-30) - 20 * np.log10(np.abs(b[m]) + 1e-30)
                recip[f"{PORTS[i]}-{PORTS[j]}"] = float(np.max(np.abs(da)))
            elif np.any(np.isnan(a)) and not np.any(np.isnan(b)):
                M[:, i, j] = b
    model = json.load(open(os.path.join(models, f"bank-{v}.json")))
    return dict(f=f, M=M, runs=have, model=model, recip=recip)


def isolation(B):
    f, M = B["f"], B["M"]
    m = band(f)
    rows, worst = {}, (-999.0, None)
    for t in TXP:
        for rx in RXP:
            s = M[:, PORTS.index(rx), PORTS.index(t)]
            if np.all(np.isnan(s)):
                continue
            db = 20 * np.log10(np.nanmax(np.abs(s[m])))
            rows[f"{t}->{rx}"] = r(db, 1)
            if db > worst[0]:
                worst = (db, f"{t}->{rx}")
    rv = [v for k, v in B["recip"].items() if k.split("-")[0] in RXP and k.split("-")[1] in TXP]
    return dict(
        worst_db=r(worst[0], 1),
        pair=worst[1],
        isolation_db=r(-worst[0], 1),
        all=rows,
        reciprocity_max_db=r(max(rv), 2) if rv else None,
    )


def coupling(B, pairs):
    f, M = B["f"], B["M"]
    m = band(f)
    out = {}
    for a, b in pairs:
        s = M[:, PORTS.index(b), PORTS.index(a)]
        if not np.all(np.isnan(s)):
            out[f"{a}->{b}"] = r(20 * np.log10(np.nanmax(np.abs(s[m]))), 1)
    return out


def codes(B, ports, th, quant):
    """Fixed element phases for scan angle th, set at 62.05 GHz (quantized for TX)."""
    pc = B["model"]["phase_centres"]
    k = 2 * math.pi * FC * 1e9 / C0 * 1e-3
    ph = {p: -math.degrees(k * pc[p][0] * math.sin(math.radians(th))) for p in ports}
    p0 = ph[ports[0]]
    ph = {p: v - p0 for p, v in ph.items()}
    if quant:
        ph = {p: round(v / quant) * quant for p, v in ph.items()}
    return {p: np.exp(1j * math.radians(v)) for p, v in ph.items()}


def active(B, ports, quant):
    """Worst active reflection (dB) per element and scan code over 60.3-63.8 GHz."""
    f, M = B["f"], B["M"]
    m = band(f)
    out, worst = {}, (-999.0, None)
    for th in THETAS:
        a = codes(B, ports, th, quant)
        row = {}
        for pi in ports:
            i = PORTS.index(pi)
            g = sum(M[:, i, PORTS.index(p)] * a[p] for p in ports) / a[pi]
            if np.any(np.isnan(g[m])):
                row[pi] = None
                continue
            db = 20 * np.log10(np.max(np.abs(g[m])))
            row[pi] = r(db, 2)
            if db > worst[0]:
                worst = (db, f"{pi}@{th}")
        out[str(th)] = row
    return dict(per_code=out, worst_db=r(worst[0], 2), worst_at=worst[1])


def self_match(B, p):
    f, M = B["f"], B["M"]
    i = PORTS.index(p)
    g = M[:, i, i]
    if np.any(np.isnan(g)):
        return None
    m = band(f)
    return dict(
        worst_db=r(20 * np.log10(np.max(np.abs(g[m]))), 2),
        at=[r(20 * np.log10(abs(at_c(f, g, x))), 2) for x in F3],
    )


def pattern_spread(B, a, b, span=45.0):
    ra, rb = B["runs"].get(a), B["runs"].get(b)
    if ra is None or rb is None:
        return None
    out = {}
    for plane in ("h", "e"):
        worst = 0.0
        for fk in ra["r"]["far_field"]:
            th, _, ga = copol(ra["r"]["far_field"][fk], plane)
            _, _, gb = copol(rb["r"]["far_field"][fk], plane)
            mm = np.abs(th) <= span
            worst = max(worst, float(np.max(np.abs(ga[mm] - gb[mm]))))
        out[plane] = r(worst, 2)
    return out


def broadside(B, p):
    v = B["runs"].get(p)
    if v is None:
        return None
    return {
        fk: dict(rg=r(ff["realized_gain_broadside_dbi"]), eff=r(ff["rad_efficiency"], 3))
        for fk, ff in v["r"]["far_field"].items()
    }


def array_scan(B, ports, quant):
    """Phased-array H-plane pattern from the embedded complex patterns (each re-referenced from
    its phase centre to x = 0): pointing error, scan loss and the highest other lobe within
    +-60 degrees, per scan code and band frequency."""
    if any(B["runs"].get(p) is None for p in ports):
        return None
    pc = B["model"]["phase_centres"]
    out = {}
    for fk in B["runs"][ports[0]]["r"]["far_field"]:
        fghz = float(fk)
        k = 2 * math.pi * fghz * 1e9 / C0 * 1e-3
        es = {}
        for p in ports:
            th, e, _ = copol(B["runs"][p]["r"]["far_field"][fk], "h")
            es[p] = e * np.exp(1j * k * pc[p][0] * np.sin(np.radians(th)))
        peak0 = None
        row = {}
        for t0 in (0, 15, -15, 30, -30, 45, -45):
            a = codes(B, ports, t0, quant)
            e = sum(es[p] * a[p] for p in ports)
            g = 10 * np.log10(
                np.maximum(4 * math.pi * np.abs(e) ** 2 / (2 * ETA0) / len(ports), 1e-12)
            )
            sec = np.abs(th) <= 60
            ip = int(np.argmax(np.where(sec, g, -999)))
            # the main lobe: from the peak down to the first nulls/minima on both sides
            lo = ip
            while lo > 0 and g[lo - 1] < g[lo]:
                lo -= 1
            hi = ip
            while hi < len(g) - 1 and g[hi + 1] < g[hi]:
                hi += 1
            other = [g[i] for i in range(len(g)) if sec[i] and not (lo <= i <= hi)]
            if t0 == 0:
                peak0 = g[ip]
            row[str(t0)] = dict(
                peak_deg=r(th[ip], 1),
                pointing_err_deg=r(th[ip] - t0, 1),
                peak_dbi=r(g[ip]),
                scan_loss_db=r(peak0 - g[ip]) if peak0 is not None else None,
                other_lobe_rel_db=r(max(other) - g[ip]) if other else None,
            )
        out[fk] = row
    return out


def probes_summary(B):
    """Per probe: the worst port-to-probe transfer over the drives and the ring-down poles with
    Q >= 20 in 57-70 GHz (amplitude >= 1e-3 of the probe's peak)."""
    out = {}
    for p, v in B["runs"].items():
        for name, pr in (v["r"].get("probes") or {}).items():
            o = out.setdefault(
                name,
                dict(
                    where=pr["where"], pair=pr["pair"], at=pr["at"], transfer_max_db=-999.0, q20=[]
                ),
            )
            tm = pr["transfer_max_57_70"]
            if tm["db"] > o["transfer_max_db"]:
                o["transfer_max_db"] = tm["db"]
                o["transfer_max_f_ghz"] = tm["f_ghz"]
                o["transfer_drive"] = p
                o["transfer_band_db"] = pr["transfer_db_at"]
            qs, w = probe_q20(v["dir"], name, T_PULSE + 0.1e-9)
            o.setdefault("window_ns", w)
            for q in qs:
                o["q20"].append(dict(q, drive=p))
    for o in out.values():
        o["q20"].sort(key=lambda q: -q["amp_rel"])
        # the strong poles only: amplitude >= 0.05 of the probe's peak at the window start
        o["q20_max"] = max((q["q"] for q in o["q20"] if q["amp_rel"] >= 0.05), default=None)
        o["q20"] = o["q20"][:6]
    return out


def bank_report(B):
    rep = dict(
        cells=next(iter(B["runs"].values()))["r"]["meta"]["cells"],
        drives=sorted(B["runs"]),
        isolation=isolation(B),
        coupling=coupling(
            B,
            [
                ("TX1", "TX2"),
                ("TX2", "TX3"),
                ("TX1", "TX3"),
                ("RX1", "RX2"),
                ("RX2", "RX3"),
                ("RX3", "RX4"),
                ("RX1", "RX4"),
            ],
        ),
        self=({p: self_match(B, p) for p in PORTS if p in B["runs"]}),
        active_tx=active(B, TXP, TX_STEP_DEG),
        active_rx=active(B, RXP, None),
        patterns=dict(
            TX1_vs_TX2=pattern_spread(B, "TX1", "TX2"),
            TX3_vs_TX2=pattern_spread(B, "TX3", "TX2"),
            RX1_vs_RX2=pattern_spread(B, "RX1", "RX2"),
            RX4_vs_RX3=pattern_spread(B, "RX4", "RX3"),
            RX2_vs_RX3=pattern_spread(B, "RX2", "RX3"),
        ),
        broadside={p: broadside(B, p) for p in PORTS if p in B["runs"]},
        scan_tx=array_scan(B, TXP, TX_STEP_DEG),
        scan_rx=array_scan(B, RXP, None),
        probes=probes_summary(B),
        rl10={p: B["runs"][p]["r"]["rl10_bands_ghz"] for p in B["runs"]},
        wall_min={p: r(B["runs"][p]["r"]["wall_s"] / 60, 1) for p in B["runs"]},
    )
    worst_rx_active = rep["active_rx"]["worst_db"]
    worst_tx_active = rep["active_tx"]["worst_db"]
    rep["worst_active_db"] = r(max(x for x in (worst_rx_active, worst_tx_active) if x is not None))
    spreads = [v["h"] for k, v in rep["patterns"].items() if v and k != "RX2_vs_RX3"]
    rep["edge_pattern_spread_h_db"] = r(max(spreads)) if spreads else None
    return rep


# ============================================================== triplets (K options)
def triplet(runs, name):
    v = run(runs, name)
    if v is None:
        return None
    f, d = v["s"]["f_ghz"], v["s"]
    g = cs(d, "T1.Pg")
    m = band(f)
    res = v["r"]
    out = dict(
        cells=res["meta"]["cells"],
        rl10=res["rl10_bands_ghz"],
        s11_at=[r(20 * np.log10(abs(at_c(f, g, x)))) for x in F3],
        s11_worst_in_band=r(20 * np.log10(np.max(np.abs(g[m])))),
        f_s11_min=r(res["f_s11_min_ghz"], 3),
        coupling_max={
            p: r(20 * np.log10(np.max(np.abs(cs(d, f"{p}.Pg")[m])))) for p in ("T0", "T2")
        },
        rg={fk: r(ff["realized_gain_broadside_dbi"]) for fk, ff in res["far_field"].items()},
        eff={fk: r(ff["rad_efficiency"], 3) for fk, ff in res["far_field"].items()},
        probes={
            k: dict(
                where=p["where"],
                pair=p["pair"],
                transfer_band_db=p["transfer_db_at"],
                transfer_max=p["transfer_max_57_70"],
                q20=probe_q20(v["dir"], k, T_PULSE + 0.1e-9)[0][:4],
                window_ns=probe_q20(v["dir"], k, T_PULSE + 0.1e-9)[1],
            )
            for k, p in (res.get("probes") or {}).items()
        },
        wall_min=r(res["wall_s"] / 60, 1),
    )
    bond = [p["transfer_band_db"] for p in out["probes"].values() if p["where"] == "bank"]
    out["bond_transfer_band_max_db"] = r(max(max(b.values()) for b in bond)) if bond else None
    return out


# ============================================================== plane-pair windows
def pp(runs, name):
    v = run(runs, name)
    if v is None:
        return None
    res = v["r"]
    probes = {}
    for k, p in res["probes"].items():
        probes[k] = dict(
            pair=p["pair"],
            late_db={a: r(b, 1) for a, b in p["late_db"].items()},
            trapped=[t for t in p["trapped_q20"] if t["amp_rel"] >= 1e-3][:5],
        )
    worst = None
    for k, p in probes.items():
        for t in p["trapped"]:
            if worst is None or t["q"] > worst["q"]:
                worst = dict(t, probe=k)
    return dict(
        cells=res["meta"]["cells"],
        res_um=res["meta"]["res_um"],
        wall_min=r(res["wall_s"] / 60, 1),
        probes=probes,
        worst=worst,
    )


def confirmed_modes(rows):
    """Modes (Q >= 20, 57-70 GHz, amplitude >= 1e-3) seen on at least two of the meshes within
    1.5 % in frequency, with the finest mesh's values."""
    by_mesh = []
    for tag in ("r27", "r20", "r15"):
        row = rows.get(tag)
        if row is None:
            continue
        ms = []
        for k, p in row["probes"].items():
            for t in p["trapped"]:
                ms.append(dict(t, pair=p["pair"], probe=k, mesh=tag))
        by_mesh.append(ms)
    out = []
    for ms in by_mesh[::-1]:  # finest first
        for t in ms:
            hits = sum(
                any(
                    abs(u["f_ghz"] - t["f_ghz"]) / t["f_ghz"] <= 0.015 and u["pair"] == t["pair"]
                    for u in other
                )
                for other in by_mesh
            )
            if hits >= 2 and not any(
                abs(o["f_ghz"] - t["f_ghz"]) / t["f_ghz"] <= 0.015 and o["pair"] == t["pair"]
                for o in out
            ):
                out.append(dict(t, meshes=hits))
    return sorted(out, key=lambda t: -t["q"])


# ============================================================== decisions
def decide(S, variants):
    D = {}
    banks = S["banks"]
    # ---- D15
    rows = {}
    for v in ("A", "B", "C"):
        b = banks.get(v)
        if b is None:
            continue
        modes = S["pp"].get(v, {}).get("confirmed", [])
        pour_q = [
            (n, p["q20_max"])
            for n, p in b["probes"].items()
            if p["where"] == "pour" and p["q20_max"] is not None
        ]
        vk = (variants or {}).get(f"s3b-{v}", {})
        rows[v] = dict(
            isolation_db=b["isolation"]["isolation_db"],
            worst_active_db=b["worst_active_db"],
            edge_pattern_spread_h_db=b["edge_pattern_spread_h_db"],
            drills=vk.get("vias_0p15_drill"),
            g3_ok=not vk.get("checks_failed"),
            pp_modes_q20=modes[:3],
            bank_pour_probe_q20_info=pour_q,
            gate_isolation=b["isolation"]["isolation_db"] >= 35,
            gate_modes=not modes,
        )
        rows[v]["passes_gates"] = bool(
            rows[v]["gate_isolation"] and rows[v]["gate_modes"] and rows[v]["g3_ok"]
        )
    cand = [v for v in rows if rows[v]["passes_gates"]]
    pick, why = None, ""
    if len(rows) < 3:
        why = "incomplete: only %s of A/B/C have data" % sorted(rows)
    elif cand:
        best = max(cand, key=lambda v: rows[v]["isolation_db"])
        tied = [v for v in cand if rows[best]["isolation_db"] - rows[v]["isolation_db"] <= 1.0]
        best_rl = min(rows[v]["worst_active_db"] for v in tied)
        tied = [v for v in tied if rows[v]["worst_active_db"] - best_rl <= 0.5]
        best_pat = min(rows[v]["edge_pattern_spread_h_db"] for v in tied)
        tied = [v for v in tied if rows[v]["edge_pattern_spread_h_db"] - best_pat <= 0.5]
        pick = min(tied, key=lambda v: rows[v]["drills"] or 1e9)
        why = f"gates passed by {cand}; tied within 1 dB / 0.5 dB / 0.5 dB: {tied}; fewest drills"
    else:
        why = "no variant passes every gate"
    D["D15"] = dict(rows=rows, pick=pick, why=why)
    # ---- S1 / S2 (pour A both)
    a, s1 = banks.get("A"), banks.get("S1")
    if a and s1:
        cmp = {}
        for p in ("TX1", "RX4", "TX2", "TX3", "RX3"):
            wa = max(
                v[p]
                for v in a["active_tx" if p.startswith("TX") else "active_rx"]["per_code"].values()
                if v.get(p) is not None
            )
            ws = max(
                v[p]
                for v in s1["active_tx" if p.startswith("TX") else "active_rx"]["per_code"].values()
                if v.get(p) is not None
            )
            cmp[p] = dict(active_worst_s2=wa, active_worst_s1=ws, s2_better_db=r(ws - wa))
        pa_ = {
            "TX1": (
                (a["patterns"]["TX1_vs_TX2"] or {}).get("h"),
                (s1["patterns"]["TX1_vs_TX2"] or {}).get("h"),
            ),
            "RX4": (
                (a["patterns"]["RX4_vs_RX3"] or {}).get("h"),
                (s1["patterns"]["RX4_vs_RX3"] or {}).get("h"),
            ),
        }
        for p, (x2, x1) in pa_.items():
            cmp[p]["pattern_spread_s2"] = x2
            cmp[p]["pattern_spread_s1"] = x1
            cmp[p]["pattern_s2_better_db"] = (
                r(x1 - x2) if x1 is not None and x2 is not None else None
            )
        noise = max(abs(cmp[p]["s2_better_db"]) for p in ("TX3", "RX3"))
        benefit = {
            p: (cmp[p]["s2_better_db"] >= 1.0) or ((cmp[p].get("pattern_s2_better_db") or 0) >= 1.0)
            for p in ("TX1", "RX4")
        }
        iso_s1 = s1["isolation"]["isolation_db"]
        if iso_s1 < 35:
            pick = "S2"
        elif benefit["TX1"] and not benefit["RX4"]:
            pick = "S1.5"
        elif benefit["TX1"] or benefit["RX4"]:
            pick = "S2"
        else:
            pick = "S1"
        D["S"] = dict(
            compare=cmp,
            isolation_s1_db=iso_s1,
            isolation_s2_db=a["isolation"]["isolation_db"],
            mesh_noise_floor_db=r(noise),
            s2_benefit_ge_1db=benefit,
            pick=pick,
        )
    # ---- K
    tri = S["triplets"]
    k0 = tri.get("tri-K0-r27")
    if k0:
        bank_q = {
            v: [
                (n, p["q20_max"])
                for n, p in banks[v]["probes"].items()
                if p["where"] == "bank" and p["q20_max"]
            ]
            for v in banks
        }
        tri_q = {
            n: [
                (k, p["q20"][0]["q"])
                for k, p in t["probes"].items()
                if p["where"] == "bank" and p["q20"]
            ]
            for n, t in tri.items()
            if t
        }
        rd = S.get("ringdown", {})
        rd_q = {n: x["bank_bond_q20_max"] for n, x in rd.items() if "K2" not in n}
        k0_modes = (
            any(bank_q.get(v) for v in ("A",))
            or bool(tri_q.get("tri-K0-r27"))
            or bool(tri_q.get("tri-K0-r20"))
            or any(q is not None for q in rd_q.values())
        )
        d15_ok = D["D15"]["pick"] is not None
        res = dict(
            bank_bond_q20=bank_q,
            triplet_bond_q20=tri_q,
            ringdown_bank_bond_q20_max=rd_q,
            d15_gates_ok=d15_ok,
            k0_modes_q20=k0_modes,
        )
        for kk in ("K2", "K1"):
            for mesh in ("r27", "r20"):
                t, b0 = tri.get(f"tri-{kk}-{mesh}"), tri.get(f"tri-K0-{mesh}")
                if not t or not b0:
                    continue
                res[f"{kk}_vs_K0_{mesh}"] = dict(
                    bond_transfer_drop_db=r(
                        b0["bond_transfer_band_max_db"] - t["bond_transfer_band_max_db"]
                    ),
                    coupling_drop_db={
                        p: r(b0["coupling_max"][p] - t["coupling_max"][p]) for p in ("T0", "T2")
                    },
                    s11_worst_change_db=r(t["s11_worst_in_band"] - b0["s11_worst_in_band"]),
                    rg_change_db={fk: r(t["rg"][fk] - b0["rg"][fk]) for fk in t["rg"]},
                )
        if d15_ok and not k0_modes:
            pick = "K0"
        else:
            k2 = res.get("K2_vs_K0_r20") or res.get("K2_vs_K0_r27")
            k1 = tri.get("tri-K1-r27")
            if (
                k2
                and k2["bond_transfer_drop_db"] >= 6
                and abs(k2["s11_worst_change_db"]) <= 0.5
                and max(abs(x) for x in k2["rg_change_db"].values()) <= 0.5
            ):
                pick = "K2"
            elif k1 and k1["s11_worst_in_band"] <= -10:
                pick = "K1"
            else:
                pick = "K0 with a quantified warning"
        res["pick"] = pick
        D["K"] = res
    return D


def main():
    runs, models, out = sys.argv[1:4]
    variants = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else None
    os.makedirs(out, exist_ok=True)
    S = dict(banks={}, triplets={}, pp={}, ringdown={})
    for v in ("A", "B", "C", "S1"):
        B = bank(runs, models, v)
        if B is not None:
            S["banks"][v] = bank_report(B)
    for k in ("K0", "K2", "K1"):
        for mesh in ("r27", "r20"):
            t = triplet(runs, f"tri-{k}-{mesh}")
            if t:
                S["triplets"][f"tri-{k}-{mesh}"] = t
    for n in ("tri-K0-rd-r27", "tri-K2-rd-r27", "tri-K0-rd-r20", "bank-A-rd"):
        x = ringdown_run(runs, n)
        if x:
            S["ringdown"][n] = x
    for v in ("A", "B", "C"):
        rows = {tag: pp(runs, f"pp-{v}-{tag}") for tag in ("r27", "r20", "r15")}
        rows = {k: x for k, x in rows.items() if x}
        if rows:
            S["pp"][v] = dict(meshes=rows, confirmed=confirmed_modes(rows))
    S["decisions"] = decide(S, variants)
    json.dump(S, open(os.path.join(out, "summary.json"), "w"), indent=1)
    print(json.dumps(S["decisions"], indent=1)[:8000])


if __name__ == "__main__":
    main()
