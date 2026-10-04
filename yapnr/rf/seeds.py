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

`seed: patch_edge` is the closed-form rectangle (W and L as above) fed at its edge, with no
inset and no tuning: an unmatched radiator (|S11| near 0 dB) of roughly the right size, from
which the optimizer has to create the match and centre the resonance.

`seed: star` joins every port with feed-width lines to the window's centre (a plain junction),
a lossless start for multi-port specs whose uniform start stays absorbing.

`seed: feeds` is every port's feed continued straight to the window's centre line, the lines
not joined: the start of the Wilkinson-type combiner, whose isolation resistor sits on the
centre line where the star's junction would short its pads on both sides (the optimizer must
then cut the junction on the input and the output side at once before the resistor does
anything, and the gradient does not lead there). From the feeds the input line ends on the
resistor's pads and the outputs must reach them.

`seed: stubs` (filter banks) is the star plus, on the arm of every output port, one open stub
per other channel, a quarter guide wavelength long at that channel's centre (Hammerstad's
open-end extension subtracted): the transmission zero that keeps that channel out of the port.
The stubs are placed by a search on the pixel grid (`stub_mask`): along the arm's straight
segment from the port inward, perpendicular to it, straight or with one bend, at least the
minimum space away from all other copper; a stub that fits nowhere is left out. Their positions
and lengths are a start, not a design: the optimizer moves, resizes or removes them like any
other copper. From the plain star the diplexer's branches only learned to roll off (low-pass and
high-pass, 9–12 dB of rejection): a stub only helps once it is long enough, so the gradient
from the junction does not lead there.

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
VALUES = {
    "patch": (0.9, 0.1),
    "patch_edge": (0.9, 0.1),
    "star": (0.7, 0.3),
    "feeds": (0.7, 0.3),
    "stubs": (0.7, 0.3),
}


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


