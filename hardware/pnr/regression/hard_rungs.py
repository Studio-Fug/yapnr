"""Hard rungs: larger and denser open boards beyond the eight-case ladder.

Run with ``run.py --hard``. Like the showcases they sit outside the ladder's gate
(``designs()`` and its digest are unchanged). They exercise what the small cases do
not: a dense mixed MCU board (TQFP-44, crystal, USB differential pairs, regulator),
hierarchical block synthesis with a template reused four times, current-sized power
copper, and Monte-Carlo successive-halving search over placements.

Most rungs are *variants* of two base designs (``09-mcu-usb-31`` and the ladder's
``07-chaser-20``) that change exactly one dimension, so an effect can be attributed.
``11-ufbga201-fanout`` breaks a 0.65 mm UFBGA176+25 out to four connectors on six
layers through a declared ``fanout`` (pnr.fanout): interstitial plane drops, surface
and dog-bone escapes, a reserved corridor and a per-layer forbidden exit; its
``-block`` variant puts a fixed RF launch beside it (ground vias on the ball lattice,
a class keep-out with the supply plane cut out, a class guard, an F.Cu-only rule
area). The dimensions:

``stackup``
    the copper stack: number of plane layers and signal layers and their order, as a
    real KiCad 10 board stackup (layer types, dielectrics) with plane zones;
``via_policy``
    through vias only, or blind/buried vias (and microvias) permitted, written as
    KiCad custom rules (``.kicad_dru``) so the judge itself enforces it; where
    microvias are permitted, a ``microvia_span`` check holds each to one dielectric,
    which KiCad's DRC does not;
``sides``
    single-sided (every part on the top side), double-sided (the tool may use both
    sides), or assigned (the designer locks named parts to the bottom side);
``constraints``
    absolute (fixed poses, edge locks, keep-outs, regions, side locks) and relative
    (proximity groups, lines with a pitch, ordering, alignment) placement constraints.

Every rung carries a tool-neutral description of those dimensions (``stackup``,
``via_policy``, ``sides``, ``checks``) besides the engine's own ``constraints``. The
``checks`` are verified on the saved board by ``check_constraints.py``, which reads
any tool's KiCad board, so other place-and-route tools are judged by the same list.
Each check names the engine constraint that expresses it (``engine``); a check the
engine could not express would carry ``"engine": "unsupported"``: still measured,
never silently dropped.

Each rung has a ``ci`` tag: ``lane`` (``nightly``: informational, every night;
``manual``: on request only) and ``minutes``, the per-seed wall budget its lane must
allow. Footprints are KiCad 10 stock library parts; pin maps follow the device
datasheets named beside each part.
"""

import math
import re
from copy import deepcopy

from designs import LIB, chaser, circuit, part, timer_parts

HARD_LIB = {
    "tqfp44": "Package_QFP:TQFP-44_10x10mm_P0.8mm",
    "usb_micro_b": "Connector_USB:USB_Micro-B_Molex-105017-0001",
    "crystal_3225": "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
    "sot23_5": "Package_TO_SOT_SMD:SOT-23-5",
    "sot23": "Package_TO_SOT_SMD:SOT-23",
    "header_1x06": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    "header_1x08": "Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
    "jst_sh_8": "Connector_JST:JST_SH_SM08B-SRSS-TB_1x08-1MP_P1.00mm_Horizontal",
    "fuse_1206": "Fuse:Fuse_1206_3216Metric",
    "d_sma": "Diode_SMD:D_SMA",
    "cp_6x7": "Capacitor_SMD:CP_Elec_6.3x7.7",
    "c_1206": "Capacitor_SMD:C_1206_3216Metric",
    "terminal_2p": "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
    "hole_m2": "MountingHole:MountingHole_2.2mm_M2",
    "u_fl": "Connector_Coaxial:U.FL_Hirose_U.FL-R-SMT-1_Vertical",
    "ufbga201": "Package_BGA:UFBGA-201_10x10mm_Layout15x15_P0.65mm",
    "jst_sh_12": "Connector_JST:JST_SH_SM12B-SRSS-TB_1x12-1MP_P1.00mm_Horizontal",
    "jst_sh_2": "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
    "c_0402": "Capacitor_SMD:C_0402_1005Metric",
    "r_0402": "Resistor_SMD:R_0402_1005Metric",
    # A 1 mm fiducial with its own 0.6 mm clearance and 0.5 mm mask margin.
    "fiducial_1mm": "Fiducial:Fiducial_1mm_Mask2mm",
    "vqfn_hr10": "Package_DFN_QFN:Texas_RPU0010A_VQFN-HR-10_2x2mm_P0.5mm",
    "l_1210": "Inductor_SMD:L_1210_3225Metric",
    "header_1x03": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
}


# Pad names a footprint repeats (one net for every copy): the receptacle's eight
# shield pads. The generator maps a name to all its pads; the connected-pad count the
# runner asserts counts each copy.
REPEATED_PADS = {
    HARD_LIB["usb_micro_b"]: {"SH": 8},
    HARD_LIB["u_fl"]: {"2": 2},
    HARD_LIB["jst_sh_12"]: {"MP": 2},
}


def connected_pads(parts):
    return sum(
        REPEATED_PADS.get(p["footprint"], {}).get(pin, 1)
        for p in parts
        for pin, net in p["pins"].items()
        if net
    )


def pinned(ref, kind, value, pins):
    """A part with an explicit pad -> net map (repeated or named pads such as USB shields)."""
    footprint = HARD_LIB[kind] if kind in HARD_LIB else LIB[kind]
    if isinstance(pins, (list, tuple)):
        pins = {str(i + 1): n for i, n in enumerate(pins)}
    return dict(ref=ref, footprint=footprint, value=value, pins=dict(pins))


# ------------------------------------------------------------------ dimensions

# Copper stacks, outer to outer: S = signal, G = ground plane, P = power plane. The
# dielectric thicknesses are a plausible 1.6 mm build (1 oz outer, 0.5 oz inner copper)
# for impedance-aware tools; KiCad's DRC does not depend on them.
STACKUPS = {
    "2L": "SS",
    "4L-SGPS": "SGPS",
    "4L-SGGS": "SGGS",
    "4L-SSGS": "SSGS",
    "6L-SGSSPS": "SGSSPS",
    "6L-SGSGPS": "SGSGPS",
    "8L-SGSGPSGS": "SGSGPSGS",
}
DIELECTRICS_MM = {
    2: [("core", 1.51)],
    4: [("prepreg", 0.2104), ("core", 1.065), ("prepreg", 0.2104)],
    6: [("prepreg", 0.09), ("core", 0.55), ("prepreg", 0.2), ("core", 0.55), ("prepreg", 0.09)],
    8: [
        ("prepreg", 0.09),
        ("core", 0.33),
        ("prepreg", 0.14),
        ("core", 0.33),
        ("prepreg", 0.14),
        ("core", 0.33),
        ("prepreg", 0.09),
    ],
}

VIA_POLICIES = {
    "through": dict(name="through", allowed=["through"]),
    "blind-buried": dict(name="blind-buried", allowed=["through", "blind", "buried"]),
    # A 0.1 mm laser drill in a 0.4 mm land: the 0.15 mm ring every rung's fab
    # (designs.py, via_annular_mm) requires of a via, which KiCad also applies to
    # microvias (a 0.3 mm land, 0.1 mm ring, fails the judge's annular_width rule).
    "hdi": dict(
        name="hdi",
        allowed=["through", "blind", "buried", "micro"],
        microvia=dict(diameter_mm=0.4, drill_mm=0.1),
    ),
}


def copper_names(count):
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


def stackup(name, ground="GND", power="VCC"):
    """A tool-neutral stackup record: copper layers in order with their role
    (``signal`` or ``plane``) and plane net, plus the dielectric build."""
    code = STACKUPS[name]
    names = copper_names(len(code))
    layers = []
    for layer, role in zip(names, code):
        if role == "S":
            layers.append(dict(name=layer, role="signal"))
        else:
            layers.append(dict(name=layer, role="plane", net=ground if role == "G" else power))
    dielectrics = [dict(kind=k, thickness_mm=t) for k, t in DIELECTRICS_MM[len(code)]]
    return dict(
        name=name,
        copper_layers=len(code),
        layers=layers,
        dielectrics=dielectrics,
        outer_copper_mm=0.035,
        inner_copper_mm=0.0152,
        board_thickness_mm=1.6,
    )


def plane_layers(spec):
    """``[(layer, net)]`` of the rung's plane layers, outer to outer."""
    return [(x["name"], x["net"]) for x in spec["stackup"]["layers"] if x["role"] == "plane"]


def with_stackup(spec, name, *, power="VCC"):
    """``spec`` on the named stackup. The engine's vocabulary is one plane layer per
    net class (``net_class.*.plane_layer``): the first plane layer of each plane net
    becomes that net's class plane, in a class of its own. A further plane layer of the
    same net (two ground planes) cannot be said that way; the generator draws it as a
    full-outline zone in the source board (``native.py``), and the engine's planes
    stage refills it. The plane-layer rule (no tracks on a plane layer) is the
    judge's, whatever the engine knows."""
    spec = deepcopy(spec)
    spec["stackup"] = stackup(name, power=power)
    cons = spec["constraints"]
    cons["board"]["layers"] = spec["stackup"]["copper_layers"]
    classes = cons["net_class"]
    seen = set()
    for layer, net in plane_layers(spec):
        if net in seen:
            continue
        seen.add(net)
        owner = next((k for k, c in classes.items() if net in c.get("nets", [])), None)
        template = dict(classes[owner]) if owner else dict(width_mm=0.4)
        if owner:
            classes[owner]["nets"] = [n for n in classes[owner]["nets"] if n != net]
            if not classes[owner]["nets"]:
                del classes[owner]
        template.pop("plane_layer", None)
        classes["plane_" + net.lower()] = dict(template, nets=[net], plane_layer=layer)
    spec["layer_mode"] = "planes" if seen else "routed_return"
    for layer, net in plane_layers(spec):
        first = classes["plane_" + net.lower()]["plane_layer"] == layer
        spec["checks"].append(
            dict(
                id="plane-%s-%s" % (layer.split(".")[0], net),
                kind="plane",
                layer=layer,
                net=net,
                min_fill_fraction=0.5,
                engine="net_class.plane_layer" if first else "source zone",
            )
        )
    spec["name"] += "-" + name
    spec["dims"]["stackup"] = name
    spec["features"] = sorted(set(spec["features"]) | {"stackup-" + name})
    return spec


def with_via_policy(spec, policy):
    spec = deepcopy(spec)
    spec["via_policy"] = deepcopy(VIA_POLICIES[policy])
    spec["name"] += {"blind-buried": "-BB", "hdi": "-HDI"}[policy]
    spec["dims"]["via_policy"] = policy
    spec["features"] = sorted(set(spec["features"]) | {"via-" + policy})
    if "micro" in spec["via_policy"]["allowed"]:
        # KiCad's DRC accepts a microvia of any span; a laser drill crosses one
        # dielectric. (A .kicad_dru rule cannot say it without reading, to a tool that
        # takes its via bans from the rules, as a ban on every microvia.)
        spec["checks"].append(
            dict(id="microvia-span", kind="microvia_span", max_dielectrics=1, engine="via_policy")
        )
    return spec


def with_double_sided(spec):
    """Parts may sit on either side: the single-sided ``side`` check is dropped and
    nothing else changes (no part is assigned a side; the tool decides)."""
    spec = deepcopy(spec)
    spec["sides"] = "double"
    spec["checks"] = [c for c in spec["checks"] if c["id"] != "single-sided"]
    spec["name"] += "-double"
    spec["dims"]["sides"] = "double"
    spec["features"] = sorted(set(spec["features"]) | {"double-sided"})
    return spec


