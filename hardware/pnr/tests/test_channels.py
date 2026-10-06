"""Routing pressure must track nets and rules, not just footprint density."""

import unittest

from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.place.channels import ChannelModel, signal_layers
from pnr.place.legalize import legalize, refine_channels
from pnr.place.metrics import overlap_pairs


def example():
    a = Component(
        "A",
        "qfn",
        (5, 5),
        0,
        "top",
        (2, 3),
        (2, 3),
        pads=[Pad(str(i), "n" + str(i), (1, i * 0.5 - 0.75), (0.2, 0.2)) for i in range(4)],
    )
    b = Component(
        "B", "body", (7.5, 5), 0, "top", (2, 3), (2, 3), pads=[Pad("1", "", (-1, 0), (0.2, 2))]
    )
    return BoardGraph(
        "test", [a, b], [Net("n" + str(i), i, [("A", str(i)), ("C", str(i))]) for i in range(4)]
    )


class ChannelTests(unittest.TestCase):
    def test_legalizer_recovers_an_unplaceable_global_orientation(self):
        a = Component("A", "body", (2, 1), 90, "top", (4, 2), (4, 2))
        result = legalize(
            BoardGraph("turn", [a], []),
            4,
            2,
            fixed={},
            keepouts=[],
            grid_mm=0.25,
            clearance=0,
            allow_rotation=True,
        )
        self.assertEqual(result.component("A").rot % 180, 0)
        self.assertFalse(overlap_pairs(result))

    def test_group_packer_reserves_the_only_legal_site(self):
        a = Component("A", "body", (1, 1), 0, "top", (2, 2), (2, 2))
        b = Component("B", "body", (1, 1), 0, "top", (2, 2), (2, 2))
        graph = BoardGraph("packing", [a, b], [])
        placed = legalize(
            graph,
            4,
            2,
            fixed={},
            keepouts=[],
            clearance=0,
            grid_mm=1,
            group_limits={"A": [(2, 1, 1)], "B": [(0, 1, 1.5)]},
        )
        self.assertEqual(placed.component("B").pos, (1, 1))
        self.assertEqual(placed.component("A").pos, (3, 1))
        self.assertFalse(overlap_pairs(placed))

    def test_track_count_width_and_spacing(self):
        model = ChannelModel(example(), {"default_clearance_mm": 0.15})
        self.assertAlmostEqual(model.demand({"n0", "n1", "n2", "n3"}), 1.55)
        self.assertAlmostEqual(model.demand({"n0"}), 0.5)

    def test_width_classes_and_usb_pair(self):
        model = ChannelModel(
            example(),
            {
                "default_clearance_mm": 0.15,
                "net_classes": [{"nets": ["power"], "width_mm": 1.5}],
                "diff_pairs": [{"p": "dp", "n": "dn", "width_mm": 0.2, "gap_mm": 0.25}],
            },
        )
        self.assertAlmostEqual(model.demand({"power", "n0"}), 2.15)
        self.assertAlmostEqual(model.demand({"dp", "dn"}), 0.95)
        # A pair that leaves width and gap to the defaults: fab track, clearance.
        model = ChannelModel(
            example(),
            {
                "default_clearance_mm": 0.15,
                "diff_pairs": [{"p": "dp", "n": "dn", "width_mm": None, "gap_mm": None}],
            },
        )
        self.assertAlmostEqual(model.demand({"dp", "dn"}), 2 * 0.2 + 0.15 + 2 * 0.15)

    def test_duplicate_pads_do_not_add_tracks(self):
        graph = example()
        baseline = ChannelModel(graph, {}).report(graph)
        graph.components[0].pads.append(Pad("copy", "n0", (1, 0.25), (0.2, 0.2)))
        self.assertEqual(ChannelModel(graph, {}).report(graph), baseline)

    def test_direct_local_net_does_not_require_longitudinal_channel(self):
        graph = example()
        for net in graph.nets:
            net.pins = [("A", "1"), ("B", "1")]
        self.assertEqual(ChannelModel(graph, {}).report(graph)["channels"], [])

    def test_plane_needs_via_corridor(self):
        model = ChannelModel(
            example(),
            {
                "default_clearance_mm": 0.15,
                "net_classes": [{"nets": ["ground"], "plane_layer": "In1.Cu"}],
            },
        )
        self.assertAlmostEqual(model.demand({"ground"}), 0.9)

    def test_shared_external_net_still_needs_one_escape(self):
        graph = example()
        for pad in graph.component("A").pads:
            pad.net = "n0"
        graph.component("B").pads[0].net = "n0"
        graph.nets[0].pins.append(("B", "1"))
        channels = ChannelModel(graph, {"default_clearance_mm": 0.15}).report(graph)["channels"]
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["nets"], ["n0"])
        self.assertAlmostEqual(channels[0]["required_mm"], 0.5)

    def test_rotated_rows_and_opposite_sides(self):
        graph = example()
        initial = ChannelModel(graph, {}).report(graph)["shortage_score"]
        for c in graph.components:
            c.pos = (10 - c.pos[1], c.pos[0])
            c.rot = 90
        self.assertAlmostEqual(ChannelModel(graph, {}).report(graph)["shortage_score"], initial)
        graph.components[1].side = "bottom"
        self.assertEqual(ChannelModel(graph, {}).report(graph)["channels"], [])

    def test_fixed_shortage_is_visible(self):
        graph = example()
        report = ChannelModel(graph, {}).report(graph, {"A", "B"})
        self.assertTrue(report["channels"][0]["both_fixed"])

    def test_partial_row_does_not_count_unobstructed_pads(self):
        graph = example()
        graph.component("B").pads[0].size = (0.2, 0.2)
        self.assertEqual(ChannelModel(graph, {}).report(graph)["channels"], [])

    def test_pressure_changes_legal_placement(self):
        graph = example()
        model = ChannelModel(graph, {})
        options = dict(fixed={"A": (5, 5)}, keepouts=[], grid_mm=0.1)
        old = legalize(graph, 15, 15, **options)
        new = legalize(graph, 15, 15, channel_model=model, **options)
        self.assertLess(model.report(new)["shortage_score"], model.report(old)["shortage_score"])
        self.assertEqual(overlap_pairs(new), [])
        self.assertEqual(new.component("A").pos, (5, 5))

    def test_checkpoint_refinement_is_bounded_and_preserves_clear_parts(self):
        import math

        graph = example()
        graph.components.append(Component("Z", "body", (12, 12), 0, "top", (1, 1), (1, 1)))
        model = ChannelModel(graph, {})
        new = refine_channels(
            graph, 15, 15, fixed={"A": (5, 5)}, keepouts=[], channel_model=model, max_move_mm=2
        )
        self.assertLess(model.report(new)["shortage_score"], model.report(graph)["shortage_score"])
        self.assertEqual(new.component("Z").pos, (12, 12))
        self.assertEqual(new.component("A").pos, (5, 5))
        self.assertEqual(graph.component("B").pos, (7.5, 5))
        self.assertLessEqual(math.dist(new.component("B").pos, (7.5, 5)), 2)
        self.assertEqual(overlap_pairs(new), [])


