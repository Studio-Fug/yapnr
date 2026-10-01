"""Monitors: DTFT probes on edge sets, Poynting-flux boxes, dissipation (design §5.6, §5.7).

A `Probe` records the DTFT of one field component on a set of edges (flat indices) at every
objective frequency; a run returns the complex array (M, P) per probe name. Every derived
quantity (port voltage and current, flux, dissipation, design-plane fields) is a function of
probe values, so its adjoint source is the Wirtinger gradient with respect to them.

Functions here accept numpy arrays or torch tensors (complex128) for the probe values, so the
objective layer can differentiate them with autograd.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.fdtd.dtft import conductance_factor
from yapnr.rf.mesh import E_COMPONENTS, Grid


@dataclass(frozen=True)
class Probe:
    """DTFT of component `comp` on edges `index` (unique flat indices)."""

    name: str
    comp: str
    index: np.ndarray

    def __post_init__(self) -> None:
        idx = np.asarray(self.index, dtype=np.int64).ravel()
        if np.unique(idx).size != idx.size:
            raise ValueError(f"probe {self.name}: duplicate edges")
        object.__setattr__(self, "index", idx)


def box_indices(grid: Grid, comp: str, ranges) -> np.ndarray:
    """Flat indices of the samples of `comp` with array indices in the given per-axis ranges
    (each a (start, stop) pair, stop exclusive)."""
    axes = [np.arange(lo, hi) for lo, hi in ranges]
    ii, jj, kk = np.meshgrid(*axes, indexing="ij")
    return grid.flat_index(comp, ii, jj, kk)


def design_plane_probes(grid: Grid, i0: int, i1: int, j0: int, j1: int, prefix="design"):
    """Probes on every copper-plane edge whose conductance depends on pixels [i0,i1)×[j0,j1).

    Ex edges: cells i0..i1−1, nodes j0..j1; Ey edges: nodes i0..i1, cells j0..j1−1.
    """
    kc = grid.k_c
    ex = box_indices(grid, "ex", [(i0, i1), (j0, j1 + 1), (kc, kc + 1)])
    ey = box_indices(grid, "ey", [(i0, i1 + 1), (j0, j1), (kc, kc + 1)])
    return [Probe(f"{prefix}_ex", "ex", ex), Probe(f"{prefix}_ey", "ey", ey)]


def _to_like(w: np.ndarray, x):
    """`w` as the array type of `x` (torch tensor or numpy array)."""
    if isinstance(x, np.ndarray):
        return w
    import torch

    return torch.as_tensor(w, dtype=torch.float64)


def _clipped_dual(primary: np.ndarray, dual: np.ndarray, a0: int, a1: int) -> np.ndarray:
    """Dual lengths of nodes a0..a1 clipped to [x_a0, x_a1] (half cells at both ends)."""
    d = dual[a0 : a1 + 1].copy()
    d[0] = 0.5 * primary[a0]
    d[-1] = 0.5 * primary[a1 - 1]
    return d


@dataclass
class FluxFace:
    """One face of a flux box: the plane at node `f` along axis `n`, outward sign ±1."""

    axis: int
    node: int
    sign: int
    probes: list = field(default_factory=list)  # [E_t1, E_t2, H_t2 below, H_t2 above, H_t1 ...]
    weights: list = field(default_factory=list)  # area weights of E_t1 and E_t2 samples


class FluxBox:
    """Poynting flux through faces of an axis-aligned box of grid nodes (design §5.6).

    On each face the tangential E samples pair with the tangential H averaged over the planes on
    either side (normal averaging); E samples on the face's rim get half their dual area
    (trapezoid rule), which makes the flux through a closed box the exact discrete power balance
    of the scheme's frequency-domain equations. `windows` are physical boxes
    ((x0, x1), (y0, y1), (z0, z1)) whose samples are left out (for example where a feed crosses
    the box).
    """

    def __init__(self, grid: Grid, name: str, node_box, faces=None, windows=()):
        self.grid = grid
        self.name = name
        (i0, i1), (j0, j1), (k0, k1) = node_box
        self.node_box = ((i0, i1), (j0, j1), (k0, k1))
        faces = faces or ("x-", "x+", "y-", "y+", "z-", "z+")
        self.faces: list[FluxFace] = []
        for tag in faces:
            n = "xyz".index(tag[0])
            sign = 1 if tag[1] == "+" else -1
            node = self.node_box[n][1 if sign > 0 else 0]
            self.faces.append(self._face(n, node, sign, tag, windows))

    def _face(self, n: int, node: int, sign: int, tag: str, windows) -> FluxFace:
        grid = self.grid
        t1, t2 = (n + 1) % 3, (n + 2) % 3
        face = FluxFace(axis=n, node=node, sign=sign)
        (a0, a1), (b0, b1) = self.node_box[t1], self.node_box[t2]
        ax1, ax2 = grid.axis(t1), grid.axis(t2)
        for e_axis, h_axis in ((t1, t2), (t2, t1)):
            ecomp = "e" + "xyz"[e_axis]
            hcomp = "h" + "xyz"[h_axis]
            ranges = [None, None, None]
            ranges[n] = (node, node + 1)
            if e_axis == t1:
                ranges[t1] = (a0, a1)
                ranges[t2] = (b0, b1 + 1)
                w = np.outer(ax1.primary[a0:a1], _clipped_dual(ax2.primary, ax2.dual, b0, b1))
            else:
                ranges[t1] = (a0, a1 + 1)
                ranges[t2] = (b0, b1)
                w = np.outer(_clipped_dual(ax1.primary, ax1.dual, a0, a1), ax2.primary[b0:b1])
            # (t1, t2) ordering of w → array (x, y, z) ordering of the samples.
            order = sorted([t1, t2])
            if order != [t1, t2]:
                w = w.T
            w = w.reshape(-1)
            eidx = box_indices(grid, ecomp, ranges)
            pos = [grid.positions(ecomp, a) for a in range(3)]
            ii, jj, kk = grid.unravel(ecomp, eidx)
            coords = (pos[0][ii], pos[1][jj], pos[2][kk])
            for (x0, x1), (y0, y1), (z0, z1) in windows:
                inside = (
                    (coords[0] >= x0)
                    & (coords[0] <= x1)
                    & (coords[1] >= y0)
                    & (coords[1] <= y1)
                    & (coords[2] >= z0)
                    & (coords[2] <= z1)
                )
                w = np.where(inside, 0.0, w)
            hr_lo = list(ranges)
            hr_hi = list(ranges)
            hr_lo[n] = (node - 1, node)
            hr_hi[n] = (node, node + 1)
            face.probes.append(Probe(f"{self.name}_{tag}_{ecomp}", ecomp, eidx))
            face.probes.append(
                Probe(f"{self.name}_{tag}_{hcomp}_lo", hcomp, box_indices(grid, hcomp, hr_lo))
            )
            face.probes.append(
                Probe(f"{self.name}_{tag}_{hcomp}_hi", hcomp, box_indices(grid, hcomp, hr_hi))
            )
            face.weights.append(w)
        return face

    @property
    def probes(self) -> list[Probe]:
        return [p for f in self.faces for p in f.probes]

    def power(self, dft) -> object:
        """Outward time-averaged power ½ Re ∮ (Ê × Ĥ*)·n̂ dA at every frequency (M,).

        `dft` maps probe names to (M, P) arrays or tensors.
        """
        total = 0.0
        for face in self.faces:
            for pair, w in enumerate(face.weights):
                e, h_lo, h_hi = (dft[p.name] for p in face.probes[3 * pair : 3 * pair + 3])
                h = 0.5 * (h_lo + h_hi)
                # pair 0: E_t1 × H_t2 (+); pair 1: E_t2 × H_t1 (−).
                s = 1.0 if pair == 0 else -1.0
                total = total + face.sign * s * 0.5 * ((e * h.conj()).real @ _to_like(w, e))
        return total


def region_probes(grid: Grid, name: str, node_box) -> list[Probe]:
    """Probes on every E edge inside a box of nodes (edges on its faces included)."""
    (i0, i1), (j0, j1), (k0, k1) = node_box
    out = []
    for comp in E_COMPONENTS:
        ranges = []
        for a, (lo, hi) in enumerate(((i0, i1), (j0, j1), (k0, k1))):
            if "xyz".index(comp[1]) == a:
                ranges.append((lo, hi))
            else:
                ranges.append((lo, hi + 1))
        out.append(Probe(f"{name}_{comp}", comp, box_indices(grid, comp, ranges)))
    return out


def dissipated_power(structure, probes, dft, omega, dt: float, weights=None) -> np.ndarray:
    """P_diss = ½ Σ_e c_ω σ_e |Ê_e|² V_e over the probes' edges (design §5.7).

    `weights` (optional, per probe) scale edges, e.g. ½ on the faces of a closed box.
    """
    grid = structure.grid
    c = conductance_factor(omega, dt)
    total = np.zeros(np.asarray(omega).size)
    for n, p in enumerate(probes):
        sig = structure.sigma(p.comp).reshape(-1)[p.index]
        vol = grid.volume(p.comp).reshape(-1)[p.index]
        w = 1.0 if weights is None else weights[n]
        e = np.asarray(dft[p.name])
        total += 0.5 * c * (np.abs(e) ** 2 @ (sig * vol * w))
    return total
