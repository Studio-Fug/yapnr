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
    priors: Tuple[Param, ...] = ()  # this stackup's priors where they differ from PARAMS
    tables: str = ""  # the stackup whose 2D tables this one reads ("": its own)
    mask_color: str = "Green"

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
        ps = params_of(self)
        return {n: ps[n] for n in self.params}

    def sha256(self) -> str:
        blob = json.dumps([asdict(x) for x in self.layers], sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


def _cu(name, t, param, verified=True):
    return Layer(name, "copper", t, "copper", 0.0, param, verified)


_L1_PARAMS = ("pp1.h", "L1.etch", "L1.t", "L1.rough", "mask.scale", "mask.dk", "mask.df")

_B_PARAMS = (
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


def _geo(p: Param) -> Param:
    """A geometric parameter with the default table range (±2.5 σ)."""
    return Param(**{**asdict(p), "table": _sym(p.nominal, p.sigma)})


# JLC06161H-2116C (board B): the 2116 prepreg replaces the 7628 of PARAMS.
_JLC2116 = "2116 prepreg, " + _JLC
_PRIORS_2116C = (
    _p("pp.dk", 4.16, 0.3, "", _JLC2116, 3.0, 6.0, _sym(4.16, 0.3)),
    _p("pp.df", 0.018, 0.008, "", "2116 prepreg, " + _EST, 0.0, 0.06),
    _geo(_p("pp1.h", 0.2464, 0.025, "mm", "L1-L2 2 x 2116, JLC06161H-2116C table", 0.12, 0.4)),
    _geo(_p("pp3.h", 0.3658, 0.037, "mm", "L3-L4 3 x 2116, JLC06161H-2116C table", 0.2, 0.55)),
    _geo(_p("core.h", 0.30, 0.03, "mm", "L2-L3 core, JLC06161H-2116C table", 0.15, 0.45)),
)

# OSH Park 4-layer (board O, Order 0). Sources: [O-4l] docs.oshpark.com/services/four-layer
# (prepreg 7.87 ± 0.797 mil, Dk 3.61 at 1 GHz; core 39 ± 3.9 mil; 1.7 mil finished outer copper),
# [O-4l-stack] its construction drawing (Df 0.009 of prepreg and core, core Dk 3.87, mask 0.6 ±
# 0.2 mil at Dk 3.90 / Df 0.033), [O-alt] the EM528 alternate page; all read 2026-10-02. The
# published tolerances are taken as 2 σ; the other σ are the Order 0 design's §9 priors (est.).
_OSH = "OSH Park 4L"
_O_PARAMS = (
    "pp.dk",
    "pp.df",
    "core.dk",
    "core.df",
    "pp1.h",
    "core.h",
    "L1.etch",
    "L1.t",
    "L1.rough",
    "L1.enig",
    "mask.scale",
    "mask.dk",
    "mask.df",
)
_PRIORS_O = (
    _p(
        "pp.dk",
        3.61,
        0.10,
        "",
        f"FR408HR 2113 prepreg at 1 GHz, {_OSH} page; σ est.",
        2.8,
        4.6,
        _sym(3.61, 0.10),
    ),
    _p("pp.df", 0.009, 0.003, "", f"{_OSH} construction drawing; σ est.", 0.0, 0.04),
    _p(
        "core.dk",
        3.87,
        0.15,
        "",
        f"FR408HR core, {_OSH} construction drawing; σ est.",
        2.8,
        4.8,
        _sym(3.87, 0.15),
    ),
    _p("core.df", 0.009, 0.003, "", f"{_OSH} construction drawing; σ est.", 0.0, 0.04),
    _geo(
        _p(
            "pp1.h",
            0.1999,
            0.0101,
            "mm",
            f"L1-L2 prepreg 7.87 ± 0.797 mil (2 σ), {_OSH} page",
            0.12,
            0.3,
        )
    ),
    _geo(_p("core.h", 0.9906, 0.0495, "mm", f"core 39 ± 3.9 mil (2 σ), {_OSH} page", 0.7, 1.3)),
    _p(
        "L1.etch",
        0.0,
        0.015,
        "mm",
        "per edge at the trace foot, est. (Order 0 design §16.5)",
        -0.06,
        0.09,
        (-0.04, 0.04),
    ),
    _geo(_p("L1.t", 0.04318, 0.005, "mm", f"1.7 mil finished, {_OSH} page; σ est.", 0.02, 0.07)),
    # OSH Park states no foil: the Order 0 review's prior Rq 1.0 ± 0.5 µm (its F2), as the Huray
    # ratio whose factor at 5 GHz equals Hammerstad's for that Rq: 1.12 / 2.98 / 3.82 at Rq 0.5 /
    # 1.0 / 1.5 µm (derived)
    _p(
        "L1.rough",
        3.0,
        1.3,
        "",
        "Huray ratio (a = 0.5 µm) of Rq 1.0 ± 0.5 µm at 5 GHz, Order 0 review F2; derived",
        0.0,
        8.0,
    ),
    _p(
        "mask.scale",
        1.0,
        0.33,
        "",
        f"× 15 µm: 0.6 ± 0.2 mil, {_OSH} construction drawing",
        0.1,
        3.0,
        (0.25, 1.85),
    ),
    _p(
        "mask.dk",
        3.9,
        0.3,
        "",
        "Taiyo PSR-4000BN as OSH Park models it; σ est.",
        2.5,
        5.5,
        _sym(3.9, 0.3),
    ),
    _p("mask.df", 0.033, 0.01, "", f"{_OSH} construction drawing; σ est.", 0.0, 0.08),
)
# The EM528 alternate [O-alt]: prepreg 2 x 3313 7.67 mil at Dk 3.76, core 4.11, Df 0.005 (no
# frequency stated); everything else as FR408HR.
_PRIORS_EM528 = tuple(
    p for p in _PRIORS_O if p.name not in ("pp.dk", "pp.df", "core.dk", "core.df", "pp1.h")
) + (
    _p(
        "pp.dk",
        3.76,
        0.10,
        "",
        "EM-528 3313 prepreg, OSH Park alternate page; σ est.",
        2.8,
        4.6,
        _sym(3.61, 0.10),
    ),
    _p("pp.df", 0.005, 0.002, "", "EM-528, OSH Park alternate page; σ est.", 0.0, 0.04),
    _p(
        "core.dk",
        4.11,
        0.15,
        "",
        "EM-528 core, OSH Park alternate page; σ est.",
        2.8,
        4.8,
        _sym(3.87, 0.15),
    ),
    _p("core.df", 0.005, 0.002, "", "EM-528, OSH Park alternate page; σ est.", 0.0, 0.04),
    _p(
        "pp1.h",
        0.19482,
        0.0101,
        "mm",
        "L1-L2 prepreg 7.67 mil, OSH Park alternate page",
        0.12,
        0.3,
        _sym(0.1999, 0.0101),
    ),
)

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
            Layer("core45", "core", 0.40, "FR-4 core", 4.6, "core45"),
            _cu("L5", 0.0152, "L5"),
            Layer("pp5", "prepreg", 0.2104, "7628", 4.4, "pp5"),
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
        notes=(
            "board B until 2026-10-02 (now JLC06161H-2116C, the product's stackup); kept as the"
            " alternative that shares board A's L1 cross-section",
        ),
    ),
    "JLC06161H-2116C": Stackup(
        id="JLC06161H-2116C",
        board="B",
        layers=(
            _cu("L1", 0.035, "L1"),
            Layer("pp1", "prepreg", 0.2464, "2 x 2116", 4.16, "pp1"),
            _cu("L2", 0.0152, "L2"),
            Layer("core", "core", 0.30, "FR-4 core", 4.6, "core"),
            _cu("L3", 0.0152, "L3"),
            Layer("pp3", "prepreg", 0.3658, "3 x 2116", 4.16, "pp3"),
            _cu("L4", 0.0152, "L4"),
            Layer("core45", "core", 0.30, "FR-4 core", 4.6, "core45"),
            _cu("L5", 0.0152, "L5"),
            Layer("pp5", "prepreg", 0.2464, "2 x 2116", 4.16, "pp5"),
            _cu("L6", 0.035, "L6"),
        ),
        params=_B_PARAMS + _L1_PARAMS,
        finish="ENIG",
        source="jlcpcb.com/impedance (JLC06161H-2116C), read 2026-10-02",
        notes=(
            "board B since 2026-10-02: the product's stackup (Order 0 owner decision); its L1"
            " dielectric is 2116, so board B no longer shares L1 with board A",
        ),
        priors=_PRIORS_2116C,
    ),
    "OSHPARK-4L-FR408HR": Stackup(
        id="OSHPARK-4L-FR408HR",
        board="O",
        layers=(
            _cu("L1", 0.04318, "L1"),
            Layer("pp1", "prepreg", 0.1999, "FR408HR 2 x 2113", 3.61, "pp1"),
            _cu("L2", 0.01727, "L2"),
            Layer("core", "core", 0.9906, "FR408HR", 3.87, "core"),
            _cu("L3", 0.01727, "L3"),
            Layer("pp3", "prepreg", 0.1999, "FR408HR 2 x 2113", 3.61, "pp3"),
            _cu("L4", 0.04318, "L4"),
        ),
        params=_O_PARAMS,
        finish="ENIG",
        source="docs.oshpark.com/services/four-layer and its construction drawing, read 2026-10-02",
        notes=(
            "Order 0 (OSH Park): region M is L1 over In1.Cu, region W L1 over B.Cu with In1/In2"
            " removed; the two prepregs are taken as one material and one thickness",
        ),
        priors=_PRIORS_O,
        mask_color="Purple",
    ),
    "OSHPARK-4L-EM528": Stackup(
        id="OSHPARK-4L-EM528",
        board="O",
        layers=(
            _cu("L1", 0.04318, "L1"),
            Layer("pp1", "prepreg", 0.19482, "EM-528 2 x 3313", 3.76, "pp1"),
            _cu("L2", 0.01727, "L2"),
            Layer("core", "core", 0.9906, "EM-528", 4.11, "core"),
            _cu("L3", 0.01727, "L3"),
            Layer("pp3", "prepreg", 0.19482, "EM-528 2 x 3313", 3.76, "pp3"),
            _cu("L4", 0.04318, "L4"),
        ),
        params=_O_PARAMS,
        finish="ENIG",
        source="docs.oshpark.com/troubleshooting/alternate-4-layer-stackup, read 2026-10-02",
        notes=("the EM528 alternate OSH Park offers at checkout: board O's 2D tables cover it",),
        priors=_PRIORS_EM528,
        tables="OSHPARK-4L-FR408HR",
        mask_color="Purple",
    ),
}


def get(stackup_id: str) -> Stackup:
    try:
        return STACKUPS[stackup_id]
    except KeyError:
        raise KeyError(f"unknown stackup {stackup_id!r}; known: {sorted(STACKUPS)}") from None


def params_of(st: Optional[Stackup] = None) -> Dict[str, Param]:
    """Every parameter definition as seen by stackup `st`: PARAMS with its own priors."""
    if st is None or not st.priors:
        return PARAMS
    out = dict(PARAMS)
    out.update({p.name: p for p in st.priors})
    return out


def param(name: str, st: Optional[Stackup] = None) -> Param:
    return params_of(st)[name]


def nominal(names: Sequence[str], st: Optional[Stackup] = None) -> Dict[str, float]:
    ps = params_of(st)
    return {n: ps[n].nominal for n in names}


def with_values(st: Stackup, values: Dict[str, float]) -> Dict[str, float]:
    """The full parameter dict of a stackup: nominal values overridden by `values`."""
    out = nominal(st.params, st)
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
            er_key = "pp.dk" if x.kind == "prepreg" else "core.dk" if x.kind == "core" else ""
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
