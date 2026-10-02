"""CPML reflection (design §4.4, §12).

An Ez dipole in the substrate above the ground radiates towards the +x CPML. The same problem
on a domain extended by 80 cells in +x is the reference: until waves reflected from its far
boundary return, the two runs differ only by what the +x CPML of the small domain reflects.
Probes sit 3 cells in front of that CPML, in the substrate and in the air.

Measured (S1-like stackup, 0.3 mm cells, 10-cell CPML, pulse content 2-25 GHz): -66.6 dB in
the substrate, -79.6 dB in the air; the assertion keeps a 6 dB margin.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import PulseSource
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Grid, PMLCells, graded_axis, substrate_z_axis
from yapnr.rf.stackup import Stackup

PITCH = 0.3e-3
N_PML = 10


class DiffGaussian:
    """−((t − t0)/τ) exp(−((t − t0)/τ)²): no DC, content up to about 1/(τ) rad/s."""

    def __init__(self, tau):
        self.tau = tau
        self.t0 = 4.5 * tau
        self.t_end = 2 * self.t0

    def __call__(self, t):
        u = (np.asarray(t) - self.t0) / self.tau
        return -u * np.exp(-u * u)


def domain(extra_cells: int):
    st = Stackup(3.55, 0.0, 0.813e-3, 10e9)
    nx = 30 + extra_cells
    x = graded_axis(
        0, nx * PITCH, PITCH, 0, nx * PITCH, max_cell=PITCH, n_pml_lo=N_PML, n_pml_hi=N_PML
    )
    y = graded_axis(
        0, 30 * PITCH, PITCH, 0, 30 * PITCH, max_cell=PITCH, n_pml_lo=N_PML, n_pml_hi=N_PML
    )
    z, kc = substrate_z_axis(st.h, 4, 15 * PITCH, dz_max=PITCH, ratio=1.0, n_pml=N_PML)
    grid = Grid(x, y, z, kc, PMLCells(N_PML, N_PML, N_PML, N_PML, N_PML))
    return grid, Structure(grid, st)


class CPMLReflectionTest(unittest.TestCase):
    def run_case(self, extra):
        grid, s = domain(extra)
        dt = 0.95 * grid.courant_dt()
        sim = Simulation(grid, s, dt=dt, backend="torch", dtype=np.float64)
        i_src = N_PML + 15
        j_c = N_PML + 15
        src = PulseSource(
            "ez", grid.flat_index("ez", i_src, j_c, [0, 1, 2, 3]), 1.0, DiffGaussian(8e-12), dt
        )
        i_probe = N_PML + 27  # 3 cells in front of the small domain's +x CPML
        probes = [(i_probe, j_c, 1), (i_probe, j_c, 6), (i_probe, j_c + 8, 2)]
        trace = []

        def cb(sim_, n):
            ez = sim_.f["ez"]
            trace.append([float(ez[p]) for p in probes])

        # The far boundary of the large domain is 80 + 3 cells past the probes: its
        # reflection returns after ≥ 2·80 cells of travel.
        window = int(2 * 80 * PITCH / (3e8 * dt))
        sim.run([src], [], [1e10], StopRule(max_steps=window), callback=cb)
        return np.array(trace)

    def test_reflection_level(self):
        small = self.run_case(0)
        large = self.run_case(80)
        err = np.abs(small - large).max(axis=0) / np.abs(large).max(axis=0)
        level = 20 * math.log10(err.max())
        self.assertLess(level, -60.0, f"CPML reflection {level:.1f} dB")


if __name__ == "__main__":
    unittest.main()
