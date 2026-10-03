"""Extraction: calibration, the joint fit, uncertainty and held-out checks (design §8.2-§8.7).

`extract(stackup_id, session_dir)` runs the whole pipeline on a measured (or synthetic) session:

1. quality checks of every file (passivity, reciprocity);
2. multiline TRL per TRL set on the repeat-averaged standards; γ's uncertainty from re-running
   the calibration on each repeat set (the re-mated thru and 40 mm line);
3. every other stick of the set corrected to the reference planes;
4. the TDR impedance of the longest line (the fit runs the same processing on the model);
5. a Levenberg-Marquardt fit of all fitted sticks, DC resistances, microsection readings and the
   priors (normalized parameters z = (θ − μ)/σ_prior); block noise levels re-estimated from the
   residuals, and the covariance inflated by each block's residual autocorrelation (AR(1)
   effective sample size), since connector and cable errors are smooth in frequency;
6. systematic refits (roughness model, SOLT load ±0.5 Ω) added to the reported σ;
7. held-out structures (rings, stubs, coupled sections) predicted and compared.
"""

from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import calibrate, catalog, families, models, session, stackups

F_PRODUCT = 5.8e9
TDR_LAUNCH_EPS = 2.9


@dataclass
class Data:
    stackup: str
    board: catalog.Board
    f_grid: np.ndarray  # measurement grid (Hz)
    f: np.ndarray  # fit frequencies
    gamma: Dict[str, np.ndarray] = field(default_factory=dict)  # TRL set -> γ at f
    gamma_sigma: Dict[str, np.ndarray] = field(default_factory=dict)
    trl: Dict[str, calibrate.TRL] = field(default_factory=dict)
    corrected: Dict[str, np.ndarray] = field(default_factory=dict)  # stick -> S on f_grid
    tdr: Dict[str, float] = field(default_factory=dict)
    tdr_f: Optional[np.ndarray] = None
    dc: List[dict] = field(default_factory=list)
    microsection: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    quality: Dict[str, dict] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    noise: "NoiseModel" = None
    repeat: Dict[str, dict] = field(default_factory=dict)
    verify: Dict[str, dict] = field(default_factory=dict)


@dataclass
class NoiseModel:
    """Measurement uncertainty of a session (design §8.5: weights from the repeatability). The
    defaults describe soldered edge SMAs on a hobby VNA after SOLT; `session.yaml` may override
    them under `noise:`. Measured thru repeats raise the connection terms when larger."""

    mag_rep_db: float = 0.03  # per connection: re-mating and the spread between soldered SMAs
    phase_rep_deg_10g: float = 1.0  # per connection at 10 GHz, proportional to frequency
    solt_directivity_db: float = -40.0  # residual directivity after SOLT: sets the TDR σ
    tdr_model_ohm: float = 0.25  # TDR processing and launch effects
    dc_rel: float = 0.005  # 4-wire resistance, including temperature (±1 K)
    conn_c_pf: float = 0.01  # shunt-capacitance difference between soldered connectors (est.)

    def tdr_sigma_ohm(self) -> float:
        return math.hypot(100.0 * 10 ** (self.solt_directivity_db / 20), self.tdr_model_ohm)


def repeatability(ses: session.Session, stick: str, f: np.ndarray) -> dict:
    """Per-connection repeatability from the repeats of one stick (the thru): the standard
    deviation of ln S21 over the repeats, magnitude (Np) and phase (rad), the phase taken
    proportional to frequency (a median fit) so that one noisy point does not set it."""
    ms = ses.meas.get(stick, [])
    if len(ms) < 2:
        return dict(n=len(ms), mag_np=np.zeros(len(f)), phase_rad=np.zeros(len(f)))
    t = np.array([0.5 * (m.s[:, 1, 0] + m.s[:, 0, 1]) for m in ms])
    lt = np.log(t / np.mean(t, axis=0))
    fg = ms[0].f
    mag = np.std(lt.real, axis=0, ddof=1) / math.sqrt(2)  # per connection (two per stick)
    ph = np.std(lt.imag, axis=0, ddof=1) / math.sqrt(2)
    k = fg > 0
    slope = float(np.median(ph[k] / fg[k]))
    return dict(
        n=len(ms),
        mag_np=np.full(len(f), float(np.median(mag))),
        phase_rad=slope * f,
        phase_deg_10g=math.degrees(slope * 10e9),
        mag_db=float(np.median(mag)) * 20 / math.log(10),
    )


def _nominal_model(stackup_id, f, tables=None):
    st = stackups.get(stackup_id)
    return models.Model(stackup_id, stackups.with_values(st, {}), f, tables)


