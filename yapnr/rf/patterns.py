"""Radiation-pattern requirements, direction sets and target densities (design §26.3).

A board model (`spec.board`) has a far field (`farfield`), so its specs may bound gain,
pattern and efficiency per excitation port j, over a band, in the angles of the spec's frame
(`far_field.frame`, default θ from +z, φ from +x) or a requirement's own `frame`:

    {gain: j, kind: realized|gain|directivity, pol: total|co|cross|theta|phi|rhcp|lhcp,
     min_dbi | max_dbi | min_mask_dbi | max_mask_dbi, directions, reference_phi_deg}
    {ripple: j, max_db, directions, kind, pol}
    {hpbw: j, cut_phi_deg, boresight_theta_deg, between_deg: [b1, b2], kind, pol}
    {front_to_back: j, min_db, front, back, kind, pol}
    {cross_pol: j, max_db, directions, reference_phi_deg}
    {shape: j, target, max_rms_db, form: kl|log_l2, weight: uniform|target}
    {efficiency: j, kind: radiation|total, min}

Each yields normalized violations φ ≤ 0 per frequency and direction; they join the
excitation's group smooth maximum like the S-parameter terms (`objectives`), one term per
direction, so the epigraph raises the worst direction (null filling for an omni antenna)
rather than the average. x = 10 log10 of the quantity, s the scale (default 1 dB, 3 dB for
cross-pol, 0.1 for efficiencies, 1 for shapes):

- gain: (L − x)/s (min) or (x − L)/s (max) per direction; masks are piecewise linear in the
  set's angle (θ along an elevation cut, φ along a conical cut);
- ripple: smooth max − smooth min of x over the set (log-sum-exp at τ = 0.2 dB), (R − L)/s;
- hpbw: x at θ_b ± b1/2 along the cut at least x(boresight) − 3 dB (not narrower than b1),
  at θ_b ± b2/2 at most x(boresight) − 3 dB (not wider than b2);
- front_to_back: x(front) − smooth max over the back set ≥ L;
- cross_pol: 10 log10(U_cross/U_co) ≤ L per direction (Ludwig-3 about `reference_phi_deg`);
- shape: the forward Kullback–Leibler divergence KL(q‖p) = Σ w q ln(q/p) of the target
  density q = D_t/4π from the floored pattern p (U plus the target's floor, normalized by the
  quadrature), against KL_tol = (max_rms_db/4.343)²/2 (for small deviations δ = ln(p/q),
  KL ≈ ½E_q[δ²]: one knob, a target-weighted RMS dB error); or the weighted mean square dB
  error ("log_l2") against max_rms_db². φ = value/tolerance − 1;
- efficiency: e_rad = P_rad/P_acc or e_tot = P_rad/P_inc against `min`.

Directions (`directions`, `front`, `back`): {point: {theta_deg, phi_deg}}, {cut: {theta_deg,
points | phi_deg: [a, b], step_deg}} (a conical cut), {cut: {phi_deg, theta_deg: [a, b],
step_deg}} (an elevation cut; negative θ is the far side, φ + 180°), {cone: {toward,
half_angle_deg, step_deg}}, "upper" or "sphere" (the far field's quadrature directions).

Targets (`patterns: {name: …}`), densities D_t(θ, φ) normalized by the quadrature to
∮ D_t dΩ = 4π over the far field's sphere (the upper hemisphere over an infinite ground), with
`floor_db` (default −20 dB relative to the peak): {grid: {theta_deg, phi_deg, dbi}} (bilinear,
periodic in φ), {sh: {lmax, coef}} (real orthonormal Y_lm of the power density, clipped at the
floor), {preset: beam, toward, hpbw_deg: h | [h_E, h_H], e_plane_phi_deg, back_db}
(cos^q of the angle from the beam, q = ln ½/ln cos(hpbw/2), per azimuth about the beam
q_E cos² + q_H sin²) and {preset: omni, axis, hpbw_deg: h | "dipole", tilt_deg} (cos^q of the
elevation about the axis, sin²θ for "dipole").
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.farfield import Frame, axis_vector

QUANTITIES = ("gain", "ripple", "hpbw", "front_to_back", "cross_pol", "shape", "efficiency")
GAIN_KINDS = ("realized", "gain", "directivity")
POLS = ("total", "co", "cross", "theta", "phi", "rhcp", "lhcp")
TAU_DB = 0.2  # smooth max/min temperature of ripple and front-to-back (dB)
DB = 10.0 / math.log(10.0)  # 4.343: dB per neper of power


def _frame(d) -> Frame:
    if d is None:
        return Frame()
    if isinstance(d, Frame):
        return d
    unknown = set(d) - {"axis", "zero"}
    if unknown:
        raise ValueError(f"frame: unknown keys {sorted(unknown)}")
    return Frame(_axis_key(d.get("axis", "+z")), _axis_key(d.get("zero", "+x")))


def _axis_key(v):
    return v if isinstance(v, str) else tuple(float(x) for x in v)


def frame_dict(frame: Frame) -> dict:
    return {"axis": _jsonable(frame.axis), "zero": _jsonable(frame.zero)}


def _jsonable(v):
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


# -- direction sets ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Directions:
    """A direction set in a frame: θ, φ (radians, (D,)) and the set's own angle (degrees, for
    masks), or the far field's quadrature ("upper", "sphere": resolved by the problem)."""

    theta: tuple = ()
    phi: tuple = ()
    angle: tuple = ()
    quadrature: str | None = None

    @property
    def size(self) -> int:
        return len(self.theta)


