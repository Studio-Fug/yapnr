"""Synthetic parent-context challenges, not electrically qualified boards.

All variants are explicit legal local placements. No fixture encodes a winning
parent placement: the production hierarchical placer chooses poses and variants.
"""

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad


def part(ref, address, xy, nets, size=(1.6, 1.6)):
    return Component(
        ref,
        "synthetic:two-pin",
        xy,
        0,
        "top",
        size,
        size,
        pads=[Pad(str(i + 1), n, ((i - 0.5) * 0.6, 0), (0.4, 0.4)) for i, n in enumerate(nets)],
        address=address,
    )


def record(layout, width, height, seed=0):
    return dict(
        layout=layout,
        width=width,
        height=height,
        seed=seed,
        utilisation=0.4,
        aspect=width / height,
        missing=0,
        port_debt_mm=0,
        status="ok",
        area=width * height,
        evidence="synthetic-placement-only",
    )


def graph_of(name, parts, width, height):
    nets = {}
    for c in parts:
        for p in c.pads:
            nets.setdefault(p.net, []).append((c.ref, p.name))
    return BoardGraph(
        name,
        parts,
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(width, height),
    )


def opposed_ports():
    """Identical blocks face opposite pin order at fixed interfaces.

    Hard component orientation prevents disguising a pad-order variant as a
    whole-block rotation; each repeated instance should choose independently.
    """
    parts, fixed, orientations = [], {}, {}
    for ch, x in (("a", 2), ("b", 28)):
        for i in range(3):
            ref, j = f"R{ch}{i}", f"J{ch}{i}"
            net = f"{ch}{i}"
            parts += [
                part(ref, f"parent.{ch}.r{i}", (10, 10), [net, f"{ch}bus"]),
                part(j, f"parent.{j}", (x, 5 + (i if ch == "a" else 2 - i) * 3), [net, f"stub{j}"]),
            ]
            fixed[j] = dict(at=list(parts[-1].pos), rot=0, side="top")
            orientations[ref] = 0
    g = graph_of("opposed-ports", parts, 30, 20)
    con = compile_constraints(
        dict(board=dict(width=30, height=20), fixed=fixed, orientation=orientations), g.refs
    )
    tier = [
        record(
            {f"r{i}": [2, 2 + (i if order == 0 else 2 - i) * 3, 0, "top"] for i in range(3)},
            4,
            10,
            order,
        )
        for order in range(2)
    ]
    return g, con, {}, {f"parent.{ch}": tier for ch in ("a", "b")}


def bank_shapes():
    """A four-capacitor bank feeding a wide fixed converter interface.

    Compact square, horizontal row, vertical row, and a reversed-pad row expose
    distinct span and power/return access choices to the parent.
    """
    parts = [part(f"C{i}", f"converter.bank.c{i}", (12, 10), ["V", "G"]) for i in range(4)]
    fixed = {}
    for ref, xy, nets in (
        ("U1", (5, 6), ["V", "G"]),
        ("J1", (21, 6), ["V", "G"]),
        ("L1", (13, 13), ["SW", "V"]),
    ):
        c = part(ref, f"converter.{ref}", xy, nets, (4, 4))
        parts.append(c)
        fixed[ref] = dict(at=list(xy), rot=0, side="top")
    g = graph_of("bank-shapes", parts, 26, 20)
    con = compile_constraints(dict(board=dict(width=26, height=20), fixed=fixed), g.refs)
    square = {f"c{i}": [2 + (i % 2) * 2.5, 2 + (i // 2) * 2.5, 0, "top"] for i in range(4)}
    row = {f"c{i}": [2 + i * 2.5, 1.5, 0, "top"] for i in range(4)}
    column = {f"c{i}": [1.5, 2 + i * 2.5, 0, "top"] for i in range(4)}
    reversed_row = {key: [*pose[:2], 180, "top"] for key, pose in row.items()}
    tier = [
        record(square, 6.5, 6.5),
        record(row, 11.5, 3, 1),
        record(column, 3, 11.5, 2),
        record(reversed_row, 11.5, 3, 3),
    ]
    return g, con, {}, {"converter.bank": tier}


def coupled_ports():
    """Two blocks with opposing local preferences and three shared signals.

    A pair of simultaneous pad-order changes can save cross-block spans that
    changing just one block would temporarily lengthen.
    """
    parts, fixed, orientations = [], {}, {}
    for ch in ("a", "b"):
        for i in range(3):
            ref = f"R{ch}{i}"
            parts.append(part(ref, f"parent.{ch}.r{i}", (10, 10), [f"link{i}", f"{ch}bus"]))
            orientations[ref] = 0
    # Fixed asymmetric ports set the parent context, with shared links carrying
    # more pin spans than either local preference alone.
    for i in (0, 2):
        ref = f"J{i}"
        parts.append(
            part(
                ref,
                f"parent.{ref}",
                (3 if i == 0 else 27, 14 if i == 0 else 5),
                [f"link{i}", f"stub{i}"],
            )
        )
        fixed[ref] = dict(at=list(parts[-1].pos), rot=0, side="top")
    g = graph_of("coupled-ports", parts, 30, 22)
    con = compile_constraints(
        dict(board=dict(width=30, height=22), fixed=fixed, orientation=orientations), g.refs
    )
    tier = [
        record(
            {f"r{i}": [2, 2 + (i if order == 0 else 2 - i) * 3, 0, "top"] for i in range(3)},
            4,
            10,
            order,
        )
        for order in range(2)
    ]
    return g, con, {}, {f"parent.{ch}": tier for ch in ("a", "b")}


CASES = {"opposed_ports": opposed_ports, "bank_shapes": bank_shapes, "coupled_ports": coupled_ports}


def crowded_library():
    """Near-duplicate high-ranked layouts crowd out useful terminal-order diversity."""
    import copy

    g, c, r, lib = opposed_ports()
    g.name = "crowded-library"
    old = lib["parent.a"]
    near = copy.deepcopy(old[1])
    near["layout"]["r0"][0] += 0.02
    near["seed"] = 3
    tier = [old[1], near, old[0]]
    return g, c, r, {name: tier for name in lib}


CASES["crowded_library"] = crowded_library
