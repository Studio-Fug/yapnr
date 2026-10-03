#!/usr/bin/env python3
"""ERC-equivalent checks of the built radar60 board (the atopile output, parts unplaced).

atopile writes no schematic, so KiCad's ERC has nothing to run on. This script checks the
netlist in the built ``.kicad_pcb`` instead:

1. **Pin table**: U1's footprint carries exactly the 161 balls of the ABL0161 ball map
   (``data/abl0161_ballmap.json``, from TI SWRS219F); the plan's pin assignments
   (board-design.md 3.2) agree with that ball map; every ball is on the net the plan gives it, and
   the balls the plan leaves unused are on no other pad.
2. **Power**: rails are traced from their sources (J1, the eFuse, the four bucks) through fitted
   inductors, ferrite beads and 0 Ohm / shunt resistors; every power-input pin must sit on a rail
   whose nominal voltage is inside the pin's data-sheet range, and every ground pin on GND.
3. **Pin conflicts**: no net with two push-pull outputs; no input pin on a net with nothing that
   drives or pulls it; no fitted IC pin that must be connected left alone on a net.
4. **Annotations**: every ``@pnr-*`` target in the sources resolves to a footprint (by atopile
   address) and its pads exist; a ``@pnr-current`` pad set lies on one net.
5. **Fit options and quantified warnings**: the two PMIC CLKIN options are never both fitted
   (that shorts SOP2 to GND); the 1.0 V rail's total capacitance against the LP87524 limit and its
   DC window at the balls are printed as quantified warnings (design assumptions for review).

Usage::

    tools/check_schematic.py BOARD.kicad_pcb [--sources elec/src] [--json report.json]

Exit status 1 when any error is found. Stdlib only, Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
BALLMAP = HERE / "data" / "abl0161_ballmap.json"

# --- the plan's pin table (board-design.md 3.2), ball -> data-sheet ball name it expects -----
PLAN_BALLS = {
    "B4": "TX1",
    "B6": "TX2",
    "B8": "TX3",
    "M2": "RX1",
    "K2": "RX2",
    "H2": "RX3",
    "F2": "RX4",
    "N4": "RS232_RX",
    "N5": "RS232_TX",
    "H14": "SPIB_CS_N",
    "F14": "SPIB_CLK",
    "E13": "SPIA_CLK",
    "E15": "SPIA_CS_N",
    "E14": "SPIA_MISO",
    "D13": "SPIA_MOSI",
    "G14": "SPIB_MISO",
    "F13": "SPIB_MOSI",
    "R12": "QSPI_CLK",
    "P11": "QSPI_CS_N",
    "R13": "QSPI[0]",
    "N12": "QSPI[1]",
    "R14": "QSPI[2]",
    "P12": "QSPI[3]",
    "J14": "LVDS_TXP[0]",
    "J15": "LVDS_TXM[0]",
    "K14": "LVDS_TXP[1]",
    "K15": "LVDS_TXM[1]",
    "L14": "LVDS_CLKP",
    "L15": "LVDS_CLKM",
    "M14": "LVDS_FRCLKP",
    "M15": "LVDS_FRCLKM",
    "P10": "TCK",
    "N10": "TMS",
    "R11": "TDI",
    "N13": "TDO",
    "P9": "PMIC_CLKOUT",
    "G13": "SYNC_OUT",
    "R3": "NRESET",
    "N6": "NERROR_OUT",
    "N9": "WARM_RESET",
    "N8": "MCU_CLKOUT",
    "B10": "VBGAP",
    "A10": "VOUT_14APLL",
    "B13": "VOUT_14SYNTH",
    "P4": "SYNC_IN",
    "A14": "OSC_CLKOUT",
    "L13": "VPP",
    "G5": "VIN_13RF1",
    "H5": "VIN_13RF1",
    "J5": "VIN_13RF1",
    "C2": "VIN_13RF2",
    "D2": "VIN_13RF2",
    "A2": "VOUT_PA",
    "B2": "VOUT_PA",
    "B15": "CLKP",
    "C15": "CLKM",
}

# --- data-sheet ball name -> net this design puts it on ----------------------------------------
NET_BY_BALLNAME = {
    "VSS": "GND",
    "VSSA": "GND",
    "VDDIN": "1V2",
    "VIN_SRAM": "1V2",
    "VNWA": "1V2",
    "VIOIN": "3V3_RADIO_IO",
    "VIOIN_18": "1V8",
    "VIOIN_18DIFF": "1V8",
    "VIN_18CLK": "1V8",
    "VIN_18VCO": "1V8",
    "VIN_18BB": "1V8",
    "VIN_13RF1": "1V0_RF1",
    "VIN_13RF2": "1V0_RF2",
    "VOUT_PA": "1V0_PA",
    "VOUT_14APLL": "VOUT_14APLL",
    "VOUT_14SYNTH": "VOUT_14SYNTH",
    "VBGAP": "VBGAP",
    "CLKP": "XTAL_P",
    "CLKM": "XTAL_N",
    "TX1": "RF_TX1",
    "TX2": "RF_TX2",
    "TX3": "RF_TX3",
    "RX1": "RF_RX1",
    "RX2": "RF_RX2",
    "RX3": "RF_RX3",
    "RX4": "RF_RX4",
    "NRESET": "NRESET",
    "WARM_RESET": "WARM_RESET",
    "NERROR_OUT": "NERROR_OUT",
    "NERROR_IN": "NERROR_IN",
    "RS232_TX": "UART_TX",
    "RS232_RX": "UART_RX",
    "SPIB_CS_N": "CAN_TX",
    "SPIB_CLK": "CAN_RX",
    "SPIB_MISO": "I2C_SCL",
    "SPIB_MOSI": "I2C_SDA",
    "SPIA_CLK": "SPIA_CLK",
    "SPIA_CS_N": "SPIA_CS_N",
    "SPIA_MISO": "SPIA_MISO",
    "SPIA_MOSI": "SPIA_MOSI",
    "SPI_HOST_INTR": "SPI_HOST_INTR",
    "QSPI_CLK": "QSPI_CLK",
    "QSPI_CS_N": "QSPI_CS_N",
    "QSPI[0]": "QSPI_D0",
    "QSPI[1]": "QSPI_D1",
    "QSPI[2]": "QSPI_D2",
    "QSPI[3]": "QSPI_D3",
    "LVDS_TXP[0]": "LVDS_TX0_P",
    "LVDS_TXM[0]": "LVDS_TX0_N",
    "LVDS_TXP[1]": "LVDS_TX1_P",
    "LVDS_TXM[1]": "LVDS_TX1_N",
    "LVDS_CLKP": "LVDS_CLK_P",
    "LVDS_CLKM": "LVDS_CLK_N",
    "LVDS_FRCLKP": "LVDS_FRCLK_P",
    "LVDS_FRCLKM": "LVDS_FRCLK_N",
    "TCK": "JTAG_TCK",
    "TMS": "JTAG_TMS",
    "TDI": "JTAG_TDI",
    "TDO": "SOP0_TDO",
    "SYNC_OUT": "SOP1",
    "PMIC_CLKOUT": "SOP2_PMIC_CLKOUT",
    "MCU_CLKOUT": "FRAME_START",
    "SYNC_IN": "SYNC_IN",
    "GPIO_2": "RADIO_GPIO_2",
}
# deliberately unused balls (no other pad on their net)
UNUSED_BALLNAMES = {
    "VPP",
    "OSC_CLKOUT",
    "GPADC1",
    "GPADC2",
    "GPADC3",
    "GPADC4",
    "GPADC5",
    "GPADC6",
    "GPIO_0",
    "GPIO_1",
    "DMM_SYNC",
    "GPIO_47",
    "GPIO_31",
    "GPIO_32",
    "GPIO_33",
    "GPIO_34",
    "GPIO_35",
    "GPIO_36",
    "GPIO_37",
    "GPIO_38",
}

# --- IC pin electrical types, by atopile address of the part --------------------------------
# out: push-pull output; od: open drain; in: input; io: bidirectional; pwr: power input with
# (min, max) volts; gnd: ground; src: rail source; ana: analog/passive (no rule); nc: no connect
P = "pwr"
PIN_TYPES: Dict[str, Dict[str, Tuple]] = {
    "radio.u1": {},  # filled from the ball map below
    "pmic.u2": {
        "VANA": (P, 2.8, 5.5),
        "VIN_B0": (P, 2.8, 5.5),
        "VIN_B1": (P, 2.8, 5.5),
        "VIN_B2": (P, 2.8, 5.5),
        "VIN_B3": (P, 2.8, 5.5),
        "AGND": ("gnd",),
        "PGND_B01": ("gnd",),
        "PGND_B23": ("gnd",),
        "SW_B0": ("ana",),
        "SW_B1": ("ana",),
        "SW_B2": ("ana",),
        "SW_B3": ("ana",),
        "FB_B0": ("in",),
        "FB_B1": ("in",),
        "FB_B2": ("in",),
        "FB_B3": ("in",),
        "EN1": ("in",),
        "EN2": ("in",),
        "EN3": ("in",),
        "CLKIN": ("in",),
        "NRST": ("in",),
        "SCL": ("in",),
        "SDA": ("io",),
        "PGOOD": ("od",),
        "nINT": ("od",),
    },
    "power_in.efuse": {
        "IN": (P, 2.7, 23.0),
        "OUT": ("ana",),
        "GND": ("gnd",),
        "EN_UVLO": ("in",),
        "OVLO": ("in",),
        "PGTH": ("in",),
        "PG": ("od",),
        "ILM": ("ana",),
        "ITIMER": ("ana",),
        "DVDT": ("ana",),
    },
    "flash.u3": {
        "VCC": (P, 2.3, 3.6),
        "GND": ("gnd",),
        "EP": ("gnd",),
        "CS_N": ("in",),
        "SCLK": ("in",),
        "SIO0": ("io",),
        "SIO1": ("io",),
        "SIO2": ("io",),
        "SIO3": ("io",),
    },
    "can.u4": {
        "VCC": (P, 4.5, 5.5),
        "VIO": (P, 1.65, 5.5),
        "GND": ("gnd",),
        "EP": ("gnd",),
        "TXD": ("in",),
        "RXD": ("out",),
        "STB": ("in",),
        "CANH": ("io",),
        "CANL": ("io",),
    },
}
RADIO_TYPES = {
    "VDDIN": (P, 1.14, 1.32),
    "VIN_SRAM": (P, 1.14, 1.32),
    "VNWA": (P, 1.14, 1.32),
    "VIOIN": (P, 3.135, 3.465),
    "VIOIN_18": (P, 1.71, 1.89),
    "VIOIN_18DIFF": (P, 1.71, 1.89),
    "VIN_18CLK": (P, 1.71, 1.89),
    "VIN_18VCO": (P, 1.71, 1.89),
    "VIN_18BB": (P, 1.71, 1.89),
    # 1-V LDO bypass: 0.95-1.05 V on VIN_13RF1/2 and VOUT_PA (SWRS219F 7.4)
    "VIN_13RF1": (P, 0.95, 1.05),
    "VIN_13RF2": (P, 0.95, 1.05),
    "VOUT_PA": (P, 0.95, 1.05),
    "VSS": ("gnd",),
    "VSSA": ("gnd",),
    "VOUT_14APLL": ("ana",),
    "VOUT_14SYNTH": ("ana",),
    "VBGAP": ("ana",),
    "CLKP": ("ana",),
    "CLKM": ("ana",),
    "TX1": ("ana",),
    "TX2": ("ana",),
    "TX3": ("ana",),
    "RX1": ("ana",),
    "RX2": ("ana",),
    "RX3": ("ana",),
    "RX4": ("ana",),
    "NRESET": ("in",),
    "WARM_RESET": ("od",),
    "NERROR_OUT": ("od",),
    "NERROR_IN": ("in",),
    "RS232_TX": ("out",),
    "RS232_RX": ("in",),
    "SPIB_CS_N": ("out",),
    "SPIB_CLK": ("in",),
    "SPIB_MISO": ("io",),
    "SPIB_MOSI": ("io",),
    "SPIA_CLK": ("io",),
    "SPIA_CS_N": ("io",),
    "SPIA_MISO": ("io",),
    "SPIA_MOSI": ("io",),
    "SPI_HOST_INTR": ("out",),
    "QSPI_CLK": ("out",),
    "QSPI_CS_N": ("out",),
    "QSPI[0]": ("io",),
    "QSPI[1]": ("io",),
    "QSPI[2]": ("io",),
    "QSPI[3]": ("io",),
    "LVDS_TXP[0]": ("out",),
    "LVDS_TXM[0]": ("out",),
    "LVDS_TXP[1]": ("out",),
    "LVDS_TXM[1]": ("out",),
    "LVDS_CLKP": ("out",),
    "LVDS_CLKM": ("out",),
    "LVDS_FRCLKP": ("out",),
    "LVDS_FRCLKM": ("out",),
    # JTAG inputs have internal pulls (TCK, TMS pull-down; TDI pull-up: SWRS219F Table 6-1)
    "TCK": ("in_pulled",),
    "TMS": ("in_pulled",),
    "TDI": ("in_pulled",),
    "TDO": ("io",),
    "SYNC_OUT": ("io",),
    "PMIC_CLKOUT": ("io",),
    "MCU_CLKOUT": ("out",),
    "SYNC_IN": ("in",),
    "GPIO_2": ("io",),  # PMIC nINT through 0 Ohm (an input in firmware)
}
# rail sources: (atopile address, pad) -> nominal volts
SOURCES = {
    ("j1", "1"): 5.0,  # J1 5 V input (5 V +/- 5 %, requirements: regulated 5 V)
    ("power_in.efuse", "6"): 5.0,
    ("pmic.l_b0", "2"): 3.3,  # LP87524J OTP Buck0
    ("pmic.l_b1", "2"): 1.2,  # Buck1
    ("pmic.l_b2", "2"): 1.0,  # Buck2
    ("pmic.l_b3", "2"): 1.8,  # Buck3
}
# fitted series elements that carry a rail on (inductors, beads, 0 Ohm and shunt resistors)
SERIES_PART = re.compile(
    r"^(Radar60_FB_|Radar60_L_|Radar60_R_0_0402$|Radar60_R_0_0612$|Radar60_R_2m_0612$)"
)


# --- a small S-expression reader ---------------------------------------------------------------

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def sexp(text: str):
    stack: List[list] = [[]]
    for tok in _TOKEN.findall(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok[1:-1] if tok.startswith('"') else tok)
    return stack[0][0]


def child(node: list, key: str) -> Optional[list]:
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def children(node: list, key: str) -> List[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key]


class Fp:
    def __init__(self, node: list) -> None:
        self.lib = node[1]
        props = {p[1]: p[2] for p in children(node, "property") if len(p) > 2}
        self.ref = props.get("Reference", "?")
        self.addr = props.get("atopile_address", "")
        self.mpn = props.get("Partnumber", "")
        attr = child(node, "attr")
        self.attrs = set(attr[1:]) if attr else set()
        self.pads: List[Tuple[str, str]] = []  # (pad name, net name)
        for pad in children(node, "pad"):
            net = child(pad, "net")
            self.pads.append((pad[1], net[2] if net and len(net) > 2 else ""))
        self.part = self.lib.split(":", 1)[0]

    @property
    def fitted(self) -> bool:
        return "dnp" not in self.attrs and "board_only" not in self.attrs

    def pad_nets(self, name: str) -> Set[str]:
        return {n for p, n in self.pads if p == name}


class Report:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: Dict[str, object] = {}

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def load(board: Path) -> List[Fp]:
    tree = sexp(board.read_text(encoding="utf-8"))
    return [Fp(n) for n in children(tree, "footprint")]


def net_members(fps: List[Fp]) -> Dict[str, List[Tuple[Fp, str]]]:
    nets: Dict[str, List[Tuple[Fp, str]]] = defaultdict(list)
    for fp in fps:
        for pad, net in fp.pads:
            if net:
                nets[net].append((fp, pad))
    return nets


def ballmap() -> Dict[str, str]:
    return json.loads(BALLMAP.read_text())["balls"]


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", name.replace("[", "_").replace("]", ""))


# --- checks ------------------------------------------------------------------------------------


def check_pin_table(fps: List[Fp], nets, rep: Report) -> None:
    balls = ballmap()
    u1 = [f for f in fps if f.addr == "radio.u1"]
    if len(u1) != 1:
        rep.err(f"radio.u1: expected one footprint, found {len(u1)}")
        return
    u1f = u1[0]
    pads = {p for p, _ in u1f.pads if p}
    if pads != set(balls):
        rep.err(
            f"U1 pads differ from the ball map: missing {sorted(set(balls) - pads)}, extra {sorted(pads - set(balls))}"
        )
    # the plan's table against the data-sheet ball map
    plan_bad = [(b, n, balls.get(b)) for b, n in PLAN_BALLS.items() if balls.get(b) != n]
    for b, want, got in plan_bad:
        rep.err(f"plan 3.2 says {b} is {want}; the data sheet ball map says {got}")
    rows = []
    unused_ok = 0
    for ball, name in balls.items():
        got = u1f.pad_nets(ball)
        net = next(iter(got)) if len(got) == 1 else "|".join(sorted(got))
        if name in UNUSED_BALLNAMES:
            others = [(f.ref, p) for f, p in nets.get(net, []) if not (f is u1f and p == ball)]
            if others:
                rep.err(
                    f"U1 {ball} ({name}) is meant to be unused but shares net {net} with {others}"
                )
            else:
                unused_ok += 1
            rows.append((ball, name, net, "unused"))
            continue
        want = NET_BY_BALLNAME.get(name)
        if want is None:
            rep.err(f"U1 {ball} ({name}): no expected net in the check table")
            continue
        if net != want:
            rep.err(f"U1 {ball} ({name}) is on {net}, expected {want}")
        rows.append((ball, name, net, "ok" if net == want else "MISMATCH"))
        # every used signal ball must reach at least one other pad
        if len(nets.get(net, [])) < 2:
            rep.err(f"U1 {ball} ({name}) on {net} has no other pad")
    rep.info["u1_balls"] = len(balls)
    rep.info["u1_unused_balls"] = unused_ok
    rep.info["plan_table_rows_checked"] = len(PLAN_BALLS)
    rep.info["u1_table"] = rows


def rails(fps: List[Fp], nets) -> Dict[str, float]:
    by_addr = {f.addr: f for f in fps}
    volts: Dict[str, float] = {}
    for (addr, pad), v in SOURCES.items():
        f = by_addr.get(addr)
        if f:
            for n in f.pad_nets(pad):
                volts[n] = v
    changed = True
    while changed:
        changed = False
        for f in fps:
            if not f.fitted or not SERIES_PART.match(f.part):
                continue
            n1, n2 = (next(iter(f.pad_nets("1")), ""), next(iter(f.pad_nets("2")), ""))
            for a, b in ((n1, n2), (n2, n1)):
                if a in volts and b and b not in volts and b != "GND":
                    volts[b] = volts[a]
                    changed = True
    return volts


def pin_types() -> Dict[str, Dict[str, Tuple]]:
    types = dict(PIN_TYPES)
    types["radio.u1"] = RADIO_TYPES
    return types


def part_signal_of_pad(fp: Fp, sources: Path) -> Dict[str, str]:
    """pad -> signal name, read from the generated part's .ato (pin X / signal S ~ pin X)."""
    ato = sources / "parts" / fp.part / f"{fp.part}.ato"
    out: Dict[str, str] = {}
    if not ato.exists():
        return out
    for m in re.finditer(
        r"^\s+(?:signal\s+)?(\w+)\s*~\s*pin\s+\"?([\w]+)\"?", ato.read_text(), re.M
    ):
        out[m.group(2)] = m.group(1)
    return out


