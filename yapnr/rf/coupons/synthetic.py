"""Synthetic measurement sessions for the recovery test (design §10).

A session is simulated the way a real one is measured: every stick between soldered SMA
connectors (series L, shunt C and a short coaxial section, varied stick to stick by the spread of
hand-soldered connectors), through the launch (pad, taper, the family's line to the reference
plane, the L1 -> L3 via on board B), behind a residual SOLT error (directivity −40 dB, source
match −35 dB), with trace noise, cable flex per connection and a phase drift over the session.
The pipeline then runs unchanged on the files.

The truth comes from the shipped tables by default (the CI test), or from tables built with a
different mesh (the slow study), so that the fit's own surrogate error is not shared.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import numpy as np

from yapnr.rf.coupons import catalog, families, models, session, stackups, touchstone

C_LIGHT = 299792458.0


@dataclass
class Imperfections:
    """Error and noise model of a hobby-lab session (design §10 step 3-4, est.)."""

    conn_l_nh: float = 0.15  # connector series inductance
    conn_c_pf: float = 0.08  # connector shunt capacitance
    conn_len_mm: float = 5.0  # coaxial section (PTFE)
    spread: float = 0.10  # relative L and C spread between soldered connectors
    spread_len_mm: float = 0.05
    directivity_db: float = -40.0  # residual SOLT error
    source_match_db: float = -35.0
    trace_noise_db: float = -60.0
    flex_db: float = 0.02  # cable flex per connection (1 σ)
    flex_deg_10g: float = 0.2
    drift_deg_10g: float = 0.5  # linear phase drift over the session
    pad_z: float = 47.0  # launch pad (inside the error box)
    via_l_nh: float = 0.25  # L1 -> L3 via (board B), est.
    via_c_pf: float = 0.12
    repeats: int = 3  # thru and the 40 mm line are re-mated (design §7.4)
    dc_temp_k: float = 0.5  # error of the recorded board temperature (one per session)
    dc_noise: float = 5e-4  # relative noise of a 4-wire reading


def ideal() -> Imperfections:
    """Identical connectors, no SOLT residual, no noise: multiline TRL is then exact."""
    return Imperfections(
        spread=0.0,
        spread_len_mm=0.0,
        directivity_db=-300.0,
        source_match_db=-300.0,
        trace_noise_db=-300.0,
        flex_db=0.0,
        flex_deg_10g=0.0,
        drift_deg_10g=0.0,
    )


def draw_truth(
    st: stackups.Stackup, rng, scale: float = 1.0, clip: float = 2.2
) -> Dict[str, float]:
    """A parameter vector from the priors (normal, clipped at `clip` σ, inside the physical and
    2D-table ranges)."""
    out = {}
    for n in st.params:
        p = stackups.param(n, st)
        z = float(np.clip(rng.standard_normal() * scale, -clip, clip))
        x = p.clip(p.nominal + z * p.sigma)
        if p.table is not None:
            a, b = p.table
            m = 0.02 * (b - a)
            x = min(max(x, a + m), b - m)
        out[n] = x
    return out


def _const_line(f, eps, z, loss_db_per_m=0.0):
    w = 2 * np.pi * f
    g = 1j * w * math.sqrt(eps) / C_LIGHT + loss_db_per_m / 8.686 * np.sqrt(f / 1e9)
    line = models.Line(g, np.full(len(f), z, complex))
    line._w = w
    return line


def reverse(m: np.ndarray) -> np.ndarray:
    """ABCD of a two-port seen from its other port."""
    A, B, C, D = m[:, 0, 0], m[:, 0, 1], m[:, 1, 0], m[:, 1, 1]
    det = A * D - B * C
    out = np.empty_like(m)
    out[:, 0, 0] = D / det
    out[:, 0, 1] = B / det
    out[:, 1, 0] = C / det
    out[:, 1, 1] = A / det
    return out


class Launcher:
    """The physical launch of a board: connector + pad + taper + line to the reference plane."""

    def __init__(self, board: catalog.Board, model: models.Model, imp: Imperfections, rng):
        self.b, self.m, self.imp, self.rng = board, model, imp, rng
        f = model.f
        self.w = 2 * np.pi * f
        L = board.launch
        p = model.line("P")
        self.pad = _const_line(f, 2.8, imp.pad_z, 1.0)
        self.taper = []
        n = 3
        for i in range(n):
            a = (i + 0.5) / n
            z = (1 - a) * imp.pad_z + a * float(np.real(p.zc[len(f) // 2]))
            self.taper.append(_const_line(f, 2.8 + a * 0.4, z, 1.0))
        self.taper_len = L.taper_len / n
        self.pad_len = L.pad_len
        self.x_line0 = L.pad_x0 + L.pad_len + L.taper_len

    def connector(self) -> np.ndarray:
        imp, r = self.imp, self.rng
        s = imp.spread
        lc = imp.conn_l_nh * 1e-9 * (1 + s * r.standard_normal())
        cc = imp.conn_c_pf * 1e-12 * (1 + s * r.standard_normal())
        ll = imp.conn_len_mm + imp.spread_len_mm * r.standard_normal()
        coax = _const_line(self.m.f, 2.05, 50.0, 0.5)
        return models.cascade(
            models.abcd_line(coax, ll),
            models.abcd_series(1j * self.w * lc),
            models.abcd_shunt(1j * self.w * cc),
        )

    def section(self, trl: str) -> np.ndarray:
        """Edge-to-RP launch for the TRL family of a stick (no connector)."""
        m = self.m
        parts = [models.abcd_line(self.pad, self.pad_len)]
        parts += [models.abcd_line(t, self.taper_len) for t in self.taper]
        fam = self.b.trl[trl]["family"] if trl else "P"
        via_x = self.b.launch.via_x
        if fam == "S" and via_x:
            parts.append(models.abcd_line(m.line("P"), via_x - self.x_line0))
            parts.append(models.abcd_series(1j * self.w * self.imp.via_l_nh * 1e-9))
            parts.append(models.abcd_shunt(1j * self.w * self.imp.via_c_pf * 1e-12))
            parts.append(models.abcd_line(m.line("S"), catalog.LAUNCH_MM - via_x))
        else:
            parts.append(models.abcd_line(m.line("P"), catalog.LAUNCH_MM - self.x_line0))
        return models.cascade(*parts)

    def port(self, trl: str) -> np.ndarray:
        return models.cascade(self.connector(), self.section(trl))


def _flex(f, imp: Imperfections, rng, drift_frac: float) -> np.ndarray:
    da = 10 ** (imp.flex_db * rng.standard_normal() / 20) - 1
    dphi = math.radians(imp.flex_deg_10g * rng.standard_normal() + imp.drift_deg_10g * drift_frac)
    return (1 + da) * np.exp(1j * dphi * f / 10e9)


def _solt_residual(f, imp: Imperfections, rng) -> Dict[str, np.ndarray]:
    def term(db):
        tau = rng.uniform(0.05e-9, 0.5e-9)
        return 10 ** (db / 20) * np.exp(1j * (2 * np.pi * f * tau + rng.uniform(0, 2 * np.pi)))

    return dict(
        e00=term(imp.directivity_db),
        e11=term(imp.source_match_db),
        e33=term(imp.directivity_db),
        e22=term(imp.source_match_db),
    )


def _apply_tier1(s, res, p1, p2):
    """Residual SOLT error boxes and cable factors around a 50 Ω two-port S."""
    n = s.shape[0]

    def box(e_vna, e_dut, p):
        b = np.zeros((n, 2, 2), complex)
        b[:, 0, 0], b[:, 1, 1] = e_vna, e_dut
        b[:, 0, 1] = b[:, 1, 0] = p * np.sqrt(1 - np.abs(e_vna) ** 2)
        return b

    b1 = box(res["e00"], res["e11"], p1)
    b2 = models.flip(box(res["e33"], res["e22"], p2))
    return models.star(models.star(b1, s), b2)


def _what(s: catalog.Stick) -> str:
    return {
        "thru": "THRU",
        "line": f"L{s.dl:g}",
        "reflect": "REFL",
        "verify": f"VER{s.dl:g}",
        "variant": "VAR",
        "ring": "RING",
        "stub": "STUB",
        "coupled": "CPL",
    }.get(s.kind, s.kind.upper())


def simulate(
    stackup_id: str,
    truth: Dict[str, float],
    out_dir: str,
    f: np.ndarray,
    rng,
    serial: str = "01",
    imp: Optional[Imperfections] = None,
    truth_tables=None,
    microsection: bool = True,
    dc_layers_truth: Optional[Dict[int, Dict[str, float]]] = None,
) -> dict:
    """Write a synthetic session for one board; returns the truth record."""
    imp = imp or Imperfections()
    st = stackups.get(stackup_id)
    b = catalog.board(stackup_id)
    tables = truth_tables if truth_tables is not None else families.load(stackup_id)
    v = stackups.with_values(st, truth)
    model = models.Model(stackup_id, v, f, tables)
    catalog.tune(
        b, models.Model(stackup_id, stackups.with_values(st, {}), f, families.load(stackup_id))
    )
    lau = Launcher(b, model, imp, rng)
    res = _solt_residual(f, imp, rng)
    os.makedirs(out_dir, exist_ok=True)
    sticks = [s for s in b.sticks if s.generated and s.ports == 2]
    order = 0
    n_conn = sum(1 + (imp.repeats - 1) * (s.kind == "thru" or s.dl == 40.0) for s in sticks)
    files = []
    for s in sticks:
        p1 = lau.port(s.trl)
        p2 = lau.port(s.trl)
        if s.kind == "reflect":
            zs = 1j * lau.w * models.via_inductance(v.get("pp1.h", 0.21), 0.3) / 3.0
            sp = np.zeros((len(f), 2, 2), complex)
            for k, pm in ((0, p1), (1, p2)):
                zin = (pm[:, 0, 0] * zs + pm[:, 0, 1]) / (pm[:, 1, 0] * zs + pm[:, 1, 1])
                sp[:, k, k] = (zin - 50.0) / (zin + 50.0)
            raw = sp
        else:
            dut = models.cascade(*[model.abcd(e) for e in s.elements]) if s.elements else None
            parts = [p1] + ([dut] if dut is not None else []) + [reverse(p2)]
            raw = models.abcd_to_s(models.cascade(*parts), 50.0)
        reps = imp.repeats if (s.kind == "thru" or (s.kind == "line" and s.dl == 40.0)) else 1
        for r in range(1, reps + 1):
            frac = order / max(n_conn - 1, 1)
            order += 1
            meas = _apply_tier1(raw, res, _flex(f, imp, rng, frac), _flex(f, imp, rng, frac))
            sig = 10 ** (imp.trace_noise_db / 20) / math.sqrt(2)
            meas = meas + sig * (
                rng.standard_normal(meas.shape) + 1j * rng.standard_normal(meas.shape)
            )
            name = session.file_name(b.letter, serial, s.id, s.family, _what(s), r)
            touchstone.write(
                os.path.join(out_dir, name), f, meas, ["synthetic session"], fmt="ri", digits=8
            )
            files.append(name)
    # DC meanders
    rows = []
    dc_truth = {}
    t_board = 23.0 + imp.dc_temp_k * float(rng.standard_normal())  # recorded as 23.0
    for s in b.sticks:
        if s.kind != "dc":
            continue
        for mnd in s.geometry["meanders"]:
            k = mnd["layer"]
            t, e = _layer_te(st, v, k, dc_layers_truth, rng, dc_truth)
            r_true = models.meander_resistance(
                mnd["w"], t, e, mnd["length"], mnd["corners"], t_board
            )
            i = 0.1
            volt = r_true * i * (1 + imp.dc_noise * rng.standard_normal())
            volt += 1e-6 * rng.standard_normal()
            rows.append(
                dict(
                    serial=serial,
                    stick=s.id,
                    layer=k,
                    width_mm=mnd["w"],
                    current_a=i,
                    voltage_v=f"{volt:.8g}",
                    temp_c=23.0,
                )
            )
    session.write_dc(out_dir, rows)
    if microsection:
        ms = {}
        for n, sig in (
            ("pp1.h", 0.005),
            ("L1.t", 0.003),
            ("pp3.h", 0.005),
            ("core.h", 0.005),
            ("L3.t", 0.003),
        ):
            if n in v and n in st.params:
                ms[n] = [round(v[n] + sig * rng.standard_normal(), 5), sig]
        if "mask.scale" in st.params:
            ms["mask.scale"] = [
                round(v["mask.scale"] * (1 + 0.2 * rng.standard_normal()), 4),
                0.2 * v["mask.scale"],
            ]
        with open(os.path.join(out_dir, "microsection.json"), "w", encoding="utf-8") as fh:
            json.dump(ms, fh, indent=2)
    session.write_manifest(
        out_dir,
        dict(
            coupon_revision="synthetic",
            stackup=stackup_id,
            lot="synthetic",
            boards=[f"{b.letter}-{serial}"],
            vna="synthetic (yapnr.rf.coupons.synthetic)",
            calibration="SOLT at the cable ends (tier 1)",
            grid=dict(start_hz=float(f[0]), stop_hz=float(f[-1]), points=len(f)),
            temperature_c=[23.0, 23.0],
            imperfections=asdict(imp),
        ),
    )
    rec = dict(stackup=stackup_id, truth=truth, dc_layers=dc_truth, files=len(files))
    with open(os.path.join(out_dir, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2)
    return rec


def _layer_te(st, v, k, given, rng, record):
    """Copper thickness and etch of layer k: fit parameters where they exist, else a draw."""
    key_t, key_e = f"L{k}.t", f"L{k}.etch"
    if key_t in v and key_e in v:
        return v[key_t], v[key_e]
    if given and k in given:
        t, e = given[k]["t"], given[k]["etch"]
    elif k in record:
        t, e = record[k]["t"], record[k]["etch"]
    else:
        nom = st.copper[k - 1].t_mm
        t = nom * (1 + 0.15 * float(np.clip(rng.standard_normal(), -2, 2)))
        e = 0.015 * float(np.clip(rng.standard_normal(), -2, 2))
    record[k] = dict(t=t, etch=e)
    return t, e


def grid(f_max: float = 6e9, step: float = 10e6) -> np.ndarray:
    """The measurement grid of design §7.3: linear from the step up."""
    return np.arange(1, int(round(f_max / step)) + 1) * step


# --- the recovery study (design §10) -------------------------------------------------------------


class PointTables:
    """Tables that return direct 2D solves at one parameter point (the truth of a draw), so that
    the fit's surrogate error is not shared with the simulated data."""

    def __init__(self, stackup_id: str, truth: Dict[str, float], mesh: str = "fine"):
        st = stackups.get(stackup_id)
        v = stackups.with_values(st, truth)
        self.out = {}
        for fid in families.BOARD_FAMILIES[stackup_id]:
            fam = families.get(fid, stackup_id)
            self.out[fid] = families.solve_point(
                fid, {p: v[p] for p in fam.params}, mesh, stackup_id=stackup_id
            )

    def __getitem__(self, fid):
        return lambda values, fid=fid: self.out[fid]

    def items(self):
        return [(k, self[k]) for k in self.out]


