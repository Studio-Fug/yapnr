"""Headless subprocess runner with a hard wall-clock limit (stdlib only).

Workers run in their own process group; on timeout the whole group is killed so a
wedged child (or grandchild) can never stall an evaluation. PNR_WORKER_TIMEOUT
(seconds, default 1800) sets the limit.
"""
import os
import signal
import subprocess

TIMEOUT = float(os.environ.get('PNR_WORKER_TIMEOUT', '1800'))


def run_status(cmd, timeout=None, **kwargs):
    """Run ``cmd``; return ``(exit code, timed_out)``.

    ``timed_out`` is True only when this function killed the process group on its
    own deadline (the exit code is then -9). A child killed by SIGKILL from
    elsewhere (e.g. the out-of-memory killer) also exits -9, but with
    ``timed_out`` False, so callers can tell the two apart.
    """
    proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    try:
        return proc.wait(timeout=TIMEOUT if timeout is None else timeout), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return -9, True


def run(cmd, timeout=None, **kwargs):
    """Run ``cmd``; return its exit code, or -9 after killing it on timeout."""
    return run_status(cmd, timeout=timeout, **kwargs)[0]