def prepare(
    stackup_id: str,
    ses: session.Session,
    f_min: float = 0.5e9,
    f_max: Optional[float] = None,
    n_fit: int = 110,
    tdr_f_max: float = 6.0e9,
) -> Data:
    b = catalog.board(stackup_id)
    any_m = next(iter(ses.meas.values()))[0]
    f_grid = any_m.f
    catalog.tune(b, _nominal_model(stackup_id, f_grid))
    f_max = min(f_max or f_grid[-1], f_grid[-1])
    sel = np.where((f_grid >= f_min) & (f_grid <= f_max))[0]
    idx = sel[np.unique(np.linspace(0, len(sel) - 1, min(n_fit, len(sel))).round().astype(int))]
    d = Data(stackup_id, b, f_grid, f_grid[idx])
    d.noise = NoiseModel(**(ses.manifest.get("noise") or {}))
    d.problems = session.check(ses, b)
    for sid, ms in ses.meas.items():
        d.quality[sid] = calibrate.quality(ms[0].s)
        if not d.quality[sid]["passive"]:
            d.problems.append(
                f"{sid}: not passive (max singular value {d.quality[sid]['max_singular']:.4f})"
            )
    nom = _nominal_model(stackup_id, f_grid)
    for set_id, t in b.trl.items():
        fam = t["family"]
        eps0 = float(np.median(nom.line(fam).eps_eff))
        sticks = {b.stick(s).dl: s for s in t["sticks"]}
        if any(s not in ses.meas for s in sticks.values()) or t["reflect"] not in ses.meas:
            d.problems.append(f"TRL set {set_id}: standards missing, skipped")
            continue
        thru_id = sticks[0.0]
        lines_ids = {dl: s for dl, s in sticks.items() if dl > 0}
        refl = ses.mean(t["reflect"])
        cal = calibrate.multiline_trl(
            f_grid,
            ses.mean(thru_id),
            [(dl, ses.mean(s)) for dl, s in lines_ids.items()],
            refl,
            eps0,
        )
        d.trl[set_id] = cal
        d.gamma[set_id] = cal.gamma[idx]
        # γ's uncertainty: the connection repeatability (connector spread between sticks and
        # re-mating, the noise model's default or the thru repeats, whichever is larger) on the
        # longest line of the set
        rep = repeatability(ses, thru_id, f_grid[idx])
        d.repeat[set_id] = rep
        nm = d.noise
        ph = np.maximum(math.radians(nm.phase_rep_deg_10g) * f_grid[idx] / 10e9, rep["phase_rad"])
        mg = np.maximum(nm.mag_rep_db * math.log(10) / 20, rep["mag_np"])
        l_max = max(lines_ids) * 1e-3
        beta = np.abs(d.gamma[set_id].imag)
        d.gamma_sigma[set_id] = (math.sqrt(2) * mg / l_max + 1e-3) + 1j * (
            math.sqrt(2) * np.maximum(ph, 0.002) / l_max + 1e-4 * beta
        )
        for s in b.sticks:
            if s.trl == set_id and s.id in ses.meas and s.kind not in ("thru", "line", "reflect"):
                d.corrected[s.id] = cal.correct(ses.mean(s.id))
        ver = t.get("verify")
        if ver and ver in ses.meas:
            d.verify[set_id] = verification(
                cal, ses.mean(ver), b.stick(ver).dl, f_grid, np.isin(f_grid, d.f)
            )
            if not d.verify[set_id]["passed"]:
                d.problems.append(
                    f"verification line {ver}: max deviation {d.verify[set_id]['max_dev']:.4f}"
                    f" above {VERIFY_LIMIT} (calibration or connector problem)"
                )
        tdr_stick = t.get("tdr")
        if tdr_stick and tdr_stick in ses.meas:
            k = f_grid <= tdr_f_max
            s11 = ses.first(tdr_stick).s[k, 0, 0]
            dl = b.stick(tdr_stick).dl
            d.tdr[set_id] = calibrate.tdr_z0(
                f_grid[k], s11, eps0, catalog.LAUNCH_MM, dl, pre_delay_s=_conn_delay()
            )
            d.tdr_f = f_grid[k]
    for row in ses.dc:
        r = row["voltage_v"] / row["current_a"]
        d.dc.append(dict(row, r_ohm=r))
    d.microsection = dict(ses.microsection)
    return d


VERIFY_LIMIT = 0.05  # max |S − S_line| of the corrected verification line, 1-6 GHz (est.)


def verification(
    cal: calibrate.TRL, s_meas: np.ndarray, dl_mm: float, f: np.ndarray, fit_sel: np.ndarray
) -> dict:
    """The corrected verification line against a matched line of the calibration's own γ: the
    residual calibration error, connector differences included (design §5.2, A08). Its RMS in
    the fit's features (`s_features`, units of S_SIGMA) is the floor of every corrected stick's
    noise level in the fit: a stick's connector error is otherwise partly absorbed by the
    parameters it alone constrains, and its residual understates it."""
    s = cal.correct(s_meas)
    e = np.exp(-cal.gamma * dl_mm * 1e-3)
    ideal = np.zeros_like(s)
    ideal[:, 0, 1] = ideal[:, 1, 0] = e
    dev = np.abs(s - ideal).max(axis=(1, 2))
    k = (f >= 1e9) & (f <= 6e9)
    mx = float(dev[k].max()) if k.any() else float(dev.max())
    feat = (s_features(s[fit_sel]) - s_features(ideal[fit_sel])) / S_SIGMA
    return dict(
        max_dev=mx,
        max_dev_full=float(dev.max()),
        rms_dev=float(np.sqrt(np.mean(dev[k] ** 2))) if k.any() else float("nan"),
        feature_rms={g: float(np.sqrt(np.mean(_group(feat, g) ** 2))) for g in FEATURE_GROUPS},
        limit=VERIFY_LIMIT,
        passed=mx <= VERIFY_LIMIT,
    )


def _conn_delay() -> float:
    """Round-trip delay of the SMA's coaxial section (est. 5 mm of PTFE)."""
    return 2 * 5e-3 * math.sqrt(2.05) / 299792458.0


