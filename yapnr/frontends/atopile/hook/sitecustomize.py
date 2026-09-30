"""Loads the yapnr atopile hook at interpreter start (see yapnr_atopile_hook.py).

``yapnr atopile build`` puts this directory first on ``PYTHONPATH`` for atopile's processes, so
Python's ``site`` module imports this file. Another ``sitecustomize`` later on the path (an
interpreter may ship one) still runs, after the hook is installed.
"""

import importlib.util
import os
import sys

import yapnr_atopile_hook

yapnr_atopile_hook.install()


def _chain() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        if os.path.abspath(entry or os.curdir) == here:
            continue
        candidate = os.path.join(entry or os.curdir, "sitecustomize.py")
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location("_yapnr_chained_sitecustomize", candidate)
            if spec is not None and spec.loader is not None:
                spec.loader.exec_module(importlib.util.module_from_spec(spec))
            return


_chain()
