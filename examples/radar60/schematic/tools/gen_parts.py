#!/usr/bin/env python3
"""Generate the radar60 atopile parts into ``elec/src/parts/`` (not committed).

Each part is one atopile part directory: ``<name>.ato`` (``is_atomic_part`` with the pin map),
a footprint and a symbol. Footprints come from two places only:

- KiCad's stock library of the local headless KiCad (``$YAPNR_KICAD_FOOTPRINTS``, else the
  library next to ``kicad-cli``): copied, with the format version rewritten for atopile 0.15.8
  and, for parts not fitted in a build, the ``dnp`` attribute added;
- ``footprints.py``: land patterns generated from the manufacturers' package drawings.

No EasyEDA data, vendor CAD file or TI design file is used. Pin maps cite the data sheet they
come from. LCSC numbers are given only where the public LCSC product page was read and its
manufacturer part number matched (2026-10-03); the others are empty on purpose.

Usage::

    tools/gen_parts.py [--out elec/src/parts] [--footprints DIR] [--catalog picker-catalog.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import footprints as gen  # noqa: E402

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
BALLMAP = HERE / "data" / "abl0161_ballmap.json"
KICAD_APP = Path.home() / "Applications" / "KiCad-headless.app"
LCSC_TBD = "LCSC-TBD"


def lcsc_tbd(part: "Part") -> str:
    """A per-part placeholder: atopile's JSON BOM merges lines by supplier number."""
    return f"{LCSC_TBD}-" + re.sub(r"[^A-Za-z0-9.-]", "-", part.mpn)


@dataclass
class Part:
    name: str  # atopile component name and part directory
    mfr: str
    mpn: str
    prefix: str
    descr: str
    pins: Dict[str, str]  # pad -> signal
    fp: str  # "stock:<Lib>:<Footprint>" or "gen:<function>"
    lcsc: str = ""
    dnp: bool = False
    board_only: bool = False
    source: str = ""
    params: Dict[str, float] = field(default_factory=dict)
    kind: str = "other"
    package: str = ""


def two(sig1: str = "p1", sig2: str = "p2") -> Dict[str, str]:
    return {"1": sig1, "2": sig2}


def cap(
    name: str,
    mpn: str,
    mfr: str,
    lcsc: str,
    pkg: str,
    value_f: float,
    v: float,
    descr: str,
    dnp: bool = False,
) -> Part:
    return Part(
        name,
        mfr,
        mpn,
        "C",
        descr,
        two(),
        f"stock:Capacitor_SMD:C_{pkg}_{_metric(pkg)}Metric",
        lcsc,
        dnp=dnp,
        kind="capacitor",
        package=pkg,
        params={"capacitance_f": value_f, "voltage_max_v": v},
    )


def res(
    name: str,
    mpn: str,
    mfr: str,
    lcsc: str,
    value: float,
    descr: str,
    pkg: str = "0402",
    dnp: bool = False,
) -> Part:
    return Part(
        name,
        mfr,
        mpn,
        "R",
        descr,
        two(),
        f"stock:Resistor_SMD:R_{pkg}_{_metric(pkg)}Metric",
        lcsc,
        dnp=dnp,
        kind="resistor",
        package=pkg,
        params={"resistance_ohm": value},
    )