def _toward(v, frame: Frame) -> tuple[float, float]:
    """(θ, φ) radians in `frame` of "+z" … or {theta_deg, phi_deg}."""
    if isinstance(v, dict):
        return math.radians(float(v.get("theta_deg", 0.0))), math.radians(
            float(v.get("phi_deg", 0.0))
        )
    t, p = frame.angles(axis_vector(_axis_key(v)))
    return float(t), float(p)


def parse_directions(d, frame: Frame) -> Directions:
    if isinstance(d, str):
        if d not in ("upper", "sphere"):
            raise ValueError(f"unknown direction set {d!r}")
        return Directions(quadrature=d)
    if not isinstance(d, dict) or len(d) != 1:
        raise ValueError(f"a direction set has one key (point, cut, cone): {d!r}")
    ((kind, v),) = d.items()
    if kind == "point":
        t, p = math.radians(float(v["theta_deg"])), math.radians(float(v.get("phi_deg", 0.0)))
        return Directions((t,), (p,), (math.degrees(t),))
    if kind == "cut":
        if "phi_deg" in v and not isinstance(v["phi_deg"], (list, tuple)):
            # Elevation cut at fixed φ, θ over [a, b] (negative: the far side).
            lo, hi = (float(x) for x in v.get("theta_deg", (-90.0, 90.0)))
            ang = _samples(lo, hi, v, closed=True)
            p0 = math.radians(float(v["phi_deg"]))
            th = np.radians(np.abs(ang))
            ph = np.where(ang < 0, p0 + math.pi, p0)
            return Directions(tuple(th), tuple(ph), tuple(ang))
        if "theta_deg" in v and not isinstance(v["theta_deg"], (list, tuple)):
            # Conical cut at fixed θ, φ over [a, b] (default the full circle).
            if "phi_deg" in v:
                lo, hi = (float(x) for x in v["phi_deg"])
                ang = _samples(lo, hi, v, closed=True)
            else:
                n = int(v.get("points", 0)) or int(round(360.0 / float(v.get("step_deg", 10.0))))
                ang = np.arange(n) * 360.0 / n
            t = math.radians(float(v["theta_deg"]))
            return Directions(tuple(np.full(ang.size, t)), tuple(np.radians(ang)), tuple(ang))
        raise ValueError(f"cut: give theta_deg (conical) or phi_deg (elevation): {v!r}")
    if kind == "cone":
        t0, p0 = _toward(v.get("toward", "+z"), frame)
        half = float(v["half_angle_deg"])
        step = float(v.get("step_deg", 10.0))
        ring_frame_dirs = []
        rings = np.arange(0.0, half + 1e-9, step)
        for psi in rings:
            n = 1 if psi == 0 else max(4, int(round(360.0 * math.sin(math.radians(psi)) / step)))
            for k in range(n):
                ring_frame_dirs.append((math.radians(psi), 2 * math.pi * k / n))
        # Rotate (ψ, χ) about the cone's axis into the frame.
        axis = frame.directions(t0, p0)
        local = Frame(tuple(axis), tuple(_normal_to(axis)))
        dirs = local.directions([a for a, _ in ring_frame_dirs], [b for _, b in ring_frame_dirs])
        th, ph = frame.angles(dirs)
        ang = np.degrees([a for a, _ in ring_frame_dirs])
        return Directions(tuple(th), tuple(ph), tuple(ang))
    raise ValueError(f"unknown direction set {kind!r}")


