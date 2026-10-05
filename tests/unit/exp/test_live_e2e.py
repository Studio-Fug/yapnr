"""End to end: a tiny task through the local backend with ``[live]`` on, driving the real
telemetry writer (hardware/pnr/pnr/live.py), the wrapper's LiveUploader, the store, the
``yapnr exp live`` mirror and the viewer's own event ingestion and lane grouping -- no KiCad, no
mocked telemetry."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from yapnr.exp import live as livemod
from yapnr.exp import testing
from yapnr.exp.store import LocalStore
from yapnr.viewer.testing import start_viewer, stop, wait_for

# tests/unit/exp/test_live_e2e.py -> repo root (hardware/pnr/pnr/live.py is real, stdlib only;
# //hardware/pnr:pnr_kicad_srcs carries it as data, the same way test_kinds reaches compact_flags).
HARDWARE_PNR = Path(__file__).resolve().parents[3] / "hardware" / "pnr"

# A real (if tiny) use of the engine's own telemetry writer: a phase_complete event with a board
# (hardware/pnr/pnr/phase_capture.py's shape, for the mirror's board handling) and a route_result
# (hardware/pnr/pnr/native_loop.py's shape, no board) the viewer applies without needing to
# extract board geometry -- so the viewer assertions below hold whether or not this machine has a
# real KiCad able to parse the fixture's placeholder board content.
SCRIPT = (
    "from pnr.live import emit\n"
    "open('board.kicad_pcb', 'w').write('(kicad_pcb (version 1))')\n"
    "emit('phase_complete', board='board.kicad_pcb', "
    "data={'phase': 'place', 'opens': 1, 'violations': 0})\n"
    "emit('route_result', data={'opens': 0, 'target': 'net-a'})\n"
    "import json, os\n"
    "os.makedirs('out', exist_ok=True)\n"
    "json.dump({'complete': True}, open('out/done.json', 'w'))\n"
)


class LiveViewerEndToEndTest(unittest.TestCase):
    def test_a_tiny_task_mirrors_into_a_lane_the_viewer_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store = tmp / "store"
            toolchain = testing.host_toolchain(tmp / "toolchain.json")
            task = testing.python_task(
                "live/tiny",
                SCRIPT,
                done_json={"complete": True},
                env={"PYTHONPATH": str(HARDWARE_PNR)},
            )
            # interval_s huge: the task finishes well inside it, so the only bundle is the final
            # one `run_task` uploads unconditionally when the command ends (docs/cloud-
            # experiments.md, "Live viewer mirror").
            testing.write_store_campaign(
                store, [task], live={"enabled": True, "interval_s": 10_000, "mode": "full"}
            )
            result = testing.run_wrapper(store, 0, toolchain)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            runs = LocalStore(store)
            bundles = livemod.list_bundles(runs, testing.CID)
            self.assertEqual(len(bundles), 1, bundles)
            task_key, attempt, seq, _path = bundles[0]
            self.assertEqual(task_key, "live~tiny")
            self.assertEqual(attempt, "s1r0")
            self.assertEqual(seq, 1)

            dest = tmp / "mirror"
            found = livemod.mirror_once(runs, testing.CID, dest, livemod.LiveState())
            self.assertEqual(found, {"bundles": 1, "events": 2, "boards": 1})
            # The mirror is idempotent and resumable: a second poll, even with a fresh state
            # loaded from disk, fetches nothing new.
            resumed = livemod.LiveState.load(dest)
            self.assertEqual(
                livemod.mirror_once(runs, testing.CID, dest, resumed),
                {"bundles": 0, "events": 0, "boards": 0},
            )

            events = [json.loads(p.read_text()) for p in (dest / "events").glob("*.json")]
            self.assertEqual(len(events), 2)
            for event in events:
                self.assertEqual(livemod.event_errors(event), [])  # pnr-live-event-v1
                self.assertEqual(event["candidate"], "live/tiny")  # PNR_LIVE_CANDIDATE = task id
            with_board = next(e for e in events if "board" in e)
            self.assertTrue(Path(with_board["board"]).is_file())  # rewritten to the local mirror
            self.assertTrue(
                Path(with_board["board"]).resolve().is_relative_to((dest / "boards").resolve())
            )

            process, base = start_viewer(dest)
            try:

                def ingested():
                    with urllib.request.urlopen(base + "/api/state", timeout=5) as resp:
                        payload = json.load(resp)
                    return payload if payload.get("revision") else None

                payload = wait_for(ingested)
                lane = payload["lanes"]["live/tiny"]  # one lane per task, by its candidate
                # route_result sets this straight from its event, with no board/KiCad involved,
                # so it holds whether or not this machine's KiCad could parse the fixture board.
                self.assertEqual(lane["opens"], 0)
            finally:
                stop(process)


if __name__ == "__main__":
    unittest.main()
