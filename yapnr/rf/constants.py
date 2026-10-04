"""Physical constants (SI) used by the RF solver.

The values are CODATA 2018; μ0 is the measured value, not 4π·1e-7 exactly, which is immaterial
at the accuracy of this solver but keeps c0 = 1/sqrt(ε0 μ0) consistent to rounding.
"""

from __future__ import annotations

import math

C0 = 299_792_458.0
"""Speed of light in vacuum (m/s)."""

MU0 = 1.25663706212e-6
"""Vacuum permeability (H/m)."""

EPS0 = 1.0 / (MU0 * C0 * C0)
"""Vacuum permittivity (F/m)."""

ETA0 = math.sqrt(MU0 / EPS0)
"""Impedance of free space (Ω)."""

SIGMA_CU = 5.8e7
"""Conductivity of annealed copper (S/m)."""
