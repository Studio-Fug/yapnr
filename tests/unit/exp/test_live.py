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
from yapnr.exp.cloud import FakeCloud
from yapnr.exp.store import GcsStore, LocalStore


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


class GcsBinaryBundleTest(unittest.TestCase):
    """The real path a live bundle takes on GCP: uploaded by LiveUploader (a local tar.gz),
    read back through ``GcsStore.read_bytes`` -> ``Gcloud.cat_bytes``, over FakeCloud's model
    of ``gcloud storage cat``.

    This caught a real bug (found on an actual GCP campaign, not in CI): every other test in
    this file uses ``LocalStore``, which reads bundles straight off disk and never exercises
    this path at all. The general ``Gcloud.run()`` always decodes stdout as UTF-8 (every other
    gcloud call -- JSON, listings, describes -- is text), which corrupts a tar.gz the moment it
    hits a byte that is not valid UTF-8 (gzip's own magic byte, 0x8b, already is not), raising
    ``UnicodeDecodeError`` before ``read_bytes`` even returns. ``cat_bytes`` exists so a binary
    object never goes through that decode.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cloud = FakeCloud()
        self.runs = GcsStore("example-yapnr-runs", self.cloud)

    def tearDown(self):
        self._tmp.cleanup()

    def _upload_one_bundle(self, cid):
        work = self.tmp / "work"
        remote_prefix = "campaigns/%s/live/t~a/s1r0" % cid
        live = wrapper.LiveUploader(
            {"enabled": True, "interval_s": 10, "mode": "full"}, work, self.tmp / "unused"
        )
        write_board(live.live_dir, "d" * 64, b"a kicad_pcb board, binary-ish on purpose \x00\xff")
        write_event(live.live_dir, "e1", "route_result", candidate="t~a", board_sha256="d" * 64)
        live.sync()
        local_bundle = (self.tmp / "unused" / "000001.tar.gz").read_bytes()
        self.cloud.objects["gs://example-yapnr-runs/%s/000001.tar.gz" % remote_prefix] = (
            local_bundle
        )
        return local_bundle

    def test_read_bytes_round_trips_a_real_gzip_bundle_byte_for_byte(self):
        cid = "20261005-ladder-aaaaaa"
        raw = self._upload_one_bundle(cid)
        fetched = self.runs.read_bytes("campaigns/%s/live/t~a/s1r0/000001.tar.gz" % cid)
        self.assertEqual(fetched, raw)  # byte-for-byte: no UTF-8 round trip in between
        added = livemod.unpack_bundle(fetched, self.tmp / "mirror")
        self.assertEqual(added, {"events": 1, "boards": 1})

    def test_mirror_once_over_a_real_gcs_store_unpacks_the_bundle(self):
        """The same scenario, but through ``mirror_once`` end to end (list_bundles's glob,
        then read_bytes, then unpack) -- what ``yapnr exp live`` actually calls."""
        cid = "20261005-ladder-bbbbbb"
        self._upload_one_bundle(cid)
        dest = self.tmp / "mirror2"
        found = livemod.mirror_once(self.runs, cid, dest, livemod.LiveState())
        self.assertEqual(found, {"bundles": 1, "events": 1, "boards": 1})
        events = [json.loads(p.read_text()) for p in (dest / "events").glob("*.json")]
        self.assertEqual(len(events), 1)
        self.assertEqual(livemod.event_errors(events[0]), [])


class SynthesizeTaskEventsTest(unittest.TestCase):
    """yapnr.exp.live.synthesize_task_events: a synthetic terminal event per _DONE-marked task
    lane, and the campaign-finished marker once every task named by tasks.jsonl has one -- how a
    finished campaign's ladder "case" lanes (no terminal event of their own, before
    hardware/pnr/regression/run.py's fix) stop reading "stalled" forever once it is over."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store_root = self.tmp / "store"
        self.store = LocalStore(self.store_root)
        self.cid = "20261006-ladder-abc123"
        self.prefix = self.store_root / "campaigns" / self.cid

    def tearDown(self):
        self._tmp.cleanup()

    def write_tasks_jsonl(self, task_ids):
        self.prefix.mkdir(parents=True, exist_ok=True)
        (self.prefix / "tasks.jsonl").write_text(
            "".join(json.dumps({"id": tid}) + "\n" for tid in task_ids)
        )

    def write_done(self, task_id, attempt="s1r0", verdict="pass", record_extra=None):
        key = livemod.task_key(task_id)
        task_dir = self.prefix / "tasks" / key
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "_DONE").write_text(
            json.dumps({"task": task_id, "attempt": attempt, "verdict": verdict}) + "\n"
        )
        if record_extra is not None:
            record_dir = task_dir / attempt
            record_dir.mkdir(parents=True, exist_ok=True)
            record = dict(exit_code=0, wall_s=12.5, timed_out=False)
            record.update(record_extra)
            (record_dir / "record.json").write_text(json.dumps(record))

    def test_writes_one_synthetic_event_per_done_task(self):
        self.write_tasks_jsonl(["ladder/case-a/s0", "ladder/case-b/s0"])
        self.write_done("ladder/case-a/s0", verdict="pass")
        dest = self.tmp / "mirror"
        found = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertEqual(found, {"tasks": 1, "synthetic_events": 1, "finished": False})
        events = [json.loads(p.read_text()) for p in (dest / "events").glob("*.json")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["candidate"], "ladder/case-a/s0")
        self.assertEqual(events[0]["kind"], livemod.SYNTHETIC_KIND)
        self.assertEqual(events[0]["data"]["verdict"], "pass")
        self.assertTrue(events[0]["synthetic"])
        self.assertEqual(livemod.event_errors(events[0]), [])  # a real schema-valid event

    def test_is_idempotent_and_resumable(self):
        self.write_tasks_jsonl(["ladder/case-a/s0"])
        self.write_done("ladder/case-a/s0")
        dest = self.tmp / "mirror"
        first = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertEqual(first["synthetic_events"], 1)
        second = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertEqual(second["synthetic_events"], 0)  # already on disk, not re-added
        self.assertEqual(len(list((dest / "events").glob("*.json"))), 1)

    def test_finished_marker_appears_only_once_every_task_is_done(self):
        self.write_tasks_jsonl(["ladder/case-a/s0", "ladder/case-b/s0"])
        self.write_done("ladder/case-a/s0")
        dest = self.tmp / "mirror"
        found = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertFalse(found["finished"])
        self.assertFalse((dest / livemod.FINISHED_MARKER).exists())
        self.write_done("ladder/case-b/s0", verdict="fail")
        found = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertTrue(found["finished"])
        marker = json.loads((dest / livemod.FINISHED_MARKER).read_text())
        self.assertEqual(marker["campaign"], self.cid)
        self.assertEqual(marker["tasks"], 2)

    def test_no_tasks_jsonl_yet_is_never_finished(self):
        # A mirror polling before the campaign's own plan has uploaded: _DONE markers alone
        # (there are none yet either) must never look "finished".
        dest = self.tmp / "mirror"
        found = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertEqual(found, {"tasks": 0, "synthetic_events": 0, "finished": False})

    def test_record_json_extras_are_folded_in_best_effort(self):
        self.write_tasks_jsonl(["t/a"])
        self.write_done("t/a", attempt="s1r0", record_extra={"exit_code": 1, "wall_s": 42.0})
        dest = self.tmp / "mirror"
        livemod.synthesize_task_events(self.store, self.cid, dest)
        event = json.loads(next((dest / "events").glob("*.json")).read_text())
        self.assertEqual(event["data"]["exit_code"], 1)
        self.assertEqual(event["data"]["wall_s"], 42.0)

    def test_missing_record_json_is_silently_ignored(self):
        self.write_tasks_jsonl(["t/a"])
        self.write_done("t/a")  # no record.json written at all
        dest = self.tmp / "mirror"
        found = livemod.synthesize_task_events(self.store, self.cid, dest)
        self.assertEqual(found["synthetic_events"], 1)  # the marker alone is already enough

    def test_mirror_calls_synthesize_and_reports_it(self):
        self.write_tasks_jsonl(["t/a"])
        self.write_done("t/a")
        dest = self.tmp / "mirror"
        report = livemod.mirror(self.store, self.cid, dest, once=True, sleep=lambda s: None)
        self.assertEqual(report["synthetic_events"], 1)
        self.assertTrue(report["finished"])


if __name__ == "__main__":
    unittest.main()
