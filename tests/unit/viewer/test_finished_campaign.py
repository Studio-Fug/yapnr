"""End-to-end check (a real viewer server process, real HTTP) for the bug a real, finished
campaign hit: ``live-hub/runs/lv2p2-rungs-all`` (252 lanes, 72 of them ladder "case" lanes whose
last event is ``source_round_start``/``worker_config_applied``, never anything terminal) finished
normally, but every one of those 72 parent lanes read "Stalled: idle N min" forever, because
nothing ever emitted a terminal event for the case lane itself -- only its ``initial-start-NN``
candidate children did (``candidate_complete``). This builds a small fixture with the same real
shape (case/seed lanes with a non-terminal last event, each with candidate children that all
ended) rather than reading that live directory directly -- a fixture is hermetic and portable,
the real one is an absolute path on one machine -- and checks the fix end to end: the server's
poll loop, yapnr.viewer.lane_tree's children-derivation and yapnr.viewer.progress's own
``finished`` fallback, all wired together exactly as a running viewer wires them.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from yapnr.viewer.server import MIRROR_FINISHED_MARKER
from yapnr.viewer.testing import start_viewer, stop

# Real lane id shapes from live-hub/runs/lv2p2-rungs-all/live (docs/viewer.md), trimmed to a
# handful of cases rather than the real 252 lanes.
CASES = ["09-mcu-usb-31-6L-SGSGPS", "14-chaser-radar", "rails-candidates"]
OLD = time.time() - 3600 * 3  # well past STALE_SECONDS (600s): the campaign "finished" 3h ago


def _write(root: Path, event_id: str, kind: str, candidate: str, when: float, **data) -> None:
    (root / "events").mkdir(parents=True, exist_ok=True)
    event = dict(
        schema="pnr-live-event-v1",
        id=event_id,
        time=when,
        kind=kind,
        candidate=candidate,
        iteration=None,
        data=data,
    )
    (root / "events" / (event_id + ".json")).write_text(json.dumps(event))


def _get(base: str, path: str):
    with urlopen(Request(base + path), timeout=5) as response:
        return json.load(response)


class FinishedLadderCampaignTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="viewer-finished-")
        self.root = Path(self._tmp.name) / "live"
        for i, case in enumerate(CASES):
            parent = "ladder/keep-on/%s/s0" % case
            _write(self.root, "parent-%d" % i, "worker_config_applied", parent, OLD)
            _write(
                self.root,
                "cand-%d-00" % i,
                "candidate_complete",
                parent + "/initial-start-00",
                OLD,
                opens=0,
                violations=0,
            )
            _write(
                self.root,
                "cand-%d-01" % i,
                "candidate_complete",
                parent + "/initial-start-01",
                OLD,
                opens=0,
                violations=0,
            )
        # A lane with no children at all and no terminal event of its own (nothing to derive
        # from) -- only the campaign-finished marker can rescue this one from "stalled".
        _write(self.root, "orphan", "source_round_start", "controller", OLD)

    def tearDown(self):
        self._tmp.cleanup()

    def _poll_until_seen(self, base, n_lanes, deadline_s=10):
        deadline = time.monotonic() + deadline_s
        state = None
        while time.monotonic() < deadline:
            state = _get(base, "/api/state")
            if len(state["lanes"]) >= n_lanes:
                return state
            time.sleep(0.05)
        return state

    def test_case_lanes_with_no_terminal_event_read_done_not_stalled(self):
        # No MIRROR_FINISHED_MARKER written here at all -- the children-derivation rule in
        # yapnr.viewer.lane_tree needs no outside "is this run finished" signal, only that every
        # candidate underneath each case lane has already ended.
        process, base = start_viewer(self.root, "--net-summaries", "off", "--agent", "off")
        try:
            state = self._poll_until_seen(base, 1 + 3 * len(CASES))
            self.assertIsNotNone(state)
            for i, case in enumerate(CASES):
                parent = "ladder/keep-on/%s/s0" % case
                lane = state["lanes"][parent]
                self.assertEqual(lane["progress"]["state"], "done", parent)
                self.assertNotEqual(lane["status_text"], "Stalled")
                self.assertFalse(lane["status_text"].startswith("Stalled"))
            tree_parent = state["tree"]["children"]["ladder"]["children"]["keep-on"]["children"][
                CASES[0]
            ]["children"]["s0"]
            self.assertEqual(tree_parent["counts"]["running"], 0)
            self.assertEqual(tree_parent["counts"]["stalled"], 0)
            self.assertEqual(tree_parent["counts"]["done"], 3)  # itself + 2 candidates
        finally:
            stop(process)

    def test_a_childless_lane_needs_the_finished_marker_to_stop_reading_stalled(self):
        (self.root / MIRROR_FINISHED_MARKER).write_text(json.dumps({"campaign": "test"}))
        process, base = start_viewer(self.root, "--net-summaries", "off", "--agent", "off")
        try:
            state = self._poll_until_seen(base, 1 + 3 * len(CASES))
            self.assertIsNotNone(state)
            self.assertTrue(state["campaign_finished"])
            orphan = state["lanes"]["controller"]
            self.assertEqual(orphan["progress"]["state"], "finished")
            self.assertEqual(orphan["status_text"], "Finished (no final event)")
        finally:
            stop(process)

    def test_marker_appearing_after_an_idle_poll_is_still_picked_up(self):
        """Regression: yapnr.viewer.server.Viewer.state_response caches one JSON response per
        revision; writing MIRROR_FINISHED_MARKER never bumps the revision (only a new event
        file under events/ or a restart-status change does -- Viewer.ingest), so a campaign
        that goes quiet and is only later marked finished (yapnr.exp.live, once run after the
        fact by something like a hub's catchup pass) must not have its first, already-cached
        response (with campaign_finished still false) served forever after. This is exactly the
        real symptom this fix chases: a run idle long enough to read "stalled" on every lane,
        with no later event ever arriving to force a fresh poll."""
        process, base = start_viewer(self.root, "--net-summaries", "off", "--agent", "off")
        try:
            state = self._poll_until_seen(base, 1 + 3 * len(CASES))
            self.assertIsNotNone(state)
            self.assertFalse(state["campaign_finished"])
            self.assertEqual(state["lanes"]["controller"]["progress"]["state"], "stalled")
            revision_before = state["revision"]
            # No new event follows -- the marker is the only thing that changes on disk.
            (self.root / MIRROR_FINISHED_MARKER).write_text(json.dumps({"campaign": "test"}))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = _get(base, "/api/state")
                if state["campaign_finished"]:
                    break
                time.sleep(0.05)
            self.assertTrue(state["campaign_finished"])
            # The flip bumps the revision, so a second client still polling with the old one
            # gets the new state too, never the "unchanged" shortcut.
            self.assertGreater(state["revision"], revision_before)
            other = _get(base, "/api/state?since=%d&run=%s" % (revision_before, state["run"]))
            self.assertNotIn("unchanged", other)
            self.assertTrue(other["campaign_finished"])
            self.assertEqual(state["lanes"]["controller"]["progress"]["state"], "finished")
            self.assertEqual(
                state["lanes"]["controller"]["status_text"], "Finished (no final event)"
            )
        finally:
            stop(process)

    def test_without_the_marker_the_same_childless_lane_still_reads_stalled(self):
        # The control case: without campaign_finished known, idling past STALE_SECONDS really is
        # indistinguishable from a dead worker, so "stalled" is the right, honest answer.
        process, base = start_viewer(self.root, "--net-summaries", "off", "--agent", "off")
        try:
            state = self._poll_until_seen(base, 1 + 3 * len(CASES))
            self.assertIsNotNone(state)
            self.assertFalse(state["campaign_finished"])
            self.assertEqual(state["lanes"]["controller"]["progress"]["state"], "stalled")
        finally:
            stop(process)


if __name__ == "__main__":
    unittest.main()
