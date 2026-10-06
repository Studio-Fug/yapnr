"""The top rung: ``12-soc-bga-N``, a purpose-built single-board computer around a BGA MCU.

An STM32F746IGK6 (UFBGA176+25, 0.65 mm) with the buses of a real Cortex-M7 board in
the class of ST's STM32F746G-DISCO: a 16-bit SDRAM on the FMC (IS42S16400J, TSOP-II-54)
whose high data byte lane is a declared ``length_match`` group, a quad-SPI NOR flash
(W25Q128JV, SOIC-8 208 mil), an RMII Ethernet PHY (LAN8742A, QFN-24) with series
terminations, MDI terminations, its own crystal and a magnetics header, USB full speed
(Micro-B with an ESD array) and a microSD socket on SDMMC1. Four supply domains: the
USB 5 V input, a buck to 3.3 V (TLV62569) that owns the supply plane, and two LDOs
(AP2112K-3.3) for the MCU's analog supply (VDDA, VREF+) and the PHY's analog supply,
both routed on signal layers: the whole supply plane goes to the main rail, as a layout
engineer would allocate it. Six layers (S G S G P S), the BGA fanout of
``11-ufbga201-fanout`` (interstitial 0.35/0.15 mm plane drops: an HDI-capable profile's
drill), its fine-pitch fab rules, and per-domain decoupling. The MCU, the SDRAM, the USB
receptacle, the magnetics header and the holes are fixed; three connectors are locked to
their edges; hard proximity groups (``SOC_GROUPS``) keep each part's support parts beside
it (decoupling, crystals, terminations, the regulators' capacitors).

Pin maps: the MCU's balls are KiCad 10's ``MCU_ST_STM32F7:STM32F746IGKx`` symbol (ST
DS10916, table 10); the alternate functions picked are the ones that symbol lists for
each ball (the DISCO board's choices where it has one: FMC SDRAM bank 1, QUADSPI bank 1
on PF6-PF9, RMII with TX on PG11/PG13/PG14). The SDRAM is the JEDEC x16 TSOP-II-54
pinout; the flash, PHY, ESD array, buck and LDO follow their datasheets (named beside
each part). Everything here is this repository's own design (AGPL), not a copy of a
vendor board.
"""

from copy import deepcopy

from designs import circuit
from hard_rungs import (
    base_checks,
    connected_pads,
    hard,
    pinned,
    with_stackup,
)

# KiCad 10 MCU_ST_STM32F7:STM32F746IGKx, ball -> pin name, rows A (north) to R (south),
# columns 1 (west) to 15 (east); "-" is a position without a ball.
STM32F746_UFBGA176 = {
    "A": "PE3 PE2 PE1 PE0 PB8 PB5 PG14 PG13 PB4 PB3 PD7 PC12 PA15 PA14 PA13",
    "B": "PE4 PE5 PE6 PB9 PB7 PB6 PG15 PG12 PG11 PG10 PD6 PD0 PC11 PC10 PA12",
    "C": "VBAT PI7 PI6 PI5 VDD PDR_ON VDD VDD VDD PG9 PD5 PD1 PI3 PI2 PA11",
    "D": "PC13 PI8 PI9 PI4 VSS BOOT0 VSS VSS VSS PD4 PD3 PD2 PH15 PI1 PA10",
    "E": "PC14 PF0 PI10 PI11 - - - - - - - PH13 PH14 PI0 PA9",
    "F": "PC15 VSS VDD PH2 - VSS VSS VSS VSS VSS - VSS VCAP_2 PC9 PA8",
    "G": "PH0 VSS VDD PH3 - VSS VSS VSS VSS VSS - VSS VDD PC8 PC7",
    "H": "PH1 PF2 PF1 PH4 - VSS VSS VSS VSS VSS - VSS VDDUSB PG8 PC6",
    "J": "NRST PF3 PF4 PH5 - VSS VSS VSS VSS VSS - VDD VDD PG7 PG6",
    "K": "PF7 PF6 PF5 VDD - VSS VSS VSS VSS VSS - PH12 PG5 PG4 PG3",
    "L": "PF10 PF9 PF8 BYPASS_REG - - - - - - - PH11 PH10 PD15 PG2",
    "M": "VSSA PC0 PC1 PC2 PC3 PB2 PG1 VSS VSS VCAP_1 PH6 PH8 PH9 PD14 PD13",
    "N": "VREF- PA1 PA0 PA4 PC4 PF13 PG0 VDD VDD VDD PE13 PH7 PD12 PD11 PD10",
    "P": "VREF+ PA2 PA6 PA5 PC5 PF12 PF15 PE8 PE9 PE11 PE14 PB12 PB13 PD9 PD8",
    "R": "VDDA PA3 PA7 PB1 PB0 PF11 PF14 PE7 PE10 PE12 PE15 PB10 PB11 PB14 PB15",
}
ROWS = "ABCDEFGHJKLMNPR"