# --- the forward model of the data -----------------------------------------------------------


class Predictor:
    def __init__(self, d: Data, tables=None, roughness: str = "huray"):
        self.d = d
        self.tables = tables if tables is not None else families.load(d.stackup)
        self.roughness = roughness
        self.st = stackups.get(d.stackup)

    def model(self, v: Dict[str, float], f: np.ndarray) -> models.Model:
        return models.Model(self.d.stackup, v, f, self.tables, roughness=self.roughness)

    def gamma(self, m: models.Model, set_id: str) -> np.ndarray:
        return m.line(self.d.board.trl[set_id]["family"]).gamma

    def stick_s(self, m: models.Model, sid: str, connectors: bool = True) -> np.ndarray:
        """The corrected stick's model; with `connectors`, its two connection nuisances (a
        reference-plane shift and a shunt capacitance per port, `nuisances`) around it."""
        s = self.d.board.stick(sid)
        fam = self.d.board.trl[s.trl]["family"]
        ref = m.line(fam).zc
        els = list(s.elements)
        if connectors and f"conn.{sid}.1.dl" in m.v:
            els = [("conn", fam, sid, 1)] + els + [("conn", fam, sid, 2)]
        return models.structure_s(m, els, ref)

    def nuisances(self) -> Dict[str, Tuple[float, float, float, float]]:
        """Per fitted stick and port: the difference of its soldered connector from the
        calibration's (design §6.1), as a reference-plane shift `dl` (mm of the TRL family's
        line; σ from the connection phase repeatability) and a shunt capacitance `c` (pF, est.
        σ 0.01 pF). Marginalizing them keeps a stick's connector error out of the fab
        parameters and their uncertainty. (mean, σ, lower, upper)."""
        out = {}
        nm = self.d.noise
        for sid in self.d.corrected:
            st = self.d.board.stick(sid)
            if not st.fitted or not st.elements:
                continue
            fam = self.d.board.trl[st.trl]["family"]
            eps = 4.48 if fam == "S" else 3.2
            beta10 = 2 * math.pi * 10e9 * math.sqrt(eps) / 299792458.0
            sd_l = math.radians(nm.phase_rep_deg_10g) / beta10 * 1e3
            for port in (1, 2):
                out[f"conn.{sid}.{port}.dl"] = (0.0, sd_l, -6 * sd_l, 6 * sd_l)
                out[f"conn.{sid}.{port}.c"] = (
                    0.0,
                    nm.conn_c_pf,
                    -6 * nm.conn_c_pf,
                    6 * nm.conn_c_pf,
                )
        return out

    def stick_features(self, m: models.Model, sid: str) -> np.ndarray:
        """`s_features` of a stick's model, cached on the model (one evaluation per group)."""
        cache = m.__dict__.setdefault("_features", {})
        if sid not in cache:
            cache[sid] = s_features(self.stick_s(m, sid))
        return cache[sid]

    def tdr(self, v: Dict[str, float], set_id: str) -> float:
        b = self.d.board
        fam = b.trl[set_id]["family"]
        dl = b.stick(b.trl[set_id]["tdr"]).dl
        f = self.d.tdr_f
        m = self.model(v, f)
        line = m.line(fam)
        s = models.abcd_to_s(models.abcd_line(line, dl + 2 * catalog.LAUNCH_MM), 50.0)
        eps0 = float(np.median(line.eps_eff))
        return calibrate.tdr_z0(f, s[:, 0, 0], eps0, catalog.LAUNCH_MM, dl)

    def dc(self, v: Dict[str, float], row: dict) -> Optional[float]:
        k = row["layer"]
        if f"L{k}.t" not in v:
            return None
        mnd = _meander(self.d.board, row)
        return models.meander_resistance(
            mnd["w"], v[f"L{k}.t"], v[f"L{k}.etch"], mnd["length"], mnd["corners"], row["temp_c"]
        )


def _meander(b: catalog.Board, row: dict) -> dict:
    s = b.stick(row["stick"])
    for m in s.geometry["meanders"]:
        if m["layer"] == row["layer"] and abs(m["w"] - row["width_mm"]) < 1e-6:
            return m
    raise KeyError(f"no meander {row}")


# --- residual blocks ---------------------------------------------------------------------------

S_SIGMA = 0.003  # nominal noise of corrected S11, S22 and of ln S21 (magnitude and phase)


def s_features(s: np.ndarray) -> np.ndarray:
    """The fitted features of a corrected two-port: S11 and S22 (real, imaginary), and ln S21
    (S21 and S12 averaged) as log magnitude and unwrapped phase, which keeps the objective
    unimodal in the line delays where complex S21 would wrap."""
    t = 0.5 * (s[:, 1, 0] + s[:, 0, 1])
    return np.concatenate(
        [
            s[:, 0, 0].real,
            s[:, 0, 0].imag,
            s[:, 1, 1].real,
            s[:, 1, 1].imag,
            np.log(np.abs(t) + 1e-12),
            np.unwrap(np.angle(t)),
        ]
    )


@dataclass
class Block:
    name: str
    sigma: float  # scale applied on top of the per-point σ (re-estimated)
    fn: Callable  # (v, model) -> whitened residual vector (before the block scale)
    smooth: bool  # residuals ordered in frequency (AR(1) inflation applies)


FEATURE_GROUPS = ("refl", "mag", "phase")


