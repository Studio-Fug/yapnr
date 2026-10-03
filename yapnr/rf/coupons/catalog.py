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
    design: str = ""  # a 2D-designed launch, "<board id>:<region>" (launch.design); "" Samtec


@dataclass
class Board:
    stackup: str
    letter: str
    launch: Launch
    sticks: List[Stick]
    trl: Dict[str, dict]
    notes: List[str] = field(default_factory=list)
    upload: str = ""  # board O: the OSH Park upload ("M", "W" or "D")
    panel: str = "jlc"  # "jlc": framed, rails and 2 mm slots; "osh": frameless, 2.54 mm slots
    launches: Dict[str, Launch] = field(default_factory=dict)  # per region, board O

    @property
    def name(self) -> str:
        """File name stem of the board's KiCad project."""
        return f"O0-{self.upload}" if self.upload else f"board-{self.letter}"

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
            **({"upload": self.upload} if self.upload else {}),
            "launch": asdict(self.launch),
            **(
                {"launches": {k: asdict(v) for k, v in self.launches.items()}}
                if self.launches
                else {}
            ),
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
    "JLC06161H-2116C": Launch(
        1.27, 0.5, 3.2, 0.435, 1.0, (2, 3), via_x=7.0, source="2D, L2+L3 cut, L4 reference"
    ),
}


def designed_launch(key: str) -> Launch:
    """The `Launch` record of a 2D-designed edge launch (`launch.design`)."""
    from yapnr.rf.coupons import launch

    ld = launch.design(key)
    return Launch(
        ld.pad_w,
        ld.x0,
        round(ld.x_pe - ld.x0, 4),
        ld.gap_tab,
        round(ld.x_te - ld.x_pe, 4),
        ld.cut_layers,
        source=f"2D (launch.py), {ld.conn.part}, region {ld.region}",
        design=key,
    )


def launch_check_board(region: str, board_id: str = "oshpark-4l-fr408hr") -> Board:
    """A small board that carries one region's launch on a thru and a line stick (Order 0
    design: A01/A03 on M, B01/B03 on W), for KiCad's DRC and for a look at the geometry. It
    is not an Order 0 board: the Order 0 catalogue (board O) builds on the same launch."""
    from yapnr.rf.coupons import launch

    key = f"{board_id}:{region}"
    ld = launch.design(key)
    w = launch.STICK_W[region]
    dl = 13.0 if region == "M" else 14.0
    sticks = [
        Stick(f"{region}01", "thru", region, "", 2 * LAUNCH_MM, w, label=f"LC {region} THRU"),
        Stick(
            f"{region}02",
            "line",
            region,
            "",
            2 * LAUNCH_MM + dl,
            w,
            dl=dl,
            label=f"LC {region} L{dl:g}",
        ),
    ]
    return Board(
        "OSHPARK-4L-FR408HR",
        "O",
        designed_launch(key),
        sticks,
        {},
        notes=[
            f"launch check, region {region}: {ld.conn.part} on a {ld.line_w} mm line",
            "the launch is 2D-designed (launch.py), not 3D-tuned",
        ],
    )


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
                label=f"{prefix}{n:02d} {label} {'THRU' if dl == 0 else 'L%g' % dl}",
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
            label=f"{prefix}{n:02d} {label} REFL",
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
                label=f"{prefix}{n:02d} {label} VER{VERIFY_DL:g}",
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


def _xsec_height(fams: Sequence[str], stackup_id=None) -> float:
    """Microsection stick width: the channels side by side, 0.9 mm of ground between them."""
    from yapnr.rf.coupons import families

    h = 4.0 + sum(2 * families.channel_halfwidth(f, stackup_id) + 0.9 for f in fams)
    return float(math.ceil(h))


def board(stackup_id: str, upload: str = "") -> Board:
    """The stick catalogue of a coupon board; board O (OSH Park, Order 0) by upload: "M" (the
    thin-microstrip coupons and R1, the default), "W" (the thick-microstrip set, R1t and the D2
    window) or "D" (the D1 window, an R1 copy, a thru and a line)."""
    st = stackups.get(stackup_id)
    if st.board == "A":
        return _board_a(st)
    if st.board == "B":
        return _board_b(st)
    if st.board == "O":
        return _board_o(st, upload or "M")
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


