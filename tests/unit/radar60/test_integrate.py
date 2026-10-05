"""examples/radar60/board/integrate.py's pure-Python pieces (no KiCad, no engine): the
stage3c wave-1 review's findings #1 and #3.

Finding #1: ``kicad_ops.measure`` summed copper length without checking the net was actually
connected, so a net with a stub on one pad (copper > 0, still open) read as "routed". The fix
is ``integrate._unconnected_nets``, which turns an independent DRC's own ``unconnected_items``
into the net-name ground truth that ``measure`` (and ``step_check``'s QSPI/LVDS counts) must
judge connectivity against, never a length threshold.

Finding #3: kicad-cli's ``--severity-all`` does not promote checks whose project-file default
severity is "ignore" (``footprint_filters_mismatch``, ``footprint_type_mismatch``,
``missing_courtyard``, ``track_not_centered_on_via``, ``tuning_profile_track_geometries``), so a
board with real violations of those types still DRCs "clean". ``integrate._DRC_IGNORED_BY_DEFAULT``
names them so ``step_check`` can report them, instead of a "0 violations" claim silently
depending on which checks kicad-cli ever ran. This test pins the list against the exact five
types the review found hidden on the routed radar60 board (9 track_not_centered_on_via, 5
missing_courtyard, 1 footprint_type_mismatch; the other two found no violations there but are
still excluded by the project's defaults and must still be checked).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).parents[3] / "examples/radar60"
sys.path.insert(0, str(EXAMPLE / "board"))

import integrate  # noqa: E402


class UnconnectedNetsTest(unittest.TestCase):
    def test_net_names_come_from_each_open_pad(self):
        drc = {
            "unconnected_items": [
                {
                    "items": [
                        {"description": "Pad 1 [VIN_5V] of R42 on F.Cu"},
                        {"description": "Pad 5 [VIN_5V] of U5 on F.Cu"},
                    ]
                },
                {"items": [{"description": "Pad 2 [GND] of C1 on B.Cu"}]},
            ]
        }
        self.assertEqual(integrate._unconnected_nets(drc), {"VIN_5V", "GND"})

    def test_a_connected_net_is_not_in_the_set(self):
        drc = {"unconnected_items": [{"items": [{"description": "Pad 1 [GND] of C1 on B.Cu"}]}]}
        nets = integrate._unconnected_nets(drc)
        self.assertNotIn("QSPI_CLK", nets)

    def test_no_unconnected_items_is_an_empty_set(self):
        self.assertEqual(integrate._unconnected_nets({"unconnected_items": []}), set())
        self.assertEqual(integrate._unconnected_nets({}), set())

    def test_an_item_without_a_bracketed_net_is_skipped_not_fatal(self):
        drc = {"unconnected_items": [{"items": [{"description": "no net here"}]}]}
        self.assertEqual(integrate._unconnected_nets(drc), set())

    def test_matches_the_radar60_wave1_cold_check(self):
        """The exact two QSPI stubs the review found: copper present, net still open."""
        drc = {
            "unconnected_items": [
                {"items": [{"description": "Pad 11 [QSPI_CS_N] of U1 on F.Cu"}]},
                {"items": [{"description": "Pad 12 [QSPI_D3] of U1 on F.Cu"}]},
            ]
        }
        nets = integrate._unconnected_nets(drc)
        self.assertEqual(nets, {"QSPI_CS_N", "QSPI_D3"})


class TopCandidatesTest(unittest.TestCase):
    """Stage 3c R7: ``--top-n`` writes more than one placement for routing, not just the
    single stage-0 winner `select` already picked."""

    LEGAL = [
        {"id": "a", "rank": 1, "audit_pass": True},
        {"id": "b", "rank": 2, "audit_pass": True},
        {"id": "c", "rank": 3, "audit_pass": False},  # legal but failed the audit: never written
        {"id": "d", "rank": 4, "audit_pass": True},
    ]

    def test_caps_at_top_n_in_rank_order(self):
        self.assertEqual([r["id"] for r in integrate._top_candidates(self.LEGAL, 2)], ["a", "b"])

    def test_skips_audit_failures_even_within_the_cap(self):
        # rank 3 failed the audit; the 4th slot in a top-4 request is rank 4 ("d"), not "c".
        self.assertEqual(
            [r["id"] for r in integrate._top_candidates(self.LEGAL, 4)], ["a", "b", "d"]
        )

    def test_fewer_passing_candidates_than_requested_is_not_an_error(self):
        self.assertEqual(
            [r["id"] for r in integrate._top_candidates(self.LEGAL, 100)], ["a", "b", "d"]
        )

    def test_top_n_zero_or_none_still_writes_the_winner(self):
        self.assertEqual([r["id"] for r in integrate._top_candidates(self.LEGAL, 0)], ["a"])
        self.assertEqual([r["id"] for r in integrate._top_candidates(self.LEGAL, None)], ["a"])

    def test_no_passing_candidates_is_empty(self):
        self.assertEqual(integrate._top_candidates([{"id": "x", "audit_pass": False}], 4), [])


class DrcIgnoredByDefaultTest(unittest.TestCase):
    def test_pins_the_five_types_the_review_found_hidden(self):
        self.assertEqual(
            set(integrate._DRC_IGNORED_BY_DEFAULT),
            {
                "footprint_filters_mismatch",
                "footprint_type_mismatch",
                "missing_courtyard",
                "track_not_centered_on_via",
                "tuning_profile_track_geometries",
            },
        )


if __name__ == "__main__":
    unittest.main()