def _group(feat: np.ndarray, grp: str) -> np.ndarray:
    """A feature group of `s_features`: S11 and S22 (4 n values), ln|S21| or arg S21 (n)."""
    n = len(feat) // 6
    return {"refl": feat[: 4 * n], "mag": feat[4 * n : 5 * n], "phase": feat[5 * n :]}[grp]


def blocks(p: Predictor, use: Optional[Sequence[str]] = None) -> List[Block]:
    """Residual blocks, each with one error mechanism, so that its noise level and AR(1)
    effective number of points are estimated separately: α and β of each TRL set; for every
    corrected stick its reflections, ln|S21| and the unwrapped phase of S21 (whose error from
    the connectors is nearly one delay, so it counts as about one observation)."""
    d = p.d
    out: List[Block] = []
    for set_id in d.gamma:
        g, sg = d.gamma[set_id], d.gamma_sigma[set_id]
        cond = d.trl[set_id].conditioning[np.isin(d.f_grid, d.f)]
        ok = cond > 0.25

        def fa(v, m, set_id=set_id, g=g, sg=sg, ok=ok):
            return ((g - p.gamma(m, set_id)).real / sg.real)[ok]

        def fb(v, m, set_id=set_id, g=g, sg=sg, ok=ok):
            return ((g - p.gamma(m, set_id)).imag / sg.imag)[ok]

        out.append(Block(f"gamma:{set_id}:alpha", 1.0, fa, True))
        out.append(Block(f"gamma:{set_id}:beta", 1.0, fb, True))
    k = np.isin(d.f_grid, d.f)
    for sid, s in d.corrected.items():
        st = d.board.stick(sid)
        if not st.fitted or not st.elements:
            continue
        meas = s_features(s[k])
        for grp in FEATURE_GROUPS:

            def fn(v, m, sid=sid, meas=meas, grp=grp):
                return _group(meas - p.stick_features(m, sid), grp) / S_SIGMA

            out.append(Block(f"stick:{sid}:{grp}", 1.0, fn, True))
    for set_id, z in d.tdr.items():
        sz = d.noise.tdr_sigma_ohm()

        def fn(v, m, s=set_id, z=z, sz=sz):
            return np.array([(z - p.tdr(v, s)) / sz])

        out.append(Block(f"tdr:{set_id}", 1.0, fn, False))
    dc_rows = [r for r in d.dc if p.dc(stackups.with_values(p.st, {}), r) is not None]
    if dc_rows:

        def fn(v, m):
            return np.array(
                [(r["r_ohm"] - p.dc(v, r)) / (d.noise.dc_rel * r["r_ohm"]) for r in dc_rows]
            )

        out.append(Block("dc", 1.0, fn, False))
    ms = {k: v for k, v in d.microsection.items() if k in p.st.params}
    if ms:
        out.append(
            Block(
                "microsection",
                1.0,
                lambda v, m: np.array([(ms[k][0] - v[k]) / ms[k][1] for k in ms]),
                False,
            )
        )
    if use is not None:
        out = [b for b in out if any(b.name.startswith(u) for u in use)]
    return out


# --- the fit -----------------------------------------------------------------------------------


@dataclass
class FitResult:
    names: List[str]
    value: Dict[str, float]
    sigma: Dict[str, float]  # statistical (with inflation)
    sigma_sys: Dict[str, float]
    cov: np.ndarray  # statistical covariance (physical units)
    prior: Dict[str, Tuple[float, float]]
    chi2: Dict[str, dict]
    iterations: int
    warnings: List[str] = field(default_factory=list)
    derived: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    held_out: Dict[str, dict] = field(default_factory=dict)
    systematic: Dict[str, Dict[str, float]] = field(default_factory=dict)
    nuisance: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    bootstrap: Optional[dict] = None
    sigma_boot: Dict[str, float] = field(default_factory=dict)
    table_edge: List[str] = field(default_factory=list)  # parameters at a 2D-table bound

    @property
    def sigma_total(self) -> Dict[str, float]:
        return {n: math.hypot(self.sigma[n], self.sigma_sys.get(n, 0.0)) for n in self.names}

    def corr(self) -> np.ndarray:
        s = np.sqrt(np.diag(self.cov))
        return self.cov / np.outer(s, s)


def _ar1_neff(r: np.ndarray) -> float:
    """Effective sample size of a residual series under an AR(1) model."""
    n = len(r)
    if n < 4:
        return float(n)
    r = r - r.mean()
    den = float(r @ r)
    if den <= 0:
        return float(n)
    rho = float(r[:-1] @ r[1:]) / den
    rho = min(max(rho, 0.0), 0.98)
    return max(1.0, n * (1 - rho) / (1 + rho))