def _ring_height(fam: str, radius: float, stackup_id=None) -> float:
    """Stick width for a ring whose feeds meet it at y = 0, a sixth of a turn apart: the ring
    spans y from −(1 + cos 30°) R to (1 − cos 30°) R, plus the clearance and a via fence."""
    from yapnr.rf.coupons import families

    clear = families.get(fam, stackup_id).w / 2 + (
        families.M_CLEAR if fam != "S" else families.S_CLEAR
    )
    reach = (1 + math.cos(math.pi * RING_ARC)) * radius + clear + 1.6
    return float(math.ceil(2 * reach))


def _ring(sid, fam, radius, trl, junction=None, label="", stackup_id=None):
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
        _ring_height(fam, radius, stackup_id),
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
    from yapnr.rf.coupons import families

    s: List[Stick] = []
    s += _trl_sticks("B", 1, "S", "S", TRL_DL, True, "S")
    s.append(_variant("B09", "S0.7", "S", "S", determines="etch and heights on L3"))
    s.append(_variant("B10", "S1.4", "S", "S", determines="tan δ vs roughness on L3"))
    s.append(_ring("B11", "S", 7.774, "S", label="B11 RING S", stackup_id=st.id))
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
            _xsec_height(["S", "S0.7", "S1.4", "P"], st.id),
            ports=0,
            determines="microsection: h_core, h_pp, t_L3",
            label="B19 XSEC",
            geometry=dict(lines=["S", "S0.7", "S1.4", "P"]),
        )
    )
    if "P-MO" in families.BOARD_FAMILIES.get(st.id, ()):
        # JLC06161H-2116C: board B no longer shares L1 with board A, so a mask-off copy of the
        # tie line separates the mask from the 2116's εr on board B itself (design §8.8)
        s.append(
            _variant(
                "B24",
                "P-MO",
                "PB",
                "P",
                determines="mask Dk x thickness on L1 (board B self-sufficient)",
                label="B24 P-MO",
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


# --- board O: the OSH Park 4-layer Order 0 uploads (Order 0 design §3-§6, §16) -------------------

O_TRL_M = (0.0, 4.5, 13.0, 30.0, 70.0)  # design §4.2: thru + ΔL 4.5, 13.0, 30, 70 mm
O_TRL_W = (0.0, 5.0, 14.0, 34.0)  # design §4.3: thru + ΔL 5, 14, 34 mm
# O0-D's own lot: thru + ΔL 9 and 30 mm (review M2: A04 alone has |sin βΔL| 0.08 at 6 GHz; with
# 9 mm the best pair keeps >= 0.958 over D1's 4.25-5.75 GHz and >= 0.81 over 1-6 GHz for εeff
# 2.60-2.95, derived)
O_TRL_D = (0.0, 9.0, 30.0)
O_VERIFY_M = 21.0
O_VARIANT_MM = 30.0
O_RING_ARC = 0.25  # feeds a quarter turn apart: notches at the odd resonances n = 1, 3
O_RING_R = 15.1  # mean radius (design §4.2 A11), re-tuned to n = 3 at 5.8 GHz in `tune`
O_STUB_LINE = 15.0  # A12: the open stub's 15 mm line between the reference planes
O_SWITCH_X = 5.0  # A14: the shunt resistor 5 mm from RP1
O_SHUNT_R, O_SHUNT_L = 100.0, 0.6e-9  # A14: 0402 100 Ω; ESL + vias + pad, est.
O_STICK_W = {"M": 12.0, "W": 16.0}  # design §3.2 (review)
O_FEED = {"M": 3.0, "W": 4.0}  # design §4.4: the feed L_f from the reference plane to the window
O_KEEP = {"M": 1.0, "W": 4.2}  # L1 keep-away (5 h, 3 h)
O_PITCH = {"M": 0.10, "W": 0.50}  # the optimizer's grid (design §3.1 rf block)
O_LINE = {"M": "M", "W": "W"}
O_REV = "A"
# The sticks' turns in each upload's panel (layout_o.Placed.rot), chosen by running the panel
# search over every combination (review F5: tabs only on edges without a launch, so a demo
# hangs by its port-free east edge; O0-M's A04R and the tag link the lines; O0-D turns its
# lines too). O0-M 26.9, O0-W 11.7, O0-D 7.2 sq in.
O_ROTS = {
    "M": {"R1": 180, "A04R": 90},
    "W": {"R1t": 90, "D2": 270},
    "D": {"R1": 0, "D1": 180, "A01": 90, "A20": 90, "A04": 90},
}

# The demo windows (design §4.4, spec sketch of divider-osh-m; divider-osh-w by analogy: the
# specs are not written yet). x from the window's west edge (P1's plane), y across, ports W (y),
# N and S (x). A filled window (R1, R1t) carries the reference's copper; a placeholder (D1, D2)
# only its feeds, outline and keep-away until the optimizer's footprint exists.
O_WINDOWS = {
    "D1": dict(region="M", w=12.0, h=15.0, ports=(("W", 0.0), ("N", 6.0), ("S", 6.0))),
    "D2": dict(region="W", w=20.0, h=24.0, ports=(("W", 0.0), ("N", 10.0), ("S", 10.0))),
}


# R1 / R1t from 2D quasi-static solves at 5.0 GHz, nominal FR408HR, 43 µm copper, the coupon
# model's dispersion (scratch solves with families.solve_point at the arm width; derived): the
# output line (Z, εeff), the arm (width, Z, εeff, λ/4), the substrate height of the junction model.
O_REF_2D = {
    "M": dict(z_out=50.66, eps_out=2.7062, w_arm=0.70, z_arm=35.72, eps_arm=2.8667, qw=8.8532,
              h=0.1999, w_out=0.40, win_h=12.0),
    "W": dict(z_out=48.64, eps_out=2.9173, w_arm=5.0, z_arm=34.47, eps_arm=3.0521, qw=8.5800,
              h=1.3904, w_out=3.0, win_h=22.0),
}  # fmt: skip
# The references' FDTD forward runs use refine 2 of the optimizer's grid (M 0.05 mm, W 0.25 mm):
# R1's 0.70 mm arm (7 cells at 0.10 mm, centred on a node) cannot be drawn on the 0.10 mm grid,
# 14 cells at 0.05 mm can (review H2).
O_REF_PITCH = {"M": 0.05, "W": 0.25}
F_REF_O = 5.0e9  # the references' centre frequency


def o_reference(region: str) -> dict:
    """R1 / R1t: the lossless T-junction with a λ/4 transformer [Pozar §7.2, §5.5] for
    4.25-5.75 GHz. The arm is the 2D-solved width nearest the ideal sqrt(Z_out · Z_out / 2) on
    the reference grid (M 0.70 mm, 35.7 Ω against 35.8; W 5.0 mm, 34.5 Ω against 34.4), and λ/4
    long at 5.0 GHz from the T-junction's branch reference plane, which sits `d_branch` from the
    output line's centre line (Hammerstad's model, `models.tee_offsets`; M 0.37 mm, W 2.18 mm).
    Counted to the junction centre, as before the review (M1), the arm was short by d_branch:
    R1 4 % and R1t 25 % high in frequency. The drawn length is snapped to the refine-2 grid
    (O_REF_PITCH); the step from the feed to the arm is not corrected (est.)."""
    from yapnr.rf.coupons import models

    r = O_REF_2D[region]
    pitch = O_REF_PITCH[region]
    w_out, w_arm, h = r["w_out"], r["w_arm"], r["win_h"]
    _, d_branch = models.tee_offsets(
        r["z_out"], r["eps_out"], r["z_arm"], r["eps_arm"], r["h"], F_REF_O
    )
    l_ideal = r["qw"] + d_branch  # window edge to the junction centre
    arm = round(round((l_ideal - w_out / 2) / pitch) * pitch, 4)  # window edge to the output line
    l_centre = round(arm + w_out / 2, 4)
    f0 = F_REF_O / 1e9 * r["qw"] / (l_centre - d_branch)
    w = round(arm + w_out, 4)
    copper = [
        (0.0, arm, -w_arm / 2, w_arm / 2),  # the λ/4 arm
        (arm, arm + w_out, -h / 2, h / 2),  # the 50 Ω outputs through the junction
    ]
    return dict(
        region=region,
        w=w,
        h=h,
        ports=(("W", 0.0), ("N", l_centre), ("S", l_centre)),
        copper=[tuple(round(v, 4) for v in c) for c in copper],
        arm=dict(
            w=w_arm,
            z0_2d=r["z_arm"],
            quarter_wave_mm=r["qw"],
            l_to_centre=l_centre,
            junction_d_branch_mm=round(d_branch, 4),
            f0_ghz_est=round(f0, 3),
        ),
        fdtd_pitch_mm=pitch,
        source="Pozar §7.2 (T-junction) and §5.5 (λ/4 transformer); 2D solves; Hammerstad's"
        " T-junction reference plane; snapped to the refine-2 grid",
    )


def _o_line_sticks(prefix, n0, region, dls, set_id, label, verify=None) -> List[Stick]:
    fam = O_LINE[region]
    w = O_STICK_W[region]
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
                w,
                dl=dl,
                elements=[("line", fam, dl)] if dl else [],
                fitted=True,
                determines=(
                    "mTRL thru and launch-only 2x-thru (IEEE 370)"
                    if dl == 0
                    else f"calibration; γ(f) of {fam}"
                ),
                label=f"{label} {prefix}{n:02d} {fam} " + ("THRU" if dl == 0 else f"dL{dl:g}"),
                geometry=dict(region=region),
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
            w,
            determines="mTRL reflect (open at both reference planes)",
            label=f"{label} {prefix}{n:02d} {fam} OPEN",
            geometry=dict(region=region, end="open"),
        )
    )
    n += 1
    if verify:
        out.append(
            Stick(
                f"{prefix}{n:02d}",
                "verify",
                fam,
                set_id,
                2 * LAUNCH_MM + verify,
                w,
                dl=verify,
                elements=[("line", fam, verify)],
                determines="residual calibration error, connector repeatability",
                label=f"{label} {prefix}{n:02d} {fam} VER{verify:g}",
                geometry=dict(region=region),
            )
        )
    return out


