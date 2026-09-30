"""Headless subprocess runner with a hard wall-clock limit (stdlib only).

Every child process of the engine goes through this module (AGENTS.md: a
subprocess without a timeout is a bug). The limits, in seconds, are generous
wedge guards: they end a hung child and never bind a healthy one.

- ``PNR_WORKER_TIMEOUT`` (default 1800): one KiCad worker or tool process, the
  default of every function here. Read once, at import. A worker given its own
  search budget gets at least twice that plus 600 s (:func:`worker_timeout`).
- ``PNR_PHASE_TIMEOUT`` (default 14400, :func:`phase_timeout`): a phase that
  itself runs bounded workers in sequence (a ``pnr.full_iteration`` phase, the
  transaction cleanup of the regional adapter when it runs on its own).
- ``PNR_EVALUATION_TIMEOUT`` (default max(172800, 48 x the native budget),
  :func:`evaluation_timeout`): one whole ``pnr.full_iteration`` evaluation.

Each is a number of seconds. ``0``, a negative value or ``inf`` means no limit
(an explicit opt-out, for debugging); an empty value means the default.

A child's deadline is enforced by its parent alone, so on timeout the parent
kills the child's whole process tree (:func:`kill_tree`): the child, every
process it started, however deep (the workers of a killed ``pnr.full_iteration``
run in their own process groups, and their deadlines died with it), and every
process group one of them leads. The same happens when the wait is interrupted
(``KeyboardInterrupt`` or any other exception), as ``subprocess.run`` kills its
child then.

By default a child runs in its own process group (``session=True``).
``session=False`` keeps the child in the caller's process group, as a plain
``subprocess.run`` does, so stopping the caller's group (Ctrl-C, ``kill -- -PGID``)
still stops it; the child's own ``session=True`` workers are outside that group,
as they always were.
"""

import math
import os
import signal
import subprocess

# How long a killed child's pipes may stay open (held by a process outside its tree).
_DRAIN_SECONDS = 30


def _seconds(value, default):
    """A limit from the environment: ``default`` if unset or empty; <= 0 or inf means no limit."""
    value = (value or "").strip()
    if not value:
        return float(default)
    seconds = float(value)
    return seconds if 0 < seconds < math.inf else math.inf


TIMEOUT = _seconds(os.environ.get("PNR_WORKER_TIMEOUT"), 1800)


class DeadlineExceeded(subprocess.CalledProcessError):
    """A child killed at its deadline; ``returncode`` is -9, as :func:`run` reports it."""

    def __init__(self, cmd, timeout, output=None, stderr=None):
        super().__init__(-9, cmd, output, stderr)
        self.timeout = timeout

    def __str__(self):
        return "Command %r killed after its %g s deadline" % (self.cmd, self.timeout)


def worker_timeout(cmd=()):
    """The deadline of one worker: PNR_WORKER_TIMEOUT, or 2 x its own budget + 600 s if longer.

    The budget is the value after ``--seconds`` or ``--max-seconds`` in ``cmd``
    (the larger if both); without one it is PNR_WORKER_TIMEOUT.
    """
    args = [str(arg) for arg in cmd]
    budget = 0.0
    for flag in ("--seconds", "--max-seconds"):
        if flag in args[:-1]:
            try:
                budget = max(budget, float(args[args.index(flag) + 1]))
            except ValueError:
                pass
    return max(TIMEOUT, 2.0 * budget + 600.0)


def phase_timeout():
    """PNR_PHASE_TIMEOUT (seconds, default 14400 = 4 h) for a sequence of bounded workers."""
    return _seconds(os.environ.get("PNR_PHASE_TIMEOUT"), 14400)


def evaluation_timeout(budget_seconds):
    """PNR_EVALUATION_TIMEOUT (seconds), else max(48 h, 48 x ``budget_seconds``).

    Bounds one ``pnr.full_iteration`` child whose native phases get ``budget_seconds``.
    Its wall time is far above that budget on a loaded machine (the hierarchical
    runs H2 to H7: up to 18.3 h for a 5400 s deep budget, 12x, and 6.9 h for a
    900 s native budget, 28x), so this is a wedge guard, not a budget.
    """
    return _seconds(
        os.environ.get("PNR_EVALUATION_TIMEOUT"), max(172800.0, 48.0 * float(budget_seconds))
    )


def _limit(timeout):
    return TIMEOUT if timeout is None else timeout


def _wait_seconds(limit):
    """The ``timeout=`` of a wait: None (no deadline) when the limit is infinite."""
    return None if math.isinf(limit) else limit