def run_fit(
    p: Predictor,
    prior: Optional[Dict[str, Tuple[float, float]]] = None,
    fixed: Optional[Dict[str, float]] = None,
    use: Optional[Sequence[str]] = None,
    max_iter: int = 60,
    reweight: bool = True,
    loss: str = "linear",
    start: Optional["FitResult"] = None,
    nuisance: bool = True,
) -> FitResult:
    """Weighted least squares over the normalized parameters z = (θ − μ)/σ_prior, with the
    priors as residuals z, by bounded Levenberg-Marquardt in two stages: the propagation
    constants and the scalar blocks (TDR, DC, microsection) first, which are nearly linear in
    the parameters, then every block. With `reweight` (twice), each block of many points is
    rescaled to its residual RMS (never below nominal) times √(n/n_eff), n_eff its AR(1)
    effective number of points, so that a block's χ² counts as n_eff observations; the
    covariance is the inverse Gauss-Newton information at the solution with those scales."""
    st = p.st
    fixed = dict(fixed or {})
    main = [n for n in st.params if n not in fixed]
    pr = {n: (stackups.PARAMS[n].nominal, stackups.PARAMS[n].sigma) for n in st.params}
    pr.update(prior or {})
    nui = p.nuisances() if nuisance else {}
    names = main + list(nui)
    mu = np.array([pr[n][0] for n in main] + [x[0] for x in nui.values()])
    sd = np.array([pr[n][1] for n in main] + [x[1] for x in nui.values()])
    lo, hi = bounds(main)
    lo = np.concatenate([lo, [x[2] for x in nui.values()]])
    hi = np.concatenate([hi, [x[3] for x in nui.values()]])
    zlo, zhi = (lo - mu) / sd, (hi - mu) / sd
    blks = blocks(p, use)
    f = p.d.f

    def values(z):
        th = np.clip(mu + sd * z, lo, hi)
        v = stackups.with_values(st, dict(zip(main, th)))
        v.update(dict(zip(names[len(main) :], th[len(main) :])))
        v.update(fixed)
        return v

    def parts_at(z, sel, scales):
        v = values(z)
        m = p.model(v, f)
        return [blks[i].fn(v, m) / scales[i] for i in sel]

    def flat(parts, z):
        r = np.concatenate(parts + [z])
        return r if np.all(np.isfinite(r)) else np.full_like(r, 1e6)

    # noise floors of the corrected sticks: their set's verification line (see `verification`)
    floors = {}
    for b in blks:
        if b.name.startswith("stick:"):
            _, sid, grp = b.name.split(":")
            ver = p.d.verify.get(p.d.board.stick(sid).trl)
            if ver:
                floors[b.name] = ver["feature_rms"][grp]
    v0 = values(np.zeros(len(names)))
    m0 = p.model(v0, f)
    neffs = [float(len(b.fn(v0, m0))) for b in blks]
    scales = [1.0] * len(blks)
    z = np.zeros(len(names))
    smooth = [i for i, b in enumerate(blks) if not b.name.startswith("stick:")]
    every = list(range(len(blks)))
    stages = [smooth, every] if len(smooth) < len(blks) else [smooth]
    if start is not None:
        # a refit (systematics): the weights and the starting point of an earlier fit
        for i, b in enumerate(blks):
            if b.name in start.chi2:
                scales[i] = start.chi2[b.name]["scale"]
                neffs[i] = start.chi2[b.name]["n_eff"]
        sv = dict(start.value, **{k: x[0] for k, x in start.nuisance.items()})
        z = np.array([(sv.get(n, m_) - m_) / s_ for n, m_, s_ in zip(names, mu, sd)])
        stages, reweight = [every], False
    it_total = 0
    for k, sel in enumerate(stages + ([every] * 2 if reweight else [])):
        if k >= len(stages):
            # re-estimate the noise level of every block of many points from its residuals
            # (never below nominal), and count it as its AR(1) effective number of points: the
            # connector and cable errors are smooth in frequency, so 600 residuals of one stick
            # carry the information of a few dozen independent ones
            parts = parts_at(z, every, [1.0] * len(blks))
            for i, (b, pp) in enumerate(zip(blks, parts)):
                if len(pp) >= 20:
                    neffs[i] = _ar1_neff(pp) if b.smooth else float(len(pp))
                    rms = max(1.0, float(np.sqrt(np.mean(pp**2))), floors.get(b.name, 0.0))
                    scales[i] = rms * math.sqrt(len(pp) / neffs[i])
        z, it = _lm(lambda zz: flat(parts_at(zz, sel, scales), zz), z, zlo, zhi, max_iter, loss)
        it_total += it
    parts = parts_at(z, every, scales)
    r = flat(parts, z)
    J = _jac(lambda zz: flat(parts_at(zz, every, scales), zz), z, r, zlo, zhi)
    F = J.T @ J  # includes the prior rows (identity)
    off = 0
    chi2 = {}
    for i, (b, pp) in enumerate(zip(blks, parts)):
        n = len(pp)
        chi2[b.name] = dict(
            n=n,
            n_eff=round(neffs[i], 1),
            rms=float(np.sqrt(np.mean(pp**2) * n / neffs[i])) if n else 0.0,
            scale=float(scales[i]),
        )
        off += n
    cz = np.linalg.inv(F)
    cov_all = cz * np.outer(sd, sd)
    th_all = np.clip(mu + sd * z, lo, hi)
    nm_ = len(main)
    nuis = {
        n: (float(th_all[i]), float(math.sqrt(cov_all[i, i])))
        for i, n in enumerate(names)
        if i >= nm_
    }
    # the fab parameters: the nuisances marginalized (the main block of the covariance)
    names, mu, sd, lo, hi, z = main, mu[:nm_], sd[:nm_], lo[:nm_], hi[:nm_], z[:nm_]
    zlo, zhi = zlo[:nm_], zhi[:nm_]
    cov = cov_all[:nm_, :nm_]
    th = th_all[:nm_]
    val = dict(zip(names, map(float, th)))
    sig = dict(zip(names, map(float, np.sqrt(np.diag(cov)))))
    warn = []
    for n, s_ in zip(names, sd):
        if sig[n] > 0.7 * s_:
            warn.append(f"{n}: prior-dominated (σ {sig[n]:.3g} of prior {s_:.3g})")
    edge = []
    for n, zi, a, b_ in zip(names, z, zlo, zhi):
        if zi <= a + 1e-9 or zi >= b_ - 1e-9:
            q = stackups.PARAMS[n]
            t_lo, t_hi = q.table if q.table is not None else (q.lo, q.hi)
            if (t_lo > q.lo and val[n] <= t_lo + 1e-9) or (t_hi < q.hi and val[n] >= t_hi - 1e-9):
                edge.append(n)
                warn.append(
                    f"{n}: at the edge of the 2D tables ({val[n]:.4g}); the as-built value is"
                    " probably outside them and the fit is biased (Z0 most): rebuild the tables"
                    " around it (`families build`) before using this fit"
                )
            else:
                warn.append(f"{n}: at its bound ({val[n]:.4g})")
    corr = cov / np.outer(np.sqrt(np.diag(cov)), np.sqrt(np.diag(cov)))
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if abs(corr[i, j]) > 0.9:
                warn.append(f"{names[i]} and {names[j]} correlated {corr[i, j]:+.2f}")
    v_all = values(z)
    for fam_id, sur in p.tables.items():
        out = sur.outside(v_all)
        if out:
            warn.append(f"{fam_id}: outside the 2D table range in {', '.join(out)} (extrapolated)")
    res = FitResult(names, val, sig, {}, cov, {n: pr[n] for n in names}, chi2, it_total, warn)
    res.table_edge = edge
    res.value.update({k: float(v) for k, v in fixed.items()})
    res.nuisance = nuis
    return res


