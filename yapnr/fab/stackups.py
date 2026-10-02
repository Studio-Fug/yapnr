"""Stackup views: microstrip, cross-section, and the adapters for ``yapnr.rf`` and the coupons.

Design: docs/design/fab-and-ordering.md §5.3. A stackup file lists every layer top to bottom;
unpublished values stay ``None``. A stand-in (``*_prior``) is used only when the caller asks for
it (``use_prior=True``), and the view then says so (``priors_used``).

    s = load("oshpark-4l-fr408hr")
    m = s.microstrip("F.Cu")                 # reference: the next copper layer (In1.Cu)
    m.h_mm, m.er, m.er_freq_hz, m.t_um       # 0.1999, 3.61, 1e9, 43.18
    m.rf_stackup_spec(10.0, use_prior=True)  # {"er", "tan_delta", "h_mm", "f_ref_ghz"}

Stdlib only. The Hammerstad-Jensen formula here is the one of ``yapnr.rf.stackup`` (quasi-static,
zero-thickness strip, no mask), used for the nominal widths in bundle READMEs and the data tests;
the 2D solver and the coupons set the real numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from yapnr.fab import capability

ETA0 = 376.730313668  # free-space wave impedance (ohm)


class StackupError(ValueError):
    """A view the stackup cannot give (a missing value, a reference too far away)."""


@dataclass(frozen=True)
class Microstrip:
    """A microstrip on one outer copper layer over the next copper layer (mm, Hz)."""

    stackup: str
    layer: str
    reference: str
    h_mm: float
    er: Optional[float]
    er_freq_hz: Optional[float]
    tan_delta: Optional[float]
    t_um: float
    er_prior: Optional[float] = None
    tan_delta_prior: Optional[float] = None
    sources: Dict[str, str] = field(default_factory=dict)

    def values(self, use_prior: bool = False):
        """(er, tan_delta, priors used); raises StackupError for a missing value."""
        used = []
        er, td = self.er, self.tan_delta
        if er is None:
            if not (use_prior and self.er_prior is not None):
                raise StackupError(f"{self.stackup}: no published Dk under {self.layer}")
            er = self.er_prior
            used.append("er")
        if td is None:
            if not (use_prior and self.tan_delta_prior is not None):
                raise StackupError(
                    f"{self.stackup}: no published Df under {self.layer} (use_prior=True takes "
                    f"the stand-in {self.tan_delta_prior})"
                )
            td = self.tan_delta_prior
            used.append("tan_delta")
        return er, td, used

    def rf_stackup_spec(self, f_ref_ghz: float, use_prior: bool = False) -> Dict[str, Any]:
        """The ``yapnr.rf`` StackupSpec fields: a uniform substrate (er, tan δ, h, f_ref).

        ``f_ref_ghz`` is the design's reference frequency (copper sheet conductance, loss
        matching), not the frequency the vendor quoted Dk at; that one travels in
        ``provenance``.
        """
        er, td, used = self.values(use_prior)
        return {
            "er": er,
            "tan_delta": td,
            "h_mm": self.h_mm,
            "f_ref_ghz": float(f_ref_ghz),
            "provenance": {
                "stackup": self.stackup,
                "layer": self.layer,
                "reference": self.reference,
                "er_freq_hz": self.er_freq_hz,
                "priors_used": used,
                "sources": dict(self.sources),
            },
        }

    def w50_mm(self, use_prior: bool = False, z0: float = 50.0) -> float:
        """Nominal width for ``z0`` (Hammerstad-Jensen, zero thickness, no mask)."""
        er = self.er if self.er is not None or not use_prior else self.er_prior
        if er is None:
            raise StackupError(f"{self.stackup}: no published Dk under {self.layer}")
        return width_for(z0, self.h_mm, er)


class Stackup:
    """One stackup file (``yapnr-fab-stackup-v1``)."""

    def __init__(self, doc: Dict[str, Any]):
        self.doc = doc
        self.id: str = doc["id"]
        self.vendor: str = doc["vendor"]
        self.title: str = doc["title"]
        self.vendor_name: str = doc["vendor_name"]
        self.checkout: str = doc["checkout"]
        self.thickness_mm: float = doc["thickness_mm"]["value"]
        self.layers: List[Dict[str, Any]] = doc["layers"]
        self.sources: Dict[str, Any] = doc["sources"]

    def copper_layers(self) -> List[str]:
        return [layer["name"] for layer in self.layers if layer["kind"] == "copper"]

    def _copper_index(self, name: str) -> int:
        for i, layer in enumerate(self.layers):
            if layer["kind"] == "copper" and layer["name"] == name:
                return i
        raise StackupError(f"{self.id}: no copper layer {name}")

    def microstrip(self, layer: str = "F.Cu") -> Microstrip:
        """The microstrip view of an outer layer over the next copper layer.

        Refuses a reference more than one dielectric away: plies of one material and Dk between
        the two copper layers count as one dielectric (their thicknesses add up).
        """
        names = self.copper_layers()
        if layer not in ("F.Cu", "B.Cu") or layer not in names:
            raise StackupError(f"{self.id}: a microstrip needs an outer layer, not {layer}")
        i = self._copper_index(layer)
        step = 1 if layer == "F.Cu" else -1
        between: List[Dict[str, Any]] = []
        j = i + step
        while 0 <= j < len(self.layers) and self.layers[j]["kind"] != "copper":
            if self.layers[j]["kind"] == "dielectric":
                between.append(self.layers[j])
            j += step
        if not (0 <= j < len(self.layers)) or not between:
            raise StackupError(f"{self.id}: {layer} has no reference copper layer")
        ers = {d.get("er") for d in between}
        if len(between) > 1 and (len(ers) > 1 or len({d.get("material") for d in between}) > 1):
            raise StackupError(
                f"{self.id}: {layer} sees {len(between)} different dielectrics before its reference"
            )
        first = between[0]
        srcs = {"h_mm": first["src"], "er": first["src"]}
        if first.get("df_prior") is not None:
            srcs["tan_delta_prior"] = first["df_prior_src"]
        if first.get("er_prior") is not None:
            srcs["er_prior"] = first["er_prior_src"]
        return Microstrip(
            stackup=self.id,
            layer=layer,
            reference=self.layers[j]["name"],
            h_mm=round(sum(d["thickness_mm"] for d in between), 6),
            er=first.get("er"),
            er_freq_hz=first.get("er_freq_hz"),
            tan_delta=first.get("df"),
            t_um=round(self.layers[i]["thickness_mm"] * 1000.0, 4),
            er_prior=first.get("er_prior"),
            tan_delta_prior=first.get("df_prior"),
            sources=srcs,
        )

    def cross_section(self) -> List[Dict[str, Any]]:
        """Every layer top to bottom, with priors and sources (the 2D solver's nominal model)."""
        return [dict(layer) for layer in self.layers]

    def summary(self) -> str:
        """One line per stackup for cards and READMEs: copper / dielectric (Dk) / copper ..."""
        parts = []
        for layer in self.layers:
            if layer["kind"] == "copper":
                parts.append(f"Cu {layer['thickness_mm'] * 1000:.1f} um")
            elif layer["kind"] == "dielectric":
                er = layer.get("er")
                dk = f"Dk {er:g}" if er is not None else "Dk n/p"
                if er is not None and layer.get("er_freq_hz"):
                    dk += f" @ {_freq(layer['er_freq_hz'])}"
                parts.append(f"{layer['material']} {layer['thickness_mm']:.4g} mm {dk}")
        return " / ".join(parts)


def _freq(hz: float) -> str:
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if hz >= scale:
            return f"{hz / scale:g} {unit}"
    return f"{hz:g} Hz"


def load(stackup_id: str, measured: Optional[str] = None) -> Stackup:
    """A stackup by id. ``measured`` (a ``yapnr-fab-stackup-measured-v1`` overlay from the coupon
    work) is reserved: the format is defined there, so it is refused here for now."""
    if measured is not None:
        raise NotImplementedError("measured stackup overlays arrive with the coupon work")
    return Stackup(capability.stackup(stackup_id))


def rf_rules(profile: str) -> Dict[str, float]:
    """``yapnr.rf`` Rules for a profile: its minimum track width and clearance (mm)."""
    from yapnr.fab import profiles

    fab = profiles.engine_fab(profile)
    return {"min_width_mm": fab["min_track_width_mm"], "min_space_mm": fab["clearance_mm"]}


# ------------------------------------------------------------------- Hammerstad and Jensen


def hammerstad_jensen(w: float, h: float, er: float):
    """Quasi-static (Z0, ε_eff) of a zero-thickness microstrip of width w on height h.

    E. Hammerstad, Ø. Jensen, "Accurate models for microstrip computer-aided design", IEEE MTT-S
    Digest (1980); the same formula as ``yapnr.rf.stackup.hammerstad_jensen``.
    """
    u = w / h
    fu = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    z01 = ETA0 / (2.0 * math.pi) * math.log(fu / u + math.sqrt(1.0 + (2.0 / u) ** 2))
    a = (
        1.0
        + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0
        + math.log(1.0 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    eps_eff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)
    return z01 / math.sqrt(eps_eff), eps_eff


def width_for(z0: float, h: float, er: float) -> float:
    """The width (same unit as ``h``) giving ``z0`` ohm, by bisection on log(w/h)."""
    lo, hi = math.log(0.01), math.log(100.0)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        z, _ = hammerstad_jensen(h * math.exp(mid), h, er)
        if z > z0:
            lo = mid
        else:
            hi = mid
    return h * math.exp(0.5 * (lo + hi))
