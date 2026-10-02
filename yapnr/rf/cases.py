"""The end-to-end cases (design §11) as spec presets, with their pass criteria.

The cases, each at two scales:

- `divider`: a 3-port equal-split power divider/combiner, 8.5–11.5 GHz (design §11.2);
- `wilkinson`: the same with matched, isolated outputs and a 100 Ω isolation resistor, 9–11 GHz
  (design §11.2, a2);
- `antenna`: a 1-port microstrip-fed antenna over ground, 9.85–10.15 GHz, matched and
  radiating, grown by the optimization from the feed line alone (design §11.3, §22);
  `antenna_patch_reference` is the closed-form patch it is compared with (not a case);
- `diplexer`: a 3-port two-channel filter bank, channels 7.6–8.4 and 11.6–12.4 GHz (§11.4).

`scale="full"` is the design's case (minutes to an hour at 4 threads); `scale="smoke"` is the
same topology on a tiny grid with a few iterations, for CI. The criteria are checked on the
binary design re-simulated from the exported footprint, on the optimization grid ("coarse")
and on a grid twice as fine in-plane and 1.5 times in the substrate ("fine", `validate`).

    python -m yapnr.rf.cases run divider --out runs/divider          # optimize, export, validate
    python -m yapnr.rf.cases run divider --out runs/divider --smoke
    python -m yapnr.rf.cases validate divider --out runs/divider     # only re-validate

Substrates: S1 is εr 3.55, tan δ 0.0027, h 0.813 mm (a Rogers 4003C-like laminate); S2 is the
same material 1.524 mm thick (antennas). The copper is a sheet with the surface resistance of
smooth copper at 10 GHz over a solid ground on the next layer.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass, replace

import numpy as np

from yapnr.rf.spec import (
    Band,
    GridSpec,
    Lumped,
    OptimizerSpec,
    Port,
    RadiatedFraction,
    RadiationBox,
    Rules,
    S,
    SolverSpec,
    Spec,
    StackupSpec,
)

S1 = StackupSpec(er=3.55, tan_delta=0.0027, h_mm=0.813, f_ref_ghz=10.0)
S2 = StackupSpec(er=3.55, tan_delta=0.0027, h_mm=1.524, f_ref_ghz=10.0)
SCALES = ("full", "smoke")

# Tiny grids for the smoke variants: a 0.6 mm (S1) or 0.8 mm (S2) pitch, two substrate cells,
# thin CPML and short feeds (about 1e4 to 3e4 cells).
_SMOKE_GRID = dict(
    substrate_cells=2,
    meas_cells=2,
    src_cells=5,
    pml_gap=2,
    core_cells=1,
    pml_cells=6,
    pml_top_cells=6,
)
# Starting points (docs/rf-inverse-design.md, "Starting points and repairs"). With copper the
# paper's uniform 0.5 is a 377 Ω/sq absorber over the whole window; from it the divider grew
# into one radiating plate (t rose from 1.1 to 2.5 at β = 32).
#
# - The divider starts from a uniform x = 0.3: ρ̄ ≈ 0.04 at β = 8, an almost transparent sheet
#   (about 3.3 kΩ/sq) still on the steep part of the projection.
# - The diplexer starts from a plain junction of its ports (`seed: star`): from x = 0.3 its
#   transmissions stayed below −30 dB for 15 iterations (the window absorbed), and at iteration
#   30 it was a radiating copper mass at t = 8.5.
# - The antenna starts from the feed line alone (`seed: star`: the port's line continued to the
#   window's centre); round 2's attempts (uniform, gray with the radiation objective, frequency
#   continuation, reactive sheet) are in docs/decisions.md. Round 1 started it from the
#   closed-form patch, which the optimizer did not change (now `antenna_patch_reference`).
INIT = 0.3
_SMOKE_OPT = OptimizerSpec(betas=(8.0, 32.0), iterations_per_beta=2, min_iterations=2, init=INIT)
_SMOKE_SOLVER = SolverSpec(backend="torch", dtype="float32", sweep_points=21)


def _check_scale(scale: str) -> None:
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {SCALES}")


def divider(scale: str = "full") -> Spec:
    """(a) Equal-split 3-port divider: |S11| ≤ −20 dB, |S21|, |S31| ≥ −3.4 dB over 8.5–11.5
    GHz, mirror symmetric about y = 0 (design §11.2).

    The design's transmission target was −3.28 dB. With the de-embedding corrected (Re k only)
    the port extraction reads transmissions 0.1–0.2 dB low at 8–12 GHz (the guide's
    "Accuracy"), so −3.28 dB left no room above the ideal −3.01 dB split plus the line loss: a
    run aiming at it oscillated (t up to 13) and its best binary design reached −3.46 dB. The
    target is −3.4 dB; the criteria (−3.45 and −3.6 dB) are unchanged."""
    _check_scale(scale)
    band = {"pass": Band(8.5, 11.5, 7)}
    reqs = (
        S(1, 1).at_most_db(-20, band="pass"),
        S(2, 1).at_least_db(-3.4, band="pass"),
        S(3, 1).at_least_db(-3.4, band="pass"),
    )
    if scale == "smoke":
        return Spec(
            name="divider-smoke",
            stackup=S1,
            grid=GridSpec(pitch_mm=0.6, margin_mm=1.8, air_mm=3.0, f_max_ghz=14.0, **_SMOKE_GRID),
            design_region=(0.0, 3.6, -3.3, 3.3),
            symmetry="mirror_y",
            rules=Rules(1.2, 1.2),
            ports=(Port(1, "W", 0.0, 3), Port(2, "E", 1.8, 3), Port(3, "E", -1.8, 3)),
            bands={"pass": Band(8.5, 11.5, 3)},
            requirements=reqs,
            optimizer=_SMOKE_OPT,
            solver=_SMOKE_SOLVER,
        )
    return Spec(
        name="divider-x10",
        stackup=S1,
        grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
        design_region=(0.0, 9.6, -6.0, 6.0),
        symmetry="mirror_y",
        rules=Rules(0.6, 0.6),
        ports=(Port(1, "W", 0.0), Port(2, "E", 4.2), Port(3, "E", -4.2)),
        bands=band,
        requirements=reqs,
        # Robust against the finer grids: on them the copper acts smaller (the zero-thickness
        # edge, guide "Accuracy") and a run without this lost 1.8 dB of |S11| per refinement
        # (−17.5, −15.7, −13.9 dB at 0.3, 0.15, 0.1 mm), so the epigraph also holds the eroded
        # design (projection threshold 0.55).
        optimizer=OptimizerSpec(
            iterations_per_beta=30,
            budget_min=75,
            init=INIT,
            eta_variants=(0.55,),
            move_late=0.05,
        ),
    )


def wilkinson(scale: str = "full") -> Spec:
    """(a2) A Wilkinson-type combiner/divider: the equal split of `divider` plus matched and
    isolated outputs (|S22| = |S33| and |S32| ≤ −20 dB), with a 100 Ω isolation resistor (an
    0402-sized SMD part across a 0.6 mm gap on the symmetry line) as a fixed lumped element;
    the copper around it is free (design §11.2, variant a2)."""
    _check_scale(scale)
    band = {"pass": Band(9.0, 11.0, 5)}
    reqs = (
        S(1, 1).at_most_db(-20, band="pass"),
        S(2, 1).at_least_db(-3.4, band="pass"),
        S(3, 1).at_least_db(-3.4, band="pass"),
        S(2, 2).at_most_db(-20, band="pass"),
        S(3, 2).at_most_db(-20, band="pass"),
    )
    if scale == "smoke":
        return Spec(
            name="wilkinson-smoke",
            stackup=S1,
            grid=GridSpec(pitch_mm=0.6, margin_mm=1.8, air_mm=3.0, f_max_ghz=14.0, **_SMOKE_GRID),
            design_region=(0.0, 4.8, -3.3, 3.3),
            symmetry="mirror_y",
            rules=Rules(1.2, 1.2),
            ports=(Port(1, "W", 0.0, 3), Port(2, "E", 1.8, 3), Port(3, "E", -1.8, 3)),
            bands={"pass": Band(9.0, 11.0, 2)},
            requirements=reqs,
            lumped=(Lumped("R1", (3.0, 3.6), (-0.3, 0.3), "y", 100.0, 0.6),),
            optimizer=_SMOKE_OPT,
            solver=_SMOKE_SOLVER,
        )
    return Spec(
        name="wilkinson-x10",
        stackup=S1,
        grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
        design_region=(0.0, 12.0, -6.0, 6.0),
        symmetry="mirror_y",
        rules=Rules(0.6, 0.6),
        ports=(Port(1, "W", 0.0), Port(2, "E", 4.2), Port(3, "E", -4.2)),
        bands=band,
        requirements=reqs,
        # The resistor sits about a quarter wave (4.5–5 mm of a 70 Ω line) from port 1, where a
        # Wilkinson's arms end; at 7.2–7.8 mm (a first try) the arms were three eighths of a
        # wave long and the run ended with |S22| −8 dB.
        lumped=(Lumped("R1", (4.8, 5.4), (-0.3, 0.3), "y", 100.0, 0.6),),
        optimizer=OptimizerSpec(iterations_per_beta=30, budget_min=90, init=INIT, move_late=0.05),
    )


def antenna(scale: str = "full") -> Spec:
    """(b) A microstrip-fed antenna over ground, generated by the optimization: judged at
    |S11| ≤ −10 dB (50 Ω) and a radiated fraction ≥ 0.6 over 9.85–10.15 GHz (3 %) on every
    grid, optimized for −10 dB and η ≥ 0.7 over 9.65–10.35 GHz; S2, mirror symmetric about
    y = 0 (design §11.3, §22; docs/decisions.md).

    The start is not an antenna: the port's feed line continued to the window's centre
    (`seed: star`, the feed-line-only start), every other pixel transparent; only the port
    pad (two pixels deep) is fixed. The spec's robust epigraph grows the radiator from it: the
    nominal design and its dilated and eroded versions (thresholds 0.45 and 0.55) must all
    meet the targets (without the eroded design the radiating edge stalled as a lossy gray
    comb; without the dilated one the design relied on one-pixel slots and corner contacts that
    the width and space repair closed), with the round-2 solver (`edge_correction`, the modal
    source), adaptive moves and the reflection judged at 50 Ω (`reference_ohm`). The
    closed-form inset patch is kept only as a reference (`antenna_patch_reference`). The band:
    on this grid and solver the inset patch matches −10 dB over 3.4 % (10.05–10.40 GHz), so
    3 % asks for about the bandwidth of one patch on S2."""
    _check_scale(scale)
    reqs = (
        S(1, 1).at_most_db(-10, band="main"),
        RadiatedFraction(1).at_least(0.70, band="main"),
    )
    solver = dict(edge_correction=True, port_source="mode")
    if scale == "smoke":
        return Spec(
            name="antenna-smoke",
            stackup=S2,
            grid=GridSpec(pitch_mm=0.8, margin_mm=3.2, air_mm=6.4, f_max_ghz=12.0, **_SMOKE_GRID),
            design_region=(0.0, 8.0, -4.0, 4.0),
            symmetry="mirror_y",
            rules=Rules(1.6, 1.6),
            ports=(Port(1, "W", 0.0, 4),),
            bands={"main": Band(9.85, 10.15, 2)},
            requirements=reqs,
            radiation=RadiationBox(offset_mm=1.6, height_mm=4.0),
            optimizer=replace(_SMOKE_OPT, seed="star", adaptive_move=True, reference_ohm=50.0),
            solver=replace(_SMOKE_SOLVER, **solver),
        )
    return Spec(
        name="antenna-x10",
        stackup=S2,
        grid=GridSpec(pitch_mm=0.4, substrate_cells=6, air_mm=11.0, dz_max_mm=1.2),
        design_region=(0.0, 18.0, -9.0, 9.0),
        symmetry="mirror_y",
        rules=Rules(0.8, 0.8),
        ports=(Port(1, "W", 0.0),),
        # Optimized over 9.65–10.35 GHz (the criteria's 9.85–10.15 plus 0.2 GHz on each side, as
        # the diplexer's channels): a first run optimized over the criteria band itself met it on
        # the optimization grid with 0.3 dB to spare at 9.85 GHz (against the feed's Z_c; −9.7 dB
        # at 50 Ω) and its −10 dB edge moved up by 0.9 and 1.3 % on the finer grids.
        bands={"main": Band(9.65, 10.35, 5)},
        requirements=reqs,
        radiation=RadiationBox(offset_mm=2.4, height_mm=8.0),
        optimizer=OptimizerSpec(
            betas=(8, 16, 32, 64),
            iterations_per_beta=15,
            budget_min=150,
            seed="star",
            adaptive_move=True,
            eta_variants=(0.45, 0.55),
            reference_ohm=50.0,
        ),
        solver=SolverSpec(**solver),
    )


def antenna_patch_reference(scale: str = "full") -> Spec:
    """A REFERENCE, not a case: round 1's antenna spec, which starts from the closed-form
    inset-fed patch (`seed: patch`, `seeds.tuned_patch`) and only refines it (the optimizer did
    not change it). Kept to compare the generated antenna with a textbook patch and for the
    seed tests; not in `CASES`. |S11| ≤ −12 dB and a radiated fraction ≥ 0.70 over 9.8–10.2
    GHz on S2 with round 1's solver settings."""
    _check_scale(scale)
    reqs = (
        S(1, 1).at_most_db(-12, band="main"),
        RadiatedFraction(1).at_least(0.70, band="main"),
    )
    if scale == "smoke":
        return Spec(
            name="antenna_patch_reference-smoke",
            stackup=S2,
            grid=GridSpec(pitch_mm=0.8, margin_mm=3.2, air_mm=6.4, f_max_ghz=12.0, **_SMOKE_GRID),
            design_region=(0.0, 8.0, -4.0, 4.0),
            symmetry="mirror_y",
            rules=Rules(1.6, 1.6),
            ports=(Port(1, "W", 0.0, 4),),
            bands={"main": Band(9.7, 10.3, 2)},
            requirements=reqs,
            radiation=RadiationBox(offset_mm=1.6, height_mm=4.0),
            optimizer=replace(_SMOKE_OPT, seed="patch"),
            solver=_SMOKE_SOLVER,
        )
    return Spec(
        name="antenna_patch_reference-x10",
        stackup=S2,
        grid=GridSpec(pitch_mm=0.4, substrate_cells=6, air_mm=11.0, dz_max_mm=1.2),
        design_region=(0.0, 18.0, -9.0, 9.0),
        symmetry="mirror_y",
        rules=Rules(0.8, 0.8),
        ports=(Port(1, "W", 0.0),),
        bands={"main": Band(9.8, 10.2, 4)},
        requirements=reqs,
        radiation=RadiationBox(offset_mm=2.4, height_mm=8.0),
        # The seed is a working patch to refine: β starts at 16, where its inset slots stay void
        # (at β = 8 they blurred into lossy gray and the first step closed them; at β = 32 every
        # pixel saturated and nothing moved). Conservative MMA with a 0.05 move: plain MMA left
        # the tuned seed (t 0.35) on its first step and wandered at t 0.7–2.8 for 12 steps, and
        # the conservative variant with a 0.1 move still rose to 0.5–0.8 (its four inner
        # iterations did not always reach a conservative approximation).
        optimizer=OptimizerSpec(
            betas=(16, 64),
            iterations_per_beta=10,
            min_iterations=5,
            budget_min=30,
            move=0.05,
            move_late=0.05,
            conservative=True,
            max_inner=4,
            seed="patch",
        ),
    )