def dru_text(spec):
    """KiCad 10 custom rules (``.kicad_dru``) that make the judge enforce the rung's
    via policy, its plane layers (a plane layer carries no tracks), its
    differential-pair skew and its own rules (``dru_rules``: a ``-classes`` rung's).
    ``None`` for a rung without such dimensions."""
    if "via_policy" not in spec:
        return None
    rules = ["(version 1)"]
    allowed = set(spec["via_policy"]["allowed"])
    banned = [kind + "_via" for kind in ("blind", "buried", "micro") if kind not in allowed]
    if banned:
        rules.append(
            '(rule "via policy %s: no %s"\n  (constraint disallow %s))'
            % (
                spec["via_policy"]["name"],
                "/".join(b.replace("_via", "") for b in banned),
                " ".join(banned),
            )
        )
    for layer, net in plane_layers(spec):
        rules.append(
            '(rule "%s is a %s plane: no tracks"\n  (layer "%s")\n'
            "  (condition \"A.Type == 'Track'\")\n  (constraint disallow track))"
            % (layer, net, layer)
        )
    for pair in spec["constraints"].get("diff_pair") or []:
        base = pair["p"][:-1]
        rules.append(
            '(rule "pair %s skew"\n  (condition "A.inDiffPair(\'%s\')")\n'
            "  (constraint skew (max %gmm)))" % (pair["name"], base, pair["skew_mm"])
        )
    rules += list(spec.get("dru_rules") or ())
    return "\n".join(rules) + "\n"


# --------------------------------------------------------------------- checks


def base_checks(spec, fixed_ids=True):
    """Checks every rung carries: each part's courtyard inside the outline, the
    single-sided rule, and every authored fixed pose."""
    checks = [
        dict(id="inside-board", kind="inside_board", refs="*", engine="native"),
        dict(id="single-sided", kind="side", refs="*", side="top", engine="native"),
    ]
    for ref, pose in sorted((spec["constraints"].get("fixed") or {}).items()):
        checks.append(
            dict(
                id="fixed-" + ref,
                kind="fixed",
                ref=ref,
                at=list(pose["at"]),
                rot=pose.get("rot", 0),
                side=pose.get("side", "top"),
                tol_mm=0.01,
                engine="fixed",
            )
        )
    return checks


def derived_checks(spec):
    """Checks for a ladder or showcase design without a ``checks`` list, derived from
    its engine constraints (fixed poses, hard edge locks, orientations, line groups,
    hard rectangle regions, hard origin-anchored aligns) plus the board-wide
    inside-the-outline and single-sided checks. A region or align the checker cannot
    measure (a polygon or union, another anchor, a glob or @address) gets no derived
    check."""
    cons = spec["constraints"]
    checks = [
        dict(id="inside-board", kind="inside_board", refs="*", engine="native"),
        dict(id="single-sided", kind="side", refs="*", side="top", engine="native"),
    ]
    for ref, pose in sorted((cons.get("fixed") or {}).items()):
        checks.append(
            dict(
                id="fixed-" + ref,
                kind="fixed",
                ref=ref,
                at=list(pose["at"]),
                rot=pose.get("rot", 0),
                side=pose.get("side", "top"),
                tol_mm=0.01,
                engine="fixed",
            )
        )
    for ref, rule in sorted((cons.get("edge_align") or {}).items()):
        if rule.get("hard"):
            checks.append(
                dict(
                    id="edge-" + ref,
                    kind="edge",
                    ref=ref,
                    edge=rule["edge"],
                    max_mm=rule.get("tolerance_mm", 1.0),
                    engine="edge_align",
                )
            )
    for ref, rot in sorted((cons.get("orientation") or {}).items()):
        rot = rot["rot"] if isinstance(rot, dict) else rot
        checks.append(
            dict(id="rot-" + ref, kind="orientation", ref=ref, rot=rot, engine="orientation")
        )
    for group in cons.get("line_group") or []:
        if group.get("pitch_mm") is not None:
            checks.append(
                dict(
                    id="line-" + group["name"],
                    kind="line",
                    refs=group["members"],
                    pitch_mm=group["pitch_mm"],
                    rot=group.get("rot", 0),
                    tol_mm=0.01,
                    engine="line_group",
                )
            )

    def plain(refs):  # the checker takes literal refs, not globs or @addresses
        return all(not set(r) & set("*?[@") for r in refs)

    for rule in cons.get("region") or []:
        if rule.get("hard", True) and rule.get("rect") is not None and plain(rule["refs"]):
            checks.append(
                dict(
                    id="region-" + rule["name"],
                    kind="region",
                    refs=list(rule["refs"]),
                    rect=list(rule["rect"]),
                    engine="region",
                )
            )
    for rule in cons.get("align") or []:
        if (
            rule.get("hard", True)
            and rule.get("anchor", "origin") == "origin"
            and plain(rule["refs"])
        ):
            checks.append(
                dict(
                    id="align-" + rule["name"],
                    kind="align",
                    refs=list(rule["refs"]),
                    axis=rule["axis"],
                    tol_mm=rule.get("tol_mm", 0.25),
                    engine="align",
                )
            )
    return checks


def hard(spec, base, features, lane, minutes):
    """Tag a rung: base design, dimensions, checks and its CI lane/budget."""
    spec["tier"] = "hard"
    spec["base"] = base
    spec["features"] = sorted(features)
    if spec["constraints"]["board"]["layers"] != 2:
        raise ValueError("a base design is two-layer; with_stackup() makes the others")
    spec.setdefault("stackup", stackup("2L"))
    spec.setdefault("via_policy", deepcopy(VIA_POLICIES["through"]))
    spec.setdefault("sides", "single")
    spec.setdefault("checks", base_checks(spec))
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    spec["dims"] = dict(
        stackup=spec["stackup"]["name"],
        via_policy=spec["via_policy"]["name"],
        sides=spec["sides"],
        constraints="base",
        search="single" if spec.get("driver") != "mc" else "mc",
        parts="base",
    )
    spec["ci"] = dict(lane=lane, minutes=minutes)
    return spec


# ------------------------------------------------------------------ MCU board

# ATmega32U4-AU, TQFP-44 (Microchip ATmega16U4/32U4 datasheet DS40002547, "Pinout";
# the same map as KiCad's MCU_Microchip_ATmega:ATmega32U4-A symbol). Unused I/O stay
# unconnected. LED/serial assignments follow the Arduino Leonardo.
ATMEGA32U4 = {
    1: "",  # PE6
    2: "VCC",  # UVCC
    3: "D_N",  # D-
    4: "D_P",  # D+
    5: "GND",  # UGND
    6: "UCAP",
    7: "VCC",  # VBUS sense
    8: "RXLED",  # PB0
    9: "",  # PB1
    10: "",  # PB2
    11: "",  # PB3
    12: "",  # PB7
    13: "RESET",
    14: "VCC",
    15: "GND",
    16: "XTAL2",
    17: "XTAL1",
    18: "SCL",  # PD0
    19: "SDA",  # PD1
    20: "RXD",  # PD2
    21: "TXD",  # PD3
    22: "TXLED",  # PD5
    23: "GND",
    24: "VCC",  # AVCC
    25: "",  # PD4
    26: "",  # PD6
    27: "",  # PD7
    28: "",  # PB4
    29: "",  # PB5
    30: "",  # PB6
    31: "",  # PC6
    32: "LED13",  # PC7
    33: "HWB",  # PE2/HWB, pulled down: boot to the USB bootloader on reset
    34: "VCC",
    35: "GND",
    36: "A0",  # PF7
    37: "BTN",  # PF6, internal pull-up
    38: "",  # PF5
    39: "",  # PF4
    40: "",  # PF1
    41: "",  # PF0
    42: "AREF",
    43: "GND",
    44: "VCC",  # AVCC
}

MCU_SIZE = (46, 34)  # the smallest outline where every probe seed legalized (40x30: none)
MCU_IO = ["VCC", "3V3", "GND", "SCL", "SDA", "TXD", "RXD", "A0"]
# R1 and R2 (pad 1 the connector-side leg, pad 2 the MCU-side leg): their pads stay
# parallel across both legs, whatever holds them side by side (check_pair_bridge).
# max_pitch_mm also catches a placement that is merely parallel, not side by side
# (owner review 2026-10-06): a generous bound above mcu_relative's hand-picked
# 2.5 mm line_group pitch, wide of any pitch the inference itself would choose,
# but well under "opposite ends of a 46x34 mm board".
MCU_PAIR_BRIDGE_CHECK = dict(
    id="pair-bridge-r1-r2",
    kind="pair_bridge",
    refs=["R1", "R2"],
    near_pad="1",
    far_pad="2",
    tol_mm=0.05,
    max_pitch_mm=6.0,
    engine="line_group",
)


def mcu_parts():
    return [
        # Molex 105017-0001: 1 VBUS, 2 D-, 3 D+, 4 ID, 5 GND, SH shield (8 pads), tied to GND.
        pinned(
            "J1",
            "usb_micro_b",
            "USB Micro-B",
            {"1": "VBUS", "2": "USB_DN", "3": "USB_DP", "4": "", "5": "GND", "SH": "GND"},
        ),
        pinned("F1", "fuse_1206", "500mA PTC", ["VBUS", "VCC"]),
        pinned("U1", "tqfp44", "ATmega32U4-AU", {str(k): v for k, v in ATMEGA32U4.items()}),
        pinned("R1", "resistor", "22", ["USB_DP", "D_P"]),
        pinned("R2", "resistor", "22", ["USB_DN", "D_N"]),
        # Crystal_SMD_3225-4Pin: 1 and 3 the crystal, 2 and 4 the case (ground).
        pinned("Y1", "crystal_3225", "16MHz", ["XTAL1", "GND", "XTAL2", "GND"]),
        pinned("C8", "capacitor", "22p", ["XTAL1", "GND"]),
        pinned("C9", "capacitor", "22p", ["XTAL2", "GND"]),
        pinned("C1", "capacitor", "10u", ["VCC", "GND"]),
        pinned("C2", "capacitor", "100n", ["VCC", "GND"]),
        pinned("C3", "capacitor", "100n", ["VCC", "GND"]),
        pinned("C4", "capacitor", "100n", ["VCC", "GND"]),
        pinned("C5", "capacitor", "100n", ["VCC", "GND"]),
        pinned("C6", "capacitor", "1u", ["UCAP", "GND"]),
        pinned("C7", "capacitor", "100n", ["AREF", "GND"]),
        pinned("R3", "resistor", "10k", ["VCC", "RESET"]),
        pinned("SW1", "button", "Reset", ["RESET", "GND"]),
        pinned("R4", "resistor", "10k", ["HWB", "GND"]),
        pinned("SW2", "button", "User", ["BTN", "GND"]),
        # AP2112K-3.3 (Diodes Inc. DS37227; KiCad Regulator_Linear:AP2112K-3.3):
        # 1 VIN, 2 GND, 3 EN (tied to VIN), 4 NC, 5 VOUT.
        pinned("U2", "sot23_5", "AP2112K-3.3", ["VCC", "GND", "VCC", "", "3V3"]),
        pinned("C10", "capacitor", "1u", ["VCC", "GND"]),
        pinned("C11", "capacitor", "1u", ["3V3", "GND"]),
        # JST SH 8-pin side-entry I/O connector: pins 1-8; the two mounting pads (MP) are
        # mechanical and left unconnected.
        pinned(
            "J2",
            "jst_sh_8",
            "I/O",
            {
                **{str(i + 1): n for i, n in enumerate(MCU_IO)},
                "MP": "",
            },
        ),
        # LED_0805: pad 1 cathode, pad 2 anode (as in the ladder).
        pinned("R5", "resistor", "1k", ["VCC", "PWR_A"]),
        pinned("D1", "led", "green", ["GND", "PWR_A"]),
        pinned("R6", "resistor", "1k", ["RXLED", "RX_A"]),
        pinned("D2", "led", "yellow", ["GND", "RX_A"]),
        pinned("R7", "resistor", "1k", ["TXLED", "TX_A"]),
        pinned("D3", "led", "yellow", ["GND", "TX_A"]),
        pinned("R8", "resistor", "1k", ["LED13", "L_A"]),
        pinned("D4", "led", "red", ["GND", "L_A"]),
    ]