# Supply and fixed-function balls by pin name.
SUPPLY_OF = {
    "VDD": "3V3",
    "PDR_ON": "3V3",  # internal power-down reset on
    "VDDUSB": "3V3",
    "VBAT": "3V3",  # no backup battery: VBAT from VDD
    "VDDA": "3V3A",
    "VREF+": "3V3A",
    "VSS": "GND",
    "VSSA": "GND",
    "VREF-": "GND",
    "BYPASS_REG": "GND",  # internal regulator on
    "VCAP_1": "VCAP1",
    "VCAP_2": "VCAP2",
    "NRST": "NRST",
    "BOOT0": "BOOT0",
}

# Signal functions, pin name -> net (each pin's alternate function from the KiCad symbol).
FMC = {
    # SDRAM address A0-A11, bank address, data, byte masks and control (bank 1).
    **{
        p: "FMC_A%d" % i
        for i, p in enumerate("PF0 PF1 PF2 PF3 PF4 PF5 PF12 PF13 PF14 PF15 PG0 PG1".split())
    },
    "PG4": "FMC_BA0",
    "PG5": "FMC_BA1",
    **{
        p: "FMC_D%d" % i
        for i, p in enumerate(
            "PD14 PD15 PD0 PD1 PE7 PE8 PE9 PE10 PE11 PE12 PE13 PE14 PE15 PD8 PD9 PD10".split()
        )
    },
    "PE0": "FMC_NBL0",
    "PE1": "FMC_NBL1",
    "PG8": "FMC_SDCLK",
    "PG15": "FMC_SDNCAS",
    "PF11": "FMC_SDNRAS",
    "PH5": "FMC_SDNWE",
    "PC3": "FMC_SDCKE0",
    "PH3": "FMC_SDNE0",
}
QSPI = {
    "PB2": "QSPI_CLK",
    "PB6": "QSPI_NCS",
    "PF8": "QSPI_IO0",
    "PF9": "QSPI_IO1",
    "PF7": "QSPI_IO2",
    "PF6": "QSPI_IO3",
}
RMII = {
    "PA1": "RMII_REF_CLK",
    "PA2": "RMII_MDIO",
    "PA7": "RMII_CRS_DV",
    "PC1": "RMII_MDC",
    "PC4": "RMII_RXD0",
    "PC5": "RMII_RXD1",
    "PG11": "RMII_TX_EN",
    "PG13": "RMII_TXD0",
    "PG14": "RMII_TXD1",
}
OTHER = {
    "PH0": "OSC_IN",
    "PH1": "OSC_OUT",
    "PA11": "USB_DN",  # OTG_FS_DM
    "PA12": "USB_DP",  # OTG_FS_DP
    "PC12": "SD_CK",
    "PD2": "SD_CMD",
    "PC8": "SD_D0",
    "PC9": "SD_D1",
    "PC10": "SD_D2",
    "PC11": "SD_D3",
    "PA13": "SWDIO",
    "PA14": "SWCLK",
    "PB3": "SWO",
    "PA9": "UART_TX",  # USART1
    "PA10": "UART_RX",
    "PB8": "I2C_SCL",  # I2C1
    "PB9": "I2C_SDA",
    "PG10": "GPIO1",
    "PG12": "GPIO2",
    "PI1": "LED_R",
    "PI2": "LED_G",
    "PI3": "LED_B",
    "PI11": "BTN",
}
SIGNALS = {**FMC, **QSPI, **RMII, **OTHER}