def _normal_to(v) -> np.ndarray:
    v = np.asarray(v, float)
    trial = np.array([1.0, 0.0, 0.0]) if abs(v[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    n = trial - (trial @ v) * v
    return n / np.linalg.norm(n)


def _samples(lo: float, hi: float, v: dict, closed: bool) -> np.ndarray:
    if "points" in v:
        return np.linspace(lo, hi, int(v["points"]))
    step = float(v.get("step_deg", 5.0))
    n = int(round((hi - lo) / step))
    return lo + step * np.arange(n + 1) if closed else lo + step * np.arange(n)


# -- targets ----------------------------------------------------------------------------------


def _real_sh(lmax: int, theta, phi) -> np.ndarray:
    """Real orthonormal spherical harmonics Y_lm (l ≤ lmax, m = −l..l; (L+1)², D)."""
    from math import factorial

    x = np.cos(theta)
    out = []
    for ell in range(lmax + 1):
        for m in range(-ell, ell + 1):
            am = abs(m)
            p = _assoc_legendre(ell, am, x)
            norm = math.sqrt(
                (2 * ell + 1) / (4 * math.pi) * factorial(ell - am) / factorial(ell + am)
            )
            if m == 0:
                out.append(norm * p)
            elif m > 0:
                out.append(math.sqrt(2) * norm * p * np.cos(m * phi))
            else:
                out.append(math.sqrt(2) * norm * p * np.sin(am * phi))
    return np.array(out)


def _assoc_legendre(ell: int, m: int, x) -> np.ndarray:
    """P_l^m(x) without the Condon–Shortley phase."""
    x = np.asarray(x, float)
    pmm = np.ones_like(x)
    if m > 0:
        s = np.sqrt(np.maximum(0.0, 1 - x * x))
        f = 1.0
        for _ in range(m):
            pmm = pmm * f * s
            f += 2.0
    if ell == m:
        return pmm
    pm1 = x * (2 * m + 1) * pmm
    if ell == m + 1:
        return pm1
    for k in range(m + 2, ell + 1):
        pm1, pmm = ((2 * k - 1) * x * pm1 - (k + m - 1) * pmm) / (k - m), pm1
    return pm1


@dataclass(frozen=True)
class Target:
    """A target radiation density (see the module doc); `raw` its spec dict as JSON."""

    raw: str

    @property
    def spec(self) -> dict:
        return json.loads(self.raw)

    @property
    def floor_db(self) -> float:
        return float(self.spec.get("floor_db", -20.0))

    def density(self, dirs: np.ndarray, frame: Frame) -> np.ndarray:
        """The unnormalized density (≥ 0, before the floor) at global unit vectors (D, 3)."""
        d = self.spec
        if "grid" in d:
            g = d["grid"]
            th = np.asarray(g["theta_deg"], float)
            ph = np.asarray(g["phi_deg"], float)
            val = 10 ** (np.asarray(g["dbi"], float) / 10.0)
            if val.shape != (th.size, ph.size):
                raise ValueError("grid target: dbi must be (theta points, phi points)")
            t, p = frame.angles(dirs)
            t, p = np.degrees(t), np.degrees(p)
            # Bilinear in θ (clamped) and φ (periodic).
            it = np.clip(np.searchsorted(th, t) - 1, 0, max(th.size - 2, 0))
            if th.size == 1:
                wt = np.zeros_like(t)
                it = np.zeros(t.shape, int)
                it1 = it
            else:
                it1 = it + 1
                wt = np.clip((t - th[it]) / (th[it1] - th[it]), 0.0, 1.0)
            php = np.concatenate([ph, [ph[0] + 360.0]])
            pp = np.mod(p - ph[0], 360.0) + ph[0]
            ip = np.clip(np.searchsorted(php, pp) - 1, 0, ph.size - 1)
            wp = (pp - php[ip]) / (php[ip + 1] - php[ip])
            ip1 = (ip + 1) % ph.size
            v0 = val[it, ip] * (1 - wp) + val[it, ip1] * wp
            v1 = val[it1, ip] * (1 - wp) + val[it1, ip1] * wp
            return v0 * (1 - wt) + v1 * wt
        if "sh" in d:
            s = d["sh"]
            lmax = int(s["lmax"])
            coef = np.asarray(s["coef"], float)
            if coef.size != (lmax + 1) ** 2:
                raise ValueError(f"sh target: {(lmax + 1) ** 2} coefficients for lmax {lmax}")
            t, p = frame.angles(dirs)
            return coef @ _real_sh(lmax, t, p)
        preset = d.get("preset")
        if preset == "beam":
            t0, p0 = _toward(d.get("toward", "+z"), frame)
            b = frame.directions(t0, p0)
            h = d.get("hpbw_deg", 60.0)
            h_e, h_h = (float(h[0]), float(h[1])) if isinstance(h, (list, tuple)) else (h, h)
            q_e, q_h = _q(h_e), _q(h_h)
            e_dir = frame.directions(math.pi / 2, math.radians(float(d.get("e_plane_phi_deg", 0))))
            e_dir = e_dir - (e_dir @ b) * b
            if np.linalg.norm(e_dir) < 1e-9:
                e_dir = _normal_to(b)
            e_dir /= np.linalg.norm(e_dir)
            h_dir = np.cross(b, e_dir)
            c = np.clip(dirs @ b, -1.0, 1.0)
            xe, xh = dirs @ e_dir, dirs @ h_dir
            rho = np.maximum(xe * xe + xh * xh, 1e-30)
            q = q_e * xe * xe / rho + q_h * xh * xh / rho
            back = 10 ** (float(d.get("back_db", -15.0)) / 10.0)
            return np.where(c > 0, np.maximum(c, 0.0) ** q, 0.0) + back * (c <= 0)
        if preset == "omni":
            ax = axis_vector(_axis_key(d.get("axis", "+z")))
            c = np.clip(dirs @ ax, -1.0, 1.0)
            el = np.arcsin(c)  # elevation above the plane normal to the axis
            tilt = math.radians(float(d.get("tilt_deg", 0.0)))
            h = d.get("hpbw_deg", "dipole")
            if h == "dipole":
                return np.cos(el - tilt) ** 2
            q = _q(float(h))
            return np.maximum(np.cos(el - tilt), 0.0) ** q
        raise ValueError(f"unknown target {d!r}")

    def normalized(self, dirs, weights, frame: Frame) -> tuple[np.ndarray, float]:
        """(q, renormalization) at quadrature directions: the density floored at `floor_db`
        below its peak and scaled to Σ w q = 1 (q = D_t/4π); the renormalization is how far
        the given density's integral was from 4π (relative; a warning above 1 %)."""
        raw = self.density(dirs, frame)
        if np.any(raw < -1e-12 * np.max(np.abs(raw))):
            raw = np.maximum(raw, 0.0)
        floor = 10 ** (self.floor_db / 10.0) * float(np.max(raw))
        val = np.maximum(raw, floor)
        total = float(val @ weights)
        q = val / total
        # Presets are shapes (normalized here by definition); a grid or harmonics given in dBi
        # should integrate to 4π already.
        renorm = 0.0 if "preset" in self.spec else abs(total / (4 * math.pi) - 1.0)
        return q, renorm


def _q(hpbw_deg: float) -> float:
    """The exponent of cos^q with half power at hpbw/2 off the beam."""
    half = math.radians(float(hpbw_deg)) / 2.0
    if not 0 < half < math.pi / 2:
        raise ValueError("hpbw_deg must lie in (0, 180)")
    return math.log(0.5) / math.log(math.cos(half))


def parse_target(d: dict) -> Target:
    if not isinstance(d, dict):
        raise ValueError(f"a target is a dict: {d!r}")
    keys = set(d) - {"floor_db"}
    if not (keys <= {"grid"} or keys <= {"sh"} or "preset" in d):
        raise ValueError(f"target: one of grid, sh or preset: {d!r}")
    if "preset" in d and d["preset"] not in ("beam", "omni"):
        raise ValueError(f"unknown target preset {d['preset']!r}")
    allowed = {
        "beam": {"preset", "toward", "hpbw_deg", "e_plane_phi_deg", "back_db", "floor_db"},
        "omni": {"preset", "axis", "hpbw_deg", "tilt_deg", "floor_db"},
    }
    if "preset" in d and set(d) - allowed[d["preset"]]:
        raise ValueError(
            f"target {d['preset']}: unknown keys {sorted(set(d) - allowed[d['preset']])}"
        )
    t = Target(json.dumps(d, sort_keys=True))
    # Validate by evaluating once.
    t.density(np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0]]), Frame())
    return t