def _o_variant(sid, fam, label, determines, mask=False) -> Stick:
    return Stick(
        sid,
        "variant",
        fam,
        "M",
        2 * LAUNCH_MM + O_VARIANT_MM,
        O_STICK_W["M"],
        elements=[("line", fam, O_VARIANT_MM)],
        fitted=True,
        determines=determines,
        label=f"{label} {sid} {fam} {O_VARIANT_MM:g}",
        geometry=dict(region="M", ref_family="M", masked=mask),
    )


def _o_demo(sid, kind, region, label, determines, window, ports_n=3, tier="core") -> Stick:
    """A 3-port demo stick: P1 on the west edge, P2 and P3 on the north and south edges, each
    with the standard launch half (10 mm to its reference plane) and a straight feed L_f from
    the reference plane to the window (design §4.4)."""
    lf = O_FEED[region]
    w_stick = O_STICK_W[region]
    xw = LAUNCH_MM + lf
    px = max(p for side, p in window["ports"] if side in ("N", "S"))
    from yapnr.rf.coupons import launch

    leg = launch.CINCH_142_0701_851.gnd_pad_y[1]
    keep = O_KEEP[region]
    # east of the window: the keep-away, then (M) room for the label along the east edge
    margin = 2.6 if region == "M" else 1.6
    east = max(xw + window["w"] + keep + margin, xw + px + leg + 0.381 + 1.0)
    length = float(math.ceil(east))
    height = window["h"] + 2 * (lf + LAUNCH_MM)
    g = dict(
        region=region,
        window=dict(window, x0=xw, feed=lf),
        ports=[dict(n=1, side="W", at=0.0)]
        + [
            dict(n=k + 2, side=side, at=round(xw + p, 4))
            for k, (side, p) in enumerate(q for q in window["ports"] if q[0] in ("N", "S"))
        ],
        stick_w=w_stick,
    )
    if kind == "window":
        g["placeholder"] = True  # `yapnr fab check` refuses the board until it is filled
    return Stick(
        sid,
        kind,
        O_LINE[region],
        "M" if region == "M" else "W",
        length,
        float(height),
        ports=ports_n,
        tier=tier,
        determines=determines,
        label=label,
        geometry=g,
    )


