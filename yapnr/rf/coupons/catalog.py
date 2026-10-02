"""The sticks of each coupon board (design §5) and the multiline-TRL line lengths.

Every structure is a stick: a strip of board with an edge SMA at each end (or none, for the DC
and microsection sticks). The launch section, edge to reference plane, is LAUNCH_MM on every
stick of a board, so the reference planes RP1 and RP2 sit LAUNCH_MM from the ends. `elements`
is the RP-to-RP model of the stick (`models.Model.abcd`), referenced to the impedance of the
stick's TRL family.

Core sticks are generated; the extended set (design §5.2/§5.3) is listed with `generated`
False where this revision does not build it yet.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import SCHEMA_CATALOG, stackups

C_LIGHT = 299792458.0
LAUNCH_MM = 10.0
STICK_W = 15.0
TRL_DL = (0.0, 2.5, 6.5, 16.0, 40.0, 100.0)  # design §5.1
TIE_DL = (0.0, 6.5, 16.0, 40.0)  # design §5.3, board B L1 tie set
VERIFY_DL = 28.0
VARIANT_MM = 40.0
F0 = 5.8e9


@dataclass
class Stick:
    id: str
    kind: str  # thru line reflect verify variant ring stub coupled switch ufl dc xsec
    family: str  # family of the test section
    trl: str  # TRL set that calibrates it ("" if none)
    length: float  # edge to edge, mm (along x)
    height: float = STICK_W  # across, mm (along y)
    dl: float = 0.0  # TRL line length beyond the thru
    elements: List[tuple] = field(default_factory=list)
    tier: str = "core"
    fitted: bool = False  # enters the joint fit (else held out or not modelled)
    ports: int = 2
    determines: str = ""
    label: str = ""
    geometry: dict = field(default_factory=dict)
    generated: bool = True

    @property
    def rp(self) -> Tuple[float, float]:
        return (LAUNCH_MM, self.length - LAUNCH_MM)


@dataclass
class Launch:
    pad_w: float  # center pad width (connector footprint)
    pad_x0: float  # pad start from the milled edge
    pad_len: float
    pad_gap: float  # 50 Ω coplanar gap around the pad (2D, cut-outs below)
    taper_len: float
    cut_layers: Tuple[int, ...]  # copper layers removed under pad and taper
    via_x: Optional[float] = None  # L1 -> L3 transition (board B), mm from the edge
    source: str = ""


@dataclass
class Board:
    stackup: str
    letter: str
    launch: Launch
    sticks: List[Stick]
    trl: Dict[str, dict]
    notes: List[str] = field(default_factory=list)

    def stick(self, sid: str) -> Stick:
        for s in self.sticks:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA_CATALOG,
            "stackup": self.stackup,
            "board": self.letter,
            "launch": asdict(self.launch),
            "trl": self.trl,
            "sticks": [_stick_json(s) for s in self.sticks],
            "notes": self.notes,
        }


def _stick_json(s: Stick) -> dict:
    d = asdict(s)
    d["elements"] = [list(e) for e in s.elements]
    d["rp_mm"] = list(s.rp) if s.ports == 2 else []
    return d


def conditioning(dls_mm: Sequence[float], eps_eff: float, f: np.ndarray) -> np.ndarray:
    """Best pair |sin(β ΔL)| per frequency (Marks 1991; DeGroot et al. 2002)."""
    L = np.asarray(dls_mm, float) * 1e-3
    beta = 2 * np.pi * np.asarray(f, float) * math.sqrt(eps_eff) / C_LIGHT
    best = np.zeros_like(beta)
    for i, j in itertools.combinations(range(len(L)), 2):
        best = np.maximum(best, np.abs(np.sin(beta * (L[j] - L[i]))))
    return best


# Launch pads: the edge SMA's centre pad (Samtec SMA-J-P-H-ST-EM1, KiCad library footprint
# geometry: 1.27 x 3.2 mm, 0.5 mm from the edge). The 50 Ω coplanar gaps are 2D solves with the
# planes removed under the pad and taper (`python -m yapnr.rf.coupons.families pad --stackup X
# --cut 2` or `--cut 2,3`, FEA environment), not 3D-tuned (design §6.2).
LAUNCHES = {
    "JLC04161H-7628": Launch(1.27, 0.5, 3.2, 0.342, 1.0, (2,), source="2D, L2 cut, L3 reference"),
    "JLC06161H-7628": Launch(
        1.27, 0.5, 3.2, 0.616, 1.0, (2, 3), via_x=7.0, source="2D, L2+L3 cut, L4 reference"
    ),
}


def _trl_sticks(prefix: str, n0: int, fam: str, set_id: str, dls, with_verify: bool, label: str):
    out = []
    n = n0
    for dl in dls:
        kind = "thru" if dl == 0 else "line"
        out.append(
            Stick(
                f"{prefix}{n:02d}",
                kind,
                fam,
                set_id,
                2 * LAUNCH_MM + dl,
                dl=dl,
                elements=[("line", fam, dl)] if dl else [],
                fitted=True,
                determines="calibration; γ(f) of " + fam,
                label=f"{label} {'THRU' if dl == 0 else 'L%g' % dl}",
            )
        )
        n += 1
    out.append(
        Stick(
            f"{prefix}{n:02d}",
            "reflect",
            fam,
            set_id,
            3 * LAUNCH_MM,
            determines="calibration (reflect)",
            label=f"{label} REFL",
        )
    )
    n += 1
    if with_verify:
        out.append(
            Stick(
                f"{prefix}{n:02d}",
                "verify",
                fam,
                set_id,
                2 * LAUNCH_MM + VERIFY_DL,
                dl=VERIFY_DL,
                elements=[("line", fam, VERIFY_DL)],
                determines="residual calibration error",
                label=f"{label} VER{VERIFY_DL:g}",
            )
        )
    return out


def _variant(sid, fam, set_id, ref, junction=None, determines="", label=""):
    els = [("line", fam, VARIANT_MM)]
    if junction:
        els = [("junction", junction)] + els + [("junction", junction)]
    return Stick(
        sid,
        "variant",
        fam,
        set_id,
        2 * LAUNCH_MM + VARIANT_MM,
        elements=els,
        fitted=True,
        determines=determines,
        label=label or f"{sid} {fam}",
        geometry={"ref_family": ref},
    )


def _meanders(layers: Sequence[int]) -> List[dict]:
    """Two meanders per copper layer, 0.2 mm x ~200 mm and 0.5 mm x ~250 mm (design §5.2), as
    columns so both ends face the pad row; layer k is shifted 4 mm so the test points of the
    layers interleave (see layout.dc_stick). `length` includes the two 1.5 mm escapes from the
    meander ends to the Kelvin junctions."""
    n = len(layers)
    width = max(18.0, 4.0 * (n - 1) + 4.0)
    out = []
    for k in layers:
        for w, pitch, target in ((0.2, 0.5, 200.0), (0.5, 1.0, 250.0)):
            cols = int(round(width / pitch)) + 1
            cols += cols % 2  # even: both ends on the same side
            run = round((target - (cols - 1) * pitch) / cols, 2)
            out.append(dict(layer=k, w=w, pitch=pitch, columns=cols, run=run, escape=1.5))
    for m in out:
        m["length"] = round(
            m["columns"] * m["run"] + (m["columns"] - 1) * m["pitch"] + 2 * m["escape"], 3
        )
        m["corners"] = 2 * (m["columns"] - 1)
    return out


def _xsec_height(fams: Sequence[str]) -> float:
    """Microsection stick width: the channels side by side, 0.9 mm of ground between them."""
    from yapnr.rf.coupons import families

    h = 4.0 + sum(2 * families.channel_halfwidth(f) + 0.9 for f in fams)
    return float(math.ceil(h))


def board(stackup_id: str) -> Board:
    st = stackups.get(stackup_id)
    if st.board == "A":
        return _board_a(st)
    if st.board == "B":
        return _board_b(st)
    raise ValueError(f"no coupon catalogue for {stackup_id} (board {st.board})")


def _board_a(st) -> Board:
    s: List[Stick] = []
    s += _trl_sticks("A", 1, "P", "P", TRL_DL, True, "P")
    s.append(
        _variant(
            "A09", "P-MO", "P", "P", determines="mask Dk x thickness; ENIG loss", label="A09 P-MO"
        )
    )
    s.append(_variant("A10", "P0.7", "P", "P", determines="Z0(w), etch vs height"))
    s.append(_variant("A11", "P1.4", "P", "P", determines="Z0(w), tan δ vs roughness"))
    s.append(_variant("A12", "M", "P", "P", "jPM", "second L1 field shape: εr vs height"))
    s.append(_variant("A13", "M-MO", "P", "P", "jPM", "M without mask"))
    # held-out resonators (design §5.2): sizes from the design, re-tuned in `tune` below
    s.append(_ring("A14", "M", 8.906, "P", junction="jPM", label="A14 RING M"))
    s.append(_stub("A15", "P", "open", 7.24, 25.0))
    s.append(_stub("A16", "P", "short", 14.47, 32.0))
    s.append(
        Stick(
            "A17",
            "coupled",
            "CPL20",
            "P",
            40.0,
            elements=[],  # set by `tune`
            determines="held out: Zeven, Zodd, εodd (gap etch, mask in the gap)",
            label="A17 CPL0.20",
            geometry=dict(
                pair_gap=0.20, w=0.348, length=7.06, feed=(20.0 - 7.06) / 2, y_port2=-(0.348 + 0.20)
            ),
        )
    )
    s.append(
        Stick(
            "A18",
            "switch",
            "P",
            "P",
            40.0,
            determines="switch connector in the product path (not predicted)",
            label="A18 SWITCH",
            generated=False,
        )
    )
    s.append(
        Stick(
            "A19",
            "switch2x",
            "P",
            "",
            30.0,
            ports=0,
            determines="probe + pad model, IEEE 370 2x-thru",
            label="A19 SW 2X",
            generated=False,
        )
    )
    s.append(
        Stick(
            "A20",
            "ufl2x",
            "P",
            "",
            30.0,
            ports=0,
            determines="u.FL test point, 2x-thru",
            label="A20 UFL 2X",
            generated=False,
        )
    )
    s.append(
        Stick(
            "A21",
            "dc",
            "",
            "",
            48.0,
            40.0,
            ports=0,
            determines="w·t per layer; etch from the width ratio; via chain",
            label="A21 DC",
            geometry=dict(meanders=_meanders((1, 2, 3, 4)), via_chain=50),
        )
    )
    s.append(
        Stick(
            "A22",
            "xsec",
            "",
            "",
            30.0,
            _xsec_height(["P", "P0.7", "P1.4", "M", "P-MO"]),
            ports=0,
            determines="microsection: h, t, trapezoid, mask thickness",
            label="A22 XSEC",
            geometry=dict(lines=["P", "P0.7", "P1.4", "M", "P-MO"]),
        )
    )
    for sid, kind, what in (
        ("A23", "staircase", "TDR check of Z0(w)"),
        ("A24", "rotated", "glass weave, 10° rotation"),
        ("A25", "rotated", "warp/fill axis"),
        ("A26", "coupled", "CPL-0.15"),
        ("A27", "coupled", "CPL-0.30"),
        ("A28", "coupled", "DIFF100-M"),
        ("A29", "filter", "3-pole edge-coupled band-pass at 5.8 GHz"),
        ("A30", "via2x", "L1-L4-L1 via transition"),
        ("A31", "inverse", "inverse-designed part (#29)"),
    ):
        s.append(Stick(sid, kind, "P", "P", 0.0, tier="extended", determines=what, generated=False))
    b = Board(
        st.id,
        "A",
        LAUNCHES[st.id],
        s,
        {
            "P": dict(
                family="P",
                dl=list(TRL_DL),
                sticks=[x.id for x in s if x.trl == "P" and x.kind in ("thru", "line")],
                reflect="A07",
                verify="A08",
                tdr="A06",
            )
        },
        notes=[
            "edge SMA footprint: Samtec SMA-J-P-H-ST-EM1 geometry (KiCad library), in place of"
            " the design's unverified Cinch 142-0701-851",
            "launch pad gaps are 2D values, not 3D-tuned (design §6.2)",
            "A18-A20 (switch connector, u.FL) and the extended set are not generated yet",
        ],
    )
    return b


RING_ARC = 1.0 / 6.0  # the shorter arc between the two feeds of a ring, as a fraction of a turn


def _ring_height(fam: str, radius: float) -> float:
    """Stick width for a ring whose feeds meet it at y = 0, a sixth of a turn apart: the ring
    spans y from −(1 + cos 30°) R to (1 − cos 30°) R, plus the clearance and a via fence."""
    from yapnr.rf.coupons import families

    clear = families.FAMILIES[fam].w / 2 + (families.M_CLEAR if fam != "S" else families.S_CLEAR)
    reach = (1 + math.cos(math.pi * RING_ARC)) * radius + clear + 1.6
    return float(math.ceil(2 * reach))


def _ring(sid, fam, radius, trl, junction=None, label=""):
    """A directly fed ring (design §5.2 held-out resonator, implemented with direct feeds: a
    gap-coupled ring on 0.21 mm of 7628 with 0.35 mm lines couples too weakly to measure, about
    −70 dB at resonance by the Garg-Bahl gap model). The feeds meet the ring a sixth of a turn
    apart, which puts transmission zeros at the ring resonances n = 1, 2, 4 (2.9, 5.8,
    11.6 GHz) and further zeros at 2.2, 6.5 and 10.9 GHz from the two arcs' difference."""
    radius = float(radius)
    half = radius * math.sin(math.pi * RING_ARC)
    feed = round(VARIANT_MM / 2 - half, 4)
    els = [("line", fam, feed), ("ring2", fam, radius, RING_ARC), ("line", fam, feed)]
    if junction:
        els = [("junction", junction)] + els + [("junction", junction)]
    return Stick(
        sid,
        "ring",
        fam,
        trl,
        2 * LAUNCH_MM + VARIANT_MM,
        _ring_height(fam, radius),
        elements=els,
        determines="held out: εeff at 2.9, 5.8, 11.6 GHz from the ring's notches",
        label=label,
        geometry=dict(radius=radius, arc=RING_ARC, feed=feed),
    )