def mcu_usb():
    """A USB microcontroller board in the Arduino Leonardo / Pro Micro class.

    ATmega32U4 with a 16 MHz crystal and load capacitors, per-pin decoupling, UCAP and
    AREF capacitors, a USB Micro-B receptacle (D+/D- through 22 ohm series resistors:
    two differential pairs), a PTC fuse, a 3.3 V LDO (AP2112K-3.3) for an I/O header,
    reset and user buttons, and power, RX, TX and user LEDs. The receptacle is fixed
    on the west edge with its mouth facing out; everything else moves.
    """
    p = mcu_parts()
    spec = circuit(
        "09-mcu-usb-%d" % len(p),
        "ATmega32U4 USB board: crystal, decoupling, USB Micro-B with two differential pairs, "
        "PTC fuse, 3.3 V LDO, two buttons and four LEDs.",
        p,
        MCU_SIZE,
    )
    cons = spec["constraints"]
    # The receptacle's mouth faces west (rot 270 turns its local +y, the mating side
    # with the footprint's "PCB Edge" mark, to -x); its courtyard stays on the board,
    # so the edge mark sits 1.3 mm inside the outline.
    cons["fixed"] = {"J1": dict(at=[4.0, MCU_SIZE[1] / 2], rot=270, side="top")}
    cons["net_class"]["supply"].update(nets=["VCC", "VBUS", "3V3"], current_a=0.5)
    # USB 2.0 full speed: both pairs declared to the engine (coupled routing, skew
    # budget); the judge's custom rules hold the same skew limit (dru_text).
    # Width and gap at the fixtures' track/clearance minimums (USB full speed is not
    # impedance-critical on a 1.6 mm two-layer board).
    cons["diff_pair"] = [
        dict(name="usb", p="USB_DP", n="USB_DN", width_mm=0.25, gap_mm=0.2, skew_mm=1.0),
        dict(name="usb_mcu", p="D_P", n="D_N", width_mm=0.25, gap_mm=0.2, skew_mm=1.0),
    ]
    spec["supply"] = dict(voltage_v=5, max_current_a=0.5)
    # R1 and R2 are the two pairs' shared series resistors (pad 1 on the connector
    # leg, pad 2 on the MCU leg): neither is tied to the MCU or fixed, so
    # pnr.constraints.infer_series_line_groups holds them side by side (the owner's
    # -rails review, 2026-10-05: a line constraint "would produce more human-like
    # corridor routing" than leaving them wherever placement's general objective
    # drops them). MCU_PAIR_BRIDGE_CHECK verifies that geometrically, independent of
    # the inference: whichever constraint (or hand-authored one, as -rel's) ends up
    # holding them, their pads stay parallel across both legs.
    spec["checks"] = base_checks(spec) + [MCU_PAIR_BRIDGE_CHECK]
    return hard(
        spec,
        "mcu-usb-31",
        ["dense-mixed", "tqfp44", "usb-diff-pair", "crystal", "regulator", "usb-series-line"],
        "nightly",
        45,
    )


# Absolute placement constraints (engine frame: mm from the outline's lower-left
# corner, y up; positions are footprint origins, as KiCad stores them).
MCU_HOLES = {"H1": [3.0, MCU_SIZE[1] - 3.0], "H2": [MCU_SIZE[0] - 3.0, 3.0]}
# A 12 x 6 mm label field on the north edge.
MCU_KEEPOUT = [MCU_SIZE[0] / 2 - 6, MCU_SIZE[1] - 6, MCU_SIZE[0] / 2 + 6, MCU_SIZE[1]]
MCU_REGION = [0.0, 0.0, MCU_SIZE[0] / 2, MCU_SIZE[1] / 2]  # the regulator: south-west quarter
# Proximity radii (origin to origin) around the MCU, whose courtyard alone reaches 6.7 mm from
# its centre: a decoupling capacitor touching it sits about 8.1 mm out. These are the smallest
# radii every placement probe seed legalized (10 mm for both groups: none).
MCU_NEAR = dict(decoupling=12.0, crystal=13.0)


def absolute(spec, *, holes, edges, rotations, keepout, region, describe):
    """Absolute placement constraints: mounting holes (stock NPTH footprints) at fixed
    poses, hard edge locks (``edges``: ref -> edge, 1 mm tolerance), locked rotations,
    a placement keep-out rectangle, and a board-region restriction (``region``: refs
    and rectangle; the engine's hard ``region``, each courtyard inside the rectangle)."""
    spec = deepcopy(spec)
    for ref in holes:
        spec["parts"].append(pinned(ref, "hole_m2", "M2", {"": ""}))
    spec["expected_components"] = len(spec["parts"])
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    cons = spec["constraints"]
    for ref, at in holes.items():
        cons["fixed"][ref] = dict(at=at, rot=0, side="top")
    cons["edge_align"] = {
        ref: dict(edge=edge, hard=True, tolerance_mm=1.0) for ref, edge in edges.items()
    }
    cons["orientation"] = dict(rotations)
    x0, y0, x1, y1 = keepout
    cons["keepout"] = [dict(name="label", polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]])]
    refs, rect = region
    cons["region"] = [dict(name="region", refs=list(refs), rect=list(rect), hard=True)]
    checks = base_checks(spec)
    checks += [
        dict(id="edge-" + ref, kind="edge", ref=ref, edge=edge, max_mm=1.0, engine="edge_align")
        for ref, edge in edges.items()
    ]
    checks += [
        dict(id="rot-" + ref, kind="orientation", ref=ref, rot=rot, engine="orientation")
        for ref, rot in rotations.items()
    ]
    checks.append(
        dict(id="keepout-label", kind="keepout", rect=keepout, refs="*", engine="keepout")
    )
    checks.append(dict(id="region", kind="region", refs=refs, rect=rect, engine="region"))
    spec["checks"] = checks
    spec["description"] += " " + describe
    spec["name"] += "-abs"
    spec["dims"]["constraints"] = "absolute"
    spec["features"] = sorted(set(spec["features"]) | {"absolute-constraints"})
    return spec


def relative(spec, *, lines, groups, align, describe):
    """Relative placement constraints: ordered lines at a pitch (``line_group``: a
    rigid row, which also fixes the members' order and their offsets), hard proximity
    groups (members within a radius of an anchor), and an alignment (``align``: refs
    sharing one coordinate within 0.25 mm; the engine's hard ``align`` on the
    footprint origins, as the check measures)."""
    spec = deepcopy(spec)
    cons = spec["constraints"]
    cons["line_group"] = [
        dict(name=name, members=members, pitch_mm=pitch, rot=90, reason=reason)
        for name, members, pitch, reason in lines
    ]
    cons["group"] = [
        dict(members=members, anchor=anchor, hard=True, radius_mm=radius)
        for _name, anchor, members, radius in groups
    ]
    refs, axis = align
    cons["align"] = [
        dict(name="align", refs=list(refs), axis=axis, anchor="origin", tol_mm=0.25, hard=True)
    ]
    checks = base_checks(spec)
    checks += [
        dict(
            id="line-" + name,
            kind="line",
            refs=members,
            pitch_mm=pitch,
            rot=90,
            tol_mm=0.01,
            engine="line_group",
        )
        for name, members, pitch, _reason in lines
    ]
    checks += [
        dict(
            id="near-" + name,
            kind="proximity",
            anchor=anchor,
            refs=members,
            max_mm=radius,
            engine="group",
        )
        for name, anchor, members, radius in groups
    ]
    checks.append(dict(id="align", kind="align", refs=refs, axis=axis, tol_mm=0.25, engine="align"))
    spec["checks"] = checks
    spec["description"] += " " + describe
    spec["name"] += "-rel"
    spec["dims"]["constraints"] = "relative"
    spec["features"] = sorted(set(spec["features"]) | {"relative-constraints"})
    return spec


def sidelock(spec, bottom, describe):
    """A designer's side assignment: ``bottom`` locked to the bottom side (hard
    ``side``), every other part on top."""
    spec = deepcopy(spec)
    spec["constraints"]["side"] = {"bottom": list(bottom)}
    spec["sides"] = "assigned"
    top = [p["ref"] for p in spec["parts"] if p["ref"] not in bottom]
    checks = [c for c in base_checks(spec) if c["id"] != "single-sided"]
    checks += [
        dict(id="side-bottom", kind="side", refs=list(bottom), side="bottom", engine="side"),
        dict(id="side-top", kind="side", refs=top, side="top", engine="side"),
    ]
    spec["checks"] = checks
    spec["description"] += " " + describe
    spec["name"] += "-sidelock"
    spec["dims"]["sides"] = "assigned"
    spec["features"] = sorted(set(spec["features"]) | {"side-lock", "bottom-side"})
    return spec


def mcu_absolute(spec):
    # Pin rows along their edge: the connector's pads run along x at rot 0, so rot 90
    # turns them along the east edge with the side-entry mouth facing out; the buttons'
    # pads run along x (designs.PAD_AXIS).
    spec = absolute(
        spec,
        holes=MCU_HOLES,
        edges={"J2": "east", "SW1": "south", "SW2": "south"},
        rotations={"J2": 90, "SW1": 0, "SW2": 0, "U1": 0},
        keepout=MCU_KEEPOUT,
        region=(["U2", "C10", "C11"], MCU_REGION),
        describe=(
            "Absolute constraints: M2 holes at two corners, the I/O connector on the east "
            "edge and both buttons on the south edge, the MCU's rotation locked, a label "
            "keep-out on the north edge, the regulator in the south-west quarter."
        ),
    )
    # absolute() rebuilds checks from base_checks(): restore the family's pair_bridge
    # check (R1/R2 are not among this variant's own absolute constraints).
    spec["checks"].append(MCU_PAIR_BRIDGE_CHECK)
    return spec


def mcu_relative(spec):
    spec = relative(
        spec,
        lines=[
            ("status-leds", ["D1", "D2", "D3", "D4"], 3.0, "Status LEDs in one ordered row"),
            ("usb-series", ["R1", "R2"], 2.5, "USB series resistors side by side"),
        ],
        groups=[
            ("crystal", "U1", ["Y1", "C8", "C9"], MCU_NEAR["crystal"]),
            ("decoupling", "U1", ["C2", "C3", "C4", "C5"], MCU_NEAR["decoupling"]),
        ],
        align=(["SW1", "SW2"], "y"),
        describe=(
            "Relative constraints: the LEDs in an ordered 3 mm line, the USB series "
            "resistors as a rigid pair, the crystal group within 13 mm and the decoupling "
            "within 12 mm of the MCU, the two buttons aligned."
        ),
    )
    # relative() already holds R1/R2 in its own explicit "usb-series" line_group
    # (at a hand-picked 2.5 mm pitch, demonstrating the authored line_group API);
    # the pair_bridge check still verifies that placement geometrically.
    spec["checks"].append(MCU_PAIR_BRIDGE_CHECK)
    return spec


def mcu_sidelock(spec):
    spec = sidelock(
        spec,
        ["C2", "C3", "C4", "C5", "C6", "C7"],
        "The MCU's decoupling, UCAP and AREF capacitors are locked to the bottom side.",
    )
    spec["checks"].append(MCU_PAIR_BRIDGE_CHECK)
    return spec


def mcu_header(spec):
    """The MCU board with the I/O on an Arduino-style 2.54 mm 1x08 through-hole pin
    header instead of the SMD connector (same nets). The header's footprint origin is
    pin 1, at one end of the part."""
    spec = deepcopy(spec)
    for p in spec["parts"]:
        if p["ref"] == "J2":
            p.update(pinned("J2", "header_1x08", "I/O", MCU_IO))
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    spec["name"] += "-header"
    spec["dims"]["parts"] = "tht-header"
    spec["features"] = sorted(set(spec["features"]) | {"tht-header", "off-centre-origin"})
    return spec


def mcu_mc(spec):
    """The same board placed by Monte-Carlo successive halving (``mc_case.py``):
    32 independent global starts ranked by the capacity proxy, the best 8 plus 2
    seeded controls screened by a short detailed route (2 iterations), and the 2 best
    screens routed with the ladder's own detailed-route budget (8 iterations); the best
    routed candidate (fewest missing connections, then vias, then copper) is kept."""
    spec = deepcopy(spec)
    spec["name"] += "-mc"
    spec["driver"] = "mc"
    spec["mc"] = dict(
        n0=32, k1=8, control=2, k2=2, iters=350, route_iters=2, final_iters=8, procs=4
    )
    spec["dims"]["search"] = "mc"
    spec["features"] = sorted(set(spec["features"]) | {"monte-carlo", "successive-halving"})
    return spec