# The SDRAM's high data byte lane (DQ8-DQ15: PE11-PE15, PD8-PD10, all on the array's
# south-east rows), matched to 1.0 mm. The low lane is not matched: two of its pins
# (PD0, PD1) leave from the north-east corner, as on the reference board.
LANE = ["FMC_D%d" % i for i in range(8, 16)]
LANE_TOLERANCE_MM = 1.0

# The designer's placement intent, as hard proximity groups (name, anchor, anchor pad or
# None for the anchor's origin, members, radius in mm, origin to origin): each part's
# support parts beside it, as a layout engineer would ask of a placer.
SOC_GROUPS = [
    # Every MCU decoupling capacitor, the bulk capacitor and the BOOT0 resistor around the
    # array (its courtyard alone reaches 6 mm from its centre).
    ("mcu-decoupling", "U1", None, ["C%d" % i for i in range(1, 23)] + ["R1"], 12.0),
    # The HSE crystal and its load capacitors at the oscillator balls (G1 OSC_IN, west edge).
    ("hse", "U1", "G1", ["Y1", "C23", "C24"], 7.0),
    # The MCU-driven RMII series terminations at the MCU.
    ("rmii-mcu-series", "U1", None, ["R11", "R12", "R13"], 12.0),
    # The SDRAM's decoupling along its body (22.2 mm long).
    ("sdram-decoupling", "U2", None, ["C%d" % i for i in range(25, 33)], 14.0),
    ("flash", "U3", None, ["C33", "R3"], 6.0),
    # The PHY's crystal, decoupling, bias, pull-ups, PHY-driven series terminations, MDI
    # terminations and link LEDs around it.
    (
        "phy",
        "U4",
        None,
        ["Y2"]
        + ["C%d" % i for i in range(34, 43)]
        + ["R%d" % i for i in range(4, 11)]
        + ["R%d" % i for i in range(14, 20)]
        + ["D1", "D2"],
        10.0,
    ),
    # The PHY near the magnetics header (its MDI pairs stay short).
    ("phy-magnetics", "J2", None, ["U4"], 16.0),
    ("usb", "J1", None, ["U8", "F1", "C43"], 10.0),
    ("buck", "U5", None, ["L1", "C44", "C45", "C46", "R20", "R21"], 7.0),
    ("ldo-analog", "U6", None, ["C47", "C48"], 4.0),
    ("ldo-phy", "U7", None, ["C49", "C50"], 4.0),
    # The card's pull-ups and decoupling beside the socket (17.7 mm long).
    ("sd", "J3", None, ["R%d" % i for i in range(22, 27)] + ["C51", "C52"], 14.0),
]

SOC_SIZE = (70, 50)
SOC_HOLES = {"H1": [3.0, 3.0], "H2": [67.0, 3.0], "H3": [3.0, 47.0], "H4": [67.0, 47.0]}

HARD_LIB_SOC = {
    "tsop54": "Package_SO:TSOP-II-54_22.2x10.16mm_P0.8mm",
    "soic8_208": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
    "qfn24": "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm",
    "sot23_6": "Package_TO_SOT_SMD:SOT-23-6",
    "microsd": "Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5",
    "swd_2x05": "Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical_SMD",
    "l_0805": "Inductor_SMD:L_0805_2012Metric",
}


def _fp(ref, kind, value, pins):
    """``pinned`` for this module's own footprints (and the shared ones)."""
    if kind in HARD_LIB_SOC:
        if isinstance(pins, (list, tuple)):
            pins = {str(i + 1): n for i, n in enumerate(pins)}
        return dict(ref=ref, footprint=HARD_LIB_SOC[kind], value=value, pins=dict(pins))
    return pinned(ref, kind, value, pins)


def ball_ring(ball):
    r, c = ROWS.index(ball[0]), int(ball[1:]) - 1
    return min(r, c, 14 - r, 14 - c)


def mcu_nets():
    """``{ball: net}`` for every ball of U1 (unused I/O: ``""``)."""
    nets = {}
    for row, names in STM32F746_UFBGA176.items():
        for col, name in enumerate(names.split()):
            if name == "-":
                continue
            nets[row + str(col + 1)] = SUPPLY_OF.get(name, SIGNALS.get(name, ""))
    return nets


def signal_balls():
    nets = mcu_nets()
    return sorted(b for b, n in nets.items() if n and n not in ("3V3", "3V3A", "GND"))


