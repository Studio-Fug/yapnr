"""A hierarchical rung whose blocks are L-shaped, so they pack best interlocked.

``13-dovetail-blocks-N``: two identical port blocks (one template) and a logic core,
placed and routed hierarchically (``regression/hier_case.py``). A port block is a 12-pin
JST SH side-entry connector (VCC, four inputs, GND on pins 1-6) with a series resistor
per input and a decoupling capacitor; a hard group holds the resistors and the capacitor
within a few millimetres of the connector's pin 1, so the routed block is an L: the
connector is the long leg, the passives the foot, and the rest of the block's rectangle
is a notch. The core is four
two-input AND gates (SN74LVC1G08, SOT-23-5: 1 A, 2 B, 3 GND, 4 Y, 5 VCC) with their
decoupling capacitors, each gate taking one input of each port; its outputs and the
supply leave on a top-level 8-pin JST SH connector.

Packed by their rectangles the three blocks leave the notches empty. Packed by their
routed outlines (``PNR_MACRO_HULL``: per-side occupancy hulls) two Ls can close on each
other and the core can sit in a notch; the rung measures the bounding box and the
outline shrink that frees (``pnr-report.json`` ``route_compact``), alongside the usual
pass, vias and copper.
"""

from designs import circuit, part
from hard_rungs import hard, pinned

SIZE = (56, 40)
GROUP_RADIUS_MM = 8.0


def at(address, spec):
    return dict(spec, address=address)


def sh_pins(count, signals):
    """A JST SH side-entry connector's pad map: VCC, the signals, GND from pin 1, the
    remaining pins and the mounting pads unconnected."""
    nets = ["VCC", *signals, "GND"]
    pins = {str(k + 1): (nets[k] if k < len(nets) else "") for k in range(count)}
    pins["MP"] = ""
    return pins


def dovetail():
    parts = [
        at("top.j1", part("J1", "connector", "5V input", ["VCC", "GND"])),
        at("top.c_bulk", part("C1", "capacitor", "10u", ["VCC", "GND"])),
        at("top.out", pinned("J2", "jst_sh_8", "Outputs", sh_pins(8, ["Y1", "Y2", "Y3", "Y4"]))),
    ]
    groups = []
    for index, port in enumerate("AB"):
        prefix = "top.port_" + port.lower()
        header = "J%d" % (3 + index)
        ins = ["IN%s%d" % (port, k) for k in range(1, 5)]
        parts.append(
            at(prefix + ".j", pinned(header, "jst_sh_12", "Port " + port, sh_pins(12, ins)))
        )
        members = []
        for k in range(4):
            ref = "R%d" % (1 + 4 * index + k)
            members.append(ref)
            parts.append(
                at(
                    "%s.r%d" % (prefix, k + 1),
                    part(ref, "resistor", "100", [ins[k], "P%s%d" % (port, k + 1)]),
                )
            )
        cap = "C%d" % (2 + index)
        members.append(cap)
        parts.append(at(prefix + ".c", part(cap, "capacitor", "100n", ["VCC", "GND"])))
        groups.append(
            dict(
                anchor=header, anchor_pad="1", members=members, radius_mm=GROUP_RADIUS_MM, hard=True
            )
        )
    for k in range(1, 5):
        parts.append(
            at(
                "top.core.u%d" % k,
                part(
                    "U%d" % k,
                    "inverter",  # the SOT-23-5 footprint
                    "SN74LVC1G08",
                    ["PA%d" % k, "PB%d" % k, "GND", "Y%d" % k, "VCC"],
                ),
            )
        )
        parts.append(
            at("top.core.c%d" % k, part("C%d" % (3 + k), "capacitor", "100n", ["VCC", "GND"]))
        )
    spec = circuit(
        "13-dovetail-blocks-%d" % len(parts),
        "Two L-shaped port blocks (a header with its series resistors grouped at one end) and "
        "a four-gate logic core, placed and routed hierarchically: the blocks pack best "
        "interlocked.",
        parts,
        SIZE,
    )
    spec["constraints"]["group"] = groups
    spec["driver"] = "hier"
    spec["hier"] = dict(
        utilisations=[0.45, 0.55],
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
        "dovetail-blocks",
        ["hierarchical", "template-reuse", "macro-placement", "dovetail"],
        "manual",
        30,
    )


def rungs():
    return [dovetail()]
