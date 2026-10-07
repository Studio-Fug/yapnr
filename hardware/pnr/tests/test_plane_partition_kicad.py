"""A plane partition drawn and filled by KiCad (KiCad Python only: python3 -m unittest
tests.test_plane_partition_kicad). Without numpy in KiCad's Python (the container
image's, the ladder lane) the partition and the IR solves run in PNR_PYTHON."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None
OFFSET = 30.0
W, H = 20.0, 12.0


def board():
    """4 layers, In2 typed power; rails A and B with three 0.6 mm pads each, a via
    under each pad to the plane, and a foreign (GND) via row between them."""
    import pcbnew as k

    def v(x, y):  # graph frame (y up) to KiCad
        return k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + H - y) * 1e6))

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    b.SetLayerType(b.GetLayerID("In2.Cu"), k.LT_POWER)
    b.GetDesignSettings().m_HasStackup = True
    nets = {}
    for name in ("A", "B", "GND"):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(v(0, H))
    edge.SetEnd(v(W, 0))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50000)
    b.Add(edge)
    pads = {
        "A": [(2.0, 2.0), (18.0, 2.0), (10.0, 4.5)],
        "B": [(2.0, 10.0), (18.0, 10.0), (10.0, 7.5)],
    }
    for net, points in pads.items():
        for n, (x, y) in enumerate(points):
            f = k.FOOTPRINT(b)
            f.SetReference("%s%d" % (net, n))
            b.Add(f)
            f.SetPosition(v(x, y))
            p = k.PAD(f)
            p.SetNumber("1")
            p.SetAttribute(k.PAD_ATTRIB_SMD)
            p.SetShape(k.PAD_SHAPE_RECT)
            p.SetSize(k.VECTOR2I(600000, 600000))
            ls = k.LSET()
            ls.AddLayer(k.F_Cu)
            p.SetLayerSet(ls)
            p.SetPosition(v(x, y))
            p.SetNet(nets[net])
            f.Add(p)
            via = k.PCB_VIA(b)
            via.SetPosition(v(x, y))
            via.SetDrill(200000)
            via.SetWidth(400000)
            via.SetViaType(k.VIATYPE_THROUGH)
            via.SetLayerPair(k.F_Cu, k.B_Cu)
            via.SetNet(nets[net])
            b.Add(via)
    for x in (6.0, 8.0, 12.0, 14.0):
        via = k.PCB_VIA(b)
        via.SetPosition(v(x, 6.0))
        via.SetDrill(200000)
        via.SetWidth(400000)
        via.SetViaType(k.VIATYPE_THROUGH)
        via.SetLayerPair(k.F_Cu, k.B_Cu)
        via.SetNet(nets["GND"])
        b.Add(via)
    return b, pads, v


_PARTITION = """
import json, sys
from pnr.plane_partition import _CACHE, Terminal, partition
d = json.load(sys.stdin)
terms = {n: [Terminal(**t) for t in ts] for n, ts in d["terms"].items()}
blocked = [(tuple(c), r) for c, r in d["blocked"]]
_CACHE.clear()
part = partition(d["entry"], width=d["w"], height=d["h"], terminals=terms, blocked=blocked,
                 fill_min_mm=d.get("fill_min", 0.0))
