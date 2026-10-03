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

    def test_resume_restores_the_checkpoint(self):
        script = (
            "import json, os, sys; os.makedirs('out/run', exist_ok=True)\n"
            "p = 'out/run/state.json'\n"
            "n = json.load(open(p))['n'] if os.path.exists(p) else 0\n"
            "json.dump({'n': n + 1}, open(p, 'w'))\n"
            "if n == 0: sys.exit(75)\n"
            "json.dump({'resumed_from': n}, open('out/done.json', 'w'))\n"
        )
        checkpoint = {"path": "out/run", "sync_every_s": 30, "on_signal": True}
        self.campaign(testing.python_task("t/rf", script, restart="resume", checkpoint=checkpoint))
        # The first attempt checkpoints and fails transiently, so it records nothing...
        first = self.run_index(0)
        self.assertEqual(first.returncode, wrapper.EXIT_TEMPFAIL, first.stdout + first.stderr)
        # ... but its state was not synced: a tempfail exits before the final sync. Simulate the
        # periodic sync of a long run instead, then resume.
        ckpt = self.store / "checkpoints" / testing.CID / "t~rf"
        ckpt.mkdir(parents=True)
        (ckpt / "state.json").write_text(json.dumps({"n": 1}))
        (ckpt / "_checkpoint.json").write_text(json.dumps({"kind": "dir", "files": ["state.json"]}))
        second = self.run_index(0, env={"BATCH_TASK_RETRY_ATTEMPT": "1"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        marker = self.marker("t/rf")
        attempt = self.store / "campaigns" / testing.CID / "tasks" / "t~rf" / marker["attempt"]
        self.assertEqual(
            json.loads((attempt / "summary" / "done.json").read_text()), {"resumed_from": 1}
        )
        self.assertEqual(json.loads((ckpt / "state.json").read_text()), {"n": 2})

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