def _metric(pkg: str) -> str:
    return {"0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "0612": "1632"}[pkg]


def _uniroyal(value: float, pkg: str = "0402") -> str:
    """UNI-ROYAL 1 % thick-film part number (0402WGF....TCE)."""
    if value == 0:
        return f"{pkg}WGF0000TCE"
    if value < 100:  # three digits and "J" (x 0.1): 22 Ohm is 220J
        return f"{pkg}WGF{int(round(value * 10)):03d}JTCE"
    exp = 0
    v = value
    while v >= 1000:
        v /= 10
        exp += 1
    digits = f"{int(round(v)):03d}"
    return f"{pkg}WGF{digits}{exp}TCE"


def iwr6843_pins() -> Dict[str, str]:
    doc = json.loads(BALLMAP.read_text())
    out = {}
    for ball, name in doc["balls"].items():
        out[ball] = re.sub(r"[^A-Za-z0-9_]", "", name.replace("[", "_").replace("]", ""))
    return out


# --- the parts -------------------------------------------------------------------------------

TI_DS = "TI SWRS219F https://www.ti.com/lit/ds/symlink/iwr6843.pdf"
QTH_SOURCE = (
    "TI SPRUIJ4A Table 5 (pin numbering); land pattern: Samtec recommended PCB layout "
    "QTH-XXX-XX-X-D-XXX rev. M (https://suddendocs.samtec.com/prints/"
    "qth-xxx-xx-x-d-xxx-footprint.pdf; read 2026-10-03), tools/footprints.py:qth030_01_a"
)


def parts() -> List[Part]:
    P: List[Part] = []
    # ICs
    P.append(
        Part(
            "Radar60_IWR6843AQGABLR",
            "Texas Instruments",
            "IWR6843AQGABLR",
            "U",
            "IWR6843 60-64 GHz radar SoC, 3TX/4RX, FCBGA-161 ABL0161B (silicon PG2.0, Q, G, reel)",
            iwr6843_pins(),
            "gen:abl0161b",
            "C2866258",
            kind="ic",
            package="FCBGA-161",
            source=f"{TI_DS}, Figures 6-2..6-5, section 6.2, Table 6-1; tools/data/abl0161_ballmap.json",
        )
    )
    P.append(
        Part(
            "Radar60_LP87524JRNFRQ1",
            "Texas Instruments",
            "LP87524JRNFRQ1",
            "U",
            "LP87524J-Q1 four 4-MHz bucks (OTP: 3.3 V/1.2 V/1.0 V/1.8 V), VQFN-HR-26 RNF0026C",
            {
                "1": "FB_B2",
                "2": "EN3",
                "3": "CLKIN",
                "4": "AGND",
                "5": "SCL",
                "6": "SDA",
                "7": "EN1",
                "8": "FB_B0",
                "9": "VIN_B0",
                "10": "SW_B0",
                "11": "PGND_B01",
                "12": "SW_B1",
                "13": "VIN_B1",
                "14": "FB_B1",
                "15": "EN2",
                "16": "PGOOD",
                "17": "AGND",
                "18": "VANA",
                "19": "nINT",
                "20": "NRST",
                "21": "FB_B3",
                "22": "VIN_B3",
                "23": "SW_B3",
                "24": "PGND_B23",
                "25": "SW_B2",
                "26": "VIN_B2",
                "27": "AGND",
            },
            "gen:rnf0026c",
            "C701982",
            kind="ic",
            package="VQFN-HR-26",
            source="TI SNVSAW2B section 5 Pin Functions https://www.ti.com/lit/ds/symlink/lp87524j-q1.pdf",
        )
    )
    P.append(
        Part(
            "Radar60_TPS259474ARPWR",
            "Texas Instruments",
            "TPS259474ARPWR",
            "U",
            "TPS259474A eFuse: adjustable OVLO, circuit breaker, auto-retry, PG; VQFN-HR-10 RPW0010A",
            {
                "1": "EN_UVLO",
                "2": "OVLO",
                "3": "PG",
                "4": "PGTH",
                "5": "IN",
                "6": "OUT",
                "7": "DVDT",
                "8": "GND",
                "9": "ILM",
                "10": "ITIMER",
            },
            "gen:rpw0010a",
            "C3662807",
            kind="ic",
            package="VQFN-HR-10",
            source="TI SLVSFC9C Table 5-1 https://www.ti.com/lit/ds/symlink/tps25947.pdf",
        )
    )
    P.append(
        Part(
            "Radar60_MX25V1635FZNQ",
            "Macronix",
            "MX25V1635FZNQ",
            "U",
            "16 Mbit 2.3-3.6 V quad SPI NOR flash, 8-WSON 6x5 mm (TI-tested for xWR boot, SPRACH9)",
            {
                "1": "CS_N",
                "2": "SIO1",
                "3": "SIO2",
                "4": "GND",
                "5": "SIO0",
                "6": "SCLK",
                "7": "SIO3",
                "8": "VCC",
                "9": "EP",
            },
            # Macronix 8-WSON (6x5 mm) drawing 6110-3401 rev. 8 (JEDEC MO-220): exposed pad
            # D1 x E1 = 3.40 x 4.00 mm nominal (3.30-3.50 x 3.90-4.10), leads b 0.40, L 0.60 at
            # 1.27 mm. KiCad's EP3.4x4mm variant matches; the EP3.4x4.3mm (Winbond) one is 0.2 mm
            # longer than E1 max. Macronix: EP floating or GND, no vias or traces under it.
            "stock:Package_SON:WSON-8-1EP_6x5mm_P1.27mm_EP3.4x4mm",
            "C2908148",
            kind="ic",
            package="WSON-8",
            source=(
                "JEDEC 8-pin serial-flash pinout as on TI ISK Rev D (SWRR164) U3; package: Macronix "
                "MX25V1635F data sheet PM2257 rev. 1.4 section 18-3, drawing 6110-3401 rev. 8 "
                "(https://www.macronix.com/en-us/products/NOR-Flash/Serial-NOR-Flash/Pages/"
                "spec.aspx?p=MX25V1635F; read 2026-10-03)"
            ),
        )
    )
    P.append(
        Part(
            "Radar60_TCAN1044AVDRBRQ1",
            "Texas Instruments",
            "TCAN1044AVDRBRQ1",
            "U",
            "TCAN1044AV-Q1 CAN FD transceiver with VIO, VSON-8 DRB",
            {
                "1": "TXD",
                "2": "GND",
                "3": "VCC",
                "4": "RXD",
                "5": "VIO",
                "6": "CANL",
                "7": "CANH",
                "8": "STB",
                "9": "EP",
            },
            "stock:Package_DFN_QFN:Texas_DRB0008A",
            "C3234119",
            kind="ic",
            package="VSON-8",
            source="TI SLLSFJ3D Table 5-1 https://www.ti.com/lit/ds/symlink/tcan1044a-q1.pdf",
        )
    )
    P.append(
        Part(
            "Radar60_TPS22917DBVR_DNP",
            "Texas Instruments",
            "TPS22917DBVR",
            "U",
            "TPS22917 load switch (DNP option: delays the radio's 3.3 V as on TI ISK Rev D, O13)",
            {"1": "VIN", "2": "GND", "3": "ON", "4": "CT", "5": "QOD", "6": "VOUT"},
            "stock:Package_TO_SOT_SMD:SOT-23-6",
            "",
            dnp=True,
            kind="ic",
            package="SOT-23-6",
            source="TI ISK Rev D schematic SWRR164 (U9 pin names)",
        )
    )
    # protection, LED, FET
    P.append(
        Part(
            "Radar60_ESD2CAN24DBZRQ1",
            "Texas Instruments",
            "ESD2CAN24DBZRQ1",
            "D",
            "2-channel 24 V CAN bus ESD diode, SOT-23",
            {"1": "IO1", "2": "IO2", "3": "GND"},
            "stock:Package_TO_SOT_SMD:SOT-23",
            "C5736151",
            kind="diode",
            package="SOT-23",
            source="TI SLVSFW5D Table 4-1",
        )
    )
    P.append(
        Part(
            "Radar60_TPD2E2U06DCKR",
            "Texas Instruments",
            "TPD2E2U06DCKR",
            "D",
            "2-channel ESD diode for the UART, SC-70-3",
            {"1": "IO1", "2": "IO2", "3": "GND"},
            "stock:Package_TO_SOT_SMD:SOT-323_SC-70",
            "",
            kind="diode",
            package="SC-70",
            source="TI TPD2E2U06 data sheet, Pin Functions (DCK)",
        )
    )
    P.append(
        Part(
            "Radar60_SMF5V0A",
            "Littelfuse",
            "SMF5.0A",
            "D",
            "5.0 V standoff unidirectional TVS, SOD-123F (SMF)",
            two("K", "A"),
            "stock:Diode_SMD:D_SMF",
            "",
            kind="diode",
            package="SOD-123F",
        )
    )
    P.append(
        Part(
            "Radar60_LED_Green_0603",
            "Everlight",
            "19-217/GHC-YR1S2/3T",
            "D",
            "green LED 0603",
            two("K", "A"),
            "stock:LED_SMD:LED_0603_1608Metric",
            "C72043",
            kind="led",
            package="0603",
        )
    )
    P.append(
        Part(
            "Radar60_2N7002",
            "JSCJ",
            "2N7002",
            "Q",
            "N-channel 60 V MOSFET, SOT-23",
            {"1": "G", "2": "S", "3": "D"},
            "stock:Package_TO_SOT_SMD:SOT-23",
            "C8545",
            kind="transistor",
            package="SOT-23",
        )
    )
    # clock, magnetics
    P.append(
        Part(
            "Radar60_CX3225SA40000D0PTWCC",
            "Kyocera AVX",
            "CX3225SA40000D0PTWCC",
            "Y",
            "40 MHz crystal, CL 8 pF, 3.2x2.5 mm (the TI ISK Rev D part)",
            {"1": "X1", "2": "GND", "3": "X2", "4": "GND"},
            "stock:Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
            "C2908116",
            kind="crystal",
            package="3225",
        )
    )
    P.append(
        Part(
            "Radar60_L_0u47_MCKK2012",
            "Taiyo Yuden",
            "MCKK2012TR47M",
            "L",
            "0.47 uH 4.5 A (Isat) 32 mOhm buck inductor 0805 (Buck0/Buck1: forward current limit <= 3.0 A)",
            two(),
            "stock:Inductor_SMD:L_0805_2012Metric",
            "C655211",
            kind="inductor",
            package="0805",
            params={"inductance_h": 0.47e-6},
        )
    )
    # Buck2 (1.0 V, 2.5 A peak) and Buck3 (1.8 V): the forward current limit reaches 6.0 A and
    # 5.4 A (SNVSAW2B 6.5, LP87524B/J) and the inductor must saturate above it with DCR <= 25 mOhm
    # (6.5 DCR_L, 8.2.1.1); MCKK2012TR47M (4.5 A, 32 mOhm) does not. Vishay IHLP-1616BZ-11
    # 0.47 uH: Isat 7.0 A, heat 7.0 A, DCR 14.5 / 16 mOhm (Vishay document 34196, rev. 22-May-2024),
    # the KiCad stock land pattern of that data sheet. TI's Table 53 lists the same family's
    # IHLP1616AB-1A (6 A typ.).
    P.append(
        Part(
            "Radar60_L_0u47_IHLP1616BZ",
            "Vishay Dale",
            "IHLP1616BZERR47M11",
            "L",
            "0.47 uH 7.0 A (Isat, typ.) 16 mOhm (max) shielded buck inductor, IHLP-1616BZ-11, 4.06x4.45x2.0 mm",
            two(),
            "stock:Inductor_SMD:L_Vishay_IHLP-1616",
            "",
            kind="inductor",
            package="IHLP-1616BZ",
            params={"inductance_h": 0.47e-6},
            source="Vishay IHLP-1616BZ-11 data sheet, document 34196 (https://www.vishay.com/docs/34196/lp16bz11.pdf)",
        )
    )
    P.append(
        Part(
            "Radar60_FB_MPZ2012S101A",
            "TDK",
            "MPZ2012S101AT000",
            "FB",
            "ferrite bead 100 Ohm at 100 MHz, 4 A, 20 mOhm, 0805 (TI ISK Rev D LC filters)",
            two(),
            "stock:Inductor_SMD:L_0805_2012Metric",
            "C15957",
            kind="inductor",
            package="0805",
        )
    )
    # connectors
    P.append(
        Part(
            "Radar60_JST_SM08B_GHS_TB",
            "JST",
            "SM08B-GHS-TB(LF)(SN)",
            "J",
            "JST GH 8-pin 1.25 mm right-angle SMD header (power, CAN-FD, UART)",
            {**{str(i): f"P{i}" for i in range(1, 9)}, "MP": "MOUNT"},
            "stock:Connector_JST:JST_GH_SM08B-GHS-TB_1x08-1MP_P1.25mm_Horizontal",
            "",
            kind="connector",
            package="GH-8",
        )
    )
    P.append(
        Part(
            "Radar60_QTH_030_01_L_D_A",
            "Samtec",
            "QTH-030-01-L-D-A",
            "J",
            "Samtec QTH 60-pin 0.5 mm header for the DCA1000EVM (development fit)",
            {**{str(i): f"P{i}" for i in range(1, 61)}, **{f"MP{i}": "MOUNT" for i in range(1, 5)}},
            "gen:qth030_01_a",
            "C2843756",
            kind="connector",
            package="QTH-030",
            source=QTH_SOURCE,
        )
    )
    P.append(
        Part(
            "Radar60_QTH_030_01_L_D_A_DNP",
            "Samtec",
            "QTH-030-01-L-D-A",
            "J",
            "Samtec QTH 60-pin 0.5 mm header for the DCA1000EVM, not fitted (product builds)",
            {**{str(i): f"P{i}" for i in range(1, 61)}, **{f"MP{i}": "MOUNT" for i in range(1, 5)}},
            "gen:qth030_01_a",
            "C2843756",
            dnp=True,
            kind="connector",
            package="QTH-030",
            source=QTH_SOURCE,
        )
    )
    P.append(
        Part(
            "Radar60_ARM10_1p27_DNP",
            "Samtec",
            "FTSH-105-01-L-DV-K",
            "J",
            "Arm Cortex 10-pin 1.27 mm debug header (JTAG, DNP)",
            {str(i): f"P{i}" for i in range(1, 11)},
            "stock:Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical_SMD",
            "",
            dnp=True,
            kind="connector",
            package="2x05 1.27",
        )
    )
    # board-only items
    P.append(
        Part(
            "Radar60_TestPad_D1",
            "",
            "",
            "TP",
            "1.0 mm test pad",
            {"1": "TP"},
            "stock:TestPoint:TestPoint_Pad_D1.0mm",
            "",
            board_only=True,
            kind="other",
        )
    )
    P.append(
        Part(
            "Radar60_KelvinSense_2Pad",
            "",
            "",
            "TP",
            "Kelvin sense pad pair for the 1.0 V shunt (probe)",
            two("SP", "SN"),
            "gen:kelvin_pads",
            "",
            board_only=True,
        )
    )
    P.append(
        Part(
            "Radar60_MountingHole_M2p5",
            "",
            "",
            "MH",
            "M2.5 mounting hole, plated, 5.4 mm pad (GND, board-design.md 5.1)",
            {"1": "MH"},
            "stock:MountingHole:MountingHole_2.7mm_M2.5_Pad",
            "",
            board_only=True,
        )
    )
    P.append(
        Part(
            "Radar60_Fiducial_1mm",
            "",
            "",
            "FID",
            "fiducial 1 mm, 2 mm mask",
            {},
            "stock:Fiducial:Fiducial_1mm_Mask2mm",
            "",
            board_only=True,
        )
    )
    P.append(
        Part(
            "Radar60_RFM1_Placeholder",
            "",
            "",
            "RFM",
            "placeholder for the RF macro RFM1",
            {
                "rx1": "RX1",
                "rx2": "RX2",
                "rx3": "RX3",
                "rx4": "RX4",
                "tx1": "TX1",
                "tx2": "TX2",
                "tx3": "TX3",
                "gnd": "GND",
            },
            "gen:rfm1_placeholder",
            "",
            board_only=True,
        )
    )
    # capacitors
    S, FH, MU = "Samsung Electro-Mechanics", "FH (Guangdong Fenghua)", "Murata"
    P += [
        cap(
            "Radar60_C_100n_0402",
            "CL05B104KO5NNNC",
            S,
            "C1525",
            "0402",
            100e-9,
            16,
            "100 nF 16 V X7R 0402",
        ),
        cap(
            "Radar60_C_220n_0402",
            "CL05B224KO5NNNC",
            S,
            "C16772",
            "0402",
            220e-9,
            16,
            "220 nF 16 V X7R 0402",
        ),
        cap(
            "Radar60_C_1u_0402",
            "CL05A105KA5NQNC",
            S,
            "C52923",
            "0402",
            1e-6,
            25,
            "1 uF 25 V X5R 0402",
        ),
        cap(
            "Radar60_C_2u2_0402",
            "CL05A225MQ5NSNC",
            S,
            "C12530",
            "0402",
            2.2e-6,
            6.3,
            "2.2 uF 6.3 V X5R 0402",
        ),
        cap(
            "Radar60_C_10u_0603",
            "CL10A106KP8NNNC",
            S,
            "C19702",
            "0603",
            10e-6,
            10,
            "10 uF 10 V X5R 0603",
        ),
        cap(
            "Radar60_C_10u_0805",
            "CL21A106KAYNNNE",
            S,
            "C15850",
            "0805",
            10e-6,
            25,
            "10 uF 25 V X5R 0805",
        ),
        cap(
            "Radar60_C_22u_0805",
            "CL21A226MAQNNNE",
            S,
            "C45783",
            "0805",
            22e-6,
            25,
            "22 uF 25 V X5R 0805",
        ),
        # VBGAP in 0402: the part TI's errata names (SWRZ087D ANA#19: GRM155R71E473KA88), so the
        # cap fits beside B10 at the package edge (review 2026-10-03); LCSC number not verified
        cap(
            "Radar60_C_47n_0402",
            "GRM155R71E473KA88D",
            MU,
            "",
            "0402",
            47e-9,
            25,
            "47 nF 25 V X7R 0402 (VBGAP, errata ANA#19 example part)",
        ),
        cap(
            "Radar60_C_10n_0402",
            "CL05B103KB5NNNC",
            S,
            "C15195",
            "0402",
            10e-9,
            50,
            "10 nF 50 V X7R 0402",
        ),
        cap(
            "Radar60_C_1n_0402",
            "0402B102K500NT",
            FH,
            "C1523",
            "0402",
            1e-9,
            50,
            "1 nF 50 V X7R 0402",
        ),
        cap(
            "Radar60_C_470p_0402",
            "0402B471K500NT",
            FH,
            "C1537",
            "0402",
            470e-12,
            50,
            "470 pF 50 V X7R 0402",
        ),
        cap(
            "Radar60_C_4p7_0402",
            "0402CG4R7C500NT",
            FH,
            "C1569",
            "0402",
            4.7e-12,
            50,
            "4.7 pF 50 V C0G 0402",
        ),
        cap(
            "Radar60_C_390p_0402",
            "GCM1555C1H391JA16D",
            MU,
            "",
            "0402",
            390e-12,
            50,
            "390 pF 50 V C0G 0402 (snubber, SNVSAW2B Table 56)",
        ),
        # ANA#17A damping option (SWRZ087D: damp the supply ringing): 22 uF behind 0.22 Ohm, DNP
        cap(
            "Radar60_C_22u_0805_DNP",
            "CL21A226MAQNNNE",
            S,
            "C45783",
            "0805",
            22e-6,
            25,
            "22 uF 25 V X5R 0805 (DNP, supply damping option)",
            dnp=True,
        ),
        cap(
            "Radar60_C_4n7_0402_DNP",
            "0402B472K500NT",
            FH,
            "C1538",
            "0402",
            4.7e-9,
            50,
            "4.7 nF 50 V X7R 0402 (DNP)",
            dnp=True,
        ),
    ]
    # resistors
    U = "UNI-ROYAL"
    P += [
        res("Radar60_R_0_0402", _uniroyal(0), U, "C17168", 0, "0 Ohm jumper 0402"),
        res(
            "Radar60_R_0_0402_DNP",
            _uniroyal(0),
            U,
            "C17168",
            0,
            "0 Ohm jumper 0402 (DNP)",
            dnp=True,
        ),
        res("Radar60_R_22_0402", _uniroyal(22), U, "C25092", 22, "22 Ohm 1 % 0402"),
        res("Radar60_R_1k_0402", _uniroyal(1000), U, "C11702", 1e3, "1 kOhm 1 % 0402"),
        res("Radar60_R_4k99_0402", _uniroyal(4990), U, "C25903", 4.99e3, "4.99 kOhm 1 % 0402"),
        res("Radar60_R_11k_0402", _uniroyal(11e3), U, "C25749", 11e3, "11 kOhm 1 % 0402"),
        res("Radar60_R_24k_0402", _uniroyal(24e3), U, "C25769", 24e3, "24 kOhm 1 % 0402"),
        res("Radar60_R_39k_0402", _uniroyal(39e3), U, "C25783", 39e3, "39 kOhm 1 % 0402"),
        res("Radar60_R_10k_0402", _uniroyal(10e3), U, "C25744", 10e3, "10 kOhm 1 % 0402"),
        res(
            "Radar60_R_10k_0402_DNP",
            _uniroyal(10e3),
            U,
            "C25744",
            10e3,
            "10 kOhm 1 % 0402 (DNP)",
            dnp=True,
        ),
        res("Radar60_R_100k_0402", _uniroyal(100e3), U, "C25741", 100e3, "100 kOhm 1 % 0402"),
        res(
            "Radar60_R_1k65_0402", _uniroyal(1650), U, "", 1.65e3, "1.65 kOhm 1 % 0402 (eFuse ILM)"
        ),
        res(
            "Radar60_R_3R9_0402",
            "CRCW04023R90JNED",
            "Vishay Dale",
            "",
            3.9,
            "3.9 Ohm 5 % 0402 (snubber, SNVSAW2B Table 56)",
        ),
        res(
            "Radar60_R_62_0402_DNP",
            _uniroyal(62),
            U,
            "",
            62,
            "62 Ohm 1 % 0402 (DNP, CAN split termination)",
            dnp=True,
        ),
        res(
            "Radar60_R_0R22_0402_DNP",
            "TBD-0R22-1%-0402",
            "TBD",
            "",
            0.22,
            "0.22 Ohm 1 % 0402 (DNP, series damping resistor of the ANA#17A option) [E]",
            dnp=True,
        ),
        res(
            "Radar60_R_2m_0612",
            "TBD-2mOhm-1%-0612",
            "TBD",
            "",
            0.002,
            "2 mOhm 1 % 0612 wide-terminal current shunt (development fit; product fit is a 0 Ohm 0612)",
            pkg="0612",
        ),
        res(
            "Radar60_R_0_0612",
            "TBD-0R-jumper-0612",
            "TBD",
            "",
            0,
            "0 Ohm 0612 jumper in the 1.0 V shunt footprint (product fit)",
            pkg="0612",
        ),
    ]
    return P


# --- writers ---------------------------------------------------------------------------------


def stock_dir(explicit: Optional[str]) -> Path:
    for cand in (explicit, os.environ.get("YAPNR_KICAD_FOOTPRINTS")):
        if cand:
            return Path(cand)
    p = KICAD_APP / "Contents" / "SharedSupport" / "footprints"
    if p.is_dir():
        return p
    for p in (Path("/usr/share/kicad/footprints"), Path("/usr/local/share/kicad/footprints")):
        if p.is_dir():
            return p
    raise SystemExit("no KiCad stock footprints: set YAPNR_KICAD_FOOTPRINTS or pass --footprints")


def stock_footprint(lib_dir: Path, ref: str, dnp: bool, board_only: bool) -> Tuple[str, str]:
    lib, name = ref.split(":", 1)
    text = (lib_dir / f"{lib}.pretty" / f"{name}.kicad_mod").read_text(encoding="utf-8")
    text = re.sub(r"\(version \d+\)", f"(version {gen.FORMAT_VERSION})", text, count=1)
    # atopile 0.15.8 reads the KiCad 9 format: drop KiCad 10-only tokens it does not know
    text = re.sub(r'\n\t\(generator_version "[^"]*"\)', "", text)
    text = re.sub(r"\n\t\(embedded_fonts no\)", "", text)
    text = _attrs(text, dnp, board_only)
    text = _silk_circles_to_rects(text)
    return name, text


_SILK_CIRCLE = re.compile(
    r"\t\(fp_circle\n\t\t\(center (?P<cx>[-\d.]+) (?P<cy>[-\d.]+)\)\n\t\t\(end (?P<ex>[-\d.]+) (?P<ey>[-\d.]+)\)\n"
    r"(?P<rest>(?:\t\t.*\n)*?\t\t\(layer \"[FB]\.SilkS\"\)\n(?:\t\t.*\n)*?)\t\)"
)


def _silk_circles_to_rects(text: str) -> str:
    """atopile 0.15.8 cannot size a silk outline made only of circles (its bounding box of the
    silk ignores circles and arcs and then fails on an empty list), so a footprint without a silk
    line or rectangle gets its silk circles drawn as squares of the same extent."""
    silk_line = re.search(r"\(fp_(?:line|rect)\n(?:\t\t.*\n)*?\t\t\(layer \"[FB]\.SilkS\"\)", text)
    if silk_line:
        return text

    def sq(m: "re.Match[str]") -> str:
        cx, cy, ex, ey = (float(m.group(k)) for k in ("cx", "cy", "ex", "ey"))
        r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
        rest = re.sub(r"\t\t\(fill [a-z]+\)\n", "", m.group("rest"))
        return (
            f"\t(fp_rect\n\t\t(start {gen._n(cx - r)} {gen._n(cy - r)})\n\t\t(end {gen._n(cx + r)} {gen._n(cy + r)})\n"
            f"{rest}\t)"
        )

    return _SILK_CIRCLE.sub(sq, text)


def _attrs(text: str, dnp: bool, board_only: bool) -> str:
    m = re.search(r"\(attr([^)]*)\)", text)
    flags = m.group(1).split() if m else []
    if dnp:
        flags += ["dnp", "exclude_from_bom", "exclude_from_pos_files"]
    if board_only:
        flags += ["exclude_from_bom", "exclude_from_pos_files"]
    flags = list(dict.fromkeys(flags))
    new = f"(attr {' '.join(flags)})"
    if m:
        return text[: m.start()] + new + text[m.end() :]
    return text.replace('\t(layer "F.Cu")\n', f'\t(layer "F.Cu")\n\t{new}\n', 1)


def kelvin_pads() -> gen.Footprint:
    fp = gen.Footprint(
        "Radar60_KelvinSense_2Pad_P1.27mm",
        "Kelvin sense pad pair, 0.8 mm pads at 1.27 mm",
        "kelvin sense test",
        attr="smd exclude_from_bom exclude_from_pos_files",
    )
    fp.pad("1", "circle", (-0.635, 0), (0.8, 0.8), layers='"F.Cu" "F.Mask"')
    fp.pad("2", "circle", (0.635, 0), (0.8, 0.8), layers='"F.Cu" "F.Mask"')
    fp.line((-0.635, -0.75), (-0.635, -1.15), "F.SilkS", 0.12)  # "+" mark at the positive pad
    fp.line((-0.835, -0.95), (-0.435, -0.95), "F.SilkS", 0.12)
    gen._courtyard(fp, (-1.2, -0.65, 1.2, 0.65))
    return fp


def generated_footprint(fn: str, part: Part) -> Tuple[str, str]:
    if fn == "abl0161b":
        fp = gen.abl0161b(json.loads(BALLMAP.read_text())["balls"])
    elif fn == "kelvin_pads":
        fp = kelvin_pads()
    else:
        fp = getattr(gen, fn)()
    text = fp.render()
    if part.dnp or part.board_only:
        text = _attrs(text, part.dnp, part.board_only)
    return fp.name, text


def symbol(part: Part) -> str:
    """A plain box symbol: one pin per pad, named after its signal."""
    pads = list(part.pins.items())
    n = max(1, len(pads))
    left = pads[: (n + 1) // 2]
    right = pads[(n + 1) // 2 :]
    h = max(len(left), len(right)) * 2.54 + 2.54
    w = 15.24 if n > 2 else 5.08

    def pin(pad: str, sig: str, x: float, y: float, rot: int) -> str:
        return (
            f"\t\t\t(pin passive line\n\t\t\t\t(at {gen._n(x)} {gen._n(y)} {rot})\n\t\t\t\t(length 2.54)\n"
            f'\t\t\t\t(name "{sig}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n'
            f"\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n"
            f'\t\t\t\t(number "{pad}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n'
            f"\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t)"
        )

    pins = []
    for i, (pad, sig) in enumerate(left):
        pins.append(pin(pad, sig, -w / 2 - 2.54, h / 2 - 2.54 - i * 2.54, 0))
    for i, (pad, sig) in enumerate(right):
        pins.append(pin(pad, sig, w / 2 + 2.54, h / 2 - 2.54 - i * 2.54, 180))
    nm = part.name

    def prop(k: str, v: str, y: float, hide: bool = False) -> str:
        hid = "\n\t\t\t\t(hide yes)" if hide else ""
        return (
            f'\t\t(property "{k}" "{v}"\n\t\t\t(at 0 {gen._n(y)} 0)\n\t\t\t(effects\n\t\t\t\t(font\n'
            f"\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t){hid}\n\t\t\t)\n\t\t)"
        )

    return (
        '(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "radar60-gen-parts")\n'
        f'\t(symbol "{nm}"\n'
        + prop("Reference", part.prefix, h / 2 + 1.27)
        + "\n"
        + prop("Value", part.mpn or nm, -h / 2 - 1.27)
        + "\n"
        + prop("Footprint", "", -h / 2 - 3.81, True)
        + "\n"
        + prop("Datasheet", "", -h / 2 - 6.35, True)
        + "\n"
        + "\t\t(in_bom yes)\n\t\t(on_board yes)\n"
        + f'\t\t(symbol "{nm}_0_1"\n\t\t\t(rectangle\n\t\t\t\t(start {gen._n(-w / 2)} {gen._n(h / 2)})\n'
        f"\t\t\t\t(end {gen._n(w / 2)} {gen._n(-h / 2)})\n\t\t\t\t(stroke\n\t\t\t\t\t(width 0)\n"
        "\t\t\t\t\t(type default)\n\t\t\t\t)\n\t\t\t\t(fill\n\t\t\t\t\t(type background)\n\t\t\t\t)\n\t\t\t)\n"
        + "\n".join(pins)
        + "\n\t\t)\n\t)\n)\n"
    )


def ato(part: Part, fp_file: str, sym_file: str) -> str:
    descr = part.descr.replace('"', "'")
    lines = [
        "# Generated by examples/radar60/schematic/tools/gen_parts.py; do not edit.",
        "import has_designator_prefix",
        "import has_part_removed" if (part.dnp or part.board_only) else "import has_part_picked",
        "import is_atomic_part",
        "",
        f"component {part.name}:",
        f'    """{descr}"""',
        "",
        f'    trait is_atomic_part<manufacturer="{part.mfr or "radar60"}", partnumber="{part.mpn or part.name}", '
        f'footprint="{fp_file}", symbol="{sym_file}">',
    ]
    if part.dnp or part.board_only:
        # not fitted / not a part: no BOM line, but the footprint stays on the board
        lines.append("    trait has_part_removed")
    else:
        # LCSC-TBD-<mpn>: the LCSC number was not verified; the JSON BOM keeps the line by MPN,
        # atopile's JLC CSV drops it with a warning (Rev A is assembled by MPN)
        lines.append(
            '    trait has_part_picked::by_supplier<supplier_id="lcsc", '
            f'supplier_partno="{part.lcsc or lcsc_tbd(part)}", '
            f'manufacturer="{part.mfr}", partno="{part.mpn}">'
        )
    lines.append(f'    trait has_designator_prefix<prefix="{part.prefix}">')
    lines.append("")
    declared = set()
    clash = set(part.pins) & set(part.pins.values())
    assert not clash, f"{part.name}: pin and signal names clash: {sorted(clash)}"
    for pad, sig in part.pins.items():
        pin = pad if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|\d+", pad) else f'"{pad}"'
        lines.append(f"    pin {pin}")
        if sig in declared:
            lines.append(f"    {sig} ~ pin {pin}")
        else:
            lines.append(f"    signal {sig} ~ pin {pin}")
            declared.add(sig)
    return "\n".join(line for line in lines if line is not None) + "\n"


def write_part(out: Path, part: Part, lib_dir: Path) -> None:
    kind, _, ref = part.fp.partition(":")
    if kind == "stock":
        fp_name, fp_text = stock_footprint(lib_dir, ref, part.dnp, part.board_only)
    else:
        fp_name, fp_text = generated_footprint(ref, part)
    d = out / part.name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    fp_file = f"{fp_name}.kicad_mod"
    sym_file = f"{part.name}.kicad_sym"
    (d / fp_file).write_text(fp_text, encoding="utf-8")
    (d / sym_file).write_text(symbol(part), encoding="utf-8")
    (d / f"{part.name}.ato").write_text(ato(part, fp_file, sym_file), encoding="utf-8")
    note = [f"# {part.name}", "", part.descr, ""]
    note.append(f"- manufacturer part number: {part.mpn or '(board-only item)'}")
    note.append(f"- LCSC: {part.lcsc or '(not verified; left empty)'}")
    note.append(
        f"- footprint: {'KiCad stock ' + ref if kind == 'stock' else 'generated, tools/footprints.py:' + ref}"
    )
    if part.source:
        note.append(f"- pin map source: {part.source}")
    if part.dnp:
        note.append("- fitted: no (DNP)")
    (d / "NOTES.md").write_text("\n".join(note) + "\n", encoding="utf-8")


def catalog(ps: Sequence[Part]) -> Dict:
    entries = []
    for p in ps:
        if not p.lcsc or p.dnp:
            continue
        entries.append(
            {
                "lcsc": p.lcsc,
                "mpn": p.mpn,
                "manufacturer": p.mfr,
                "package": p.package,
                "kind": p.kind,
                "description": p.descr,
                "params": p.params,
                "basic": False,
                "stock": "unknown",
            }
        )
    return {
        "schema": "yapnr-picker-catalog-v1",
        "provenance": {
            "source": "LCSC public product pages (title: manufacturer part number), one page per id",
            "retrieved": "2026-10-03",
            "licence_note": "part facts only (identifiers, values, packages)",
        },
        "parts": sorted(entries, key=lambda e: e["lcsc"]),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(PROJECT / "elec" / "src" / "parts"))
    ap.add_argument("--footprints", default=None, help="KiCad stock footprint directory")
    ap.add_argument(
        "--catalog", default=None, help="also write a picker catalog of the verified LCSC parts"
    )
    args = ap.parse_args(argv)
    lib_dir = stock_dir(args.footprints)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ps = parts()
    names = [p.name for p in ps]
    assert len(names) == len(set(names)), "duplicate part names"
    for stale in sorted(out.glob("Radar60_*")):
        if stale.is_dir() and stale.name not in names:
            shutil.rmtree(stale)
    for p in ps:
        write_part(out, p, lib_dir)
    if args.catalog:
        Path(args.catalog).write_text(json.dumps(catalog(ps), indent=2) + "\n", encoding="utf-8")
    print(f"{len(ps)} parts -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
