"""A rung's copper stack as native KiCad 10 board data (pcbnew, KiCad's Python).

``hard_rungs.stackup()`` describes the stack tool-neutrally: copper layers in order,
each a signal layer or a plane of one net, and the dielectric build. This module
writes it into a board: the layer types (a plane layer is KiCad's ``power`` type),
the ``(stackup ...)`` block of the board setup, and full-outline plane zones.

``which="extra"`` (the ladder generator) draws only the zones the engine cannot
declare itself: the engine takes one plane layer per net class, so the first plane
layer of each net comes from its net class (pnr.planes) and only further layers of
the same net are drawn here. ``which="all"`` (the benchmark inputs for other tools)
draws a zone on every plane layer.
"""

import re

PLANE_CLEARANCE_MM = 0.2  # the fixtures' copper clearance
PLANE_MIN_WIDTH_MM = 0.25  # the fixtures' track width


def layer_types(board, stackup):
    """Type every plane layer as KiCad ``power`` and every signal layer ``signal``."""
    import pcbnew

    for layer in stackup["layers"]:
        lid = board.GetLayerID(layer["name"])
        board.SetLayerType(lid, pcbnew.LT_POWER if layer["role"] == "plane" else pcbnew.LT_SIGNAL)


def zone_layers(spec, which):
    """``[(layer, net)]`` to draw as zones: every plane layer, or (``extra``) only those
    after the first plane layer of the same net (the engine's net class covers that)."""
    planes = [(x["name"], x["net"]) for x in spec["stackup"]["layers"] if x["role"] == "plane"]
    if which == "all":
        return planes
    seen, out = set(), []
    for layer, net in planes:
        if net in seen:
            out.append((layer, net))
        seen.add(net)
    return out


def add_plane_zones(board, spec, rect_nm, which):
    """Add a full-outline zone per plane layer (see :func:`zone_layers`) unless the
    board already has a zone of that net on that layer. ``rect_nm`` is the outline
    ``(x0, y0, x1, y1)`` in board units. Returns the zones added."""
    import pcbnew

    added = 0
    for layer, net_name in zone_layers(spec, which):
        lid = board.GetLayerID(layer)
        net = board.FindNet(net_name)
        if net is None:
            raise ValueError("plane net %s is not on the board" % net_name)
        if any(
            not z.GetIsRuleArea() and z.GetNetCode() == net.GetNetCode() and z.IsOnLayer(lid)
            for z in board.Zones()
        ):
            continue
        zone = pcbnew.ZONE(board)
        zone.SetLayer(lid)
        zone.SetNetCode(net.GetNetCode())
        zone.SetZoneName("plane %s %s" % (net_name, layer))
        zone.SetLocalClearance(pcbnew.FromMM(PLANE_CLEARANCE_MM))
        zone.SetMinThickness(pcbnew.FromMM(PLANE_MIN_WIDTH_MM))
        outline = zone.Outline()
        outline.NewOutline()
        x0, y0, x1, y1 = rect_nm
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            outline.Append(pcbnew.VECTOR2I(int(x), int(y)))
        board.Add(zone)
        added += 1
    return added


def stackup_text(stackup, indent="\t\t"):
    """The board setup's ``(stackup ...)`` block for ``stackup``."""
    copper = [x["name"] for x in stackup["layers"]]
    rows = [
        '(layer "F.SilkS" (type "Top Silk Screen"))',
        '(layer "F.Paste" (type "Top Solder Paste"))',
        '(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))',
    ]
    for index, name in enumerate(copper):
        outer = index in (0, len(copper) - 1)
        thickness = stackup["outer_copper_mm"] if outer else stackup["inner_copper_mm"]
        rows.append('(layer "%s" (type "copper") (thickness %g))' % (name, thickness))
        if index < len(copper) - 1:
            d = stackup["dielectrics"][index]
            rows.append(
                '(layer "dielectric %d" (type "%s") (thickness %g) (material "FR4") '
                "(epsilon_r 4.5) (loss_tangent 0.02))" % (index + 1, d["kind"], d["thickness_mm"])
            )
    rows += [
        '(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))',
        '(layer "B.Paste" (type "Bottom Solder Paste"))',
        '(layer "B.SilkS" (type "Bottom Silk Screen"))',
        '(copper_finish "None")',
        "(dielectric_constraints no)",
    ]
    inner = "".join("\n" + indent + "\t" + r for r in rows)
    return indent + "(stackup" + inner + "\n" + indent + ")"


def insert_stackup(text, stackup):
    """Insert (or replace) the ``(stackup ...)`` block at the head of ``(setup``."""
    start = text.find("(stackup")
    if start >= 0:
        depth = 0
        for end in range(start, len(text)):
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            if depth == 0:
                break
        text = text[:start] + text[end + 1 :]
    match = re.search(r"\(setup[ \t]*\n", text)
    if not match:
        raise ValueError("board has no (setup block")
    return text[: match.end()] + stackup_text(stackup) + "\n" + text[match.end() :]
