"""Small, electrically meaningful placement/routing regression inputs.

These are circuit/netlist fixtures, not preplaced or prerouted golden boards.
Pin numbering follows KiCad standard footprints and the named device datasheets.
"""

from copy import deepcopy

LIB = {
    "connector": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "input": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "resistor": "Resistor_SMD:R_0805_2012Metric",
    "capacitor": "Capacitor_SMD:C_0805_2012Metric",
    "led": "LED_SMD:LED_0805_2012Metric",
    "timer": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
    "counter": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
    "inverter": "Package_TO_SOT_SMD:SOT-23-5",
    "button": "Button_Switch_SMD:SW_SPST_EVQPE1",
}

# The axis of each footprint's pad row at rotation 0 (from the KiCad footprints: the
# header's pins run along y, the button's, LED's and passives' pads along x). An edge
# part is turned so its pad row, its long axis, runs along the edge. The contract tests
# check the table against the .kicad_mod files and the edge case's turns against it.
PAD_AXIS = {"connector": "y", "button": "x", "led": "x", "resistor": "x", "capacitor": "x"}


def part(ref, kind, value, nets):
    return dict(
        ref=ref, footprint=LIB[kind], value=value, pins={str(i + 1): n for i, n in enumerate(nets)}
    )


def circuit(name, description, parts, size, *, planes=False):
    # A supply connector is a real mechanical anchor; all other parts are movable.
    return dict(
        name=name,
        description=description,
        parts=parts,
        constraints=dict(
            schema="v0",
            board=dict(
                outline=dict(w=size[0], h=size[1]),
                layers=4 if planes else 2,
                default_clearance_mm=0.4,
                references_on_fab=True,
            ),
            fab=dict(
                track_width_mm=0.25,
                clearance_mm=0.2,
                via_diameter_mm=0.6,
                via_drill_mm=0.3,
                hole_clearance_mm=0.25,
                edge_clearance_mm=0.3,
                min_through_drill_mm=0.3,
                via_annular_mm=0.15,
            ),
            fixed={"J1": dict(at=[4, size[1] / 2], rot=0, side="top")},
            net_class={
                "supply": dict(
                    nets=["VCC"], width_mm=0.4, current_a=0.1, copper_oz=1, delta_t_c=10
                ),
                "return": dict(
                    nets=["GND"], width_mm=0.4, **({"plane_layer": "In1.Cu"} if planes else {})
                ),
            },
        ),
        expected_components=len(parts),
        expected_connected_pads=sum(bool(n) for p in parts for n in p["pins"].values()),
        supply=dict(voltage_v=5, max_current_a=0.1),
        layer_mode="ground_plane" if planes else "routed_return",
    )


def timer_parts():
    return [
        part("J1", "connector", "5V input", ["VCC", "GND"]),
        part(
            "U1",
            "timer",
            "TLC555",
            ["GND", "TIMING", "CLOCK", "VCC", "CONTROL", "TIMING", "DISCHARGE", "VCC"],
        ),
        part("R1", "resistor", "10k", ["VCC", "DISCHARGE"]),
        part("R2", "resistor", "100k", ["DISCHARGE", "TIMING"]),
        part("C1", "capacitor", "4.7u", ["TIMING", "GND"]),
        part("C2", "capacitor", "10n", ["CONTROL", "GND"]),
        part("C3", "capacitor", "100n", ["VCC", "GND"]),
    ]


def chaser(count, *, planes=False):
    p = timer_parts()
    # CD4017 pin1..16: Q5,Q1,Q0,Q2,Q6,Q7,Q3,GND,Q8,Q4,Q9,CO,CE,CLK,RESET,VDD.
    # Reset on Q[count] makes a count-stage one-hot LED sequence.
    qpins = {0: 3, 1: 2, 2: 4, 3: 7, 4: 10, 5: 1, 6: 5, 7: 6, 8: 9, 9: 11}
    pins = [""] * 16
    for i in range(count):
        pins[qpins[i] - 1] = "Q" + str(i)
    pins[qpins[count] - 1] = "RESET"
    for pin, net in {8: "GND", 13: "GND", 14: "CLOCK", 15: "RESET", 16: "VCC"}.items():
        pins[pin - 1] = net
    p.append(part("U2", "counter", "CD4017B", pins))
    p.extend(
        [
            part("C4", "capacitor", "100n", ["VCC", "GND"]),
            part("C5", "capacitor", "10u", ["VCC", "GND"]),
        ]
    )
    for i in range(count):
        p += [
            part("R" + str(i + 3), "resistor", "2.2k", ["Q" + str(i), "LED" + str(i)]),
            part("D" + str(i + 1), "led", "red", ["GND", "LED" + str(i)]),
        ]
    return circuit(
        ("07" if planes else "06" if count == 5 else "05")
        + "-chaser-"
        + str(len(p))
        + ("-plane" if planes else ""),
        "TLC555 clock + CD4017B modulo-%d LED chaser; unused counter outputs intentionally NC."
        % count,
        p,
        (42, 32) if count == 5 else (36, 28),
        planes=planes,
    )