def diplexer(scale: str = "full") -> Spec:
    """(c) A two-channel filter bank: channel A (7.6–8.4 GHz) to port 2, channel B
    (11.6–12.4 GHz) to port 3, the common port 1 matched in both (design §11.4)."""
    _check_scale(scale)
    if scale == "smoke":
        bands = {"A": Band(7.6, 8.4, 2), "B": Band(11.6, 12.4, 2)}
    else:
        # The objective frequencies widen each channel by 0.2 GHz against coarse-to-fine shifts.
        bands = {"A": Band(7.4, 8.6, 4), "B": Band(11.4, 12.6, 4)}
    reqs = (
        S(2, 1).at_least_db(-1.0, band="A"),
        S(3, 1).at_most_db(-22, band="A"),
        S(1, 1).at_most_db(-12, band="A"),
        S(3, 1).at_least_db(-1.0, band="B"),
        S(2, 1).at_most_db(-22, band="B"),
        S(1, 1).at_most_db(-12, band="B"),
    )
    if scale == "smoke":
        return Spec(
            name="diplexer-smoke",
            stackup=S1,
            grid=GridSpec(pitch_mm=0.6, margin_mm=1.8, air_mm=3.0, f_max_ghz=15.0, **_SMOKE_GRID),
            design_region=(0.0, 4.8, -3.3, 3.3),
            rules=Rules(1.2, 1.2),
            ports=(Port(1, "W", 0.0, 3), Port(2, "E", 1.8, 3), Port(3, "E", -1.8, 3)),
            bands=bands,
            requirements=reqs,
            optimizer=replace(_SMOKE_OPT, seed="stubs"),
            solver=_SMOKE_SOLVER,
        )
    return Spec(
        name="diplexer-x8x12",
        stackup=S1,
        grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
        design_region=(0.0, 15.0, -7.5, 7.5),
        rules=Rules(0.6, 0.6),
        ports=(Port(1, "W", 0.0), Port(2, "E", 4.5), Port(3, "E", -4.5)),
        bands=bands,
        requirements=reqs,
        # From the plain junction (`seed: star`) the branches only learned to roll off (rejection
        # 9–12 dB, t 1.8–2.4); the stub seed puts a quarter-wave open stub for the other channel
        # on each branch (`seeds.stub_mask`), which the optimizer then reshapes. Its best
        # binarized design comes at β = 8 (t 0.79) and plain MMA loses it from β = 16 on (t 1.84;
        # the export keeps the best); moves of 0.15/0.05 did worse (best t 1.84).
        optimizer=OptimizerSpec(iterations_per_beta=25, budget_min=55, seed="stubs"),
    )