def feeds_mask(problem) -> np.ndarray:
    """Every port's feed continued straight to the window's centre line across its axis, the
    lines not joined: the ports' own lines and nothing else (for a combiner with a lumped part
    on the centre line, which the star's junction would short)."""
    spec = problem.spec
    pitch = problem.pitch
    ni, nj = problem.design_shape
    x0, _, y0, _ = (v * 1e-3 for v in spec.design_region)
    m = np.zeros((ni, nj))
    for port in spec.ports:
        w = problem.widths[port.n]
        along, at0 = (ni, y0) if port.side in ("W", "E") else (nj, x0)
        t_lo = int(round((port.at_mm * 1e-3 - at0) / pitch - 0.5 * w))
        a = slice(0, along // 2) if port.side in ("W", "S") else slice((along + 1) // 2, along)
        if port.side in ("W", "E"):
            m[a, t_lo : t_lo + w] = 1.0
        else:
            m[t_lo : t_lo + w, a] = 1.0
    return m


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


def channel_plan(spec) -> dict:
    """Output port → (its pass band's centre, [the other channels' centres]) in Hz, from the
    spec's S(k, 1) ≥ requirements (excitation 1)."""
    passes = {}
    for r in spec.requirements:
        if r.quantity == "s" and r.bound == "min" and r.ports[1] == 1 and r.ports[0] != 1:
            b = spec.bands[r.band]
            passes[r.ports[0]] = 0.5 * (b.lo_ghz + b.hi_ghz) * 1e9
    return {k: (f, [g for j, g in passes.items() if j != k]) for k, f in passes.items()}


def _stub_cells(problem, freq: float, width_cells: int) -> int:
    """Pixels of an open stub a quarter guide wavelength long at `freq` (Kirschning–Jansen
    ε_eff, Hammerstad's open-end extension subtracted)."""
    from yapnr.rf.stackup import kirschning_jansen_eps_eff

    st = problem.spec.stackup.to_stackup()
    w = width_cells * problem.pitch
    eeff = kirschning_jansen_eps_eff(w, st.h, st.er, freq)
    u = w / st.h
    dl = 0.412 * st.h * (eeff + 0.3) * (u + 0.264) / ((eeff - 0.258) * (u + 0.8))
    length = C0 / (4.0 * freq * math.sqrt(eeff)) - dl
    return max(1, int(round(length / problem.pitch)))


def _stub_cells_at(i_rng, j_rng, d, length, bend):
    """Pixels of a stub leaving the attach rectangle (i_rng × j_rng) in direction d = (di, dj)
    (a unit step along x or y), `length` pixels long and as wide as the rectangle across d;
    with `bend` (±1) it turns by 90° half way (towards ±the other axis)."""
    di, dj = d
    if di:  # along x: the stub's width spans j_rng
        start = (i_rng[1] if di > 0 else i_rng[0] - 1, None)
        width = list(range(*j_rng))
    else:
        start = (None, j_rng[1] if dj > 0 else j_rng[0] - 1)
        width = list(range(*i_rng))
    run = length if bend is None else max(len(width), length // 2)
    cells = []
    for k in range(run):
        for c in width:
            cells.append((start[0] + di * k, c) if di else (c, start[1] + dj * k))
    if bend is not None and length > run:
        # The last `len(width)` rows of the run, continued sideways.
        last = run - 1
        lo_c, hi_c = min(width), max(width)
        side = hi_c + 1 if bend > 0 else lo_c - 1
        for k in range(length - run):
            c = side + bend * k
            for r in range(len(width)):
                along = last - r
                cells.append((start[0] + di * along, c) if di else (c, start[1] + dj * along))
    return cells


def stub_mask(problem) -> np.ndarray:
    """The star plus quarter-wave open stubs (see the module doc).

    For every output port and other channel (longest stub first), the candidate attach points
    are the stations of the port's arm in the star (its segment along the window's centre line
    from the junction, then its straight run to the port), the stub leaving perpendicular on
    either side, straight or with one bend. Candidates are tried in the order of how far their
    path length from the junction is from a quarter wave (or three quarters) at the stub's
    frequency, where the stub's short circuit appears at the junction as an open; the first
    that fits in the window, on void, at least the minimum space from all copper but its attach
    point, is placed.
    """
    spec = problem.spec
    m = star_mask(problem).astype(bool)
    ni, nj = m.shape
    pitch = problem.pitch
    space = max(1, int(math.ceil((spec.rules.min_space_mm or 0.0) * 1e-3 / pitch - 1e-6)))
    x0, _, y0, _ = (v * 1e-3 for v in spec.design_region)
    ic, jc = ni / 2.0, nj / 2.0
    plan = channel_plan(spec)
    st = spec.stackup.to_stackup()
    jobs = []
    for port in spec.ports:
        if port.n not in plan:
            continue
        w = problem.widths[port.n]
        for f in plan[port.n][1]:
            from yapnr.rf.stackup import kirschning_jansen_eps_eff

            eeff = kirschning_jansen_eps_eff(w * pitch, st.h, st.er, f)
            quarter = C0 / (4.0 * f * math.sqrt(eeff)) / pitch  # pixels
            jobs.append((_stub_cells(problem, f, w), quarter, port, w))
    jobs.sort(key=lambda j: -j[0])

    def fits(cells, near):
        for i, j in cells:
            if not (0 <= i < ni and 0 <= j < nj) or m[i, j]:
                return False
        other = m & ~near
        for i, j in cells:
            if other[
                max(0, i - space) : min(ni, i + space + 1),
                max(0, j - space) : min(nj, j + space + 1),
            ].any():
                return False
        return True

    for length, quarter, port, w in jobs:
        horizontal = port.side in ("W", "E")
        lo_axis = y0 if horizontal else x0
        t_lo = int(round((port.at_mm * 1e-3 - lo_axis) / pitch - 0.5 * w))
        stations = []  # (path distance in pixels, i range, j range, directions)
        if horizontal:
            c_lo = int(round(ic - 0.5 * w))
            # Along the centre column, from the junction to the port's row.
            step = 1 if t_lo + 0.5 * w > jc else -1
            for b in range(int(round(jc - 0.5 * w)), t_lo + step, step):
                d = abs(b + 0.5 * w - jc)
                stations.append((d, (c_lo, c_lo + w), (b, b + w), ((-1, 0), (1, 0))))
            # Along the port's row, from the centre column to the port.
            run = range(c_lo, ni - w + 1) if port.side == "E" else range(c_lo, -1, -1)
            for a in run:
                d = abs(t_lo + 0.5 * w - jc) + abs(a + 0.5 * w - ic)
                stations.append((d, (a, a + w), (t_lo, t_lo + w), ((0, -1), (0, 1))))
        else:
            c_lo = int(round(jc - 0.5 * w))
            step = 1 if t_lo + 0.5 * w > ic else -1
            for b in range(int(round(ic - 0.5 * w)), t_lo + step, step):
                d = abs(b + 0.5 * w - ic)
                stations.append((d, (b, b + w), (c_lo, c_lo + w), ((0, -1), (0, 1))))
            run = range(c_lo, nj - w + 1) if port.side == "N" else range(c_lo, -1, -1)
            for a in run:
                d = abs(t_lo + 0.5 * w - ic) + abs(a + 0.5 * w - jc)
                stations.append((d, (t_lo, t_lo + w), (a, a + w), ((-1, 0), (1, 0))))
        targets = (quarter, 3.0 * quarter)
        stations.sort(key=lambda s_: min(abs(s_[0] - t) for t in targets))
        for _, i_rng, j_rng, dirs in stations:
            if not m[i_rng[0] : i_rng[1], j_rng[0] : j_rng[1]].all():
                continue  # not on the arm
            near = np.zeros_like(m)
            near[
                max(0, i_rng[0] - space) : i_rng[1] + space,
                max(0, j_rng[0] - space) : j_rng[1] + space,
            ] = True
            near &= m
            done = False
            for d in dirs:
                for bend in (None, -1, 1):
                    cells = _stub_cells_at(i_rng, j_rng, d, length, bend)
                    if fits(cells, near):
                        for c in cells:
                            m[c] = True
                        done = True
                        break
                if done:
                    break
            if done:
                break
    return m.astype(np.float64)


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
    elif seed == "patch_edge":
        mask = patch_mask(problem, 0, 0, -(10**6))
    elif seed == "star":
        mask = star_mask(problem)
    elif seed == "feeds":
        mask = feeds_mask(problem)
    elif seed == "stubs":
        mask = stub_mask(problem)
    else:
        raise ValueError(f"unknown seed {seed!r}")
    copper, void = VALUES[seed]
    rho = void + (copper - void) * mask
    return problem.param.grid.restrict(rho)
