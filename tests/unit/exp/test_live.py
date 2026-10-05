"""The live-viewer mirror: the wrapper's LiveUploader (packing, thin mode) and yapnr.exp.live
(unpacking, path rewriting, idempotence, schema validation, mirroring from a store)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.exp import live as livemod
from yapnr.exp import task as wrapper
from yapnr.exp.store import LocalStore


def event(event_id, kind, candidate="t/a", board_sha256=None, **data):
    e = {
        "schema": livemod.EVENT_SCHEMA,
        "id": event_id,
        "time": 1700000000.0,
        "kind": kind,
        "candidate": candidate,
        "iteration": None,
        "data": data,
    }
    if board_sha256:
        e.update(
            board="/wherever/this/task/ran/boards/%s.kicad_pcb" % board_sha256,
            board_sha256=board_sha256,
        )
    return e


def write_event(live_dir: Path, event_id: str, kind: str, **kw) -> None:
    (live_dir / "events").mkdir(parents=True, exist_ok=True)
    (live_dir / "events" / (event_id + ".json")).write_text(json.dumps(event(event_id, kind, **kw)))


def write_board(live_dir: Path, sha: str, content: bytes = b"(kicad_pcb)") -> None:
    (live_dir / "boards").mkdir(parents=True, exist_ok=True)
    (live_dir / "boards" / (sha + ".kicad_pcb")).write_bytes(content)


class LiveUploaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.work = self.tmp / "work"
        self.remote = self.tmp / "remote"

    def tearDown(self):
        self._tmp.cleanup()

    def uploader(self, mode="full", interval_s=10):
        return wrapper.LiveUploader(
            {"enabled": True, "interval_s": interval_s, "mode": mode}, self.work, self.remote
        )

    def test_disabled_is_a_no_op(self):
        live = wrapper.LiveUploader(None, self.work, self.remote)
        write_event(live.live_dir, "e1", "phase_start")
        self.assertFalse(live.due())
        self.assertFalse(live.sync())
        self.assertFalse(self.remote.exists())

    def test_sync_bundles_only_whats_new(self):
        live = self.uploader()
        write_event(live.live_dir, "e1", "phase_start")
        self.assertTrue(live.sync())
        self.assertTrue((self.remote / "000001.tar.gz").is_file())
        self.assertFalse(live.sync())  # nothing new: no second bundle
        self.assertFalse((self.remote / "000002.tar.gz").exists())
        write_event(live.live_dir, "e2", "phase_complete")
        self.assertTrue(live.sync())
        self.assertTrue((self.remote / "000002.tar.gz").is_file())
        added = livemod.unpack_bundle(
            (self.remote / "000002.tar.gz").read_bytes(), self.tmp / "out"
        )
        self.assertEqual(added, {"events": 1, "boards": 0})  # only e2, not e1 again

    def test_boards_are_sent_once_per_sha(self):
        live = self.uploader()
        write_board(live.live_dir, "a" * 64)
        write_event(live.live_dir, "e1", "candidate_complete", board_sha256="a" * 64)
        write_event(live.live_dir, "e2", "candidate_complete", board_sha256="a" * 64)
        self.assertTrue(live.sync())
        out = self.tmp / "out"
        added = livemod.unpack_bundle((self.remote / "000001.tar.gz").read_bytes(), out)
        self.assertEqual(added, {"events": 2, "boards": 1})

    def test_due_respects_the_interval(self):
        live = self.uploader(interval_s=10_000)
        write_event(live.live_dir, "e1", "phase_start")
        self.assertFalse(live.due())
        live.last_sync = time.monotonic() - 10_001
        self.assertTrue(live.due())

    def test_thin_mode_drops_per_net_maze_events_keeps_everything_else(self):
        live = self.uploader(mode="thin")
        write_event(live.live_dir, "e1", "signal_net_added")
        write_event(live.live_dir, "e2", "signal_net_removed")
        write_event(live.live_dir, "e3", "phase_complete")
        write_event(live.live_dir, "e4", "route_result")
        self.assertTrue(live.sync())
        out = self.tmp / "out"
        livemod.unpack_bundle((self.remote / "000001.tar.gz").read_bytes(), out)
        kinds = sorted(json.loads(p.read_text())["kind"] for p in (out / "events").glob("*.json"))
        self.assertEqual(kinds, ["phase_complete", "route_result"])

    def test_full_mode_keeps_per_net_maze_events(self):
        live = self.uploader(mode="full")
        write_event(live.live_dir, "e1", "signal_net_added")
        self.assertTrue(live.sync())
        out = self.tmp / "out"
        added = livemod.unpack_bundle((self.remote / "000001.tar.gz").read_bytes(), out)
        self.assertEqual(added["events"], 1)

    def test_namespaced_by_attempt_does_not_collide_with_an_earlier_attempt(self):
        first = wrapper.LiveUploader(
            {"enabled": True, "interval_s": 10, "mode": "full"}, self.work, self.remote / "s1r0"
        )
        write_event(first.live_dir, "e1", "phase_start")
        first.sync()
        second_work = self.tmp / "work2"
        second = wrapper.LiveUploader(
            {"enabled": True, "interval_s": 10, "mode": "full"}, second_work, self.remote / "s1r1"
        )
        write_event(second.live_dir, "e1", "phase_start")  # the id can repeat; a new attempt
        second.sync()
        self.assertTrue((self.remote / "s1r0" / "000001.tar.gz").is_file())
        self.assertTrue((self.remote / "s1r1" / "000001.tar.gz").is_file())

    def test_a_failed_write_does_not_lose_the_pending_events(self):
        """If write_atomic raises, the next sync() must see the same events as still
        pending (review finding: sent_events/seq were updated before the write, so a
        failure silently dropped them even though the docstring promises a retry)."""
        live = self.uploader()
        write_event(live.live_dir, "e1", "phase_start")
        orig_write_atomic = wrapper.write_atomic
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated upload failure")
            return orig_write_atomic(path, data)

        wrapper.write_atomic = flaky
        try:
            with self.assertRaises(OSError):
                live.sync()
            self.assertEqual(live.seq, 0)
            self.assertEqual(live.sent_events, set())
            # The retry (same event still pending) now succeeds and is actually sent.
            self.assertTrue(live.sync())
        finally:
            wrapper.write_atomic = orig_write_atomic
        self.assertTrue((self.remote / "000001.tar.gz").is_file())
        added = livemod.unpack_bundle(
            (self.remote / "000001.tar.gz").read_bytes(), self.tmp / "out"
        )
        self.assertEqual(added, {"events": 1, "boards": 0})


class EventSchemaTest(unittest.TestCase):
    def test_valid_event_has_no_errors(self):
        self.assertEqual(livemod.event_errors(event("e1", "phase_start")), [])
        self.assertEqual(
            livemod.event_errors(event("e1", "candidate_complete", board_sha256="a" * 64)), []
        )

    def test_every_finding_is_listed(self):
        bad = {"schema": "nope", "id": "", "time": "now", "kind": 1, "candidate": "", "data": []}
        errors = livemod.event_errors(bad)
        self.assertEqual(
            sorted(errors),
            [
                "candidate is a non-empty string",
                "data is an object",
                "id is a non-empty string",
                "kind is a non-empty string",
                "schema must be 'pnr-live-event-v1'",
                "time is a number",
            ],
        )
        self.assertIn(
            "board_sha256 is a sha256 hex digest",
            livemod.event_errors(event("e1", "k", board_sha256="not-a-sha")),
        )

    def test_not_a_dict(self):
        self.assertEqual(
            livemod.event_errors(["not", "a", "dict"]), ["a live event is a JSON object"]
        )


class UnpackBundleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.work = self.tmp / "work"
        self.remote = self.tmp / "remote"
        self.dest = self.tmp / "mirror"
        self.live = wrapper.LiveUploader(
            {"enabled": True, "interval_s": 10, "mode": "full"}, self.work, self.remote
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_board_path_is_rewritten_to_the_local_mirror(self):
        sha = "b" * 64
        write_board(self.live.live_dir, sha, b"the board")
        write_event(self.live.live_dir, "e1", "route_result", board_sha256=sha)
        self.live.sync()
        livemod.unpack_bundle((self.remote / "000001.tar.gz").read_bytes(), self.dest)
        written = json.loads((self.dest / "events" / "e1.json").read_text())
        self.assertEqual(
            written["board"], str((self.dest / "boards" / (sha + ".kicad_pcb")).resolve())
        )
        self.assertEqual((self.dest / "boards" / (sha + ".kicad_pcb")).read_bytes(), b"the board")

    def test_unpacking_the_same_bundle_twice_is_a_no_op(self):
        write_event(self.live.live_dir, "e1", "phase_start")
        self.live.sync()
        data = (self.remote / "000001.tar.gz").read_bytes()
        first = livemod.unpack_bundle(data, self.dest)
        second = livemod.unpack_bundle(data, self.dest)
        self.assertEqual(first, {"events": 1, "boards": 0})
        self.assertEqual(second, {"events": 0, "boards": 0})

    def test_source_host_path_is_stripped_not_just_board(self):
        """``source`` carries the uploading host's absolute path (hardware/pnr/pnr/live.py);
        it must not survive into the mirror, which other people may later share."""
        sha = "c" * 64
        write_board(self.live.live_dir, sha, b"the board")
        (self.live.live_dir / "events").mkdir(parents=True, exist_ok=True)
        raw = event("e1", "candidate_complete", board_sha256=sha)
        raw["source"] = "~someone/private/board.kicad_pcb"
        (self.live.live_dir / "events" / "e1.json").write_text(json.dumps(raw))
        self.live.sync()
        livemod.unpack_bundle((self.remote / "000001.tar.gz").read_bytes(), self.dest)
        written = json.loads((self.dest / "events" / "e1.json").read_text())
        self.assertNotIn("source", written)
        self.assertNotIn("someone", json.dumps(written))

    def test_a_malformed_event_in_the_bundle_is_skipped_not_fatal(self):
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("events/bad.json")
            payload = b"not json"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        added = livemod.unpack_bundle(buf.getvalue(), self.dest)
        self.assertEqual(added, {"events": 0, "boards": 0})


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store_root = self.tmp / "store"
        self.store = LocalStore(self.store_root)
        self.cid = "20261005-ladder-abc123"
        self.base = self.store_root / "campaigns" / self.cid / "live"

    def tearDown(self):
        self._tmp.cleanup()

    def write_bundle(self, task_key, attempt, seq, events):
        work = self.tmp / ("src-%s-%s-%d" % (task_key, attempt, seq))
        live = wrapper.LiveUploader(
            {"enabled": True, "interval_s": 10, "mode": "full"},
            work,
            self.base / task_key / attempt,
        )
        live.seq = seq - 1
        for event_id, kind in events:
            write_event(live.live_dir, event_id, kind, candidate=task_key)
        live.sync()

    def test_mirror_once_unpacks_every_lane_and_is_resumable(self):
        self.write_bundle("ladder~01~s0", "s1r0", 1, [("e1", "phase_start")])
        self.write_bundle("ladder~02~s0", "s1r0", 1, [("e2", "phase_start")])
        dest = self.tmp / "mirror"
        state = livemod.LiveState.load(dest)
        found = livemod.mirror_once(self.store, self.cid, dest, state)
        self.assertEqual(found, {"bundles": 2, "events": 2, "boards": 0})
        self.assertEqual(state.next_seq("ladder~01~s0", "s1r0"), 2)
        # A second poll with nothing new finds nothing (idempotent, no re-download).
        found = livemod.mirror_once(self.store, self.cid, dest, state)
        self.assertEqual(found, {"bundles": 0, "events": 0, "boards": 0})
        # The state survives a reload (resumability across a restarted `yapnr exp live`).
        reloaded = livemod.LiveState.load(dest)
        self.assertEqual(reloaded.next_seq("ladder~01~s0", "s1r0"), 2)
        self.assertEqual(
            sorted(p.name for p in (dest / "events").glob("*.json")), ["e1.json", "e2.json"]
        )

    def test_mirror_once_lane_is_two_different_tasks_candidates(self):
        self.write_bundle("ladder~01~s0", "s1r0", 1, [("e1", "phase_start")])
        self.write_bundle("ladder~02~s0", "s1r0", 1, [("e2", "phase_start")])
        dest = self.tmp / "mirror"
        livemod.mirror_once(self.store, self.cid, dest, livemod.LiveState())
        candidates = sorted(
            json.loads(p.read_text())["candidate"] for p in (dest / "events").glob("*.json")
        )
        self.assertEqual(candidates, ["ladder~01~s0", "ladder~02~s0"])  # one lane per task

    def test_mirror_once_skips_bundles_before_the_saved_seq(self):
        self.write_bundle("t~a", "s1r0", 1, [("e1", "phase_start")])
        self.write_bundle("t~a", "s1r0", 2, [("e2", "phase_complete")])
        dest = self.tmp / "mirror"
        state = livemod.LiveState({"t~a/s1r0": 2})  # already have seq 1
        found = livemod.mirror_once(self.store, self.cid, dest, state)
        self.assertEqual(found, {"bundles": 1, "events": 1, "boards": 0})
        self.assertEqual(sorted(p.name for p in (dest / "events").glob("*.json")), ["e2.json"])

    def test_mirror_once_with_no_bundles_yet_is_a_quiet_no_op(self):
        dest = self.tmp / "mirror"
        found = livemod.mirror_once(self.store, self.cid, dest, livemod.LiveState())
        self.assertEqual(found, {"bundles": 0, "events": 0, "boards": 0})

    def test_mirror_once_is_resumable_after_a_partial_poll(self):
        # Simulates an interrupted mirror: the first lane's progress was already saved.
        self.write_bundle("t~a", "s1r0", 1, [("e1", "phase_start")])
        self.write_bundle("t~b", "s1r0", 1, [("e2", "phase_start")])
        dest = self.tmp / "mirror"
        dest.mkdir()
        state = livemod.LiveState({"t~a/s1r0": 1})  # t~a not fetched yet, t~b neither
        found = livemod.mirror_once(self.store, self.cid, dest, state)
        self.assertEqual(found["bundles"], 2)

    def test_mirror_once_skips_a_corrupt_bundle_and_keeps_going(self):
        """A truncated/corrupt bundle must not wedge the mirror loop forever (review
        finding: an uncaught tarfile error previously meant every later poll re-hit the
        same bad bundle with nothing ever advancing)."""
        self.write_bundle("t~a", "s1r0", 1, [("e1", "phase_start")])
        path = self.base / "t~a" / "s1r0" / "000002.tar.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a valid tar.gz at all")
        self.write_bundle("t~a", "s1r0", 3, [("e2", "phase_complete")])
        dest = self.tmp / "mirror"
        state = livemod.LiveState()
        messages = []
        found = livemod.mirror_once(self.store, self.cid, dest, state, say=messages.append)
        # The good bundles (seq 1 and 3) are unpacked; the corrupt one (seq 2) is skipped,
        # warned about, and still advances state so it is never retried.
        self.assertEqual(found, {"bundles": 2, "events": 2, "boards": 0})
        self.assertEqual(state.next_seq("t~a", "s1r0"), 4)
        self.assertTrue(any("skipping unreadable bundle" in m for m in messages))
        # A second poll, state reloaded from disk, does not choke on the bad bundle again.
        reloaded = livemod.LiveState.load(dest)
        found_again = livemod.mirror_once(self.store, self.cid, dest, reloaded)
        self.assertEqual(found_again, {"bundles": 0, "events": 0, "boards": 0})

    def test_mirror_say_and_once(self):
        self.write_bundle("t~a", "s1r0", 1, [("e1", "phase_start")])
        dest = self.tmp / "mirror"
        messages = []
        report = livemod.mirror(
            self.store, self.cid, dest, once=True, say=messages.append, sleep=lambda s: None
        )
        self.assertEqual(report["polls"], 1)
        self.assertEqual(report["bundles"], 1)
        self.assertTrue(any("mirrored" in m for m in messages))

    def test_mirror_loops_until_once_without_sleeping_forever(self):
        self.write_bundle("t~a", "s1r0", 1, [("e1", "phase_start")])
        dest = self.tmp / "mirror"
        calls = []
        polls = {"n": 0}

        def fake_sleep(_seconds):
            polls["n"] += 1
            if polls["n"] >= 2:
                raise StopIteration

        with self.assertRaises(StopIteration):
            livemod.mirror(
                self.store, self.cid, dest, once=False, sleep=fake_sleep, say=calls.append
            )
        self.assertEqual(polls["n"], 2)


if __name__ == "__main__":
    unittest.main()
