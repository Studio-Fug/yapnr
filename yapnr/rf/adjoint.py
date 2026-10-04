"""The adjoint run and the gradient recombination (design §6).

Let F be a real function of probe DTFTs q (complex (M, P) arrays) whose part F_m depends only
on the values at ω_m. With the Wirtinger derivatives g = ∂F_m/∂q at ω_m (conjugates held
fixed, dF = 2 Re Σ g dq), the gradient with respect to the conductivity of edge e is

    ∂F_m/∂σ_e = 2 Re[ iΩ_m c_m V_e Ê^adj_e(ω_m) Ê_e(ω_m) ]

where Ê is the forward DTFT, Ê^adj the DTFT of an ordinary FDTD run driven by

    Ĵ^adj_e(ω_m) =  g_E,e / (iΩ_m V_e)     on E probe edges,
    K̂^adj_h(ω_m) = −g_H,h / (iΩ_m V_h)     on H probe edges,

Ω_m = (2/Δt) sin(ω_m Δt/2), c_m = cos(ω_m Δt/2) and V the dual volumes. This is exact for the
discrete scheme: the volume-weighted frequency-domain operator is complex symmetric (also with
the CPML, whose stretch depends only on the position along each axis), so the transposed solve
is a forward solve with the sources divided by the volumes. Sources are realized by the
Nuttall-basis fit (`sources.NuttallFit`), exact at every ω_m, so one adjoint run gives the
gradient of every F_m.

Probes must lie outside the CPML. For the copper sheet σ_e = G_e/Δz_d(k_c), so
∂F/∂G_e = ∂F/∂σ_e / Δz_d; `materials.edges_to_pixels` restricts edge gradients to pixels.
In general a sheet edge enters the discrete equations through a coefficient a_e(ω) (c_ω σ_e for
a resistive sheet, Σ w c_ω Y_d/Δz for an inductive one, `materials`), and

    ∂F_m/∂p = 2 Re[ K_e(ω_m) ∂(Δz a_e)/∂p ],   K_e = iΩ_m V_e Ê^adj_e Ê_e / Δz_d

for any parameter p of the sheet; `Gradient.pixel_kernel` restricts K to pixels (complex).

A typical iteration (one excitation, one objective group):

    sim.structure.set_pixels(dom.pixels(sheet_conductance(rho_bar, g_min, g_max)))
    sim.update_materials()
    fwd = sim.run(port.mode_sources(pulse, dt), probes, omega, stop,
                  decimation=dtft.decimation(pulse.f_top, dt))
    f, g = wirtinger(objective, fwd.dft, names)          # f: (M,), one value per ω_m
    grad = gradient(sim, fwd, probes, g, dom.design_probes(), stop, decimation="auto")
    df_drho_bar = dom.window_pixels(grad.pixels(dom.grid)) * sheet_conductance_derivative(...)

`probes` must include the design probes, and `objective` maps the probe DTFTs (torch
complex128) to the (M,) per-frequency values. With the copper-edge correction (`edges`) the
design also sets ε and μ next to the copper's edges; `edge_probes` (`edges.design_probes`, in
the forward run too) give their part of the gradient, `Gradient.edge_pixels`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from yapnr.rf.fdtd.dtft import conductance_factor
from yapnr.rf.fdtd.dtft import decimation as dtft_decimation
from yapnr.rf.fdtd.dtft import numerical_omega
from yapnr.rf.fdtd.engine import RunResult, Simulation
from yapnr.rf.fdtd.monitors import Probe
from yapnr.rf.fdtd.sources import NuttallFit, SpectralSource
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import edges_to_pixels
from yapnr.rf.numerics import make_complex


def wirtinger(fn, dft: dict, names):
    """Evaluate F = fn(dft) (a real (M,) tensor) with torch autograd.

    Returns (F as numpy (M,), {name: ∂(Σ_m F_m)/∂q as complex (M, P)}). Torch returns
    grad = 2 conj(∂F/∂q) for a real F, so ∂F/∂q = conj(grad)/2. When F_m depends only on the
    values at ω_m, row m of each gradient is ∂F_m/∂q(ω_m).
    """
    import torch

    leaves = {}
    inputs = dict(dft)
    for n in names:
        t = torch.as_tensor(np.asarray(dft[n]), dtype=torch.complex128).clone()
        t.requires_grad_(True)
        leaves[n] = t
        inputs[n] = t
    for n, v in dft.items():
        if n not in leaves:
            inputs[n] = torch.as_tensor(np.asarray(v), dtype=torch.complex128)
    f = fn(inputs)
    f.sum().backward()
    grads = {}
    for n, t in leaves.items():
        g = t.grad
        grads[n] = np.zeros(t.shape, complex) if g is None else np.conj(g.numpy()) / 2.0
    return f.detach().numpy(), grads


def adjoint_sources(sim: Simulation, probes: list[Probe], grads: dict, omega) -> list:
    """Nuttall-fitted J and K sources realizing the adjoint spectra of `grads` (§6.2, §6.5)."""
    omega = np.asarray(omega, dtype=np.float64)
    big_omega = numerical_omega(omega, sim.dt)
    by_comp: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    pmap = {p.name: p for p in probes}
    for name, g in grads.items():
        p = pmap[name]
        g = np.asarray(g, dtype=np.complex128)
        if not np.any(g):
            continue
        vol = sim.grid.volume(p.comp).reshape(-1)[p.index]
        sign = 1.0 if p.comp[0] == "e" else -1.0
        # g / (iΩV) = −i g / (ΩV), in real arithmetic (see numerics).
        scale = sign / (big_omega[:, None] * vol[None, :])
        req = make_complex(g.imag * scale, -g.real * scale)  # (M, P)
        by_comp.setdefault(p.comp, []).append((p.index, req))
    out = []
    fits: dict[bool, NuttallFit] = {}
    for comp, parts in by_comp.items():
        idx = np.concatenate([i for i, _ in parts])
        req = np.concatenate([r for _, r in parts], axis=1)
        uniq, inv = np.unique(idx, return_inverse=True)
        merged = np.zeros((omega.size, uniq.size), dtype=np.complex128)
        np.add.at(merged.T, inv, req.T)
        magnetic = comp[0] == "h"
        if magnetic not in fits:
            fits[magnetic] = NuttallFit.build(omega, sim.dt, magnetic)
        coef = fits[magnetic].coefficients(merged.T)
        out.append(SpectralSource(comp, uniq, coef, fits[magnetic]))
    return out


@dataclass
class Gradient:
    """∂F_m/∂σ and ∂F_m/∂G of the design-plane edges, per objective frequency."""

    omega: np.ndarray
    d_sigma: dict  # probe name → real (M, P)
    d_gx: np.ndarray  # (M, Nx, Ny+1) ∂F/∂G on copper-plane Ex edges (zero off the probes)
    d_gy: np.ndarray  # (M, Nx+1, Ny)
    adjoint: RunResult
    k_gx: tuple = ()  # (Re, Im) of K on copper-plane Ex edges, (M, Nx, Ny+1) each
    k_gy: tuple = ()  # (Re, Im) on Ey edges, (M, Nx+1, Ny) each
    # ∂F/∂(edge-correction factor) per component (`edges.kernels`), when the correction is on.
    edge: dict | None = None

    def edge_pixels(self, structure) -> np.ndarray:
        """∂F_m/∂c_p (M, Nx, Ny) through the copper-edge correction's factors (`edges`), c the
        pixel copper fraction of the plane; zero without the correction."""
        from yapnr.rf.edges import vjp

        if not self.edge:
            return 0.0
        return vjp(structure.copper, structure.edge_constants(), self.edge)

    def pixels(self, grid) -> np.ndarray:
        """∂F_m/∂G_p for every pixel of the copper plane, (M, Nx, Ny) (resistive sheet)."""
        return edges_to_pixels(grid, self.d_gx, self.d_gy)

    def pixel_kernel(self, grid) -> tuple[np.ndarray, np.ndarray]:
        """(Re, Im) of K restricted to the pixels of the copper plane, (M, Nx, Ny) each: for an
        inductive sheet ∂F_m/∂p = 2 Re[K_p c_ω ∂Y_d/∂p] (`materials.branch_admittance_derivative`).
        """
        return (
            edges_to_pixels(grid, self.k_gx[0], self.k_gy[0]),
            edges_to_pixels(grid, self.k_gx[1], self.k_gy[1]),
        )


def gradient(
    sim: Simulation,
    forward: RunResult,
    probes: list[Probe],
    grads: dict,
    design_probes: list[Probe],
    stop: StopRule,
    *,
    decimation: int | str = 1,
    edge_probes: list[Probe] = (),
) -> Gradient:
    """Run the adjoint for Wirtinger gradients `grads` (name → (M, P)) of the forward probes
    and recombine onto the design-plane edges (which `forward` must have recorded).

    `decimation="auto"` accumulates the adjoint DTFT every d steps, d from the adjoint
    spectrum's top frequency (main lobe; the Nuttall sidelobes alias at about −90 dB). Use 1
    for exact gradients (tests)."""
    omega = forward.omega
    sources = adjoint_sources(sim, probes, grads, omega)
    if decimation == "auto":
        f_top = max((s.fit.f_top for s in sources), default=float(omega.max()) / (2 * np.pi))
        decimation = dtft_decimation(f_top, sim.dt)
    adj = sim.run(
        sources, list(design_probes) + list(edge_probes), omega, stop, decimation=decimation
    )
    big_omega = numerical_omega(omega, sim.dt)[:, None]
    c = conductance_factor(omega, sim.dt)[:, None]
    grid = sim.grid
    nx, ny, _ = grid.n
    kc = grid.k_c
    dz = float(grid.z.dual[kc])
    d_sigma = {}
    d_gx = np.zeros((omega.size, nx, ny + 1))
    d_gy = np.zeros((omega.size, nx + 1, ny))
    k_gx = (np.zeros_like(d_gx), np.zeros_like(d_gx))
    k_gy = (np.zeros_like(d_gy), np.zeros_like(d_gy))
    for p in design_probes:
        vol = grid.volume(p.comp).reshape(-1)[p.index][None, :]
        # K = iΩ V Ê^adj Ê / Δz in real arithmetic (see numerics):
        # Re K = −Ω V Im(Ê^adj Ê)/Δz, Im K = Ω V Re(Ê^adj Ê)/Δz; ∂F/∂σ = 2 c Re(K) Δz.
        ea, ef = adj.dft[p.name], forward.dft[p.name]
        prod_im = ea.real * ef.imag + ea.imag * ef.real
        prod_re = ea.real * ef.real - ea.imag * ef.imag
        ds = -2.0 * big_omega * c * vol * prod_im
        d_sigma[p.name] = ds
        if p.comp in ("ex", "ey"):
            i, j, k = grid.unravel(p.comp, p.index)
            if np.any(k != kc):
                continue
            target = d_gx if p.comp == "ex" else d_gy
            target[:, i, j] += ds / dz
            kr, ki = k_gx if p.comp == "ex" else k_gy
            kr[:, i, j] += -big_omega * vol * prod_im / dz
            ki[:, i, j] += big_omega * vol * prod_re / dz
    edge = None
    if edge_probes:
        from yapnr.rf.edges import kernels

        edge = kernels(sim.structure, edge_probes, forward.dft, adj.dft, omega, sim.dt)
    return Gradient(
        omega=omega,
        d_sigma=d_sigma,
        d_gx=d_gx,
        d_gy=d_gy,
        adjoint=adj,
        k_gx=k_gx,
        k_gy=k_gy,
        edge=edge,
    )
