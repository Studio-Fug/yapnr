"""Design specs: transfer-function targets for a microstrip footprint (design §9).

A spec names the stackup, the grid, the design region, the ports, frequency bands and the
requirements on the S-parameters and the radiated fraction. Files are YAML or JSON
(`yapnr-rf-spec/1`) with lengths in mm and frequencies in GHz; the Python API mirrors them:

    spec = Spec(
        name="divider-x10",
        stackup=StackupSpec(er=3.55, tan_delta=0.0027, h_mm=0.813, f_ref_ghz=10),
        grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
        design_region=(0.0, 9.6, -6.0, 6.0),
        symmetry="mirror_y",
        rules=Rules(min_width_mm=0.6, min_space_mm=0.6),
        ports=(Port(1, "W", 0.0), Port(2, "E", 4.2), Port(3, "E", -4.2)),
        bands={"pass": Band(8.5, 11.5, 7)},
        requirements=(
            S(1, 1).at_most_db(-20, band="pass"),
            S(2, 1).at_least_db(-3.28, band="pass"),
            S(3, 1).at_least_db(-3.28, band="pass"),
        ),
    )

Requirement forms (YAML on the left, Python on the right):

    {s: [1, 1], max_db: -20, band: b}            S(1, 1).at_most_db(-20, band="b")
    {s: [2, 1], min_db: -3.3, band: b}           S(2, 1).at_least_db(-3.3, band="b")
    {s: [2, 1], between_db: [-3.5, -3.0], ...}   S(2, 1).between_db(-3.5, -3.0, band="b")
    {s: [1, 1], mask_db: [[8, -10], [9, -20]]}   S(1, 1).mask_db([(8, -10), (9, -20)], ...)
    {s: [2, 1], min_mask_db: [[f, L], ...]}      S(2, 1).mask_db([...], kind="min", ...)
    {s: [2, 1], phase_deg: 90, tol_deg: 5}       S(2, 1).phase_deg(90, tol=5, band="b")
    {radiated: 1, min: 0.7, band: b}             RadiatedFraction(1).at_least(0.7, band="b")
    {absorbed: 2, element: R1, min: 0.4, ...}    Absorbed("R1", 2).at_least(0.4, band="b")
    {loss: 2, max: 0.08, band: b}                Loss(2).at_most(0.08, band="b")

Lumped resistors (`lumped`, e.g. the isolation resistor of a Wilkinson divider) are SMD parts
across a void gap with copper pads at both ends: `Lumped(name, x_mm, y_mm, axis, ohms, pad_mm)`.
An `absorbed` requirement bounds the fraction of the power incident at a port that one of them
dissipates (a Wilkinson's resistor takes half of what enters an output port); a `loss`
requirement bounds the rest of what is lost, the fraction of the incident power that leaves
neither through a port nor into a lumped resistor (radiation and dissipation in the copper,
gray copper included, and the substrate).

Masks are piecewise linear in frequency (GHz, dB) and are evaluated at the band's samples.
Phases use the engineering e^{+jωt} convention, like everything reported. Each requirement may
set `scale` (its normalization, §9 of the design) and every requirement needs a band.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace

import numpy as np

from yapnr.rf.stackup import Stackup

SCHEMA = "yapnr-rf-spec/1"
SIDES = ("W", "E", "S", "N")


def _tuple(x):
    return tuple(x) if isinstance(x, (list, tuple)) else x


# -- components -------------------------------------------------------------------------------


@dataclass(frozen=True)
class StackupSpec:
    """Substrate εr, tan δ and thickness h (mm); the reference frequency (GHz) sets the copper
    sheet conductance and the matched dielectric loss."""

    er: float
    tan_delta: float
    h_mm: float
    f_ref_ghz: float

    def to_stackup(self) -> Stackup:
        return Stackup(self.er, self.tan_delta, self.h_mm * 1e-3, self.f_ref_ghz * 1e9)


@dataclass(frozen=True)
class GridSpec:
    """The solver grid. Unset values take the design's defaults (problem.py)."""

    pitch_mm: float
    substrate_cells: int
    air_mm: float | None = None
    dz_max_mm: float | None = None
    margin_mm: float | None = None
    meas_cells: int | None = None
    src_cells: int | None = None
    pml_gap: int = 3
    core_cells: int = 2
    pml_cells: int = 10
    pml_top_cells: int = 8
    ratio: float = 1.25
    courant: float = 0.95
    f_max_ghz: float | None = None


@dataclass(frozen=True)
class Port:
    """A line port: number, the side of the design region it enters (W, E, S, N), the
    transverse position of the strip centre (mm) and its width in cells (None: chosen by
    calibration for 50 Ω). Or, in a board model (`board`), a lumped port (`kind="lumped"`):
    Ez columns from the ground to the copper on the grid nodes inside the rectangle `x_mm` ×
    `y_mm` (a degenerate side is a line of nodes), `ohms` (default 50) in series with the
    source (design §26.1)."""

    n: int
    side: str | None = None
    at_mm: float | None = None
    width_cells: int | None = None
    kind: str = "line"
    x_mm: tuple | None = None
    y_mm: tuple | None = None
    ohms: float | None = None

    def to_dict(self) -> dict:
        if self.kind == "lumped":
            return {
                "n": self.n,
                "kind": "lumped",
                "x_mm": list(self.x_mm),
                "y_mm": list(self.y_mm),
                "ohms": 50.0 if self.ohms is None else self.ohms,
            }
        return {
            "n": self.n,
            "side": self.side,
            "at_mm": self.at_mm,
            "width_cells": self.width_cells,
        }


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle (mm)."""

    x_mm: tuple
    y_mm: tuple

    def to_dict(self) -> dict:
        return {"x_mm": list(self.x_mm), "y_mm": list(self.y_mm)}

    def si(self) -> tuple:
        return (tuple(v * 1e-3 for v in self.x_mm), tuple(v * 1e-3 for v in self.y_mm))


@dataclass(frozen=True)
class GroundSpec:
    """A board's ground plane (at `stackup.h_mm` below the copper): the outline (default the
    board's) less the keepout rectangles."""

    x_mm: tuple | None = None
    y_mm: tuple | None = None
    keepout: tuple = ()

    def to_dict(self) -> dict:
        out: dict = {}
        if self.x_mm is not None:
            out["x_mm"] = list(self.x_mm)
            out["y_mm"] = list(self.y_mm)
        if self.keepout:
            out["keepout"] = [k.to_dict() for k in self.keepout]
        return out


