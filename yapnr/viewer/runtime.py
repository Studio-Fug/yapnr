"""The engine runtime the viewer's subprocess services run, and their environments.

The viewer imports the engine (``pnr``) in-process for the runtime controls. The component-cost
replay, the schematic builder and the notes MCP server run as child processes of the hermetic
Python, and two scripts run under KiCad's Python. Each needs an explicit import path:

- rules_python's bootstrap sets ``sys.path`` in-process and exports no ``PYTHONPATH``, so a child
  of the hermetic Python would lose the pip dependencies (numpy, torch, PyYAML) and this package.
  :func:`hermetic_env` passes ``PYTHONPATH = <engine runtime> + sys.path``.
- KiCad's Python (3.9 on macOS) must not see the 3.11 site-packages: :func:`kicad_env` passes the
  engine runtime only.

The *engine runtime* is the directory that contains the ``pnr`` package. By default it is the one
this process imports; ``--engine-runtime`` points at a frozen copy instead (the engine a run was
made with), which then comes first on the child's path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

# Variables that belong to a running experiment, never to a viewer's replay.
_EXPERIMENT_ENV = ("PNR_LIVE_DIR", "PNR_PROFILE_DIR", "PNR_COST_CAPTURE_DIR")


def imported_runtime() -> Optional[Path]:
    """The directory holding the ``pnr`` package on this interpreter's path, else None."""
    try:
        spec = importlib.util.find_spec("pnr")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent.parent


def engine_runtime(explicit: Optional[os.PathLike] = None) -> Optional[Path]:
    """The engine runtime for child processes: ``explicit`` if given, else the imported one."""
    if explicit:
        return Path(explicit).absolute()
    return imported_runtime()


def _unique(paths: Iterable[str]) -> list:
    out = []
    for p in paths:
        if p and p not in out:
            out.append(p)
    return out


def child_pythonpath(runtime: Optional[os.PathLike] = None) -> str:
    """``PYTHONPATH`` for a hermetic child: the runtime first, then this process's ``sys.path``."""
    return os.pathsep.join(_unique([str(runtime) if runtime else "", *sys.path]))


def _base_env(threads: bool) -> Dict[str, str]:
    env = dict(os.environ)
    for name in _EXPERIMENT_ENV:
        env.pop(name, None)
    if threads:
        env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    return env


def hermetic_env(runtime: Optional[os.PathLike] = None, threads: bool = True) -> Dict[str, str]:
    """Environment for a child of this interpreter (``sys.executable -m ...``)."""
    env = _base_env(threads)
    env["PYTHONPATH"] = child_pythonpath(runtime)
    return env


def kicad_env(runtime: Optional[os.PathLike]) -> Dict[str, str]:
    """Environment for a script run by KiCad's Python: the engine runtime only on its path."""
    env = _base_env(threads=True)
    env.pop("PYTHONSAFEPATH", None)
    env.pop("PYTHONHOME", None)
    if runtime:
        env["PYTHONPATH"] = str(runtime)
    else:
        env.pop("PYTHONPATH", None)
    return env


def module_command(module: str, *args: str) -> list:
    """``[sys.executable, -m, module, *args]``: run a module of this package in a child."""
    return [sys.executable, "-m", module, *args]