# ------------------------------------------------------------- quad bank (hier)


def quad_bank():
    """hier-twin-bank-32 grown to four identical CD4017B banks: five blocks (one
    clock, four instances of one bank template), placed and routed hierarchically."""

    def at(address, spec):
        return dict(spec, address=address)

    p = [
        at("top.j1", part("J1", "connector", "5V input", ["VCC", "GND"])),
        at("top.c_bulk", part("C4", "capacitor", "10u", ["VCC", "GND"])),
    ]
    clock = timer_parts()[1:]  # U1, R1, R2, C1, C2, C3; the 555's RESET is tied to VCC
    for local, item in zip(["u", "r1", "r2", "c1", "c2", "c3"], clock):
        p.append(at("top.clock." + local, item))
    # CD4017 pin1..16: Q5,Q1,Q0,Q2,Q6,Q7,Q3,GND,Q8,Q4,Q9,CO,CE,CLK,RESET,VDD (as in chaser(5)).
    qpins = {0: 3, 1: 2, 2: 4, 3: 7, 4: 10, 5: 1}
    for index, bank in enumerate("ABCD"):
        prefix = "top.bank_" + bank.lower()
        pins = [""] * 16
        for i in range(5):
            pins[qpins[i] - 1] = "Q%s%d" % (bank, i)
        pins[qpins[5] - 1] = "RESET" + bank
        for pin, net in {8: "GND", 13: "GND", 14: "CLOCK", 15: "RESET" + bank, 16: "VCC"}.items():
            pins[pin - 1] = net
        p.append(at(prefix + ".u", part("U%d" % (index + 2), "counter", "CD4017B", pins)))
        p.append(at(prefix + ".c", part("C%d" % (index + 5), "capacitor", "100n", ["VCC", "GND"])))
        for i in range(5):
            led = "LED%s%d" % (bank, i)
            r, d = 3 + 5 * index + i, 1 + 5 * index + i
            p.append(
                at(
                    "%s.r%d" % (prefix, i),
                    part("R%d" % r, "resistor", "2.2k", ["Q%s%d" % (bank, i), led]),
                )
            )
            p.append(at("%s.d%d" % (prefix, i), part("D%d" % d, "led", "red", ["GND", led])))
    spec = circuit(
        "10-quad-bank-%d" % len(p),
        "TLC555 clock and four identical CD4017B five-LED banks as five blocks (two "
        "templates, one reused four times), placed and routed hierarchically.",
        p,
        (72, 50),
    )
    spec["driver"] = "hier"
    spec["hier"] = dict(
        utilisations=[0.3, 0.4],
        aspects=[1.5, 1 / 1.5],
        trial_seeds=[0, 1],
        block_iters=350,
        route_pitch_mm=0.25,
        route_iters=8,
        top_seeds=4,
        top_iters=350,
        representative_retries=3,
    )
    return hard(
        spec,
        "quad-bank-56",
        ["hierarchical", "template-reuse", "macro-placement"],
        "manual",
        60,
    )


# --------------------------------------------------------------- power switch


def power_switch():
    """A four-channel 12 V low-side load switch with current-sized copper.

    Screw-terminal input, PTC fuse, TVS, bulk and ceramic input capacitors, a 5 V LDO
    (MCP1703A-5002, SOT-23) for the control header, a power LED, and per channel an
    AO3400A N-MOSFET (SOT-23), gate and pull-down resistors, a flyback Schottky and a
    two-pin output. Net classes carry currents: the 12 V trunk and the return 3 A
    (20 C rise), each output 1.5 A (10 C rise); the engine sizes those widths
    (IPC-2221, 1 oz outer copper), writeback stamps them into the net classes and the
    runner's width audit rejects narrower copper.
    """
    p = [
        pinned("J1", "terminal_2p", "12V in", ["VIN", "GND"]),
        pinned("F1", "fuse_1206", "3A PTC", ["VIN", "VP"]),
        # D_SMA: pad 1 cathode. SMAJ15A unidirectional TVS from VP to ground.
        pinned("D1", "d_sma", "SMAJ15A", ["VP", "GND"]),
        pinned("C1", "cp_6x7", "100u 25V", ["VP", "GND"]),
        pinned("C2", "c_1206", "10u 25V", ["VP", "GND"]),
        # MCP1703A-5002 SOT-23A (Microchip DS20005122): 1 GND, 2 VOUT, 3 VIN.
        pinned("U1", "sot23", "MCP1703A-5002", ["GND", "V5", "VP"]),
        pinned("C3", "capacitor", "1u 25V", ["VP", "GND"]),
        pinned("C4", "capacitor", "1u", ["V5", "GND"]),
        pinned("R1", "resistor", "1k", ["V5", "PWR_A"]),
        pinned("D2", "led", "green", ["GND", "PWR_A"]),
        pinned("J2", "header_1x06", "Control", ["PWM1", "PWM2", "PWM3", "PWM4", "V5", "GND"]),
    ]
    for i in range(1, 5):
        p += [
            # AO3400A (Alpha & Omega datasheet; KiCad Transistor_FET:AO3400A): 1 G, 2 S, 3 D.
            pinned("Q%d" % i, "sot23", "AO3400A", ["G%d" % i, "GND", "OUT%d" % i]),
            pinned("R%d" % (i + 1), "resistor", "100", ["PWM%d" % i, "G%d" % i]),
            pinned("R%d" % (i + 5), "resistor", "10k", ["G%d" % i, "GND"]),
            # SS14 Schottky, pad 1 cathode: clamps the load's flyback to VP.
            pinned("D%d" % (i + 2), "d_sma", "SS14", ["VP", "OUT%d" % i]),
            pinned("J%d" % (i + 2), "connector", "Load %d" % i, ["VP", "OUT%d" % i]),
        ]
    size = (50, 40)
    spec = circuit(
        "11-power-switch-%d" % len(p),
        "Four-channel 12 V low-side MOSFET load switch with a 3 A trunk and return, 1.5 A "
        "outputs and current-sized copper.",
        p,
        size,
    )
    cons = spec["constraints"]
    # The terminal block's wire entry faces west (rot 90 turns its pin row along y).
    # Its origin is pin 1 and its body overhangs the pins: this pose keeps the courtyard on
    # the board (west edge 0.25 mm) with the two pins centred on the edge.
    cons["fixed"] = {"J1": dict(at=[6.0, size[1] / 2 - 2.54], rot=90, side="top")}
    cons["net_class"] = {
        "trunk": dict(nets=["VIN", "VP"], current_a=3.0, copper_oz=1, delta_t_c=20),
        "return": dict(nets=["GND"], current_a=3.0, copper_oz=1, delta_t_c=20),
        "load": dict(
            nets=["OUT1", "OUT2", "OUT3", "OUT4"], current_a=1.5, copper_oz=1, delta_t_c=10
        ),
        "logic_supply": dict(nets=["V5"], width_mm=0.3, current_a=0.1),
    }
    spec["supply"] = dict(voltage_v=12, max_current_a=3.0)
    return hard(
        spec,
        "power-switch-31",
        ["current-widths", "power"],
        "nightly",
        30,
    )


# ----------------------------------------------------------- chaser variants

CHASER_SIZE = (42, 32)  # chaser(5)'s outline
CHASER_HOLES = {"H1": [3.0, CHASER_SIZE[1] - 3.0], "H2": [CHASER_SIZE[0] - 3.0, 3.0]}
CHASER_KEEPOUT = [
    CHASER_SIZE[0] / 2 - 6,
    CHASER_SIZE[1] - 5,
    CHASER_SIZE[0] / 2 + 6,
    CHASER_SIZE[1],
]
CHASER_REGION = [0.0, 0.0, CHASER_SIZE[0] / 2, CHASER_SIZE[1]]  # the clock: west half
CHASER_NEAR = dict(timer=8.0, counter=8.0)


def chaser_base():
    """The ladder's 07-chaser-20 as a hard-rung base: same netlist, rules and fixed
    connector, with the tool-neutral dimensions and checks attached."""
    spec = chaser(5)
    spec["name"] = "07-chaser-20"
    return hard(spec, "chaser-20", ["2-layer"], "nightly", 20)


def chaser_absolute(spec):
    return absolute(
        spec,
        holes=CHASER_HOLES,
        edges={"D%d" % i: "south" for i in range(1, 6)},
        rotations={"D%d" % i: 0 for i in range(1, 6)} | {"U1": 0, "U2": 0},
        keepout=CHASER_KEEPOUT,
        region=(["U1", "R1", "R2", "C1", "C2", "C3"], CHASER_REGION),
        describe=(
            "Absolute constraints: M2 holes at two corners, the five LEDs on the south edge, "
            "both ICs' rotations locked, a label keep-out on the north edge, the clock parts "
            "in the west half."
        ),
    )


def chaser_relative(spec):
    return relative(
        spec,
        lines=[
            ("leds", ["D1", "D2", "D3", "D4", "D5"], 3.0, "Chaser LEDs in one ordered row"),
            ("timing", ["R1", "R2"], 2.5, "Timing resistors side by side"),
        ],
        groups=[
            ("timer", "U1", ["C1", "C2", "C3"], CHASER_NEAR["timer"]),
            ("counter", "U2", ["C4"], CHASER_NEAR["counter"]),
        ],
        align=(["U1", "U2"], "y"),
        describe=(
            "Relative constraints: the LEDs in an ordered 3 mm line, the timing resistors as "
            "a rigid pair, the timer's capacitors within 8 mm of the timer and the counter's "
            "decoupling within 8 mm of the counter, the two ICs aligned."
        ),
    )


def chaser_sidelock(spec):
    return sidelock(
        spec, ["C3", "C4", "C5"], "The supply capacitors are locked to the bottom side."
    )


# ------------------------------------------------------ fixed copper (arcs)

# A fixed block on the chaser (07-chaser-20-4L-SGPS-arcblock): two matched meander
# delay lines carrying the clock to two U.FL monitor outputs (Hirose U.FL-R-SMT-1, the
# stock footprint: pad 1 signal, both pads 2 ground), 64 locked arcs, a ground rail of
# fence vias between them and ground vias at the outer ground pads, all one KiCad group.
# The engine holds the connectors out of placement and joins the clock to each line's
# free west end (its port). A copper keep-out over the block bars foreign tracks on
# F.Cu and vias (In1.Cu listed) but not In2.Cu or B.Cu tracks; a 2 mm class guard
# around it lets only the planes' nets and the clock cross on the signal layers.
ARC_GROUP = "DELAY_LINES"
ARC_NAME = "delay"
ARC_LINES = (27.6, 22.4)  # each line's baseline y; its U.FL sits on it at x = UFL_X
ARC_PORT_X, ARC_START_X, UFL_X = 24.8, 27.0, 39.7
ARC_R, ARC_GAP, ARC_H, ARC_PERIODS = 0.25, 0.15, 1.8, 8  # 4 quarter arcs per period
ARC_W, RAIL_W = 0.25, 0.4  # the clock's and the ground class's (plane_gnd) widths
ARC_VIA = (0.6, 0.3)  # the chaser fab's via
ARC_KEEPOUT = [26.3, 19.8, 41.75, 30.2]  # the block (F.Cu tracks, vias)
ARC_GUARD = [24.3, 17.8, 42.0, 32.0]  # 2 mm around it, clipped at the board edge
KICAD_PAGE_MM = 30.0  # the generator's outline offset (pnr.writeback frame_region)