def filterbank3(scale: str = "full") -> Spec:
    """(c2, stretch) A three-channel filter bank: 7.0–7.6, 9.7–10.3 and 12.4–13.0 GHz from the
    common port 1 to ports 2, 3 and 4 (design §11.4); looser targets than the diplexer."""
    _check_scale(scale)
    if scale == "smoke":
        bands = {"A": Band(7.0, 7.6, 1), "B": Band(9.7, 10.3, 1), "C": Band(12.4, 13.0, 1)}
    else:
        bands = {"A": Band(6.8, 7.8, 3), "B": Band(9.5, 10.5, 3), "C": Band(12.2, 13.2, 3)}
    reqs = []
    for band, port in (("A", 2), ("B", 3), ("C", 4)):
        reqs.append(S(port, 1).at_least_db(-1.5, band=band))
        reqs += [S(q, 1).at_most_db(-20, band=band) for q in (2, 3, 4) if q != port]
        reqs.append(S(1, 1).at_most_db(-10, band=band))
    if scale == "smoke":
        return Spec(
            name="filterbank3-smoke",
            stackup=S1,
            grid=GridSpec(pitch_mm=0.6, margin_mm=1.8, air_mm=3.0, f_max_ghz=15.0, **_SMOKE_GRID),
            design_region=(0.0, 4.8, -3.9, 3.9),
            rules=Rules(1.2, 1.2),
            ports=(
                Port(1, "W", 0.0, 3),
                Port(2, "E", 2.4, 3),
                Port(3, "E", 0.0, 3),
                Port(4, "E", -2.4, 3),
            ),
            bands=bands,
            requirements=tuple(reqs),
            optimizer=_SMOKE_OPT,
            solver=_SMOKE_SOLVER,
        )
    return Spec(
        name="filterbank3-x7x10x13",
        stackup=S1,
        grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
        design_region=(0.0, 18.0, -9.0, 9.0),
        rules=Rules(0.6, 0.6),
        ports=(Port(1, "W", 0.0), Port(2, "E", 6.0), Port(3, "E", 0.0), Port(4, "E", -6.0)),
        bands=bands,
        requirements=tuple(reqs),
        # Moves of 0.05 from β = 32 on: with 0.1 the near-binary designs of the other cases
        # flipped boundary pixels back and forth (t alternating between 0.7 and 8).
        optimizer=OptimizerSpec(
            iterations_per_beta=25, budget_min=90, seed="stubs", move_late=0.05
        ),
    )


