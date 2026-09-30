"""pnr.proc: every engine subprocess is time-bounded (AGENTS.md).

The runner tests start small Python children (no KiCad). The source scan fails on
any subprocess call in the engine, its regression scripts or the regional worker
tool that neither passes ``timeout=`` nor goes through pnr.proc.
"""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from pnr import proc

PNR = Path(__file__).parent.parent  # hardware/pnr (also inside Bazel runfiles)
TOOLS = PNR.parent / 'tools'
SLEEP = [sys.executable, '-c', 'import time; time.sleep(60)']

# Children that are bounded otherwise, with the reason.
UNBOUNDED_OK = {
    ('pnr/pnr/proc.py', 'Popen'): 'the bounded runner itself',
    ('pnr/pnr/paired_bootstrap.py', 'Popen'): 'polled parallel trial with its own deadline (_trial)',
    ('pnr/pnr/drc_warm/launch_host.py', 'Popen'): 'the opt-in warm DRC host daemon; DrcSession.close ends it',
}
CALLS = {'run', 'call', 'check_call', 'check_output', 'Popen'}


def engine_sources():
    yield from sorted(PNR.glob('pnr/**/*.py'))
    yield from sorted(PNR.glob('regression/*.py'))
    yield TOOLS / 'keyhole_region.py'


def unbounded_calls():
    """(path, callee, line) of subprocess/os calls that pass no timeout."""
    for path in engine_sources():
        rel = path.relative_to(PNR.parent).as_posix()
        for node in ast.walk(ast.parse(path.read_text(), rel)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            name = owner.id if isinstance(owner, ast.Name) else None
            if name == 'os' and node.func.attr in ('system', 'popen'):
                yield rel, 'os.' + node.func.attr, node.lineno
            elif name == 'subprocess' and node.func.attr in CALLS:
                if not any(k.arg == 'timeout' for k in node.keywords) and \
                        (rel, node.func.attr) not in UNBOUNDED_OK:
                    yield rel, node.func.attr, node.lineno


class SourceScanTest(unittest.TestCase):
    def test_every_engine_subprocess_is_bounded(self):
        self.assertEqual(list(unbounded_calls()), [],
                         'bound the child: pnr.proc (run_status, run_checked, run_output) or timeout=')

    def test_scan_sees_the_known_sites(self):
        text = {p.relative_to(PNR.parent).as_posix(): p.read_text() for p in engine_sources()}
        for rel, name in UNBOUNDED_OK:
            self.assertIn('subprocess.' + name, text[rel], rel)

    def test_scan_catches_an_unbounded_call(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'x.py'
            path.write_text('import os, subprocess\nsubprocess.run(["a"], check=True)\n'
                            'subprocess.run(["a"], timeout=5)\nos.system("a")\n')
            found = [(n.func.attr, n.lineno) for n in ast.walk(ast.parse(path.read_text()))
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and not any(k.arg == 'timeout' for k in n.keywords)]
            self.assertEqual(sorted(found), [('run', 2), ('system', 4)])


class RunnerTest(unittest.TestCase):
    def test_exit_code_and_deadline(self):
        self.assertEqual(proc.run_status([sys.executable, '-c', 'raise SystemExit(3)']), (3, False))
        started = time.monotonic()
        self.assertEqual(proc.run_status(SLEEP, timeout=.5), (-9, True))
        self.assertLess(time.monotonic() - started, 30)

    def test_session_false_stays_in_the_callers_process_group(self):
        code = 'import os,sys; sys.exit(0 if os.getpgid(0) == %d else 1)' % os.getpgid(0)
        self.assertEqual(proc.run_status([sys.executable, '-c', code], session=False), (0, False))
        self.assertEqual(proc.run_status([sys.executable, '-c', code]), (1, False))  # own group
        self.assertEqual(proc.run_status(SLEEP, timeout=.5, session=False), (-9, True))

    def test_run_checked(self):
        self.assertEqual(proc.run_checked([sys.executable, '-c', 'pass'], session=False), 0)
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            proc.run_checked([sys.executable, '-c', 'raise SystemExit(4)'])
        self.assertEqual(failed.exception.returncode, 4)
        self.assertNotIsInstance(failed.exception, proc.DeadlineExceeded)
        with self.assertRaises(proc.DeadlineExceeded) as late:
            proc.run_checked(SLEEP, timeout=.5, session=False)
        self.assertIsInstance(late.exception, subprocess.CalledProcessError)
        self.assertEqual((late.exception.returncode, late.exception.timeout), (-9, .5))
        self.assertIn('deadline', str(late.exception))

    def test_run_output(self):
        done = proc.run_output([sys.executable, '-c', 'print("hi")'])
        self.assertEqual((done.returncode, done.stdout), (0, 'hi\n'))
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            proc.run_output([sys.executable, '-c', 'import sys; sys.stderr.write("bad"); sys.exit(2)'])
        self.assertEqual((failed.exception.returncode, failed.exception.stderr), (2, 'bad'))
        self.assertEqual(proc.run_output(SLEEP, timeout=.5, check=False).returncode, -9)
        with self.assertRaises(proc.DeadlineExceeded):
            proc.run_output(SLEEP, timeout=.5)

    def test_timeouts_and_overrides(self):
        with mock.patch.dict(os.environ, {'PNR_PHASE_TIMEOUT': '', 'PNR_EVALUATION_TIMEOUT': ''}):
            os.environ.pop('PNR_PHASE_TIMEOUT'); os.environ.pop('PNR_EVALUATION_TIMEOUT')
            self.assertEqual(proc.phase_timeout(), 14400)
            self.assertEqual(proc.evaluation_timeout(900), 172800)
            self.assertEqual(proc.evaluation_timeout(5400), 259200)
        with mock.patch.dict(os.environ, {'PNR_PHASE_TIMEOUT': '7', 'PNR_EVALUATION_TIMEOUT': '9'}):
            self.assertEqual((proc.phase_timeout(), proc.evaluation_timeout(5400)), (7, 9))
        # The worker default is read at import, as before (PNR_WORKER_TIMEOUT, 1800 s).
        self.assertEqual(proc.TIMEOUT, float(os.environ.get('PNR_WORKER_TIMEOUT', '1800')))
        with mock.patch.object(proc, 'TIMEOUT', 1800.0):
            self.assertEqual(proc.worker_timeout(['python', '-m', 'pnr.x']), 1800)
            self.assertEqual(proc.worker_timeout(['x', '--seconds', '300']), 1800)
            self.assertEqual(proc.worker_timeout(['x', '--seconds', '1200', '--max-seconds', 5]), 3000)
            self.assertEqual(proc.worker_timeout(['x', '--max-seconds', 'bad', '--seconds']), 1800)


class TrialDeadlineTest(unittest.TestCase):
    def test_parallel_pair_trial_is_bounded(self):
        import threading
        from pnr import paired_bootstrap
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'trial.log'
            with self.assertRaises(proc.DeadlineExceeded):
                paired_bootstrap._trial(SLEEP, log, dict(os.environ), threading.Event(), poll=.05, timeout=.5)
            self.assertIn('deadline', log.read_text())


if __name__ == '__main__':
    unittest.main()
