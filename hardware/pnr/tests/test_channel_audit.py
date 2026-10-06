"""The channel audit measures routed copper along the channels the model prices."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.place.channel_audit import audit, summarize
from pnr.place.channels import ChannelModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))

import channel_audit as cli  # noqa: E402

RULES = {"default_clearance_mm": 0.15, "layers": 2}


def example():
    # A's four pads face B's tall pad across a 0.3 mm channel (x 6.1 .. 6.4, y 4.15 .. 5.85).
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


TRACKS = [
    ["n0", "F.Cu", [6.25, 4.0], [6.25, 6.0], 0.1],  # runs along the channel
    ["n2", "F.Cu", [6.0, 5.25], [6.5, 5.25], 0.1],  # crosses it straight
    ["n0", "B.Cu", [6.3, 4.0], [6.3, 6.0], 0.1],  # another layer
]


class ChannelAuditTests(unittest.TestCase):
    def test_runs_along_the_channel_count_crossings_do_not(self):
        graph = example()
        model = ChannelModel(graph, RULES, layers=False)
        (record,) = audit(graph, model, TRACKS, [["n1", 5.7, 4.75]])
        self.assertEqual(record["refs"], ["A", "B"])
        self.assertEqual(record["direction"], "east")
        self.assertAlmostEqual(record["gap_mm"], 0.3)
        self.assertAlmostEqual(record["required_mm"], 1.55)
        self.assertTrue(record["predicted_short"])
        self.assertEqual(record["used_tracks"], 1)
        self.assertEqual(record["used_vias"], 0)
        self.assertEqual(record["used_nets"], ["n0"])
        self.assertAlmostEqual(record["used_mm"], 0.1 + 2 * 0.15)
        self.assertEqual(
            record["fate"],
            {"n0": "surface", "n1": "dropped", "n2": "elsewhere", "n3": "elsewhere"},
        )

    def test_a_via_in_the_channel_is_a_lengthwise_obstacle(self):
        graph = example()
        model = ChannelModel(graph, RULES, layers=False)
        (record,) = audit(graph, model, TRACKS, [["n3", 6.25, 5.6]])
        self.assertEqual((record["used_tracks"], record["used_vias"]), (1, 1))
        self.assertAlmostEqual(record["used_mm"], 0.1 + 0.6 + 3 * 0.15)
        self.assertEqual(record["fate"]["n3"], "dropped")

    def test_summary_and_no_routes(self):
        graph = example()
        model = ChannelModel(graph, RULES, layers=False)
        (record,) = audit(graph, model, [], [])
        self.assertEqual((record["used_tracks"], record["used_mm"]), (0, 0.0))
        total = summarize([record])
        self.assertEqual(total["priced"], 1)
        self.assertEqual(total["predicted_short"], 1)
        self.assertEqual(total["fates"], {"surface": 0, "dropped": 0, "elsewhere": 4})
        self.assertEqual(total["over_predicted"], 1)

    def test_cli_tables_passing_cases_by_layer_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "run" / "toy-seed-0"
            case.mkdir(parents=True)
            (case / "placed.json").write_text(example().to_json())
            (case / "rules.json").write_text(json.dumps(RULES))
            (case / "routes.json").write_text(json.dumps(dict(tracks=TRACKS, vias=[])))
            (case / "result.json").write_text(json.dumps(dict(passed=True)))
            out = Path(tmp) / "audit.json"
            for layers in ("--no-layers", "--layers"):
                self.assertEqual(cli.main([layers, "--json", str(out), tmp]), 0)
                (result,) = json.loads(out.read_text())
                self.assertEqual((result["layers"], result["signal_layers"]), (2, 2))
            self.assertLess(result["records"][0]["required_mm"], 1.55)
            self.assertIn("| 2 (2) | 1 |", cli.table([result]))


if __name__ == "__main__":
    unittest.main()