CASES = {
    "divider": divider,
    "wilkinson": wilkinson,
    "antenna": antenna,
    "diplexer": diplexer,
    "filterbank3": filterbank3,
}


def spec_for(case: str, scale: str = "full") -> Spec:
    if case not in CASES:
        raise ValueError(f"unknown case {case!r}; one of {sorted(CASES)}")
    return CASES[case](scale)


# -- pass criteria ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One pass criterion on a sweep (engineering S-parameters renormalized to 50 Ω).

    kind: "s_max" / "s_min" (|S_ij| in dB against `limit`), "imbalance" (||S_ij| − |S_kl||
      in dB ≤ `limit`, `ports` = (i, j, k, l)), "eta_min" (radiated fraction of port j ≥
      `limit`), "passivity" (min eig(I − SᴴS) ≥ `limit`, every frequency), "balance" (the
      power balance error of port j, `validate.power_balance`, |error| ≤ `limit`, at the
      `at_ghz` frequencies).
    ghz: the inclusive band the check applies to (None: every frequency), or `at_ghz`: the
      exact frequencies.
    """

    name: str
    kind: str
    ports: tuple[int, ...] = ()
    limit: float = 0.0
    ghz: tuple[float, float] | None = None
    at_ghz: tuple[float, ...] | None = None

    def mask(self, freqs_hz: np.ndarray) -> np.ndarray:
        f = np.asarray(freqs_hz) / 1e9
        if self.at_ghz is not None:
            return np.any(np.abs(f[:, None] - np.asarray(self.at_ghz)[None, :]) < 1e-6, axis=1)
        if self.ghz is None:
            return np.ones(f.shape, bool)
        return (f >= self.ghz[0] - 1e-9) & (f <= self.ghz[1] + 1e-9)

    def evaluate(self, freqs_hz, s, eta=None, balance=None) -> dict:
        """{name, kind, limit, worst, ok, points}; `s` is (F, N, N) at `freqs_hz`; `balance`
        maps a port to `validate.power_balance`'s report (for "balance")."""
        from yapnr.rf.sparams import db, passivity_margin

        if self.kind == "balance":
            if not balance or self.ports[0] not in balance:
                raise ValueError(f"check {self.name}: no power balance")
            err = np.abs(np.asarray(balance[self.ports[0]]["error"], dtype=np.float64))
            worst = float(np.max(err))
            return {
                "name": self.name,
                "kind": self.kind,
                "limit": self.limit,
                "worst": worst,
                "ok": bool(worst <= self.limit),
                "points": int(err.size),
            }
        m = self.mask(freqs_hz)
        if not m.any():
            raise ValueError(f"check {self.name}: no sweep frequency in its band")
        if self.kind in ("s_max", "s_min"):
            i, j = self.ports
            x = db(s[m, i - 1, j - 1])
            worst = float(np.max(x) if self.kind == "s_max" else np.min(x))
            ok = worst <= self.limit if self.kind == "s_max" else worst >= self.limit
        elif self.kind == "imbalance":
            i, j, k, l_ = self.ports
            x = np.abs(db(s[m, i - 1, j - 1]) - db(s[m, k - 1, l_ - 1]))
            worst = float(np.max(x))
            ok = worst <= self.limit
        elif self.kind == "eta_min":
            if not eta or self.ports[0] not in eta:
                raise ValueError(f"check {self.name}: no radiated fraction in the sweep")
            worst = float(np.min(np.asarray(eta[self.ports[0]])[m]))
            ok = worst >= self.limit
        elif self.kind == "passivity":
            worst = float(np.min(passivity_margin(s[m])))
            ok = worst >= self.limit
        else:
            raise ValueError(f"unknown check kind {self.kind!r}")
        return {
            "name": self.name,
            "kind": self.kind,
            "limit": self.limit,
            "worst": worst,
            "ok": bool(ok),
            "points": int(m.sum()),
        }


