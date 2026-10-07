"""Native maze kernel: the packed A* loop in C, loaded with ctypes.

The default kernel (``PNR_MAZE_KERNEL`` unset or ``native``; :func:`.maze.maze_kernel`). The C
side (``native/maze.c``) runs only the search loop over a dense field (:mod:`.dense_maze`): the
same moves, prices, octile heuristic, ``(f, tie)`` heap order and relaxation rules as the packed
and reference kernels, compiled without floating-point contraction, so it returns the same
paths, ties included, on every machine (IEEE doubles, no libm, no fused multiply-add: the loader
probes that). The library is looked up at ``PNR_MAZE_LIB``, beside the package (a local build,
Bazel's runfiles), then in the installed yapnr wheel (``yapnr/native/``, which the container
image carries); a library is refused unless it records the sha256 of the ``maze.c`` beside this
module, so a stale one never runs. When none loads, the packed Python kernel runs (the same
routes, slower) and :func:`status` says why. A compiled dependency is never required.
"""

from __future__ import annotations

import ctypes
import hashlib
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


# Where the yapnr wheel carries the library (//yapnr/native:libpnr_maze.so).
WHEEL_DIR = "yapnr/native"


def _installed():
    """The installed yapnr wheel's libraries (found through its metadata, so a frozen
    ``yapnr`` on ``PYTHONPATH`` does not hide them)."""
    try:
        from importlib import metadata

        dist = metadata.distribution("yapnr")
        return [Path(dist.locate_file("%s/%s" % (WHEEL_DIR, name))) for name in library_names()]
    except Exception:  # not installed, or no metadata
        return []


def _candidates():
    configured = os.environ.get("PNR_MAZE_LIB")
    if configured:
        yield Path(configured)
    # Beside the package (a local build), then Bazel's runfiles, where
    # //hardware/pnr:libpnr_maze.so lands: the module's own (unresolved) path is inside the
    # runfiles tree, the resolved one is the source tree.
    roots = []
    for here in (Path(os.path.abspath(__file__)), Path(__file__).resolve()):
        roots += [here.parent / "native", here.parents[3]]
    for env in ("RUNFILES_DIR", "TEST_SRCDIR"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]) / "_main" / "hardware" / "pnr")
    seen = set()
    for root in roots:
        for name in library_names():
            if root / name not in seen:
                seen.add(root / name)
                yield root / name
    yield from _installed()


def source_sha256():
    """sha256 of ``native/maze.c`` beside this module (what a library must record), or None
    when it is not there."""
    try:
        return hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    except OSError:
        return None


def fp_check(library):
    """Why the library's double arithmetic is not Python's, or None: ``pnr_maze_fp_probe``
    computes x*y - p with p = fl(x*y) (0 unless fused into a multiply-add) and (u + v) - u
    with v below u's ulp (0 unless reassociated), on inputs the compiler cannot see."""
    eps = 2.0**-30
    x = 1.0 + eps
    probe = (ctypes.c_double * 5)(x, x, x * x, 1.0, eps * eps)
    out = (ctypes.c_double * 2)(float("nan"), float("nan"))
    library.pnr_maze_fp_probe(probe, out)
    problems = []
    if out[0] != 0.0:
        problems.append("multiply-add fused")
    if out[1] != 0.0:
        problems.append("sums reassociated")
    return "; ".join(problems) or None


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


def _bytes(mask):
    """A flat uint8 array of a boolean mask's values: its own bytes when it is
    contiguous (numpy stores a bool as one 0 or 1 byte), else a converted copy."""
    flat = mask.reshape(-1)
    if flat.dtype == np.bool_ and flat.flags.c_contiguous:
        return flat.view(np.uint8)
    return np.ascontiguousarray(flat, dtype=np.uint8)