TWO_LAYERS = {"default_clearance_mm": 0.15, "layers": 2}


class LayeredChannelTests(unittest.TestCase):
    """PNR_CHANNEL_LAYERS: the other signal layers take a share of a face's nets."""

    def east(self, graph, rules, layers=True):
        model = ChannelModel(graph, rules, layers=layers)
        (channel,) = model.report(graph)["channels"]
        return channel["required_mm"]

    def test_signal_layers_leave_planes_out(self):
        self.assertEqual(signal_layers({"layers": 2}), 2)
        self.assertEqual(signal_layers({}), 2)
        stack = {
            "layers": [
                {"name": name, "kind": "copper"} for name in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
            ],
            "planes": ["In1.Cu"],
        }
        rules = {
            "layers": 4,
            "stackup": stack,
            "net_classes": [{"nets": ["VCC"], "plane_layer": "In2.Cu"}],
        }
        self.assertEqual(signal_layers(rules), 2)
        self.assertEqual(signal_layers({"layers": 1}), 1)

    def test_off_and_single_layer_keep_the_surface_demand(self):
        graph = example()
        self.assertAlmostEqual(self.east(graph, TWO_LAYERS, layers=False), 1.55)
        self.assertAlmostEqual(self.east(graph, {"default_clearance_mm": 0.15, "layers": 1}), 1.55)
        # The flag reads the environment when the caller does not say.
        self.assertEqual(ChannelModel(graph, TWO_LAYERS).share, 1.0)

    def test_corner_pads_drop_off_the_channel_middle_pads_share_it(self):
        # n0 and n3 are corner pads (also on the south/north face): their vias can go
        # out that way, so they ask half a track on two signal layers; n1 and n2 sit
        # mid-row and stay on the surface: 0.2 + 0.2 + 0.1 + 0.1 + (3 + 1) * 0.15.
        self.assertAlmostEqual(self.east(example(), TWO_LAYERS), 1.2)

    def test_a_via_row_pays_for_the_mid_row_drops(self):
        graph = example()
        # A long row whose corner pads are unconnected: every net sits mid-row, so
        # every drop needs a via in the channel.
        graph.component("A").pads = [
            Pad(str(i), "n" + str(i % 4) if 0 < i < 8 else "", (1, i * 0.25 - 1.0), (0.2, 0.1))
            for i in range(9)
        ]
        surface = self.east(graph, TWO_LAYERS, layers=False)
        self.assertAlmostEqual(surface, 4 * 0.2 + 5 * 0.15)
        # Half of 4 tracks plus one via row (0.6 + 0.15) is dearer than the surface.
        self.assertAlmostEqual(self.east(graph, TWO_LAYERS), surface)
        rules = dict(TWO_LAYERS, fab={"via_diameter_mm": 0.3})
        self.assertAlmostEqual(self.east(graph, rules), 2 * 0.2 + 3 * 0.15 + 0.45)

    def test_through_hole_and_in_pad_drops_are_free(self):
        graph = example()
        for pad in graph.component("A").pads:
            pad.through_hole = True
        self.assertAlmostEqual(self.east(graph, TWO_LAYERS), 0.4 + 3 * 0.15)
        graph = example()
        for pad in graph.component("A").pads:
            pad.size = (0.6, 0.6)
            pad.land_corner = 0.0
        in_pad = {
            "filled": True,
            "diameter_mm": 0.3,
            "drill_mm": 0.15,
            "min_hole_edge_to_pad_edge_mm": 0.05,
            "min_row_pitch_mm": 0.5,
        }
        rules = dict(TWO_LAYERS, fab={"via_classes": {"in_pad": in_pad}})
        self.assertAlmostEqual(self.east(graph, rules), 0.4 + 3 * 0.15)

    def test_more_signal_layers_ask_less_and_planes_are_unchanged(self):
        stack = {
            "layers": [{"name": "L%d" % i, "kind": "copper"} for i in range(6)],
            "planes": ["L1", "L4"],
        }
        rules = dict(TWO_LAYERS, layers=6, stackup=stack)
        self.assertLess(self.east(example(), rules), self.east(example(), TWO_LAYERS))
        rules = dict(TWO_LAYERS, net_classes=[{"nets": ["ground"], "plane_layer": "In1.Cu"}])
        self.assertAlmostEqual(ChannelModel(example(), rules, layers=True).demand({"ground"}), 0.9)

    def test_layered_never_exceeds_the_surface_demand(self):
        graph = example()
        for rules in (TWO_LAYERS, dict(TWO_LAYERS, layers=8)):
            on = ChannelModel(graph, rules, layers=True)
            off = ChannelModel(graph, rules, layers=False)
            for nets in ({"n0"}, {"n1", "n2"}, {"n0", "n1", "n2", "n3"}):
                self.assertLessEqual(on.demand(nets), off.demand(nets) + 1e-9)
            face = on.shape(graph.component("A"))[4][1]
            self.assertEqual(face.free, {"n0", "n3"})
            self.assertLessEqual(on.demand(face), off.demand(face))


if __name__ == "__main__":
    unittest.main()