# Passivity tolerance: min eig(I − SᴴS) ≥ −1e-3 (design §11). An earlier de-embedding with the
# calibration's Im k (an artifact of the near-source fields, up to 4 Np/m against a true line
# loss of about 0.8 Np/m) inflated every |S| by up to 0.15 dB and needed −0.01; with Re k only
# (`sparams`) and the 6h feeds the divider's margin is +0.02 to +0.05.
PASSIVITY = -1e-3

# Power balance of radiators (design §11.3, `validate.power_balance`): the port's net input
# power against the flux out of a box closed by the ground, the other ports' power and the
# dissipation inside it, within 4 % (round 1: 2 %). The box leaves a window where the feed
# crosses it; on the antenna's grid the window's blind area limits the balance to about 2–3.5 %
# for radiators near the feed (the closed-form patch: −1.9 % at its resonance, −3.5 % at
# 10.35 GHz; a smaller window counts the line's guided fringe, −4.9 and −10.5 %, a larger one
# misses more radiation, −3.6 and −5.4 %). The error is negative (the box finds less power than
# the port), i.e. on the safe side for the radiated fraction (docs/decisions.md, round 2).
BALANCE = 0.04


def _divider_checks(r11, t, imb=None):
    out = [
        Check("|S11| max dB", "s_max", (1, 1), r11, (8.5, 11.5)),
        Check("|S21| min dB", "s_min", (2, 1), t, (8.5, 11.5)),
        Check("|S31| min dB", "s_min", (3, 1), t, (8.5, 11.5)),
    ]
    if imb is not None:
        out.append(Check("||S21|-|S31|| dB", "imbalance", (2, 1, 3, 1), imb, (8.5, 11.5)))
    return out + [Check("passivity", "passivity", limit=PASSIVITY)]