def caps(prefix, start, count, value, net, kind="c_0402"):
    return [pinned("%s%d" % (prefix, start + i), kind, value, [net, "GND"]) for i in range(count)]


def soc_parts():
    p = [pinned("U1", "ufbga201", "STM32F746IGK6", mcu_nets())]
    # MCU decoupling (ST AN4661): one 100 nF per VDD ball group, bulk 4.7 uF, VDDUSB and
    # VBAT 100 nF; VDDA and VREF+ 1 uF + 100 nF each; VCAP 2.2 uF each; NRST 100 nF.
    p += caps("C", 1, 12, "100n", "3V3")
    p += [
        pinned("C13", "capacitor", "4.7u", ["3V3", "GND"]),
        pinned("C14", "c_0402", "100n", ["3V3", "GND"]),  # VDDUSB
        pinned("C15", "c_0402", "100n", ["3V3", "GND"]),  # VBAT
        pinned("C16", "c_0402", "1u", ["3V3A", "GND"]),
        pinned("C17", "c_0402", "100n", ["3V3A", "GND"]),
        pinned("C18", "c_0402", "1u", ["3V3A", "GND"]),
        pinned("C19", "c_0402", "100n", ["3V3A", "GND"]),
        pinned("C20", "c_0402", "2.2u", ["VCAP1", "GND"]),
        pinned("C21", "c_0402", "2.2u", ["VCAP2", "GND"]),
        pinned("C22", "c_0402", "100n", ["NRST", "GND"]),
        pinned("R1", "r_0402", "10k", ["BOOT0", "GND"]),
        pinned("SW1", "button", "Reset", ["NRST", "GND"]),
        pinned("SW2", "button", "User", ["BTN", "3V3"]),
        pinned("R2", "r_0402", "10k", ["BTN", "GND"]),
        # 25 MHz HSE crystal (Crystal_SMD_3225-4Pin: 1 and 3 the crystal, 2 and 4 the case).
        pinned("Y1", "crystal_3225", "25MHz", ["OSC_IN", "GND", "OSC_OUT", "GND"]),
        pinned("C23", "c_0402", "12p", ["OSC_IN", "GND"]),
        pinned("C24", "c_0402", "12p", ["OSC_OUT", "GND"]),
    ]
    # SDRAM IS42S16400J-7TL, JEDEC x16 TSOP-II-54 (ISSI datasheet, "Pin configuration").
    sd = {
        1: "3V3",
        14: "3V3",
        27: "3V3",
        3: "3V3",
        9: "3V3",
        43: "3V3",
        49: "3V3",
        28: "GND",
        41: "GND",
        54: "GND",
        6: "GND",
        12: "GND",
        46: "GND",
        52: "GND",
        15: "FMC_NBL0",
        16: "FMC_SDNWE",
        17: "FMC_SDNCAS",
        18: "FMC_SDNRAS",
        19: "FMC_SDNE0",
        20: "FMC_BA0",
        21: "FMC_BA1",
        22: "FMC_A10",
        37: "FMC_SDCKE0",
        38: "FMC_SDCLK",
        39: "FMC_NBL1",
        36: "",
        40: "",
    }
    for i, pin in enumerate([2, 4, 5, 7, 8, 10, 11, 13, 42, 44, 45, 47, 48, 50, 51, 53]):
        sd[pin] = "FMC_D%d" % i
    for a, pin in zip(
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11], [23, 24, 25, 26, 29, 30, 31, 32, 33, 34, 35]
    ):
        sd[pin] = "FMC_A%d" % a
    p.append(_fp("U2", "tsop54", "IS42S16400J", {str(k): v for k, v in sd.items()}))
    p += caps("C", 25, 7, "100n", "3V3")  # C25-C31: one per VDD/VDDQ pin
    p.append(pinned("C32", "capacitor", "10u", ["3V3", "GND"]))
    # W25Q128JV, SOIC-8 208 mil (Winbond datasheet): 1 /CS, 2 DO (IO1), 3 /WP (IO2), 4 GND,
    # 5 DI (IO0), 6 CLK, 7 /HOLD (IO3), 8 VCC.
    p += [
        _fp(
            "U3",
            "soic8_208",
            "W25Q128JV",
            ["QSPI_NCS", "QSPI_IO1", "QSPI_IO2", "GND", "QSPI_IO0", "QSPI_CLK", "QSPI_IO3", "3V3"],
        ),
        pinned("C33", "c_0402", "100n", ["3V3", "GND"]),
        pinned("R3", "r_0402", "10k", ["3V3", "QSPI_NCS"]),
    ]
    # LAN8742A-CZ, QFN-24 4x4 (Microchip DS00001989, "Pin description"): 1 VDD2A, 2 LED2,
    # 3 LED1, 4 XTAL2, 5 XTAL1, 6 VDDCR, 7 RXD1, 8 RXD0, 9 VDDIO, 10 RXER, 11 CRS_DV,
    # 12 MDIO, 13 MDC, 14 nINT/REFCLKO (50 MHz to the MCU), 15 nRST, 16 TXEN, 17 TXD0,
    # 18 TXD1, 19 VDD1A, 20 TXN, 21 TXP, 22 RXN, 23 RXP, 24 RBIAS, 25 exposed pad (VSS).
    phy = [
        "3V3_PHY",
        "PHY_LED2",
        "PHY_LED1",
        "PHY_XO",
        "PHY_XI",
        "PHY_VDDCR",
        "PHY_RXD1",
        "PHY_RXD0",
        "3V3",
        "",
        "PHY_CRS_DV",
        "RMII_MDIO",
        "RMII_MDC",
        "PHY_REFCLK",
        "PHY_NRST",
        "PHY_TXEN",
        "PHY_TXD0",
        "PHY_TXD1",
        "3V3_PHY",
        "ETH_TXN",
        "ETH_TXP",
        "ETH_RXN",
        "ETH_RXP",
        "PHY_RBIAS",
        "GND",
    ]
    # The footprint's four paste pads are unnamed (no net).
    p.append(_fp("U4", "qfn24", "LAN8742A", {**{str(i + 1): n for i, n in enumerate(phy)}, "": ""}))
    p += [
        pinned("Y2", "crystal_3225", "25MHz", ["PHY_XI", "GND", "PHY_XO", "GND"]),
        pinned("C34", "c_0402", "15p", ["PHY_XI", "GND"]),
        pinned("C35", "c_0402", "15p", ["PHY_XO", "GND"]),
        pinned("C36", "c_0402", "100n", ["3V3", "GND"]),  # VDDIO
        pinned("C37", "c_0402", "100n", ["3V3_PHY", "GND"]),  # VDD1A
        pinned("C38", "c_0402", "100n", ["3V3_PHY", "GND"]),  # VDD2A
        pinned("C39", "c_0402", "470n", ["PHY_VDDCR", "GND"]),
        pinned("C40", "c_0402", "1u", ["PHY_VDDCR", "GND"]),
        pinned("R4", "r_0402", "12.1k", ["PHY_RBIAS", "GND"]),
        pinned("R5", "r_0402", "1.5k", ["3V3", "RMII_MDIO"]),
        pinned("R6", "r_0402", "10k", ["3V3", "PHY_NRST"]),
    ]
    # Series terminations (22 ohm) at each RMII driver: PHY-driven lines at the PHY,
    # MCU-driven lines at the MCU.
    series = [
        ("PHY_RXD0", "RMII_RXD0"),
        ("PHY_RXD1", "RMII_RXD1"),
        ("PHY_CRS_DV", "RMII_CRS_DV"),
        ("PHY_REFCLK", "RMII_REF_CLK"),
        ("RMII_TX_EN", "PHY_TXEN"),
        ("RMII_TXD0", "PHY_TXD0"),
        ("RMII_TXD1", "PHY_TXD1"),
    ]
    for i, (a, b) in enumerate(series):
        p.append(pinned("R%d" % (7 + i), "r_0402", "22", [a, b]))
    # MDI: 49.9 ohm pull-ups to the PHY's analog rail, centre-tap decoupling, and a header
    # to an external magnetics module (1 TX+, 2 TX-, 3 RX+, 4 RX-, 5 centre tap, 6 GND).
    for i, net in enumerate(["ETH_TXP", "ETH_TXN", "ETH_RXP", "ETH_RXN"]):
        p.append(pinned("R%d" % (14 + i), "r_0402", "49.9", ["3V3_PHY", net]))
    p += [
        pinned("C41", "c_0402", "100n", ["3V3_PHY", "GND"]),
        pinned("C42", "c_0402", "100n", ["3V3_PHY", "GND"]),
        pinned(
            "J2",
            "header_1x06",
            "Magnetics",
            ["ETH_TXP", "ETH_TXN", "ETH_RXP", "ETH_RXN", "3V3_PHY", "GND"],
        ),
        pinned("R18", "r_0402", "330", ["PHY_LED1", "PHY_LED1_A"]),
        pinned("D1", "led", "green", ["GND", "PHY_LED1_A"]),
        pinned("R19", "r_0402", "330", ["PHY_LED2", "PHY_LED2_A"]),
        pinned("D2", "led", "yellow", ["GND", "PHY_LED2_A"]),
    ]
    # USB full speed: Micro-B (1 VBUS, 2 D-, 3 D+, 4 ID, 5 GND, shield to GND), USBLC6-2SC6
    # (ST, SOT-23-6: 1 and 6 I/O1, 2 GND, 3 and 4 I/O2, 5 VBUS), PTC fuse to the 5 V rail.
    p += [
        pinned(
            "J1",
            "usb_micro_b",
            "USB Micro-B",
            {"1": "VBUS", "2": "USB_DN", "3": "USB_DP", "4": "", "5": "GND", "SH": "GND"},
        ),
        _fp(
            "U8", "sot23_6", "USBLC6-2SC6", ["USB_DP", "GND", "USB_DN", "USB_DN", "VBUS", "USB_DP"]
        ),
        pinned("F1", "fuse_1206", "1A PTC", ["VBUS", "V5"]),
        pinned("C43", "capacitor", "10u", ["V5", "GND"]),
    ]
    # Buck 5 V -> 3.3 V, TLV62569DBV (TI SLVSDH0, SOT-23-5: 1 EN, 2 GND, 3 SW, 4 VIN,
    # 5 FB; 0.6 V reference, 453k/100k divider).
    p += [
        pinned("U5", "sot23_5", "TLV62569DBV", ["V5", "GND", "BUCK_SW", "V5", "BUCK_FB"]),
        _fp("L1", "l_0805", "2.2u", ["BUCK_SW", "3V3"]),
        pinned("C44", "capacitor", "10u", ["V5", "GND"]),
        pinned("C45", "capacitor", "22u", ["3V3", "GND"]),
        pinned("C46", "capacitor", "22u", ["3V3", "GND"]),
        pinned("R20", "r_0402", "453k", ["3V3", "BUCK_FB"]),
        pinned("R21", "r_0402", "100k", ["BUCK_FB", "GND"]),
    ]
    # Two LDOs from 5 V, AP2112K-3.3 (Diodes DS37227: 1 VIN, 2 GND, 3 EN, 4 NC, 5 VOUT):
    # the MCU's analog supply and the PHY's analog supply.
    for ref, out, cin, cout in (("U6", "3V3A", "C47", "C48"), ("U7", "3V3_PHY", "C49", "C50")):
        p += [
            pinned(ref, "sot23_5", "AP2112K-3.3", ["V5", "GND", "V5", "", out]),
            pinned(cin, "c_0402", "1u", ["V5", "GND"]),
            pinned(cout, "c_0402", "1u", [out, "GND"]),
        ]
    # microSD (Hirose DM3AT: 1 DAT2, 2 CD/DAT3, 3 CMD, 4 VDD, 5 CLK, 6 VSS, 7 DAT0, 8 DAT1,
    # 9-10 the detect switch, unused; shell to GND), 47k pull-ups on CMD and data.
    p.append(
        _fp(
            "J3",
            "microsd",
            "microSD",
            {
                "1": "SD_D2",
                "2": "SD_D3",
                "3": "SD_CMD",
                "4": "3V3",
                "5": "SD_CK",
                "6": "GND",
                "7": "SD_D0",
                "8": "SD_D1",
                "9": "",
                "10": "",
                "SH": "GND",
            },
        )
    )
    for i, net in enumerate(["SD_CMD", "SD_D0", "SD_D1", "SD_D2", "SD_D3"]):
        p.append(pinned("R%d" % (22 + i), "r_0402", "47k", ["3V3", net]))
    p += [
        pinned("C51", "c_0402", "100n", ["3V3", "GND"]),
        pinned("C52", "capacitor", "10u", ["3V3", "GND"]),
    ]
    # Cortex debug 10-pin (1.27 mm): 1 VTref, 2 SWDIO, 3 GND, 4 SWCLK, 5 GND, 6 SWO, 7 key,
    # 8 NC, 9 GNDdetect, 10 nRESET.
    p.append(
        _fp(
            "J4",
            "swd_2x05",
            "SWD",
            ["3V3", "SWDIO", "GND", "SWCLK", "GND", "SWO", "", "", "GND", "NRST"],
        )
    )
    # I/O connector (JST SH 8): 3V3, GND, UART, I2C (4.7k pull-ups), two GPIO.
    io = ["3V3", "GND", "UART_TX", "UART_RX", "I2C_SCL", "I2C_SDA", "GPIO1", "GPIO2"]
    p += [
        pinned("J5", "jst_sh_8", "I/O", {**{str(i + 1): n for i, n in enumerate(io)}, "MP": ""}),
        pinned("R27", "r_0402", "4.7k", ["3V3", "I2C_SCL"]),
        pinned("R28", "r_0402", "4.7k", ["3V3", "I2C_SDA"]),
    ]
    # LEDs (LED_0805: pad 1 cathode): RGB status on PI1-PI3 and the 3.3 V power LED.
    for i, (net, colour) in enumerate((("LED_R", "red"), ("LED_G", "green"), ("LED_B", "blue"))):
        p += [
            pinned("R%d" % (29 + i), "r_0402", "1k", [net, net + "_A"]),
            pinned("D%d" % (3 + i), "led", colour, ["GND", net + "_A"]),
        ]
    p += [
        pinned("R32", "r_0402", "1k", ["3V3", "PWR_A"]),
        pinned("D6", "led", "green", ["GND", "PWR_A"]),
    ]
    for ref in SOC_HOLES:
        p.append(pinned(ref, "hole_m2", "M2", {"": ""}))
    return p