def _o_ring(sid, label) -> Stick:
    """A11: a directly fed M ring, the feeds a quarter turn apart: notches at the odd resonances
    n = 1 (1.9 GHz, in the LibreVNA's clean band) and n = 3 (5.8 GHz, the product band); at n = 2
    both arcs are half-wave multiples and the ring passes. The port axis sits off the stick
    centre so the ring fits a 36 mm stick."""
    r = O_RING_R
    rebuilt = _o_ring_geometry(sid, r, label)
    return rebuilt


def _o_ring_geometry(sid, r, label) -> Stick:
    from yapnr.rf.coupons import families

    half = r * math.sin(math.pi * O_RING_ARC)
    feed = 18.0 - half  # RP-to-ring feed: 36 mm between the reference planes
    length = 2 * LAUNCH_MM + 36.0
    clear = families.get("M", "OSHPARK-4L-FR408HR").w / 2 + O_KEEP["M"]
    top = r * (1 - math.cos(math.pi * O_RING_ARC)) + clear + 1.6
    bottom = r * (1 + math.cos(math.pi * O_RING_ARC)) + clear + 1.6
    top = max(top, O_STICK_W["M"] / 2)
    height = float(math.ceil(top + bottom))
    axis_y = round(-height / 2 + top, 4)  # port axis from the stick centre (negative: north)
    return Stick(
        sid,
        "ring",
        "M",
        "M",
        length,
        height,
        elements=[
            ("line", "M", round(feed, 4)),
            ("ring2", "M", r, O_RING_ARC),
            ("line", "M", round(feed, 4)),
        ],
        determines="held out: εeff at the ring's notches n = 1 and 3 (1.9 and 5.8 GHz)",
        label=label,
        geometry=dict(region="M", radius=r, arc=O_RING_ARC, feed=round(feed, 4), axis_y=axis_y),
    )


