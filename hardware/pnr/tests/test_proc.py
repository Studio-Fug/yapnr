"""pnr.proc: every engine subprocess is time-bounded (AGENTS.md).

The runner tests start small Python children (no KiCad). A deadline kills the
child's whole process tree, including workers it started in their own process
groups, whose own deadlines die with it. The source scan fails on any subprocess
call in the engine, its regression scripts or the regional worker tool that
neither passes ``timeout=`` nor goes through pnr.proc.
"""

import ast
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pnr import proc

PNR = Path(__file__).parent.parent  # hardware/pnr (also inside Bazel runfiles)
TOOLS = PNR.parent / "tools"
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]

# Children that are bounded otherwise, with the reason.
UNBOUNDED_OK = {
    ("pnr/pnr/proc.py", "Popen"): "the bounded runner itself",
    (
        "pnr/pnr/paired_bootstrap.py",
        "Popen",
    ): "polled parallel trial with its own deadline (_trial)",
    (
        "pnr/pnr/drc_warm/launch_host.py",
        "Popen",
    ): "the opt-in warm DRC host daemon; DrcSession.close ends it",
}
SUBPROCESS_CALLS = {"run", "call", "check_call", "check_output", "Popen"}
OS_CALLS = {"system", "popen"}


def engine_sources():
    yield from sorted(PNR.glob("pnr/**/*.py"))
    yield from sorted(PNR.glob("regression/*.py"))
    yield TOOLS / "keyhole_region.py"


def _bindings(tree):
    """Names bound to the subprocess and os modules and to their process functions,
    by ``import m [as x]`` and ``from m import f [as g]`` anywhere in the module."""
    modules, functions = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("subprocess", "os"):
                    modules[alias.asname or alias.name] = alias.name
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and node.module in ("subprocess", "os")
        ):
            wanted = SUBPROCESS_CALLS if node.module == "subprocess" else OS_CALLS
            for alias in node.names:
                if alias.name in wanted:
                    functions[alias.asname or alias.name] = (node.module, alias.name)
    return modules, functions


