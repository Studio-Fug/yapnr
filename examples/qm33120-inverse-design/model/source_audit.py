"""Check native fitted spectra against independently merged requested spectra."""

import hashlib

import numpy as np

from yapnr.rf.adjoint import adjoint_sources
from yapnr.rf.fdtd.dtft import numerical_omega
from yapnr.rf.fdtd.sources import source_time


def digest(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def audit_sources(m, cotangents):
    p = m.p
    probes = p.port_probes + p.box_probes
    by = {q.name: q for q in probes}
    reqs = {}
    om = numerical_omega(p.omega, p.dt)
    for name, g in cotangents.items():
        if not np.any(g):
            continue
        q = by[name]
        v = p.grid.volume(q.comp).reshape(-1)[q.index]
        sign = 1 if q.comp.startswith("e") else -1
        reqs.setdefault(q.comp, []).append((q.index, sign * g / (1j * om[:, None] * v[None, :])))
    ss = adjoint_sources(p.sim, probes, cotangents, p.omega)
    reports = []
    for s in ss:
        idx = np.concatenate([x[0] for x in reqs[s.comp]])
        val = np.concatenate([x[1] for x in reqs[s.comp]], axis=1)
        uniq, inv = np.unique(idx, return_inverse=True)
        expect = np.zeros((len(om), len(uniq)), complex)
        np.add.at(expect.T, inv, val.T)
        np.testing.assert_array_equal(uniq, s.index)
        # Factor the finite DTFT; avoid an edges x 29598 time-history allocation.
        times = source_time(np.arange(s.fit.n_window + 1), p.dt, s.fit.magnetic)
        B = p.dt * np.exp(1j * np.outer(p.omega, times)) @ s.fit._basis.T
        realized = s.coef @ B.T
        err = float(np.max(abs(realized - expect.T)) / max(float(np.max(abs(expect))), 1e-300))
        if not np.isfinite(err) or err > 1e-10:
            raise ValueError("Native source-spectrum reconstruction failed")
        reports.append(
            dict(
                comp=s.comp,
                index_sha256=digest(s.index),
                coefficients_sha256=digest(s.coef),
                requested_sha256=digest(expect),
                n_window=s.fit.n_window,
                condition=s.fit.cond,
                relative_reconstruction_error=err,
                source_edges=len(s.index),
            )
        )
    if not reports:
        raise ValueError("Zero joint source; derivative is uninformative")
    return reports


def cotangent_hashes(joint, j):
    return {k: digest(v) for k, v in sorted(joint.cotangents[j].items())}


def forward_source(m, j):
    out = []
    for s in m.p.port_sources(j):
        out.append(
            dict(comp=s.comp, index_sha256=digest(s.index), amplitude_sha256=digest(s.amplitude))
        )
    return dict(
        kind="forward",
        excitation=j,
        sources=out,
        spectrum_sha256=digest(m.p._source_spectrum(m.p.omega)),
    )