def _stub(sid, fam, end, length, height):
    return Stick(
        sid,
        "stub",
        fam,
        "P" if fam == "P" else "S",
        60.0,
        height,
        elements=[],  # set by `tune`
        determines=f"held out: {'open-end' if end == 'open' else 'via'} model; notch at 5.8 GHz",
        label=f"{sid} STUB {'OPEN' if end == 'open' else 'SHORT'}",
        geometry=dict(end=end, stub_mm=length),
    )


def _board_b(st) -> Board:
    s: List[Stick] = []
    s += _trl_sticks("B", 1, "S", "S", TRL_DL, True, "S")
    s.append(_variant("B09", "S0.7", "S", "S", determines="etch and heights on L3"))
    s.append(_variant("B10", "S1.4", "S", "S", determines="tan δ vs roughness on L3"))
    s.append(_ring("B11", "S", 7.774, "S", label="B11 RING S"))
    tie = _trl_sticks("B", 12, "P", "PB", TIE_DL, False, "P")
    s += tie
    s.append(
        Stick(
            "B17",
            "via2x",
            "S",
            "",
            40.0,
            determines="held out: the via model (2x-thru, not predicted)",
            label="B17 VIA 2X",
            generated=False,
        )
    )
    s.append(
        Stick(
            "B18",
            "dc",
            "",
            "",
            56.0,
            40.0,
            ports=0,
            determines="w·t and etch per layer; via chain",
            label="B18 DC",
            geometry=dict(meanders=_meanders((1, 2, 3, 4, 5, 6)), via_chain=50),
        )
    )
    s.append(
        Stick(
            "B19",
            "xsec",
            "",
            "",
            30.0,
            _xsec_height(["S", "S0.7", "S1.4", "P"]),
            ports=0,
            determines="microsection: h_core, h_pp, t_L3",
            label="B19 XSEC",
            geometry=dict(lines=["S", "S0.7", "S1.4", "P"]),
        )
    )
    for sid, kind, what in (
        ("B20", "coupled", "DIFF100-S"),
        ("B21", "coupled", "DIFF90-S"),
        ("B22", "stub", "S open stub"),
        ("B23", "rotated", "weave on L3"),
    ):
        s.append(Stick(sid, kind, "S", "S", 0.0, tier="extended", determines=what, generated=False))
    return Board(
        st.id,
        "B",
        LAUNCHES[st.id],
        s,
        {
            "S": dict(
                family="S",
                dl=list(TRL_DL),
                sticks=[x.id for x in s if x.trl == "S" and x.kind in ("thru", "line")],
                reflect="B07",
                verify="B08",
                tdr="B06",
            ),
            "PB": dict(
                family="P",
                dl=list(TIE_DL),
                sticks=[x.id for x in tie if x.kind in ("thru", "line")],
                reflect="B16",
                verify="",
                tdr="B15",
            ),
        },
        notes=[
            "edge SMA footprint: Samtec SMA-J-P-H-ST-EM1 geometry (KiCad library)",
            "L1 -> L3 transition: 0.3 mm via, antipads 1.0 mm on L2, L4-L6, four ground vias on"
            " a 0.9 mm radius; not 3D-tuned",
            "B17 (via 2x-thru) and the extended set are not generated yet",
        ],
    )


