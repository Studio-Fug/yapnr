"""Optional native maze kernel: the packed A* loop in C, loaded with ctypes.

Selected with ``PNR_MAZE_KERNEL=native``. The C side (``native/maze.c``) runs
only the search loop over a dense field (:mod:`.dense_maze`): the same moves,
prices, octile heuristic, ``(f, tie)`` heap order and relaxation rules as the
packed and reference kernels, compiled without floating-point contraction, so
it returns the same paths. The library is looked up at ``PNR_MAZE_LIB``, then
beside the package (Bazel runfiles); when it is absent or fails to load, the
packed Python kernel runs and :func:`status` says why. A compiled dependency is
never required.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import numpy as np

_SQRT2 = 2.0**0.5
_ABI = 2
_STATE = {"loaded": False, "kernel": None, "reason": "not requested"}


def library_names():
    return (
        ["libpnr_maze.dylib", "libpnr_maze.so"] if sys.platform == "darwin" else ["libpnr_maze.so"]
    )


def _candidates():
    configured = os.environ.get("PNR_MAZE_LIB")
    if configured:
        yield Path(configured)
    here = Path(__file__).resolve()
    for root in (here.parent / "native", here.parents[3]):
        for name in library_names():
            yield root / name


class _Field(ctypes.Structure):
    _fields_ = [
        ("nx", ctypes.c_int32),
        ("ny", ctypes.c_int32),
        ("nl", ctypes.c_int32),
        ("diagonal", ctypes.c_int32),
        ("price", ctypes.POINTER(ctypes.c_double)),
        ("via_price", ctypes.POINTER(ctypes.c_double)),
        ("ok", ctypes.POINTER(ctypes.c_uint8)),
        ("col", ctypes.POINTER(ctypes.c_uint8)),
        ("plated", ctypes.POINTER(ctypes.c_uint8)),
        ("hole", ctypes.POINTER(ctypes.c_uint8)),
        ("stencil", ctypes.POINTER(ctypes.c_uint8)),
        ("corner", ctypes.POINTER(ctypes.c_uint8)),
        ("diag", ctypes.POINTER(ctypes.c_uint8)),
        ("stencil_radius", ctypes.c_int32),
        ("via_cost", ctypes.c_double),
        ("sqrt2", ctypes.c_double),
        ("octile", ctypes.c_double),
    ]


def _pointer(array, ctype):
    return array.ctypes.data_as(ctypes.POINTER(ctype))


class NativeKernel:
    def __init__(self, library):
        self.lib = library
        library.pnr_maze_abi.restype = ctypes.c_int32
        library.pnr_maze_new.restype = ctypes.c_void_p
        library.pnr_maze_new.argtypes = [ctypes.c_int32]
        library.pnr_maze_free.argtypes = [ctypes.c_void_p]
        library.pnr_maze_search.restype = ctypes.c_int32
        library.pnr_maze_search.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Field),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
        ]
        self.context = None
        self.size = 0

    def _context(self, size):
        if self.context is None or self.size < size:
            if self.context is not None:
                self.lib.pnr_maze_free(self.context)
            self.context = self.lib.pnr_maze_new(size)
            if not self.context:
                raise MemoryError("native maze context")
            self.size = size
        return self.context

    def search(self, field, starts, ends, via_cost, diagonal, drill_sites):
        from .dense_maze import cell_of_key

        size = field.nlayers * field.nx * field.ny
        context = self._context(size)
        hole = np.ascontiguousarray(field.tree_hole(drill_sites).reshape(-1), dtype=np.uint8)
        radius, mask = field.stencil
        stencil = np.ascontiguousarray(mask, dtype=np.uint8)
        ok = field.ok.view(np.uint8)
        col = field.col.view(np.uint8)
        plated = field.plated.view(np.uint8)
        corner = field.corner.view(np.uint8)
        diag = None if field.diag is None else field.diag.view(np.uint8)
        spec = _Field(
            field.nx,
            field.ny,
            field.nlayers,
            1 if diagonal else 0,
            _pointer(field.price, ctypes.c_double),
            _pointer(field.via_price, ctypes.c_double),
            _pointer(ok, ctypes.c_uint8),
            _pointer(col, ctypes.c_uint8),
            _pointer(plated, ctypes.c_uint8),
            _pointer(hole, ctypes.c_uint8),
            _pointer(stencil, ctypes.c_uint8),
            _pointer(corner, ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint8)() if diag is None else _pointer(diag, ctypes.c_uint8),
            radius,
            float(via_cost),
            _SQRT2,
            2.0 - _SQRT2,
        )
        start = np.asarray(starts, dtype=np.int32)
        end = np.asarray(sorted(ends), dtype=np.int32)
        path = np.empty(size, dtype=np.int32)
        length = self.lib.pnr_maze_search(
            context,
            ctypes.byref(spec),
            _pointer(start, ctypes.c_int32),
            len(start),
            _pointer(end, ctypes.c_int32),
            len(end),
            _pointer(path, ctypes.c_int32),
            size,
        )
        if length < 0:
            raise RuntimeError("native maze search failed (%d)" % length)
        if length == 0:
            return None
        return [cell_of_key(field, int(k)) for k in path[:length]]

    def __del__(self):
        if self.context is not None:
            try:
                self.lib.pnr_maze_free(self.context)
            except Exception:  # interpreter shutdown
                pass


SOURCE = Path(__file__).resolve().parent / "native" / "maze.c"
# No contraction into fused multiply-adds: every double as in Python.
CFLAGS = ["-O2", "-ffp-contract=off", "-fno-fast-math", "-fPIC", "-shared"]


def build_library(out_dir, compiler=None, timeout=300):
    """Compile ``native/maze.c`` into ``out_dir`` with the host C compiler
    (``$CC`` or ``cc``) and return the library path. For runners and tests;
    Bazel builds the same source with the same flags."""
    import shutil
    import subprocess

    compiler = compiler or os.environ.get("CC") or shutil.which("cc")
    if not compiler:
        raise FileNotFoundError("no C compiler (set CC)")
    out = Path(out_dir) / library_names()[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [compiler, *CFLAGS, "-o", str(out), str(SOURCE)],
        check=True,
        timeout=timeout,
        capture_output=True,
    )
    return out


def reset():
    """Forget the loaded library (tests)."""
    _STATE.update(loaded=False, kernel=None, reason="not requested", warned=False)


def load():
    """The native kernel, or None (``status()["reason"]`` says why)."""
    if _STATE["loaded"]:
        return _STATE["kernel"]
    _STATE["loaded"] = True
    reasons = []
    for path in _candidates():
        if not path.is_file():
            continue
        try:
            library = ctypes.CDLL(str(path))
            if library.pnr_maze_abi() != _ABI:
                reasons.append("%s: ABI mismatch" % path.name)
                continue
            _STATE["kernel"] = NativeKernel(library)
            _STATE["reason"] = "loaded " + path.name
            return _STATE["kernel"]
        except (OSError, AttributeError) as error:
            reasons.append("%s: %s" % (path.name, error))
    _STATE["reason"] = "; ".join(reasons) or "library not found"
    return None


def active():
    """The native kernel when ``PNR_MAZE_KERNEL=native`` selects it and it loads."""
    from .maze import maze_kernel

    if maze_kernel() != "native":
        return None
    kernel = load()
    if kernel is None and not _STATE.get("warned"):
        _STATE["warned"] = True
        sys.stderr.write("PNR_MAZE_KERNEL=native: %s; packed kernel used\n" % _STATE["reason"])
    return kernel


def status():
    """Which kernel searches run on, for provenance: the selected kernel and, under
    ``reference_fallback``, why searches on some grid ran on the reference kernel
    instead (grid state the dense fields do not model, such as a via model)."""
    from .dense_maze import fallbacks
    from .maze import maze_kernel

    kernel = maze_kernel()
    if kernel == "native":
        out = dict(kernel="native" if load() is not None else "packed", reason=_STATE["reason"])
    else:
        out = dict(kernel=kernel, reason="")
    if kernel != "reference" and fallbacks():
        out["reference_fallback"] = fallbacks()
    return out
