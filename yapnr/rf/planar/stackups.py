"""Stackup presets for planar models, and the Hammerstad roughness factor.

The presets are the radar60 Board A RF stack (4-mil RO4835 LoPro core over L2, RO4450F bondply
to L3), with the values and sources of that board plan:

- RO4835 LoPro, 0.1016 mm (4 mil), Dk 3.56 (the plan's mid value between process Dk 3.33-3.48
  and design Dk 3.66), Df 0.0037 [Rogers RO4835 data sheet 92-160];
- RO4450F bondply, 0.096 mm pressed, Dk 3.52, Df 0.004 [Rogers RO4400 bondply data sheet]; it also
  fills the 17.5 um L2 copper gap, so the bond slab reaches the top of L2 (0.1135 mm);
- L1 copper 35 um finished, LoPro reverse-treated foil with Rq about 0.4 um. L2 is the core's
  other foil, also reverse-treated with its treated side bonded to the core, i.e. facing L1:
  the surface the lines' return current flows on has the same roughness, so L2 gets the same
  factor K (L3, on the bondply's far side, keeps 1.0);
- solder mask (``solder_mask``): about 15-20 um over copper, Dk about 3.5-4 (stage-2 plan),
  Df 0.025 assumed (LPI masks are quoted at 0.02-0.03 at microwave frequencies).

``feed`` is the TX feed models' stack (L2 is the floor of the domain, no windows: ``floor``
gives it as a ``metal`` wall); ``window`` adds the bondply, L2 as a sheet that can carry
windows, and L3 (the patch models).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

MU0 = 4.0e-7 * math.pi
SIGMA_CU = 5.8e7  # S/m, annealed copper (IACS 100 %)

RO4835 = dict(name="RO4835", eps_r=3.56, tan_d=0.0037, h=0.1016)
RO4450F = dict(name="RO4450F", eps_r=3.52, tan_d=0.004, h=0.096)
T_L1, T_L2, RQ_L1_UM = 0.035, 0.0175, 0.4
RQ_L2_UM = RQ_L1_UM  # the core's other LoPro foil, treated side towards L1
MASK = dict(name="MASK", eps_r=3.8, tan_d=0.025, h=0.017)
F_DESIGN = 62.05e9  # the ANT-02 band centre, where the roughness factor is taken


def skin_depth(f_hz: float, sigma: float = SIGMA_CU) -> float:
    """Skin depth in metres."""
    return math.sqrt(1.0 / (math.pi * f_hz * MU0 * sigma))


def hammerstad_k(rq_um: float, f_hz: float, sigma: float = SIGMA_CU) -> float:
    """Hammerstad-Jensen roughness factor K = 1 + (2/pi) atan(1.4 (Rq/delta)^2)."""
    ratio = rq_um * 1e-6 / skin_depth(f_hz, sigma)
    return 1.0 + (2.0 / math.pi) * math.atan(1.4 * ratio * ratio)


def radar60(
    kind: str = "feed", l1_model: str = "sheet", f_hz: float = F_DESIGN
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(dielectrics, layers) of the radar60 RF stack, z = 0 at the bottom of the stack.

    ``feed``: RO4835 from z = 0 (the L2 plane, the domain floor) to 0.1016, L1 on top.
    ``window``: L3 at z = 0, RO4450F to 0.1135 (top of L2), L2 there, RO4835 to 0.2151, L1.
    """
    k = round(hammerstad_k(RQ_L1_UM, f_hz), 4)
    k2 = round(hammerstad_k(RQ_L2_UM, f_hz), 4)
    if kind == "feed":
        diel = [
            dict(
                name=RO4835["name"],
                z0=0.0,
                z1=RO4835["h"],
                eps_r=RO4835["eps_r"],
                tan_d=RO4835["tan_d"],
            )
        ]
        layers = [
            dict(name="L1", z=RO4835["h"], t=T_L1, sigma=SIGMA_CU, rough_k=k, model=l1_model),
        ]
        return diel, layers
    if kind == "window":
        z_l2 = round(RO4450F["h"] + T_L2, 6)
        z_l1 = round(z_l2 + RO4835["h"], 6)
        diel = [
            dict(
                name=RO4450F["name"],
                z0=0.0,
                z1=z_l2,
                eps_r=RO4450F["eps_r"],
                tan_d=RO4450F["tan_d"],
            ),
            dict(
                name=RO4835["name"], z0=z_l2, z1=z_l1, eps_r=RO4835["eps_r"], tan_d=RO4835["tan_d"]
            ),
        ]
        layers = [
            dict(name="L3", z=0.0, t=T_L2, sigma=SIGMA_CU, rough_k=1.0, model="pec"),
            dict(name="L2", z=z_l2, t=T_L2, sigma=SIGMA_CU, rough_k=k2, model="pec"),
            dict(name="L1", z=z_l1, t=T_L1, sigma=SIGMA_CU, rough_k=k, model=l1_model),
        ]
        return diel, layers
    raise ValueError(f"unknown radar60 stack {kind!r}")


def floor(f_hz: float = F_DESIGN) -> Dict[str, Any]:
    """The feed stack's L2 as the domain floor's ``metal`` wall (``domain.metal["zmin"]``): the
    LoPro foil under the core, its treated side up, so the lines' return current sees the same
    roughness as L1."""
    return dict(name="L2", sigma=SIGMA_CU, rough_k=round(hammerstad_k(RQ_L2_UM, f_hz), 4), t=T_L2)


def solder_mask(
    layer: Dict[str, Any],
    h: float = MASK["h"],
    eps_r: float = MASK["eps_r"],
    tan_d: float = MASK["tan_d"],
    fill: str = "conformal",
    outline: Optional[List[List[float]]] = None,
    name: str = MASK["name"],
) -> Dict[str, Any]:
    """A solder-mask dielectric coating ``layer`` (planar ``coat``): ``h`` over the copper (its
    ``z1`` is ``z + t + h``), optionally limited to ``outline`` (the mask opening is outside)."""
    d = dict(
        name=name,
        z0=float(layer["z"]),
        z1=round(float(layer["z"]) + float(layer.get("t", 0.0)) + float(h), 6),
        eps_r=float(eps_r),
        tan_d=float(tan_d),
        coat=dict(layer=layer["name"], fill=fill),
    )
    if outline is not None:
        d["outline"] = outline
    return d