def designs():
    p = [
        part("J1", "connector", "Externally current-limited input", ["LED_A", "GND"]),
        part("D1", "led", "red", ["GND", "LED_A"]),
    ]
    out = [
        circuit(
            "01-connector-led-2",
            "Two parts, external 2mA current source required (no omitted onboard resistor).",
            p,
            (18, 14),
        )
    ]
    p = [
        part("J1", "connector", "5V input", ["VCC", "GND"]),
        part("R1", "resistor", "1k", ["VCC", "LED_A"]),
        part("D1", "led", "red", ["GND", "LED_A"]),
    ]
    out.append(
        circuit(
            "02-resistor-led-3", "Current-limited LED with movable resistor and LED.", p, (20, 16)
        )
    )
    p = p + [
        part("R2", "resistor", "1k", ["VCC", "LED_B"]),
        part("D2", "led", "red", ["GND", "LED_B"]),
    ]
    out.append(
        circuit(
            "03-branched-leds-5",
            "Two independent LED loads sharing supply and return.",
            p,
            (24, 18),
        )
    )
    p = [
        part("J1", "input", "3.3V, GND, input", ["VCC", "GND", "INPUT"]),
        part("U1", "inverter", "SN74LVC1G04", ["", "INPUT", "GND", "OUTPUT", "VCC"]),
        part("C1", "capacitor", "100n", ["VCC", "GND"]),
        part("C2", "capacitor", "1u", ["VCC", "GND"]),
        part("R1", "resistor", "2.2k", ["VCC", "LED_A"]),
        part("D1", "led", "red", ["OUTPUT", "LED_A"]),
        part("R2", "resistor", "2.2k", ["OUTPUT", "LED_B"]),
        part("D2", "led", "red", ["GND", "LED_B"]),
    ]
    out.append(
        circuit(
            "04-inverter-leds-8",
            "Logic-controlled complementary LED indicators and bypass capacitors.",
            p,
            (26, 20),
        )
    )
    out[-1]["supply"]["voltage_v"] = 3.3
    p = timer_parts() + [
        part("R3", "resistor", "2.2k", ["CLOCK", "LED_A"]),
        part("D1", "led", "red", ["GND", "LED_A"]),
        part("C4", "capacitor", "10u", ["VCC", "GND"]),
    ]
    # Keep stable numerical ordering as complexity grows.
    t = circuit(
        "05-timer-led-10",
        "TLC555 astable LED blinker with timing, control and supply capacitors.",
        p,
        (30, 24),
    )
    out.append(t)
    small = chaser(2)
    small["name"] = "06-chaser-14"
    out.append(small)
    large = chaser(5)
    large["name"] = "07-chaser-20"
    out.append(large)
    plane = chaser(5, planes=True)
    plane["name"] = "08-chaser-20-plane"
    out.append(plane)
    return deepcopy(out)


# ----------------------------------------------------------------------- showcases
# Demonstrations of placement constraints and hierarchy, run with ``run.py --showcases``.
# They sit outside the ladder: no NN- prefix, never gated, animated in
# docs/constraints-and-hierarchy.md.


def line_chaser():
    """07-chaser-20 with its five LEDs held in one rigid line (the only difference)."""
    spec = chaser(5)
    spec["name"] = "line-chaser-20"
    spec["description"] = (
        "The 07-chaser-20 chaser with LEDs D1 to D5 held in one line group (3.0 mm pitch), "
        "so the sequence reads as a row."
    )
    spec["constraints"]["line_group"] = [
        dict(
            name="chaser_leds",
            members=["D1", "D2", "D3", "D4", "D5"],
            pitch_mm=3.0,
            rot=90,
            reason="Chaser LEDs in one row, so the sequence reads as a line",
        )
    ]
    return spec


EDGE_PARTS = {"J1": "connector", "SW1": "button", "D1": "led"}


