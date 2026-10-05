"""Palace results as the radar sign-offs compare them (docs/rf-palace.md, "Reading the results").

- ``read_table``, ``port_s``, ``port_z``, ``mode_results``, ``palace_meta``: Palace's CSV tables
  (``port-S.csv``, ``port-Z.csv``, ``mode-*.csv``) and ``palace.json``;
- ``stage_dirs``: the outputs of a ``palace_job.py`` task (its stages, then the main solve);
- ``to_50``: Palace's modal wave-port S-parameters (each port matched to its own mode) as openEMS
  reports them, 50-ohm voltage waves with the lines still terminated in their own impedance;
- ``eeff_loss``: effective permittivity and loss per mm from two lines of different length (the
  ports' ends cancel);
- ``notch``, ``dip``, ``re_peak``, ``zin``: the frequency of a transmission notch, of a reflection
  dip and of the peak of Re Zin (a resonance whose frequency does not depend on the match);
- ``deembed_lumped``: a lumped port's S11 moved along a line of known Z0 and gamma;
- ``compare``: two solvers' S-parameter column at chosen frequencies, against the sign-off
  criteria (frequency within 1 %, |S21| within 0.5 dB, phase difference reported).

numpy only; no Palace or gmsh needed (results are read where they were fetched).
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

C0 = 299792458.0
S_MAG_RE = re.compile(r"^\|S\[(\d+)\]\[(\d+)\]\| \(dB\)$")
Z_RE = re.compile(r"^Re\{Z_PV\[(\d+)\]\} \(Ohm\)$")
FREQ_TOL = 0.01  # 1 %: the sign-off agreement in frequency (design.md section 4)
DB_TOL = 0.5  # dB: in |S21|


# --- Palace's tables


def read_table(path: str) -> Dict[str, np.ndarray]:
    """{column header: values} of a Palace CSV (headers stripped of padding)."""
    with open(path, newline="") as fh:
        rows = [[c.strip() for c in r] for r in csv.reader(fh) if r]
    head = [h for h in rows[0] if h]
    data = np.array([[float(c) for c in r[: len(head)]] for r in rows[1:]], dtype=float)
    data = data.reshape(len(rows) - 1, len(head))
    return {h: data[:, i] for i, h in enumerate(head)}


def port_s(path: str) -> Tuple[np.ndarray, Dict[Tuple[int, int], np.ndarray]]:
    """(f GHz, {(i, j): complex S}) of a ``port-S.csv``."""
    t = read_table(path)
    out = {}
    for h in t:
        m = S_MAG_RE.match(h)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            ph = np.radians(t["arg(S[%d][%d]) (deg.)" % (i, j)])
            out[(i, j)] = 10 ** (t[h] / 20) * np.exp(1j * ph)
    return t["f (GHz)"], out


def port_z(path: str) -> Optional[Tuple[np.ndarray, Dict[int, np.ndarray]]]:
    """(f GHz, {port: complex Z_PV}) of a ``port-Z.csv`` (wave ports), None without the file."""
    if not os.path.exists(path):
        return None
    t = read_table(path)
    out = {}
    for h in t:
        m = Z_RE.match(h)
        if m:
            k = int(m.group(1))
            out[k] = t[h] + 1j * t["Im{Z_PV[%d]} (Ohm)" % k]
    return t["f (GHz)"], out


def mode_results(post: str) -> Dict[str, Dict[str, float]]:
    """A BoundaryMode output directory's ``mode-kn.csv`` and ``mode-Z.csv`` (first mode), plus
    ``eeff`` (Re n_eff squared), ``loss_db_mm`` (from Im kn, 1/m) and ``z_pv`` (ohm) where those
    columns exist."""
    out: Dict[str, Any] = {}
    for key, name in (("kn", "mode-kn.csv"), ("z", "mode-Z.csv")):
        path = os.path.join(post, name)
        if os.path.exists(path):
            out[key] = {k: float(v[0]) for k, v in read_table(path).items()}
    kn = out.get("kn", {})
    if "Re{n_eff}" in kn:
        out["eeff"] = kn["Re{n_eff}"] ** 2
    if "Im{kn} (1/m)" in kn:
        out["loss_db_mm"] = float(20 / np.log(10) * abs(kn["Im{kn} (1/m)"]) * 1e-3)
    z = out.get("z", {})
    if "Z_PV[1] (Ohm)" in z:
        out["z_pv"] = z["Z_PV[1] (Ohm)"]
    return out


def palace_meta(post: str) -> Optional[Dict[str, Any]]:
    """The numbers of ``palace.json`` a comparison needs (None without the file)."""
    try:
        with open(os.path.join(post, "palace.json"), encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    pr = m.get("Problem") or {}
    lin = m.get("LinearSolver") or {}
    peak = m.get("PeakMemoryMegabytes") or {}
    return dict(
        dofs=pr.get("DegreesOfFreedom"),
        elements=pr.get("MeshElements"),
        iteration=pr.get("Iteration"),
        solves=lin.get("TotalSolves"),
        its=lin.get("TotalIts"),
        total_s=((m.get("ElapsedTime") or {}).get("Durations") or {}).get("Total"),
        peak_mb_sum=peak.get("Total"),
        peak_mb_max=peak.get("Max"),
    )


def stage_dirs(task_out: str) -> List[Tuple[str, str]]:
    """(name, postpro directory) of a ``palace_job.py`` task's solves in order: its stages
    (``stage-<k>-<stem>``, by k) and then the main solve (``postpro``)."""
    out = []
    for path in glob.glob(os.path.join(task_out, "stage-*")):
        if not os.path.isdir(path):
            continue
        m = re.match(r"^stage-(\d+)-(.+)$", os.path.basename(path))
        out.append(((int(m.group(1)) if m else 0), os.path.basename(path), path))
    rows = [(name, path) for _, name, path in sorted(out)]
    if os.path.isdir(os.path.join(task_out, "postpro")):
        rows.append(("main", os.path.join(task_out, "postpro")))
    return rows


# --- conversions


def to_50(
    S: Dict[Tuple[int, int], np.ndarray],
    Z: Dict[int, np.ndarray],
    excite: int = 1,
    R: float = 50.0,
) -> Dict[Tuple[int, int], np.ndarray]:
    """Palace's modal S column of port ``excite`` (every port matched to its own mode, power
    waves) as openEMS reports it: ``R``-ohm voltage waves at every port with the lines still
    terminated in their own impedance Re Z_PV, S'_i1 = b'_i / a'_1 (for i != 1, the line's
    transmission scaled by sqrt(Z_i / Z_1) and the input's mismatch). ``Z``: {port: Z_PV}."""
    s11 = S[(excite, excite)]
    z1 = np.real(Z[excite])
    g1 = (z1 - R) / (z1 + R)
    out = {}
    for (i, j), s in S.items():
        if j != excite:
            continue
        if i == excite:
            out[(i, j)] = (s + g1) / (1 + g1 * s)
        else:
            zi = np.real(Z[i])
            out[(i, j)] = s * (zi + R) * np.sqrt(z1) / ((z1 + R) * np.sqrt(zi) * (1 + g1 * s11))
    return out