def _o_stub(sid, label, stub_mm=7.81) -> Stick:
    """A12: an open λ/4 stub (notch at F_STUB_O), shunt at the middle of a 15 mm M line (the stub's
    length is set at nominal by `tune`)."""
    clear = 0.2 + O_KEEP["M"]
    bottom = 0.2 + stub_mm + O_KEEP["M"] + 2.6
    top = O_STICK_W["M"] / 2
    height = float(math.ceil(top + bottom))
    return Stick(
        sid,
        "stub",
        "M",
        "M",
        2 * LAUNCH_MM + O_STUB_LINE,
        height,
        elements=[],
        determines="held out: the open-end and T-junction models; notch at 5.5 GHz",
        label=label,
        geometry=dict(
            region="M", end="open", stub_mm=stub_mm, axis_y=round(-height / 2 + top, 4), clear=clear
        ),
    )


def _o_board_notes(upload: str) -> List[str]:
    return [
        "OSH Park 4 layer, FR408HR (EM528 alternate covered by the same tables); 3 copies;"
        " frameless single outline, 2.54 mm milled slots, OSH Park's mouse-bite tab pattern on"
        " the sticks' long sides only",
        "edge SMA: Cinch 142-0701-851 on every port (custom footprint from Cinch's drawing);"
        " the launches are 2D-designed (launch.py), not 3D-tuned",
        "mask open over all RF copper and its keep-away, 0.2 mm dams at the pin pads; A10 is"
        " the masked line",
    ] + (
        ["D2 window is a placeholder: the optimizer's footprint is added when D2 passes"]
        if upload == "W"
        else (
            ["D1 window is a placeholder: the optimizer's footprint is added when D1 passes"]
            if upload == "D"
            else []
        )
    )