def _diplexer_checks(loss, rej, match):
    a, b = (7.6, 8.4), (11.6, 12.4)
    return [
        Check("A: |S21| min dB", "s_min", (2, 1), loss, a),
        Check("B: |S31| min dB", "s_min", (3, 1), loss, b),
        Check("A: |S31| max dB", "s_max", (3, 1), rej, a),
        Check("B: |S21| max dB", "s_max", (2, 1), rej, b),
        Check("A: |S11| max dB", "s_max", (1, 1), match, a),
        Check("B: |S11| max dB", "s_max", (1, 1), match, b),
        Check("passivity", "passivity", limit=PASSIVITY),
    ]


def _bank3_checks(loss, rej, match):
    out = []
    for name, (lo, hi), port in (
        ("A", (7.0, 7.6), 2),
        ("B", (9.7, 10.3), 3),
        ("C", (12.4, 13.0), 4),
    ):
        out.append(Check(f"{name}: |S{port}1| min dB", "s_min", (port, 1), loss, (lo, hi)))
        for q in (2, 3, 4):
            if q != port:
                out.append(Check(f"{name}: |S{q}1| max dB", "s_max", (q, 1), rej, (lo, hi)))
        out.append(Check(f"{name}: |S11| max dB", "s_max", (1, 1), match, (lo, hi)))
    return out + [Check("passivity", "passivity", limit=PASSIVITY)]


