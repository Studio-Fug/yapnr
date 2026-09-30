"""Bounded subprocesses for the atopile runner: a deadline, and the whole process tree on timeout.

The same contract as the engine's ``pnr.proc`` (AGENTS.md: a subprocess without a timeout is a
bug), kept here as a small stdlib module until PR3 moves ``pnr.proc`` to ``yapnr.runtime``:

- the child starts in its own session (process group);
- on its deadline, or when the wait is interrupted, every process of the tree is stopped
  (SIGSTOP, repeated until no new process appears, so nothing can fork away), then killed with
  SIGKILL, with every process group one of them leads. atopile's forkserver and build workers are
  part of the tree, so none outlives a killed build;
- after a normal exit, whatever is left in the child's process group (a worker the child did not
  reap) is killed too, so nothing outlives a finished build either.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple


def _parents() -> Dict[int, int]:
    """{pid: parent pid} of every process ({} if the table cannot be read)."""
    table: Dict[int, int] = {}
    if os.path.isdir("/proc/self"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8") as stat:
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


def descendants(pid: int) -> List[int]:
    children: Dict[int, List[int]] = {}
    for child, parent in _parents().items():
        children.setdefault(parent, []).append(child)
    found, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child)
    return found


def _signal(pid: int, sig: int, group: bool = False) -> None:
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except OSError:
        pass


def kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL ``proc`` (started with ``start_new_session=True``) and all it started."""
    own_group = os.getpgid(0)
    stopped: set = set()
    for _ in range(8):
        new = [pid for pid in [proc.pid] + descendants(proc.pid) if pid not in stopped]
        if not new:
            break
        for pid in new:
            _signal(pid, signal.SIGSTOP)
            stopped.add(pid)
    groups = {proc.pid}
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


def _reap_group(pgid: int) -> None:
    """SIGKILL what remains of the process group ``pgid`` (the exited child led it)."""
    if pgid != os.getpgid(0):
        _signal(pgid, signal.SIGKILL, group=True)


def run(
    cmd: Sequence[str],
    timeout: float,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Tuple[int, bool]:
    """Run ``cmd`` with stdout and stderr to ``log_path``; returns (exit code, timed out).

    A killed child reports -9 with ``timed out`` True.
    """
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            list(cmd),
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=timeout)
            _reap_group(proc.pid)
            return code, False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            return -9, True
        except BaseException:
            kill_tree(proc)
            proc.wait()
            raise
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def output(cmd: Sequence[str], timeout: float, env: Optional[Dict[str, str]] = None) -> str:
    """The stdout of a short command under a deadline (CalledProcessError on failure)."""
    proc = subprocess.Popen(
        list(cmd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        raise subprocess.CalledProcessError(-9, list(cmd), "", f"killed after {timeout:g} s")
    except BaseException:
        kill_tree(proc)
        proc.wait()
        raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, list(cmd), out, err)
    return out