def soc_bga():
    """``12-soc-bga-N``: the board on six layers (S G S G P S: GND In1 and In3, 3V3 In4)."""
    parts = soc_parts()
    spec = circuit(
        "12-soc-bga-%d" % len(parts),
        "STM32F746 (0.65 mm UFBGA176+25) single-board computer: 16-bit SDRAM with a "
        "length-matched byte lane, quad-SPI flash, RMII Ethernet PHY with series and MDI "
        "terminations, USB full speed, microSD, SWD and I/O connectors; a buck and two LDOs "
        "make four supply domains on six layers.",
        parts,
        SOC_SIZE,
    )
    cons = spec["constraints"]
    cons["board"]["default_clearance_mm"] = 0.2
    # The fine-pitch rules of 11-ufbga201-fanout: 0.10/0.10 mm tracks, 0.40/0.20 mm vias,
    # 0.35/0.15 mm plane drops in the array.
    cons["fab"] = dict(
        track_width_mm=0.1,
        clearance_mm=0.1,
        via_diameter_mm=0.4,
        via_drill_mm=0.2,
        hole_clearance_mm=0.15,
        edge_clearance_mm=0.3,
        min_through_drill_mm=0.15,
        via_annular_mm=0.075,
        min_track_width_mm=0.1,
        smd_pad_clearance_mm=0.1,
        hole_to_hole_mm=0.25,
        via_to_smd_pad_mm=0.1,
        min_via_diameter_mm=0.35,
    )
    cons["net_class"] = {
        "supply": dict(nets=["3V3"], width_mm=0.1),
        "return": dict(nets=["GND"], width_mm=0.1),
        # USB 5 V and the fused rail: 0.5 A (10 C rise) sized by the engine.
        "input": dict(nets=["VBUS", "V5"], current_a=0.5, copper_oz=1, delta_t_c=10),
        # The analog rails, routed on signal layers.
        "analog": dict(nets=["3V3A", "3V3_PHY"], width_mm=0.25),
        "switch": dict(nets=["BUCK_SW"], width_mm=0.4),
    }
    w, h = SOC_SIZE
    cons["fixed"] = {
        "U1": dict(at=[35.0, 28.0], rot=0, side="top"),
        # The SDRAM south of the array, its pin rows facing north and south.
        "U2": dict(at=[35.0, 9.5], rot=90, side="top"),
        # USB on the north edge, its mouth facing out; the magnetics header on the west edge.
        "J1": dict(at=[54.0, h - 4.5], rot=180, side="top"),
        "J2": dict(at=[3.0, 25.0], rot=0, side="top"),
        **{ref: dict(at=at, rot=0, side="top") for ref, at in SOC_HOLES.items()},
    }
    # The microSD socket on the east edge, the debug header on the north edge, the I/O
    # connector on the south edge: placed by the engine along their edges.
    cons["edge_align"] = {
        "J3": dict(edge="east", hard=True, tolerance_mm=1.0),
        "J4": dict(edge="north", hard=True, tolerance_mm=1.0),
        "J5": dict(edge="south", hard=True, tolerance_mm=1.0),
    }
    cons["group"] = []
    for _name, anchor, pad, members, radius in SOC_GROUPS:
        group = dict(members=list(members), anchor=anchor, hard=True, radius_mm=radius)
        if pad is not None:
            group["anchor_pad"] = pad
        cons["group"].append(group)
    cons["diff_pair"] = [
        dict(name="usb", p="USB_DP", n="USB_DN", width_mm=0.15, gap_mm=0.15, skew_mm=1.0),
        dict(name="eth_tx", p="ETH_TXP", n="ETH_TXN", width_mm=0.15, gap_mm=0.15, skew_mm=1.0),
        dict(name="eth_rx", p="ETH_RXP", n="ETH_RXN", width_mm=0.15, gap_mm=0.15, skew_mm=1.0),
    ]
    cons["length_match"] = [dict(name="dq_high", nets=list(LANE), tolerance_mm=LANE_TOLERANCE_MM)]
    cons["fanout"] = [
        dict(
            name="u1",
            ref="U1",
            via_classes=dict(
                planes=dict(
                    diameter_mm=0.35, drill_mm=0.15, nets=["GND", "3V3"], sites=["interstitial"]
                ),
                default=dict(diameter_mm=0.4, drill_mm=0.2, sites=["vacant", "outside"]),
            ),
            surface_rings=2,
        )
    ]
    spec["supply"] = dict(voltage_v=5, max_current_a=0.5)
    spec = hard(
        spec,
        "soc-bga",
        ["bga-fanout", "fine-pitch", "length-match", "diff-pairs", "power-domains", "buses"],
        "manual",
        240,
    )
    # A target for the router (2026-10-06, GCP seeds 0 and 1): about 34 of its 112 nets stay
    # open, the SDRAM lane is not matched, and tracks cross the microSD and QFN footprints'
    # own keep-outs (the router does not read footprint rule areas).
    spec["ci"]["target"] = dict(
        feature="dense-board routing",
        reason=(
            "about 34 of 112 nets left open; the length-matched lane out of skew; footprint "
            "keep-outs and PTH hole spacing not modelled by the router"
        ),
    )
    spec["checks"] = base_checks(spec) + [
        dict(
            id="edge-" + ref, kind="edge", ref=ref, edge=r["edge"], max_mm=1.0, engine="edge_align"
        )
        for ref, r in sorted(cons["edge_align"].items())
    ]
    for name, anchor, pad, members, radius in SOC_GROUPS:
        check = dict(
            id="near-" + name,
            kind="proximity",
            anchor=anchor,
            refs=list(members),
            max_mm=radius,
            engine="group",
        )
        if pad is not None:
            check["anchor_pad"] = pad
        spec["checks"].append(check)
    spec["checks"] += [
        dict(
            id="fanout-plane-vias",
            kind="via_class",
            ref="U1",
            nets=["GND", "3V3"],
            diameter_mm=0.35,
            drill_mm=0.15,
            site="interstitial",
            engine="fanout",
        ),
        dict(id="fanout-escape", kind="escape", ref="U1", pads=signal_balls(), engine="fanout"),
    ]
    spec = with_stackup(spec, "6L-SGSGPS", power="3V3")
    spec["name"] = "12-soc-bga-%d" % len(parts)  # the top rung's name carries no stackup
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    return deepcopy(spec)


def rungs():
    return [soc_bga()]
