"""PNR_SKIP_PASTE_PADS: a footprint's paste-only pads are not copper.

An exposed pad's solder-paste windows are drawn as pads on no copper layer (the
LAN8742A's QFN-24 on 12-soc-bga-113 has four). Ingest took them for SMD lands of no
net, so the router kept vias 0.127 mm off them: no cell inside the exposed pad
held a via, the pad had no drop for its plane net, and the net stayed open. The
ingest side (KiCad's Python) is tests/test_paste_pads_kicad.py.
"""

import unittest

from pnr import fab_profile
from pnr.place.geometry import Rect
from pnr.route.detail.grid import RouteGrid

EP = (5.0, 5.0)


def in_pad():
    return fab_profile.geometry(name="jlc-pofv").in_pad


def grid(paste_windows):
    """A 2.6 mm exposed pad of GND, with the four 1.05 mm paste windows ingest
    recorded as SMD lands of no net when ``paste_windows``."""
    g = RouteGrid(10, 10, 0.25, clearance=0.09, track_width=0.1, via_radius=0.2)
    lands = [("GND", Rect(EP[0], EP[1], 2.6, 2.6), 0.0)]
    if paste_windows:
        lands += [
            ("", Rect(EP[0] + dx, EP[1] + dy, 1.05, 1.05), 0.25)
            for dx in (-0.65, 0.65)
            for dy in (-0.65, 0.65)
        ]
    for net, r, corner in lands:
        g.add_pad(0, net, r)
        g.smd_pads.append((0, net, r, corner))
    g.restrict_smd_vias(in_pad(), 0.127)
    return g


class PasteWindowGridTest(unittest.TestCase):
    def test_paste_windows_bar_every_via_in_the_exposed_pad(self):
        g = grid(paste_windows=True)
        self.assertFalse(g.smd_via_ok(EP, "GND"))
        i, j = g.cell_of(*EP)
        self.assertTrue(g.smd_via_blocked[j, i])

    def test_without_them_the_pad_takes_a_filled_via_of_its_net(self):
        g = grid(paste_windows=False)
        self.assertTrue(g.smd_via_ok(EP, "GND"))
        self.assertFalse(g.smd_via_ok(EP, "SIG"))


if __name__ == "__main__":
    unittest.main()