def _wilkinson_checks(match, t, iso):
    band = (9.0, 11.0)
    return [
        Check("|S11| max dB", "s_max", (1, 1), match, band),
        Check("|S21| min dB", "s_min", (2, 1), t, band),
        Check("|S31| min dB", "s_min", (3, 1), t, band),
        Check("|S22| max dB", "s_max", (2, 2), match, band),
        Check("|S33| max dB", "s_max", (3, 3), match, band),
        Check("|S32| max dB (isolation)", "s_max", (3, 2), iso, band),
        Check("passivity", "passivity", limit=PASSIVITY),
    ]


# Design §11.2–§11.4: "coarse" on the binary design re-simulated from the footprint on the
# optimization grid, "fine" on the finer re-validation grid.
CRITERIA = {
    "divider": {
        "coarse": _divider_checks(-17.0, -3.45),
        "fine": _divider_checks(-15.0, -3.6, 0.25),
    },
    "wilkinson": {
        "coarse": _wilkinson_checks(-17.0, -3.6, -17.0),
        "fine": _wilkinson_checks(-15.0, -3.8, -15.0),
    },
    # Round 2: the band (9.85–10.15 GHz) and the levels (|S11| ≤ −10 dB, η ≥ 0.6) are the same
    # on every grid; with the copper-edge correction the grids agree to about 0.2 %.
    "antenna": {
        level: [
            Check("|S11| max dB", "s_max", (1, 1), -10.0, (9.85, 10.15)),
            Check("eta min", "eta_min", (1,), 0.60, at_ghz=(9.85, 10.0, 10.15)),
            Check("power balance", "balance", (1,), BALANCE, at_ghz=(9.85, 10.0, 10.15)),
            Check("passivity", "passivity", limit=PASSIVITY),
        ]
        for level in ("coarse", "fine")
    },
    "diplexer": {
        "coarse": _diplexer_checks(-1.5, -18.0, -10.0),
        "fine": _diplexer_checks(-2.0, -15.0, -8.0),
    },
    # Stretch (c2): looser than the diplexer (three resonant channels in 18 × 18 mm).
    "filterbank3": {
        "coarse": _bank3_checks(-2.5, -15.0, -8.0),
        "fine": _bank3_checks(-3.0, -12.0, -6.0),
    },
}

# The third re-validation grid of the full cases (a third of the pitch, twice the substrate
# cells): the fine criteria must hold there too, and the report shows the trend over the grids.
FINER = 3

# Dense in-band frequencies (GHz) of the re-validation sweeps; the criteria are judged there.
DENSE_GHZ = {
    "divider": np.linspace(8.5, 11.5, 61),
    "wilkinson": np.linspace(9.0, 11.0, 41),
    "antenna": np.linspace(9.7, 10.3, 61),  # includes 9.8–10.2 at 0.01 GHz
    "diplexer": np.concatenate([np.linspace(7.4, 8.6, 25), np.linspace(11.4, 12.6, 25)]),
    "filterbank3": np.concatenate(
        [np.linspace(6.8, 7.8, 21), np.linspace(9.5, 10.5, 21), np.linspace(12.2, 13.2, 21)]
    ),
}


def case_of(spec: Spec) -> str:
    """The case name of a preset spec ("divider-x10" → "divider")."""
    head = spec.name.split("-")[0]
    if head not in CASES:
        raise ValueError(f"spec {spec.name!r} is not one of the preset cases")
    return head