def _lm(fun, z0, zlo, zhi, max_iter: int, loss: str):
    """Levenberg-Marquardt with Marquardt scaling and steps projected onto the bounds."""
    z = np.clip(z0, zlo, zhi)
    r = fun(z)
    w = _irls(r, loss)
    cost = float((w * r) @ (w * r))
    lam = 1e-3
    it = 0
    for it in range(1, max_iter + 1):
        J = _jac(fun, z, r, zlo, zhi)
        Jw = J * w[:, None]
        A = Jw.T @ Jw
        g = Jw.T @ (w * r)
        improved = False
        for _ in range(16):
            D = lam * (np.diag(np.diag(A)) + 1e-9 * np.eye(len(z)))
            zn = np.clip(z + np.linalg.solve(A + D, -g), zlo, zhi)
            rn = fun(zn)
            wn = _irls(rn, loss)
            cn = float((wn * rn) @ (wn * rn))
            if cn < cost:
                dz = float(np.max(np.abs(zn - z)))
                done = cost - cn < 1e-9 * cost and dz < 1e-6
                z, r, w, cost = zn, rn, wn, cn
                lam = max(lam / 5, 1e-12)
                improved = True
                break
            lam *= 5
        if not improved or done:
            break
    return z, it


def bounds(names: Sequence[str]) -> Tuple[np.ndarray, np.ndarray]:
    """Hard bounds of the fit: the parameter's physical range, narrowed to its 2D-table range
    where it has one (the surrogate is not trusted outside it)."""
    lo, hi = [], []
    for n in names:
        q = stackups.PARAMS[n]
        a, b = (q.lo, q.hi) if q.table is None else (max(q.lo, q.table[0]), min(q.hi, q.table[1]))
        lo.append(a)
        hi.append(b)
    return np.array(lo), np.array(hi)


def _irls(r: np.ndarray, loss: str) -> np.ndarray:
    if loss == "soft_l1":
        c = 3.0
        return (1 + (r / c) ** 2) ** -0.25
    return np.ones_like(r)


def _jac(fun, z, r0, zlo=None, zhi=None, h=1e-4):
    """Forward differences, taken inward at an upper bound."""
    J = np.empty((len(r0), len(z)))
    for i in range(len(z)):
        zz = z.copy()
        hi_ = h if zhi is None or z[i] + h <= zhi[i] else -h
        zz[i] += hi_
        J[:, i] = (fun(zz) - r0) / hi_
    return J


# --- derived quantities, held-out checks, systematics ---------------------------------------


def derived(p: Predictor, res: FitResult) -> Dict[str, Tuple[float, float]]:
    """Z0 and εeff at 5.8 GHz of each TRL family (and every fitted family), σ by the delta
    method from the statistical covariance."""
    st = p.st
    fams = sorted({t["family"] for t in p.d.board.trl.values()} | _fitted_families(p.d.board))
    f = np.array([F_PRODUCT])

    def q(v):
        m = p.model(v, f)
        out = []
        for fam in fams:
            line = m.line(fam)
            out += [
                float(abs(line.zc[0])),
                float(line.eps_eff[0]),
                float(line.gamma[0].real * 8.686 * 0.1),
            ]
        return np.array(out)

    v0 = stackups.with_values(st, res.value)
    y0 = q(v0)
    G = np.zeros((len(y0), len(res.names)))
    for j, n in enumerate(res.names):
        h = 1e-3 * max(math.sqrt(res.cov[j, j]), 1e-9)
        v = dict(v0)
        v[n] += h
        G[:, j] = (q(v) - y0) / h
    cy = G @ res.cov @ G.T
    out = {}
    for i, fam in enumerate(fams):
        for k, key in enumerate(("z0_ohm", "eps_eff", "loss_db_per_100mm")):
            a = 3 * i + k
            out[f"{fam}.{key}_5g8"] = (float(y0[a]), float(math.sqrt(max(cy[a, a], 0.0))))
    return out


