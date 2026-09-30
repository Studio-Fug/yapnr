import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pnr import native_loop
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad


class PortalControllerTest(unittest.TestCase):
    def test_optional_retry_dispatch_is_joint_bounded_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            package = repo / "hardware/pnr/pnr"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            adapter = repo / "hardware/tools/keyhole_region.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("")
            board = root / "input.kicad_pcb"
            board.write_text("fixture")
            board.with_suffix(".kicad_pro").write_text("{}")
            rules = root / "rules.json"
            rules.write_text("{}")
            constraints = root / "constraints.yaml"
            constraints.write_text("{}")
            target = dict(
                net="signal",
                source="A.1",
                target="B.1",
                source_xy=[2, 2],
                target_xy=[4, 4],
                mode="signal",
                distance=3,
            )
            poses = {"A": [2, 2], "B": [4, 4]}
            graph = BoardGraph(
                "fixture",
                [
                    Component(
                        ref,
                        "fixture",
                        (x, 10 - y),
                        0,
                        "top",
                        (1, 1),
                        (1, 1),
                        pads=[Pad("1", "signal", (0, 0), (0.6, 0.6))],
                    )
                    for ref, (x, y) in poses.items()
                ],
                [Net("signal", 1, [("A", "1"), ("B", "1")])],
                BoardOutline(10, 10),
            ).to_dict()
            inventory = dict(
                targets=[target],
                footprint_poses=poses,
                graph=graph,
                item_nets={"a": "signal"},
                excluded=[],
                owners={},
                bounds=[0, 0, 10, 10],
            )
            drc = dict(unconnected_items=[dict(items=[dict(uuid="a")])], violations=[])
            calls = []

            def invoke(cmd, **kwargs):
                if "--worker" in cmd:
                    mode = cmd[cmd.index("--worker") + 1]
                    data = {"reopen": ["other"]} if mode == "terminal-blockers" else inventory
                    Path(cmd[cmd.index("--report") + 1]).write_text(json.dumps(data))
                else:
                    calls.append(cmd)
                    out = Path(cmd[cmd.index("--out-dir") + 1])
                    out.mkdir()
                    (out / "result.json").write_text(
                        json.dumps(dict(status="no_joint_alternative", accepted=False))
                    )

            cwd = Path.cwd()
            try:
                with patch.dict(
                    os.environ,
                    dict(
                        PNR_PORTAL_REPAIR="1",
                        PNR_SINGLE_TRACK_WORKERS="1",
                        PNR_PLANE_LEAF_REPAIR="0",
                        PNR_POWER_DETOUR_REPAIR="0",
                    ),
                ), patch.object(native_loop.subprocess, "run", side_effect=invoke), patch(
                    "pnr.native_drc.run_drc", return_value=drc
                ):
                    native_loop.main(
                        [
                            str(board),
                            "--repo",
                            str(repo),
                            "--rules",
                            str(rules),
                            "--constraints",
                            str(constraints),
                            "--out-dir",
                            str(root / "run"),
                            "--kicad-python",
                            "fake",
                            "--kicad-cli",
                            "fake",
                            "--cycles",
                            "2",
                            "--seconds",
                            "3000",
                            "--route-only",
                        ]
                    )
            finally:
                os.chdir(cwd)
            portal = [c for c in calls if "--portal-joint" in c]
            self.assertEqual(len(portal), 1)
            self.assertEqual(portal[0].count("--net"), 2)
            self.assertEqual(portal[0][portal[0].index("--max-seconds") + 1], "450.0")
            progress = json.loads((root / "run/progress.json").read_text())
            events = [e for e in progress["events"] if e["stage"] == "portal_repair"]
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0]["accepted"])
            self.assertEqual(progress["opens"], 1)


if __name__ == "__main__":
    unittest.main()
