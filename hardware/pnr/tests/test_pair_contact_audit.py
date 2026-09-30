import unittest

import pcbnew as k
from test_native_electrical import board, pad

from pnr.native_electrical import add_track
from pnr.pair_contact_audit import audit_pair_contacts


class PairContactAuditTest(unittest.TestCase):
    def fixture(self):
        b = board()
        positions = {
            "A+": (2, 2),
            "B+": (4, 2),
            "Z+": (3, 7),
            "A-": (2, 10),
            "B-": (4, 10),
            "Z-": (3, 15),
        }
        for label, pos in positions.items():
            pad(b, "J", label, "p" if label.endswith("+") else "n", pos, (0.2, 0.2))
        keep = []
        for net, y in [("p", 2), ("n", 10)]:
            keep.append(add_track(b, net, k.F_Cu, (2, y), (4, y), 0.2))
            keep.append(add_track(b, net, k.F_Cu, (3, y), (3, y + 5), 0.2))
        pair = dict(
            name="pair",
            p="p",
            n="n",
            skew_mm=0.3,
            terminal_chain=[dict(p="J.A+", n="J.A-"), dict(p="J.Z+", n="J.Z-")],
            auxiliary_pairs=[
                dict(source=dict(p="J.B+", n="J.B-"), target=dict(p="J.A+", n="J.A-"))
            ],
        )
        return b, dict(diff_pairs=[pair], electrical_fab=dict(board_thickness_mm=1.6)), keep

    def test_native_symmetric_branch_audit_without_mutation(self):
        b, r, keep = self.fixture()
        before = [(t.m_Uuid.AsString(), t.GetStart().x, t.GetEnd().y) for t in b.GetTracks()]
        report = audit_pair_contacts(b, r)
        self.assertTrue(report["qualified"])
        self.assertEqual(report["pairs"][0]["skews_mm"], {"J.A+/J.A-": 0, "J.B+/J.B-": 0})
        self.assertEqual(
            before, [(t.m_Uuid.AsString(), t.GetStart().x, t.GetEnd().y) for t in b.GetTracks()]
        )

    def test_ambiguous_duplicate_pad_fails_closed(self):
        b, r, keep = self.fixture()
        pad(b, "J", "A+", "p", (2, 2), (0.2, 0.2))
        report = audit_pair_contacts(b, r)
        self.assertFalse(report["qualified"])
        self.assertIn("missing or ambiguous pad J.A+", report["pairs"][0]["unsupported"])

    def test_unsupported_inner_route_fails_closed(self):
        b, r, keep = self.fixture()
        keep.append(add_track(b, "p", k.In2_Cu, (3, 3), (5, 3), 0.2))
        report = audit_pair_contacts(b, r)
        self.assertFalse(report["qualified"])
        self.assertTrue(any("In2.Cu" in t for t in report["pairs"][0]["unsupported"]))

    def test_literal_pad_net_mismatch_fails_closed(self):
        b, r, keep = self.fixture()
        r["diff_pairs"][0]["terminal_chain"][0] = dict(p="J.A-", n="J.A+")
        report = audit_pair_contacts(b, r)
        self.assertFalse(report["qualified"])
        self.assertIn("literal pad/net mismatch J.A-", report["pairs"][0]["unsupported"])

    def test_missing_thickness_fails_closed(self):
        b, r, keep = self.fixture()
        r["electrical_fab"] = {}
        report = audit_pair_contacts(b, r)
        self.assertFalse(report["qualified"])
        self.assertIn("missing physical board thickness", report["pairs"][0]["unsupported"])


if __name__ == "__main__":
    unittest.main()