def check_power_and_conflicts(fps: List[Fp], nets, sources: Path, rep: Report) -> None:
    volts = rails(fps, nets)
    rep.info["rails"] = dict(sorted(volts.items()))
    types = pin_types()
    balls = ballmap()
    by_addr = {f.addr: f for f in fps}
    drivers: Dict[str, List[str]] = defaultdict(list)
    inputs: Dict[str, List[str]] = defaultdict(list)
    checked = 0
    for addr, table in types.items():
        f = by_addr.get(addr)
        if f is None:
            rep.err(f"{addr}: not on the board")
            continue
        sig_of_pad = part_signal_of_pad(f, sources)
        for pad, net in f.pads:
            if not pad:
                continue
            if addr == "radio.u1":
                name = balls.get(pad, "")
                if name in UNUSED_BALLNAMES:
                    continue
            else:
                name = sig_of_pad.get(pad, pad)
            t = table.get(name)
            if t is None:
                rep.err(f"{f.ref} ({addr}) pad {pad} ({name}): no pin type in the check table")
                continue
            checked += 1
            kind = t[0]
            if kind == P:
                v = volts.get(net)
                if v is None:
                    rep.err(f"{f.ref} {name} (pad {pad}) on {net}: no rail reaches this power pin")
                elif not (t[1] <= v <= t[2]):
                    rep.err(f"{f.ref} {name} (pad {pad}) on {net}: {v} V outside {t[1]}-{t[2]} V")
            elif kind == "gnd":
                if net != "GND":
                    rep.err(f"{f.ref} {name} (pad {pad}) is a ground pin on {net}")
            elif kind == "out":
                drivers[net].append(f"{f.ref}.{name}")
            elif kind == "in":
                inputs[net].append(f"{f.ref}.{name}")
            if kind != "nc" and len(nets.get(net, [])) < 2:
                rep.err(f"{f.ref} {name} (pad {pad}) on {net} has no other pad")
    for net, ds in drivers.items():
        if len(ds) > 1:
            rep.err(f"net {net}: {len(ds)} push-pull outputs {ds}")
    # an input needs something on its net besides other inputs: a driver, an open-drain/io pin,
    # a resistor (pull), a capacitor (RC) or a rail
    for net, ins in inputs.items():
        if net in volts or net == "GND" or net in drivers:
            continue
        others = [
            (fp, pad)
            for fp, pad in nets.get(net, [])
            if f"{fp.ref}" not in {i.split(".")[0] for i in ins}
        ]
        fitted = [(fp, pad) for fp, pad in others if fp.fitted]
        if not fitted:
            rep.err(f"net {net}: inputs {ins} with nothing fitted that drives or pulls them")
    rep.info["typed_pins_checked"] = checked