def _fitted_families(b: catalog.Board):
    out = set()
    for s in b.sticks:
        if s.fitted:
            for e in s.elements:
                if e[0] == "line":
                    out.add(e[1])
    return out


def _centre(f, db, fc, rel=0.4):
    """Centre of a passband near fc: the power-weighted mean frequency of the contiguous
    region within 3 dB of the peak."""
    sel = np.where(np.abs(f - fc) <= rel * fc)[0]
    y = db[sel]
    j = int(np.argmax(y))
    a = b = j
    while a > 0 and y[a - 1] >= y[j] - 3.0:
        a -= 1
    while b < len(y) - 1 and y[b + 1] >= y[j] - 3.0:
        b += 1
    w = 10 ** (y[a : b + 1] / 10)
    return float(np.sum(w * f[sel][a : b + 1]) / np.sum(w))


def _notches(f, t, centers, rel=0.02):
    """Transmission zeros near `centers` from complex S21 `t`: a complex quadratic through the
    five points around the smallest |S21|, minimized on a fine grid. Near a zero S21 passes
    the origin almost linearly, so this resolves a notch far below the frequency step (a
    parabola in dB does not)."""
    out = []
    for fc in centers:
        sel = np.where(np.abs(f - fc) <= rel * fc)[0]
        if len(sel) < 5:
            out.append(float("nan"))
            continue
        j = int(np.argmin(np.abs(t[sel])))
        j = min(max(j, 2), len(sel) - 3)
        k = sel[j - 2 : j + 3]
        x = f[k] - f[k[2]]
        c = np.polyfit(x, t[k].real, 2) + 1j * np.polyfit(x, t[k].imag, 2)
        xx = np.linspace(x[0], x[-1], 801)
        out.append(float(f[k[2]] + xx[int(np.argmin(np.abs(np.polyval(c, xx))))]))
    return out


def held_out(p: Predictor, res: FitResult) -> Dict[str, dict]:
    """Predicted vs measured features of the held-out sticks (design §8.7 acceptance)."""
    d = p.d
    v = stackups.with_values(p.st, res.value)
    out = {}
    fmax = d.f_grid[-1]
    for sid, s_meas in d.corrected.items():
        st = d.board.stick(sid)
        if st.fitted or not st.elements or st.kind not in ("ring", "stub", "coupled"):
            continue
        fine = np.arange(d.f_grid[0], fmax, 2e6)
        s_mod = p.stick_s(p.model(v, fine), sid)
        db_m = 20 * np.log10(np.abs(s_meas[:, 1, 0]) + 1e-12)
        db_p = 20 * np.log10(np.abs(s_mod[:, 1, 0]) + 1e-12)
        if st.kind in ("ring", "stub"):
            # the ring's notches at its resonances n = 1, 2, 4 (its other zeros are 6-25 %
            # away); the stubs' notches. The predicted notch (the fitted model) centres the
            # search in the measurement.
            if st.kind == "ring":
                fr, win = [x for x in (2.9e9, 5.8e9, 11.6e9) if x < fmax * 0.95], 0.05
            else:
                fr = [5.8e9] + ([11.6e9] if st.geometry["end"] == "short" and fmax > 12e9 else [])
                win = 0.08
            fp = _notches(fine, s_mod[:, 1, 0], fr, rel=win)
            step = float(d.f_grid[1] - d.f_grid[0])
            fm = [
                _notches(d.f_grid, s_meas[:, 1, 0], [x], rel=max(0.015, 3.5 * step / x))[0]
                for x in fp
            ]
            tol = 0.005
        else:
            # the coupled section's passband centre: the power centroid above -3 dB of its peak
            fr = [5.8e9]
            fm, fp = [_centre(d.f_grid, db_m, 5.8e9)], [_centre(fine, db_p, 5.8e9)]
            tol = 0.01
        rel = [(a - b) / b for a, b in zip(fm, fp)]
        ok = all(abs(x) <= tol for x in rel if x == x)
        out[sid] = dict(
            kind=st.kind,
            measured_ghz=[round(x / 1e9, 4) for x in fm],
            predicted_ghz=[round(x / 1e9, 4) for x in fp],
            rel_err=[round(x, 5) for x in rel],
            tolerance=tol,
            passed=bool(ok),
        )
    return out


def systematics(
    p: Predictor, res: FitResult, prior=None, fixed=None
) -> Dict[str, Dict[str, float]]:
    """Refit under alternative model choices; |Δ| per parameter (design §8.6)."""
    out = {}
    # roughness model: Groiss with Rq (µm) instead of Huray's ratio
    alt = Predictor(p.d, p.tables, roughness="groiss")
    pr = dict(prior or {})
    for n in p.st.params:
        if n.endswith(".rough"):
            pr[n] = (1.0, 0.7)
    r2 = run_fit(alt, pr, fixed, start=res)
    out["roughness=groiss"] = {
        n: abs(r2.value[n] - res.value[n]) for n in res.names if not n.endswith(".rough")
    }
    # SOLT load ±0.5 Ω: shifts the TDR impedance
    if p.d.tdr:
        saved = dict(p.d.tdr)
        deltas = {}
        for sgn in (+1, -1):
            p.d.tdr = {k: z + sgn * 0.5 for k, z in saved.items()}
            r3 = run_fit(p, prior, fixed, start=res)
            for n in res.names:
                deltas[n] = max(deltas.get(n, 0.0), abs(r3.value[n] - res.value[n]))
        p.d.tdr = saved
        out["solt_load=±0.5ohm"] = deltas
    return out