def _board_o(st, upload: str) -> Board:
    up = upload.upper()
    tag = f"O0-{up}"
    s: List[Stick] = []
    trl: Dict[str, dict] = {}
    if up == "M":
        s += _o_line_sticks("A", 1, "M", O_TRL_M, "M", tag, verify=O_VERIFY_M)
        s.append(
            _o_variant("A08", "M0.7", tag, "Z0(w) and α(w): h, etch and εr; the thickness bias")
        )
        s.append(_o_variant("A09", "M1.4", tag, "Z0(w) and α(w): tan δ against roughness"))
        s.append(
            _o_variant(
                "A10",
                "M-MK",
                tag,
                "mask Dk x thickness: OSH Park's default (masked) against mask-open",
                mask=True,
            )
        )
        s.append(_o_ring("A11", f"{tag} A11 RING M"))
        s.append(_o_stub("A12", f"{tag} A12 STUB"))
        s.append(
            Stick(
                "A14",
                "switch",
                "M",
                "M",
                2 * LAUNCH_MM + O_VARIANT_MM,
                O_STICK_W["M"],
                elements=[
                    ("line", "M", O_SWITCH_X),
                    ("shunt_rl", O_SHUNT_R, O_SHUNT_L),
                    ("line", "M", O_VARIANT_MM - O_SWITCH_X),
                ],
                determines="switch terms (Hatab): asymmetric, reciprocal, transmissive; both"
                " orientations",
                label=f"{tag} A14 SHUNT 100R",
                geometry=dict(region="M", x_shunt=O_SWITCH_X, part="0402 100 ohm 1 %"),
            )
        )
        s.append(
            Stick(
                "A15",
                "tag",
                "",
                "",
                68.0,
                24.0,
                ports=0,
                determines="w·t and etch on L1 (4-wire meanders); microsection; the tag and QR",
                label=f"{tag} A15 DC XSEC TAG",
                geometry=dict(
                    region="M",
                    lines=["M", "M0.7", "M1.4", "M-MK"],
                    meanders=[
                        dict(layer=1, w=0.2, length=200.0),
                        dict(layer=1, w=0.5, length=250.0),
                    ],
                    url="https://github.com/Studio-Fug/yapnr/tree/main/docs/rf/order0",
                ),
            )
        )
        s.append(
            Stick(
                "A16",
                "cpad",
                "M",
                "M",
                50.0,
                16.0,
                ports=2,
                determines="h (and so the absolute Z0) from C at 30-300 MHz: 6 and 12 mm pads,"
                " two 1-ports, tier-1 data",
                label=f"{tag} A16",
                geometry=dict(region="M", pads=[6.0, 12.0], families=["C6", "C12"]),
            )
        )
        a04r = Stick(
            "A04R",
            "line",
            "M",
            "M",
            2 * LAUNCH_MM + 30.0,
            O_STICK_W["M"],
            dl=30.0,
            elements=[("line", "M", 30.0)],
            determines="glass-weave warp/fill: A04 rotated 90° (not part of the mTRL set)",
            label=f"{tag} A04R M dL30 ROT",
            geometry=dict(region="M", rot=90),
        )
        s.append(a04r)
        ref = o_reference("M")
        s.append(
            _o_demo(
                "R1",
                "demo",
                "M",
                f"{tag} R1 ref Pozar 7.2",
                "textbook reference for D1: T-junction + λ/4 35.36 Ω, 4.25-5.75 GHz",
                ref,
            )
        )
        trl["M"] = dict(
            family="M",
            dl=list(O_TRL_M),
            sticks=[
                x.id for x in s if x.trl == "M" and x.kind in ("thru", "line") and x.id != "A04R"
            ],
            reflect="A06",
            verify="A07",
            tdr="A05",
        )
    elif up == "W":
        s += _o_line_sticks("B", 1, "W", O_TRL_W, "W", tag)
        s.append(
            _o_demo(
                "R1t",
                "demo",
                "W",
                f"{tag} R1t ref Pozar 7.2",
                "textbook reference for D2: T-junction + λ/4 35.36 Ω on W",
                o_reference("W"),
            )
        )
        s.append(
            _o_demo(
                "D2",
                "window",
                "W",
                f"{tag} D2 opt divider",
                "optimizer divider, thick (placeholder window until D2 passes)",
                O_WINDOWS["D2"],
            )
        )
        trl["W"] = dict(
            family="W",
            dl=list(O_TRL_W),
            sticks=[x.id for x in s if x.trl == "W" and x.kind in ("thru", "line")],
            reflect="B05",
            verify="",
            tdr="B04",
        )
    elif up == "D":
        s += [x for x in _o_line_sticks("A", 1, "M", O_TRL_D, "M", tag) if x.kind != "reflect"]
        # the O0-M ids where the line is the same (A01, A04); the 9 mm line is O0-D's own
        for x, sid in zip(s, ("A01", "A20", "A04")):
            x.id = sid
            x.label = f"{tag} {sid} M " + ("THRU" if x.dl == 0 else f"dL{x.dl:g}")
        s.append(
            _o_demo(
                "R1",
                "demo",
                "M",
                f"{tag} R1 ref Pozar 7.2",
                "R1 copy on the D1 lot",
                o_reference("M"),
            )
        )
        s.append(
            _o_demo(
                "D1",
                "window",
                "M",
                f"{tag} D1 opt divider",
                "optimizer divider, thin: the headline demo (placeholder window until D1 passes)",
                O_WINDOWS["D1"],
            )
        )
        trl["M"] = dict(
            family="M",
            dl=list(O_TRL_D),
            sticks=["A01", "A20", "A04"],
            reflect="",
            verify="",
            tdr="",
        )
    else:
        raise ValueError(f"board O uploads are M, W and D, not {upload!r}")
    for x in s:
        if x.id in O_ROTS[up]:
            x.geometry["rot"] = O_ROTS[up][x.id]
    launches = {reg: designed_launch(f"oshpark-4l-fr408hr:{reg}") for reg in ("M", "W")}
    main_region = "W" if up == "W" else "M"
    return Board(
        st.id,
        "O",
        launches[main_region],
        s,
        trl,
        notes=_o_board_notes(up),
        upload=up,
        panel="osh",
        launches=launches,
    )