def check_singletons(fps: List[Fp], nets, rep: Report) -> None:
    single = []
    for net, members in nets.items():
        if len(members) == 1:
            fp, pad = members[0]
            single.append(f"{fp.ref}:{pad}:{net}")
    rep.info["single_pad_nets"] = sorted(single)


ANN = re.compile(r"^\s*#\s*@pnr-([\w-]+)\s+(\{.*\})\s*$")


def _targets(kind: str, data: dict) -> List[Tuple[str, Optional[str]]]:
    """(instance address, pad or None) pairs an annotation refers to."""
    out: List[Tuple[str, Optional[str]]] = []

    def term(s: str) -> None:
        inst, _, pad = s.partition(":")
        out.append((inst, pad or None))

    if "target" in data:
        pads = data.get("pads")
        if pads:
            out.extend((data["target"], str(p)) for p in pads)
        else:
            out.append((data["target"], None))
    for k in ("driver", "receiver", "return", "anchor"):
        if k in data:
            term(data[k])
    if "series" in data:
        out.append((data["series"], None))
    for k in ("members", "order"):
        for s in data.get(k, []):
            term(s)
    for t in data.get("terminal_chain", []):
        term(t["p"])
        term(t["n"])
    if kind == "kelvin":
        out += [
            (data["shunt"], data["positive_pad"]),
            (data["shunt"], data["negative_pad"]),
            (data["sense_component"], data["sense_positive_pad"]),
            (data["sense_component"], data["sense_negative_pad"]),
        ]
    if isinstance(data.get("min_distance_mm"), dict):
        out.extend((k, None) for k in data["min_distance_mm"])
    return out