def bootstrap(
    p: Predictor, res: FitResult, draws: int = 12, seed: int = 7, prior=None, imp=None
) -> dict:
    """Parametric bootstrap through the whole pipeline (design §8.6): `draws` synthetic sessions
    simulated at the fitted values with the error model of `synthetic.Imperfections` (soldered
    connectors, SOLT residuals, noise, drift), each calibrated and refitted with the fit's own
    weights. The spread of the refits is the estimator's sampling uncertainty, including what
    the block model of the fit leaves out (the calibration's errors are shared by every
    corrected stick). The session's verification line scales the connector spread when it is
    worse than 0.021 (the default model gives a median of 0.023 on board A and 0.027 on board
    B, 1-6 GHz, so typical sessions are scaled up slightly: conservative)."""
    import shutil
    import tempfile

    from yapnr.rf.coupons import synthetic

    imp = imp or synthetic.Imperfections()
    ver = [v["max_dev"] for v in p.d.verify.values()]
    if ver and max(ver) > 0.021:
        k = max(ver) / 0.021
        imp = dataclasses.replace(imp, spread=imp.spread * k, spread_len_mm=imp.spread_len_mm * k)
    rng = np.random.default_rng(seed)
    vals, ders = [], []
    tmp = tempfile.mkdtemp(prefix="coupon-boot-")
    try:
        for k in range(draws):
            out = os.path.join(tmp, f"b{k}")
            synthetic.simulate(
                p.d.stackup,
                dict(res.value),
                out,
                p.d.f_grid,
                rng,
                imp=imp,
                truth_tables=p.tables,
                microsection=bool(p.d.microsection),
            )
            d = prepare(p.d.stackup, session.load(out), f_max=float(p.d.f[-1]))
            if not p.d.microsection:
                d.microsection = {}
            if not p.d.dc:
                d.dc = []
            pb = Predictor(d, p.tables, p.roughness)
            rb = run_fit(pb, prior, start=res)
            vals.append([rb.value[n] for n in res.names])
            ders.append({key: v for key, (v, _) in derived(pb, rb).items()})
            shutil.rmtree(out, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    v = np.array(vals)
    cov = np.cov(v.T, ddof=1) if draws > 1 else np.zeros((len(res.names),) * 2)
    dsig = (
        {key: float(np.std([x[key] for x in ders], ddof=1)) for key in ders[0]} if draws > 1 else {}
    )
    return dict(draws=draws, cov=cov, derived_sigma=dsig, imperfections=dataclasses.asdict(imp))


def apply_bootstrap(res: FitResult, boot: dict) -> None:
    """Report max(fit σ, bootstrap σ) per parameter, keeping the fit's correlations, and the
    same for the derived quantities."""
    sb = np.sqrt(np.clip(np.diag(boot["cov"]), 0, None))
    sf = np.sqrt(np.diag(res.cov))
    s = np.maximum(sf, sb)
    corr = res.cov / np.outer(sf, sf)
    res.cov = corr * np.outer(s, s)
    res.sigma = dict(zip(res.names, map(float, s)))
    res.sigma_boot = dict(zip(res.names, map(float, sb)))
    for key, (v, sd) in list(res.derived.items()):
        res.derived[key] = (v, max(sd, boot["derived_sigma"].get(key, 0.0)))


def extract(
    stackup_id: str,
    session_dir: str,
    serial: Optional[str] = None,
    prior: Optional[Dict[str, Tuple[float, float]]] = None,
    f_max: Optional[float] = None,
    with_systematics: bool = True,
    tables=None,
    loss: str = "linear",
    boot: int = 0,
) -> Tuple[FitResult, Data, Predictor]:
    """The whole pipeline on a session directory (design §8): quality checks, multiline TRL,
    the joint fit, derived quantities, held-out checks, optionally the parametric bootstrap
    (`boot` sessions) and the systematic refits."""
    ses = session.load(session_dir, serial)
    d = prepare(stackup_id, ses, f_max=f_max)
    p = Predictor(d, tables)
    res = run_fit(p, prior, loss=loss)
    for n in res.table_edge:
        d.problems.append(
            f"{n} at the edge of the 2D tables ({res.value[n]:.4g}): the fab is outside the"
            " modelled range; rebuild the tables around it and refit"
        )
    res.derived = derived(p, res)
    res.held_out = held_out(p, res)
    if boot:
        res.bootstrap = bootstrap(p, res, boot, prior=prior)
        apply_bootstrap(res, res.bootstrap)
    if with_systematics:
        res.systematic = systematics(p, res, prior)
        sys_sum: Dict[str, float] = {}
        for deltas in res.systematic.values():
            for n, x in deltas.items():
                sys_sum[n] = math.hypot(sys_sum.get(n, 0.0), x)
        res.sigma_sys = sys_sum
    return res, d, p
