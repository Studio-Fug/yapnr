"""Hard rungs: larger and denser open boards beyond the eight-case ladder.

Run with ``run.py --hard``. Like the showcases they sit outside the ladder's gate
(``designs()`` and its digest are unchanged). They exercise what the small cases do
not: a dense mixed MCU board (TQFP-44, crystal, USB differential pairs, regulator),
hierarchical block synthesis with a template reused four times, current-sized power
copper, and Monte-Carlo successive-halving search over placements.

Most rungs are *variants* of two base designs (``09-mcu-usb-31`` and the ladder's
``07-chaser-20``) that change exactly one dimension, so an effect can be attributed:

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
}


# Pad names a footprint repeats (one net for every copy): the receptacle's eight
# shield pads. The generator maps a name to all its pads; the connected-pad count the
# runner asserts counts each copy.
REPEATED_PADS = {HARD_LIB["usb_micro_b"]: {"SH": 8}, HARD_LIB["u_fl"]: {"2": 2}}


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
    via policy, its plane layers (a plane layer carries no tracks) and its
    differential-pair skew. ``None`` for a rung without such dimensions."""
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
    return hard(
        spec,
        "mcu-usb-31",
        ["dense-mixed", "tqfp44", "usb-diff-pair", "crystal", "regulator"],
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
    return absolute(
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


def mcu_relative(spec):
    return relative(
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


def mcu_sidelock(spec):
    return sidelock(
        spec,
        ["C2", "C3", "C4", "C5", "C6", "C7"],
        "The MCU's decoupling, UCAP and AREF capacitors are locked to the bottom side.",
    )


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
    others = [quad_bank(), power_switch()]
    for spec in chasers + others:
        spec["yapnr_args"] = YAPNR_BEST
    return deepcopy(out + chasers + others)
