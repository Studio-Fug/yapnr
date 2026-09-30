from pnr.graph import BoardGraph, BoardOutline, Component, Pad, Net
from pnr.constraints import compile_constraints


def fixture():
    def c(ref, address, at, pads):
        return Component(ref, "test", at, 0, "top", (2, 2), (2, 2), pads=pads, address=address)

    cap = c(
        "C9",
        "board.supply.cap",
        (25, 25),
        [Pad("1", "V", (-0.6, 0), (0.5, 0.5)), Pad("2", "G", (0.6, 0), (0.5, 0.5))],
    )
    dev = c(
        "U2",
        "board.supply.ic",
        (6, 6),
        [Pad("5", "V", (-0.6, 0), (0.5, 0.5)), Pad("6", "G", (0.6, 0), (0.5, 0.5))],
    )
    comps = [cap, dev]
    for i, at in enumerate([(2, 2), (38, 2), (38, 38), (2, 38)]):
        comps.append(
            c(
                "J" + str(i),
                "board.remote" + str(i),
                at,
                [Pad("1", "V", (-0.5, 0), (0.5, 0.5)), Pad("2", "G", (0.5, 0), (0.5, 0.5))],
            )
        )
    g = BoardGraph(
        "local-cap",
        comps,
        [
            Net(n, i, [(c.ref, p.name) for c in comps for p in c.pads if p.net == n])
            for i, n in enumerate(["V", "G"], 1)
        ],
        BoardOutline(40, 40),
    )
    cons = compile_constraints(
        {
            "board": {"width_mm": 40, "height_mm": 40},
            "fixed": {
                c.ref: {"at": list(c.pos), "rot": 0, "side": "top"} for c in comps if c.ref != "C9"
            },
        },
        g.refs,
    )
    return g, cons
