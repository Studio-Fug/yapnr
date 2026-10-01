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
    calibration for 50 Ω)."""

    n: int
    side: str
    at_mm: float
    width_cells: int | None = None


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
class RadiationBox:
    """The radiated-power box (design §5.6): `offset_mm` beyond the design region, `height_mm`
    above the copper; feed windows (defaults 2h and 3h)."""

    offset_mm: float | None = None
    height_mm: float | None = None
    window_margin_mm: float | None = None
    window_height_mm: float | None = None


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


@dataclass(frozen=True)
class SolverSpec:
    """Solver settings of the optimization runs."""

    backend: str = "torch"
    dtype: str = "float32"
    tol: float = 1e-3
    adjoint_tol: float = 1e-3
    decimation: str | int = "auto"
    max_steps: int = 200_000
    threads: int = 4
    sweep_points: int = 101


# -- requirements -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    """One transfer-function requirement over a band.

    quantity: "s" (|S_ij| in dB), "phase" (∠S_ij in degrees) or "radiated" (radiated fraction
      of the power incident at port j).
    ports: (i, j) for S_ij; (j,) for the radiated fraction.
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

    def __post_init__(self) -> None:
        if self.quantity not in ("s", "phase", "radiated"):
            raise ValueError(f"unknown quantity {self.quantity!r}")
        if self.bound not in ("max", "min", "target"):
            raise ValueError(f"unknown bound {self.bound!r}")
        if (self.quantity == "phase") != (self.bound == "target"):
            raise ValueError("phase requirements (and only they) have a target")
        n = 1 if self.quantity == "radiated" else 2
        if len(self.ports) != n:
            raise ValueError(f"{self.quantity} requirement needs {n} port numbers")
        if self.quantity == "phase" and not (self.tol_deg and 0 < self.tol_deg < 180):
            raise ValueError("phase requirements need 0 < tol_deg < 180")

    @property
    def excitation(self) -> int:
        """The port whose excitation the requirement is measured with."""
        return self.ports[-1]

    @property
    def default_scale(self) -> float:
        if self.quantity == "radiated":
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
        """One requirement, or two for `between_db`."""
        d = dict(d)
        band = d.pop("band", None)
        if band is None:
            raise ValueError(f"requirement {d} needs a band")
        scale = d.pop("scale", None)
        if "radiated" in d:
            port = int(d.pop("radiated"))
            (bound,) = [k for k in ("min", "max") if k in d]
            return [cls("radiated", (port,), bound, float(d[bound]), band, scale)]
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


def _flatten(reqs) -> tuple[Requirement, ...]:
    out = []
    for r in reqs:
        if isinstance(r, Requirement):
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirements", _flatten(self.requirements))
        object.__setattr__(self, "design_region", tuple(float(v) for v in self.design_region))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "fixed", tuple(self.fixed))
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
            if p.side not in SIDES:
                raise ValueError(f"port {p.n}: side must be one of {SIDES}")
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
        if any(r.quantity == "radiated" for r in self.requirements) and self.radiation is None:
            object.__setattr__(self, "radiation", RadiationBox())

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
            "ports": [clean(p) for p in self.ports],
            "bands": {k: _band_dict(v) for k, v in sorted(self.bands.items())},
            "requirements": [r.to_dict() for r in self.requirements],
            "fixed": [
                {"x_mm": list(f.x_mm), "y_mm": list(f.y_mm), "value": f.value} for f in self.fixed
            ],
            # `seed` is left out when unset, so specs written before it keep their hashes.
            "optimizer": {
                k: v for k, v in clean(self.optimizer).items() if not (k == "seed" and v is None)
            },
            "solver": clean(self.solver),
        }
        if self.radiation is not None:
            out["radiation"] = clean(self.radiation)
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
            width = p.pop("width_cells", None)
            if width in (None, "auto"):
                width = None
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