def sweep_frequencies(case: str, spec: Spec, broadband: np.ndarray | None = None) -> np.ndarray:
    """The re-validation frequencies (Hz): the dense in-band points, the spec's objective
    frequencies and an optional broadband list, sorted and unique."""
    f = [DENSE_GHZ[case] * 1e9, spec.objective_frequencies()]
    if broadband is not None:
        f.append(np.asarray(broadband, dtype=np.float64))
    f = np.sort(np.concatenate(f))
    keep = np.concatenate([[True], np.diff(f) > 1e-6 * f[1:]])
    return f[keep]


def judge(checks, freqs_hz, s, eta=None, balance=None) -> dict:
    """Evaluate `checks`; {"checks": [...], "ok": all passed}."""
    res = [c.evaluate(freqs_hz, s, eta, balance) for c in checks]
    return {"checks": res, "ok": all(r["ok"] for r in res)}


# -- running a case ---------------------------------------------------------------------------


def run(
    case: str,
    out_dir: str,
    *,
    scale: str = "full",
    validate_fine: bool = True,
    refine: int = 2,
    finer: int | None = None,
    max_iterations: int | None = None,
    log=print,
) -> dict:
    """Optimize, export and re-validate one case into `out_dir`; returns the validation
    report (also written as `validation.json`). A run directory with a checkpoint resumes.
    `finer` adds a third re-validation grid (default 3 for the full cases, none for smoke)."""
    from yapnr.rf import validate
    from yapnr.rf.driver import Optimizer, design

    spec = spec_for(case, scale)
    t0 = time.perf_counter()
    if max_iterations is not None:
        # Stop the loop early (still binarize, export and validate): for trials.
        from yapnr.rf.problem import Problem

        os.makedirs(out_dir, exist_ok=True)
        problem = Problem(spec, cache_dir=os.path.join(out_dir, "cache"), log=log)
        opt = Optimizer(problem, out_dir=out_dir, log=log)
        opt.run(max_iterations=max_iterations)
        if not opt.done:
            opt.state.stop_reason = "max_iterations"
            opt.checkpoint()
        result = design(spec, out_dir, problem=problem, log=log, resume=True)
    else:
        result = design(spec, out_dir, log=log)
    t_design = time.perf_counter() - t0
    if finer is None and scale == "full":
        finer = FINER
    report = validate.validate_case(
        out_dir, case=case, refine=refine, fine=validate_fine, finer=finer, log=log
    )
    report["wall_s"]["design"] = t_design
    report["optimizer"] = result["optimizer"]
    validate.write_report(out_dir, report)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m yapnr.rf.cases", description=__doc__.split("\n")[0]
    )
    ap.add_argument("action", choices=("run", "validate", "spec", "criteria"))
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("--out", help="run directory")
    ap.add_argument("--smoke", action="store_true", help="the tiny CI variant")
    ap.add_argument("--refine", type=int, default=2)
    ap.add_argument("--finer", type=int, help=f"third grid (default {FINER}; 0: none)")
    ap.add_argument("--no-fine", action="store_true", help="skip the fine re-validation")
    ap.add_argument("--max-iterations", type=int)
    args = ap.parse_args(argv)
    scale = "smoke" if args.smoke else "full"
    if args.action == "spec":
        print(json.dumps(spec_for(args.case, scale).to_dict(), indent=1))
        return 0
    if args.action == "criteria":
        out = {
            level: [
                {k: v for k, v in asdict(c).items() if v not in (None, ())}
                for c in CRITERIA[args.case][level]
            ]
            for level in ("coarse", "fine")
        }
        print(json.dumps(out, indent=1))
        return 0
    if not args.out:
        ap.error("--out is required")
    if args.action == "run":
        rep = run(
            args.case,
            args.out,
            scale=scale,
            validate_fine=not args.no_fine,
            refine=args.refine,
            finer=args.finer,
            max_iterations=args.max_iterations,
        )
    else:
        from yapnr.rf import validate

        finer = args.finer if args.finer is not None else (None if args.smoke else FINER)
        rep = validate.validate_case(
            args.out, case=args.case, refine=args.refine, fine=not args.no_fine, finer=finer or None
        )
        validate.write_report(args.out, rep)
    print(json.dumps(validate_summary(rep), indent=1))
    return 0 if rep["ok"] else 1


def validate_summary(rep: dict) -> dict:
    out = {"ok": rep["ok"]}
    for level in ("coarse", "fine", "finer"):
        if level in rep:
            out[level] = {c["name"]: [round(c["worst"], 3), c["ok"]] for c in rep[level]["checks"]}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
