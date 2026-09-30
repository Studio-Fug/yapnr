import copy, json, unittest
import pcbnew as k
from test_native_electrical import board, pad, FAB
from pnr.electrical import compile_policy
from pnr.native_electrical import add_track, uid, vec
from pnr.power_bank_reuse import banks, proposals, apply
from pnr.via_coalesce import partition, preserved
from pnr.pad_entry import snapshot


class PowerBankTest(unittest.TestCase):
    def fixture(self):
        b = board()
        pads = [
            pad(b, "RENAMED_A", "19", "rail", (4, 8)),
            pad(b, "RENAMED_Z", "31", "rail", (14, 8)),
        ]
        r = compile_policy(
            {
                "fab": {"track_width_mm": 0.2, "clearance_mm": 0.15},
                "net_classes": [{"name": "power", "nets": ["rail"], "width_mm": 1.5}],
            },
            [
                {
                    "net": "rail",
                    "rms_current_a": 5,
                    "peak_current_a": 5,
                    "source": {"path": "example.ato", "line": 14, "sha256": "source-proof"},
                }
            ],
            FAB,
        )
        ts = [add_track(b, "rail", k.F_Cu, (4, 8), (14, 8), 1.5)]
        vs = []
        for x, end in [(6, 4), (10, 14)]:
            ts.append(add_track(b, "rail", k.B_Cu, (x - 0.7, 8), (x + 0.7, 8), 1.5))
            ts.append(add_track(b, "rail", k.B_Cu, (x, 8), (end, 10), 1.5))
            for dx in (-0.7, 0, 0.7):
                v = k.PCB_VIA(b)
                v.SetViaType(k.VIATYPE_THROUGH)
                v.SetLayerPair(k.F_Cu, k.B_Cu)
                v.SetPosition(vec((x + dx, 8)))
                v.SetWidth(600000)
                v.SetDrill(300000)
                v.SetNetCode(b.FindNet("rail").GetNetCode())
                b.Add(v)
                v.thisown = False
                vs.append(v)
        b.BuildConnectivity()
        return b, r, vs, ts

    def test_general_names_full_source_current_and_capacity(self):
        b, r, vs, ts = self.fixture()
        p = proposals(b, r)
        self.assertTrue(p)
        self.assertEqual(len(p[0]["removed_vias"]), 3)
        self.assertEqual(len(p[0]["retained_vias"]), 3)
        self.assertEqual(p[0]["full_net_current"]["rms_current_a"], 5)
        self.assertEqual(p[0]["width"], 1.5)
        self.assertEqual(len(banks(b, r)), 2)

    def test_proposal_does_not_mutate_and_serialized_apply_preserves_contacts(self):
        b, r, vs, ts = self.fixture()
        before = partition(b)
        entries = snapshot(b, r)
        n = len(list(b.GetTracks()))
        p = proposals(b, r)[0]
        self.assertEqual(len(list(b.GetTracks())), n)
        removed = apply(b, json.loads(json.dumps(p)), r)
        b.BuildConnectivity()
        self.assertEqual(sum(t.GetClass() == "PCB_VIA" for t in b.GetTracks()), 3)
        self.assertTrue(preserved(before, partition(b)))
        self.assertEqual(snapshot(b, r), entries)

    def test_current_increase_requires_larger_bank(self):
        b, r, vs, ts = self.fixture()
        r["electrical_nets"]["rail"]["rms_current_a"] = 10
        self.assertEqual(banks(b, r), [])

    def test_missing_current_provenance_rejected(self):
        b, r, vs, ts = self.fixture()
        r["electrical_nets"]["rail"]["sources"] = []
        self.assertEqual(banks(b, r), [])

    def test_weak_front_feed_cannot_bridge_power_banks(self):
        b, r, vs, ts = self.fixture()
        ts[0].SetWidth(200000)
        self.assertEqual(proposals(b, r), [])

    def test_locks_and_mismatched_drill_protect_whole_bank(self):
        b, r, vs, ts = self.fixture()
        vs[0].SetLocked(True)
        self.assertEqual(proposals(b, r), [])
        vs[0].SetLocked(False)
        vs[0].SetDrill(200000)
        self.assertEqual(proposals(b, r), [])

    def test_inner_layer_port_protects_bank(self):
        b, r, vs, ts = self.fixture()
        add_track(b, "rail", k.In2_Cu, (6, 8), (6, 12), 1.5)
        self.assertEqual(proposals(b, r), [])

    def test_pair_and_plane_policies_protect_banks(self):
        b, r, vs, ts = self.fixture()
        r["diff_pairs"] = [{"p": "rail", "n": "other"}]
        self.assertEqual(banks(b, r), [])
        r.pop("diff_pairs")
        r["net_classes"][0]["plane_layer"] = "In1.Cu"
        self.assertEqual(banks(b, r), [])

    def test_foreign_back_wall_blocks_reuse_route(self):
        b, r, vs, ts = self.fixture()
        add_track(b, "other", k.B_Cu, (8, 0), (8, 20), 0.2)
        self.assertEqual(proposals(b, r), [])

    def test_stale_proposal_fails_before_mutation(self):
        b, r, vs, ts = self.fixture()
        p = proposals(b, r)[0]
        add_track(b, "other", k.B_Cu, (8, 0), (8, 20), 0.2)
        n = len(list(b.GetTracks()))
        with self.assertRaises(ValueError):
            apply(b, p, r)
        self.assertEqual(n, len(list(b.GetTracks())))


if __name__ == "__main__":
    unittest.main()