def edge_io(hard=True):
    """A "hold to blink" front panel: a TLC555 astable whose RESET a pushbutton pulls high.

    With ``hard`` the supply connector, the button and the LED sit on the south edge
    (hard edge_align, each turned so its long axis runs along the edge); without it the
    same parts are free. Nothing is fixed."""
    p = [
        part("J1", "connector", "5V input", ["VCC", "GND"]),
        part(
            "U1",
            "timer",
            "TLC555",
            ["GND", "TIMING", "CLOCK", "RESET", "CONTROL", "TIMING", "DISCHARGE", "VCC"],
        ),
        part("R1", "resistor", "10k", ["VCC", "DISCHARGE"]),
        part("R2", "resistor", "100k", ["DISCHARGE", "TIMING"]),
        part("C1", "capacitor", "4.7u", ["TIMING", "GND"]),
        part("C2", "capacitor", "10n", ["CONTROL", "GND"]),
        part("C3", "capacitor", "100n", ["VCC", "GND"]),
        part("SW1", "button", "Hold to blink", ["VCC", "RESET"]),
        part("R4", "resistor", "100k", ["RESET", "GND"]),
        part("R3", "resistor", "2.2k", ["CLOCK", "LED_A"]),
        part("D1", "led", "red", ["GND", "LED_A"]),
        part("C4", "capacitor", "10u", ["VCC", "GND"]),
    ]
    spec = circuit(
        "edge-io-12" if hard else "edge-io-12-free",
        "TLC555 blinker that runs while SW1 is held; supply connector, button and LED "
        + ("on the south edge (hard edge alignment)." if hard else "placed freely."),
        p,
        (36, 26),
    )
    del spec["constraints"]["fixed"]  # nothing fixed: the edge parts choose their order
    if hard:
        spec["constraints"]["edge_align"] = {
            ref: dict(edge="south", hard=True, tolerance_mm=1.0) for ref in EDGE_PARTS
        }
        # Pad row along the south edge: the header's y row turns by 90 degrees.
        spec["constraints"]["orientation"] = {
            ref: 90 if PAD_AXIS[kind] == "y" else 0 for ref, kind in EDGE_PARTS.items()
        }
    return spec


def hier_twin_bank():
    """A 555 clock driving two identical CD4017B LED banks, built from atopile-style
    module addresses: three blocks (top.clock, top.bank_a, top.bank_b), two templates,
    placed and routed hierarchically (regression/hier_case.py)."""

    def at(address, spec):
        return dict(spec, address=address)

    p = [
        at("top.j1", part("J1", "connector", "5V input", ["VCC", "GND"])),
        at("top.c_bulk", part("C4", "capacitor", "10u", ["VCC", "GND"])),
    ]
    clock = timer_parts()[1:]  # U1, R1, R2, C1, C2, C3; the 555's RESET is tied to VCC
    for local, spec in zip(["u", "r1", "r2", "c1", "c2", "c3"], clock):
        p.append(at("top.clock." + local, spec))
    # CD4017 pin1..16: Q5,Q1,Q0,Q2,Q6,Q7,Q3,GND,Q8,Q4,Q9,CO,CE,CLK,RESET,VDD (as in chaser(5)).
    qpins = {0: 3, 1: 2, 2: 4, 3: 7, 4: 10, 5: 1}
    refs = dict(A=dict(u="U2", c="C5", r=3, d=1), B=dict(u="U3", c="C6", r=8, d=6))
    for bank, ref in refs.items():
        prefix = "top.bank_" + bank.lower()
        pins = [""] * 16
        for i in range(5):
            pins[qpins[i] - 1] = "Q%s%d" % (bank, i)
        pins[qpins[5] - 1] = "RESET" + bank
        for pin, net in {8: "GND", 13: "GND", 14: "CLOCK", 15: "RESET" + bank, 16: "VCC"}.items():
            pins[pin - 1] = net
        p.append(at(prefix + ".u", part(ref["u"], "counter", "CD4017B", pins)))
        p.append(at(prefix + ".c", part(ref["c"], "capacitor", "100n", ["VCC", "GND"])))
        for i in range(5):
            led = "LED%s%d" % (bank, i)
            p.append(
                at(
                    "%s.r%d" % (prefix, i),
                    part("R%d" % (ref["r"] + i), "resistor", "2.2k", ["Q%s%d" % (bank, i), led]),
                )
            )
            p.append(
                at("%s.d%d" % (prefix, i), part("D%d" % (ref["d"] + i), "led", "red", ["GND", led]))
            )
    spec = circuit(
        "hier-twin-bank-%d" % len(p),
        "TLC555 clock and two CD4017B five-LED banks as three blocks (two templates), "
        "placed and routed hierarchically.",
        p,
        (56, 40),
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
    return spec


def showcases():
    return deepcopy([line_chaser(), edge_io(hard=False), edge_io(hard=True), hier_twin_bank()])