def check_annotations(fps: List[Fp], sources: Path, rep: Report) -> None:
    by_addr = {f.addr: f for f in fps}
    rows = []
    for ato in sorted(sources.glob("*.ato")):
        for i, line in enumerate(ato.read_text().splitlines(), 1):
            m = ANN.match(line)
            if not m:
                continue
            kind = m.group(1)
            where = f"{ato.name}:{i} @pnr-{kind}"
            try:
                data = json.loads(m.group(2))
            except json.JSONDecodeError as e:
                rep.err(f"{where}: bad JSON ({e})")
                continue
            ok = True
            pad_nets: Set[str] = set()
            for inst, pad in _targets(kind, data):
                f = by_addr.get(inst)
                if f is None:
                    rep.err(f"{where}: target {inst} is not an atopile address on the board")
                    ok = False
                    continue
                if pad is not None:
                    got = f.pad_nets(pad)
                    if not got:
                        rep.err(f"{where}: {inst} ({f.ref}) has no pad {pad}")
                        ok = False
                    pad_nets |= got
            if kind == "current" and len(pad_nets) != 1:
                rep.err(f"{where}: pads span nets {sorted(pad_nets)} (want one)")
                ok = False
            rows.append({"where": where, "ok": ok, "nets": sorted(pad_nets)})
    rep.info["annotations"] = rows