def tune(b: Board, model) -> Board:
    """Set the resonator and coupler lengths at nominal parameters so that their features land
    at 5.8 GHz (`model` is a models.Model at nominal values over a fine frequency grid)."""
    from yapnr.rf.coupons import models

    f = model.f
    for s in b.sticks:
        if s.kind == "stub" and s.generated:
            fam = s.family
            end = s.geometry["end"]
            line = model.line(fam)
            # electrical length from the main line's centre: notch at F0
            target = 0.25 if end == "open" else 0.5
            k = int(np.argmin(abs(f - F0)))
            lam = 2 * math.pi / line.gamma[k].imag * 1e3
            w = 0.291
            if end == "open":
                ext = models.open_end_extension(w, model.v["pp1.h"], model.v["pp.dk"], 3.18)
                drawn = target * lam - w / 2 - ext
                model_len = drawn + w / 2 + ext
            else:
                # via inductance as an equivalent extra length: L_via = L' Δl
                l_per_m = (line.zc[k] * line.gamma[k] / (1j * 2 * math.pi * F0)).real
                lv = models.via_inductance(model.v["pp1.h"], 0.3) / 3.0
                ext = lv / l_per_m * 1e3
                drawn = target * lam - w / 2 - ext
                model_len = drawn + w / 2
            s.geometry["stub_mm"] = round(drawn, 3)
            half = VARIANT_MM / 2
            s.elements = [
                ("line", fam, half),
                ("stub", fam, round(model_len, 4), end),
                ("line", fam, half),
            ]
        if s.kind == "ring" and s.generated:
            fam = s.family
            line = model.line(fam)
            k = int(np.argmin(abs(f - F0 / 2)))
            lam = 2 * math.pi / line.gamma[k].imag * 1e3  # guided wavelength at 2.9 GHz
            r = float(round(lam / (2 * math.pi), 3))
            rebuilt = _ring(
                s.id, fam, r, s.trl, "jPM" if s.elements[0][0] == "junction" else None, s.label
            )
            s.elements, s.geometry, s.height = rebuilt.elements, rebuilt.geometry, rebuilt.height
        if s.kind == "coupled" and s.generated:
            le, lo = model.line("CPL20", "e"), model.line("CPL20", "o")
            k = int(np.argmin(abs(f - F0)))
            beta = 0.5 * (le.gamma[k].imag + lo.gamma[k].imag)
            ext = models.open_end_extension(0.348, model.v["pp1.h"], model.v["pp.dk"], 3.4)
            lc = (math.pi / 2) / beta * 1e3 - ext
            s.geometry["length"] = round(lc, 3)
            a = (s.length - 2 * LAUNCH_MM - lc) / 2
            s.geometry["feed"] = round(a, 4)
            s.elements = [
                ("junction", "jPM"),
                ("line", "M", a),
                ("coupled", "CPL20", round(lc + ext, 4)),
                ("line", "M", a),
                ("junction", "jPM"),
            ]
    return b
