"""Closed-form starting designs (`optimizer.seed`), computed from the spec alone.

The paper starts from a uniform density. For a radiated-power target with copper that start is
a local optimum: a transparent sheet (x = 0.3), an absorbing one (0.5) and a near-copper plate
(0.7, also from β = 32) all stayed there (the bare open-ended feed, or the plate), because gray
copper absorbs before it radiates (docs/rf-inverse-design.md). `seed: patch` starts the
antenna instead from the textbook inset-fed rectangular patch, which the optimizer then
reshapes freely:

- width W = c/(2 f0) √(2/(εr + 1)), ε_eff of a strip of width W (Hammerstad–Jensen), the
  fringing extension ΔL (Hammerstad) and the length L = c/(2 f0 √ε_eff) − 2ΔL;
- the edge resistance R_e = 1/(2 G1) with G1 = W/(120 λ0) (1 − (k0 h)²/24), and the inset
  depth y0 = (L/π) arccos √(Z_feed/R_e) for the port's 50 Ω feed, with slots of 1.5 times the
  minimum space beside the feed;
- f0 the centre of the bands the requirements use; the patch is centred in the design region
  (on the port's axis) and fed by a straight line from the port pad, all rounded to pixels
  (W to an odd or even count matching the feed, so a mirror-symmetric spec stays symmetric);
- then the best of its whole-pixel neighbours (length ±1, width ±2, inset ±1 pixels) by the
  spec's epigraph value on the problem's grid (`tuned_patch`, 27 forward runs): one pixel of
  length moves the resonance by about 5 %, so the closed form lands between pixels (on the
  antenna case's grid it resonates 1.5 % high and the optimizer alone did not move it).

`seed: star` joins every port with feed-width lines to the window's centre (a plain junction),
a lossless start for multi-port specs whose uniform start stays absorbing.

Copper pixels get x = 0.7 and void x = 0.3 (the star): near binary after the β = 8 projection
(ρ̄ ≈ 0.96 and 0.04) but on its steep part, so every boundary can move from the first
iteration. The patch's seed is 0.9/0.1 and its schedule starts at β = 16 (`VALUES`).
"""

from __future__ import annotations

import math

import numpy as np

from yapnr.rf.constants import C0
from yapnr.rf.stackup import hammerstad_jensen

# x of copper and void pixels per seed. The star's feed-width lines survive the β = 8 filter
# and projection at 0.7/0.3 (ρ̄ ≈ 0.96/0.04). The patch's inset slots are narrow: at 0.7/0.3,
# two pixels wide and β = 8 they blurred to ρ̄ ≈ 0.42 (a lossy 32 Ω/sq) and the first step
# closed them; at 0.9/0.1 and β = 32 every pixel saturated and nothing moved. The patch uses
# 0.9/0.1, three-pixel slots and a schedule from β = 16.
VALUES = {"patch": (0.9, 0.1), "star": (0.7, 0.3)}


def patch_dimensions(er: float, h: float, f0: float, z_feed: float = 50.0) -> dict:
    """Closed-form inset-fed patch (metres): width, length, inset depth, edge resistance."""
    lam0 = C0 / f0
    w = C0 / (2.0 * f0) * math.sqrt(2.0 / (er + 1.0))
    _, eeff = hammerstad_jensen(w, h, er)
    u = w / h
    dl = 0.412 * h * (eeff + 0.3) * (u + 0.264) / ((eeff - 0.258) * (u + 0.8))
    length = C0 / (2.0 * f0 * math.sqrt(eeff)) - 2.0 * dl
    k0h = 2.0 * math.pi * h / lam0
    g1 = w / (120.0 * lam0) * (1.0 - k0h * k0h / 24.0)
    r_edge = 1.0 / (2.0 * g1)
    inset = length / math.pi * math.acos(min(1.0, math.sqrt(z_feed / r_edge)))
    return {"w": w, "l": length, "inset": inset, "r_edge": r_edge, "eps_eff": eeff}


