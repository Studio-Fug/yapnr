"""Synthetic board for the PNR_GLOSS adapter tests (headless KiCad python).

usage: python gloss_fixture.py OUT_DIR      writes OUT_DIR/board.kicad_pcb (+ rules.json)

Nets (F.Cu, 30 x 20 mm board, all ordinary 0.2 mm signal copper unless noted):
  sig   pad A (4,10) -> pad B (26,10); two detours: the first bridges a foreign pad
        ('other' at 10.5,10) and must stay (homotopy), the second is free (gloss).
  sig2  pad (4,14) -> pad (26,14), one straight run split at x = 8, 12, 16 (normalize).
  c1/c2 c1 is a U with 45-degree shoulders whose 11.5 mm bottom leg runs 0.35 mm (edge gap)
        above c2's straight run: the 0.094 mm free sliver between them is dead space. Corridor
        packs c1's leg down to the 0.328 mm pitch (+0.184 mm of length, within the 0.2 slack).
  sig3  a duplicate carrying a joint: trunk (4.5,5)-(26,5) to a pad, a 1.5 mm duplicate on its
        start and a stub from the duplicate's end to a pad (removing the duplicate dangles
        the trunk start: KiCad track_dangling).
"""

import json
import sys
from pathlib import Path

MM = 1_000_000


def rules():
    from pnr.fab_profile import apply_rules

    return apply_rules(
        {
            "fab": {"track_width_mm": 0.2, "clearance_mm": 0.127, "edge_clearance_mm": 0.3},
            "net_classes": [],
            "diff_pairs": [],
        }
    )


def build(extra=None):
    """In-memory board; extra(board, helpers) may add items. Returns (board, helpers)."""
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(4)

    def vec(x, y):
        return k.VECTOR2I(round(x * MM), round(y * MM))

    nets = {}

    def net(name):
        if name not in nets:
            n = k.NETINFO_ITEM(b, name)
            b.Add(n)
            nets[name] = n.GetNetCode()
        return nets[name]

    for a, z in (((0, 0), (30, 0)), ((30, 0), (30, 20)), ((30, 20), (0, 20)), ((0, 20), (0, 0))):
        s = k.PCB_SHAPE(b)
        s.SetShape(k.SHAPE_T_SEGMENT)
        s.SetStart(vec(*a))
        s.SetEnd(vec(*z))
        s.SetLayer(k.Edge_Cuts)
        s.SetWidth(100000)
        b.Add(s)
    footprints = {}

    def pad(ref, num, xy, name, size=0.6, layer=None):
        f = footprints.get(ref)
        if f is None:
            f = footprints[ref] = k.FOOTPRINT(b)
            f.SetReference(ref)
            f.Reference().SetVisible(False)
            b.Add(f)
        p = k.PAD(f)
        p.SetNumber(num)
        p.SetShape(k.PAD_SHAPE_RECT)
        p.SetSize(vec(size, size))
        p.SetPosition(vec(*xy))
        p.SetAttribute(k.PAD_ATTRIB_SMD)
        ls = k.LSET()
        ls.AddLayer(k.F_Cu if layer is None else layer)
        p.SetLayerSet(ls)
        p.SetNetCode(net(name))
        f.Add(p)
        return p

    def track(a, z, name, layer=None, width=0.2):
        t = k.PCB_TRACK(b)
        t.SetStart(vec(*a))
        t.SetEnd(vec(*z))
        t.SetLayer(k.F_Cu if layer is None else layer)
        t.SetWidth(round(width * MM))
        t.SetNetCode(net(name))
        b.Add(t)
        return t

    def path(points, name, **kw):
        return [track(a, z, name, **kw) for a, z in zip(points, points[1:])]

    pad("U1", "1", (4, 10), "sig")
    pad("U2", "1", (26, 10), "sig")
    pad("X1", "1", (10.5, 10), "other")
    path(
        [
            (4, 10),
            (8, 10),
            (9, 9),
            (12, 9),
            (13, 10),
            (16, 10),
            (17, 11),
            (18, 11),
            (19, 12),
            (20, 12),
            (21, 11),
            (22, 11),
            (23, 10),
            (26, 10),
        ],
        "sig",
    )
    pad("U1", "2", (4, 14), "sig2")
    pad("U2", "2", (26, 14), "sig2")
    path([(4, 14), (8, 14), (12, 14), (16, 14), (26, 14)], "sig2")
    pad("U2", "3", (26, 5), "sig3")
    pad("U1", "3", (6, 7.5), "sig3")
    track((4.5, 5), (26, 5), "sig3")
    track((4.5, 5), (6, 5), "sig3")
    track((6, 5), (6, 7.5), "sig3")
    pad("U1", "4", (6, 15.2), "c1")
    pad("U2", "4", (20, 15.2), "c1")
    pad("U1", "5", (4, 17), "c2")
    pad("U2", "5", (22, 17), "c2")
    path([(6, 15.2), (7.25, 16.45), (18.75, 16.45), (20, 15.2)], "c1")
    track((4, 17), (22, 17), "c2")
    helpers = dict(vec=vec, net=net, pad=pad, track=track, path=path, nets=nets, k=k)
    if extra:
        extra(b, helpers)
    b.BuildConnectivity()
    return b, helpers


def write(folder, extra=None):
    import pcbnew as k

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    b, _ = build(extra)
    path = folder / "board.kicad_pcb"
    k.SaveBoard(str(path), b)
    if not path.with_suffix(".kicad_pro").exists():
        path.with_suffix(".kicad_pro").write_text("{}\n")
    # The project carries the routing rules (writeback.patch_project_rules), as in the flow:
    # Default netclass clearance 0.127 / width 0.2 for native DRC.
    from pnr.writeback import patch_project_rules

    patch_project_rules(str(path.with_suffix(".kicad_pro")), rules())
    (folder / "rules.json").write_text(json.dumps(rules(), indent=1))
    return path


if __name__ == "__main__":
    print(write(sys.argv[1]))