def meander(net, y0):
    """``(tracks, arcs)`` of one line: the port lead from (ARC_PORT_X, y0), the
    rounded square-wave of ARC_PERIODS periods (four quarter arcs each), and the lead
    into its U.FL's pad 1."""
    r, g, h, w = ARC_R, ARC_GAP, ARC_H, ARC_W
    s = math.sqrt(0.5) * r
    tracks = [[net, "F.Cu", [ARC_PORT_X, y0], [ARC_START_X, y0], w]]
    arcs = []
    x = ARC_START_X
    for _ in range(ARC_PERIODS):
        up, top = (x + r, y0 + r), (x + r, y0 + h - r)
        a, b = (x + 2 * r, y0 + h), (x + 2 * r + g, y0 + h)
        down, low = (x + 3 * r + g, y0 + h - r), (x + 3 * r + g, y0 + r)
        nxt = x + 4 * r + g
        arcs.append([net, "F.Cu", [x, y0], [x + s, y0 + r - s], list(up), w])
        tracks.append([net, "F.Cu", list(up), list(top), w])
        arcs.append([net, "F.Cu", list(top), [x + 2 * r - s, y0 + h - r + s], list(a), w])
        tracks.append([net, "F.Cu", list(a), list(b), w])
        arcs.append([net, "F.Cu", list(b), [x + 2 * r + g + s, y0 + h - r + s], list(down), w])
        tracks.append([net, "F.Cu", list(down), list(low), w])
        arcs.append([net, "F.Cu", list(low), [nxt - s, y0 + r - s], [nxt, y0], w])
        tracks.append([net, "F.Cu", [nxt, y0], [nxt + g, y0], w])
        x = nxt + g
    tracks.append([net, "F.Cu", [x, y0], [UFL_X - 1.525, y0], w])
    return tracks, arcs


def arc_block():
    """The block: footprint poses, tracks, arcs and vias (engine mm, y up)."""
    tracks, arcs, vias = [], [], []
    for y0 in ARC_LINES:
        t, a = meander("CLOCK", y0)
        tracks += t
        arcs += a
    top, bottom = ARC_LINES
    rail_y = (top + bottom) / 2
    fence = [ARC_START_X + 0.6 + 1.3 * k for k in range(8)]
    vias += [["GND", [x, rail_y]] for x in fence]
    # The rail joins the fence vias and the U.FLs' inner ground pads.
    tracks.append(["GND", "F.Cu", [fence[0], rail_y], [UFL_X, rail_y], RAIL_W])
    tracks.append(["GND", "F.Cu", [UFL_X, top - 1.475], [UFL_X, bottom + 1.475], RAIL_W])
    for y in (top + 1.475, bottom - 1.475):  # the outer ground pads to their own vias
        tracks.append(["GND", "F.Cu", [UFL_X, y], [UFL_X + 1.4, y], RAIL_W])
        vias.append(["GND", [UFL_X + 1.4, y]])
    return dict(
        name=ARC_NAME,
        group=ARC_GROUP,
        footprints={"J10": [UFL_X, top, 0], "J11": [UFL_X, bottom, 0]},
        tracks=tracks,
        arcs=arcs,
        vias=[[net, xy, ARC_VIA[0], ARC_VIA[1]] for net, xy in vias],
    )


def block_sha256(block, height, offset=KICAD_PAGE_MM):
    """The copper digest of a generated block (``pnr.fixed_copper.block_digest`` of
    its group without an anchor: KiCad nanometres from the outline's lower-left
    corner, y down), computed from the generator's own coordinates."""
    import hashlib

    ox, oy = round(offset * 1e6), round((offset + height) * 1e6)

    def local(p):
        return "%d %d" % (
            round((offset + p[0]) * 1e6) - ox,
            round((offset + height - p[1]) * 1e6) - oy,
        )

    rows = []
    for net, layer, a, b, w in block["tracks"]:
        rows.append("|".join(["segment", local(a), local(b), "w %d" % round(w * 1e6), layer, net]))
    for net, layer, a, m, b, w in block["arcs"]:
        rows.append(
            "|".join(["arc", local(a), local(m), local(b), "w %d" % round(w * 1e6), layer, net])
        )
    for net, xy, d, drill in block["vias"]:
        rows.append(
            "|".join(
                [
                    "via",
                    local(xy),
                    "d %d" % round(d * 1e6),
                    "drill %d" % round(drill * 1e6),
                    "type through",
                    "F.Cu-B.Cu",
                    net,
                ]
            )
        )
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def rect_polygon(rect):
    x0, y0, x1, y1 = rect
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def chaser_arcblock(spec):
    """``spec`` (the 4L-SGPS chaser) with the fixed block: its parts, the block, the
    constraints that express it to the engine (``fixed_block``, ``copper_keepout``
    v1, a placement ``keepout``) and the tool-neutral checks (``copper_digest``,
    ``no_copper``)."""
    spec = deepcopy(spec)
    block = arc_block()
    block["sha256"] = block_sha256(block, spec["constraints"]["board"]["outline"]["h"])
    spec["fixed_block"] = block
    for ref in sorted(block["footprints"]):
        spec["parts"].append(pinned(ref, "u_fl", "U.FL clock monitor", {"1": "CLOCK", "2": "GND"}))
    spec["expected_components"] = len(spec["parts"])
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    cons = spec["constraints"]
    cons["fixed_block"] = [
        dict(
            name=ARC_NAME, group=ARC_GROUP, sha256=block["sha256"], refs=sorted(block["footprints"])
        )
    ]
    planes = sorted(k for k, c in cons["net_class"].items() if c.get("plane_layer"))
    x0, y0, x1, y1 = ARC_KEEPOUT
    gx0, gy0, gx1, gy1 = ARC_GUARD
    guards = {
        "guard-west": [gx0, gy0, x0, gy1],
        "guard-south": [x0, gy0, gx1, y0],
        "guard-north": [x0, y1, gx1, gy1],
    }
    cons["copper_keepout"] = [
        dict(
            name="block",
            rect=list(ARC_KEEPOUT),
            layers=["F.Cu", "In1.Cu"],
            items=["tracks", "vias"],
            exempt_groups=[ARC_GROUP],
        )
    ] + [
        dict(
            name=name,
            rect=rect,
            layers=["F.Cu", "B.Cu"],
            items=["tracks", "vias"],
            allow_classes=planes,
            allow_nets=["CLOCK"],
        )
        for name, rect in guards.items()
    ]
    cons["keepout"] = (cons.get("keepout") or []) + [
        dict(name="block", polygon=rect_polygon(ARC_GUARD))
    ]
    spec["checks"].append(
        dict(
            id="block-copper",
            kind="copper_digest",
            group=ARC_GROUP,
            sha256=block["sha256"],
            engine="fixed_block",
        )
    )
    spec["checks"].append(
        dict(
            id="keepout-block",
            kind="no_copper",
            polygon=rect_polygon(ARC_KEEPOUT),
            layers=["F.Cu", "In1.Cu"],
            items=["tracks", "vias", "pads"],
            allow_nets=[],
            exempt_groups=[ARC_GROUP],
            engine="copper_keepout",
        )
    )
    allowed = sorted({n for k in planes for n in cons["net_class"][k]["nets"]} | {"CLOCK"})
    for name, rect in guards.items():
        spec["checks"].append(
            dict(
                id="keepout-" + name,
                kind="no_copper",
                polygon=rect_polygon(rect),
                layers=["F.Cu", "B.Cu"],
                items=["tracks", "vias", "pads"],
                allow_nets=allowed,
                exempt_groups=[ARC_GROUP],
                engine="copper_keepout",
            )
        )
    spec["name"] += "-arcblock"
    spec["description"] = (
        spec.get("description", "")
        + " With a fixed block: two matched meander delay lines (64 locked arcs) from the "
        "clock to two U.FL monitor outputs, a fenced ground rail, a copper keep-out over "
        "it and a 2 mm class guard."
    )
    spec["dims"]["parts"] = "arcblock"
    spec["features"] = sorted(set(spec["features"]) | {"fixed-copper", "arcs", "copper-keepout"})
    spec["ci"] = dict(lane="nightly", minutes=30)
    return spec


# ------------------------------------------------------------ BGA fanout

# STM32F207IGH6 in UFBGA176+25 (ST DS6329, "UFBGA176+25 ballout"), the device KiCad's
# stock footprint Package_BGA:UFBGA-201_10x10mm_Layout15x15_P0.65mm is drawn for: the
# ball map of KiCad's stock symbol MCU_ST_STM32F2:STM32F207IGHx (generated from ST's
# open pin data), row by row, columns 1-15; "-" is a vacant site (ring 4 and the centre
# block's surround). The 25 centre balls are VSS.
STM32F207_UFBGA176 = {
    "A": "PE3 PE2 PE1 PE0 PB8 PB5 PG14 PG13 PB4 PB3 PD7 PC12 PA15 PA14 PA13",
    "B": "PE4 PE5 PE6 PB9 PB7 PB6 PG15 PG12 PG11 PG10 PD6 PD0 PC11 PC10 PA12",
    "C": "VBAT PI7 PI6 PI5 VDD RFU VDD VDD VDD PG9 PD5 PD1 PI3 PI2 PA11",
    "D": "PC13 PI8 PI9 PI4 VSS BOOT0 VSS VSS VSS PD4 PD3 PD2 PH15 PI1 PA10",
    "E": "PC14 PF0 PI10 PI11 - - - - - - - PH13 PH14 PI0 PA9",
    "F": "PC15 VSS VDD PH2 - VSS VSS VSS VSS VSS - VSS VCAP_2 PC9 PA8",
    "G": "PH0 VSS VDD PH3 - VSS VSS VSS VSS VSS - VSS VDD PC8 PC7",
    "H": "PH1 PF2 PF1 PH4 - VSS VSS VSS VSS VSS - VSS VDD PG8 PC6",
    "J": "NRST PF3 PF4 PH5 - VSS VSS VSS VSS VSS - VDD VDD PG7 PG6",
    "K": "PF7 PF6 PF5 VDD - VSS VSS VSS VSS VSS - PH12 PG5 PG4 PG3",
    "L": "PF10 PF9 PF8 REGOFF - - - - - - - PH11 PH10 PD15 PG2",
    "M": "VSSA PC0 PC1 PC2 PC3 PB2 PG1 VSS VSS VCAP_1 PH6 PH8 PH9 PD14 PD13",
    "N": "VREF- PA1 PA0 PA4 PC4 PF13 PG0 VDD VDD VDD PE13 PH7 PD12 PD11 PD10",
    "P": "VREF+ PA2 PA6 PA5 PC5 PF12 PF15 PE8 PE9 PE11 PE14 PB12 PB13 PD9 PD8",
    "R": "VDDA PA3 PA7 PB1 PB0 PF11 PF14 PE7 PE10 PE12 PE15 PB10 PB11 PB14 PB15",
}
BGA_ROWS = "ABCDEFGHJKLMNPR"
BGA_SIZE = (36, 36)
# Three east-edge balls left for a small fixed block (an RF launch, say): unconnected
# here, their surface exits a reserved corridor in U1's frame (x east, y north).
BGA_RESERVED_BALLS = ["E15", "F15", "G15"]
BGA_RESERVED_RECT = [4.8, 0.3, 6.4, 2.3]


def _bga_ring(ball):
    r, c = BGA_ROWS.index(ball[0]), int(ball[1:]) - 1
    return r, c, min(r, c, 14 - r, 14 - c)


def bga_signals():
    """``{side: [balls]}``: three GPIO balls per ring 0-3 on each side (fewer where a
    ring has fewer), each ball on the side of its nearest package edge (corners and
    ties left out), spread along the edge."""
    out = {}
    for side in ("north", "south", "west", "east"):
        rings = {k: [] for k in range(4)}
        for row, names in STM32F207_UFBGA176.items():
            for col, name in enumerate(names.split()):
                ball = row + str(col + 1)
                if ball in BGA_RESERVED_BALLS or not re.fullmatch(r"P[A-I]\d+", name):
                    continue
                r, c, k = _bga_ring(ball)
                if k > 3:
                    continue
                edge = dict(north=r, south=14 - r, west=c, east=14 - c)
                if [e for e in edge if edge[e] == min(edge.values())] != [side]:
                    continue
                rings[k].append((c if side in ("north", "south") else r, ball))
        balls = []
        for k in range(4):
            ring = sorted(rings[k])
            n = len(ring)
            picks = [round((i + 0.5) * n / 3 - 0.5) for i in range(3)] if n >= 3 else range(n)
            balls += [ring[i][1] for i in picks]
        out[side] = balls
    return out


