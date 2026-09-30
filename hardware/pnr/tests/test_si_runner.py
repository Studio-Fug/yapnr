"""ngspice runner isolation with fake backends: timeout, crash, garbage, flat, retry-once,
machine-wide slots, child self-deadline, niceness without preexec_fn."""

import atexit
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pnr import proc
from pnr.si import metrics, runner

_SCRATCH = tempfile.TemporaryDirectory(prefix="pnr-si-test-")  # removed at interpreter exit
atexit.register(_SCRATCH.cleanup)

FAKE = textwrap.dedent(
    """
    import math, os, signal, time
    MODE = %(mode)r
    if MODE == 'nice':
        open(%(mark)r, 'w').write(str(os.getpriority(os.PRIO_PROCESS, 0)))
    MARK = %(mark)r
    class Backend:
        def __init__(self, lib, codemodels, log):
            self.log = log
        def command(self, cmd):
            if cmd == 'run':
                if MODE == 'hang':
                    time.sleep(3600)
                if MODE == 'crash':
                    os.kill(os.getpid(), signal.SIGSEGV)
                if MODE == 'errorlog':
                    self.log.append('stderr Error: singular matrix: check node n4')
                if MODE == 'flaky' and not os.path.exists(MARK):
                    open(MARK, 'w').close()
                    os._exit(3)
            return 0
        def circ(self, lines):
            self.lines = lines
            return 0
        def vector(self, name):
            t = [i * 1e-11 for i in range(8201)]
            if MODE == 'short':
                t = t[:10]
            if name == 'time':
                return t[:100] if MODE == 'ragged' else t
            def edge(x, tau):
                if x < 2e-9: return 0.0
                if x < 42e-9: return 5 * (1 - math.exp(-(x - 2e-9) / tau))
                return 5 * math.exp(-(x - 42e-9) / tau)
            if MODE == 'nan' and name == 'rx':
                return [float('nan')] * len(t)
            if MODE == 'flat' and name == 'rx':
                return [0.001] * len(t)
            if MODE == 'missing' and name == 'conn':
                raise KeyError(name)
            return [edge(x, 1e-9 if name == 'rx' else 2e-10) for x in t]
"""
)


def fake(mode):
    d = tempfile.mkdtemp(prefix="pnr-si-fake-", dir=_SCRATCH.name)
    path = Path(d) / ("fake_%s.py" % mode)
    path.write_text(FAKE % dict(mode=mode, mark=str(Path(d) / "mark")))
    return str(path)


def check(t, v):
    return metrics.sanity(t, v["rx"], v["drv"], rail=5.0, t_rise=2e-9, t_fall=42e-9, t_end=82e-9)


DECK = "* fake deck\n.end\n"


