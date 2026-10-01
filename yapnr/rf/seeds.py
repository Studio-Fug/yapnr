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
  depth y0 = (L/π) arccos √(Z_feed/R_e) for the port's 50 Ω feed, with gaps of the minimum
  space beside the feed;
- f0 the centre of the bands the requirements use; the patch is centred in the design region
  (on the port's axis) and fed by a straight line from the port pad, all rounded to pixels
  (W to an odd or even count matching the feed, so a mirror-symmetric spec stays symmetric).

Copper pixels get x = 0.7 and void x = 0.3: near binary after the β = 8 projection (ρ̄ ≈ 0.96
and 0.04) but on its steep part, so every boundary can move from the first iteration.
"""

from __future__ import annotations

import math

import numpy as np

from yapnr.rf.constants import C0
from yapnr.rf.stackup import hammerstad_jensen

COPPER, VOID = 0.7, 0.3


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


def patch_mask(problem) -> np.ndarray:
    """The inset-fed patch as window pixels (1 copper, 0 void) for a one-port spec."""
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
    n_l = max(1, int(round(dims["l"] / pitch)))
    n_in = int(round(dims["inset"] / pitch))
    gap = max(1, int(math.ceil(spec.rules.min_space_mm * 1e-3 / pitch - 1e-6)))
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


def initial_x(problem) -> np.ndarray:
    """The design variables of the spec's seed (`optimizer.seed`)."""
    seed = problem.spec.optimizer.seed
    if seed == "patch":
        mask = patch_mask(problem)
    else:
        raise ValueError(f"unknown seed {seed!r}")
    rho = VOID + (COPPER - VOID) * mask
    return problem.param.grid.restrict(rho)