def ufbga_base():
    """A 0.65 mm UFBGA breakout: the 2-layer base of ``11-ufbga201-fanout``.

    STM32F207IGH6 (UFBGA176+25) fixed at the centre; 47 GPIO balls from rings 0-3 run
    to four 12-pin JST SH connectors fixed at the four edges (each its own side's
    balls, in edge order); decoupling (six 100 nF, a 4.7 uF bulk), the VCAP, NRST and
    BOOT0 parts, and a 2-pin supply header. VDD, VDDA, VREF+ and VBAT are VCC; VSS,
    VSSA, VREF- and REGOFF (regulator on) are GND."""
    nets = {}
    for row, names in STM32F207_UFBGA176.items():
        for col, name in enumerate(names.split()):
            if name == "-":
                continue
            ball = row + str(col + 1)
            net = ""
            if name in ("VDD", "VDDA", "VREF+", "VBAT"):
                net = "VCC"
            elif name in ("VSS", "VSSA", "VREF-", "REGOFF"):
                net = "GND"
            elif name in ("VCAP_1", "VCAP_2", "NRST", "BOOT0"):
                net = name
            nets[ball] = net
    sides = bga_signals()
    for balls in sides.values():
        for ball in balls:
            r, c, _ = _bga_ring(ball)
            nets[ball] = STM32F207_UFBGA176[ball[0]].split()[c]
    parts = [pinned("U1", "ufbga201", "STM32F207IGH6", nets)]
    order = dict(north=1, south=-1, west=1, east=-1)  # pin 1 first along the edge at its pose
    for k, side in enumerate(("north", "south", "west", "east")):
        balls = sorted(
            sides[side],
            key=lambda b: order[side]
            * (int(b[1:]) if side in ("north", "south") else -BGA_ROWS.index(b[0])),
        )
        pins = {str(i + 1): "" for i in range(12)}
        for i, ball in enumerate(balls):
            pins[str(i + 1)] = nets[ball]
        pins["MP"] = ""
        parts.append(pinned("J%d" % (k + 1), "jst_sh_12", "SM12B-SRSS-TB", pins))
    parts.append(part("J5", "connector", "3V3 input", ["VCC", "GND"]))
    for i in range(1, 7):
        parts.append(pinned("C%d" % i, "c_0402", "100n", ["VCC", "GND"]))
    parts += [
        pinned("C7", "capacitor", "4.7u", ["VCC", "GND"]),
        pinned("C8", "c_0402", "2.2u", ["VCAP_1", "GND"]),
        pinned("C9", "c_0402", "2.2u", ["VCAP_2", "GND"]),
        pinned("C10", "c_0402", "100n", ["NRST", "GND"]),
        pinned("R1", "r_0402", "10k", ["BOOT0", "GND"]),
    ]
    spec = circuit(
        "11-ufbga201-fanout",
        "STM32F207 in a 0.65 mm UFBGA176+25 broken out to four connectors: 47 GPIO balls "
        "from rings 0-3, the ground and supply balls dropped to their planes.",
        parts,
        BGA_SIZE,
    )
    cons = spec["constraints"]
    cons["board"]["default_clearance_mm"] = 0.2
    # Fine-pitch rules: 0.10/0.10 mm tracks, 0.40/0.20 mm vias, 0.15 mm minimum drill.
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
        "supply": dict(nets=["VCC"], width_mm=0.1),
        "return": dict(nets=["GND"], width_mm=0.1),
    }
    w, h = BGA_SIZE
    cons["fixed"] = {
        "U1": dict(at=[w / 2, h / 2], rot=0, side="top"),
        "J1": dict(at=[w / 2, h - 3.6], rot=0, side="top"),
        "J2": dict(at=[w / 2, 3.6], rot=180, side="top"),
        "J3": dict(at=[3.6, h / 2], rot=90, side="top"),
        "J4": dict(at=[w - 3.6, h / 2], rot=270, side="top"),
        "J5": dict(at=[2.5, 5.5], rot=0, side="top"),
    }
    # Escape every ball of U1: ground and supply drops 0.35/0.15 at interstitial sites,
    # signals by the 0.40/0.20 class; no surface exit north (an antenna edge, say).
    cons["fanout"] = [
        dict(
            name="u1",
            ref="U1",
            via_classes=dict(
                planes=dict(
                    diameter_mm=0.35, drill_mm=0.15, nets=["GND", "VCC"], sites=["interstitial"]
                ),
                default=dict(diameter_mm=0.4, drill_mm=0.2, sites=["vacant", "outside"]),
            ),
            surface_rings=2,
            forbidden_exits={"F.Cu": ["north"]},
            reserved=[dict(name="block", rect=BGA_RESERVED_RECT, layers=["F.Cu"])],
        )
    ]
    spec["supply"] = dict(voltage_v=3.3, max_current_a=0.1)
    spec = hard(spec, "ufbga201-fanout", ["bga-fanout", "fine-pitch"], "manual", 120)
    signals = sorted(b for balls in sides.values() for b in balls)
    spec["checks"] += [
        dict(
            id="fanout-plane-vias",
            kind="via_class",
            ref="U1",
            nets=["GND", "VCC"],
            diameter_mm=0.35,
            drill_mm=0.15,
            site="interstitial",
            engine="fanout",
        ),
        dict(id="fanout-escape", kind="escape", ref="U1", pads=signals, engine="fanout"),
    ]
    return spec


def ufbga_fanout():
    """``11-ufbga201-fanout``: the base on six layers (S G S G P S), where the drops reach
    the ground (In1, In3) and supply (In4) planes."""
    return with_stackup(ufbga_base(), "6L-SGSGPS")


# A fixed block on the BGA rung (11-ufbga201-fanout-6L-SGSGPS-block): an RF launch from
# ball F15 to a U.FL connector east of the array, all one KiCad group: the feed, two
# ground stitching vias on the array's interstitial lattice beside F15 (sites the ground
# drops of E15 and G15 may also choose: the plan reuses them, never drills them again),
# a ground fence along the feed and the connector's ground vias. A copper keep-out over
# the launch bars every net but ground (its plane class) on F.Cu, In2.Cu and the
# supply plane In4.Cu (tracks, vias and pours: the engine cuts its VCC plane out of it);
# a class guard on F.Cu north and south of it lets only the plane nets through; a rule
# area in the group bars tracks on F.Cu alone north-east of the array (inner-layer exits
# stay open below it).
LAUNCH_GROUP = "LAUNCH"
LAUNCH_NAME = "launch"
LAUNCH_UFL = (27.0, 19.3)  # J6's centre: pad 1 at x - 1.525, ground pads at y +- 1.475
LAUNCH_BALL = (22.55, 19.3)  # F15 (U1 at the board centre, rot 0)
LAUNCH_KEEPOUT = [22.95, 18.3, 28.4, 20.3]
LAUNCH_GUARDS = {"guard-north": [22.95, 20.3, 29.0, 21.4], "guard-south": [22.95, 17.2, 29.0, 18.3]}
LAUNCH_RULE = [22.95, 21.4, 23.9, 22.6]  # F.Cu tracks only: rows A-B's surface exits east
LAUNCH_VIA = (0.35, 0.15)  # the fanout's plane class (via_class judges them inside U1)


def launch_block():
    """The launch: footprint pose, tracks, vias and rule areas (engine mm, y up)."""
    ux, uy = LAUNCH_UFL
    bx, by = LAUNCH_BALL
    tracks = [["RF_OUT", "F.Cu", [bx, by], [ux - 1.525, uy], 0.2]]
    vias = [["GND", [bx - 0.325, by + 0.325]], ["GND", [bx - 0.325, by - 0.325]]]
    vias += [["GND", [24.6, by + 0.55]], ["GND", [24.6, by - 0.55]]]
    vias += [["GND", [25.0, by + 1.0]], ["GND", [25.0, by - 1.0]]]
    for y in (uy + 1.475, uy - 1.475):  # the connector's ground pads to their own vias
        tracks.append(["GND", "F.Cu", [ux, y], [ux + 1.6, y], 0.4])
        vias.append(["GND", [ux + 1.6, y]])
    return dict(
        name=LAUNCH_NAME,
        group=LAUNCH_GROUP,
        footprints={"J6": [ux, uy, 0]},
        tracks=tracks,
        arcs=[],
        vias=[[net, xy, LAUNCH_VIA[0], LAUNCH_VIA[1]] for net, xy in vias],
        rule_areas=[
            dict(polygon=rect_polygon(LAUNCH_RULE), layers=["F.Cu"], tracks=True, vias=False)
        ],
    )


def ufbga_block(spec):
    """``spec`` (the 6L BGA rung) with the launch block: F15 drives it (RF_OUT), E15 and
    G15 become ground balls beside it, the fanout skips F15 instead of reserving the
    three balls' corridor, and the constraints and checks that express the block."""
    spec = deepcopy(spec)
    block = launch_block()
    block["sha256"] = block_sha256(block, spec["constraints"]["board"]["outline"]["h"])
    spec["fixed_block"] = block
    u1 = spec["parts"][0]
    u1["pins"].update(E15="GND", F15="RF_OUT", G15="GND")
    spec["parts"].append(pinned("J6", "u_fl", "U.FL RF out", {"1": "RF_OUT", "2": "GND"}))
    spec["expected_components"] = len(spec["parts"])
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    cons = spec["constraints"]
    cons["fixed_block"] = [
        dict(name=LAUNCH_NAME, group=LAUNCH_GROUP, sha256=block["sha256"], refs=["J6"])
    ]
    (fanout,) = cons["fanout"]
    fanout.pop("reserved")
    fanout["skip_pads"] = ["F15"]
    planes = sorted(k for k, c in cons["net_class"].items() if c.get("plane_layer"))
    ground = sorted(k for k in planes if "GND" in cons["net_class"][k]["nets"])
    cons["copper_keepout"] = [
        dict(
            name=LAUNCH_NAME,
            rect=list(LAUNCH_KEEPOUT),
            layers=["F.Cu", "In2.Cu", "In4.Cu"],
            allow_classes=ground,
            exempt_groups=[LAUNCH_GROUP],
        )
    ] + [
        dict(
            name=name,
            rect=rect,
            layers=["F.Cu"],
            items=["tracks", "vias"],
            allow_classes=planes,
            exempt_groups=[LAUNCH_GROUP],
        )
        for name, rect in sorted(LAUNCH_GUARDS.items())
    ]
    cons["keepout"] = (cons.get("keepout") or []) + [
        # The launch and the array's east exits beside it (J4's courtyard starts at 29.12).
        dict(name=LAUNCH_NAME, polygon=rect_polygon([24.1, 14.5, 29.0, 24.0]))
    ]
    spec["checks"] = [c for c in spec["checks"] if c["id"] != "fanout-escape"]
    signals = sorted(b for balls in bga_signals().values() for b in balls)
    spec["checks"] += [
        dict(id="fanout-escape", kind="escape", ref="U1", pads=signals, engine="fanout"),
        dict(
            id="block-copper",
            kind="copper_digest",
            group=LAUNCH_GROUP,
            sha256=block["sha256"],
            engine="fixed_block",
        ),
        dict(
            id="keepout-launch",
            kind="no_copper",
            polygon=rect_polygon(LAUNCH_KEEPOUT),
            layers=["F.Cu", "In2.Cu", "In4.Cu"],
            items=["tracks", "vias", "pads", "zones"],
            allow_nets=["GND"],
            exempt_groups=[LAUNCH_GROUP],
            engine="copper_keepout",
        ),
        dict(
            id="block-rule-area",
            kind="no_copper",
            polygon=rect_polygon(LAUNCH_RULE),
            layers=["F.Cu"],
            items=["tracks"],
            allow_nets=[],
            exempt_groups=[LAUNCH_GROUP],
            engine="fixed_block",
        ),
    ]
    for name, rect in sorted(LAUNCH_GUARDS.items()):
        spec["checks"].append(
            dict(
                id="keepout-" + name,
                kind="no_copper",
                polygon=rect_polygon(rect),
                layers=["F.Cu"],
                items=["tracks", "vias", "pads"],
                allow_nets=["GND", "VCC"],
                exempt_groups=[LAUNCH_GROUP],
                engine="copper_keepout",
            )
        )
    spec["name"] += "-block"
    spec["description"] = (
        spec.get("description", "")
        + " With a fixed block: an RF launch from F15 to a U.FL, ground stitching vias on "
        "the array's interstitial lattice, a class keep-out over it (the supply plane cut "
        "out), a class guard and an F.Cu-only rule area at the fanout's edge."
    )
    spec["dims"]["parts"] = "block"
    spec["features"] = sorted(set(spec["features"]) | {"fixed-copper", "copper-keepout"})
    return spec