def unbounded_calls(sources=None, root=PNR.parent):
    """(path relative to ``root``, callee, line) of process calls that pass no timeout."""
    for path in engine_sources() if sources is None else sources:
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(), rel)
        modules, functions = _bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            ):
                module, name = modules[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in functions:
                module, name = functions[func.id]
            else:
                continue
            if module == "os":
                if name in OS_CALLS:
                    yield rel, "os." + name, node.lineno
            elif (
                name in SUBPROCESS_CALLS
                and not any(k.arg == "timeout" for k in node.keywords)
                and (rel, name) not in UNBOUNDED_OK
            ):
                yield rel, name, node.lineno


class SourceScanTest(unittest.TestCase):
    def test_every_engine_subprocess_is_bounded(self):
        self.assertEqual(
            list(unbounded_calls()),
            [],
            "bound the child: pnr.proc (run_status, run_checked, run_output) or timeout=",
        )

    def test_scan_sees_the_known_sites(self):
        text = {p.relative_to(PNR.parent).as_posix(): p.read_text() for p in engine_sources()}
        for rel, name in UNBOUNDED_OK:
            self.assertIn("subprocess." + name, text[rel], rel)

    def test_scan_catches_an_unbounded_call(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.py"
            path.write_text(
                "\n".join(
                    [
                        "import os, subprocess",  # 1
                        "import subprocess as sp",  # 2
                        "from subprocess import run, Popen as P",  # 3
                        "from os import system",  # 4
                        'subprocess.run(["a"], check=True)',  # 5 unbounded
                        'subprocess.run(["a"], timeout=5)',  # 6
                        'os.system("a")',  # 7 unbounded
                        'sp.check_output(["a"])',  # 8 unbounded
                        'run(["a"])',  # 9 unbounded
                        'P(["a"])',  # 10 unbounded
                        'system("a")',  # 11 unbounded
                        'run(["a"], timeout=1)',  # 12
                        "from pnr.proc import run_status",  # 13
                        'run_status(["a"])',  # 14 bounded by pnr.proc
                    ]
                )
                + "\n"
            )
            found = sorted(unbounded_calls([path], Path(d)))
            self.assertEqual(
                found,
                [
                    ("x.py", "Popen", 10),
                    ("x.py", "check_output", 8),
                    ("x.py", "os.system", 7),
                    ("x.py", "os.system", 11),
                    ("x.py", "run", 5),
                    ("x.py", "run", 9),
                ],
            )


def _alive(pid):
    """True while ``pid`` runs (a zombie counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.path.isdir("/proc/self"):
        try:
            with open("/proc/%d/stat" % pid) as stat:
                return stat.read().rpartition(")")[2].split()[0] != "Z"
        except OSError:
            return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _gone(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


# A child that starts a worker in its own session (as pnr.proc does by default),
# records both pids, and outlives any deadline below.
WITH_WORKER = r"""
import os, subprocess, sys, time
worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
with open(sys.argv[1] + ".tmp", "w") as out:
    out.write("%d %d" % (os.getpid(), worker.pid))
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
print("started", flush=True)
time.sleep(60)
"""


class RunnerTest(unittest.TestCase):
    def test_exit_code_and_deadline(self):
        self.assertEqual(proc.run_status([sys.executable, "-c", "raise SystemExit(3)"]), (3, False))
        started = time.monotonic()
        self.assertEqual(proc.run_status(SLEEP, timeout=0.5), (-9, True))
        self.assertLess(time.monotonic() - started, 30)

    def test_session_false_stays_in_the_callers_process_group(self):
        code = "import os,sys; sys.exit(0 if os.getpgid(0) == %d else 1)" % os.getpgid(0)
        self.assertEqual(proc.run_status([sys.executable, "-c", code], session=False), (0, False))
        self.assertEqual(proc.run_status([sys.executable, "-c", code]), (1, False))  # own group
        self.assertEqual(proc.run_status(SLEEP, timeout=0.5, session=False), (-9, True))
        self.assertEqual(
            proc.run_output([sys.executable, "-c", code], session=False, check=False).returncode, 0
        )

    def test_run_checked(self):
        self.assertEqual(proc.run_checked([sys.executable, "-c", "pass"], session=False), 0)
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            proc.run_checked([sys.executable, "-c", "raise SystemExit(4)"])
        self.assertEqual(failed.exception.returncode, 4)
        self.assertNotIsInstance(failed.exception, proc.DeadlineExceeded)
        with self.assertRaises(proc.DeadlineExceeded) as late:
            proc.run_checked(SLEEP, timeout=0.5, session=False)
        self.assertIsInstance(late.exception, subprocess.CalledProcessError)
        self.assertEqual((late.exception.returncode, late.exception.timeout), (-9, 0.5))
        self.assertIn("deadline", str(late.exception))

    def test_run_output(self):
        done = proc.run_output([sys.executable, "-c", 'print("hi")'])
        self.assertEqual((done.returncode, done.stdout), (0, "hi\n"))
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            proc.run_output(
                [sys.executable, "-c", 'import sys; sys.stderr.write("bad"); sys.exit(2)']
            )
        self.assertEqual((failed.exception.returncode, failed.exception.stderr), (2, "bad"))
        self.assertEqual(proc.run_output(SLEEP, timeout=0.5, check=False).returncode, -9)
        with self.assertRaises(proc.DeadlineExceeded):
            proc.run_output(SLEEP, timeout=0.5)

    def test_timeouts_and_overrides(self):
        with mock.patch.dict(os.environ, {"PNR_PHASE_TIMEOUT": "", "PNR_EVALUATION_TIMEOUT": ""}):
            self.assertEqual(proc.phase_timeout(), 14400)  # empty: the default
            os.environ.pop("PNR_PHASE_TIMEOUT")
            os.environ.pop("PNR_EVALUATION_TIMEOUT")
            self.assertEqual(proc.phase_timeout(), 14400)
            self.assertEqual(proc.evaluation_timeout(900), 172800)
            self.assertEqual(proc.evaluation_timeout(5400), 259200)
        with mock.patch.dict(os.environ, {"PNR_PHASE_TIMEOUT": "7", "PNR_EVALUATION_TIMEOUT": "9"}):
            self.assertEqual((proc.phase_timeout(), proc.evaluation_timeout(5400)), (7, 9))
        # 0, a negative value or inf: no limit (never "kill at once").
        for value in ("0", "-1", "inf"):
            with mock.patch.dict(
                os.environ, {"PNR_PHASE_TIMEOUT": value, "PNR_EVALUATION_TIMEOUT": value}
            ):
                self.assertEqual(
                    (proc.phase_timeout(), proc.evaluation_timeout(900)), (math.inf, math.inf)
                )
        self.assertEqual(
            proc.run_status([sys.executable, "-c", "pass"], timeout=math.inf), (0, False)
        )
        self.assertEqual(
            proc.run_output([sys.executable, "-c", "print(1)"], timeout=math.inf).stdout, "1\n"
        )
        # The worker default is read at import, as before (PNR_WORKER_TIMEOUT, 1800 s).
        self.assertEqual(proc.TIMEOUT, proc._seconds(os.environ.get("PNR_WORKER_TIMEOUT"), 1800))
        self.assertEqual(proc._seconds(None, 1800), 1800)
        self.assertEqual(proc._seconds("0", 1800), math.inf)
        with mock.patch.object(proc, "TIMEOUT", 1800.0):
            self.assertEqual(proc.worker_timeout(["python", "-m", "pnr.x"]), 1800)
            self.assertEqual(proc.worker_timeout(["x", "--seconds", "300"]), 1800)
            self.assertEqual(
                proc.worker_timeout(["x", "--seconds", "1200", "--max-seconds", 5]), 3000
            )
            self.assertEqual(proc.worker_timeout(["x", "--max-seconds", "bad", "--seconds"]), 1800)
        with mock.patch.object(proc, "TIMEOUT", math.inf):
            self.assertEqual(proc.worker_timeout(["x", "--seconds", "300"]), math.inf)


class TreeKillTest(unittest.TestCase):
    """A deadline (or an interrupted wait) ends the child's workers too."""

    def pids(self, directory):
        return tuple(int(v) for v in (Path(directory) / "pids").read_text().split())

    def test_deadline_kills_workers_in_their_own_sessions(self):
        for session in (False, True):
            with self.subTest(session=session), tempfile.TemporaryDirectory() as d:
                code, timed_out = proc.run_status(
                    [sys.executable, "-c", WITH_WORKER, str(Path(d) / "pids")],
                    timeout=3,
                    session=session,
                    stdout=subprocess.DEVNULL,
                )
                self.assertEqual((code, timed_out), (-9, True))
                child, worker = self.pids(d)
                self.assertTrue(_gone(child) and _gone(worker), (child, worker))

    def test_run_output_is_not_held_open_by_a_worker(self):
        # The worker inherits the pipes; only killing it lets the output end.
        with tempfile.TemporaryDirectory() as d:
            started = time.monotonic()
            done = proc.run_output(
                [sys.executable, "-c", WITH_WORKER, str(Path(d) / "pids")],
                timeout=3,
                check=False,
                session=False,
            )
            self.assertLess(time.monotonic() - started, 30)
            self.assertEqual((done.returncode, done.stdout), (-9, "started\n"))
            child, worker = self.pids(d)
            self.assertTrue(_gone(child) and _gone(worker), (child, worker))

    def test_interrupted_wait_kills_the_tree(self):
        class Interrupted(Exception):
            pass

        def interrupt(signum, frame):
            raise Interrupted()

        previous = signal.signal(signal.SIGALRM, interrupt)
        try:
            for call in (proc.run_status, proc.run_output):
                with self.subTest(call=call.__name__), tempfile.TemporaryDirectory() as d:
                    pids = Path(d) / "pids"
                    signal.setitimer(signal.ITIMER_REAL, 3)
                    with self.assertRaises(Interrupted):
                        call(
                            [sys.executable, "-c", WITH_WORKER, str(pids)],
                            timeout=60,
                            session=False,
                            **({"stdout": subprocess.DEVNULL} if call is proc.run_status else {})
                        )
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    child, worker = self.pids(d)
                    self.assertTrue(_gone(child) and _gone(worker), (child, worker))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_descendants(self):
        with tempfile.TemporaryDirectory() as d:
            pids = Path(d) / "pids"
            child = subprocess.Popen(
                [sys.executable, "-c", WITH_WORKER, str(pids)], stdout=subprocess.DEVNULL
            )
            try:
                deadline = time.monotonic() + 30
                while not pids.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                _, worker = self.pids(d)
                self.assertIn(worker, proc.descendants(child.pid))
                self.assertNotIn(os.getpid(), proc.descendants(child.pid))
            finally:
                proc.kill_tree(child)
                child.wait()
            self.assertTrue(_gone(worker), worker)


class TrialDeadlineTest(unittest.TestCase):
    def test_parallel_pair_trial_is_bounded(self):
        import threading

        from pnr import paired_bootstrap

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "trial.log"
            pids = Path(d) / "pids"
            with self.assertRaises(proc.DeadlineExceeded):
                paired_bootstrap._trial(
                    [sys.executable, "-c", WITH_WORKER, str(pids)],
                    log,
                    dict(os.environ),
                    threading.Event(),
                    poll=0.05,
                    timeout=3,
                )
            self.assertIn("deadline", log.read_text())
            child, worker = (int(v) for v in pids.read_text().split())
            self.assertTrue(_gone(child) and _gone(worker), (child, worker))


if __name__ == "__main__":
    unittest.main()