@dataclass(frozen=True)
class BoardSpec:
    """A board model (design §26.1): the board's outline `x_mm` × `y_mm`, its substrate block
    `thickness_mm` (default `stackup.h_mm`, the ground at the bottom), its `ground` ("infinite":
    a PEC floor under everything, far field by image theory; or a `GroundSpec`), fixed top
    `copper` outside the design region (feeds to the ports), `air_mm` of air beyond it to the
    CPML (default λ0/4 at the highest frequency) and `max_cell_mm` (default min(λd/15, λ0/20))."""

    x_mm: tuple
    y_mm: tuple
    thickness_mm: float | None = None
    ground: object = field(default_factory=GroundSpec)
    air_mm: float | None = None
    max_cell_mm: float | None = None
    copper: tuple = ()
    pml_cells: int = 8

    @property
    def infinite(self) -> bool:
        return self.ground == "infinite"

    def to_dict(self) -> dict:
        out: dict = {"x_mm": list(self.x_mm), "y_mm": list(self.y_mm)}
        if self.thickness_mm is not None:
            out["thickness_mm"] = self.thickness_mm
        out["ground"] = "infinite" if self.infinite else self.ground.to_dict()
        for key in ("air_mm", "max_cell_mm"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.copper:
            out["copper"] = [r.to_dict() for r in self.copper]
        if self.pml_cells != 8:
            out["pml_cells"] = self.pml_cells
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "BoardSpec":
        d = dict(d)
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"board: unknown keys {sorted(unknown)}")
        ground = d.get("ground", {})
        if ground != "infinite":
            if not isinstance(ground, dict):
                raise ValueError("board.ground: 'infinite' or {x_mm, y_mm, keepout}")
            bad = set(ground) - {"x_mm", "y_mm", "keepout"}
            if bad:
                raise ValueError(f"board.ground: unknown keys {sorted(bad)}")
            ground = GroundSpec(
                _tuple(ground.get("x_mm")),
                _tuple(ground.get("y_mm")),
                tuple(_rect(k) for k in ground.get("keepout", ())),
            )
        return cls(
            x_mm=tuple(float(v) for v in d["x_mm"]),
            y_mm=tuple(float(v) for v in d["y_mm"]),
            thickness_mm=d.get("thickness_mm"),
            ground=ground,
            air_mm=d.get("air_mm"),
            max_cell_mm=d.get("max_cell_mm"),
            copper=tuple(_rect(r) for r in d.get("copper", ())),
            pml_cells=int(d.get("pml_cells", 8)),
        )


def _rect(d: dict) -> Rect:
    bad = set(d) - {"x_mm", "y_mm"}
    if bad:
        raise ValueError(f"rectangle: unknown keys {sorted(bad)}")
    return Rect(tuple(float(v) for v in d["x_mm"]), tuple(float(v) for v in d["y_mm"]))


@dataclass(frozen=True)
class Band:
    """A frequency band [lo, hi] GHz sampled at `points` equally spaced frequencies (or at the
    explicit `ghz_points`)."""

    lo_ghz: float
    hi_ghz: float
    points: int = 5
    ghz_points: tuple[float, ...] | None = None

    def frequencies(self) -> np.ndarray:
        """Sample frequencies in Hz."""
        if self.ghz_points:
            return np.asarray(self.ghz_points, dtype=np.float64) * 1e9
        if self.points == 1:
            return np.array([0.5 * (self.lo_ghz + self.hi_ghz)]) * 1e9
        return np.linspace(self.lo_ghz, self.hi_ghz, self.points) * 1e9


@dataclass(frozen=True)
class Rules:
    """Minimum copper width and space (mm)."""

    min_width_mm: float = 0.0
    min_space_mm: float = 0.0

    @property
    def active(self) -> bool:
        return self.min_width_mm > 0.0 or self.min_space_mm > 0.0


@dataclass(frozen=True)
class FixedRegion:
    """Design pixels whose centre lies in the rectangle get `value` (1 copper, 0 keepout)."""

    x_mm: tuple[float, float]
    y_mm: tuple[float, float]
    value: float = 0.0


@dataclass(frozen=True)
class Lumped:
    """A lumped resistor across a gap in the copper (an SMD part, e.g. a Wilkinson divider's
    isolation resistor). The body rectangle (mm) is kept void and carries `ohms` between its two
    ends along `axis` ("x" or "y"); beyond each end a pad of `pad_mm` (the body's width) is
    kept copper for the part's terminals. The optimizer connects the pads to the rest."""

    name: str
    x_mm: tuple[float, float]
    y_mm: tuple[float, float]
    axis: str
    ohms: float
    pad_mm: float

    def pads(self) -> tuple[tuple, tuple]:
        """The two pad rectangles ((x0, x1), (y0, y1)) in mm, low end first."""
        (x0, x1), (y0, y1) = sorted(self.x_mm), sorted(self.y_mm)
        p = self.pad_mm
        if self.axis == "x":
            return ((x0 - p, x0), (y0, y1)), ((x1, x1 + p), (y0, y1))
        return ((x0, x1), (y0 - p, y0)), ((x0, x1), (y1, y1 + p))


