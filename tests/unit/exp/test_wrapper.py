"""The in-task wrapper (yapnr/exp/task.py) against a local store: the exit-code contract,
_DONE markers, preemption and retry, checkpoints, summaries and pruning, chunks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.exp import task as wrapper
from yapnr.exp import testing

WRITE_DONE = (
    "import json, os; os.makedirs('out', exist_ok=True); "
    "json.dump({'complete': True, 'passed': %s}, open('out/done.json', 'w'))"
)


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self.toolchain = testing.host_toolchain(self.tmp / "toolchain.json")

    def tearDown(self):
        self._tmp.cleanup()

    def campaign(self, *tasks):
        return testing.write_store_campaign(self.store, list(tasks))

    def run_index(self, index, **kw):
        return testing.run_wrapper(self.store, index, self.toolchain, **kw)

    def marker(self, task_id):
        path = (
            self.store / "campaigns" / testing.CID / "tasks" / task_id.replace("/", "~") / "_DONE"
        )
        return json.loads(path.read_text()) if path.is_file() else None

    def test_pass_and_fail_are_results_and_exit_0(self):
        verdict = {"file": "out/done.json", "json_path": "passed"}
        done = {"complete": True}
        base = self.campaign(
            testing.python_task("t/pass", WRITE_DONE % "True", verdict=verdict, done_json=done),
            testing.python_task("t/fail", WRITE_DONE % "False", verdict=verdict, done_json=done),
        )
        for index, want in ((0, "pass"), (1, "fail")):
            result = self.run_index(index)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = self.marker(["t/pass", "t/fail"][index])
            self.assertEqual(marker["verdict"], want)
            attempt = base / "tasks" / marker["task"].replace("/", "~") / marker["attempt"]
            record = json.loads((attempt / "record.json").read_text())
            self.assertEqual(record["verdict"], want)
            self.assertEqual(record["backend"], "local")
            self.assertIsNone(record["image"]["ref"])  # a host toolchain, not the image
            self.assertTrue((attempt / "summary" / "done.json").is_file())
            self.assertTrue((attempt / "result.tar.gz").is_file())
            self.assertTrue((attempt / "log.tail").is_file())

    def test_done_task_is_skipped(self):
        self.campaign(testing.python_task("t/a", WRITE_DONE % "True"))
        self.assertEqual(self.run_index(0).returncode, 0)
        first = self.marker("t/a")
        result = self.run_index(0)
        self.assertEqual(result.returncode, 0)
        self.assertIn('"skip"', result.stdout)
        self.assertEqual(self.marker("t/a"), first)

    def test_crash_is_recorded_as_error_not_retried(self):
        self.campaign(testing.python_task("t/crash", "raise SystemExit(3)"))
        result = self.run_index(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.marker("t/crash")["verdict"], "error")

    def test_tempfail_without_result_is_retried_and_records_nothing(self):
        self.campaign(testing.python_task("t/flaky", "raise SystemExit(75)"))
        result = self.run_index(0)
        self.assertEqual(result.returncode, wrapper.EXIT_TEMPFAIL)
        self.assertIsNone(self.marker("t/flaky"))

    def test_frozen_store_stops_before_work(self):
        self.campaign(testing.python_task("t/a", WRITE_DONE % "True"))
        (self.store / "control").mkdir()
        (self.store / "control" / "frozen").write_text("budget\n")
        self.assertEqual(self.run_index(0).returncode, wrapper.EXIT_FROZEN)
        self.assertIsNone(self.marker("t/a"))

    def test_tampered_task_is_refused(self):
        base = self.campaign(testing.python_task("t/a", WRITE_DONE % "True"))
        lines = (base / "tasks.jsonl").read_text().replace("out/done.json", "out/other.json")
        (base / "tasks.jsonl").write_text(lines)
        self.assertEqual(self.run_index(0).returncode, wrapper.EXIT_USAGE)

    def test_missing_bundle_is_transient(self):
        task = testing.python_task("t/a", WRITE_DONE % "True")
        task["inputs"] = [{"dest": "src", "bundle": "0" * 64, "kind": "source"}]
        self.campaign(task)
        self.assertEqual(self.run_index(0).returncode, wrapper.EXIT_TEMPFAIL)

    def test_preemption_then_retry(self):
        script = "import time; time.sleep(%s); " + WRITE_DONE % "True"
        self.campaign(testing.python_task("t/long", script % "4"))
        wrapper_py = self.store / "campaigns" / testing.CID / "task.py"
        argv = [
            sys.executable,
            str(wrapper_py),
            "--store",
            str(self.store),
            "--inputs",
            str(self.store / "bundles"),
            "--campaign",
            testing.CID,
            "--submission",
            "1",
            "--index",
            "0",
            "--toolchain",
            str(self.toolchain),
        ]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if '"staged"' in line:
                break
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)  # what Batch sends a container on Spot preemption
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, wrapper.EXIT_TEMPFAIL, out)
        self.assertIsNone(self.marker("t/long"))
        result = self.run_index(0, env={"BATCH_TASK_RETRY_ATTEMPT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.marker("t/long")["attempt"], "s1r1")

    RESUMING = (
        "import json, os, sys; os.makedirs('out/run', exist_ok=True)\n"
        "p = 'out/run/state.json'\n"
        "n = json.load(open(p))['n'] if os.path.exists(p) else 0\n"
        "json.dump({'n': n + 1}, open(p, 'w'))\n"
        "if n == 0: sys.exit(75)\n"
        "json.dump({'resumed_from': n}, open('out/done.json', 'w'))\n"
    )

    def resumable(self, task_id):
        checkpoint = {"path": "out/run", "sync_every_s": 30, "on_signal": True}
        return testing.python_task(task_id, self.RESUMING, restart="resume", checkpoint=checkpoint)

    def resumed_from(self, task_id):
        marker = self.marker(task_id)
        key = task_id.replace("/", "~")
        attempt = self.store / "campaigns" / testing.CID / "tasks" / key / marker["attempt"]
        return json.loads((attempt / "summary" / "done.json").read_text())["resumed_from"]

    def test_resume_restores_the_checkpoint(self):
        self.campaign(self.resumable("t/rf"))
        # The first attempt checkpoints and exits 75 (its time is up), so it records nothing...
        first = self.run_index(0)
        self.assertEqual(first.returncode, wrapper.EXIT_TEMPFAIL, first.stdout + first.stderr)
        self.assertIsNone(self.marker("t/rf"))
        # ... but its last checkpoint is synced before the wrapper exits 75.
        ckpt = self.store / "checkpoints" / testing.CID / "t~rf"
        manifest = json.loads((ckpt / "_checkpoint.json").read_text())
        self.assertEqual(manifest["files"], {"state.json": "g1/state.json"})
        self.assertEqual(json.loads((ckpt / "g1" / "state.json").read_text()), {"n": 1})
        second = self.run_index(0, env={"BATCH_TASK_RETRY_ATTEMPT": "1"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.resumed_from("t/rf"), 1)
        # The final sync wrote a new generation and removed the superseded one.
        manifest = json.loads((ckpt / "_checkpoint.json").read_text())
        self.assertEqual(manifest["files"], {"state.json": "g2/state.json"})
        self.assertEqual(json.loads((ckpt / "g2" / "state.json").read_text()), {"n": 2})
        self.assertFalse((ckpt / "g1").exists())

    def test_resume_reads_the_first_layout(self):
        self.campaign(self.resumable("t/old"))
        # A checkpoint of the first layout: the files flat beside a manifest that lists them.
        ckpt = self.store / "checkpoints" / testing.CID / "t~old"
        ckpt.mkdir(parents=True)
        (ckpt / "state.json").write_text(json.dumps({"n": 1}))
        (ckpt / "_checkpoint.json").write_text(json.dumps({"kind": "dir", "files": ["state.json"]}))
        result = self.run_index(0, env={"BATCH_TASK_RETRY_ATTEMPT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.resumed_from("t/old"), 1)
        # The final sync wrote a new generation and removed the superseded first-layout file.
        manifest = json.loads((ckpt / "_checkpoint.json").read_text())
        self.assertEqual(manifest["files"], {"state.json": "g1/state.json"})
        self.assertEqual(json.loads((ckpt / "g1" / "state.json").read_text()), {"n": 2})
        self.assertFalse((ckpt / "state.json").exists())

    def checkpoint(self, root):
        task = {"restart": "resume", "checkpoint": {"path": "run", "sync_every_s": 30}}
        return wrapper.Checkpoints(task, root, self.store / "checkpoints" / "t")

    def test_a_cut_short_sync_leaves_the_last_whole_checkpoint(self):
        # Two files that only make sense together (a field and its iteration count).
        work = self.tmp / "work1"
        (work / "run").mkdir(parents=True)
        (work / "run" / "field.npz").write_text("field@10")
        (work / "run" / "history.json").write_text("10")
        (work / "run" / ".field.npz.tmp").write_text("an application's temporary file")
        first = self.checkpoint(work)
        self.assertTrue(first.sync())
        self.assertFalse(first.sync())  # nothing changed, nothing written
        time.sleep(0.01)
        (work / "run" / "field.npz").write_text("field@20")
        (work / "run" / "history.json").write_text("20")
        # Spot preemption: the container is killed after the first file of the next sync.
        copies = []
        real = wrapper.copy_atomic

        def dies_on_second(src, dest):
            copies.append(dest)
            if len(copies) == 2:
                raise OSError("killed")
            real(src, dest)

        wrapper.copy_atomic = dies_on_second
        try:
            with self.assertRaises(OSError):
                first.sync()
        finally:
            wrapper.copy_atomic = real
        # The retry restores generation 1 whole, not field@20 with history 10.
        work2 = self.tmp / "work2"
        second = self.checkpoint(work2)
        self.assertTrue(second.restore())
        self.assertEqual((work2 / "run" / "field.npz").read_text(), "field@10")
        self.assertEqual((work2 / "run" / "history.json").read_text(), "10")
        self.assertFalse((work2 / "run" / ".field.npz.tmp").exists())
        # Its next sync starts generation 2 and removes the half-written attempt's leftovers.
        (work2 / "run" / "history.json").write_text("11")
        self.assertTrue(second.sync())
        ckpt = self.store / "checkpoints" / "t"
        manifest = json.loads((ckpt / "_checkpoint.json").read_text())
        self.assertEqual(
            manifest["files"], {"field.npz": "g1/field.npz", "history.json": "g2/history.json"}
        )
        self.assertEqual(sorted(p.name for p in (ckpt / "g2").iterdir()), ["history.json"])
        self.assertFalse((ckpt / "g1" / "history.json").exists())

    def test_a_checkpoint_with_a_missing_file_is_not_restored(self):
        work = self.tmp / "work1"
        (work / "run").mkdir(parents=True)
        (work / "run" / "a").write_text("a")
        (work / "run" / "b").write_text("b")
        self.checkpoint(work).sync()
        (self.store / "checkpoints" / "t" / "g1" / "b").unlink()
        work2 = self.tmp / "work2"
        self.assertFalse(self.checkpoint(work2).restore())
        self.assertFalse((work2 / "run" / "a").exists())  # no half-restored mix

    def test_summary_and_prune_globs(self):
        script = (
            "import os, json; os.makedirs('out/a/big', exist_ok=True)\n"
            "open('out/a/big/board.kicad_pcb', 'w').write('x' * 1000)\n"
            "json.dump({}, open('out/a/result.json', 'w'))\n"
            "json.dump({}, open('out/done.json', 'w'))\n"
        )
        base = self.campaign(
            testing.python_task(
                "t/p", script, summary=["a/*.json", "done.json"], prune=["a/big/*.kicad_pcb"]
            )
        )
        self.assertEqual(self.run_index(0).returncode, 0)
        marker = self.marker("t/p")
        attempt = base / "tasks" / "t~p" / marker["attempt"]
        record = json.loads((attempt / "record.json").read_text())
        self.assertEqual(record["summary_files"], ["a/result.json", "done.json"])
        self.assertEqual(record["pruned"], 1)
        import tarfile

        with tarfile.open(attempt / "result.tar.gz") as tar:
            names = tar.getnames()
        self.assertNotIn("out/a/big/board.kicad_pcb", names)
        self.assertIn("out/a/result.json", names)

    def test_chunk_runs_consecutive_tasks(self):
        tasks = [testing.python_task("t/%d" % i, WRITE_DONE % "True") for i in range(5)]
        self.campaign(*tasks)
        result = self.run_index(1, extra=["--chunk", "2"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual([t["id"] for t in tasks if self.marker(t["id"])], ["t/2", "t/3"])

    def test_environment_is_scrubbed_and_home_isolated(self):
        script = (
            "import json, os; os.makedirs('out', exist_ok=True)\n"
            "json.dump({k: os.environ.get(k) for k in ('HOME', 'GOOGLE_X', 'PNR_FOO', "
            "'XDG_CONFIG_HOME', 'YAPNR_ENGINE_REVISION', 'MINE')}, open('out/done.json', 'w'))\n"
        )
        base = self.campaign(testing.python_task("t/env", script, env={"MINE": "1"}))
        result = self.run_index(0, env={"GOOGLE_X": "secret", "PNR_FOO": "1"})
        self.assertEqual(result.returncode, 0)
        attempt = base / "tasks" / "t~env" / self.marker("t/env")["attempt"]
        seen = json.loads((attempt / "summary" / "done.json").read_text())
        self.assertIsNone(seen["GOOGLE_X"])
        self.assertIsNone(seen["PNR_FOO"])
        self.assertEqual(seen["MINE"], "1")
        self.assertEqual(seen["YAPNR_ENGINE_REVISION"], "f" * 40)
        self.assertTrue(seen["HOME"].endswith(os.path.join(".yapnr", "home")))
        self.assertEqual(seen["XDG_CONFIG_HOME"], os.path.join(seen["HOME"], ".config"))


class RunCommandTest(unittest.TestCase):
    def test_wall_time_limit_kills_the_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            task = testing.python_task("t/x", "pass")
            checkpoints = wrapper.Checkpoints(task, work, work / "ckpt")
            stop = wrapper.Stop()
            start = time.monotonic()
            code, timed_out, stopped, _ = wrapper.run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                work,
                dict(os.environ),
                1,
                0,
                stop,
                checkpoints,
                work / "logs",
                False,
            )
            self.assertTrue(timed_out)
            self.assertIsNone(stopped)
            self.assertLess(time.monotonic() - start, 30)

    def test_the_campaign_runtime_names_the_image_python(self):
        # An image other than yapnr's (no /opt/venv): ${PYTHON} is the campaign's runtime.python.
        verdict = {"file": "out/done.json", "json_path": "passed"}
        task = testing.python_task("t/rt", WRITE_DONE % "True", verdict=verdict)
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            base = testing.write_store_campaign(store, [task])
            meta = json.loads((base / "campaign.json").read_text())
            meta["image"] = {
                "ref": "registry.example/solver/image@" + testing.DIGEST,
                "runtime": {"python": sys.executable, "entrypoint": ""},
            }
            (base / "campaign.json").write_text(json.dumps(meta))
            result = testing.run_wrapper(
                store, 0, "image", extra=["--work-root", str(Path(tmp) / "work")]
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = json.loads((base / "tasks" / "t~rt" / "_DONE").read_text())
            self.assertEqual(marker["verdict"], "pass")

    def test_with_launcher_uses_the_task_entrypoint_in_the_image(self):
        task = {"entrypoint": sys.executable}
        argv = wrapper.with_launcher(["x"], task, {"use_task_entrypoint": True, "launcher": []})
        self.assertEqual(argv, [sys.executable, "x"])
        self.assertEqual(
            wrapper.with_launcher(
                ["x"], {"entrypoint": "/nonexistent"}, wrapper.TOOLCHAINS["image"]
            ),
            ["x"],
        )
        self.assertEqual(wrapper.with_launcher(["x"], task, {"launcher": []}), ["x"])


if __name__ == "__main__":
    unittest.main()