# -- requirements -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternRequirement:
    """A pattern requirement (see the module doc); `raw` is its spec dict as JSON. It offers
    the attributes the objective layer reads from `spec.Requirement` (quantity, ports, bound,
    band, excitation, label, scale_value)."""

    raw: str
    quantity: str = field(init=False)
    ports: tuple = field(init=False)
    band: str = field(init=False)
    bound: str = field(init=False)
    scale: float | None = field(init=False)
    element: None = field(init=False, default=None)
    tol_deg: None = field(init=False, default=None)

    def __post_init__(self) -> None:
        d = json.loads(self.raw)
        (q,) = [k for k in QUANTITIES if k in d]
        object.__setattr__(self, "quantity", q)
        object.__setattr__(self, "ports", (int(d[q]),))
        object.__setattr__(self, "band", str(d["band"]))
        object.__setattr__(self, "scale", d.get("scale"))
        bound = "min"
        if q == "gain":
            keys = [k for k in ("min_dbi", "max_dbi", "min_mask_dbi", "max_mask_dbi") if k in d]
            if len(keys) != 1:
                raise ValueError("a gain requirement has one of min_dbi, max_dbi, *_mask_dbi")
            bound = "max" if keys[0].startswith("max") else "min"
        elif q in ("ripple", "cross_pol"):
            bound = "max"
        object.__setattr__(self, "bound", bound)
        self._check(d)

    @property
    def spec(self) -> dict:
        return json.loads(self.raw)

    def _check(self, d: dict) -> None:
        q = self.quantity
        allowed = {
            "gain": {"kind", "pol", "min_dbi", "max_dbi", "min_mask_dbi", "max_mask_dbi"}
            | {"directions", "reference_phi_deg", "frame"},
            "ripple": {"kind", "pol", "max_db", "directions", "reference_phi_deg", "frame"},
            "hpbw": {"kind", "pol", "cut_phi_deg", "boresight_theta_deg", "between_deg"}
            | {"reference_phi_deg", "frame"},
            "front_to_back": {"kind", "pol", "min_db", "front", "back", "reference_phi_deg"}
            | {"frame"},
            "cross_pol": {"max_db", "directions", "reference_phi_deg", "frame"},
            "shape": {"target", "max_rms_db", "form", "weight"},
            "efficiency": {"kind", "min"},
        }[q] | {q, "band", "scale"}
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"{q} requirement: unknown keys {sorted(unknown)}")
        if q in ("gain", "ripple", "hpbw", "front_to_back"):
            if d.get("kind", "realized") not in GAIN_KINDS:
                raise ValueError(f"{q}: kind must be one of {GAIN_KINDS}")
            if d.get("pol", "total") not in POLS:
                raise ValueError(f"{q}: pol must be one of {POLS}")
        need = {
            "gain": ["directions"],
            "ripple": ["max_db", "directions"],
            "hpbw": ["between_deg"],
            "front_to_back": ["min_db", "front", "back"],
            "cross_pol": ["max_db", "directions"],
            "shape": ["target", "max_rms_db"],
            "efficiency": ["min"],
        }[q]
        for k in need:
            if k not in d:
                raise ValueError(f"{q} requirement needs {k}")
        if q == "efficiency" and d.get("kind", "total") not in ("radiation", "total"):
            raise ValueError("efficiency: kind must be 'radiation' or 'total'")
        if q == "shape":
            if d.get("form", "kl") not in ("kl", "log_l2"):
                raise ValueError("shape: form must be 'kl' or 'log_l2'")
            if d.get("weight", "uniform") not in ("uniform", "target"):
                raise ValueError("shape: weight must be 'uniform' or 'target'")
            if not float(d["max_rms_db"]) > 0:
                raise ValueError("shape: max_rms_db must be positive")
        if q == "hpbw":
            b1, b2 = (float(v) for v in d["between_deg"])
            if not 0 < b1 < b2 < 180:
                raise ValueError("hpbw: between_deg [b1, b2] with 0 < b1 < b2 < 180")
        frame = _frame(d.get("frame"))
        for key in ("directions", "front", "back"):
            if key in d:
                parse_directions(d[key], frame)

    @property
    def excitation(self) -> int:
        return self.ports[0]

    @property
    def default_scale(self) -> float:
        return {"cross_pol": 3.0, "efficiency": 0.1, "shape": 1.0}.get(self.quantity, 1.0)

    @property
    def scale_value(self) -> float:
        return float(self.scale) if self.scale else self.default_scale

    def frame(self, default: Frame) -> Frame:
        d = self.spec
        return _frame(d["frame"]) if "frame" in d else default

    @property
    def label(self) -> str:
        d = self.spec
        q, j = self.quantity, self.ports[0]
        if q == "gain":
            kind = {"realized": "Gr", "gain": "G", "directivity": "D"}[d.get("kind", "realized")]
            pol = d.get("pol", "total")
            key = [k for k in ("min_dbi", "max_dbi", "min_mask_dbi", "max_mask_dbi") if k in d][0]
            op = "≥" if key.startswith("min") else "≤"
            lim = "mask" if "mask" in key else f"{d[key]:g}"
            p = "" if pol == "total" else f",{pol}"
            return f"{kind}{j}{p} {op} {lim} dBi @ {self.band}"
        if q == "ripple":
            return f"ripple{j} ≤ {d['max_db']:g} dB @ {self.band}"
        if q == "hpbw":
            b1, b2 = d["between_deg"]
            return f"hpbw{j} {b1:g}–{b2:g}° @ {self.band}"
        if q == "front_to_back":
            return f"F/B{j} ≥ {d['min_db']:g} dB @ {self.band}"
        if q == "cross_pol":
            return f"xpol{j} ≤ {d['max_db']:g} dB @ {self.band}"
        if q == "shape":
            return f"shape{j}~{d['target']} ≤ {d['max_rms_db']:g} dB rms @ {self.band}"
        kind = d.get("kind", "total")
        return f"e_{'rad' if kind == 'radiation' else 'tot'}{j} ≥ {d['min']:g} @ {self.band}"

    def to_dict(self) -> dict:
        return self.spec

    @classmethod
    def from_dict(cls, d: dict) -> "PatternRequirement":
        if "band" not in d:
            raise ValueError(f"requirement {d} needs a band")
        keys = [k for k in QUANTITIES if k in d]
        if len(keys) != 1:
            raise ValueError(f"a pattern requirement has one of {QUANTITIES}: {d}")
        return cls(json.dumps(_jsonable(dict(d)), sort_keys=True))

    def limit_at(self, freqs_hz) -> np.ndarray:
        return np.zeros(np.asarray(freqs_hz).shape)