def patch_mask(problem, dl: int = 0, dw: int = 0, di: int = 0) -> np.ndarray:
    """The inset-fed patch as window pixels (1 copper, 0 void) for a one-port spec; `dl`, `dw`
    and `di` change its length, width and inset depth by whole pixels."""
    spec = problem.spec
    if len(spec.ports) != 1:
        raise ValueError("the patch seed needs a one-port spec")
    port = spec.ports[0]
    st = spec.stackup.to_stackup()
    used = {r.band for r in spec.requirements}
    lo = min(spec.bands[b].lo_ghz for b in used)
    hi = max(spec.bands[b].hi_ghz for b in used)
    f0 = 0.5 * (lo + hi) * 1e9
    dims = patch_dimensions(st.er, st.h, f0)
    pitch = problem.pitch
    ni, nj = problem.design_shape
    # Work along the feed axis a (from the port inward) and across it t.
    along_n, across_n = (ni, nj) if port.side in ("W", "E") else (nj, ni)
    x0, x1, y0, y1 = (v * 1e-3 for v in spec.design_region)
    t_lo = y0 if port.side in ("W", "E") else x0
    wf = problem.widths[port.n]
    tc = (port.at_mm * 1e-3 - t_lo) / pitch  # the feed centre in pixels across
    f_lo = int(round(tc - 0.5 * wf))
    n_w = int(round(dims["w"] / pitch))
    if (n_w - wf) % 2:
        n_w += 1  # centred on the feed's centre line
    n_w = max(wf, n_w + int(dw))
    n_l = max(1, int(round(dims["l"] / pitch)) + int(dl))
    n_in = max(0, min(n_l - 1, int(round(dims["inset"] / pitch)) + int(di)))
    # Slots of 1.5 times the minimum space, so they stay void through the filter at β = 16.
    gap = max(1, int(math.ceil(1.5 * spec.rules.min_space_mm * 1e-3 / pitch - 1e-6)))
    start = max(0, (along_n - n_l) // 2)
    m = np.zeros((along_n, across_n))
    m[:start, f_lo : f_lo + wf] = 1.0  # the feed line
    p_lo = f_lo - (n_w - wf) // 2
    m[start : start + n_l, max(0, p_lo) : min(across_n, p_lo + n_w)] = 1.0
    if n_in > 0:
        m[start : start + n_in, max(0, f_lo - gap) : f_lo] = 0.0
        m[start : start + n_in, f_lo + wf : min(across_n, f_lo + wf + gap)] = 0.0
    if port.side in ("E", "N"):
        m = m[::-1]
    return m if port.side in ("W", "E") else m.T


def star_mask(problem) -> np.ndarray:
    """Every port's feed continued straight to the window's centre line across its axis, then
    along that line to the centre: a junction of feed-width lines joining all ports."""
    spec = problem.spec
    pitch = problem.pitch
    ni, nj = problem.design_shape
    x0, _, y0, _ = (v * 1e-3 for v in spec.design_region)
    m = np.zeros((ni, nj))
    ic, jc = ni / 2.0, nj / 2.0
    for port in spec.ports:
        w = problem.widths[port.n]
        if port.side in ("W", "E"):
            t_lo = int(round((port.at_mm * 1e-3 - y0) / pitch - 0.5 * w))
            a_lo, a_hi = (
                (0, int(round(ic + 0.5 * w)))
                if port.side == "W"
                else (
                    int(round(ic - 0.5 * w)),
                    ni,
                )
            )
            m[a_lo:a_hi, t_lo : t_lo + w] = 1.0
            c_lo = int(round(ic - 0.5 * w))
            lo, hi = sorted((t_lo, int(round(jc - 0.5 * w))))
            m[c_lo : c_lo + w, lo : hi + w] = 1.0
        else:
            t_lo = int(round((port.at_mm * 1e-3 - x0) / pitch - 0.5 * w))
            a_lo, a_hi = (
                (0, int(round(jc + 0.5 * w)))
                if port.side == "S"
                else (
                    int(round(jc - 0.5 * w)),
                    nj,
                )
            )
            m[t_lo : t_lo + w, a_lo:a_hi] = 1.0
            c_lo = int(round(jc - 0.5 * w))
            lo, hi = sorted((t_lo, int(round(ic - 0.5 * w))))
            m[lo : hi + w, c_lo : c_lo + w] = 1.0
    return m


# The whole-pixel neighbours of the closed-form patch that `tuned_patch` compares: one pixel of
# length is about 5 % of resonance at the cases' pitch, so the closed form lands between pixels.
PATCH_FAMILY = [(dl, dw, di) for dl in (-1, 0, 1) for dw in (-2, 0, 2) for di in (-1, 0, 1)]


def _geometry_sha(spec) -> str:
    """A hash of the spec without its optimizer settings (the seed does not depend on them)."""
    import hashlib
    import json

    d = spec.to_dict()
    d.pop("optimizer", None)
    blob = json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def tuned_patch(problem, cache_dir: str | None = None, log=None) -> tuple[np.ndarray, dict]:
    """The best binary patch of `PATCH_FAMILY` by the spec's epigraph value max_k f_k on the
    problem's grid (one forward run each, no gradients), and the choice. With `cache_dir` the
    choice is kept in `seed.json` (keyed by a hash of the spec without its optimizer settings)
    so a resumed or re-tuned run does not repeat it."""
    import json
    import os

    sha = _geometry_sha(problem.spec)
    path = os.path.join(cache_dir, "seed.json") if cache_dir else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("spec_geometry_sha256") == sha:
            dl, dw, di = saved["choice"]
            return patch_mask(problem, dl, dw, di), saved
    log = log or (lambda *_: None)
    scores = []
    for dl, dw, di in PATCH_FAMILY:
        m = patch_mask(problem, dl, dw, di)
        t = float(np.max(problem.evaluate(m, gradients=False).values))
        scores.append(((dl, dw, di), t))
        log(f"patch seed {(dl, dw, di)}: t {t:+.3f}")
    choice, t = min(scores, key=lambda c: (c[1], [abs(v) for v in c[0]]))
    info = {
        "spec_geometry_sha256": sha,
        "seed": "patch",
        "choice": list(choice),
        "t": t,
        "family": [[list(c), v] for c, v in scores],
    }
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
    return patch_mask(problem, *choice), info


def initial_x(problem, cache_dir: str | None = None, log=None) -> np.ndarray:
    """The design variables of the spec's seed (`optimizer.seed`)."""
    seed = problem.spec.optimizer.seed
    if seed == "patch":
        mask, _ = tuned_patch(problem, cache_dir, log)
    elif seed == "star":
        mask = star_mask(problem)
    else:
        raise ValueError(f"unknown seed {seed!r}")
    copper, void = VALUES[seed]
    rho = void + (copper - void) * mask
    return problem.param.grid.restrict(rho)
