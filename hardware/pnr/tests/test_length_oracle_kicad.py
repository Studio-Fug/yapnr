"""pnr.length_model against KiCad itself (KiCad Python and kicad-cli only).

Probe boards hold one two-pin net routed in a way the model must get right; KiCad's
DRC measures each (pnr.length_oracle, a ``length`` rule per net) and the model,
reading the same board (pnr.quality.kicad_net_lengths), must agree within 1 um.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

# A headless kicad-cli only (AGENTS.md: never the stock macOS application bundle,
# whose every call registers a Dock icon); skipped without one.
CLI = os.environ.get("KICAD_CLI") or os.environ.get("PNR_KICAD_CLI", "")
NATIVE = importlib.util.find_spec("pcbnew") is not None and bool(CLI) and Path(CLI).exists()


def build(path, chains, vias, layers=2, pad_b_layer="F", pad_b_angle=0.0):
    """A board with SMD pads A (1.0 x 1.4 mm) and B (1.4 x 1.0 mm, on ``pad_b_layer``,
    turned by ``pad_b_angle`` degrees) of net N, the ``chains`` [(layer, [points])]
    and ``vias`` [(x, y)] of N."""
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(layers)
    mm = lambda v: round(v * 1e6)  # noqa: E731
    V = lambda x, y: k.VECTOR2I(mm(x), mm(y))  # noqa: E731
    net = k.NETINFO_ITEM(b, "N")
    b.Add(net)
    for a, z in [((0, 0), (40, 0)), ((40, 0), (40, 30)), ((40, 30), (0, 30)), ((0, 30), (0, 0))]:
        s = k.PCB_SHAPE(b)
        s.SetShape(k.SHAPE_T_SEGMENT)
        s.SetStart(V(*a))
        s.SetEnd(V(*z))
        s.SetLayer(k.Edge_Cuts)
        s.SetWidth(mm(0.1))
        b.Add(s)
    layer = {"F": k.F_Cu, "B": k.B_Cu, "In1": k.In1_Cu, "In2": k.In2_Cu}
    for ref, at, size, side in (("A", (10, 10), (1.0, 1.4), "F"), ("B", (30, 14), (1.4, 1.0), "F")):
        side = pad_b_layer if ref == "B" else side
        fp = k.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(V(*at))
        pad = k.PAD(fp)
        pad.SetNumber("1")
        pad.SetAttribute(k.PAD_ATTRIB_SMD)
        pad.SetShape(k.PAD_SHAPE_ROUNDRECT)
        pad.SetSize(V(*size))
        pad.SetRoundRectRadiusRatio(0.25)
        cu = k.LSET()
        cu.AddLayer(layer[side])
        pad.SetLayerSet(cu)
        pad.SetPosition(V(*at))
        if ref == "B" and pad_b_angle:
            pad.SetOrientationDegrees(pad_b_angle)
        pad.SetNet(net)
        fp.Add(pad)
        b.Add(fp)
    for name, pts in chains:
        for a, z in zip(pts, pts[1:]):
            t = k.PCB_TRACK(b)
            t.SetStart(V(*a))
            t.SetEnd(V(*z))
            t.SetWidth(mm(0.25))
            t.SetLayer(layer[name])
            t.SetNet(net)
            b.Add(t)
    for x, y in vias:
        v = k.PCB_VIA(b)
        v.SetPosition(V(x, y))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetNet(net)
        b.Add(v)
    b.Save(str(path))
    Path(path).with_suffix(".kicad_pro").write_text("{}\n")


@unittest.skipUnless(NATIVE, "needs KiCad's pcbnew and KICAD_CLI or PNR_KICAD_CLI")
class OracleAgreementTest(unittest.TestCase):
    A, B = (10.0, 10.0), (30.0, 14.0)

    def agree(self, chains, vias=(), **kw):
        import pcbnew

        from pnr import length_oracle, quality

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.kicad_pcb"
            build(path, chains, vias, **kw)
            board = pcbnew.LoadBoard(str(path))
            model, _vias, _ = quality.kicad_net_lengths(board, path.read_text(), ["N"])
            kicad = length_oracle.measure(path, CLI, ["N"])
        self.assertAlmostEqual(model["N"], kicad["N"], delta=1e-3)
        return kicad["N"]

    def test_routes(self):
        a, b = self.A, self.B

        def off(dx, dy):
            return (a[0] + dx, a[1] + dy)

        near, far = off(0.2, -0.125), off(0.3, -0.55)
        hook = [a, off(-0.0375, 0.0375), off(-0.0375, 0.125)]
        mid = ((a[0] + b[0]) / 2, a[1] - 3)
        cases = {
            "straight": ([("F", [a, b])], []),
            "dogleg": ([("F", [a, (b[0], a[1]), b])], []),
            "wander-in-pad": ([("F", [a, off(0, 0.3), off(0.3, 0.3), off(0.3, 2.0), b])], []),
            "two-vias": (
                [("F", [a, mid]), ("B", [mid, (b[0], mid[1])]), ("F", [(b[0], mid[1]), b])],
                [mid, (b[0], mid[1])],
            ),
            "via-in-pad": ([("B", [a, b])], [a, b]),
            "hook-to-near-via": ([("F", hook + [near]), ("B", [near, b])], [near, b]),
            "hook-to-far-via": ([("F", hook + [far]), ("B", [far, b])], [far, b]),
            "stub-via": ([("F", [a, mid, b])], [mid]),
            "meander": (
                [
                    (
                        "F",
                        [
                            a,
                            (15, 10),
                            (15, 12),
                            (15.5, 12),
                            (15.5, 10),
                            (16, 10),
                            (16, 12),
                            (16.5, 12),
                            (16.5, 10),
                            b,
                        ],
                    )
                ],
                [],
            ),
        }
        for name, (chains, vias) in cases.items():
            with self.subTest(name):
                self.agree(chains, vias)

    def test_bottom_and_turned_pads(self):
        a, b = self.A, self.B
        mid = (20.0, 12.0)
        # B on the bottom: the line reaches it on B.Cu through a via, wandering
        # inside the pad; then B turned 30 degrees, entered off its axis.
        wander = [mid, (b[0] - 3, b[1]), (b[0] - 0.2, b[1] + 0.3), (b[0] + 0.3, b[1] + 0.2), b]
        self.agree([("F", [a, mid]), ("B", wander)], [mid], pad_b_layer="B")
        off_axis = [a, (b[0] - 2, b[1] + 1.0), (b[0] - 0.3, b[1] + 0.25), b]
        for angle in (30.0, 90.0):
            with self.subTest(angle=angle):
                self.agree([("F", off_axis)], [], pad_b_angle=angle)

    def test_inner_layer_via(self):
        a, b = self.A, self.B
        mid = (20.0, 10.0)
        self.agree([("F", [a, mid]), ("In1", [mid, b])], [mid, b], layers=4, pad_b_layer="F")


if __name__ == "__main__":
    unittest.main()