def is_pattern(d: dict) -> bool:
    return any(k in d for k in QUANTITIES)


# -- evaluation (torch) -----------------------------------------------------------------------


@dataclass
class DirectionTable:
    """The union of every direction a spec's pattern requirements evaluate (global unit
    vectors), with per requirement and role the rows into it, and the quadrature."""

    dirs: np.ndarray  # (D, 3)
    rows: dict  # (requirement index, role) → (rows (n,), Directions, Frame)
    quad_dirs: np.ndarray | None = None
    quad_weights: np.ndarray | None = None


def direction_table(spec, quad_dirs, quad_weights, default_frame: Frame) -> DirectionTable:
    """Collect the directions of every pattern requirement of `spec` (index k in
    spec.requirements). The quadrature directions come first (rows 0..Q−1) when any requirement
    needs them (shape, "upper"/"sphere" sets) or always when `quad_dirs` is given."""
    dirs = []
    rows = {}
    q_n = 0
    if quad_dirs is not None:
        dirs.append(np.asarray(quad_dirs, float))
        q_n = len(quad_dirs)
    offset = q_n
    for k, r in enumerate(spec.requirements):
        if not isinstance(r, PatternRequirement):
            continue
        d = r.spec
        frame = r.frame(default_frame)
        roles = {}
        if r.quantity in ("gain", "ripple", "cross_pol"):
            roles["dirs"] = parse_directions(d["directions"], frame)
        elif r.quantity == "front_to_back":
            roles["front"] = parse_directions(d["front"], frame)
            roles["back"] = parse_directions(d["back"], frame)
        elif r.quantity == "hpbw":
            b1, b2 = (float(v) for v in d["between_deg"])
            tb = float(d.get("boresight_theta_deg", 0.0))
            pc = math.radians(float(d.get("cut_phi_deg", 0.0)))
            ang = np.array([tb, tb - b1 / 2, tb + b1 / 2, tb - b2 / 2, tb + b2 / 2])
            th = np.radians(np.abs(ang))
            ph = np.where(ang < 0, pc + math.pi, pc)
            roles["dirs"] = Directions(tuple(th), tuple(ph), tuple(ang))
        elif r.quantity == "shape":
            roles["dirs"] = Directions(quadrature="sphere")
        for role, ds in roles.items():
            if ds.quadrature is not None:
                if quad_dirs is None:
                    raise ValueError(f"{r.label}: quadrature directions need the far field")
                q_frame = default_frame
                idx = np.arange(q_n)
                if ds.quadrature == "upper":
                    t, _ = frame.angles(quad_dirs)
                    idx = idx[t <= math.pi / 2 + 1e-12]
                theta, phi = q_frame.angles(np.asarray(quad_dirs)[idx])
                rows[(k, role)] = (
                    idx,
                    Directions(tuple(theta), tuple(phi), (), ds.quadrature),
                    frame,
                )
                continue
            v = frame.directions(np.array(ds.theta), np.array(ds.phi))
            dirs.append(v.reshape(-1, 3))
            idx = np.arange(offset, offset + len(ds.theta))
            offset += len(ds.theta)
            rows[(k, role)] = (idx, ds, frame)
    allv = np.concatenate(dirs) if dirs else np.zeros((0, 3))
    return DirectionTable(allv, rows, quad_dirs, quad_weights)