def zin(s11: np.ndarray, R: float = 50.0) -> np.ndarray:
    """Input impedance of a reflection referenced to ``R``."""
    return R * (1 + s11) / (1 - s11)


def deembed_lumped(
    s11: np.ndarray, z0: np.ndarray, gamma: np.ndarray, length: float, R: float = 50.0
) -> np.ndarray:
    """S11 (referenced to ``R``) moved ``length`` mm along a line of ``z0`` and ``gamma`` (1/mm,
    alpha + j beta) away from the port: the load the line feeds."""
    zi = zin(s11, R)
    t = np.tanh(gamma * length)
    zl = z0 * (zi - z0 * t) / (z0 - zi * t)
    return (zl - R) / (zl + R)


def eeff_loss(
    f_ghz: np.ndarray,
    s21_short: np.ndarray,
    s21_long: np.ndarray,
    dl_mm: float,
    n_range: Tuple[float, float] = (1.0, 4.0),
    n_guess: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Effective permittivity and loss (dB/mm, positive) from the transmission of two lines
    ``dl_mm`` apart in length: the phase difference picks the branch of beta closest to
    ``n_guess`` x k0 (default: the middle of ``n_range``) at the first frequency and follows it."""
    f = np.asarray(f_ghz, dtype=float)
    k0 = 2 * np.pi * f * 1e9 / C0 * 1e-3  # rad/mm
    dphi = np.unwrap(np.angle(s21_short) - np.angle(s21_long))
    guess = n_guess if n_guess is not None else 0.5 * sum(n_range)
    m = np.round((guess * k0[0] * dl_mm - dphi[0]) / (2 * np.pi))
    n = (dphi + 2 * np.pi * m) / dl_mm / k0
    n = np.where((n > n_range[0]) & (n < n_range[1]), n, np.nan)
    loss = (20 * np.log10(np.abs(s21_short)) - 20 * np.log10(np.abs(s21_long))) / dl_mm
    return n * n, loss


# --- features


def _parabola(ff: np.ndarray, yy: np.ndarray, i: int) -> Tuple[float, float]:
    if 0 < i < len(ff) - 1:
        y0, y1, y2 = yy[i - 1], yy[i], yy[i + 1]
        den = y0 - 2 * y1 + y2
        d = 0.5 * (y0 - y2) / den if den else 0.0
        return float(ff[i] + d * (ff[i + 1] - ff[i])), float(y1 - 0.25 * (y0 - y2) * d)
    return float(ff[i]), float(yy[i])


def notch(
    f: np.ndarray, y_db: np.ndarray, lo: float = -np.inf, hi: float = np.inf
) -> Tuple[float, float]:
    """(frequency, depth) of the deepest minimum of ``y_db`` in [lo, hi], refined by a parabola
    through the three samples around it."""
    m = (f >= lo) & (f <= hi)
    ff, yy = np.asarray(f)[m], np.asarray(y_db)[m]
    if not len(ff):
        raise ValueError("no samples in [%g, %g]" % (lo, hi))
    return _parabola(ff, yy, int(np.argmin(yy)))


dip = notch  # a reflection dip is found the same way


def re_peak(
    f: np.ndarray, z: np.ndarray, lo: float = -np.inf, hi: float = np.inf
) -> Tuple[float, float]:
    """(frequency, value) of the largest Re Z in [lo, hi] (parabolic refinement): a resonance's
    frequency that, unlike the reflection dip, does not move with the match."""
    m = (f >= lo) & (f <= hi)
    ff, yy = np.asarray(f)[m], -np.real(np.asarray(z)[m])
    fpk, v = _parabola(ff, yy, int(np.argmin(yy)))
    return fpk, -v


def band_below(
    f: np.ndarray, y_db: np.ndarray, level: float = -10.0
) -> Optional[Tuple[float, float]]:
    """The contiguous range around the deepest point where ``y_db`` stays at or below ``level``."""
    i = int(np.argmin(y_db))
    if y_db[i] > level:
        return None
    lo = hi = i
    while lo > 0 and y_db[lo - 1] <= level:
        lo -= 1
    while hi < len(y_db) - 1 and y_db[hi + 1] <= level:
        hi += 1
    return float(f[lo]), float(f[hi])


def at(f: np.ndarray, values: np.ndarray, f0: float) -> complex:
    """``values`` interpolated (real and imaginary parts) at ``f0``."""
    v = np.asarray(values)
    if np.iscomplexobj(v):
        return complex(np.interp(f0, f, v.real) + 1j * np.interp(f0, f, v.imag))
    return float(np.interp(f0, f, v))


# --- the sign-off comparison


def compare(
    a: Tuple[np.ndarray, np.ndarray],
    b: Tuple[np.ndarray, np.ndarray],
    freqs: Sequence[float],
    feature: Optional[Tuple[float, float]] = None,
    freq_tol: float = FREQ_TOL,
    db_tol: float = DB_TOL,
) -> Dict[str, Any]:
    """Two solvers' transmission ``(f GHz, complex S21)``: |S21| (dB) and phase differences
    (degrees, a - b) at ``freqs``, and, with ``feature`` (lo, hi), the frequency of the deepest
    notch of each in that range. ``ok`` is |d|S21|| <= ``db_tol`` everywhere and the notch
    frequencies within ``freq_tol`` (relative); phase is reported, not judged (an absolute phase
    over a long feed carries both solvers' dispersion error, design.md section 4)."""
    fa, sa = a
    fb, sb = b
    rows = []
    worst = 0.0
    for f0 in freqs:
        va, vb = at(fa, sa, f0), at(fb, sb, f0)
        d_db = 20 * np.log10(abs(va)) - 20 * np.log10(abs(vb))
        d_ph = float(np.degrees(np.angle(va / vb)))
        worst = max(worst, abs(d_db))
        rows.append(dict(f_ghz=f0, a_db=20 * np.log10(abs(va)), b_db=20 * np.log10(abs(vb)),
                         d_db=float(d_db), d_deg=d_ph))  # fmt: skip
    out: Dict[str, Any] = dict(points=rows, max_d_db=float(worst), ok=bool(worst <= db_tol))
    if feature is not None:
        na = notch(fa, 20 * np.log10(np.abs(sa)), *feature)
        nb = notch(fb, 20 * np.log10(np.abs(sb)), *feature)
        rel = (na[0] - nb[0]) / nb[0]
        out["notch"] = dict(a=na, b=nb, rel=float(rel))
        out["ok"] = bool(out["ok"] and abs(rel) <= freq_tol)
    return out