def clamp_on() -> Imperfections:
    """A reusable clamp-on pair moved between sticks: nearly identical error boxes (est.)."""
    return Imperfections(spread=0.02, spread_len_mm=0.01, flex_db=0.01, flex_deg_10g=0.1)


def study(
    stackup_id: str,
    out_dir: str,
    draws: int = 50,
    seed: int = 1000,
    f_max: float = 12e9,
    truth: str = "tables",
    clamp_on_pair: bool = False,
    log=print,
    imp: Optional[Imperfections] = None,
    keep_sessions: bool = False,
    boot: int = 0,
    **kw,
) -> dict:
    """Draw parameter vectors from the priors, simulate a session for each, run the extraction
    unchanged and compare. Pass criteria (design §10): the 2σ intervals contain the truth for at
    least 90 % of parameters and draws, |mean z| < 0.5 per parameter, the reported σ within a
    factor 1.5 of the empirical spread, and the held-out checks pass."""
    import shutil
    import time

    from yapnr.rf.coupons import fit

    clamp_on_pair = clamp_on_pair or kw.get("clamp_on", False)
    st = stackups.get(stackup_id)
    imp = imp or (clamp_on() if clamp_on_pair else Imperfections())
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for k in range(draws):
        rng = np.random.default_rng(seed + k)
        tr = draw_truth(st, rng)
        ses = os.path.join(out_dir, f"draw{k:03d}")
        t0 = time.time()
        tables = PointTables(stackup_id, tr) if truth == "direct" else None
        f = grid(f_max, 10e6 if f_max <= 6.5e9 else 20e6)
        simulate(stackup_id, tr, ses, f, rng, imp=imp, truth_tables=tables)
        t1 = time.time()
        res, d, p = fit.extract(stackup_id, ses, with_systematics=False, boot=boot)
        z = {n: (res.value[n] - tr[n]) / res.sigma[n] for n in res.names}
        err = {n: res.value[n] - tr[n] for n in res.names}
        # derived quantities at the truth, through the same (truth) tables
        m_true = models.Model(
            stackup_id, stackups.with_values(st, tr), np.array([fit.F_PRODUCT]), tables
        )
        dz = {}
        for key, (val, sig) in res.derived.items():
            fam, q = key.rsplit(".", 1)
            if not q.startswith(("z0_ohm", "eps_eff")):
                continue
            line = m_true.line(fam)
            true = float(abs(line.zc[0])) if q.startswith("z0") else float(line.eps_eff[0])
            dz[key] = dict(err=val - true, sigma=sig, z=(val - true) / sig if sig > 0 else 0.0)
        ho = {sid: h["passed"] for sid, h in res.held_out.items()}
        rows.append(
            dict(
                draw=k,
                truth=tr,
                value=res.value,
                sigma=res.sigma,
                z=z,
                err=err,
                derived=dz,
                held_out=ho,
                verify=d.verify,
                warnings=res.warnings,
                seconds=[t1 - t0, time.time() - t1],
            )
        )
        log(
            f"draw {k}: {time.time() - t0:.1f} s; max |z| {max(abs(x) for x in z.values()):.2f}"
            f" ({max(z, key=lambda n: abs(z[n]))}); held-out {sum(ho.values())}/{len(ho)}"
        )
        if not keep_sessions:
            shutil.rmtree(ses, ignore_errors=True)
    summary = summarize(rows, st)
    rec = dict(
        stackup=stackup_id,
        draws=draws,
        seed=seed,
        f_max=f_max,
        truth=truth,
        boot=boot,
        imperfections=asdict(imp),
        summary=summary,
        rows=rows,
    )
    with open(os.path.join(out_dir, "study.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    return rec


def summarize(rows: List[dict], st: Optional[stackups.Stackup] = None) -> dict:
    names = list(rows[0]["z"])
    Z = np.array([[r["z"][n] for n in names] for r in rows])
    E = np.array([[r["err"][n] for n in names] for r in rows])
    S = np.array([[r["sigma"][n] for n in names] for r in rows])
    per = {}
    for i, n in enumerate(names):
        spread = float(np.std(E[:, i], ddof=1)) if len(rows) > 1 else float("nan")
        per[n] = dict(
            unit=stackups.param(n, st).unit,
            rms_err=float(np.sqrt(np.mean(E[:, i] ** 2))),
            median_sigma=float(np.median(S[:, i])),
            prior_sigma=stackups.param(n, st).sigma,
            mean_z=float(np.mean(Z[:, i])),
            rms_z=float(np.sqrt(np.mean(Z[:, i] ** 2))),
            coverage_2s=float(np.mean(np.abs(Z[:, i]) <= 2)),
            spread_over_sigma=spread / float(np.mean(S[:, i])) if len(rows) > 1 else float("nan"),
        )
    derived = {}
    for key in rows[0]["derived"]:
        e = np.array([r["derived"][key]["err"] for r in rows])
        s = np.array([r["derived"][key]["sigma"] for r in rows])
        z = e / s
        derived[key] = dict(
            rms_err=float(np.sqrt(np.mean(e**2))),
            median_sigma=float(np.median(s)),
            coverage_2s=float(np.mean(np.abs(z) <= 2)),
            mean_z=float(np.mean(z)),
        )
    ho = [v for r in rows for v in r["held_out"].values()]
    coverage = float(np.mean(np.abs(Z) <= 2))
    bias_ok = all(abs(p["mean_z"]) < 0.5 for p in per.values())
    spread_ok = all(
        (p["spread_over_sigma"] != p["spread_over_sigma"])
        or 1 / 1.5 <= p["spread_over_sigma"] <= 1.5
        for p in per.values()
    )
    held_ok = (sum(ho) / len(ho) >= 0.9) if ho else True
    return dict(
        coverage_2s=coverage,
        bias_ok=bias_ok,
        spread_ok=spread_ok,
        held_out_pass_rate=(sum(ho) / len(ho)) if ho else None,
        passed=bool(coverage >= 0.9 and bias_ok and spread_ok and held_ok),
        parameters=per,
        derived=derived,
        seconds_per_draw=float(np.mean([sum(r["seconds"]) for r in rows])),
    )