class RunnerTest(unittest.TestCase):
    def run_mode(self, mode, **kw):
        kw.setdefault("timeout", 20)
        return runner.run_deck(
            DECK, ["drv", "conn", "rx"], tstop=82e-9, lib=fake(mode), check=check, **kw
        )

    def test_ok(self):
        r = self.run_mode("ok")
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(len(r["attempts"]), 1)
        self.assertEqual(len(r["t"]), 8201)
        m = metrics.edge_metrics(
            r["t"],
            r["v"]["rx"],
            r["v"]["drv"],
            rail=5,
            vih=3.5,
            vil=1.5,
            t_rise=2e-9,
            t_fall=42e-9,
            t_end=82e-9,
        )
        self.assertAlmostEqual(m["rise_10_90_ns"], 2.197, delta=0.01)

    def test_hang_is_killed_and_retried_once(self):
        t0 = time.monotonic()
        r = self.run_mode("hang", timeout=2)
        self.assertEqual(r["status"], "error")
        self.assertIn("timeout", r["error"])
        self.assertEqual(len(r["attempts"]), 2)
        self.assertLess(time.monotonic() - t0, 15)

    def test_crash_is_an_error(self):
        r = self.run_mode("crash")
        self.assertEqual(r["status"], "error")
        self.assertIn("runner exit", r["error"])
        self.assertEqual(len(r["attempts"]), 2)

    def test_garbage_results_are_errors(self):
        for mode, needle in (
            ("nan", "non-finite"),
            ("flat", "flat"),
            ("short", "time points"),
            ("ragged", "length mismatch"),
            ("missing", "missing vector conn"),
            ("errorlog", "singular matrix"),
        ):
            r = self.run_mode(mode)
            self.assertEqual(r["status"], "error", mode)
            self.assertIn(needle, r["error"], mode)

    def test_retry_once_recovers(self):
        r = self.run_mode("flaky")
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual([a["exit"] for a in r["attempts"]], [3, 0])

    def test_no_retry(self):
        r = self.run_mode("crash", retries=0)
        self.assertEqual(len(r["attempts"]), 1)

    def test_work_dir_keeps_deck_and_logs(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_mode("ok", work_dir=d)
            self.assertEqual(r["status"], "ok")
            self.assertTrue((Path(d) / "deck.cir").exists())
            self.assertTrue((Path(d) / "out0.json").exists())

    def test_child_interpreter_never_from_gui_bundle(self):
        r = runner.run_deck(
            DECK,
            ["drv"],
            lib=fake("ok"),
            env={
                "PNR_SI_PYTHON": "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
            },
        )
        self.assertEqual(r["status"], "error")
        self.assertIn("PNR_SI_PYTHON", r["error"])
        self.assertEqual(r["attempts"], [])

    def test_child_is_niced_without_preexec_fn(self):
        lib = fake("nice")
        r = runner.run_deck(
            DECK,
            ["drv", "conn", "rx"],
            tstop=82e-9,
            lib=lib,
            check=check,
            timeout=20,
            env=dict(os.environ, PNR_SI_NICE="7"),
        )
        self.assertEqual(r["status"], "ok", r)
        mark = Path(lib).parent / "mark"
        self.assertGreaterEqual(int(mark.read_text()), os.getpriority(os.PRIO_PROCESS, 0) + 7)
        self.assertNotIn("preexec_fn", inspect.getsource(runner.run_deck))

    def test_child_dies_on_its_own_deadline(self):
        """The child arms its own alarm: with no parent watching (parent timeout far away) it still dies."""
        d = Path(tempfile.mkdtemp(prefix="pnr-si-orphan-", dir=_SCRATCH.name))
        (d / "deck.cir").write_text(DECK)
        job = d / "job.json"
        job.write_text(
            json.dumps(
                dict(
                    deck=str(d / "deck.cir"),
                    lib=fake("hang"),
                    codemodels=[],
                    vectors=["rx"],
                    out=str(d / "out"),
                    deadline_s=1.0,
                    nice=0,
                )
            )
        )
        t0 = time.monotonic()
        code, timed_out = proc.run_status(
            [sys.executable, "-m", "pnr.si.runner", str(job)],
            timeout=60,
            env=dict(os.environ, PYTHONPATH=str(runner.PNR_ROOT)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual((code, timed_out), (-signal.SIGALRM, False))
        self.assertLess(time.monotonic() - t0, 15)

    def test_external_sigkill_is_not_reported_as_timeout(self):
        code, timed_out = proc.run_status(
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
            timeout=30,
        )
        self.assertEqual((code, timed_out), (-9, False))
        code, timed_out = proc.run_status(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
        )
        self.assertEqual((code, timed_out), (-9, True))
        self.assertEqual(
            proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5), -9
        )

    def test_bounded_cmd_nices_and_deadlines_external_tools(self):
        t0 = time.monotonic()
        code, timed_out = proc.run_status(runner.bounded_cmd(["/bin/sleep", "30"], 1.0), timeout=60)
        self.assertEqual((code, timed_out), (-signal.SIGALRM, False))
        self.assertLess(time.monotonic() - t0, 15)
        out = subprocess.run(
            runner.bounded_cmd(["/bin/sh", "-c", "ps -o nice= -p $$"], 30.0, {"PNR_SI_NICE": "5"}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Relative to this process: CI runners may start jobs at a negative nice (e.g. -10 on macOS).
        self.assertGreaterEqual(int(out.stdout.strip()), min(19, os.nice(0) + 5))

    def test_library_resolution_refuses_gui_bundle(self):
        with self.assertRaises(RuntimeError):
            runner.find_lib(
                {
                    "PNR_NGSPICE_LIB": "/Applications/KiCad/KiCad.app/Contents/PlugIns/sim/libngspice.0.dylib"
                }
            )
        lib = fake("ok")
        self.assertEqual(runner.find_lib({"PNR_NGSPICE_LIB": lib}), (lib, ""))
        with self.assertRaises(FileNotFoundError):
            runner.find_lib({"PNR_NGSPICE_LIB": "/nonexistent/libngspice.dylib"})


class SlotTest(unittest.TestCase):
    """Machine-wide cap: PNR_SI_SLOTS flock slots shared by every thread and process."""

    def env(self, n=2, wait=30):
        d = tempfile.mkdtemp(prefix="pnr-si-slots-", dir=_SCRATCH.name)
        return dict(PNR_SI_SLOT_DIR=d, PNR_SI_SLOTS=str(n), PNR_SI_SLOT_WAIT=str(wait))

    def test_threads_never_exceed_the_cap(self):
        env = self.env(2)
        live, peak, lock = [0], [0], threading.Lock()

        def work():
            with runner.slot(env):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.2)
                with lock:
                    live[0] -= 1

        ts = [threading.Thread(target=work) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(peak[0], 2)

    def test_other_processes_hold_slots_and_crashes_release_them(self):
        env = self.env(1, wait=0.5)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time\nsys.path.insert(0, %r)\n"
                'from pnr.si import runner\nwith runner.slot(%r):\n    print("held", flush=True)\n'
                "    time.sleep(60)" % (str(runner.PNR_ROOT), env),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(runner.SlotTimeout):
                with runner.slot(env):
                    pass
            r = runner.run_deck(
                DECK,
                ["drv", "conn", "rx"],
                tstop=82e-9,
                lib=fake("ok"),
                check=check,
                env=env,
                timeout=20,
            )
            self.assertEqual(r["status"], "error")  # fail closed, never a hang
            self.assertIn("no free SI slot", r["error"])
        finally:
            holder.kill()  # SIGKILL: no cleanup code runs
            holder.wait()
            holder.stdout.close()
        with runner.slot(env, wait_s=5) as i:  # the kernel released the dead holder's lock
            self.assertEqual(i, 0)

    def test_run_deck_takes_a_slot(self):
        env = self.env(1)
        with mock.patch.object(runner, "slot", wraps=runner.slot) as spy:
            r = runner.run_deck(
                DECK,
                ["drv", "conn", "rx"],
                tstop=82e-9,
                lib=fake("ok"),
                check=check,
                env=env,
                timeout=20,
            )
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(spy.call_count, 1)
        self.assertIn("slot_wait_s", r["attempts"][0])


@unittest.skipUnless(os.environ.get("PNR_SI_LIVE") == "1", "live ngspice (PNR_SI_LIVE=1)")
class LiveRunnerTest(unittest.TestCase):
    def test_rc_1k_10p(self):
        deck = (
            "rc\nV1 in 0 PULSE(0 5 1n 10p 10p 100n 200n)\nR1 in a 1k\nC1 a 0 10p\n"
            ".tran 10p 100n 0 25p\n.end\n"
        )
        r = runner.run_deck(deck, ["a", "in"], tstop=100e-9)
        self.assertEqual(r["status"], "ok", r)
        up = [x for x, s in metrics.crossings(r["t"], r["v"]["a"], 4.5) if s == 1][0]
        lo = [x for x, s in metrics.crossings(r["t"], r["v"]["a"], 0.5) if s == 1][0]
        self.assertAlmostEqual((up - lo) * 1e9, 21.97, delta=21.97 * 0.005)


if __name__ == "__main__":
    unittest.main()
