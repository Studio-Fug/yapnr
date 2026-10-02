"""Layered stackups of the coupon boards, and the fit parameters with their priors (design §4, §8.1).

Units: lengths in mm, frequencies in Hz. Dk and Df are the Djordjevic-Sarkar values at
`F_REF` (1 GHz). Every number records its source; "est." marks a planning estimate and
"unverified" a vendor value that could not be checked against a datasheet.

JLCPCB publishes εr without a frequency, no loss tangent, no etch model and no roughness
(jlcpcb.com/impedance, read 2026-10-02), so those priors are wide.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

F_REF = 1.0e9  # Hz: the reference frequency of Dk and Df
DS_F1, DS_F2 = 1.0e3, 1.0e12  # Hz: Djordjevic-Sarkar corner frequencies (fixed, design §8.1)
RHO_CU = 1.72e-8  # ohm m at 20 °C (fixed: DC data fit w·t, not ρ)
ALPHA_CU = 0.00393  # 1/K, temperature coefficient of copper resistivity
MASK_SUB_MM, MASK_CU_MM = 0.030, 0.015  # conformal mask over substrate / over copper (JLC calc.)
HURAY_RADIUS_UM = 0.5  # fixed sphere radius of the Huray roughness model (est.)


@dataclass(frozen=True)
class Param:
    """One fit parameter: nominal value, prior σ, hard bounds and the 2D-table range."""

    name: str
    nominal: float
    sigma: float
    unit: str
    source: str
    lo: float
    hi: float
    table: Optional[Tuple[float, float]] = None  # surrogate range (2D tables), if geometric

    def clip(self, value: float) -> float:
        return min(max(value, self.lo), self.hi)


def _p(name, nominal, sigma, unit, source, lo, hi, table=None) -> Param:
    return Param(name, nominal, sigma, unit, source, lo, hi, table)


def _sym(nominal: float, sigma: float, k: float = 2.5) -> Tuple[float, float]:
    return (nominal - k * sigma, nominal + k * sigma)


# --- parameter definitions (design §8.1) --------------------------------------------------------

_JLC = "JLC impedance page (frequency unstated)"
_EST = "est.; JLC publishes none"

PARAMS: Dict[str, Param] = {
    p.name: p
    for p in [
        _p("pp.dk", 4.4, 0.3, "", "7628 prepreg, " + _JLC, 3.0, 6.0, _sym(4.4, 0.3)),
        _p("pp.df", 0.018, 0.008, "", "7628 prepreg, " + _EST, 0.0, 0.06),
        _p("core.dk", 4.6, 0.3, "", "core, " + _JLC, 3.0, 6.0, _sym(4.6, 0.3)),
        _p("core.df", 0.018, 0.008, "", "core, " + _EST, 0.0, 0.06),
        _p("pp1.h", 0.2104, 0.021, "mm", "L1-L2 7628, JLC stackup table", 0.1, 0.35),
        _p("pp3.h", 0.2028, 0.020, "mm", "L3-L4 7628, JLC06161H-7628 table", 0.1, 0.35),
        _p("core.h", 0.40, 0.04, "mm", "L2-L3 core, JLC06161H-7628 table", 0.2, 0.6),
        _p("L1.etch", 0.0, 0.025, "mm", "per edge at the trace foot, est.", -0.06, 0.09),
        _p("L1.t", 0.035, 0.007, "mm", "1 oz finished, JLC table; σ 20 % est.", 0.012, 0.07),
        _p("L1.rough", 2.0, 1.5, "", "Huray surface ratio (a = 0.5 µm), est. ED foil", 0.0, 8.0),
        _p("L1.enig", 1.2, 0.3, "", "conductor-loss factor of exposed ENIG copper, est.", 0.8, 3.0),
        _p("L3.etch", 0.0, 0.025, "mm", "per edge at the trace foot, est.", -0.06, 0.09),
        _p("L3.t", 0.0152, 0.004, "mm", "0.5 oz, JLC table; σ est.", 0.006, 0.035),
        _p("L3.rough", 2.0, 1.5, "", "Huray surface ratio (a = 0.5 µm), est.", 0.0, 8.0),
        _p("mask.scale", 1.0, 0.5, "", "× 30/15 µm, JLC calculator", 0.1, 3.0, (0.25, 2.25)),
        _p("mask.dk", 3.8, 0.3, "", "LPI mask, JLC calculator", 2.5, 5.5, _sym(3.8, 0.3)),
        _p("mask.df", 0.025, 0.015, "", "LPI mask, est.", 0.0, 0.08),
        _p("jPM.c", 0.0, 0.02, "pF", "P->M junction shunt C (nuisance), est.", -0.1, 0.1),
    ]
}

# Geometric parameters get a table range of ±2.5 σ unless set above.
for _n in ("pp1.h", "pp3.h", "core.h", "L1.etch", "L1.t", "L3.etch", "L3.t"):
    _q = PARAMS[_n]
    PARAMS[_n] = Param(**{**asdict(_q), "table": _sym(_q.nominal, _q.sigma)})


# --- stackups -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer:
    """One stackup entry: copper (`kind` = "copper") or dielectric ("core", "prepreg")."""

    name: str
    kind: str
    t_mm: float
    material: str = ""
    er: float = 0.0
    param: str = ""  # the fit parameter prefix of this entry ("pp1", "core", "L1", ...)
    verified: bool = True


@dataclass(frozen=True)
class Stackup:
    id: str
    board: str  # coupon board letter
    layers: Tuple[Layer, ...]
    params: Tuple[str, ...]  # fit parameter names, in order
    finish: str
    source: str
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def copper(self) -> List[Layer]:
        return [x for x in self.layers if x.kind == "copper"]

    @property
    def thickness_mm(self) -> float:
        return sum(x.t_mm for x in self.layers)

    def kicad_layer(self, index: int) -> str:
        """KiCad name of copper layer L<index> (1-based)."""
        n = len(self.copper)
        if index == 1:
            return "F.Cu"
        if index == n:
            return "B.Cu"
        return f"In{index - 1}.Cu"

    def prior(self) -> Dict[str, Param]:
        return {n: PARAMS[n] for n in self.params}

    def sha256(self) -> str:
        blob = json.dumps([asdict(x) for x in self.layers], sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


def _cu(name, t, param, verified=True):
    return Layer(name, "copper", t, "copper", 0.0, param, verified)


_L1_PARAMS = ("pp1.h", "L1.etch", "L1.t", "L1.rough", "mask.scale", "mask.dk", "mask.df")

STACKUPS: Dict[str, Stackup] = {
    "JLC04161H-7628": Stackup(
        id="JLC04161H-7628",
        board="A",
        layers=(
            _cu("L1", 0.035, "L1"),
            Layer("pp1", "prepreg", 0.2104, "7628", 4.4, "pp1"),
            _cu("L2", 0.0152, "L2"),
            Layer("core", "core", 1.065, "FR-4 core", 4.6, "core"),
            _cu("L3", 0.0152, "L3"),
            Layer("pp3", "prepreg", 0.2104, "7628", 4.4, "pp3"),
            _cu("L4", 0.035, "L4"),
        ),
        params=("pp.dk", "pp.df") + _L1_PARAMS + ("L1.enig", "jPM.c"),
        finish="ENIG",
        source="jlcpcb.com/impedance (JLC04161H-7628), read 2026-10-02",
    ),
    "JLC06161H-7628": Stackup(
        id="JLC06161H-7628",
        board="B",
        layers=(
            _cu("L1", 0.035, "L1"),
            Layer("pp1", "prepreg", 0.2104, "7628", 4.4, "pp1"),
            _cu("L2", 0.0152, "L2"),
            Layer("core", "core", 0.40, "FR-4 core", 4.6, "core"),
            _cu("L3", 0.0152, "L3"),
            Layer("pp3", "prepreg", 0.2028, "7628", 4.4, "pp3"),
            _cu("L4", 0.0152, "L4"),
            Layer("core45", "core", 0.40, "FR-4 core", 4.6, "core45", verified=False),
            _cu("L5", 0.0152, "L5", verified=False),
            Layer("pp5", "prepreg", 0.2104, "7628", 4.4, "pp5", verified=False),
            _cu("L6", 0.035, "L6"),
        ),
        params=(
            "core.dk",
            "core.df",
            "pp.dk",
            "pp.df",
            "core.h",
            "pp3.h",
            "L3.etch",
            "L3.t",
            "L3.rough",
        )
        + _L1_PARAMS,
        finish="ENIG",
        source="jlcpcb.com/impedance (JLC06161H-7628), read 2026-10-02",
        notes=("L4-L6 assumed symmetric to L1-L3 (unverified: 'per JLC's table')",),
    ),
    "JLC06161H-2116C": Stackup(
        id="JLC06161H-2116C",
        board="B'",
        layers=(
            _cu("L1", 0.035, "L1"),
            Layer("pp1", "prepreg", 0.2464, "2116", 4.16, "pp1"),
            _cu("L2", 0.0152, "L2"),
            Layer("core", "core", 0.30, "FR-4 core", 4.6, "core"),
            _cu("L3", 0.0152, "L3"),
            Layer("pp3", "prepreg", 0.366, "3 x 2116", 4.16, "pp3"),
            _cu("L4", 0.0152, "L4"),
            Layer("core45", "core", 0.30, "FR-4 core", 4.6, "core45", verified=False),
            _cu("L5", 0.0152, "L5", verified=False),
            Layer("pp5", "prepreg", 0.2464, "2116", 4.16, "pp5", verified=False),
            _cu("L6", 0.035, "L6"),
        ),
        params=(),  # no 2D tables shipped: run the table tool first (design §4.2 alternative)
        finish="ENIG",
        source="jlcpcb.com/impedance (JLC06161H-2116C), read 2026-10-02",
        notes=("alternative board B'; L4-L6 unverified",),
    ),
}


def get(stackup_id: str) -> Stackup:
    try:
        return STACKUPS[stackup_id]
    except KeyError:
        raise KeyError(f"unknown stackup {stackup_id!r}; known: {sorted(STACKUPS)}") from None


def nominal(names: Sequence[str]) -> Dict[str, float]:
    return {n: PARAMS[n].nominal for n in names}


def with_values(st: Stackup, values: Dict[str, float]) -> Dict[str, float]:
    """The full parameter dict of a stackup: nominal values overridden by `values`."""
    out = nominal(st.params)
    out.update({k: float(v) for k, v in values.items() if k in out})
    return out


def physical_layers(st: Stackup, values: Optional[Dict[str, float]] = None) -> List[dict]:
    """The `rules['stackup']` layer list (the SI stackup block format), with `values` applied.

    Heights, Dk and copper thicknesses that are fit parameters take the fitted value; the others
    stay nominal. Dk is reported at F_REF.
    """
    v = values or {}
    out = []
    for x in st.layers:
        if x.kind == "copper":
            t = v.get(f"{x.param}.t", x.t_mm)
            out.append(dict(name=st.kicad_layer(int(x.name[1:])), kind="copper", t_mm=round(t, 5)))
        else:
            er_key = "pp.dk" if x.material == "7628" else "core.dk" if x.kind == "core" else ""
            df_key = er_key.replace(".dk", ".df") if er_key else ""
            entry = dict(
                name=x.name,
                kind="dielectric",
                t_mm=round(v.get(f"{x.param}.h", x.t_mm), 5),
                er=round(v.get(er_key, x.er), 4),
                material=x.material,
            )
            if df_key in v:
                entry["tand"] = round(v[df_key], 5)
            out.append(entry)
    return out
