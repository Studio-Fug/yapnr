"""Joint terminal access never walls a selected exit in (pnr.route.detail.joint_escape).

The joint search only sees pairwise conflicts between options: it may choose a plane
drop whose via and halo close the last gap out of a neighbouring signal pad (a
connector's ground drop sealing the middle pin of a 0.65 mm row under the jlc-pofv
profile's smaller vias, 09-mcu-usb-31-4L). ``_select_unsealed`` re-selects with the
walling options banned for that exit; a stub that crosses another land of its own net
is priced as a last resort.
"""

import unittest

from pnr.place.geometry import Rect
from pnr.route.detail.joint_escape import (
    GRAZE_COST,
    AccessOption,
    _grazes_own_lands,
    _sealed_access,
    _select_unsealed,
)


class Box:
    """A one-layer grid: ``walls`` are static copper, ``vias`` the legal via sites."""

    nlayers = 1

    def __init__(self, nx, ny, walls, vias):
        self.nx, self.ny = nx, ny
        self.walls, self.vias = set(walls), set(vias)

    def in_bounds(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny

    def passable(self, layer, i, j, net=None):
        return self.in_bounds(i, j) and (i, j) not in self.walls

    def via_passable(self, layer, i, j, net=None, point=None):
        return self.passable(layer, i, j, net) and (i, j) in self.vias


def option(net, cell, cost, occupied=(), vias=()):
    """An exit at ``cell`` that occupies ``occupied`` cells (no copper geometry: the
    pairwise test sees no conflict between options far apart)."""
    i, j = cell
    return AccessOption(
        escape=type("E", (), {"net": net})(),
        cost=cost,
        segments=(),
        vias=tuple(vias),
        occupied=frozenset((0, a, b) for a, b in occupied),
        access_key=(0, i, j),
        bounds=(10.0 * i, 10.0 * j, 10.0 * i + 1, 10.0 * j + 1),
    )


def pocket():
    """A signal pad S at (1, 2) in a pocket whose only opening is (2, 2); a via site
    at (9, 2). The drop D (another net) may stand in the opening (cheap) or at (8, 4)."""
    walls = {(i, j) for i in range(0, 3) for j in (1, 3)} | {(0, 2)}
    grid = Box(10, 5, walls, {(9, 2)})
    options = {
        "S": [option("SIG", (1, 2), 0.0)],
        "D": [
            option("GND", (2, 2), 1.0, occupied=[(2, 2)], vias=[(0.5, 0.5)]),
            option("GND", (8, 4), 2.0, occupied=[(8, 4)], vias=[(2.1, 1.1)]),
        ],
    }
    terminals = {"S": ("SIG", (0.0, 0.0), 0), "D": ("GND", (0.0, 0.0), 0)}
    bounds = {k: opts[0].bounds for k, opts in options.items()}
    return grid, options, terminals, bounds


class SealTest(unittest.TestCase):
    def test_the_cheap_drop_seals_the_pad_and_is_moved(self):
        grid, options, terminals, bounds = pocket()
        sealed = _sealed_access(grid, options, terminals, {"D"}, {"S": 0, "D": 0})
        self.assertEqual(list(sealed), ["S"])
        self.assertEqual(sealed["S"][0], ["D"])
        selection, report = _select_unsealed(
            grid, options, terminals, {"D"}, bounds, max_states=1000, max_cluster_size=24
        )
        self.assertEqual(selection.selected, {"S": 0, "D": 1})
        self.assertEqual(report["found"], {"S": ["D"]})
        self.assertEqual(report["left"], [])
        self.assertFalse(_sealed_access(grid, options, terminals, {"D"}, selection.selected))

    def test_nothing_sealed_keeps_the_selection_and_reports_nothing(self):
        grid, options, terminals, bounds = pocket()
        grid.walls.discard((2, 1))  # a second way out of the pocket
        selection, report = _select_unsealed(
            grid, options, terminals, {"D"}, bounds, max_states=1000, max_cluster_size=24
        )
        self.assertEqual(selection.selected, {"S": 0, "D": 0})
        self.assertEqual(report, {})

    def test_static_seal_names_no_wall_and_keeps_the_selection(self):
        grid, options, terminals, bounds = pocket()
        grid.walls.add((2, 2))  # static copper closes the pocket: no option to move
        options["D"][0] = option("GND", (5, 4), 1.0, occupied=[(5, 4)])
        selection, report = _select_unsealed(
            grid, options, terminals, {"D"}, bounds, max_states=1000, max_cluster_size=24
        )
        self.assertEqual(selection.selected, {"S": 0, "D": 0})
        self.assertEqual(report["found"], {"S": []})
        self.assertEqual(report["left"], ["S"])

    def test_a_reselection_that_loses_a_terminal_is_not_taken(self):
        grid, options, terminals, bounds = pocket()
        options["D"] = options["D"][:1]  # the drop has no other site
        selection, report = _select_unsealed(
            grid, options, terminals, {"D"}, bounds, max_states=1000, max_cluster_size=24
        )
        self.assertEqual(selection.selected, {"S": 0, "D": 0})
        self.assertEqual(report["left"], ["S"])

    def test_a_same_net_terminal_or_via_site_in_reach_is_an_exit(self):
        grid, options, terminals, bounds = pocket()
        grid.vias.add((1, 2))
        self.assertFalse(_sealed_access(grid, options, terminals, {"D"}, {"S": 0, "D": 0}))
        grid, options, terminals, bounds = pocket()
        options["T"] = [option("SIG", (1, 2), 0.0)]
        options["S"] = [option("SIG", (2, 2), 0.0)]
        options["D"] = [option("GND", (5, 4), 1.0, occupied=[(5, 4)])]
        terminals["T"] = ("SIG", (0.0, 0.0), 0)
        grid.walls |= {(3, 1), (3, 3), (3, 2)}
        self.assertFalse(_sealed_access(grid, options, terminals, {"D"}, {"S": 0, "T": 0, "D": 0}))


class GrazeTest(unittest.TestCase):
    def test_a_stub_over_another_land_of_its_net_grazes_it(self):
        own = Rect(0.0, 0.0, 1.0, 0.2)
        other = Rect(0.05, -0.675, 0.25, 0.55)
        foreign = Rect(-0.45, -0.675, 0.25, 0.55)
        grid = type("G", (), {"smd_pads": [(0, "GND", own, 0.0), (0, "GND", other, 0.0)]})()
        grid.smd_pads.append((0, "VIN", foreign, 0.0))
        across = [(0, (0.0, 0.0), (0.5, -1.07), 0.3)]
        away = [(0, (0.0, 0.0), (0.0, 1.2), 0.3)]
        self.assertTrue(_grazes_own_lands(grid, "GND", 0, across, own))
        self.assertFalse(_grazes_own_lands(grid, "GND", 0, away, own))
        self.assertFalse(_grazes_own_lands(grid, "GND", 1, across, own))  # other layer
        self.assertFalse(_grazes_own_lands(grid, "VIN", 0, across, foreign))
        self.assertGreater(GRAZE_COST, 3.0)  # dearer than one more via


if __name__ == "__main__":
    unittest.main()