F_STUB_O = 5.5e9  # A12's notch (review H1: inside the LibreVNA's 6 GHz with its upper shoulder)


def tune_o(b: Board, model) -> Board:
    """Board O at nominal FR408HR: the ring's radius puts its n = 3 notch at 5.8 GHz, the open
    stub's drawn length its notch at F_STUB_O. Both use Hammerstad's T-junction reference planes
    (`models.tee_offsets`): the stub counts from its plane `d_branch` off the main line's centre
    line (0.28 mm, beyond the 0.20 mm line edge) plus the open-end extension; each ring arc is
    shortened by the feeds' `d_main` at both ends; the main line and the feeds by theirs."""
    from yapnr.rf.coupons import families, models

    f = model.f

    def at(f0):
        line = model.line("M")
        k = int(np.argmin(abs(f - f0)))
        lam = 2 * math.pi / line.gamma[k].imag * 1e3
        z, eps = float(line.zc[k].real), float(line.eps_eff[k])
        d_main, d_branch = models.tee_offsets(z, eps, z, eps, model.v["pp1.h"], f0)
        return lam, eps, d_main, d_branch

    for s in b.sticks:
        if s.kind == "ring":
            lam, _, d_main, d_branch = at(F0)
            # 2πr - 4 d_main = 3λ at F0
            r = float(round((3 * lam + 4 * d_main) / (2 * math.pi), 3))
            rebuilt = _o_ring_geometry(s.id, r, s.label)
            s.geometry, s.height = rebuilt.geometry, rebuilt.height
            feed = rebuilt.geometry["feed"]
            s.elements = [
                ("line", "M", round(feed - d_branch, 4)),
                ("ring2", "M", r, O_RING_ARC, round(d_main, 4)),
                ("line", "M", round(feed - d_branch, 4)),
            ]
            s.geometry["junction"] = dict(d_main=round(d_main, 4), d_branch=round(d_branch, 4))
        if s.kind == "stub":
            lam, eps, d_main, d_branch = at(F_STUB_O)
            fam = families.get("M", b.stackup)
            w = fam.w
            h_kj, er_kj = fam.dispersion_substrate(model.v)
            ext = models.open_end_extension(w, h_kj, er_kj, eps)
            # from the main line's edge: λ/4 - open end + (d_branch - w/2)
            drawn = 0.25 * lam - ext + d_branch - w / 2
            rebuilt = _o_stub(s.id, s.label, round(drawn, 3))
            s.geometry, s.height = rebuilt.geometry, rebuilt.height
            half = O_STUB_LINE / 2
            electrical = round(drawn, 3) + w / 2 - d_branch + ext
            s.elements = [
                ("line", "M", round(half - d_main, 4)),
                ("stub", "M", round(electrical, 4), "open"),
                ("line", "M", round(half - d_main, 4)),
            ]
            s.geometry["junction"] = dict(d_main=round(d_main, 4), d_branch=round(d_branch, 4))
            s.geometry["notch_ghz_target"] = F_STUB_O / 1e9
    return b