def _x_db(u, eps=1e-30):
    import torch

    return DB * torch.log(u + eps)


def _smax(x, tau: float, dim: int = -1):
    import torch

    return tau * torch.logsumexp(x / tau, dim=dim)


def phi_pattern(req: PatternRequirement, k: int, far: dict, table: DirectionTable, freqs):
    """φ (T, M) torch of a pattern requirement (T terms per frequency) from the excitation's
    far-field quantities `far` (`Problem.far_quantities`)."""
    import torch

    from yapnr.rf.farfield import components

    d = req.spec
    q = req.quantity
    s = req.scale_value
    m = len(freqs)
    if q == "efficiency":
        e = far["p_rad"] / (far["p_acc"] if d.get("kind", "total") == "radiation" else far["p_inc"])
        return ((float(d["min"]) - e) / s).reshape(1, m)

    def level(role: str, pol=None):
        """10 log10 of the requirement's kind and polarization at the role's rows (M, n)."""
        rows, ds, frame = table.rows[(k, role)]
        f = far["F"][:, rows, :]
        pol = pol or d.get("pol", "total")
        ref = math.radians(float(d.get("reference_phi_deg", 0.0)))
        u = far["u_factor"][:, None] * components(f, table.dirs[rows], frame, pol, ref)
        kind = d.get("kind", "realized")
        p = {"realized": far["p_inc"], "gain": far["p_acc"], "directivity": far["p_rad"]}[kind]
        return _x_db(4 * math.pi * u / p[:, None]), ds

    if q == "gain":
        x, ds = level("dirs")
        key = [kk for kk in ("min_dbi", "max_dbi", "min_mask_dbi", "max_mask_dbi") if kk in d][0]
        if "mask" in key:
            pts = np.asarray(d[key], float)
            lim = np.interp(np.asarray(ds.angle, float), pts[:, 0], pts[:, 1])
        else:
            lim = np.full(x.shape[1], float(d[key]))
        lim = torch.as_tensor(lim, dtype=torch.float64)[None]
        phi = (lim - x) / s if key.startswith("min") else (x - lim) / s
        return phi.T
    if q == "ripple":
        x, _ = level("dirs")
        r = _smax(x, TAU_DB) + _smax(-x, TAU_DB)
        return ((r - float(d["max_db"])) / s).reshape(1, m)
    if q == "front_to_back":
        xf, _ = level("front")
        xb, _ = level("back")
        fb = -_smax(-xf, TAU_DB) - _smax(xb, TAU_DB)
        return ((float(d["min_db"]) - fb) / s).reshape(1, m)
    if q == "hpbw":
        x, _ = level("dirs")
        x0 = x[:, 0]
        narrow = (x0[:, None] - 3.0 - x[:, 1:3]) / s
        wide = (x[:, 3:5] - x0[:, None] + 3.0) / s
        return torch.cat([narrow, wide], dim=1).T
    if q == "cross_pol":
        xc, _ = level("dirs", "co")
        xx, _ = level("dirs", "cross")
        return ((xx - xc - float(d["max_db"])) / s).T
    if q == "shape":
        rows, _, frame = table.rows[(k, "dirs")]
        w = torch.as_tensor(table.quad_weights[rows], dtype=torch.float64)
        u = far["u_factor"][:, None] * components(
            far["F"][:, rows, :], table.dirs[rows], frame, "total"
        )
        qt = torch.as_tensor(far["targets"][d["target"]][rows], dtype=torch.float64)[None]
        qt = qt / (qt * w[None]).sum(-1, keepdim=True)
        floor = 10 ** (far["floors"][d["target"]] / 10.0) * qt.max()
        p = u / (u * w[None]).sum(-1, keepdim=True)
        pf = p + floor
        pf = pf / (pf * w[None]).sum(-1, keepdim=True)
        tol_db = float(d["max_rms_db"])
        if d.get("form", "kl") == "kl":
            kl = (w[None] * qt * torch.log(qt / pf)).sum(-1)
            tol = 0.5 * (tol_db / DB) ** 2
            return (kl / tol - 1.0).reshape(1, m) / s
        ww = w[None] * (qt if d.get("weight", "uniform") == "target" else torch.ones_like(qt))
        err = DB * torch.log(pf / qt)
        mse = (ww * err**2).sum(-1) / ww.sum(-1)
        return (mse / tol_db**2 - 1.0).reshape(1, m) / s
    raise ValueError(f"unknown pattern quantity {q!r}")