def _parents():
    """{pid: parent pid} of every process ({} if the table cannot be read)."""
    table = {}
    if os.path.isdir("/proc/self"):  # Linux; a container may have no ps
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % entry) as stat:
                    # "pid (comm) state ppid ...": comm may contain spaces and parentheses.
                    table[int(entry)] = int(stat.read().rpartition(")")[2].split()[1])
            except (OSError, ValueError, IndexError):
                pass
        return table
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            table[int(fields[0])] = int(fields[1])
    return table


def descendants(pid):
    """The pids of every live descendant of ``pid`` (a snapshot)."""
    children = {}
    for child, parent in _parents().items():
        children.setdefault(parent, []).append(child)
    found, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child)
    return found


def _signal(pid, sig, group=False):
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except OSError:  # gone already, or not ours
        pass


def kill_tree(proc, session=False):
    """SIGKILL ``proc`` (a :class:`subprocess.Popen`) and every process it started.

    The tree is stopped first (SIGSTOP, listed again until nothing new appears),
    so no process can fork, or be re-parented out of the tree by its parent's
    death, between listing and killing. Then every stopped process is killed,
    with every process group one of them leads (a ``session=True`` worker's
    group, which may hold processes whose parent already exited) and, with
    ``session``, ``proc``'s own group. The caller's process group is never
    signalled as a group.
    """
    own_group = os.getpgid(0)
    stopped = set()
    for _ in range(8):
        new = [pid for pid in [proc.pid] + descendants(proc.pid) if pid not in stopped]
        if not new:
            break
        for pid in new:
            _signal(pid, signal.SIGSTOP)
            stopped.add(pid)
    groups = {proc.pid} if session else set()
    for pid in stopped:
        try:
            if os.getpgid(pid) == pid:
                groups.add(pid)
        except OSError:
            pass
    groups.discard(own_group)
    for group in groups:
        _signal(group, signal.SIGKILL, group=True)
    for pid in stopped:
        _signal(pid, signal.SIGKILL)


def run_status(cmd, timeout=None, session=True, **kwargs):
    """Run ``cmd``; return ``(exit code, timed_out)``.

    ``timed_out`` is True only when this function killed the child on its own
    deadline (the exit code is then -9). A child killed by SIGKILL from
    elsewhere (e.g. the out-of-memory killer) also exits -9, but with
    ``timed_out`` False, so callers can tell the two apart.
    """
    proc = subprocess.Popen(cmd, start_new_session=session, **kwargs)
    try:
        return proc.wait(timeout=_wait_seconds(_limit(timeout))), False
    except subprocess.TimeoutExpired:
        kill_tree(proc, session)
        proc.wait()
        return -9, True
    except BaseException:  # interrupted: the child must not outlive the wait
        kill_tree(proc, session)
        proc.wait()
        raise


def run(cmd, timeout=None, **kwargs):
    """Run ``cmd``; return its exit code, or -9 after killing it on timeout."""
    return run_status(cmd, timeout=timeout, **kwargs)[0]


def run_checked(cmd, timeout=None, **kwargs):
    """``subprocess.run(cmd, check=True)`` under a deadline.

    Raises :class:`subprocess.CalledProcessError` on a non-zero exit, and its
    subclass :class:`DeadlineExceeded` (returncode -9) on timeout, so callers that
    treat a failed worker as a rejected transaction keep doing so.
    """
    code, timed_out = run_status(cmd, timeout=timeout, **kwargs)
    if timed_out:
        raise DeadlineExceeded(cmd, _limit(timeout))
    if code:
        raise subprocess.CalledProcessError(code, cmd)
    return code


def _close(proc):
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            pipe.close()
    proc.wait()


def _drain(proc):
    """The rest of a killed child's output; gives up if a process outside its tree holds the pipes."""
    try:
        return proc.communicate(timeout=_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        _close(proc)
        return None, None


def run_output(
    cmd, timeout=None, check=True, capture_output=True, text=True, session=True, **kwargs
):
    """``subprocess.run(cmd, capture_output=True, text=True, check=True)`` under a deadline.

    Returns the :class:`subprocess.CompletedProcess` (returncode -9 on timeout,
    with the output read so far). On timeout the child's whole tree is killed,
    so no descendant keeps the pipes open.
    """
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, start_new_session=session, text=text, **kwargs)
    limit = _limit(timeout)
    try:
        out, err = proc.communicate(timeout=_wait_seconds(limit))
        code, timed_out = proc.returncode, False
    except subprocess.TimeoutExpired:
        kill_tree(proc, session)
        out, err = _drain(proc)
        code, timed_out = -9, True
    except BaseException:  # interrupted: the child must not outlive the wait
        kill_tree(proc, session)
        _close(proc)
        raise
    if check and timed_out:
        raise DeadlineExceeded(cmd, limit, out, err)
    if check and code:
        raise subprocess.CalledProcessError(code, cmd, out, err)
    return subprocess.CompletedProcess(cmd, code, out, err)
