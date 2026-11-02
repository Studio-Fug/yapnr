"""Pure complex-response prototype; NOT wired into yapnr/FDTD or an optimizer.

Frequency is the first batch dimension. Fields have [..., Jones_component, port]
as their last axes, all at one origin. h = analysis_jones^H E is a declared
algebra convention; physical receive/reciprocity sign and basis need EM tests.
"""

import torch


def checked_solve(a, b, max_condition=1e10):
    if not torch.isfinite(a).all() or not torch.isfinite(b).all():
        raise ValueError("Nonfinite response")
    condition = torch.linalg.cond(a).detach()
    if not torch.isfinite(condition).all() or (condition > max_condition).any():
        raise ValueError("Ill-conditioned response; no silent regularization")
    return torch.linalg.solve(a, b)


def right_solve(b, a):
    return checked_solve(a.transpose(-1, -2), b.transpose(-1, -2)).transpose(-1, -2)


def normalize_basis(incident, outgoing, fields):
    """S=B A^-1; E=F A^-1, including non-diagonal incident-wave bases."""
    s = right_solve(outgoing, incident)
    shape = fields.shape
    f = fields.reshape(shape[0], -1, shape[-1])
    return s, right_solve(f, incident).reshape(shape)


def load_network(s_ant, e_ant, network, chip_ports=2):
    """Six-port network for two upstream chip and four downstream antenna ports.

    network order [U,D]; compatible real reference impedances/directions assumed.
    Scalar-group differentiation includes both S and embedded-field dependence.
    """
    d = s_ant.shape[-1]
    if chip_ports != 2 or s_ant.shape[-2:] != (d, d):
        raise ValueError("Expected two chip ports and square antenna matrix")
    if network.shape[-2:] != (d + chip_ports, d + chip_ports) or e_ant.shape[-1] != d:
        raise ValueError("Wrong network dimensions; two plus four means six ports")
    uu = network[..., :chip_ports, :chip_ports]
    ud = network[..., :chip_ports, chip_ports:]
    du = network[..., chip_ports:, :chip_ports]
    dd = network[..., chip_ports:, chip_ports:]
    eye = torch.eye(d, dtype=s_ant.dtype, device=s_ant.device)
    transfer = checked_solve(eye - dd @ s_ant, du)
    s_chip = uu + ud @ s_ant @ transfer
    e_chip = torch.einsum("faqd,fdu->faqu", e_ant, transfer)
    return s_chip, e_chip, transfer


def project_jones(fields, analysis_jones):
    """analysis_jones shape [frequency,angle,2]; fields [frequency,angle,2,2]."""
    norm = (analysis_jones.conj() * analysis_jones).real.sum(-1)
    if not torch.allclose(norm, torch.ones_like(norm), atol=1e-12, rtol=1e-12):
        raise ValueError("Declare unit polarization states; do not normalize away mismatch")
    return torch.einsum("faq,faqu->fau", analysis_jones.conj(), fields)


def target_phase(freq_hz, directions, baseline_m, offset_rad, sign=1):
    if sign not in (-1, 1):
        raise ValueError("Explicit signed convention required")
    if not torch.allclose(
        directions.square().sum(-1), torch.ones_like(directions[:, 0]), atol=1e-12, rtol=1e-12
    ):
        raise ValueError("Directions must be unit vectors")
    if baseline_m.shape != (len(freq_hz), 3) or offset_rad.shape != (len(freq_hz),):
        raise ValueError("Fixed per-frequency baseline and direction-independent offset required")
    k = 2 * torch.pi * freq_hz / 299792458.0
    return (
        sign * k[:, None] * torch.einsum("fc,ac->fa", baseline_m, directions) + offset_rad[:, None]
    )


def pair_metrics(h, target, power_floor):
    """Fixed floors; invalid phases cannot disappear from the coverage denominator.

    Losses are diagnostic components, not final engineering weights/thresholds.
    A smooth phasor avoids zero-field division; the explicit floor violation must
    remain a hard constraint in the future adapter.
    """
    if power_floor <= 0 or h.shape[-1] != 2 or target.shape != h.shape[:-1]:
        raise ValueError("Positive floor and compatible pair target required")
    power = h.abs().square()
    valid = (power >= power_floor).all(-1)
    product = h[..., 1] * h[..., 0].conj()
    eps = power_floor * 1e-6
    unit = product / torch.sqrt(product.abs().square() + eps * eps)
    residual = unit * torch.exp(-1j * target)
    floor_violation = torch.relu((power_floor - power) / power_floor)
    return dict(
        phasor_loss=(residual - 1).abs().square().mean(),
        floor_loss=floor_violation.square().mean(),
        minimum_power=power.min(),
        valid=valid,
        coverage_fraction=valid.to(torch.float64).mean(),
        raw_wrapped_residual=torch.angle(residual),
    )


def differential_phase_rate(h, derivative_h, power_floor):
    """Im(dhB/hB - dhA/hA); derivative may be frequency or tangent direction.

    Rate values below signal floor are explicitly invalid. Unlike a wrapped
    phase loss, this local derivative distinguishes coincident/opposite axes.
    """
    if power_floor <= 0 or derivative_h.shape != h.shape or h.shape[-1] != 2:
        raise ValueError("Invalid pair derivative contract")
    power = h.abs().square()
    terms = (h.conj() * derivative_h).imag / torch.clamp(power, min=power_floor)
    valid = (power >= power_floor).all(-1)
    return terms[..., 1] - terms[..., 0], valid


def excess_pair_delay(h, spatial_target, frequencies_hz, power_floor):
    """Adjacent-frequency residual phase slope, evaluated separately per band.

    The caller supplies the fixed spatial target and declared calibration term.
    This function neither fits an angle-dependent correction nor unwraps aliases.
    A dense independent frequency check is still required: a wrapped step alone
    cannot reveal additional integer turns between samples. Signal validity is
    returned for every interval and must not become a candidate-dependent mask.
    """
    if h.shape[0] != len(frequencies_hz) or len(frequencies_hz) < 2:
        raise ValueError("At least two ordered samples in one declared band required")
    delta = frequencies_hz[1:] - frequencies_hz[:-1]
    if (delta <= 0).any() or not torch.isfinite(delta).all():
        raise ValueError("Strictly increasing finite frequencies required")
    metrics = pair_metrics(h, spatial_target, power_floor)
    product = h[..., 1] * h[..., 0].conj() * torch.exp(-1j * spatial_target)
    step = torch.angle(product[1:] * product[:-1].conj())
    valid = metrics["valid"][1:] & metrics["valid"][:-1]
    return -step / (2 * torch.pi * delta[:, None]), valid, step