json.dump(part.rows(), sys.stdout)
"""


def partition_rows(entry, pads, blocked, terms=None, fill_min=0.0):
    """The partition's region rows (pnr.plane_partition, numpy): here, or in the numeric
    Python PNR_PYTHON names when this one has no numpy. ``terms`` (net -> Terminal
    fields) replaces the default (a via of 0.2 mm radius under each of ``pads``)."""
    if terms is None:
        terms = {
            net: [
                dict(name="%s%d.1" % (net, n), kind="via", at=list(p), radius=0.2)
                for n, p in enumerate(points)
            ]
            for net, points in pads.items()
        }
    payload = dict(
        entry=entry,
        terms=terms,
        blocked=[[list(c), r] for c, r in blocked],
        w=W,
        h=H,
        fill_min=fill_min,
    )
    if importlib.util.find_spec("numpy") is not None:
        from pnr.plane_partition import _CACHE, Terminal, partition

        _CACHE.clear()
        part = partition(
            entry,
            width=W,
            height=H,
            terminals={n: [Terminal(**t) for t in ts] for n, ts in terms.items()},
            blocked=blocked,
            fill_min_mm=fill_min,
        )
        return part.rows()
    python = os.environ.get("PNR_PYTHON")
    if not python:
        raise unittest.SkipTest("no numpy here and no PNR_PYTHON")
    root = str(Path(__file__).resolve().parents[1])  # hardware/pnr (the pnr package)
    paths = [root] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths))
    out = subprocess.run(
        [python, "-c", _PARTITION],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if out.returncode:
        raise AssertionError("the partition in PNR_PYTHON failed:\n" + out.stderr[-3000:])
    return json.loads(out.stdout)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class DrawTest(unittest.TestCase):
    def test_zones_fill_apart_and_the_rails_are_whole(self):
        import pcbnew as k

        from pnr.ir_extract import report
        from pnr.writeback import draw_plane_regions

        b, pads, v = board()
        blocked = [((x, 6.0), 0.3) for x in (6.0, 8.0, 12.0, 14.0)]
        entry = dict(
            layer="In2.Cu",
            nets=["A", "B"],
            order="current",
            split_gap_mm=0.3,
            min_width_mm=1.0,
            fill="GND",
            core_no_vias=True,
            terminal_reach_mm=0.8,
            h_mm=0.1,
        )
        rows = partition_rows(entry, pads, blocked)
        rules = dict(fab=dict(clearance_mm=0.1, track_width_mm=0.1))
        full = [v(0, 0), v(W, 0), v(W, H), v(0, H)]
        made = draw_plane_regions(b, rows, rules, lambda p: v(*p), full)
        self.assertEqual(sorted(z.GetNetname() for z in made), ["A", "B", "GND"])
        # Drawn again (a placed board whose writeback drew the rails' zones, then the
        # routed append): the rails' single-layer zones on the layer are replaced.
        made = draw_plane_regions(b, rows, rules, lambda p: v(*p), full)
        lid = b.GetLayerID("In2.Cu")
        rails = [z for z in b.Zones() if z.IsOnLayer(lid) and z.GetNetname() in ("A", "B")]
        self.assertEqual(len(rails), len([z for z in made if z.GetNetname() in ("A", "B")]))
        k.ZONE_FILLER(b).Fill(b.Zones())
        lid = b.GetLayerID("In2.Cu")
        fills = {z.GetNetname(): z.GetFilledPolysList(lid) for z in made}
        for net in ("A", "B"):
            self.assertGreater(fills[net].Area(), 50e12, net)
            self.assertEqual(fills[net].OutlineCount(), 1, net)  # one piece
        overlap = k.SHAPE_POLY_SET(fills["A"])
        overlap.BooleanIntersection(fills["B"])
        self.assertEqual(overlap.Area(), 0)
        overlap = k.SHAPE_POLY_SET(fills["A"])
        overlap.BooleanIntersection(fills["GND"])
        self.assertEqual(overlap.Area(), 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "part.kicad_pcb")
            k.SaveBoard(path, b)
            b = k.LoadBoard(path)
            rules["ir_drop"] = [
                dict(net=net, sources=["%s0:1" % net], sinks="all", current_a=1.0)
                for net in ("A", "B")
            ]
            result = report(b, rules, Path(tmp) / "ir", path)
        for net in ("A", "B"):
            self.assertEqual(result[net]["status"], "pass", result[net].get("opens"))


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class ThroughLandTest(unittest.TestCase):
    """A rail that does not own the leftover, with a square through-hole terminal
    (pnr.plane_partition.through_land): KiCad relieves the pad about its outline, and
    the rail's zone still fills as one piece round the relief. Modelled as the disc
    inside the pad (before 2026-10-07), the territory left the relief's corners out
    and the fill fell into three pieces, two of them slivers held by one spoke each
    (the -rails rung's VBAT on J7.1)."""

    def test_the_fill_round_a_square_pad_is_one_piece(self):
        import pcbnew as k

        from pnr.writeback import draw_plane_regions

        b, _pads, v = board()
        for z in list(b.Zones()):
            b.Delete(z)
        net_b = b.FindNet("B")
        f = k.FOOTPRINT(b)
        f.SetReference("J7")
        b.Add(f)
        f.SetPosition(v(2.5, 3.0))
        p = k.PAD(f)
        p.SetNumber("1")
        p.SetAttribute(k.PAD_ATTRIB_PTH)
        p.SetShape(k.PAD_SHAPE_RECT)
        p.SetSize(k.VECTOR2I(1700000, 1700000))
        p.SetDrillSize(k.VECTOR2I(1000000, 1000000))
        p.SetLayerSet(k.PAD.PTHMask())
        p.SetPosition(v(2.5, 3.0))
        p.SetNet(net_b)
        f.Add(p)
        # A's terminals: the vias of its first two pads; B's: J7.1 and the via of its
        # third pad (10, 7.5).
        terms = dict(
            A=[
                dict(name="A0.1", kind="via", at=[2.0, 2.0], radius=0.2),
                dict(name="A1.1", kind="via", at=[18.0, 2.0], radius=0.2),
                dict(name="A2.1", kind="via", at=[10.0, 4.5], radius=0.2),
            ],
            B=[
                dict(name="J7.1", kind="land", at=[2.5, 3.0], radius=1.2021, size=[1.7, 1.7]),
                dict(name="B2.1", kind="via", at=[10.0, 7.5], radius=0.2),
            ],
        )
        # B's pads B0, B1 sit in A's leftover here: off the layer for this test.
        for t in list(b.GetTracks()):
            q = t.GetPosition()
            if t.GetClass() == "PCB_VIA" and t.GetNetname() == "B":
                if (q.x, q.y) != (v(10.0, 7.5).x, v(10.0, 7.5).y):
                    b.Delete(t)
            elif t.GetClass() == "PCB_VIA" and t.GetNetname() == "A":
                if (q.x, q.y) == (v(2.0, 2.0).x, v(2.0, 2.0).y):
                    b.Delete(t)  # under J7.1's land
        terms["A"] = terms["A"][1:]
        entry = dict(
            layer="In2.Cu",
            nets=["A", "B"],
            order="current",
            split_gap_mm=0.3,
            min_width_mm=1.0,
            fill="A",
            terminal_reach_mm=0.8,
            h_mm=0.1,
        )
        rows = partition_rows(entry, None, [], terms=terms, fill_min=0.1)
        rules = dict(fab=dict(clearance_mm=0.1, track_width_mm=0.1))
        full = [v(0, 0), v(W, 0), v(W, H), v(0, H)]
        made = draw_plane_regions(b, rows, rules, lambda q: v(*q), full)
        k.ZONE_FILLER(b).Fill(b.Zones())
        lid = b.GetLayerID("In2.Cu")
        fills = {z.GetNetname(): z.GetFilledPolysList(lid) for z in made}
        self.assertEqual(fills["B"].OutlineCount(), 1, "B fills in pieces round J7.1")
        self.assertEqual(fills["A"].OutlineCount(), 1)
        # The ladder's judges of a partition on the drawn board: one piece per rail
        # (rail_zones) and no region the judge's raster sees serving a lone terminal
        # (plane_quality: a relief ring narrower than its raster fell apart there).
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))
        from check_constraints import run_checks

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "through.kicad_pcb")
            k.SaveBoard(path, b)
            spec = dict(
                checks=[
                    dict(
                        id="rails",
                        kind="rail_zones",
                        layer="In2.Cu",
                        nets=["A", "B"],
                        candidates=True,
                        min_area_mm2=1.0,
                    ),
                    dict(
                        id="quality",
                        kind="plane_quality",
                        layer="In2.Cu",
                        candidates=["A", "B"],
                        currents={"A": 1.0, "B": 0.01},
                        min_width_mm=1.0,
                    ),
                ]
            )
            result = run_checks(path, spec)
        for row in result["checks"]:
            self.assertEqual(row["status"], "satisfied", row)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class CheckerTest(unittest.TestCase):
    """The constraint checker's supply checks (regression/check_constraints.py) on the
    drawn partition: rail_zones, ir_drop and unconnected."""

    def test_supply_checks(self):
        import pcbnew as k

        from pnr.writeback import draw_plane_regions

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))
        from check_constraints import run_checks

        b, pads, v = board()
        # A's last pad loses its via: it is cut off from the rest of A.
        cut = pads["A"][2]
        for t in list(b.GetTracks()):
            p = t.GetPosition()
            if t.GetClass() == "PCB_VIA" and (p.x, p.y) == (v(*cut).x, v(*cut).y):
                b.Delete(t)
        entry = dict(
            layer="In2.Cu",
            nets=["A", "B"],
            order="current",
            split_gap_mm=0.3,
            min_width_mm=1.0,
            fill="GND",
            core_no_vias=True,
            terminal_reach_mm=0.8,
            h_mm=0.1,
        )
        rows = partition_rows(entry, pads, [])
        rules = dict(fab=dict(clearance_mm=0.1, track_width_mm=0.1))
        full = [v(0, 0), v(W, 0), v(W, H), v(0, H)]
        draw_plane_regions(b, rows, rules, lambda p: v(*p), full)
        k.ZONE_FILLER(b).Fill(b.Zones())
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "checks.kicad_pcb")
            k.SaveBoard(path, b)
            spec = dict(
                checks=[
                    dict(
                        id="rails",
                        kind="rail_zones",
                        layer="In2.Cu",
                        nets=["A", "B"],
                        fill="GND",
                        min_area_mm2=1.0,
                    ),
                    dict(
                        id="ir-B",
                        kind="ir_drop",
                        net="B",
                        sources=["B0:1"],
                        sinks="all",
                        current_a=1.0,
                        budget_mv=50.0,
                    ),
                    dict(
                        id="ir-A",
                        kind="ir_drop",
                        net="A",
                        sources=["A0:1"],
                        sinks="all",
                        current_a=1.0,
                        budget_mv=50.0,
                    ),
                    dict(id="cut", kind="unconnected", pads=["A2.1"]),
                ]
            )
            result = run_checks(path, spec)
        status = {r["id"]: r["status"] for r in result["checks"]}
        self.assertEqual(status["rails"], "satisfied", result["checks"][0])
        self.assertEqual(status["ir-B"], "satisfied", result["checks"][1])
        self.assertEqual(status["ir-A"], "violated")  # A2.1 has no copper to the plane: open
        self.assertEqual(status["cut"], "satisfied", result["checks"][3])


if __name__ == "__main__":
    unittest.main()