class NativeKernel:
    """The loaded library, refused (OSError) unless its arithmetic is Python's
    (:func:`fp_check`) and it records the sha256 of the ``maze.c`` beside this module."""

    def __init__(self, library, path=None):
        self.context = None
        self.lib = library
        self.path = None if path is None else Path(path)
        library.pnr_maze_src_sha.restype = ctypes.c_char_p
        library.pnr_maze_fp_probe.restype = None
        library.pnr_maze_fp_probe.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        problem = fp_check(library)
        if problem:
            raise OSError("not Python's arithmetic (%s): build with %s" % (problem, CFLAGS[1]))
        self.src_sha = library.pnr_maze_src_sha().decode()
        here = source_sha256()
        if here and self.src_sha != here:
            raise OSError(
                "stale: built from maze.c %s, this is %s"
                % (self.src_sha[:12] or "(unrecorded)", here[:12])
            )
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
        self._last = None  # (key, field, spec, its hole pointer, the arrays it points into)
        self._path = None  # the path buffer, reused

    def _context(self, size):
        if self.context is None or self.size < size:
            if self.context is not None:
                self.lib.pnr_maze_free(self.context)
            self.context = self.lib.pnr_maze_new(size)
            if not self.context:
                raise MemoryError("native maze context")
            self.size = size
        return self.context

    def _spec(self, field, diagonal, via_cost):
        """The ctypes description of ``field`` (its ``hole`` member set per search).
        Kept for the last field searched, as long as the field holds the same arrays:
        a net's tree searches its field once per terminal. The cache holds the arrays
        and their byte views, so the pointers stay theirs; the C loop reads them
        in place, so a change to an array's values is seen as before."""
        stencil = field.stencil
        arrays = (
            field.price,
            field.via_price,
            field.ok,
            field.col,
            field.plated,
            field.corner,
            field.diag,
            field.hole,
            stencil[1],
        )
        key = (bool(diagonal), float(via_cost), stencil[0]) + tuple(map(id, arrays))
        cached = self._last
        if cached is not None and cached[0] == key and cached[1] is field:
            return cached[2], cached[3]
        price = np.ascontiguousarray(field.price, dtype=np.float64)
        via_price = np.ascontiguousarray(field.via_price, dtype=np.float64)
        ok = field.ok.view(np.uint8)
        col = field.col.view(np.uint8)
        plated = field.plated.view(np.uint8)
        corner = field.corner.view(np.uint8)
        diag = None if field.diag is None else field.diag.view(np.uint8)
        mask = np.ascontiguousarray(stencil[1], dtype=np.uint8)
        hole = _bytes(field.hole)
        # Its own pointer object: one read back from the structure aliases the member,
        # which later searches set to their tree's mask.
        hole_pointer = _pointer(hole, ctypes.c_uint8)
        spec = _Field(
            field.nx,
            field.ny,
            field.nlayers,
            1 if diagonal else 0,
            _pointer(price, ctypes.c_double),
            _pointer(via_price, ctypes.c_double),
            _pointer(ok, ctypes.c_uint8),
            _pointer(col, ctypes.c_uint8),
            _pointer(plated, ctypes.c_uint8),
            hole_pointer,
            _pointer(mask, ctypes.c_uint8),
            _pointer(corner, ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint8)() if diag is None else _pointer(diag, ctypes.c_uint8),
            stencil[0],
            float(via_cost),
            _SQRT2,
            2.0 - _SQRT2,
        )
        keep = (arrays, price, via_price, ok, col, plated, corner, diag, mask, hole)
        self._last = (key, field, spec, hole_pointer, keep)
        return spec, hole_pointer

    def _buffer(self, size):
        if self._path is None or len(self._path) < size:
            self._path = np.empty(size, dtype=np.int32)
        return self._path

    def search(self, field, starts, ends, via_cost, diagonal, drill_sites):
        from .grid import Cell

        size = field.nlayers * field.nx * field.ny
        context = self._context(size)
        spec, field_hole = self._spec(field, diagonal, via_cost)
        if drill_sites:
            # The tree's new vias: a fresh hole mask for this search only.
            hole = _bytes(field.tree_hole(drill_sites))
            spec.hole = _pointer(hole, ctypes.c_uint8)
        else:
            spec.hole = field_hole
        start = np.asarray(starts, dtype=np.int32)
        end = np.asarray(sorted(ends), dtype=np.int32)
        path = self._buffer(size)
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
        # dense_maze.cell_of_key, per key.
        plane, nx = field.nx * field.ny, field.nx
        out = []
        for key in path[:length].tolist():
            la, ij = divmod(key, plane)
            j, i = divmod(ij, nx)
            out.append(Cell(la, i, j))
        return out

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
    (``$CC``, ``cc`` or ``gcc``) and return the library path. For runners and tests;
    Bazel builds the same source with the same flags. The build records the source's
    sha256 (``PNR_MAZE_SRC_SHA``), so the loader refuses it once ``maze.c`` changes."""
    import shutil
    import subprocess

    compiler = compiler or os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        raise FileNotFoundError("no C compiler (set CC)")
    out = Path(out_dir) / library_names()[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    define = '-DPNR_MAZE_SRC_SHA="%s"' % source_sha256()
    subprocess.run(
        [compiler, *CFLAGS, define, "-o", str(out), str(SOURCE)],
        check=True,
        timeout=timeout,
        capture_output=True,
    )
    return out


def prebuilt():
    """``(path, reason)``: the path of a library that loads and matches this ``maze.c``
    (``PNR_MAZE_LIB``, beside the package, or the installed wheel's) or None, and what the
    loader said. For runners deciding whether to compile one."""
    reset()
    try:
        kernel = load()
        return (None if kernel is None else kernel.path), _STATE["reason"]
    finally:
        reset()


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
            _STATE["kernel"] = NativeKernel(library, path)
            _STATE["reason"] = "loaded " + path.name
            return _STATE["kernel"]
        except (OSError, AttributeError) as error:
            reasons.append("%s: %s" % (path.name, error))
    _STATE["reason"] = "; ".join(reasons) or "library not found"
    return None


def active():
    """The native kernel when it is selected (the default, or ``PNR_MAZE_KERNEL=native``)
    and it loads. An explicit ``PNR_MAZE_KERNEL=native`` without a library says so once on
    stderr; the default falls back quietly (:func:`status` records it either way)."""
    from .maze import maze_kernel

    if maze_kernel() != "native":
        return None
    kernel = load()
    if kernel is None and not _STATE.get("warned") and os.environ.get("PNR_MAZE_KERNEL"):
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
        loaded = load()
        out = dict(kernel="native" if loaded is not None else "packed", reason=_STATE["reason"])
        if loaded is not None:
            out["src_sha256"] = loaded.src_sha
    else:
        out = dict(kernel=kernel, reason="")
    if kernel != "reference" and fallbacks():
        out["reference_fallback"] = fallbacks()
    return out


def main(argv=None):
    """``python -m pnr.route.detail.native_maze build [DIR]``: compile the library (into
    ``native/`` beside the source by default, where the loader and ``run.py`` find it);
    ``status``: which kernel this process would route with, and why."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m pnr.route.detail.native_maze")
    parser.add_argument("command", choices=("build", "status"))
    parser.add_argument("dir", nargs="?", default=str(SOURCE.parent))
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build_library(args.dir))
    else:
        print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