# Net class clearances, a board's custom rules and its own outline on the BGA rung
# (11-ufbga201-fanout-6L-SGSGPS-classes): the supply plane class at 0.12 mm (its
# 0.35 mm interstitial drops leave 0.125 mm to the 0.32 mm balls around them) and two
# ring-0 south GPIO nets in a CLK class at 0.20 mm that the custom rules keep off
# vias and 0.25 mm from every net without a class; a 1 mm fiducial with its own
# 0.6 mm clearance and 0.5 mm mask margin in the south exits' corridor; an outline
# with 1 mm corner radii at a 0.15 mm stroke (J1-J4's pads within 1 mm of the edge),
# judged by a hole-to-edge rule written for that stroke. The engine declares
# board.class_clearance: maze, dru_routing and edge: exact.
CLASSES_CLK_BALLS = ["R4", "R8"]  # ring 0, south: PB1 and PE7, to J2
CLASSES_FIDUCIAL = (21.5, 9.0)  # between U1 (y >= 12.7) and J2 (y <= 6.9)
CLASSES_OUTLINE = dict(corner_radius_mm=1.0, stroke_mm=0.15)
CLASSES_CORNER_KEEPOUT = 1.1  # mm: past the 1 mm arc; J5 (fixed at 2.5, 5.5) starts at y 1.135
CLASSES_RULES = [
    '(rule "clk: no vias"\n  (constraint disallow via)\n'
    "  (condition \"A.Type == 'Via' && A.hasNetclass('clk')\"))",
    '(rule "clk to nets without a class"\n  (constraint clearance (min 0.25mm))\n'
    "  (condition \"A.hasNetclass('clk') && B.NetClass == 'Default'\"))",
    '(rule "hole to edge, to the 0.15 mm stroke"\n'
    "  (constraint physical_hole_clearance (min 0.425mm))\n"
    "  (condition \"A.Layer == 'Edge.Cuts' || B.Layer == 'Edge.Cuts'\"))",
]


def ufbga_classes(spec):
    """``spec`` (the 6L BGA rung) with net class clearances, custom rules, a fiducial
    and a rounded outline (above), and the checks that judge them."""
    spec = deepcopy(spec)
    u1 = spec["parts"][0]
    clk = [u1["pins"][ball] for ball in CLASSES_CLK_BALLS]
    spec["parts"].append(pinned("FID1", "fiducial_1mm", "Fiducial", {"": ""}))
    spec["expected_components"] = len(spec["parts"])
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    cons = spec["constraints"]
    cons["fixed"]["FID1"] = dict(at=list(CLASSES_FIDUCIAL), rot=0, side="top")
    cons["net_class"]["plane_vcc"]["clearance_mm"] = 0.12
    cons["net_class"]["clk"] = dict(nets=clk, clearance_mm=0.2)
    cons["board"].update(class_clearance="maze", dru_routing=True, edge="exact")
    spec["outline_shape"] = dict(CLASSES_OUTLINE)
    # The placer frames parts in the outline's rectangle: the rounded corners (and
    # the edge clearance past them) are kept free of parts by keep-outs.
    w, h = cons["board"]["outline"]["w"], cons["board"]["outline"]["h"]
    c = CLASSES_CORNER_KEEPOUT
    cons["keepout"] = (cons.get("keepout") or []) + [
        dict(name="corner-" + name, polygon=rect_polygon(rect))
        for name, rect in (
            ("sw", [0, 0, c, c]),
            ("se", [w - c, 0, w, c]),
            ("ne", [w - c, h - c, w, h]),
            ("nw", [0, h - c, c, h]),
        )
    ]
    spec["dru_rules"] = list(CLASSES_RULES)
    spec["checks"] += [
        dict(
            id="fixed-FID1",
            kind="fixed",
            ref="FID1",
            at=list(CLASSES_FIDUCIAL),
            rot=0,
            side="top",
            tol_mm=0.01,
            engine="fixed",
        ),
        dict(id="clk-no-vias", kind="net_vias", nets=clk, max=0, engine="dru_routing"),
    ]
    spec["name"] += "-classes"
    spec["description"] = (
        spec.get("description", "")
        + " With class clearances (supply 0.12 mm, two CLK nets 0.20 mm), custom rules (no "
        "vias on CLK, CLK 0.25 mm from unclassed nets, hole to edge to the stroke), a "
        "fiducial with its own clearance in an exit corridor and 1 mm rounded corners."
    )
    spec["dims"]["constraints"] = "classes"
    spec["features"] = sorted(
        set(spec["features"]) | {"class-clearance", "custom-rules", "board-edge", "pad-clearance"}
    )
    return spec


# Partial fanout and rails on the BGA rung (11-ufbga201-fanout-6L-SGSGPS-partial and
# -rails). Ball centres in U1's frame (x east, y north): column c at (c - 8) * 0.65,
# row r (A = 0) at (7 - r) * 0.65.


def _ball_xy(ball):
    r, c = BGA_ROWS.index(ball[0]), int(ball[1:])
    return ((c - 8) * 0.65, (7 - r) * 0.65)


def _square(centre, half):
    x, y = centre
    return [[x - half, y - half], [x + half, y - half], [x + half, y + half], [x - half, y + half]]


# C8 is a VCC ball between VCC balls C7 and C9: reserved squares on its four via
# sites, its north, south and east channels and the two diagonal steps out of its west
# channel leave it no drop (the planner would otherwise reach C7's via through that
# channel), and the bridge joins it to C7 straight through the channel. K4 is a VCC
# ball without a VCC neighbour: a copper keepout over it bars tracks and vias, so it
# stays open.
PARTIAL_BRIDGED = "C8"
PARTIAL_OPEN = "K4"


def ufbga_partial(spec):
    """``spec`` (the 6L BGA rung) with a partial fanout (``partial: {bridge: true}``):
    one supply ball bridged to its neighbour, one left open while its net (the VCC
    plane) is whole."""
    spec = deepcopy(spec)
    cons = spec["constraints"]
    (fanout,) = cons["fanout"]
    fanout["partial"] = dict(bridge=True)
    x, y = _ball_xy(PARTIAL_BRIDGED)
    h = 0.325
    sites = [(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)]
    sites += [(x, y + h), (x, y - h), (x + h, y)]  # north, south, east channels
    steps = [(x - 1.5 * h, y + h / 2), (x - 1.5 * h, y - h / 2)]  # west channel's diagonals
    fanout["reserved"] = (
        list(fanout.get("reserved") or [])
        + [
            dict(name="c8-%d" % k, polygon=_square(p, 0.08), layers=["*"])
            for k, p in enumerate(sites)
        ]
        + [
            dict(name="c8-%d" % (len(sites) + k), polygon=_square(p, 0.05), layers=["*"])
            for k, p in enumerate(steps)
        ]
    )
    w, hh = BGA_SIZE
    bx, by = _ball_xy(PARTIAL_OPEN)
    keep = [w / 2 + bx - 0.3, hh / 2 + by - 0.3, w / 2 + bx + 0.3, hh / 2 + by + 0.3]
    cons["copper_keepout"] = list(cons.get("copper_keepout") or []) + [
        dict(
            name="k4",
            rect=keep,
            layers=copper_names(spec["stackup"]["copper_layers"]),
            items=["tracks", "vias"],
        )
    ]
    spec["checks"].append(
        dict(
            id="unconnected-designed",
            kind="unconnected",
            pads=["U1." + PARTIAL_OPEN],
            engine="fanout.partial",
        )
    )
    # K4's open is the design's: run.py does not count it (its check above holds the
    # cut-off pads to exactly this list).
    spec["designed_open"] = ["U1." + PARTIAL_OPEN]
    spec["name"] += "-partial"
    spec["description"] = (
        spec.get("description", "")
        + " With a partial fanout: C8's drop sites are reserved and the bridge joins it to C7;"
        " K4 sits under a keepout and stays open while the VCC plane is whole."
    )
    spec["dims"]["constraints"] = "partial"
    spec["features"] = sorted(set(spec["features"]) | {"partial-fanout"})
    return spec


# The rails rung: VCC split by the STM32F207's supply pins (VDD, VDDA with VREF+,
# VBAT), each from its own header (VDDA from J5 by its balls, VDD from J6, VBAT from
# J7 by C1), all three on the supply plane In4 with the rest ground. Currents and
# budgets [D]: VDD 0.15 A (the datasheet's run-mode envelope), VDDA 0.02 A, VBAT
# 0.001 A; each rail's copper may drop 1 % of 3.3 V (33 mV).
RAIL_OF = {"VDD": "VDD", "VDDA": "VDDA", "VREF+": "VDDA", "VBAT": "VBAT"}
RAIL_HEADERS = {"VDDA": ("J5", (2.5, 5.5)), "VDD": ("J6", (33.5, 5.5)), "VBAT": ("J7", (2.5, 33.0))}
RAIL_CURRENT = {"VDD": 0.15, "VDDA": 0.02, "VBAT": 0.001}
RAIL_BUDGET_MV = 33.0
RAIL_CAPS = {"C1": "VDD", "C2": "VDD", "C3": "VDD", "C4": "VDD", "C5": "VDDA", "C6": "VBAT"}


def ufbga_rails(spec):
    """``spec`` (the 6L BGA rung) with its supply split into three rails sharing the
    supply plane (``plane_partition``) and an IR-drop check per rail (``ir_drop``)."""
    spec = deepcopy(spec)
    u1 = spec["parts"][0]
    balls = {}
    for row, names in STM32F207_UFBGA176.items():
        for col, name in enumerate(names.split()):
            if name in RAIL_OF:
                ball = row + str(col + 1)
                u1["pins"][ball] = RAIL_OF[name]
                balls.setdefault(RAIL_OF[name], []).append(ball)
    parts = {p["ref"]: p for p in spec["parts"]}
    for ref, net in RAIL_CAPS.items():
        parts[ref]["pins"]["1"] = net
    parts["C7"]["pins"]["1"] = "VDD"
    cons = spec["constraints"]
    for net, (ref, at) in sorted(RAIL_HEADERS.items()):
        if ref in parts:
            parts[ref]["pins"] = {"1": net, "2": "GND"}
        else:
            spec["parts"].append(part(ref, "connector", "%s input" % net, [net, "GND"]))
        cons["fixed"][ref] = dict(at=list(at), rot=0, side="top")
    spec["expected_components"] = len(spec["parts"])
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    rails = ["VDD", "VDDA", "VBAT"]
    classes = cons["net_class"]
    vcc = classes.pop("plane_vcc")
    for net in rails:
        classes["plane_" + net.lower()] = dict(vcc, nets=[net])
    layer = vcc["plane_layer"]
    for x in spec["stackup"]["layers"]:
        if x.get("net") == "VCC":
            x["net"] = "VDD"
    (fanout,) = cons["fanout"]
    fanout["via_classes"]["planes"]["nets"] = ["GND"] + rails
    cons["plane_partition"] = [
        dict(
            layer=layer,
            nets=rails,
            split_gap_mm=0.3,
            min_width_mm=1.0,
            fill="GND",
            currents=dict(RAIL_CURRENT),
            # The fanout's access cells stay open to the caps' drops (seed 1: a VDD
            # cap's drop via closed B12's tail; stage 3c E4).
            protect_fanouts=True,
        )
    ]
    cons["ir_drop"] = []
    checks = [c for c in spec["checks"] if not (c["kind"] == "plane" and c["net"] == "VCC")]
    for c in checks:
        if c["id"] == "fanout-plane-vias":
            c["nets"] = ["GND"] + rails
    for net in rails:
        sink = {"U1": sorted(balls[net])}
        entry = dict(
            net=net,
            sources=["%s:1" % RAIL_HEADERS[net][0]],
            sinks=sink,
            current_a=RAIL_CURRENT[net],
            budget_mv=RAIL_BUDGET_MV,
            temperature_c=25,
        )
        cons["ir_drop"].append(entry)
        checks.append(dict(dict(entry, id="ir-" + net, kind="ir_drop"), engine="ir_drop"))
    checks.append(
        dict(
            id="rails-" + layer.split(".")[0],
            kind="rail_zones",
            layer=layer,
            nets=rails,
            fill="GND",
            min_area_mm2=1.0,
            engine="plane_partition",
        )
    )
    have = {c["id"] for c in checks}
    for net, (ref, at) in sorted(RAIL_HEADERS.items()):
        if "fixed-" + ref not in have:
            checks.append(
                dict(
                    id="fixed-" + ref,
                    kind="fixed",
                    ref=ref,
                    at=list(at),
                    rot=0,
                    side="top",
                    tol_mm=0.01,
                    engine="fixed",
                )
            )
    spec["checks"] = checks
    spec["name"] += "-rails"
    spec["description"] = (
        spec.get("description", "")
        + " With three supply rails (VDD, VDDA with VREF+, VBAT) from their own headers,"
        " sharing the supply plane by a plane partition, each within 33 mV of IR drop."
    )
    spec["dims"]["parts"] = "rails"
    spec["features"] = sorted(set(spec["features"]) | {"plane-partition", "ir-drop"})
    return spec