_CAP = re.compile(r"^Radar60_C_(\d+)([pnu])(\d*)_")
_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6}


def cap_value(part: str) -> Optional[float]:
    """Capacitance of a generated capacitor part, from its name (Radar60_C_2u2_0402 -> 2.2 uF)."""
    m = _CAP.match(part)
    if not m:
        return None
    whole, unit, frac = m.groups()
    return float(f"{whole}.{frac or 0}") * _SCALE[unit]


# LP87524J Buck2 (SNVSAW2B 6.5): total output capacitance of a 1-phase output, maximum
BUCK2_COUT_MAX_F = 100e-6
# 1.0 V window at the balls (SWRS219F 7.4, LDO bypass) and the DC budget terms (review_calc 5)
RF_WINDOW_V = (0.95, 1.05)
BUCK_DC, BUCK_STEP, IR_DROP_V = 0.02, 0.03, 0.028
BUCK2_VSET_FIRMWARE_V = 1.025  # BUCK2_VSET = 0x52, written by firmware before the RF starts


def check_fit_options(fps: List[Fp], nets, rep: Report) -> None:
    """Footprint options that must not be fitted together, and the quantified power warnings."""
    by_addr = {f.addr: f for f in fps}
    gnd, sync = by_addr.get("pmic.r_clkin_gnd"), by_addr.get("pmic.r_clkin_sync")
    if gnd is not None and sync is not None and gnd.fitted and sync.fitted:
        rep.err(
            "pmic.r_clkin_gnd and pmic.r_clkin_sync are both fitted: SOP2 (PMIC_CLKOUT) shorted to GND"
        )
    volts = rails(fps, nets)
    one_volt = {n for n, v in volts.items() if abs(v - 1.0) < 1e-9}
    total = 0.0
    for f in fps:
        c = cap_value(f.part)
        if c is None or not f.fitted:
            continue
        if any(net in one_volt for _, net in f.pads):
            total += c
    rep.info["buck2_output_capacitance_uf"] = round(total * 1e6, 2)
    if total > BUCK2_COUT_MAX_F:
        rep.warn(
            f"1.0 V rail (Buck2) carries {total * 1e6:.0f} uF of fitted capacitance (nominal) against"
            f" {BUCK2_COUT_MAX_F * 1e6:.0f} uF total for a 1-phase output (SNVSAW2B 6.5); kept on the"
            " IWR6843ISK Rev D precedent; B4: start-up time, BUCK2_ILIM_INT during soft start, a 2.5 A"
            " load step"
        )
    lo = 1.0 * (1 - BUCK_DC - BUCK_STEP) - IR_DROP_V
    v = BUCK2_VSET_FIRMWARE_V
    lo_fw, hi_fw = v * (1 - BUCK_DC - BUCK_STEP) - IR_DROP_V, v * (1 + BUCK_DC)
    rep.info["rf_rail_window_v"] = {
        "otp_1v0_worst_min": round(lo, 4),
        "firmware_vset": v,
        "firmware_worst_min": round(lo_fw, 4),
        "firmware_no_load_max": round(hi_fw, 4),
    }
    rep.warn(
        f"1.0 V at the balls: OTP 1.000 V gives {lo:.3f} V worst case (-2 % DC, -3 % step,"
        f" {IR_DROP_V * 1e3:.0f} mV IR) against {RF_WINDOW_V[0]} V; firmware must write BUCK2_VSET ="
        f" {v:.3f} V (0x52) before the RF starts: {lo_fw:.3f} V worst case, {hi_fw:.4f} V at no load"
        f" (max {RF_WINDOW_V[1]} V); the step term was characterized with 44 uF; B4 measures VOUT_PA"
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("board")
    ap.add_argument("--sources", default=str(HERE.parent / "elec" / "src"))
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    fps = load(Path(args.board))
    nets = net_members(fps)
    rep = Report()
    sources = Path(args.sources)
    check_pin_table(fps, nets, rep)
    check_power_and_conflicts(fps, nets, sources, rep)
    check_singletons(fps, nets, rep)
    check_annotations(fps, sources, rep)
    check_fit_options(fps, nets, rep)
    rep.info["footprints"] = len(fps)
    rep.info["fitted"] = sum(1 for f in fps if f.fitted)
    rep.info["dnp"] = sorted(f.ref + " " + f.addr for f in fps if "dnp" in f.attrs)
    rep.info["nets"] = len(nets)
    anns = rep.info["annotations"]
    print(
        f"footprints {rep.info['footprints']} (fitted {rep.info['fitted']}, "
        f"DNP {len(rep.info['dnp'])}), nets {len(nets)}"
    )
    print(
        f"U1: {rep.info.get('u1_balls')} balls checked, {rep.info.get('u1_unused_balls')} deliberately unused; "
        f"plan table rows {rep.info.get('plan_table_rows_checked')}"
    )
    print(
        f"typed IC pins checked: {rep.info.get('typed_pins_checked')}; rails: "
        + ", ".join(f"{k}={v}" for k, v in rep.info["rails"].items())
    )
    print(f"annotations: {sum(1 for a in anns if a['ok'])}/{len(anns)} resolve")
    print(f"single-pad nets: {len(rep.info['single_pad_nets'])}")
    for w in rep.warnings:
        print(f"WARNING {w}")
    for e in rep.errors:
        print(f"ERROR {e}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"errors": rep.errors, "warnings": rep.warnings, **rep.info}, indent=1)
            + "\n"
        )
    print("OK" if not rep.errors else f"{len(rep.errors)} error(s)")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
