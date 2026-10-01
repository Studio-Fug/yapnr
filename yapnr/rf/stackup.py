"""Single-layer microstrip stackups, the copper sheet model and closed-form line formulas.

The board is a PEC ground at z = 0, a uniform substrate of thickness h, and one zero-thickness
copper layer at z = h with air above (design §4.1). Copper is a sheet of conductance
G_max = 1/R_s(f_ref): the surface resistance of smooth copper at the reference frequency (§4.3).

The closed forms are for zero-thickness strips:

- Hammerstad and Jensen (1980): the quasi-static impedance and effective permittivity;
- Kirschning and Jansen (1982): the dispersion of the effective permittivity.

They are the references of the microstrip unit tests, not part of the solver.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from yapnr.rf.constants import EPS0, ETA0, MU0, SIGMA_CU


@dataclass(frozen=True)
class Stackup:
    """A single-layer microstrip stackup (SI units).

    Attributes:
      er: relative permittivity of the substrate (design value).
      tan_delta: loss tangent of the substrate, matched at f_ref.
      h: substrate thickness (m).
      f_ref: reference frequency (Hz) for the dielectric loss and the copper sheet conductance.
      sigma_cu: copper conductivity (S/m).
    """

    er: float
    tan_delta: float
    h: float
    f_ref: float
    sigma_cu: float = SIGMA_CU

    def __post_init__(self) -> None:
        if not (self.er >= 1.0 and self.h > 0.0 and self.f_ref > 0.0 and self.tan_delta >= 0.0):
            raise ValueError(f"invalid stackup {self!r}")

    @property
    def sigma_sub(self) -> float:
        """Substrate conductivity (S/m) giving tan δ at f_ref: 2π f_ref ε0 εr tan δ."""
        return 2.0 * math.pi * self.f_ref * EPS0 * self.er * self.tan_delta

    @property
    def surface_resistance(self) -> float:
        """R_s = sqrt(π f μ0 / σ_Cu) at f_ref (Ω per square)."""
        return math.sqrt(math.pi * self.f_ref * MU0 / self.sigma_cu)

    @property
    def g_max(self) -> float:
        """Sheet conductance of copper (S): 1/R_s(f_ref)."""
        return 1.0 / self.surface_resistance

    @property
    def g_min(self) -> float:
        """Sheet conductance of void, log-symmetric to G_max about 1/η0: 1/(η0² G_max)."""
        return 1.0 / (ETA0 * ETA0 * self.g_max)


def hammerstad_jensen(w: float, h: float, er: float) -> tuple[float, float]:
    """Quasi-static (Z0, ε_eff) of a zero-thickness microstrip of width w on height h.

    E. Hammerstad, Ø. Jensen, "Accurate models for microstrip computer-aided design", IEEE MTT-S
    Digest (1980). Stated accuracy 0.2 % (ε_eff) and 0.01 % (Z0 in air) for 0.01 ≤ w/h ≤ 100.
    """
    u = w / h
    fu = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    z01 = ETA0 / (2.0 * math.pi) * math.log(fu / u + math.sqrt(1.0 + (2.0 / u) ** 2))
    a = (
        1.0
        + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0
        + math.log(1.0 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    eps_eff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)
    return z01 / math.sqrt(eps_eff), eps_eff


def kirschning_jansen_eps_eff(w: float, h: float, er: float, f: float) -> float:
    """Dispersive ε_eff(f) of a zero-thickness microstrip (Kirschning and Jansen, 1982).

    The static value is Hammerstad and Jensen's; f in Hz, w and h in m.
    """
    u = w / h
    _, e0 = hammerstad_jensen(w, h, er)
    fn = f * h * 1e-6  # GHz·mm
    p1 = (
        0.27488
        + (0.6315 + 0.525 / (1.0 + 0.0157 * fn) ** 20) * u
        - 0.065683 * math.exp(-8.7513 * u)
    )
    p2 = 0.33622 * (1.0 - math.exp(-0.03442 * er))
    p3 = 0.0363 * math.exp(-4.6 * u) * (1.0 - math.exp(-((fn / 38.7) ** 4.97)))
    p4 = 1.0 + 2.751 * (1.0 - math.exp(-((er / 15.916) ** 8)))
    p = p1 * p2 * ((0.1844 + p3 * p4) * fn) ** 1.5763
    return er - (er - e0) / (1.0 + p)