# A buck power stage on hot-rod lands (11-buck-vqfnhr-pour): KiCad's VQFN-HR-10 land
# pattern (TI RPU0010A: three 1.0 x 0.2 mm and three 0.65 x 0.2 mm side lands, four
# 0.25 x 0.55 mm bottom lands, 0.5 mm pitch) with a synthetic buck pin map, not a
# device's: EN tied to VIN (1), VIN (2, 9, 10), PG (3), SW (4), FB (5), GND (6, 7, 8).
BUCK_PINS = {
    "1": "VIN",
    "2": "VIN",
    "3": "PG",
    "4": "SW",
    "5": "FB",
    "6": "GND",
    "7": "GND",
    "8": "GND",
    "9": "VIN",
    "10": "VIN",
}
BUCK_SIZE = (26, 18)


def buck_pour():
    """``11-buck-vqfnhr-4L-SGPS-pour``: a buck stage whose hot-rod lands no track can
    enter at its clearance, joined by an outer pour per rail (``plane_partition``
    with a ``region`` over U1, L1 and C1, whole-land terminals, solid connection, the
    GND pour stitched to In1). VOUT is the In2 plane; VIN arrives on J1 and joins its
    pour; the FB divider, PG pull-up and output caps are placed and routed freely."""
    parts = [
        pinned("U1", "vqfn_hr10", "buck (synthetic pin map)", BUCK_PINS),
        pinned("L1", "l_1210", "1u", ["SW", "VOUT"]),
        pinned("C1", "capacitor", "10u", ["VIN", "GND"]),
        pinned("C2", "capacitor", "22u", ["VOUT", "GND"]),
        pinned("C3", "capacitor", "22u", ["VOUT", "GND"]),
        pinned("R1", "r_0402", "100k", ["VOUT", "FB"]),
        pinned("R2", "r_0402", "50k", ["FB", "GND"]),
        pinned("R3", "r_0402", "100k", ["VOUT", "PG"]),
        part("J1", "connector", "5V in", ["VIN", "GND"]),
        pinned("J2", "header_1x03", "out", ["VOUT", "GND", "PG"]),
    ]
    spec = circuit(
        "11-buck-vqfnhr",
        "A buck stage on a VQFN-HR-10 land pattern whose hot-rod lands are joined by "
        "outer-layer pours, with its input, output and feedback parts.",
        parts,
        BUCK_SIZE,
    )
    cons = spec["constraints"]
    cons["board"]["default_clearance_mm"] = 0.2
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
        "supply": dict(nets=["VIN", "VOUT"], width_mm=0.3),
        "return": dict(nets=["GND"], width_mm=0.3),
    }
    w, h = BUCK_SIZE
    cons["fixed"] = {
        # The stage as its pins ask: C1 across the VIN (9, 10) and GND (7, 8) bottom
        # lands (y up here: KiCad's bottom row faces +y), L1 east of SW (4).
        "U1": dict(at=[11.0, 9.0], rot=0, side="top"),
        "C1": dict(at=[11.0, 11.9], rot=0, side="top"),
        "L1": dict(at=[15.6, 8.25], rot=0, side="top"),
        "J1": dict(at=[2.5, 9.0], rot=0, side="top"),
        "J2": dict(at=[w - 2.5, 9.0], rot=0, side="top"),
    }
    spec["supply"] = dict(voltage_v=5, max_current_a=2.0)
    return hard(spec, "buck-vqfnhr", ["power-stage"], "manual", 60)


def buck_four_layer():
    """``11-buck-vqfnhr-4L-SGPS``: the buck stage on four layers (GND In1, VOUT In2),
    without pours: the control arm (no track enters the hot-rod lands)."""
    return with_stackup(buck_pour(), "4L-SGPS", power="VOUT")


def buck_pour_rung(spec):
    """``spec`` (the four-layer buck stage) with its outer pours."""
    spec = deepcopy(spec)
    cons = spec["constraints"]
    cons["plane_partition"] = [
        dict(
            layer="F.Cu",
            nets=["VIN", "SW", "GND"],
            region=dict(refs=["U1", "L1", "C1"], margin_mm=0.6),
            terminals="pad",
            connect="solid",
            stitch_vias=3,
            split_gap_mm=0.2,
            min_width_mm=0.25,
        )
    ]
    spec["name"] += "-pour"
    spec["dims"]["parts"] = "pour"
    spec["features"] = sorted(set(spec["features"]) | {"outer-pour", "hot-rod-lands"})
    spec["description"] += (
        " The hot-rod lands of VIN, SW and GND are joined by one F.Cu pour per rail"
        " inside the power stage's region (whole-land terminals, solid connection)."
    )
    return spec


# A coupled LVDS pair on the BGA rung (11-ufbga201-fanout-6L-SGSGPS-pairs): two
# adjacent ring-0 balls on the south edge that the base leaves unused (R13 PB11, R14
# PB14) become LVDS_N and LVDS_P and run to a 2-pin 1.0 mm JST SH header on the south
# edge east of J2, coupled on F.Cu from their fanout exits (board.route_pairs: coupled).
# The west ball is N, the west pin (2, at rot 180) too: the pair never crosses itself.
PAIRS_BALLS = {"R13": "LVDS_N", "R14": "LVDS_P"}
PAIRS_HEADER = ("J7", (29.5, 3.6), 180)
PAIRS_RULE = dict(name="lvds", p="LVDS_P", n="LVDS_N", width_mm=0.1, gap_mm=0.15, skew_mm=0.1)
PAIRS_UNCOUPLED_MM = 3.0
PAIRS_RULES = [
    '(rule "lvds uncoupled"\n  (condition "A.inDiffPair(\'LVDS_\')")\n'
    "  (constraint diff_pair_uncoupled (max %gmm)))" % PAIRS_UNCOUPLED_MM,
    '(rule "lvds gap"\n  (condition "A.inDiffPair(\'LVDS_\')")\n'
    "  (constraint diff_pair_gap (min 0.14mm) (opt 0.15mm) (max 0.16mm)))",
]


def ufbga_pairs(spec):
    """``spec`` (the 6L BGA rung) with one LVDS pair from two adjacent balls to a
    header, routed coupled on F.Cu from the fanout exits; KiCad judges its skew,
    uncoupled length and gap (custom rules), the checks its vias (none)."""
    spec = deepcopy(spec)
    u1 = spec["parts"][0]
    u1["pins"].update(PAIRS_BALLS)
    ref, at, rot = PAIRS_HEADER
    spec["parts"].append(
        pinned(ref, "jst_sh_2", "LVDS out", {"1": "LVDS_P", "2": "LVDS_N", "MP": ""})
    )
    spec["expected_components"] = len(spec["parts"])
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    cons = spec["constraints"]
    cons["fixed"][ref] = dict(at=list(at), rot=rot, side="top")
    cons["diff_pair"] = [dict(PAIRS_RULE, layers=["F.Cu"], max_uncoupled_mm=PAIRS_UNCOUPLED_MM)]
    cons["board"]["route_pairs"] = "coupled"
    spec["dru_rules"] = list(PAIRS_RULES)
    spec["checks"] += [
        dict(
            id="fixed-" + ref,
            kind="fixed",
            ref=ref,
            at=list(at),
            rot=rot,
            side="top",
            tol_mm=0.01,
            engine="fixed",
        ),
        dict(
            id="lvds-no-vias",
            kind="net_vias",
            nets=["LVDS_P", "LVDS_N"],
            max=0,
            engine="diff_pair",
        ),
    ]
    spec["name"] += "-pairs"
    spec["description"] = (
        spec.get("description", "")
        + " With an LVDS pair from two adjacent south balls to a 2-pin header, coupled on"
        " F.Cu from the fanout exits: 0.10/0.15 mm, skew 0.1 mm, 3 mm uncoupled."
    )
    spec["dims"]["parts"] = "pairs"
    spec["features"] = sorted(set(spec["features"]) | {"coupled-pairs", "custom-rules"})
    return spec


# --------------------------------------------------------- run configurations

# yapnr's configuration per family (run.py arguments). The ladder's documented best: the
# initial pool (8 starts, 3 routed finalists) and 4 place-route rounds. The MCU family
# uses a budgeted form of it (1 routed finalist, 2 rounds, the packed maze kernel, which
# routes identically): one detailed route of the 2-layer board took 30 minutes.
YAPNR_BEST = ["--initial-pool", "--initial-starts", "8", "--initial-finalists", "3"]
YAPNR_BUDGET = [
    "--initial-pool",
    "--initial-starts",
    "8",
    "--initial-finalists",
    "1",
    "--rounds",
    "2",
    "--packed-maze",
]


# ------------------------------------------------------------------- the list


def hard_rungs():
    """Every hard rung, base designs first, then their one-dimension variants."""
    mcu = mcu_usb()
    out = [mcu]
    out += [with_stackup(mcu, name) for name in ("4L-SGPS", "4L-SSGS", "6L-SGSGPS")]
    out += [mcu_absolute(mcu), mcu_relative(mcu), mcu_sidelock(mcu), mcu_mc(mcu), mcu_header(mcu)]
    for spec in out:
        spec["yapnr_args"] = YAPNR_BUDGET if spec.get("driver") != "mc" else ["--packed-maze"]
        spec["ci"] = dict(lane="manual", minutes=150 if spec.get("driver") == "mc" else 90)
    base = chaser_base()
    chasers = [
        with_stackup(base, name)
        for name in ("4L-SGPS", "4L-SGGS", "4L-SSGS", "6L-SGSSPS", "6L-SGSGPS", "8L-SGSGPSGS")
    ]
    six = with_stackup(base, "6L-SGSGPS")
    chasers += [with_via_policy(six, "blind-buried"), with_via_policy(six, "hdi")]
    chasers += [with_double_sided(base), with_double_sided(with_stackup(base, "4L-SGPS"))]
    chasers += [chaser_absolute(base), chaser_relative(base), chaser_sidelock(base)]
    chasers.append(chaser_arcblock(chasers[0]))  # on 4L-SGPS: one new dimension
    bga = ufbga_fanout()
    others = [quad_bank(), power_switch(), bga, ufbga_block(bga), ufbga_classes(bga)]
    buck = buck_four_layer()
    others += [
        ufbga_partial(bga),
        ufbga_rails(bga),
        ufbga_pairs(bga),
        buck,
        buck_pour_rung(buck),
    ]
    for spec in chasers + others:
        spec["yapnr_args"] = YAPNR_BEST
    return deepcopy(out + chasers + others)
