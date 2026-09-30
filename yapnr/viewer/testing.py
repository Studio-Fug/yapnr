"""Helpers for the viewer's tests: child environments, fake executables and viewer processes.

The viewer itself never imports this module. It lives in the package because the repository's
test folders hold only ``test_*.py`` files.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence, Tuple

from yapnr.viewer import runtime

# tests/fixtures/viewer next to the tests (runfiles or a checkout).
FIXTURE_DIR = Path("tests/fixtures/viewer")


def fixture(*parts: str) -> Path:
    """A path under tests/fixtures/viewer, from Bazel runfiles or the checkout."""
    roots = []
    srcdir = os.environ.get("TEST_SRCDIR")
    if srcdir:
        roots.append(Path(srcdir) / os.environ.get("TEST_WORKSPACE", "_main"))
    roots.append(Path(__file__).parent.parent.parent)
    for root in roots:
        p = root / FIXTURE_DIR.joinpath(*parts)
        if p.exists():
            return p.absolute()
    raise FileNotFoundError(f"test fixture {FIXTURE_DIR.joinpath(*parts)} not found")


def fixture_copy(*parts: str) -> Path:
    """A private copy of a fixture (regular files, symlinks followed), removed at exit.

    Bazel runfiles are symlinks, and the atopile source browser skips symlinked files on
    purpose (they could point out of the source folder), so tests that read sources use a copy.
    """
    import atexit
    import shutil
    import tempfile

    src = fixture(*parts)
    tmp = Path(tempfile.mkdtemp(prefix="viewer-fixture-"))
    atexit.register(shutil.rmtree, tmp, True)
    dest = tmp / src.name
    shutil.copytree(src, dest, symlinks=False)
    return dest


def child_env(**extra: str) -> dict:
    """Environment for a child Python of the test: this interpreter's import path, no machine
    config (``YAPNR_USER_CONFIG`` empty) and no KiCad or claude found by accident."""
    env = runtime.hermetic_env(threads=False)
    env["YAPNR_USER_CONFIG"] = ""
    for name in ("YAPNR_KICAD_CLI", "PNR_KICAD_CLI", "YAPNR_KICAD_PYTHON"):
        env.pop(name, None)
    env.update(extra)
    return env


def module_argv(module: str, *args: str) -> list:
    return [sys.executable, "-m", module, *args]


def write_fake(path: os.PathLike, script: str, python: Optional[str] = None) -> Path:
    """An executable ``path`` that runs the Python ``script`` (saved as ``<path>.py``).

    A ``#!/bin/sh`` wrapper instead of a ``#!<python>`` shebang: the interpreter paths of Bazel
    runfiles are longer than the kernel's shebang limit on Linux.
    """
    path = Path(path)
    body = path.with_name(path.name + ".py")
    body.write_text(script)
    path.write_text(
        '#!/bin/sh\nexec %s %s "$@"\n'
        % (shlex.quote(python or sys.executable), shlex.quote(str(body)))
    )
    path.chmod(0o755)
    return path


def start_viewer(
    root: os.PathLike, *args: str, env: Optional[dict] = None, stderr=subprocess.PIPE
) -> Tuple[subprocess.Popen, str]:
    """Start ``python -m yapnr.viewer`` on an ephemeral loopback port; (process, base URL)."""
    proc = subprocess.Popen(
        module_argv(
            "yapnr.viewer", "--root", str(root), "--port", "0", "--listen", "127.0.0.1", *args
        ),
        env=env or child_env(),
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )
    line = proc.stdout.readline().strip()
    if not line.startswith("yapnr viewer: "):
        proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        proc.wait(10)
        raise RuntimeError(f"viewer did not start: {line!r} {err[-2000:]}")
    return proc, line.split(": ", 1)[1]


def stop(proc: subprocess.Popen, timeout: float = 10) -> int:
    """Terminate a started viewer and close its pipes; its exit code."""
    if proc.poll() is None:
        proc.terminate()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(timeout)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()
    return code


def wait_for(fn, timeout: float = 20, step: float = 0.05):
    """fn()'s first truthy result within timeout seconds (AssertionError otherwise)."""
    end = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(step)


def argv_value(argv: Sequence[str], flag: str) -> str:
    return argv[list(argv).index(flag) + 1]