@dataclass(frozen=True)
class RadiationBox:
    """The radiated-power box (design §25.2–§25.3): closed by the ground, without feed windows,
    enclosing the design region (and, with a `board`, the whole board) by `clearance_cells` on
    every side and above the copper. `offset_mm` (beyond the design region) and `height_mm`
    (above the copper) optionally place its faces further out; the result does not depend on
    them beyond the feed's own loss inside the box (about 0.12 %/mm on S2, the audit).

    The feed windows of round 2 (`window_margin_mm`, `window_height_mm`) are removed: they
    dropped 3–6 % of the input power leaving backwards through the window and counted 1–1.6 %
    of the guided wave as inflow; a non-null value is an error."""

    clearance_cells: int = 2
    offset_mm: float | None = None
    height_mm: float | None = None
    window_margin_mm: None = None
    window_height_mm: None = None

    def __post_init__(self) -> None:
        for key in ("window_margin_mm", "window_height_mm"):
            if getattr(self, key) is not None:
                raise ValueError(
                    f"radiation.{key} is removed: the radiation box is closed and the feeds' "
                    "guided waves are separated modally (docs/design/rf-topology-optimization.md "
                    "§25.3); leave it out"
                )
        if int(self.clearance_cells) < 1:
            raise ValueError("radiation.clearance_cells must be at least 1")

    def to_dict(self) -> dict:
        out: dict = {}
        if self.clearance_cells != 2:
            out["clearance_cells"] = int(self.clearance_cells)
        for key in ("offset_mm", "height_mm"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


@dataclass(frozen=True)
class OptimizerSpec:
    """The β schedule, iteration caps, budget and objective aggregation (design §7.3, §9)."""

    betas: tuple[float, ...] = (8.0, 16.0, 32.0, 64.0, 128.0)
    iterations_per_beta: int | tuple[int, ...] = 30
    min_iterations: int = 8
    budget_min: float | None = None
    move: float = 0.2
    move_late: float = 0.1
    init: float = 0.5
    eta: float = 0.5
    tau: float = 0.05
    aggregate: str = "excitation"
    damping: float = 0.0  # G_d in units of 1/η0
    # Gray copper: "resistive" (G from G_min to G_max, 377 Ω/sq at ρ̄ = ½) or "reactive" (an
    # inductive sheet R_s − iωL(ρ̄), lossless when gray; `materials`). Binary designs are the
    # same copper and void either way.
    interpolation: str = "resistive"
    reactive_damping: float = 0.1  # loss tangent R/X of gray reactive pixels at f_ref
    conservative: bool = False  # CCSA/GCMMA inner iterations (extra forward runs)
    max_inner: int = 5
    filter_radius_mm: float | None = None  # default: from the rules (or 1.5 pitches)
    seed: str | None = None  # a closed-form start instead of the uniform `init` (`seeds`)
    binary_every: int = 5  # evaluate the binarized design every n iterations (0: epochs only)
    # Robust optimization (Hammond et al. §5.3): projection thresholds of extra designs (above
    # `eta`: eroded, below: dilated) whose objectives join the epigraph with the nominal ones.
    eta_variants: tuple[float, ...] = ()
    # The β from which the variants join the epigraph (below it the nominal design alone): at
    # β = 8 the eroded and dilated designs of a gray design are about as gray as it is, and they
    # triple an iteration's cost. Binarized designs are always judged with every variant.
    robust_from_beta: float = 0.0
    # Adaptive move limits with a step test on the epigraph value (`Epigraph.trust_step`): a
    # step whose true t exceeds t_k + trust_slack·max(1, |t_k|) is refused and retried with
    # half the move (one forward run per refusal); accepted improving steps grow the move
    # again up to the schedule's. Off by default.
    adaptive_move: bool = False
    trust_slack: float = 0.05
    # What the slack is measured from: "current", t_k (round 2's runs: steps may each raise t
    # by the slack, so t can creep up over an epoch), or "best", the best t of the β epoch so
    # far, with the slack scaled by β_a/β from the first adaptive β_a on (accepted points stay
    # within a shrinking slack of the epoch's best; design §24). Neither guarantees descent.
    trust_reference: str = "current"
    # With `adaptive_move`, the β from which the adaptive steps apply (epochs below it take plain
    # MMA steps at the schedule's move): free exploration while the design is gray, steps that
    # keep the epigraph value once it is nearly binary. 0: every epoch.
    adaptive_from_beta: float = 0.0
    # The objective of each β epoch (`Problem.evaluate`): "spec", the epigraph of the
    # requirements (design §9), or "radiation", per lower bound on a radiated fraction the band
    # average log(1 + R̄) − log η̄ of its port (R̄, η̄ the band means of |S_jj|² and η_j; Lu,
    # Wadbro, Lundström, Starck, Berggren and Hassan, arXiv 2608.05712, Eq. 3). The radiation
    # objective rewards radiated power and saturates on reflection, so absorbing gray copper
    # does not pay: from a gray start it grows a radiator, which the spec's epochs then shape
    # to the band. Empty: "spec" in every epoch. Binarized designs (the export's choice) are
    # always judged by the spec.
    epoch_objectives: tuple[str, ...] = ()
    # Frequency continuation: each β epoch solves at the objective frequencies times its factor
    # (judged against the requirements as written), e.g. (0.925, 1, 1, 1, 1): the radiator grows
    # against a band below the target first and is trimmed to the band after. Empty: 1 in every
    # epoch. Binarized designs are always judged at the nominal frequencies.
    epoch_frequency_scale: tuple[float, ...] = ()
    # One-port specs: judge the reflection renormalized to this real reference (Ω) instead of
    # the feed's Z_c, i.e. what the validator reports (50 Ω). The feeds are whole cells wide, so
    # Z_c is a few per cent off 50 Ω (47.5 Ω for the antenna's 11-cell feed), which moves a
    # −10 dB reflection by up to 0.6 dB. None: the feed's Z_c (design §5.4).
    reference_ohm: float | None = None


@dataclass(frozen=True)
class SolverSpec:
    """Solver settings of the optimization runs.

    `backend`: "auto" (native where its library loads, else the numpy reference: the same
    float64 values), "native", "numpy" or "torch"; `dtype` float64 or float32
    (`fdtd.native_kernel.choose`, docs/rf-solver-backends.md)."""

    backend: str = "auto"
    dtype: str = "float64"
    tol: float = 1e-3
    adjoint_tol: float = 1e-3
    decimation: str | int = "auto"
    max_steps: int = 200_000
    threads: int = 4
    sweep_points: int = 101
    # The subcell correction of the copper's edges (`yapnr.rf.edges`): static edge-field factors
    # on ε and μ next to every copper edge, so that the coarse grid's lines and resonators
    # agree with finer grids. Off by default.
    edge_correction: bool = False
    # The port source: "static" (J from the strip's static field in air) or "mode" (J from the
    # line's discrete mode, `yapnr.rf.modes`, which removes the excited port's incident-wave
    # bias of up to 2 %).
    port_source: str = "static"
    # The line ports' waves: "modal" (the transverse plane projected on the feed's discrete
    # mode, `ports.ModalPlane`, design §25.1) or "vi" (round 2's voltage and current samples
    # with the calibration's Z_c; radiation reaching the V/I plane biases them by up to 5 %
    # next to an antenna). The default changed to "modal" with the corrected radiation box; on
    # circuits the two agree to about 1e-4.
    port_extraction: str = "modal"


# Solver options added after the first cases, with their defaults (`Spec.to_dict`).
_SOLVER_NEW = {"edge_correction": False, "port_source": "static", "port_extraction": "modal"}
_OPTIMIZER_NEW = {
    "seed": None,
    "adaptive_move": False,
    "trust_slack": 0.05,
    "trust_reference": "current",
    "adaptive_from_beta": 0.0,
    "robust_from_beta": 0.0,
    "epoch_objectives": [],
    "epoch_frequency_scale": [],
    "reference_ohm": None,
}
OBJECTIVES = ("spec", "radiation")


# -- requirements -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    """One transfer-function requirement over a band.

    quantity: "s" (|S_ij| in dB), "phase" (∠S_ij in degrees), "radiated" (radiated fraction
      of the power incident at port j) or "absorbed" (the fraction of the power incident at
      port j that the lumped resistor `element` dissipates) or "loss" (the fraction of the
      power incident at port j that leaves neither through a port nor into a lumped resistor).
    ports: (i, j) for S_ij; (j,) for the radiated, absorbed and lost fractions.
    bound: "max" or "min" ("target" for phases).
    limit: a number, or a piecewise-linear mask ((f_GHz, value), ...).
    """

    quantity: str
    ports: tuple[int, ...]
    bound: str
    limit: float | tuple[tuple[float, float], ...]
    band: str
    scale: float | None = None
    tol_deg: float | None = None
    element: str | None = None

    def __post_init__(self) -> None:
        if self.quantity not in ("s", "phase", "radiated", "absorbed", "loss"):
            raise ValueError(f"unknown quantity {self.quantity!r}")
        if self.bound not in ("max", "min", "target"):
            raise ValueError(f"unknown bound {self.bound!r}")
        if (self.quantity == "phase") != (self.bound == "target"):
            raise ValueError("phase requirements (and only they) have a target")
        n = 1 if self.quantity in ("radiated", "absorbed", "loss") else 2
        if len(self.ports) != n:
            raise ValueError(f"{self.quantity} requirement needs {n} port numbers")
        if self.quantity == "phase" and not (self.tol_deg and 0 < self.tol_deg < 180):
            raise ValueError("phase requirements need 0 < tol_deg < 180")
        if (self.quantity == "absorbed") != (self.element is not None):
            raise ValueError("absorbed requirements (and only they) name a lumped element")

    @property
    def excitation(self) -> int:
        """The port whose excitation the requirement is measured with."""
        return self.ports[-1]

    @property
    def default_scale(self) -> float:
        if self.quantity in ("radiated", "absorbed", "loss"):
            return 0.1
        if self.quantity == "phase":
            return 1.0
        return 10.0 if self.bound == "max" else 1.0

    @property
    def scale_value(self) -> float:
        return float(self.scale) if self.scale else self.default_scale

    def limit_at(self, freqs_hz) -> np.ndarray:
        """The limit at the given frequencies (constant or interpolated mask)."""
        f = np.asarray(freqs_hz, dtype=np.float64)
        if isinstance(self.limit, tuple):
            pts = np.asarray(self.limit, dtype=np.float64)
            return np.interp(f, pts[:, 0] * 1e9, pts[:, 1])
        return np.full(f.shape, float(self.limit))

    @property
    def label(self) -> str:
        if self.quantity == "radiated":
            op = "≥" if self.bound == "min" else "≤"
            return f"eta{self.ports[0]} {op} {self._limit_text()} @ {self.band}"
        if self.quantity == "absorbed":
            op = "≥" if self.bound == "min" else "≤"
            return f"{self.element}/P{self.ports[0]} {op} {self._limit_text()} @ {self.band}"
        if self.quantity == "loss":
            op = "≥" if self.bound == "min" else "≤"
            return f"loss{self.ports[0]} {op} {self._limit_text()} @ {self.band}"
        sij = f"S{self.ports[0]}{self.ports[1]}"
        if self.quantity == "phase":
            return f"∠{sij} = {self.limit:g}° ± {self.tol_deg:g}° @ {self.band}"
        op = "≤" if self.bound == "max" else "≥"
        return f"|{sij}| {op} {self._limit_text()} dB @ {self.band}"

    def _limit_text(self) -> str:
        return "mask" if isinstance(self.limit, tuple) else f"{self.limit:g}"

    # -- (de)serialization ----------------------------------------------------------------------

    def to_dict(self) -> dict:
        out: dict = {}
        if self.quantity == "radiated":
            out["radiated"] = self.ports[0]
            out[self.bound] = self.limit
        elif self.quantity == "absorbed":
            out["absorbed"] = self.ports[0]
            out["element"] = self.element
            out[self.bound] = self.limit
        elif self.quantity == "loss":
            out["loss"] = self.ports[0]
            out[self.bound] = self.limit
        elif self.quantity == "phase":
            out["s"] = list(self.ports)
            out["phase_deg"] = self.limit
            out["tol_deg"] = self.tol_deg
        else:
            out["s"] = list(self.ports)
            if isinstance(self.limit, tuple):
                key = "mask_db" if self.bound == "max" else "min_mask_db"
                out[key] = [list(p) for p in self.limit]
            else:
                out[f"{self.bound}_db"] = self.limit
        out["band"] = self.band
        if self.scale:
            out["scale"] = self.scale
        return out

    @classmethod
    def from_dict(cls, d: dict) -> list["Requirement"]:
        """One requirement, or two for `between_db` (a pattern requirement for the keys of
        `patterns.QUANTITIES`)."""
        from yapnr.rf.patterns import PatternRequirement, is_pattern

        if is_pattern(d):
            return [PatternRequirement.from_dict(d)]
        d = dict(d)
        band = d.pop("band", None)
        if band is None:
            raise ValueError(f"requirement {d} needs a band")
        scale = d.pop("scale", None)
        if "radiated" in d:
            port = int(d.pop("radiated"))
            (bound,) = [k for k in ("min", "max") if k in d]
            return [cls("radiated", (port,), bound, float(d[bound]), band, scale)]
        if "loss" in d:
            port = int(d.pop("loss"))
            (bound,) = [k for k in ("min", "max") if k in d]
            return [cls("loss", (port,), bound, float(d[bound]), band, scale)]
        if "absorbed" in d:
            port = int(d.pop("absorbed"))
            (bound,) = [k for k in ("min", "max") if k in d]
            return [
                cls("absorbed", (port,), bound, float(d[bound]), band, scale, None, d["element"])
            ]
        ports = tuple(int(p) for p in d.pop("s"))
        if "phase_deg" in d:
            return [cls("phase", ports, "target", float(d["phase_deg"]), band, scale, d["tol_deg"])]
        if "between_db" in d:
            lo, hi = d["between_db"]
            return [
                cls("s", ports, "min", float(lo), band, scale),
                cls("s", ports, "max", float(hi), band, scale),
            ]
        for key, bound in (("max_db", "max"), ("min_db", "min")):
            if key in d:
                return [cls("s", ports, bound, float(d[key]), band, scale)]
        for key, bound in (("mask_db", "max"), ("min_mask_db", "min")):
            if key in d:
                mask = tuple((float(f), float(v)) for f, v in d[key])
                return [cls("s", ports, bound, mask, band, scale)]
        raise ValueError(f"unknown requirement form {d}")


class S:
    """Builder of S-parameter requirements: `S(2, 1).at_least_db(-3.28, band="pass")`."""

    def __init__(self, i: int, j: int):
        self.ports = (int(i), int(j))

    def at_most_db(self, limit: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement("s", self.ports, "max", float(limit), band, scale)

    def at_least_db(self, limit: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement("s", self.ports, "min", float(limit), band, scale)

    def between_db(self, lo: float, hi: float, *, band: str, scale: float | None = None):
        return (
            Requirement("s", self.ports, "min", float(lo), band, scale),
            Requirement("s", self.ports, "max", float(hi), band, scale),
        )

    def mask_db(self, points, *, band: str, kind: str = "max", scale: float | None = None):
        """A piecewise-linear limit [(f_GHz, dB), ...]: an upper (`kind="max"`) or lower mask."""
        mask = tuple((float(f), float(v)) for f, v in points)
        return Requirement("s", self.ports, kind, mask, band, scale)

    def phase_deg(self, target: float, *, tol: float, band: str) -> Requirement:
        return Requirement("phase", self.ports, "target", float(target), band, None, float(tol))


class RadiatedFraction:
    """Builder of radiated-fraction requirements: `RadiatedFraction(1).at_least(0.7, band=...)`."""

    def __init__(self, port: int):
        self.port = int(port)

    def at_least(self, value: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement("radiated", (self.port,), "min", float(value), band, scale)

    def at_most(self, value: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement("radiated", (self.port,), "max", float(value), band, scale)


class Absorbed:
    """Builder of absorbed-fraction requirements: the fraction of the power incident at port
    `port` that the lumped resistor `element` dissipates, e.g. a Wilkinson's isolation resistor
    with an output port excited: `Absorbed("R1", 2).at_least(0.4, band=...)`."""

    def __init__(self, element: str, port: int):
        self.element = str(element)
        self.port = int(port)

    def at_least(self, value: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement(
            "absorbed", (self.port,), "min", float(value), band, scale, None, self.element
        )

    def at_most(self, value: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement(
            "absorbed", (self.port,), "max", float(value), band, scale, None, self.element
        )


class Loss:
    """Builder of lost-fraction requirements: the fraction of the power incident at port `port`
    that leaves neither through a port nor into a lumped resistor (radiation, and dissipation in
    the copper, gray copper included, and the substrate): `Loss(2).at_most(0.08, band=...)`."""

    def __init__(self, port: int):
        self.port = int(port)

    def at_most(self, value: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement("loss", (self.port,), "max", float(value), band, scale)

    def at_least(self, value: float, *, band: str, scale: float | None = None) -> Requirement:
        return Requirement("loss", (self.port,), "min", float(value), band, scale)


def _flatten(reqs) -> tuple:
    from yapnr.rf.patterns import PatternRequirement

    out = []
    for r in reqs:
        if isinstance(r, (Requirement, PatternRequirement)):
            out.append(r)
        else:
            out.extend(_flatten(r))
    return tuple(out)


# -- the spec ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    name: str
    stackup: StackupSpec
    grid: GridSpec
    design_region: tuple[float, float, float, float]  # x0, x1, y0, y1 (mm)
    ports: tuple[Port, ...]
    bands: dict
    requirements: tuple[Requirement, ...]
    symmetry: str = "none"
    rules: Rules = field(default_factory=Rules)
    fixed: tuple[FixedRegion, ...] = ()
    radiation: RadiationBox | None = None
    optimizer: OptimizerSpec = field(default_factory=OptimizerSpec)
    solver: SolverSpec = field(default_factory=SolverSpec)
    lumped: tuple[Lumped, ...] = ()
    # A board model (design §26): a finite board in free space or on an infinite ground, with
    # lumped ports; needed by every pattern requirement (`patterns`).
    board: BoardSpec | None = None
    # The far field's frame ({"frame": {"axis": "+z", "zero": "+x"}}: θ from the axis, φ from
    # `zero`), and the named target densities of `shape` requirements (`patterns.Target`).
    far_field: dict | None = None
    patterns: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirements", _flatten(self.requirements))
        object.__setattr__(self, "design_region", tuple(float(v) for v in self.design_region))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "fixed", tuple(self.fixed))
        object.__setattr__(self, "lumped", tuple(self.lumped))
        self.validate()

    def validate(self) -> None:
        x0, x1, y0, y1 = self.design_region
        if not (x1 > x0 and y1 > y0):
            raise ValueError("design_region must be (x0, x1, y0, y1) with x1 > x0, y1 > y0")
        pitch = self.grid.pitch_mm
        for lo, hi in ((x0, x1), (y0, y1)):
            cells = (hi - lo) / pitch
            if abs(cells - round(cells)) > 1e-6:
                raise ValueError("the design region's sides must be whole pitches long")
        numbers = [p.n for p in self.ports]
        if sorted(numbers) != list(range(1, len(numbers) + 1)):
            raise ValueError("ports must be numbered 1..N")
        for p in self.ports:
            if p.kind == "lumped":
                if p.x_mm is None or p.y_mm is None or len(p.x_mm) != 2 or len(p.y_mm) != 2:
                    raise ValueError(f"port {p.n}: a lumped port needs x_mm and y_mm [lo, hi]")
                if p.ohms is not None and not p.ohms > 0:
                    raise ValueError(f"port {p.n}: ohms must be positive")
                if self.board is None:
                    raise ValueError(f"port {p.n}: lumped ports need a board model (`board`)")
            elif p.kind == "line":
                if p.side not in SIDES or p.at_mm is None:
                    raise ValueError(f"port {p.n}: side must be one of {SIDES}, with at_mm")
                if self.board is not None:
                    raise ValueError(
                        f"port {p.n}: a board model's ports are lumped (a line port's feed "
                        "into the CPML would cross the Huygens box, design §26.1)"
                    )
            else:
                raise ValueError(f"port {p.n}: kind must be 'line' or 'lumped'")
        self._validate_board()
        if not self.requirements:
            raise ValueError("a spec needs at least one requirement")
        for r in self.requirements:
            if r.band not in self.bands:
                raise ValueError(f"requirement {r.label}: unknown band {r.band!r}")
            for n in r.ports:
                if n not in numbers:
                    raise ValueError(f"requirement {r.label}: unknown port {n}")
        if self.symmetry not in ("none", "mirror_x", "mirror_y", "mirror_xy"):
            raise ValueError(f"unknown symmetry {self.symmetry!r}")
        if self.optimizer.aggregate not in ("excitation", "none"):
            raise ValueError("optimizer.aggregate must be 'excitation' or 'none'")
        for el in self.lumped:
            if el.axis not in ("x", "y") or not el.ohms > 0 or not el.pad_mm > 0:
                raise ValueError(f"lumped {el.name}: axis x or y, ohms > 0 and pad_mm > 0")
        names = [el.name for el in self.lumped]
        if len(set(names)) != len(names):
            raise ValueError("lumped elements need distinct names")
        for r in self.requirements:
            if r.quantity == "absorbed" and r.element not in names:
                raise ValueError(f"requirement {r.label}: unknown lumped element {r.element!r}")
        eo = self.optimizer.epoch_objectives
        if eo:
            if len(eo) != len(self.optimizer.betas) or any(o not in OBJECTIVES for o in eo):
                raise ValueError(
                    f"optimizer.epoch_objectives: one of {OBJECTIVES} per β epoch, or none"
                )
            if "radiation" in eo and not any(
                r.quantity == "radiated" and r.bound == "min" for r in self.requirements
            ):
                raise ValueError(
                    "the radiation objective needs a lower bound on a radiated fraction"
                )
        ref = self.optimizer.reference_ohm
        if ref is not None and (len(self.ports) != 1 or not ref > 0):
            raise ValueError("optimizer.reference_ohm: a positive reference, one-port specs only")
        if self.solver.port_extraction not in ("modal", "vi"):
            raise ValueError("solver.port_extraction must be 'modal' or 'vi'")
        if self.solver.backend not in ("auto", "native", "numpy", "torch"):
            raise ValueError("solver.backend must be 'auto', 'native', 'numpy' or 'torch'")
        try:
            dtype_ok = np.dtype(self.solver.dtype).name in ("float64", "float32")
        except TypeError:
            dtype_ok = False
        if not dtype_ok:
            raise ValueError("solver.dtype must be 'float64' or 'float32'")
        if self.optimizer.trust_reference not in ("current", "best"):
            raise ValueError("optimizer.trust_reference must be 'current' or 'best'")
        fs = self.optimizer.epoch_frequency_scale
        if fs and (len(fs) != len(self.optimizer.betas) or not all(0.5 < f < 2.0 for f in fs)):
            raise ValueError("optimizer.epoch_frequency_scale: one factor in (0.5, 2) per β epoch")
        if any(r.quantity == "radiated" for r in self.requirements) and self.radiation is None:
            object.__setattr__(self, "radiation", RadiationBox())

    def _validate_board(self) -> None:
        from yapnr.rf.patterns import PatternRequirement, _frame, parse_target

        pats = [r for r in self.requirements if isinstance(r, PatternRequirement)]
        b = self.board
        if b is None:
            if pats:
                raise ValueError(
                    f"requirement {pats[0].label}: pattern and efficiency requirements need a "
                    "board model (`board`): the infinite substrate has no far field (its "
                    "surface wave never leaves a closed surface; design §25.4)"
                )
            if self.far_field is not None or self.patterns:
                raise ValueError("far_field and patterns need a board model (`board`)")
            return
        (x0, x1), (y0, y1) = sorted(b.x_mm), sorted(b.y_mm)
        if not (x1 > x0 and y1 > y0):
            raise ValueError("board: x_mm and y_mm must be [lo, hi] with hi > lo")
        if b.thickness_mm is not None and b.thickness_mm < self.stackup.h_mm - 1e-12:
            raise ValueError("board.thickness_mm must be at least stackup.h_mm")
        if b.infinite:
            if b.thickness_mm not in (None, self.stackup.h_mm):
                raise ValueError("board: over an infinite ground the block is the substrate h")
        else:
            g = b.ground
            if (g.x_mm is None) != (g.y_mm is None):
                raise ValueError("board.ground: x_mm and y_mm together")
        if self.radiation is not None and (
            self.radiation.offset_mm is not None or self.radiation.height_mm is not None
        ):
            raise ValueError(
                "radiation.offset_mm and height_mm do not apply to a board model: its Huygens "
                "box encloses the whole board (radiation.clearance_cells)"
            )
        if self.optimizer.seed is not None:
            raise ValueError("optimizer.seed: the closed-form starts need line ports")
        if self.optimizer.reference_ohm is not None:
            raise ValueError("optimizer.reference_ohm: lumped ports are referenced to their ohms")
        if self.far_field is not None:
            unknown = set(self.far_field) - {"frame"}
            if unknown:
                raise ValueError(f"far_field: unknown keys {sorted(unknown)}")
            _frame(self.far_field.get("frame"))
        for name, t in self.patterns.items():
            parse_target(t.spec if hasattr(t, "spec") else t)
        for r in pats:
            if r.quantity == "shape" and r.spec["target"] not in self.patterns:
                raise ValueError(f"requirement {r.label}: unknown target {r.spec['target']!r}")

    @property
    def frame(self):
        """The far field's frame (`farfield.Frame`)."""
        from yapnr.rf.patterns import _frame

        return _frame((self.far_field or {}).get("frame"))

    # -- derived --------------------------------------------------------------------------------

    def objective_frequencies(self) -> np.ndarray:
        """The union of the samples of every band used by a requirement (Hz, sorted)."""
        used = sorted({r.band for r in self.requirements})
        f = np.concatenate([self.bands[b].frequencies() for b in used])
        f = np.sort(f)
        keep = np.concatenate([[True], np.diff(f) > 1e-6 * f[1:]])
        return f[keep]

    def band_mask(self, band: str, freqs) -> np.ndarray:
        """True at the frequencies (Hz) that are samples of `band`."""
        b = self.bands[band].frequencies()
        f = np.asarray(freqs, dtype=np.float64)
        return np.any(np.abs(f[:, None] - b[None, :]) <= 1e-6 * f[:, None], axis=1)

    @property
    def excitations(self) -> tuple[int, ...]:
        return tuple(sorted({r.excitation for r in self.requirements}))

    def replace(self, **changes) -> "Spec":
        return replace(self, **changes)

    # -- (de)serialization ----------------------------------------------------------------------

    def to_dict(self) -> dict:
        def clean(obj):
            d = asdict(obj)
            return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}

        out = {
            "schema": SCHEMA,
            "name": self.name,
            "stackup": clean(self.stackup),
            "grid": clean(self.grid),
            "design_region": {
                "x_mm": list(self.design_region[:2]),
                "y_mm": list(self.design_region[2:]),
            },
            "symmetry": self.symmetry,
            "rules": clean(self.rules),
            "ports": [p.to_dict() for p in self.ports],
            "bands": {k: _band_dict(v) for k, v in sorted(self.bands.items())},
            "requirements": [r.to_dict() for r in self.requirements],
            "fixed": [
                {"x_mm": list(f.x_mm), "y_mm": list(f.y_mm), "value": f.value} for f in self.fixed
            ],
            # `seed` and later options are left out at their defaults, so specs written before
            # them keep their hashes.
            "optimizer": {
                k: v
                for k, v in clean(self.optimizer).items()
                if not (k in _OPTIMIZER_NEW and v == _OPTIMIZER_NEW[k])
            },
            # Solver options added after the first cases are left out at their defaults, so
            # specs written before them keep their hashes.
            "solver": {
                k: v
                for k, v in clean(self.solver).items()
                if not (k in _SOLVER_NEW and v == _SOLVER_NEW[k])
            },
        }
        if self.radiation is not None:
            out["radiation"] = self.radiation.to_dict()
        if self.lumped:
            out["lumped"] = [clean(el) for el in self.lumped]
        if self.board is not None:
            out["board"] = self.board.to_dict()
        if self.far_field is not None:
            out["far_field"] = self.far_field
        if self.patterns:
            out["patterns"] = {k: v.spec for k, v in sorted(self.patterns.items())}
        return out

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @classmethod
    def from_dict(cls, d: dict) -> "Spec":
        d = dict(d)
        schema = d.pop("schema", SCHEMA)
        if schema != SCHEMA:
            raise ValueError(f"unsupported spec schema {schema!r}")
        region = d["design_region"]
        if isinstance(region, dict):
            region = tuple(region["x_mm"]) + tuple(region["y_mm"])
        ports = []
        for p in d["ports"]:
            p = dict(p)
            if p.get("kind", "line") == "lumped":
                unknown = set(p) - {"n", "kind", "x_mm", "y_mm", "ohms"}
                if unknown:
                    raise ValueError(f"port {p.get('n')}: unknown keys {sorted(unknown)}")
                ports.append(
                    Port(
                        int(p["n"]),
                        kind="lumped",
                        x_mm=tuple(float(v) for v in p["x_mm"]),
                        y_mm=tuple(float(v) for v in p["y_mm"]),
                        ohms=float(p.get("ohms", 50.0)),
                    )
                )
                continue
            width = p.pop("width_cells", None)
            if width in (None, "auto"):
                width = None
            p.pop("kind", None)
            ports.append(Port(int(p["n"]), str(p["side"]), float(p["at_mm"]), width))
        bands = {}
        for name, b in d["bands"].items():
            if "ghz" in b:
                lo, hi = b["ghz"]
                bands[name] = Band(float(lo), float(hi), int(b.get("points", 5)))
            else:
                pts = b.get("ghz_points")
                bands[name] = Band(
                    float(b["lo_ghz"]),
                    float(b["hi_ghz"]),
                    int(b.get("points", 5)),
                    tuple(float(v) for v in pts) if pts else None,
                )
        reqs = []
        for r in d["requirements"]:
            reqs.extend(Requirement.from_dict(r))
        fixed = tuple(
            FixedRegion(tuple(f["x_mm"]), tuple(f["y_mm"]), float(f.get("value", 0.0)))
            for f in d.get("fixed", ())
        )
        rad = d.get("radiation")
        return cls(
            name=str(d["name"]),
            stackup=_build(StackupSpec, d["stackup"]),
            grid=_build(GridSpec, d["grid"]),
            design_region=tuple(float(v) for v in region),
            ports=tuple(ports),
            bands=bands,
            requirements=tuple(reqs),
            symmetry=str(d.get("symmetry", "none")),
            rules=_build(Rules, d.get("rules", {})),
            fixed=fixed,
            radiation=None if rad is None else _build(RadiationBox, rad),
            optimizer=_build(OptimizerSpec, d.get("optimizer", {})),
            solver=_build(SolverSpec, d.get("solver", {})),
            lumped=tuple(_build(Lumped, el) for el in d.get("lumped", ())),
            board=None if d.get("board") is None else BoardSpec.from_dict(d["board"]),
            far_field=d.get("far_field"),
            patterns=_targets(d.get("patterns", {})),
        )

    @classmethod
    def load(cls, path: str) -> "Spec":
        """Read a spec from a YAML (.yaml/.yml) or JSON file."""
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if path.endswith((".yaml", ".yml")):
            import yaml

            return cls.from_dict(yaml.safe_load(text))
        return cls.from_dict(json.loads(text))


def _targets(d: dict) -> dict:
    from yapnr.rf.patterns import parse_target

    return {str(k): parse_target(v) for k, v in d.items()}


def _band_dict(b: Band) -> dict:
    out = {"lo_ghz": b.lo_ghz, "hi_ghz": b.hi_ghz, "points": b.points}
    if b.ghz_points:
        out["ghz_points"] = list(b.ghz_points)
    return out


def _build(cls, d: dict):
    """A dataclass from a dict, ignoring nothing: unknown keys are an error."""
    names = {f.name for f in fields(cls)}
    unknown = set(d) - names
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
    return cls(**{k: _tuple(v) for k, v in d.items()})
